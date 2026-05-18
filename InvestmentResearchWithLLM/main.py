import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from database import engine, Base
import models  # noqa: F401 — ensures all ORM models (including WatchItem) are registered
from llm_client import AVAILABLE_MODELS, resolve_model

Base.metadata.create_all(bind=engine)

app = FastAPI(title="行业投研助手 API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import chat, research

app.include_router(chat.router,     prefix="/api/chat",     tags=["chat"])
app.include_router(research.router, prefix="/api/research", tags=["research"])

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    index_file = os.path.join(STATIC_DIR, "chat.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "行业投研助手", "docs": "/docs"}


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0", "service": "investment-research"}


@app.get("/api/models")
def list_models():
    current = resolve_model(None)
    return {"models": AVAILABLE_MODELS, "default": current}
