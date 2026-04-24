import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from database import engine, Base
import models  # noqa: F401

Base.metadata.create_all(bind=engine)

app = FastAPI(title="持仓管理与调仓建议 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import portfolio, trades, signals, scan, dashboard
from routers import advisor

app.include_router(portfolio.router, prefix="/api/portfolio",  tags=["portfolio"])
app.include_router(trades.router,    prefix="/api/trades",     tags=["trades"])
app.include_router(signals.router,   prefix="/api/signals",    tags=["signals"])
app.include_router(scan.router,      prefix="/api/scan",       tags=["scan"])
app.include_router(dashboard.router, prefix="/api/dashboard",  tags=["dashboard"])
app.include_router(advisor.router,   prefix="/api/advisor",    tags=["advisor"])

# 手机上传页面
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/upload")
def upload_page():
    return FileResponse(os.path.join(STATIC_DIR, "upload.html"))


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0"}
