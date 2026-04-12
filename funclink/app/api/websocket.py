import asyncio
import json
import logging
import traceback

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.session import Session, SessionStatus, Step, StepAction
from app.services import browser as bm
from app.services.llm import replan_remaining_steps, generate_steps_from_scan
from app.services.memory import save_memory

router = APIRouter()
logger = logging.getLogger(__name__)

SCREENSHOT_INTERVAL = 0.12   # seconds between frames — JPEG allows much faster streaming

# Sentinel selector: when encountered, scan the live page and generate real steps
_SCAN_SENTINEL = "__funclink_scan__"


# ---------------------------------------------------------------------------
# Helper — load session + ordered steps from DB
# ---------------------------------------------------------------------------

async def _load_session(db: AsyncSession, token: str) -> Session | None:
    result = await db.execute(
        select(Session).where(Session.token == token)
    )
    return result.scalar_one_or_none()


async def _load_steps(db: AsyncSession, token: str) -> list[dict]:
    """Load steps as plain dicts to avoid detached ORM object issues."""
    result = await db.execute(
        select(Step)
        .where(Step.session_token == token)
        .order_by(Step.step_number)
    )
    rows = result.scalars().all()
    out = []
    for s in rows:
        # Safely extract action as a lowercase string
        if hasattr(s.action, 'value'):
            action = s.action.value
        else:
            action = str(s.action)
        action = action.lower()

        out.append({
            "id": s.id,
            "step_number": s.step_number,
            "action": action,
            "selector": s.selector,
            "instruction": s.instruction,
            "prefill_value": s.prefill_value,
            "url": s.url,
            "is_skippable": s.is_skippable,
            "is_done": s.is_done,
        })
    return out


# ---------------------------------------------------------------------------
# Helper — check if remaining steps match current page
# ---------------------------------------------------------------------------

def _remaining_steps_match_page(remaining: list[dict], page_fields: list[dict]) -> bool:
    """
    Return True if the next fill/select step's selector is found among
    visible page fields.  Only judges by the FIRST fill/select step
    (click/wait steps are never a mismatch signal).
    """
    page_labels = {f.get("label", "").lower().strip() for f in page_fields if f.get("label")}
    for step in remaining:
        action = step.get("action", "")
        selector = (step.get("selector") or "").lower().strip()
        if action in ("fill", "select") and selector:
            # Require that the selector matches a label as a whole-word
            # (not just substring — avoids 'submit' matching 'subjects').
            found = any(
                selector == plbl                            # exact
                or (len(selector) > 3 and plbl.startswith(selector))  # label starts with selector
                or (len(plbl) > 3 and selector.startswith(plbl))     # selector starts with label
                for plbl in page_labels
            )
            return found  # judge by the FIRST fill/select step only
    # Only click/wait/navigate steps remain — not a mismatch
    return True


# ---------------------------------------------------------------------------
# Helper — replan: delete remaining DB steps, insert new ones from LLM
# ---------------------------------------------------------------------------

async def _do_replan(
    token: str,
    task: str,
    completed_steps: list[dict],
    page_fields: list[dict],
    page_url: str,
    next_step_num: int,
) -> list[dict]:
    """
    Call the LLM to generate new remaining steps, persist them to DB,
    and return the new steps as dicts (same format as _load_steps).
    """
    # Only send EMPTY (unfilled) fields to the LLM — prevents it from
    # regenerating steps for fields the user already completed.
    empty_fields = [
        f for f in page_fields
        if not f.get("value")                       # text inputs with no value
        or f.get("type") in ("click",)               # buttons are always shown
    ]
    logger.info(f"[WS:{token}] Replan: {len(page_fields)} visible fields, {len(empty_fields)} empty")

    new_raw_steps = await replan_remaining_steps(
        task=task,
        completed_steps=completed_steps,
        visible_fields=empty_fields,
        page_url=page_url,
        next_step_number=next_step_num,
    )
    logger.info(f"[WS:{token}] LLM replan returned {len(new_raw_steps)} raw steps")

    # ---- Validate: keep only steps whose selector is actually visible on page ----
    # This prevents the LLM from hallucinating fields that don't exist yet.
    if page_fields:
        visible_labels = {f.get("label", "").lower().strip() for f in page_fields if f.get("label")}

        def _selector_visible(step: dict) -> bool:
            sel = (step.get("selector") or "").lower().strip()
            action = step.get("action", "")
            if not sel or action in ("navigate", "wait"):
                return True  # non-field steps always pass
            # Accept if any visible label contains selector OR selector contains label
            return any(sel in lbl or lbl in sel for lbl in visible_labels)

        validated = [s for s in new_raw_steps if _selector_visible(s)]
        if validated:
            new_raw_steps = validated
            logger.info(f"[WS:{token}] After validation: {len(new_raw_steps)} steps kept (visible fields: {list(visible_labels)[:8]})")
        else:
            # All were hallucinated — fall back to a generic wait step
            logger.warning(f"[WS:{token}] All replanned steps failed validation — generating wait step")
            new_raw_steps = [{
                "step_number": next_step_num,
                "action": "wait",
                "selector": None,
                "instruction": "Follow the instructions shown on the page.",
                "url": None,
                "prefill_value": None,
            }]

    logger.info(f"[WS:{token}] Final replan: {len(new_raw_steps)} steps")

    # ---- Filter out steps that duplicate already-completed selectors ----
    if completed_steps:
        done_selectors = [
            (s.get("selector") or "").lower().strip()
            for s in completed_steps
            if s.get("selector")
        ]

        def _already_done(step: dict) -> bool:
            """Fuzzy check: is this step's selector already completed?"""
            act = step.get("action", "")
            if act in ("navigate", "wait"):
                return False  # never filter these
            sel = (step.get("selector") or "").lower().strip()
            if not sel:
                return False
            for ds in done_selectors:
                if not ds:
                    continue
                # Exact, or either contains the other (fuzzy)
                if sel == ds or sel in ds or ds in sel:
                    return True
            return False

        before_len = len(new_raw_steps)
        new_raw_steps = [s for s in new_raw_steps if not _already_done(s)]
        if len(new_raw_steps) < before_len:
            logger.info(f"[WS:{token}] Filtered {before_len - len(new_raw_steps)} duplicate steps (already completed)")
        # Renumber
        for i, s in enumerate(new_raw_steps):
            s["step_number"] = next_step_num + i

    # Persist to DB: delete old remaining, insert new
    new_step_dicts = []
    async with AsyncSessionLocal() as db:
        # Delete all undone steps for this session
        await db.execute(
            delete(Step).where(Step.session_token == token, Step.is_done == False)
        )

        for s in new_raw_steps:
            action_str = s.get("action", "wait").lower()
            try:
                action_enum = StepAction(action_str)
            except ValueError:
                action_enum = StepAction.WAIT

            step = Step(
                session_token=token,
                step_number=s.get("step_number", next_step_num),
                action=action_enum,
                selector=s.get("selector"),
                instruction=s.get("instruction", "Follow the instructions on screen."),
                prefill_value=s.get("prefill_value"),
                url=s.get("url"),
                is_skippable=s.get("is_skippable", False),
                is_done=False,
            )
            db.add(step)
            # We need the ID after flush
            await db.flush()

            new_step_dicts.append({
                "id": step.id,
                "step_number": step.step_number,
                "action": action_str,
                "selector": step.selector,
                "instruction": step.instruction,
                "prefill_value": step.prefill_value,
                "url": step.url,
                "is_skippable": step.is_skippable,
                "is_done": False,
            })

        await db.commit()

    return new_step_dicts


# ---------------------------------------------------------------------------
# WebSocket  /ws/{token}
# ---------------------------------------------------------------------------

@router.websocket("/ws/{token}")
async def websocket_session(websocket: WebSocket, token: str):
    await websocket.accept()
    logger.info(f"[WS:{token}] WebSocket accepted.")

    # ----------------------------------------------------------------
    # 1. Load session + steps from DB
    # ----------------------------------------------------------------
    try:
        async with AsyncSessionLocal() as db:
            session_row = await _load_session(db, token)
            if not session_row:
                logger.warning(f"[WS:{token}] Session not found in DB.")
                await websocket.send_json({"type": "error", "message": "Session not found or link is invalid."})
                await websocket.close()
                return

            steps = await _load_steps(db, token)
            logger.info(f"[WS:{token}] Loaded {len(steps)} steps from DB.")
            if not steps:
                await websocket.send_json({"type": "error", "message": "No steps found. Please regenerate the link."})
                await websocket.close()
                return

            # Copy scalar values before the DB session closes
            s_user_id    = session_row.user_id
            s_task       = session_row.task
            s_target_url = session_row.target_url

            # Mark session as ACTIVE
            await db.execute(
                update(Session)
                .where(Session.token == token)
                .values(status=SessionStatus.ACTIVE)
            )
            await db.commit()
            logger.info(f"[WS:{token}] Session marked ACTIVE. target_url={s_target_url}")

    except Exception as e:
        logger.error(f"[WS:{token}] DB error during setup: {e}\n{traceback.format_exc()}")
        try:
            await websocket.send_json({"type": "error", "message": f"Database error: {e}"})
        except Exception:
            pass
        return

    # ----------------------------------------------------------------
    # 2. Launch browser
    # ----------------------------------------------------------------
    # Always clean up any stale session for this token first
    try:
        await bm.end_session(token)
    except Exception:
        pass

    browser_session = None
    for _attempt in range(2):  # retry once on transient failure
        try:
            await websocket.send_json({"type": "status", "message": "Launching browser..."})
            logger.info(f"[WS:{token}] Launching Playwright browser (attempt {_attempt+1}) for: {s_target_url}")

            browser_session = await bm.start_session(token, s_target_url)
            logger.info(f"[WS:{token}] Browser launched successfully.")
            break  # success

        except Exception as e:
            err_detail = repr(e) if not str(e).strip() else str(e)
            logger.error(f"[WS:{token}] Browser launch FAILED (attempt {_attempt+1}): {err_detail}\n{traceback.format_exc()}")
            if _attempt == 1:  # both attempts failed
                try:
                    await websocket.send_json({"type": "error", "message": f"Failed to launch browser: {err_detail}"})
                except Exception:
                    pass
                return
            await asyncio.sleep(2)  # brief pause before retry

    # ----------------------------------------------------------------
    # 3. Process steps one by one
    # ----------------------------------------------------------------
    try:
        prefill_values_collected: dict = {}
        total_steps = len(steps)

        await websocket.send_json({"type": "status", "message": f"Browser ready. Processing {total_steps} steps..."})

        # Use index-based loop so we can swap the step list mid-flight
        step_idx = 0
        last_frame_hash = ""  # skip sending duplicate frames
        # Replan guard: after a replan, trust the new steps for their full batch
        # without re-running element_exists (prevents infinite replan loops).
        replan_grace_remaining = 0   # how many replanned steps still get a free pass
        last_replan_url = ""         # never replan on the same URL twice in a row
        while step_idx < len(steps):
            step = steps[step_idx]
            step_num    = step["step_number"]
            action      = step["action"]
            selector    = step["selector"]
            instruction = step["instruction"]
            prefill     = step["prefill_value"]
            url         = step["url"]
            is_skip     = step["is_skippable"]
            step_id     = step["id"]
            total_steps = len(steps)  # refresh — may change after replan

            if step["is_done"]:
                logger.info(f"[WS:{token}] Step {step_num} already done, skipping.")
                step_idx += 1
                continue

            # ----- Scan sentinel: scan the live page, generate real steps -----
            if selector == _SCAN_SENTINEL:
                logger.info(f"[WS:{token}] Scan sentinel hit — scanning page for real fields...")
                try:
                    await websocket.send_json({"type": "status", "message": "🔍 Analyzing the page to build your steps..."})
                    page_fields = await bm.scan_page_fields(browser_session)
                    current_url = browser_session.page.url

                    if page_fields:
                        logger.info(f"[WS:{token}] Scan found {len(page_fields)} fields: {[f.get('label') for f in page_fields[:10]]}")
                        new_raw = await generate_steps_from_scan(
                            task=s_task,
                            target_url=current_url,
                            visible_fields=page_fields,
                            next_step_number=step_num,
                        )
                    else:
                        # No fields found — LLM fallback using task + URL only
                        logger.warning(f"[WS:{token}] Scan found no fields — falling back to LLM with URL context")
                        from app.services.llm import generate_steps as _gen_steps
                        new_raw = await _gen_steps(task=s_task, context="", target_url=current_url)
                        # Strip the navigate step since we're already there
                        new_raw = [s for s in new_raw if s.get("action") != "navigate"]
                        for i, s in enumerate(new_raw):
                            s["step_number"] = step_num + i

                    if not new_raw:
                        logger.warning(f"[WS:{token}] Scan-based generation returned empty — skipping sentinel.")
                        step_idx += 1
                        continue

                    # Persist: delete sentinel + any remaining undone steps, insert new ones
                    new_step_dicts = []
                    async with AsyncSessionLocal() as db:
                        await db.execute(
                            delete(Step).where(Step.session_token == token, Step.is_done == False)
                        )
                        for s in new_raw:
                            action_str = s.get("action", "wait").lower()
                            try:
                                action_enum = StepAction(action_str)
                            except ValueError:
                                action_enum = StepAction.WAIT
                            new_step = Step(
                                session_token=token,
                                step_number=s.get("step_number", step_num),
                                action=action_enum,
                                selector=s.get("selector"),
                                instruction=s.get("instruction", "Follow the on-screen instructions."),
                                prefill_value=s.get("prefill_value"),
                                url=s.get("url"),
                                is_skippable=s.get("is_skippable", False),
                                is_done=False,
                            )
                            db.add(new_step)
                            await db.flush()
                            new_step_dicts.append({
                                "id": new_step.id,
                                "step_number": new_step.step_number,
                                "action": action_str,
                                "selector": new_step.selector,
                                "instruction": new_step.instruction,
                                "prefill_value": new_step.prefill_value,
                                "url": new_step.url,
                                "is_skippable": new_step.is_skippable,
                                "is_done": False,
                            })
                        await db.commit()

                    # Replace remaining steps in memory
                    steps = [s for s in steps if s["is_done"]] + new_step_dicts
                    step_idx = next((i for i, s in enumerate(steps) if not s["is_done"]), len(steps))
                    replan_grace_remaining = len(new_step_dicts)
                    logger.info(f"[WS:{token}] Scan complete: {len(new_step_dicts)} real steps generated.")
                    await websocket.send_json({
                        "type": "replan",
                        "message": f"Ready — {len(new_step_dicts)} steps to complete your task.",
                        "new_total": len(steps),
                    })
                    continue  # restart loop with real steps

                except Exception as scan_err:
                    logger.error(f"[WS:{token}] Scan sentinel failed: {scan_err}\n{traceback.format_exc()}")
                    step_idx += 1
                    continue



            # Detect Booking.com specialist selectors (booking:destination, etc.)
            is_booking_step = (selector or "").startswith("booking:")
            booking_key = selector.split(":", 1)[1] if is_booking_step else ""

            # ----- Check if the field actually exists on the current page -----
            field_found = True
            current_page_url = browser_session.page.url
            if is_booking_step:
                # Booking.com steps use exact data-testid selectors — skip element_exists
                field_found = True
            elif replan_grace_remaining > 0:
                # Still inside a replan grace window — trust the new steps, no element_exists check
                logger.info(f"[WS:{token}] Grace period active ({replan_grace_remaining} left) — skipping element_exists for '{selector}'")
            elif selector and action in ("fill", "select", "click"):
                field_found = await bm.element_exists(browser_session, selector)

            # ----- If field not found → trigger dynamic replan (once per URL) -----
            if not field_found:
                if current_page_url == last_replan_url:
                    # Already replanned on this page — don't loop. Skip this step instead.
                    logger.warning(f"[WS:{token}] Field '{selector}' still missing after replan on same URL — skipping step.")
                    step["is_done"] = True
                    try:
                        async with AsyncSessionLocal() as db:
                            await db.execute(update(Step).where(Step.id == step_id).values(is_done=True))
                            await db.commit()
                    except Exception:
                        pass
                    step_idx += 1
                    continue

                logger.warning(f"[WS:{token}] Field '{selector}' NOT FOUND — triggering replan...")
                try:
                    await websocket.send_json({"type": "status", "message": "🔄 Page changed — replanning steps..."})
                    page_fields = await bm.scan_page_fields(browser_session)
                    page_url = current_page_url

                    # Gather completed steps (everything before current index that's done)
                    completed = [s for s in steps if s["is_done"]]

                    new_steps = await _do_replan(
                        token=token,
                        task=s_task,
                        completed_steps=completed,
                        page_fields=page_fields,
                        page_url=page_url,
                        next_step_num=step_num,
                    )

                    if new_steps:
                        # Replace remaining portion of the step list
                        steps = [s for s in steps if s["is_done"]] + new_steps
                        total_steps = len(steps)
                        # Find the new index to continue from
                        step_idx = next(
                            (i for i, s in enumerate(steps) if not s["is_done"]),
                            len(steps),
                        )
                        # Set grace period — trust ALL new replanned steps without element_exists
                        replan_grace_remaining = len(new_steps)
                        last_replan_url = page_url
                        logger.info(f"[WS:{token}] Replan complete: {len(new_steps)} new steps, grace={replan_grace_remaining}, url={page_url}")
                        await websocket.send_json({
                            "type": "replan",
                            "message": f"Adjusted plan — {len(new_steps)} steps remaining.",
                            "new_total": total_steps,
                        })
                        continue  # restart loop with new steps
                    else:
                        logger.warning(f"[WS:{token}] Replan returned empty — skipping step.")
                        step_idx += 1
                        continue
                except Exception as replan_err:
                    logger.error(f"[WS:{token}] Replan failed: {replan_err}\n{traceback.format_exc()}")
                    step_idx += 1
                    continue

            # ----- Execute the step action (each wrapped individually) -----
            try:
                if action == "navigate" and url:
                    # Skip redundant navigation if already on the target URL
                    current_url = browser_session.page.url
                    if url.rstrip("/") in current_url or current_url.startswith(url.rstrip("/")):
                        logger.info(f"[WS:{token}] Already on {url}, skipping redundant navigate")
                    else:
                        await websocket.send_json({"type": "status", "message": f"Opening {url}..."})
                        result = await bm.navigate_to(browser_session, url)
                        logger.info(f"[WS:{token}] navigate result: {result}")
                        await bm.wait_for_page_stable(browser_session)
                    # Dismiss Booking.com overlays (sign-in banner, cookies) after page load
                    if bm.is_booking_com(url) or bm.is_booking_com(current_url):
                        await bm.booking_dismiss_overlays(browser_session)
                        logger.info(f"[WS:{token}] Dismissed Booking.com overlays after navigate")

                elif is_booking_step and selector:
                    # Booking.com step: highlight via data-testid
                    await bm.booking_highlight_step(browser_session, booking_key)
                    logger.info(f"[WS:{token}] Booking: highlighted '{booking_key}'")

                elif action in ("fill", "select", "click", "highlight") and selector:
                    # Only highlight during presentation — actual fill/select/click
                    # happens AFTER the user clicks Done (prevents double-typing and
                    # cursor-drift that confused earlier versions).
                    await bm.highlight_element(browser_session, selector)
                    logger.info(f"[WS:{token}] Highlighted '{selector}' for {action}")

                elif action == "wait":
                    logger.info(f"[WS:{token}] Wait step, showing instruction only.")

                else:
                    logger.warning(f"[WS:{token}] Unknown action '{action}' or missing selector/url.")

            except Exception as step_err:
                logger.warning(f"[WS:{token}] Step {step_num} action error (non-fatal): {step_err}")

            # ----- Send step info + screenshot to frontend -----
            try:
                screenshot = await bm.take_screenshot(browser_session)
                await websocket.send_json({
                    "type": "step",
                    "step_number": step_idx + 1,  # 1-based display number
                    "total_steps": total_steps,
                    "action": action,
                    "selector": selector,
                    "instruction": instruction,
                    "prefill_value": prefill or "",
                    "is_skippable": is_skip,
                    "screenshot": screenshot,
                })
                logger.info(f"[WS:{token}] Sent step {step_num} to frontend (screenshot={len(screenshot)} chars)")
            except WebSocketDisconnect:
                logger.info(f"[WS:{token}] Client disconnected while sending step.")
                return
            except Exception as e:
                logger.error(f"[WS:{token}] Failed to send step {step_num}: {e}")
                return

            # ----- Stream screenshots until user says done -----
            done = False
            user_value = None
            while not done:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=SCREENSHOT_INTERVAL,
                    )
                    msg = json.loads(raw)
                    if msg.get("type") == "action_done":
                        user_value = msg.get("value")
                        if user_value and selector:
                            prefill_values_collected[selector] = user_value
                        done = True
                        logger.info(f"[WS:{token}] User completed step {step_num}. value={user_value!r}")
                except asyncio.TimeoutError:
                    try:
                        screenshot = await bm.take_screenshot(browser_session)
                        if screenshot and screenshot != last_frame_hash:
                            last_frame_hash = screenshot
                            await websocket.send_json({
                                "type": "frame",
                                "screenshot": screenshot,
                            })
                    except WebSocketDisconnect:
                        logger.info(f"[WS:{token}] Client disconnected during streaming.")
                        return
                    except Exception as ss_err:
                        logger.warning(f"[WS:{token}] Screenshot stream error: {ss_err}")
                except WebSocketDisconnect:
                    logger.info(f"[WS:{token}] Client disconnected while waiting for action_done.")
                    return

            # ----- After user presses Done: execute the real action -----
            needs_replan_check = False
            try:
                if is_booking_step and selector:
                    # Booking.com specialist execution
                    value_to_use = user_value or prefill or ""
                    ok = await bm.booking_execute_step(browser_session, booking_key, value_to_use)
                    logger.info(f"[WS:{token}] Booking execute '{booking_key}' => {ok}")
                    # Extra wait for search results page to fully load
                    if booking_key == "search":
                        await asyncio.sleep(3.0)
                    else:
                        await asyncio.sleep(0.5)
                    screenshot = await bm.take_screenshot(browser_session)
                    if screenshot:
                        await websocket.send_json({"type": "frame", "screenshot": screenshot})
                    needs_replan_check = False  # never replan booking steps

                elif action == "fill" and selector:
                    value_to_type = user_value or prefill or ""
                    if value_to_type:
                        await bm.prefill_input(browser_session, selector, value_to_type)
                        logger.info(f"[WS:{token}] Typed '{value_to_type}' into '{selector}'")
                        await asyncio.sleep(0.3)
                        screenshot = await bm.take_screenshot(browser_session)
                        if screenshot:
                            await websocket.send_json({"type": "frame", "screenshot": screenshot})

                elif action == "select" and selector:
                    value_to_select = user_value or prefill or ""
                    if value_to_select:
                        await bm.select_option(browser_session, selector, value_to_select)
                        logger.info(f"[WS:{token}] Selected '{value_to_select}' for '{selector}'")
                        await asyncio.sleep(0.5)
                        screenshot = await bm.take_screenshot(browser_session)
                        if screenshot:
                            await websocket.send_json({"type": "frame", "screenshot": screenshot})

                elif action == "click" and selector:
                    await bm.click_element(browser_session, selector)
                    logger.info(f"[WS:{token}] Clicked '{selector}'")
                    await bm.wait_for_page_stable(browser_session)
                    await asyncio.sleep(1.0)
                    screenshot = await bm.take_screenshot(browser_session)
                    if screenshot:
                        await websocket.send_json({"type": "frame", "screenshot": screenshot})
                    needs_replan_check = True  # click may have changed the page

            except Exception as post_err:
                logger.warning(f"[WS:{token}] Post-action error (non-fatal): {post_err}")

            # ----- Mark step done in DB -----
            step["is_done"] = True  # update in-memory too
            if replan_grace_remaining > 0:
                replan_grace_remaining -= 1
                logger.info(f"[WS:{token}] Grace remaining after step: {replan_grace_remaining}")
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(Step).where(Step.id == step_id).values(is_done=True)
                    )
                    await db.execute(
                        update(Session).where(Session.token == token).values(current_step=step_num + 1)
                    )
                    await db.commit()
            except Exception as db_err:
                logger.warning(f"[WS:{token}] DB update after step {step_num} failed: {db_err}")

            # ----- After click: proactively check if remaining steps match the new page -----
            # Only run if (a) grace period is exhausted, (b) URL actually changed after click
            # Booking.com steps never trigger replan — they're hardcoded.
            if needs_replan_check and replan_grace_remaining == 0 and not is_booking_step:
                post_click_url = browser_session.page.url
                # Skip if URL is the same (same-page interaction, e.g. radio/checkbox click)
                if post_click_url == current_page_url:
                    logger.info(f"[WS:{token}] Post-click: same page URL, no replan needed.")
                elif post_click_url == last_replan_url:
                    logger.info(f"[WS:{token}] Post-click: same URL as last replan, skipping mismatch check.")
                else:
                    remaining = [s for s in steps if not s["is_done"]]
                    if remaining:
                        page_fields = await bm.scan_page_fields(browser_session)
                        if page_fields and not _remaining_steps_match_page(remaining, page_fields):
                            logger.info(f"[WS:{token}] Post-click page mismatch detected — replanning...")
                            try:
                                await websocket.send_json({"type": "status", "message": "🔄 Page changed — replanning steps..."})
                                completed = [s for s in steps if s["is_done"]]
                                next_num = step_num + 1

                                new_steps = await _do_replan(
                                    token=token,
                                    task=s_task,
                                    completed_steps=completed,
                                    page_fields=page_fields,
                                    page_url=post_click_url,
                                    next_step_num=next_num,
                                )
                                if new_steps:
                                    steps = [s for s in steps if s["is_done"]] + new_steps
                                    total_steps = len(steps)
                                    step_idx = next(
                                        (i for i, s in enumerate(steps) if not s["is_done"]),
                                        len(steps),
                                    )
                                    replan_grace_remaining = len(new_steps)
                                    last_replan_url = post_click_url
                                    logger.info(f"[WS:{token}] Post-click replan: {len(new_steps)} new steps, grace={replan_grace_remaining}")
                                    await websocket.send_json({
                                        "type": "replan",
                                        "message": f"Adjusted plan — {len(new_steps)} steps remaining.",
                                        "new_total": total_steps,
                                    })
                                    continue  # restart loop with new steps
                            except Exception as rp_err:
                                logger.warning(f"[WS:{token}] Post-click replan failed: {rp_err}")

            step_idx += 1

        # ----------------------------------------------------------------
        # 4. All steps done — mark session COMPLETE
        # ----------------------------------------------------------------
        from datetime import datetime, timezone
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Session)
                    .where(Session.token == token)
                    .values(
                        status=SessionStatus.COMPLETE,
                        completed_at=datetime.now(timezone.utc),
                    )
                )
                await db.commit()
            logger.info(f"[WS:{token}] Session marked COMPLETE.")
        except Exception as e:
            logger.warning(f"[WS:{token}] Failed to mark session complete: {e}")

        # 5. Save to Supermemory (non-fatal)
        try:
            await save_memory(
                user_id=s_user_id,
                task=s_task,
                steps=steps,
                prefill_values=prefill_values_collected,
            )
            logger.info(f"[WS:{token}] Memory saved to Supermemory.")
        except Exception as e:
            logger.warning(f"[WS:{token}] Supermemory save failed: {e}")

        # 6. Notify teammate webhook (non-fatal)
        await _notify_teammate_raw(s_user_id, s_task, token)

        # 7. Tell frontend session is complete
        try:
            await websocket.send_json({
                "type": "complete",
                "message": "All done! Your task is complete.",
            })
        except Exception:
            pass

    except WebSocketDisconnect:
        logger.info(f"[WS:{token}] WebSocket disconnected.")
    except Exception as e:
        logger.error(f"[WS:{token}] UNEXPECTED ERROR: {e}\n{traceback.format_exc()}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        logger.info(f"[WS:{token}] Cleaning up browser session.")
        await bm.end_session(token)


# ---------------------------------------------------------------------------
# Notify teammate when session completes
# ---------------------------------------------------------------------------

async def _notify_teammate_raw(user_id: str, task: str, token: str):
    webhook = settings.TEAMMATE_WEBHOOK_URL
    if not webhook:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook, json={
                "user_id": user_id,
                "status": "complete",
                "task": task,
                "token": token,
            })
    except Exception as e:
        logger.warning(f"Teammate webhook failed: {e}")
