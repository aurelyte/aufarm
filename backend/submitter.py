"""
AuFarm — submitter.py

Flag auto-submission engine.

Responsibilities:
  1. Extract flags from exploit stdout using a configurable regex.
  2. Deduplicate via the `flags` table (UNIQUE constraint on flag text).
  3. Run a periodic submission loop that batches QUEUED flags and sends
     them to the scoring server.
  4. Support three submission modes (matching both reference examples):
       http_json — POST {"flag": "..."} per flag (Gemastik finals)
       http_form — POST flag=<value>       (simple form POST)
       tcp       — raw TCP line-by-line    (ForcAD / classic gameserver)
  5. Expire flags that are older than flag_lifetime without submitting
     (marks them EXPIRED so they don't pile up forever).

The loop runs independently of the farm scheduler — flags are queued by
_execute_exploit() after each job, then submitted by the loop every
submit_interval seconds.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from database import get_db
from scheduler import broadcaster

logger = logging.getLogger("aufarm.submitter")

GEMASTIK_SUBMIT_URL = "https://gemastik19-finals.siberlab.id/api/flag"

# Serializes all scoreboard submissions so manual/background submission cannot race
# with the scheduler's per-service batch submission.
SUBMISSION_LOCK = asyncio.Lock()

# ── Status constants ──────────────────────────────────────────────────────────

class FlagStatus:
    QUEUED   = "QUEUED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE= "DUPLICATE"
    EXPIRED  = "EXPIRED"
    ERROR    = "ERROR"


# ── Config helper ─────────────────────────────────────────────────────────────

async def _get_submit_cfg() -> dict:
    db = await get_db()
    try:
        async with db.execute(
            "SELECT key, value FROM farm_config WHERE key IN "
            "('submit_enabled','submit_mode','submit_url','submit_token',"
            " 'submit_host','submit_port','flag_regex','submit_interval','flag_lifetime')"
        ) as cur:
            rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        await db.close()


# ── Flag extraction ───────────────────────────────────────────────────────────

def extract_flags(text: str, pattern: str) -> list[str]:
    """
    Pull every unique flag out of `text` using the configured regex.
    Falls back to a common default if the pattern is empty or invalid.

    Uses full regex matches instead of ``re.findall`` capture semantics so patterns
    with one or more capture groups still return the actual flag text.
    """
    if not pattern:
        pattern = r"[A-Za-z0-9_]{2,16}\{[A-Za-z0-9_+=\-/\.]{8,}\}"
    try:
        seen = set()
        flags: list[str] = []
        for match in re.finditer(pattern, text):
            flag = match.group(1) if match.lastindex == 1 else match.group(0)
            if flag and flag not in seen:
                seen.add(flag)
                flags.append(flag)
        return flags
    except re.error as e:
        logger.warning("Bad flag regex %r: %s", pattern, e)
        return []


async def queue_flags(
    flags: list[str],
    *,
    exploit_name: str,
    target_host: str,
    target_name: str,
    tick: int,
) -> int:
    """
    Insert newly discovered flags into the flags table (dedup via UNIQUE).
    Returns the count of genuinely new flags inserted.
    """
    if not flags:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    db = await get_db()
    try:
        for flag in flags:
            try:
                cur = await db.execute(
                    """
                    INSERT OR IGNORE INTO flags
                        (flag, exploit_name, target_host, target_name, tick,
                         status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'QUEUED', ?)
                    """,
                    (flag, exploit_name, target_host, target_name, tick, now)
                )
                if cur.rowcount == 1:
                    inserted += 1
            except Exception:
                pass
        await db.commit()
    finally:
        await db.close()

    if inserted:
        broadcaster.publish({
            "type": "flags_queued",
            "data": {
                "count": inserted,
                "exploit_name": exploit_name,
                "target_host": target_host,
                "tick": tick,
                "flags": flags[:10],   # preview, not full list for large outputs
            }
        })
    return inserted


# ── HTTP submission ───────────────────────────────────────────────────────────

async def _submit_http_json(
    flags: list[str],
    url: str,
    token: str,
    timeout: int = 10,
) -> list[tuple[str, str, str]]:
    """
    POST {"flag": flag} once for each flag (Gemastik finals API).
    Returns list of (flag, status, response_text).

    Reads the API's JSON status/message when present; HTTP 2xx without an
    explicit failure is treated as accepted.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            for flag in flags:
                try:
                    async with session.post(
                        url,
                        json={"flag": flag},
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        body = await resp.text()
                        body_lower = body.lower()
                        try:
                            import json as _json
                            data = _json.loads(body)
                        except (ValueError, TypeError):
                            data = {}

                        status_text = str(data.get("message", data.get("status", body[:120])))
                        if resp.status >= 400:
                            flag_st = FlagStatus.ERROR
                        elif any(k in body_lower for k in ("already", "duplicate", "dup", "owned")):
                            flag_st = FlagStatus.DUPLICATE
                        elif any(k in body_lower for k in ("reject", "invalid", "wrong", "bad")):
                            flag_st = FlagStatus.REJECTED
                        elif data.get("success") is False or data.get("valid") is False:
                            flag_st = FlagStatus.REJECTED
                        else:
                            flag_st = FlagStatus.ACCEPTED
                        results.append((flag, flag_st, status_text[:120]))
                except Exception as exc:
                    results.append((flag, FlagStatus.ERROR, str(exc)[:120]))

    except Exception as exc:
        logger.warning("HTTP submission error: %s", exc)
        for f in flags:
            results.append((f, FlagStatus.ERROR, str(exc)[:120]))
    return results


async def _submit_http_form(
    flags: list[str],
    url: str,
    token: str,
    timeout: int = 10,
) -> list[tuple[str, str, str]]:
    """POST flag=<value> for each flag individually."""
    results = []
    headers = {"Authorization": f"Bearer {token}", "X-Team-Token": token}
    try:
        async with aiohttp.ClientSession() as session:
            for flag in flags:
                try:
                    async with session.post(
                        url,
                        data={"flag": flag},
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        body = await resp.text()
                        body_lower = body.lower()
                        if resp.status >= 400:
                            st = FlagStatus.ERROR
                        elif any(k in body_lower for k in ("already", "dup", "owned")):
                            st = FlagStatus.DUPLICATE
                        elif any(k in body_lower for k in ("reject", "invalid", "wrong")):
                            st = FlagStatus.REJECTED
                        else:
                            st = FlagStatus.ACCEPTED
                        results.append((flag, st, body[:120]))
                except Exception as exc:
                    results.append((flag, FlagStatus.ERROR, str(exc)[:120]))
    except Exception as exc:
        for f in flags:
            results.append((f, FlagStatus.ERROR, str(exc)[:120]))
    return results


def _submit_tcp_sync(
    flags: list[str],
    host: str,
    port: int,
    timeout: int = 10,
) -> list[tuple[str, str, str]]:
    """
    Raw TCP submission: connect, send each flag as a line, read response.
    Blocking — called via asyncio.to_thread().
    """
    results = []
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            # Some gameservers send a banner first
            try:
                s.recv(1024)
            except socket.timeout:
                pass

            for flag in flags:
                try:
                    s.sendall(flag.encode() + b"\n")
                    resp = b""
                    while True:
                        try:
                            chunk = s.recv(256)
                            if not chunk:
                                break
                            resp += chunk
                            if b"\n" in resp:
                                break
                        except socket.timeout:
                            break
                    resp_text = resp.decode(errors="replace").strip()
                    resp_lower = resp_text.lower()
                    if any(k in resp_lower for k in ("accepted", "ok", "correct", "got it")):
                        st = FlagStatus.ACCEPTED
                    elif any(k in resp_lower for k in ("already", "dup", "owned")):
                        st = FlagStatus.DUPLICATE
                    else:
                        st = FlagStatus.REJECTED
                    results.append((flag, st, resp_text[:120]))
                except Exception as exc:
                    results.append((flag, FlagStatus.ERROR, str(exc)[:80]))
    except Exception as exc:
        for f in flags:
            results.append((f, FlagStatus.ERROR, str(exc)[:80]))
    return results


# ── Submission dispatcher ─────────────────────────────────────────────────────

async def _submit_batch(
    flags: list[str],
    cfg: dict,
) -> list[tuple[str, str, str]]:
    """Route to the right submission backend based on config."""
    mode  = cfg.get("submit_mode", "http_json")
    url   = (cfg.get("submit_url") or GEMASTIK_SUBMIT_URL).strip()
    token = cfg.get("submit_token", "")
    host  = cfg.get("submit_host", "")
    port  = int(cfg.get("submit_port", 31337))

    if mode == "http_json":
        return await _submit_http_json(flags, url, token)
    elif mode == "http_form":
        return await _submit_http_form(flags, url, token)
    elif mode == "tcp":
        return await asyncio.to_thread(_submit_tcp_sync, flags, host, port)
    else:
        return [(f, FlagStatus.ERROR, f"Unknown mode: {mode}") for f in flags]


async def submit_service_batch(
    flags: list[str],
    cfg: Optional[dict] = None,
) -> dict[str, tuple[str, str]]:
    """
    Batch submit collected flags for a service after collection completes.
    Returns: dict mapping flag -> (status, response_text).
    """
    if not flags:
        return {}
    if cfg is None:
        cfg = await _get_submit_cfg()

    async with SUBMISSION_LOCK:
        # Deduplicate within this batch
        unique_flags = list(dict.fromkeys(flags))

        # Check database to see if any of these flags were ALREADY ACCEPTED in earlier ticks
        db = await get_db()
        already_accepted = set()
        try:
            placeholders = ",".join("?" * len(unique_flags))
            async with db.execute(
                f"SELECT flag FROM flags WHERE flag IN ({placeholders}) AND status = ?",
                (*unique_flags, FlagStatus.ACCEPTED)
            ) as cur:
                rows = await cur.fetchall()
                already_accepted = {r["flag"] for r in rows}
        finally:
            await db.close()

        to_submit = [f for f in unique_flags if f not in already_accepted]
        flag_outcomes: dict[str, tuple[str, str]] = {}

        for f in already_accepted:
            flag_outcomes[f] = (FlagStatus.DUPLICATE, "Already submitted in prior run")

        if to_submit:
            logger.info("Batch submitting %d flags for service", len(to_submit))
            batch_results = await _submit_batch(to_submit, cfg)
            await _update_flag_statuses(batch_results)
            for flag, st, resp in batch_results:
                flag_outcomes[flag] = (st, resp)

            accepted  = sum(1 for _, st, _ in batch_results if st == FlagStatus.ACCEPTED)
            rejected  = sum(1 for _, st, _ in batch_results if st == FlagStatus.REJECTED)
            duplicate = sum(1 for _, st, _ in batch_results if st == FlagStatus.DUPLICATE) + len(already_accepted)
            errors    = sum(1 for _, st, _ in batch_results if st == FlagStatus.ERROR)

            summary = {
                "submitted": len(batch_results),
                "accepted":  accepted,
                "rejected":  rejected,
                "duplicate": duplicate,
                "errors":    errors,
            }
            broadcaster.publish({"type": "flags_submitted", "data": summary})

        return flag_outcomes


async def test_connection(cfg: Optional[dict] = None) -> tuple[bool, str]:
    """Test scoreboard connectivity without submitting real flags."""
    if cfg is None:
        cfg = await _get_submit_cfg()

    mode = cfg.get("submit_mode", "http_json")
    url = (cfg.get("submit_url") or GEMASTIK_SUBMIT_URL).strip()
    token = (cfg.get("submit_token") or "").strip()
    host = (cfg.get("submit_host") or "").strip()
    port = int(cfg.get("submit_port") or 31337)

    if mode in ("http_json", "http_form"):
        if not url:
            return False, "Submit URL is not configured"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "AuFarm-CTF/1.0",
        }
        try:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status in (401, 403):
                            return True, f"Reachable, but server returned HTTP {resp.status} (check Token/Credentials)"
                        return True, f"Connection OK (HTTP {resp.status})"
                except Exception:
                    async with session.post(
                        url,
                        json={"flag": ""},
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status in (401, 403):
                            return True, f"Reachable, but server returned HTTP {resp.status} (check Token/Credentials)"
                        return True, f"Connection OK (HTTP {resp.status})"
        except asyncio.TimeoutError:
            return False, f"Connection timed out connecting to {url}"
        except Exception as exc:
            return False, f"Connection failed: {exc}"

    elif mode == "tcp":
        if not host:
            return False, "Submit host is not configured"
        try:
            def _test_tcp():
                with socket.create_connection((host, port), timeout=5) as s:
                    s.settimeout(2)
                    try:
                        banner = s.recv(1024)
                        return True, f"Connected to {host}:{port}. Banner: {banner[:40].decode(errors='replace').strip()}"
                    except socket.timeout:
                        return True, f"Connected to {host}:{port} successfully"
            return await asyncio.to_thread(_test_tcp)
        except Exception as exc:
            return False, f"TCP connection failed to {host}:{port} — {exc}"

    return False, f"Unknown submit mode: {mode}"


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _expire_old_flags(lifetime_sec: int) -> None:
    """Mark QUEUED flags older than lifetime_sec as EXPIRED."""
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE flags SET status = 'EXPIRED'
            WHERE status = 'QUEUED'
              AND (strftime('%s', 'now') - strftime('%s', created_at)) > ?
            """,
            (lifetime_sec,)
        )
        await db.commit()
    finally:
        await db.close()


async def _fetch_queued(limit: int = 200, include_errors: bool = False) -> list[dict]:
    db = await get_db()
    try:
        statuses = ("QUEUED", "ERROR") if include_errors else ("QUEUED",)
        placeholders = ",".join("?" * len(statuses))
        async with db.execute(
            f"SELECT id, flag, exploit_name, target_host FROM flags WHERE status IN ({placeholders}) LIMIT ?",
            (*statuses, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def _update_flag_statuses(
    results: list[tuple[str, str, str]]
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        for flag, status, response in results:
            await db.execute(
                "UPDATE flags SET status = ?, response = ?, submitted_at = ? WHERE flag = ?",
                (status, response, now, flag)
            )
        await db.commit()
    finally:
        await db.close()


# ── Submission loop ───────────────────────────────────────────────────────────

class Submitter:
    """
    Background task that periodically submits queued flags.
    Runs independently of the farm scheduler.
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="flag-submitter")
            logger.info("Flag submitter started")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Submitter loop error: %s", exc)

            cfg = await _get_submit_cfg()
            interval = max(2, int(cfg.get("submit_interval", 5)))
            await asyncio.sleep(interval)

    async def _run_once(self, include_errors: bool = False) -> None:
        cfg = await _get_submit_cfg()

        # Always expire stale flags regardless of submit_enabled
        lifetime = max(30, int(cfg.get("flag_lifetime", 300)))
        await _expire_old_flags(lifetime)

        if cfg.get("submit_enabled", "0") != "1":
            return

        # Do not submit while exploit jobs are running to enforce collect-first, submit-later
        from scheduler import scheduler as farm_sched
        if farm_sched._running_jobs > 0 or getattr(farm_sched, "_batch_submission_active", False):
            return

        async with SUBMISSION_LOCK:
            queued = await _fetch_queued(limit=200, include_errors=include_errors)
            if not queued:
                return

            flags = [r["flag"] for r in queued]
            logger.info("Submitting %d flag(s)", len(flags))

            results = await _submit_batch(flags, cfg)
            await _update_flag_statuses(results)

        accepted  = sum(1 for _, st, _ in results if st == FlagStatus.ACCEPTED)
        rejected  = sum(1 for _, st, _ in results if st == FlagStatus.REJECTED)
        duplicate = sum(1 for _, st, _ in results if st == FlagStatus.DUPLICATE)
        errors    = sum(1 for _, st, _ in results if st == FlagStatus.ERROR)

        summary = {
            "submitted": len(results),
            "accepted":  accepted,
            "rejected":  rejected,
            "duplicate": duplicate,
            "errors":    errors,
        }
        broadcaster.publish({"type": "flags_submitted", "data": summary})

        if accepted:
            logger.info("Flags: %d accepted, %d rejected, %d dup, %d err",
                        accepted, rejected, duplicate, errors)


# Global singleton — started by main.py lifespan
submitter = Submitter()
