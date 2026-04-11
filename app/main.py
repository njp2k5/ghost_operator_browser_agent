from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from app.core.database import create_tables


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

# Routers will be registered here in later steps
# from app.api import generate, guide, websocket
# app.include_router(generate.router)
# app.include_router(guide.router)
# app.include_router(websocket.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "FuncLink"}
