from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from pydantic import BaseModel, Field

from api.housing_ws import router as housing_ws_router
from api.hindu_ws import router as hindu_ws_router
from api.irctc_ws import router as irctc_ws_router
from api.linkedin_ws import router as linkedin_ws_router  # file retained; now serves /ws/olx
from api.practo_ws import router as practo_ws_router
from api.ws import router as ws_router
from tool_registry import load_builtin_tools
from tool_registry.executor import execute_tool


class ToolExecutionRequest(BaseModel):
    tool: str = Field(..., description="Registered tool name")
    params: dict = Field(default_factory=dict, description="Tool input parameters")


load_builtin_tools()

app = FastAPI()

app.include_router(housing_ws_router)
app.include_router(practo_ws_router)
app.include_router(hindu_ws_router)
app.include_router(irctc_ws_router)  # specific routes first - must precede wildcard /ws/{sender}
app.include_router(linkedin_ws_router)  # specific routes first - must precede wildcard /ws/{sender}
app.include_router(ws_router)


@app.get("/")
def health():
    return {"status": "ok"}


class FuncLinkWebhookPayload(BaseModel):
    user_id: str
    status: str
    task: str
    token: str


@app.post("/webhook/funclink")
async def funclink_webhook(payload: FuncLinkWebhookPayload):
    """Receive session-complete notifications from FuncLink."""
    import logging
    log = logging.getLogger("ghost_operator.funclink_webhook")
    log.info(
        "[webhook] user=%s status=%s token=%s task=%r",
        payload.user_id, payload.status, payload.token, payload.task,
    )
    if payload.status == "complete":
        from core.websocket_manager import manager
        await manager.send(
            payload.user_id,
            f"✅ Your guided session on *{payload.task}* is complete!\n"
            f"Hope everything went smoothly. Let me know if you need anything else.",
        )
    return {"received": True}


@app.post("/test/tool")
async def test_tool(payload: ToolExecutionRequest):
    return await execute_tool(payload.tool, payload.params)