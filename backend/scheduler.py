"""
AuFarm — scheduler.py

The core tick-based scheduler engine.

Design goals:
- Runs entirely in asyncio, so no threads are blocking the event loop.
- Clock-anchored ticks: each tick fires at start_time + N * tick_interval,
  not start_time + sum(actual_execution_times). This prevents drift.
- Farm lifecycle is independent of the browser/websocket connection.
- Duplicate scheduler loops are prevented with an asyncio.Lock.
- One broken exploit never kills the farm loop.
- Process cleanup is guaranteed on STOP even if processes are mid-run.
- Concurrency is limited via asyncio.Semaphore.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from database import DB_PATH, get_db

logger = logging.getLogger("aufarm.scheduler")


EXPLOITS_DIR = os.getenv("EXPLOITS_DIR", "/exploits")
STDOUT_LIMIT = 64 * 1024   # 64 KB cap per stream
STDERR_LIMIT = 64 * 1024


# ── Status constants ──────────────────────────────────────────────────────────

class JobStatus:
    SUCCESS   = "SUCCESS"
    FAILED    = "FAILED"
    TIMEOUT   = "TIMEOUT"
    CRASHED   = "CRASHED"
    SKIPPED   = "SKIPPED"
    CANCELLED = "CANCELLED"


class FarmState:
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED  = "PAUSED"


# ── Broadcaster (WebSocket live updates) ──────────────────────────────────────

class Broadcaster:
    """Simple pub/sub for WebSocket connections."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, msg: dict) -> None:
        dead = set()
        for q in self._subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.add(q)
        for q in dead:
            self._subscribers.discard(q)


broadcaster = Broadcaster()


# ── Farm Scheduler ────────────────────────────────────────────────────────────

class FarmScheduler:
    """
    Singleton scheduler managing the farm lifecycle.

    State machine:
        STOPPED → start() → RUNNING
        RUNNING → stop()  → STOPPED
        RUNNING → pause() → PAUSED
        PAUSED  → resume() → RUNNING
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()   # not paused by default
        self._stop_event = asyncio.Event()
        self._running_procs: list[asyncio.subprocess.Process] = []
        self._procs_lock = asyncio.Lock()
        self._running_jobs: int = 0
        self._semaphore: Optional[asyncio.Semaphore] = None

    # ── Config helpers ────────────────────────────────────────────────────────

    async def _get_config(self) -> dict:
        db = await get_db()
        try:
            async with db.execute("SELECT key, value FROM farm_config") as cur:
                rows = await cur.fetchall()
            return {r["key"]: r["value"] for r in rows}
        finally:
            await db.close()

    async def _set_config(self, key: str, value: str) -> None:
        db = await get_db()
        try:
            await db.execute(
                "INSERT OR REPLACE INTO farm_config (key, value) VALUES (?, ?)",
                (key, value)
            )
            await db.commit()
        finally:
            await db.close()

    async def _set_status(self, status: str) -> None:
        await self._set_config("farm_status", status)

    async def _set_tick(self, tick: int) -> None:
        await self._set_config("current_tick", str(tick))

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_status(self) -> dict:
        cfg = await self._get_config()
        db = await get_db()
        try:
            stats = {}
            for st in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.TIMEOUT,
                       JobStatus.CRASHED, JobStatus.CANCELLED, JobStatus.SKIPPED):
                async with db.execute(
                    "SELECT COUNT(*) as cnt FROM results WHERE status = ?", (st,)
                ) as cur:
                    row = await cur.fetchone()
                    stats[st.lower()] = row["cnt"] if row else 0
        finally:
            await db.close()

        return {
            "status":          cfg.get("farm_status", FarmState.STOPPED),
            "current_tick":    int(cfg.get("current_tick", 0)),
            "tick_count":      int(cfg.get("tick_count", 100)),
            "tick_interval":   int(cfg.get("tick_interval", 60)),
            "exploit_timeout": int(cfg.get("exploit_timeout", 30)),
            "concurrency":     int(cfg.get("concurrency", 20)),
            "continuous":      cfg.get("continuous", "0") == "1",
            "running_jobs":    self._running_jobs,
            "stats":           stats,
        }

    async def start(self) -> bool:
        async with self._lock:
            if self._task and not self._task.done():
                return False   # already running
            cfg = await self._get_config()
            concurrency = int(cfg.get("concurrency", 20))
            self._semaphore = asyncio.Semaphore(concurrency)
            self._stop_event.clear()
            self._pause_event.set()
            await self._set_status(FarmState.RUNNING)
            await self._set_tick(0)
            self._task = asyncio.create_task(self._run_loop(), name="farm-loop")
            broadcaster.publish({"type": "status", "data": await self.get_status()})
            return True

    async def stop(self) -> None:
        async with self._lock:
            self._stop_event.set()
            self._pause_event.set()   # unblock any pause waits
            if self._task:
                self._task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            await self._kill_all_procs()
            await self._set_status(FarmState.STOPPED)
            broadcaster.publish({"type": "status", "data": await self.get_status()})

    async def pause(self) -> None:
        self._pause_event.clear()
        await self._set_status(FarmState.PAUSED)
        broadcaster.publish({"type": "status", "data": await self.get_status()})

    async def resume(self) -> None:
        self._pause_event.set()
        await self._set_status(FarmState.RUNNING)
        broadcaster.publish({"type": "status", "data": await self.get_status()})

    async def run_one_tick(self) -> bool:
        """Run a single tick regardless of farm state (for testing)."""
        cfg = await self._get_config()
        current = int(cfg.get("current_tick", 0))
        timeout = int(cfg.get("exploit_timeout", 30))
        concurrency = int(cfg.get("concurrency", 20))
        sem = asyncio.Semaphore(concurrency)
        asyncio.create_task(
            self._execute_tick(current + 1, timeout, sem),
            name=f"one-tick-{current+1}"
        )
        return True

    async def reset(self) -> None:
        await self.stop()
        db = await get_db()
        try:
            await db.execute("DELETE FROM results")
            await db.commit()
        finally:
            await db.close()
        await self._set_tick(0)
        broadcaster.publish({"type": "status", "data": await self.get_status()})

    # ── Scheduler loop ────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = await self._get_config()
        tick_count    = int(cfg.get("tick_count", 100))
        tick_interval = int(cfg.get("tick_interval", 60))
        timeout       = int(cfg.get("exploit_timeout", 30))
        continuous    = cfg.get("continuous", "0") == "1"

        loop_start = time.monotonic()
        tick = 0

        while not self._stop_event.is_set():
            # Check pause
            await self._pause_event.wait()
            if self._stop_event.is_set():
                break

            tick += 1
            if not continuous and tick > tick_count:
                logger.info("All ticks completed.")
                break

            await self._set_tick(tick)
            broadcaster.publish({
                "type": "tick_start",
                "data": {"tick": tick, "tick_count": tick_count}
            })

            # Execute this tick concurrently (non-blocking; we await done below)
            tick_task = asyncio.create_task(
                self._execute_tick(tick, timeout, self._semaphore),
                name=f"tick-{tick}"
            )

            # Wait for the tick to complete OR for the next interval deadline
            deadline = loop_start + tick * tick_interval
            now = time.monotonic()
            remaining = deadline - now

            if remaining > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(tick_task), timeout=remaining)
                except asyncio.TimeoutError:
                    # Tick ran over interval — note it but continue
                    logger.warning(
                        "Tick %d exceeded interval of %ds, continuing",
                        tick, tick_interval
                    )
                    # Let the tick task finish in background; next tick starts
            else:
                logger.warning(
                    "Tick %d started %ds late — interval is too short",
                    tick, -remaining
                )

            # Recalculate sleep to next deadline
            next_deadline = loop_start + tick * tick_interval
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0 and not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._stop_event.wait()),
                        timeout=sleep_for
                    )
                except asyncio.TimeoutError:
                    pass

        await self._set_status(FarmState.STOPPED)
        broadcaster.publish({"type": "status", "data": await self.get_status()})

    # ── Tick execution ────────────────────────────────────────────────────────

    async def _execute_tick(
        self,
        tick: int,
        timeout: int,
        semaphore: asyncio.Semaphore,
    ) -> None:
        db = await get_db()
        try:
            async with db.execute(
                "SELECT * FROM exploits WHERE enabled = 1"
            ) as cur:
                exploits = [dict(r) for r in await cur.fetchall()]
            async with db.execute(
                "SELECT * FROM targets WHERE enabled = 1"
            ) as cur:
                targets = [dict(r) for r in await cur.fetchall()]
        finally:
            await db.close()

        if not exploits:
            logger.warning("Tick %d: no enabled exploits", tick)
            return
        if not targets:
            logger.warning("Tick %d: no enabled targets", tick)
            return

        jobs = []
        for exploit in exploits:
            mode = exploit.get("target_mode", "all")
            if mode == "all":
                job_targets = targets
            else:
                ids = {int(x) for x in mode.split(",") if x.strip().isdigit()}
                job_targets = [t for t in targets if t["id"] in ids]
            for target in job_targets:
                jobs.append((exploit, target))

        tasks = [
            asyncio.create_task(
                self._run_job(tick, exploit, target, timeout, semaphore),
                name=f"tick-{tick}-e{exploit['id']}-t{target['id']}"
            )
            for exploit, target in jobs
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── Job execution ─────────────────────────────────────────────────────────

    async def _run_job(
        self,
        tick: int,
        exploit: dict,
        target: dict,
        timeout: int,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            self._running_jobs += 1
            broadcaster.publish({
                "type": "job_start",
                "data": {
                    "tick": tick,
                    "exploit_id": exploit["id"],
                    "exploit_name": exploit["name"],
                    "target_id": target["id"],
                    "target_host": target["host"],
                    "target_name": target["name"],
                }
            })
            try:
                # Per-exploit timeout overrides global config
                effective_timeout = int(exploit.get("timeout") or timeout or 30)
                result = await self._execute_exploit(tick, exploit, target, effective_timeout)
                await self._save_result(result)
                broadcaster.publish({"type": "job_done", "data": result})

                # Extract and queue flags from stdout for auto-submission
                if result.get("stdout"):
                    asyncio.create_task(
                        self._queue_flags_from_result(result),
                        name=f"flag-extract-t{tick}-e{exploit['id']}"
                    )

            except Exception as exc:
                logger.exception("Job tick=%d exploit=%d target=%d crashed: %s",
                                 tick, exploit["id"], target["id"], exc)
            finally:
                self._running_jobs -= 1

    async def _execute_exploit(
        self,
        tick: int,
        exploit: dict,
        target: dict,
        timeout: int,
    ) -> dict:
        script_path = os.path.join(EXPLOITS_DIR, exploit["filename"])
        runtime = exploit.get("runtime", "python3").strip()
        extra_args = exploit.get("args", "").strip()

        # Build command: <runtime> <script> <host> [extra_args...]
        cmd = []
        if runtime:
            cmd.append(runtime)
        cmd.append(script_path)
        cmd.append(target["host"])
        if target.get("port"):
            cmd.append(str(target["port"]))
        if extra_args:
            cmd.extend(extra_args.split())

        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "TARGET_HOST": target["host"],
            "TARGET_PORT": str(target.get("port", 0)),
            "TARGET_NAME": target.get("name", ""),
            "TARGET_SERVICE": target.get("service", ""),
            "AUFARM_TICK": str(tick),
        }

        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        status = JobStatus.FAILED
        exit_code = None
        stdout = ""
        stderr = ""

        if not os.path.isfile(script_path):
            return self._make_result(
                tick, exploit, target,
                status=JobStatus.SKIPPED,
                exit_code=None,
                stdout="",
                stderr=f"Script not found: {script_path}",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=0,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            async with self._procs_lock:
                self._running_procs.append(proc)

            try:
                raw_stdout, raw_stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=float(timeout)
                )
                exit_code = proc.returncode
                stdout = raw_stdout[:STDOUT_LIMIT].decode(errors="replace")
                stderr = raw_stderr[:STDERR_LIMIT].decode(errors="replace")
                status = JobStatus.SUCCESS if exit_code == 0 else JobStatus.FAILED

            except asyncio.TimeoutError:
                status = JobStatus.TIMEOUT
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception:
                    pass

        except FileNotFoundError:
            status = JobStatus.CRASHED
            stderr = f"Runtime not found: {runtime}"
        except Exception as exc:
            status = JobStatus.CRASHED
            stderr = str(exc)
        finally:
            async with self._procs_lock:
                try:
                    self._running_procs.remove(proc)
                except (ValueError, UnboundLocalError):
                    pass

        duration_ms = int((time.monotonic() - t0) * 1000)
        finished_at = datetime.now(timezone.utc).isoformat()
        return self._make_result(
            tick, exploit, target,
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )

    def _make_result(
        self,
        tick: int,
        exploit: dict,
        target: dict,
        *,
        status: str,
        exit_code,
        stdout: str,
        stderr: str,
        started_at: str,
        finished_at: str,
        duration_ms: int,
    ) -> dict:
        return {
            "tick":         tick,
            "exploit_id":   exploit["id"],
            "exploit_name": exploit["name"],
            "target_id":    target["id"],
            "target_host":  target["host"],
            "target_name":  target["name"],
            "status":       status,
            "exit_code":    exit_code,
            "duration_ms":  duration_ms,
            "stdout":       stdout,
            "stderr":       stderr,
            "started_at":   started_at,
            "finished_at":  finished_at,
        }

    async def _save_result(self, r: dict) -> None:
        db = await get_db()
        try:
            await db.execute(
                """
                INSERT INTO results
                    (tick, exploit_id, exploit_name, target_id, target_host, target_name,
                     status, exit_code, duration_ms, stdout, stderr, started_at, finished_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["tick"], r["exploit_id"], r["exploit_name"],
                    r["target_id"], r["target_host"], r["target_name"],
                    r["status"], r["exit_code"], r["duration_ms"],
                    r["stdout"], r["stderr"], r["started_at"], r["finished_at"],
                )
            )
            await db.commit()
        finally:
            await db.close()

    # ── Flag extraction helper ────────────────────────────────────────────────

    async def _queue_flags_from_result(self, result: dict) -> None:
        """Read the flag regex from config and queue any found flags."""
        try:
            # Lazy import avoids circular dep (submitter imports broadcaster from here)
            from submitter import extract_flags, queue_flags, _get_submit_cfg
            cfg = await _get_submit_cfg()
            pattern = cfg.get("flag_regex", "")
            flags = extract_flags(result.get("stdout", ""), pattern)
            if flags:
                await queue_flags(
                    flags,
                    exploit_name=result.get("exploit_name", ""),
                    target_host=result.get("target_host", ""),
                    target_name=result.get("target_name", ""),
                    tick=result.get("tick", 0),
                )
                logger.info(
                    "Tick %d | %s → %s | queued %d flag(s)",
                    result.get("tick", 0),
                    result.get("exploit_name", "?"),
                    result.get("target_host", "?"),
                    len(flags),
                )
        except Exception as exc:
            logger.warning("Flag extraction error: %s", exc)

    # ── Cleanup ───────────────────────────────────────────────────────────────


    async def _kill_all_procs(self) -> None:
        async with self._procs_lock:
            for proc in list(self._running_procs):
                try:
                    proc.kill()
                except Exception:
                    pass
            self._running_procs.clear()


# Global singleton
scheduler = FarmScheduler()
