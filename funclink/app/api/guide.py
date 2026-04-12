from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.session import Session, SessionStatus, Step

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/f/{token}", response_class=HTMLResponse)
async def guide_page(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    """Serve the guided browser session page to the customer."""
    result = await db.execute(select(Session).where(Session.token == token))
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found or link is invalid.")

    if session.status == SessionStatus.EXPIRED:
        raise HTTPException(status_code=410, detail="This link has expired.")

    if session.status == SessionStatus.COMPLETE:
        return HTMLResponse(content=_already_done_page(session.task), status_code=200)

    # If session was ACTIVE (from a failed/retried attempt), reset it to PENDING
    # and mark all steps as not done so the session can restart cleanly
    if session.status == SessionStatus.ACTIVE:
        await db.execute(
            update(Session).where(Session.token == token).values(
                status=SessionStatus.PENDING, current_step=1
            )
        )
        await db.execute(
            update(Step).where(Step.session_token == token).values(is_done=False)
        )
        await db.commit()

    return templates.TemplateResponse("guide.html", {
        "request": request,
        "token": token,
        "task": session.task,
        "ws_url": f"ws://localhost:8000/ws/{token}",  # overridden by JS using window.location
    })


def _already_done_page(task: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>FuncLink — Done</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.tailwindcss.com"></script></head>
    <body class="bg-gray-950 text-white flex items-center justify-center min-h-screen">
      <div class="text-center p-8">
        <div class="text-6xl mb-4">✅</div>
        <h1 class="text-2xl font-bold mb-2">Already Completed</h1>
        <p class="text-gray-400">The task <strong class="text-white">"{task}"</strong> was already completed.</p>
      </div>
    </body>
    </html>
    """
