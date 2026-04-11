import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# ── Windows: force ProactorEventLoop so Playwright can spawn Chromium ──────────
# SelectorEventLoop (uvicorn default on Windows) does NOT support subprocess
# transport, which causes NotImplementedError() when Playwright tries to launch.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
# ────────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from app.core.database import create_tables

# Configure root logger so all our logger.info/warning/error calls appear in console
logging.basicConfig(level=logging.INFO, format="%(levelname)s:  %(name)s - %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables if they don't exist (dev convenience)
    await create_tables()
    yield
    # Shutdown: nothing to clean up yet


app = FastAPI(
    title="FuncLink",
    description="Guided browser automation sessions via shareable links",
    version="0.1.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="app/templates")

from app.api import generate, guide, websocket
app.include_router(generate.router)
app.include_router(guide.router)
app.include_router(websocket.router)



@app.get("/health")
async def health():
    return {"status": "ok", "service": "FuncLink"}
