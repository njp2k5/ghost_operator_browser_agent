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

    # 2. Generate step plan via Groq LLM
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
