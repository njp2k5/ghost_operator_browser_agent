import asyncio
import json
import logging
import traceback

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.session import Session, SessionStatus, Step, StepAction
from app.services import browser as bm
from app.services.memory import save_memory

router = APIRouter()
logger = logging.getLogger(__name__)

SCREENSHOT_INTERVAL = 0.35   # seconds between frames while waiting for user


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
    browser_session = None
    try:
        await websocket.send_json({"type": "status", "message": "Launching browser..."})
        logger.info(f"[WS:{token}] Launching Playwright browser for: {s_target_url}")

        browser_session = await bm.start_session(token, s_target_url)
        logger.info(f"[WS:{token}] Browser launched successfully.")

    except Exception as e:
        logger.error(f"[WS:{token}] Browser launch FAILED: {e}\n{traceback.format_exc()}")
        try:
            await websocket.send_json({"type": "error", "message": f"Failed to launch browser: {e}"})
        except Exception:
            pass
        return

    # ----------------------------------------------------------------
    # 3. Process steps one by one
    # ----------------------------------------------------------------
    try:
        prefill_values_collected: dict = {}
        total_steps = len(steps)

        await websocket.send_json({"type": "status", "message": f"Browser ready. Processing {total_steps} steps..."})

        for step in steps:
            step_num    = step["step_number"]
            action      = step["action"]
            selector    = step["selector"]
            instruction = step["instruction"]
            prefill     = step["prefill_value"]
            url         = step["url"]
            is_skip     = step["is_skippable"]
            step_id     = step["id"]

            if step["is_done"]:
                logger.info(f"[WS:{token}] Step {step_num} already done, skipping.")
                continue

            logger.info(f"[WS:{token}] === Step {step_num}/{total_steps}: action={action}, selector={selector}, url={url}")

            # ----- Execute the step action (each wrapped individually) -----
            try:
                if action == "navigate" and url:
                    await websocket.send_json({"type": "status", "message": f"Opening {url}..."})
                    result = await bm.navigate_to(browser_session, url)
                    logger.info(f"[WS:{token}] navigate result: {result}")

                elif action == "fill" and selector:
                    # Highlight the field, prefill if value provided
                    await bm.highlight_element(browser_session, selector)
                    if prefill:
                        await bm.prefill_input(browser_session, selector, prefill)
                        prefill_values_collected[selector] = prefill
                        logger.info(f"[WS:{token}] prefilled '{selector}' with '{prefill}'")

                elif action == "select" and selector:
                    # Highlight the field; auto-select if prefill value exists
                    await bm.highlight_element(browser_session, selector)
                    if prefill:
                        await bm.select_option(browser_session, selector, prefill)
                        prefill_values_collected[selector] = prefill
                        logger.info(f"[WS:{token}] selected '{prefill}' in '{selector}'")

                elif action == "click" and selector:
                    await bm.highlight_element(browser_session, selector)
                    logger.info(f"[WS:{token}] highlighted click target '{selector}'")

                elif action == "highlight" and selector:
                    # Legacy highlight steps — just highlight
                    await bm.highlight_element(browser_session, selector)
                    logger.info(f"[WS:{token}] highlight '{selector}'")

                elif action == "wait":
                    logger.info(f"[WS:{token}] Wait step, showing instruction only.")

                else:
                    logger.warning(f"[WS:{token}] Unknown action '{action}' or missing selector/url.")

            except Exception as step_err:
                logger.warning(f"[WS:{token}] Step {step_num} action error (non-fatal): {step_err}")
                # Continue anyway — still show the step to user

            # ----- Send step info + screenshot to frontend -----
            try:
                screenshot = await bm.take_screenshot(browser_session)
                await websocket.send_json({
                    "type": "step",
                    "step_number": step_num,
                    "total_steps": total_steps,
                    "action": action,
                    "selector": selector,
                    "instruction": instruction,
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
                    # Send a new screenshot frame
                    try:
                        screenshot = await bm.take_screenshot(browser_session)
                        if screenshot:
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
            try:
                if action == "fill" and selector:
                    # Type the user's value (or prefill) into the browser field
                    value_to_type = user_value or prefill or ""
                    if value_to_type:
                        await bm.prefill_input(browser_session, selector, value_to_type)
                        logger.info(f"[WS:{token}] Typed '{value_to_type}' into '{selector}'")
                        await asyncio.sleep(0.3)
                        screenshot = await bm.take_screenshot(browser_session)
                        if screenshot:
                            await websocket.send_json({"type": "frame", "screenshot": screenshot})

                elif action == "select" and selector:
                    # Select the user's value or prefill in dropdown/radio
                    value_to_select = user_value or prefill or ""
                    if value_to_select:
                        await bm.select_option(browser_session, selector, value_to_select)
                        logger.info(f"[WS:{token}] Selected '{value_to_select}' for '{selector}'")
                        await asyncio.sleep(0.5)
                        screenshot = await bm.take_screenshot(browser_session)
                        if screenshot:
                            await websocket.send_json({"type": "frame", "screenshot": screenshot})

                elif action == "click" and selector:
                    # Actually click the element in the browser
                    await bm.click_element(browser_session, selector)
                    logger.info(f"[WS:{token}] Clicked '{selector}'")
                    # Wait for page to react
                    await asyncio.sleep(1.5)
                    screenshot = await bm.take_screenshot(browser_session)
                    if screenshot:
                        await websocket.send_json({"type": "frame", "screenshot": screenshot})

            except Exception as post_err:
                logger.warning(f"[WS:{token}] Post-action error (non-fatal): {post_err}")

            # ----- Mark step done in DB -----
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(Step)
                        .where(Step.id == step_id)
                        .values(is_done=True)
                    )
                    await db.execute(
                        update(Session)
                        .where(Session.token == token)
                        .values(current_step=step_num + 1)
                    )
                    await db.commit()
            except Exception as db_err:
                logger.warning(f"[WS:{token}] DB update after step {step_num} failed: {db_err}")

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
