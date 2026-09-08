"""
AuFarm — routers/submission.py

API for flag auto-submission configuration and flag history.
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from database import get_db

router = APIRouter(prefix="/api/submission", tags=["submission"])


# ── Models ────────────────────────────────────────────────────────────────────

class SubmitConfig(BaseModel):
    submit_enabled:  bool   = False
    submit_mode:     str    = "http_json"   # http_json | http_form | tcp
    submit_url:      str    = ""
    submit_token:    str    = ""
    submit_host:     str    = ""
    submit_port:     int    = 31337
    flag_regex:      str    = ""
    submit_interval: int    = 5
    flag_lifetime:   int    = 300


# ── Config endpoints ──────────────────────────────────────────────────────────

@router.get("/config")
async def get_submit_config():
    db = await get_db()
    try:
        keys = (
            "submit_enabled", "submit_mode", "submit_url", "submit_token",
            "submit_host", "submit_port", "flag_regex",
            "submit_interval", "flag_lifetime",
        )
        placeholders = ",".join("?" * len(keys))
        async with db.execute(
            f"SELECT key, value FROM farm_config WHERE key IN ({placeholders})",
            keys,
        ) as cur:
            rows = await cur.fetchall()
        cfg = {r["key"]: r["value"] for r in rows}
        return {
            "submit_enabled":  cfg.get("submit_enabled", "0") == "1",
            "submit_mode":     cfg.get("submit_mode", "http_json"),
            "submit_url":      cfg.get("submit_url", ""),
            "submit_token":    cfg.get("submit_token", ""),
            "submit_host":     cfg.get("submit_host", ""),
            "submit_port":     int(cfg.get("submit_port", 31337)),
            "flag_regex":      cfg.get("flag_regex", ""),
            "submit_interval": int(cfg.get("submit_interval", 5)),
            "flag_lifetime":   int(cfg.get("flag_lifetime", 300)),
        }
    finally:
        await db.close()


@router.put("/config")
async def set_submit_config(body: SubmitConfig):
    db = await get_db()
    try:
        updates = {
            "submit_enabled":  "1" if body.submit_enabled else "0",
            "submit_mode":     body.submit_mode,
            "submit_url":      body.submit_url,
            "submit_token":    body.submit_token,
            "submit_host":     body.submit_host,
            "submit_port":     str(body.submit_port),
            "flag_regex":      body.flag_regex,
            "submit_interval": str(body.submit_interval),
            "flag_lifetime":   str(body.flag_lifetime),
        }
        for key, val in updates.items():
            await db.execute(
                "INSERT OR REPLACE INTO farm_config (key, value) VALUES (?, ?)",
                (key, val)
            )
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


# ── Flag history endpoints ────────────────────────────────────────────────────

@router.get("/flags")
async def list_flags(
    status:   Optional[str] = None,
    exploit:  Optional[str] = None,
    limit:    int = Query(default=200, le=1000),
    offset:   int = Query(default=0, ge=0),
):
    conditions = []
    values: list = []

    if status:
        conditions.append("status = ?")
        values.append(status.upper())
    if exploit:
        conditions.append("exploit_name LIKE ?")
        values.append(f"%{exploit}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    count_values = list(values)
    values += [limit, offset]

    db = await get_db()
    try:
        async with db.execute(
            f"SELECT * FROM flags {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            values
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            f"SELECT COUNT(*) as cnt FROM flags {where}", count_values
        ) as cur:
            cnt = (await cur.fetchone())["cnt"]
        return {
            "total":  cnt,
            "limit":  limit,
            "offset": offset,
            "flags":  [dict(r) for r in rows],
        }
    finally:
        await db.close()


@router.get("/flags/stats")
async def flag_stats():
    db = await get_db()
    try:
        stats = {}
        for st in ("QUEUED", "ACCEPTED", "REJECTED", "DUPLICATE", "EXPIRED", "ERROR"):
            async with db.execute(
                "SELECT COUNT(*) as cnt FROM flags WHERE status = ?", (st,)
            ) as cur:
                stats[st.lower()] = (await cur.fetchone())["cnt"]
        async with db.execute("SELECT COUNT(*) as cnt FROM flags") as cur:
            stats["total"] = (await cur.fetchone())["cnt"]
        return stats
    finally:
        await db.close()


@router.post("/submit_now")
async def submit_now():
    """Trigger an immediate submission run (ignores interval timer)."""
    try:
        import asyncio
        from submitter import submitter as sub_instance
        asyncio.create_task(sub_instance._run_once(), name="submit-now")
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

