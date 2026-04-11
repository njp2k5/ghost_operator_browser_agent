from fastapi import FastAPI
from api.ws import router as ws_router

app = FastAPI()

app.include_router(ws_router)

@app.get("/")
def health():
    return {"status": "ok"}