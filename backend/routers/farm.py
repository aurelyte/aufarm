"""
AuFarm — routers/farm.py

Farm control endpoints (start/stop/pause/resume/reset/tick)
and configuration management.
"""

from fastapi import APIRouter, HTTPException

from database import get_db
from models import FarmConfig
from scheduler import scheduler, FarmState

router = APIRouter(prefix="/api/farm", tags=["farm"])


@router.get("/status")
async def farm_status():
    return await scheduler.get_status()


@router.post("/start")
async def farm_start():
    cfg = await scheduler.get_status()
    if cfg["status"] == FarmState.RUNNING:
        raise HTTPException(status_code=409, detail="Farm is already running")
    ok = await scheduler.start()
    if not ok:
        raise HTTPException(status_code=409, detail="Farm could not be started")
    return {"ok": True, "status": "RUNNING"}


@router.post("/stop")
async def farm_stop():
    await scheduler.stop()
    return {"ok": True, "status": "STOPPED"}


@router.post("/pause")
async def farm_pause():
    cfg = await scheduler.get_status()
    if cfg["status"] != FarmState.RUNNING:
        raise HTTPException(status_code=409, detail="Farm is not running")
    await scheduler.pause()
    return {"ok": True, "status": "PAUSED"}


@router.post("/resume")
async def farm_resume():
    cfg = await scheduler.get_status()
    if cfg["status"] != FarmState.PAUSED:
        raise HTTPException(status_code=409, detail="Farm is not paused")
    await scheduler.resume()
    return {"ok": True, "status": "RUNNING"}


@router.post("/tick")
async def farm_run_one_tick():
    """Run a single tick immediately, regardless of farm state."""
    await scheduler.run_one_tick()
    return {"ok": True}


@router.post("/reset")
async def farm_reset():
    """Stop the farm and clear all execution history."""
    await scheduler.reset()
    return {"ok": True}


@router.get("/config")
async def get_config():
    db = await get_db()
    try:
        async with db.execute("SELECT key, value FROM farm_config") as cur:
            rows = await cur.fetchall()
        cfg = {r["key"]: r["value"] for r in rows}
        return {
            "tick_count":      int(cfg.get("tick_count", 100)),
            "tick_interval":   int(cfg.get("tick_interval", 60)),
            "exploit_timeout": int(cfg.get("exploit_timeout", 30)),
            "concurrency":     int(cfg.get("concurrency", 20)),
            "continuous":      cfg.get("continuous", "1") == "1",
        }
    finally:
        await db.close()


@router.put("/config")
async def set_config(body: FarmConfig):
    db = await get_db()
    try:
        updates = {
            "tick_count":      str(body.tick_count),
            "tick_interval":   str(body.tick_interval),
            "exploit_timeout": str(body.exploit_timeout),
            "concurrency":     str(body.concurrency),
            "continuous":      "1" if body.continuous else "0",
        }
        for key, val in updates.items():
            await db.execute(
                "INSERT OR REPLACE INTO farm_config (key, value) VALUES (?, ?)",
                (key, val)
            )
        await db.commit()
        return updates
    finally:
        await db.close()
