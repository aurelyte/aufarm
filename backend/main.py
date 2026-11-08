"""
AuFarm — main.py

FastAPI application entry point.

Serves:
  - REST API at /api/...
  - WebSocket live feed at /ws
  - Static frontend files at /
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from database import init_db
from scheduler import broadcaster
from submitter import submitter
from routers import farm, targets, exploits, results, competition
from routers.submission import router as submission_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("aufarm")

FRONTEND_DIR = os.getenv("FRONTEND_DIR", "/frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AuFarm starting — initializing database")
    await init_db()
    logger.info("Database ready")
    submitter.start()
    logger.info("Flag submitter started")
    yield
    logger.info("AuFarm shutting down")
    submitter.stop()



app = FastAPI(title="AuFarm", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(farm.router)
app.include_router(targets.router)
app.include_router(exploits.router)
app.include_router(results.router)
app.include_router(submission_router)
app.include_router(competition.router)


# ── Status alias ──────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    from scheduler import scheduler
    return await scheduler.get_status()


# ── WebSocket live feed ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    q = broadcaster.subscribe()
    logger.info("WebSocket client connected")
    try:
        # Send current status on connect
        from scheduler import scheduler
        status = await scheduler.get_status()
        await ws.send_text(json.dumps({"type": "status", "data": status}))

        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30)
                await ws.send_text(json.dumps(msg))
            except asyncio.TimeoutError:
                # Send heartbeat
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.warning("WebSocket error: %s", e)
    finally:
        broadcaster.unsubscribe(q)


# ── Frontend static files ─────────────────────────────────────────────────────

if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str):
        # Serve index.html for all non-API paths (SPA fallback)
        index = os.path.join(FRONTEND_DIR, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        return {"error": "Frontend not found"}
