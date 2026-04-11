from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from pydantic import BaseModel, Field

from api.hindu_ws import router as hindu_ws_router
from api.linkedin_ws import router as linkedin_ws_router  # file retained; now serves /ws/olx
from api.ws import router as ws_router
from tool_registry.executor import execute_tool


class ToolExecutionRequest(BaseModel):
    tool: str = Field(..., description="Registered tool name")
    params: dict = Field(default_factory=dict, description="Tool input parameters")

app = FastAPI()

app.include_router(hindu_ws_router)
app.include_router(linkedin_ws_router)  # specific routes first — must precede wildcard /ws/{sender}
app.include_router(ws_router)


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/test/tool")
async def test_tool(payload: ToolExecutionRequest):
    return await execute_tool(payload.tool, payload.params)