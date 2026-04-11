import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import generate_session_token
from app.models.session import Session, SessionStatus, Step, StepAction, TaskMemory
from app.services.llm import generate_steps
from app.services.memory import get_memory

router = APIRouter()


# ---------------------------------------------------------------------------
# Booking.com — hardcoded step builder
# ---------------------------------------------------------------------------

def _is_booking_com(url: str) -> bool:
    return "booking.com" in (url or "").lower()


def _build_booking_steps(task: str) -> list[dict]:
    """
    Parse the task string for destination, dates, and guests, then return
    deterministic steps that use special booking:* selectors understood by
    the WebSocket handler.
    """
    task_l = task.lower()

    # --- Destination ---
    dest = ""
    # First try: quoted text like 'Goa' or "Goa"
    m_q = re.search(r"['\"]([^'\"]+)['\"]", task)
    if m_q:
        dest = m_q.group(1).strip()
    if not dest:
        # Second try: "hotels in <place>" — stop at "on", ".", ",", "set", "check", digits
        m = re.search(
            r"(?:destination|enter|search for|hotels? in|stay in|go to)\s+"
            r"([A-Za-z][A-Za-z ]*?)"           # non-greedy capture
            r"(?:\s+on\b|\s+set\b|\s+check|\s+click|\s+from|[.,;]|\d|\s*$)",
            task_l,
        )
        if m:
            dest = m.group(1).strip().title()

    # --- Dates ---
    date_matches = re.findall(r"\d{4}-\d{2}-\d{2}", task)
    checkin = date_matches[0] if len(date_matches) >= 1 else ""
    checkout = date_matches[1] if len(date_matches) >= 2 else ""

    # --- Adults ---
    adults = 2  # default
    m_a = re.search(r"(\d+)\s*adult", task_l)
    if m_a:
        adults = int(m_a.group(1))

    # --- Children ---
    children = 0
    m_c = re.search(r"(\d+)\s*child", task_l)
    if m_c:
        children = int(m_c.group(1))

    # --- Rooms ---
    rooms = 1
    m_r = re.search(r"(\d+)\s*room", task_l)
    if m_r:
        rooms = int(m_r.group(1))

    steps: list[dict] = []
    n = 1

    # Step 1: Navigate
    steps.append({
        "step_number": n, "action": "navigate",
        "selector": None, "url": "https://www.booking.com",
        "instruction": "Opening Booking.com...",
        "prefill_value": None, "is_skippable": False,
    })
    n += 1

    # Step 2: Fill destination
    if dest:
        steps.append({
            "step_number": n, "action": "fill",
            "selector": "booking:destination",
            "url": None,
            "instruction": f"Enter '{dest}' in the destination search box.",
            "prefill_value": dest, "is_skippable": False,
        })
        n += 1

    # Step 3: Pick dates
    if checkin and checkout:
        steps.append({
            "step_number": n, "action": "fill",
            "selector": "booking:dates",
            "url": None,
            "instruction": f"Dates: check-in {checkin} → check-out {checkout}. The value is pre-filled — just press ✓ Done.",
            "prefill_value": f"{checkin} to {checkout}", "is_skippable": False,
        })
        n += 1

    # Step 4: Set guests
    guest_parts = [f"{adults} adult{'s' if adults != 1 else ''}"]
    if children > 0:
        guest_parts.append(f"{children} child{'ren' if children != 1 else ''}")
    if rooms > 1:
        guest_parts.append(f"{rooms} rooms")
    guest_label = ", ".join(guest_parts)

    guest_prefill = f"{adults} adults"
    if children > 0:
        guest_prefill += f", {children} children"
    if rooms > 1:
        guest_prefill += f", {rooms} rooms"

    steps.append({
        "step_number": n, "action": "fill",
        "selector": "booking:guests",
        "url": None,
        "instruction": f"Set guests to {guest_label} using the +/− buttons.",
        "prefill_value": guest_prefill, "is_skippable": False,
    })
    n += 1

    # Step 5: Click search
    steps.append({
        "step_number": n, "action": "click",
        "selector": "booking:search",
        "url": None,
        "instruction": "Click the Search button to find hotels.",
        "prefill_value": None, "is_skippable": False,
    })
    n += 1

    # Step 6: View search results
    steps.append({
        "step_number": n, "action": "wait",
        "selector": "booking:results",
        "url": None,
        "instruction": "Here are the hotel search results! Browse the available options.",
        "prefill_value": None, "is_skippable": False,
    })

    return steps


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class GenerateLinkRequest(BaseModel):
    user_id: str
    task: str
    context: str = ""
    target_url: str = ""   # The exact URL to open — passed to LLM so it won't guess


class GenerateLinkResponse(BaseModel):
    url: str
    token: str
    step_count: int
    from_memory: bool


# ---------------------------------------------------------------------------
# POST /generate-link
# ---------------------------------------------------------------------------

@router.post("/generate-link", response_model=GenerateLinkResponse)
async def generate_link(payload: GenerateLinkRequest, db: AsyncSession = Depends(get_db)):
    """
    Called by teammate's WhatsApp bot when classification = 'funclink'.
    Returns a unique guided session URL.
    """

    # 1. Check Supermemory for existing learned flow
    learned_flow = await get_memory(payload.user_id, payload.task)
    from_memory = learned_flow is not None

    # 2. Generate step plan — hardcoded for Booking.com, LLM for everything else
    if _is_booking_com(payload.target_url):
        steps_data = _build_booking_steps(payload.task)
        from_memory = False  # not from memory — hardcoded
    else:
        steps_data = await generate_steps(
            task=payload.task,
            context=payload.context,
            target_url=payload.target_url,
            learned_flow=learned_flow,
        )

    if not steps_data:
        raise HTTPException(status_code=500, detail="Failed to generate step plan")

    # 3. Create session token
    token = generate_session_token()

    # 4. Determine target URL — prefer caller-provided URL, fall back to LLM's first navigate step
    target_url = payload.target_url or next(
        (s.get("url") for s in steps_data if s.get("action") == "navigate" and s.get("url")),
        None,
    )

    # Fix the first navigate step to use the exact target_url (not the LLM's guess)
    if target_url:
        for s in steps_data:
            if s.get("action") == "navigate":
                s["url"] = target_url
                break

    # 5. Persist session to PostgreSQL
    session = Session(
        token=token,
        user_id=payload.user_id,
        task=payload.task,
        target_url=target_url,
        context=payload.context,
        status=SessionStatus.PENDING,
        current_step=1,
        created_at=datetime.now(timezone.utc),
    )
    db.add(session)

    # 6. Persist each step
    for s in steps_data:
        action_str = s.get("action", "wait").lower()
        try:
            action = StepAction(action_str)
        except ValueError:
            action = StepAction.WAIT

        step = Step(
            session_token=token,
            step_number=s.get("step_number", 1),
            action=action,
            selector=s.get("selector"),
            instruction=s.get("instruction", "Follow the instructions on screen."),
            prefill_value=s.get("prefill_value") or (
                learned_flow.get("prefill", {}).get(s.get("selector")) if learned_flow else None
            ),
            url=s.get("url"),
            is_skippable=s.get("is_skippable", False),
            is_done=False,
        )
        db.add(step)

    await db.commit()

    # 7. Build and return the guide URL
    guide_url = f"{settings.BASE_URL}/f/{token}"

    return GenerateLinkResponse(
        url=guide_url,
        token=token,
        step_count=len(steps_data),
        from_memory=from_memory,
    )
