"""
AuFarm — routers/competition.py

Centralized competition configuration and connectivity testing.
Consolidates Team configuration, Scoreboard API settings, and Execution parameters.
"""

from fastapi import APIRouter, HTTPException
from typing import Optional

from database import get_db
from models import CentralizedConfig, TeamConfig, ScoreboardApiConfig, ExecutionConfig
from submitter import test_connection as run_test_connection

router = APIRouter(prefix="/api/competition", tags=["competition"])

SECRET_MASK = "••••••••"


def _mask_secret(value: str) -> str:
    return SECRET_MASK if value else ""


@router.get("/config", response_model=CentralizedConfig)
async def get_competition_config():
    """Retrieve full centralized configuration."""
    db = await get_db()
    try:
        async with db.execute("SELECT key, value FROM farm_config") as cur:
            rows = await cur.fetchall()
        cfg = {r["key"]: r["value"] for r in rows}

        return CentralizedConfig(
            team=TeamConfig(
                team_id=cfg.get("team_id", "1"),
                team_name=cfg.get("team_name", "MyTeam"),
                team_ip=cfg.get("team_ip", ""),
                team_password=_mask_secret(cfg.get("team_password", "")),
            ),
            api=ScoreboardApiConfig(
                submit_mode=cfg.get("submit_mode", "http_json"),
                submit_url=cfg.get("submit_url", ""),
                submit_token=_mask_secret(cfg.get("submit_token", "")),
                submit_host=cfg.get("submit_host", ""),
                submit_port=int(cfg.get("submit_port", 31337)),
                flag_regex=cfg.get("flag_regex", ""),
            ),
            execution=ExecutionConfig(
                tick_count=int(cfg.get("tick_count", 100)),
                tick_interval=int(cfg.get("tick_interval", 60)),
                exploit_timeout=int(cfg.get("exploit_timeout", 30)),
                collection_deadline=int(cfg.get("collection_deadline", cfg.get("exploit_timeout", 30))),
                concurrency=int(cfg.get("concurrency", 20)),
                continuous=cfg.get("continuous", "1") == "1",
                submit_enabled=cfg.get("submit_enabled", "0") == "1",
                submit_interval=int(cfg.get("submit_interval", 5)),
                flag_lifetime=int(cfg.get("flag_lifetime", 300)),
            ),
        )
    finally:
        await db.close()


@router.put("/config")
async def update_competition_config(body: CentralizedConfig):
    """
    Validate and save centralized competition configuration.
    Updates all team identity, API scoreboard, and execution parameters atomically.
    """
    # Validation
    if not body.team.team_id.strip():
        raise HTTPException(status_code=400, detail="Team ID is required")
    if not body.team.team_name.strip():
        raise HTTPException(status_code=400, detail="Team Name is required")
    if body.api.submit_mode not in ("http_json", "http_form", "tcp"):
        raise HTTPException(status_code=400, detail="Invalid submission mode")
    if body.api.submit_port < 1 or body.api.submit_port > 65535:
        raise HTTPException(status_code=400, detail="Submit port must be between 1 and 65535")
    if body.execution.tick_interval <= 0:
        raise HTTPException(status_code=400, detail="Tick interval must be greater than 0")
    if body.execution.exploit_timeout <= 0:
        raise HTTPException(status_code=400, detail="Exploit timeout must be greater than 0")
    if body.execution.collection_deadline < 0:
        raise HTTPException(status_code=400, detail="Collection deadline must be 0 or greater")
    if body.execution.concurrency <= 0:
        raise HTTPException(status_code=400, detail="Concurrency must be greater than 0")

    db = await get_db()
    try:
        async with db.execute("SELECT key, value FROM farm_config") as cur:
            rows = await cur.fetchall()
        existing = {r["key"]: r["value"] for r in rows}

        team_password = body.team.team_password
        if team_password == SECRET_MASK:
            team_password = existing.get("team_password", "")
        submit_token = body.api.submit_token.strip()
        if submit_token == SECRET_MASK:
            submit_token = existing.get("submit_token", "")

        updates = {
            # Team config
            "team_id":         body.team.team_id.strip(),
            "team_name":       body.team.team_name.strip(),
            "team_ip":         body.team.team_ip.strip(),
            "team_password":   team_password,
            # Scoreboard API config
            "submit_mode":     body.api.submit_mode,
            "submit_url":      body.api.submit_url.strip(),
            "submit_token":    submit_token,
            "submit_host":     body.api.submit_host.strip(),
            "submit_port":     str(body.api.submit_port),
            "flag_regex":      body.api.flag_regex.strip(),
            # Execution config
            "tick_count":      str(body.execution.tick_count),
            "tick_interval":   str(body.execution.tick_interval),
            "exploit_timeout": str(body.execution.exploit_timeout),
            "collection_deadline": str(body.execution.collection_deadline),
            "concurrency":     str(body.execution.concurrency),
            "continuous":      "1" if body.execution.continuous else "0",
            "submit_enabled":  "1" if body.execution.submit_enabled else "0",
            "submit_interval": str(body.execution.submit_interval),
            "flag_lifetime":   str(body.execution.flag_lifetime),
        }

        for k, v in updates.items():
            await db.execute(
                "INSERT OR REPLACE INTO farm_config (key, value) VALUES (?, ?)",
                (k, v)
            )
        await db.commit()
        return {"ok": True, "message": "Competition configuration saved successfully"}
    finally:
        await db.close()


@router.post("/test_connection")
async def test_scoreboard_connection(cfg_override: Optional[CentralizedConfig] = None):
    """
    Test connectivity to the scoreboard server using either current or provided settings.
    Does not submit any real flags.
    """
    if cfg_override:
        token = cfg_override.api.submit_token
        if token == SECRET_MASK:
            db = await get_db()
            try:
                async with db.execute("SELECT value FROM farm_config WHERE key = 'submit_token'") as cur:
                    row = await cur.fetchone()
                token = row["value"] if row else ""
            finally:
                await db.close()
        cfg = {
            "submit_mode":  cfg_override.api.submit_mode,
            "submit_url":   cfg_override.api.submit_url,
            "submit_token": token,
            "submit_host":  cfg_override.api.submit_host,
            "submit_port":  str(cfg_override.api.submit_port),
        }
    else:
        db = await get_db()
        try:
            async with db.execute(
                "SELECT key, value FROM farm_config WHERE key IN ('submit_mode','submit_url','submit_token','submit_host','submit_port')"
            ) as cur:
                rows = await cur.fetchall()
            cfg = {r["key"]: r["value"] for r in rows}
        finally:
            await db.close()

    ok, message = await run_test_connection(cfg)
    return {"ok": ok, "message": message}
