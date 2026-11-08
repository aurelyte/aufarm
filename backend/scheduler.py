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
import shlex
import signal
import time
from datetime import datetime, timezone
from typing import Optional

from database import get_db

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
        self._latest_batch_result: Optional[dict] = None
        self._batch_submission_active: bool = False

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
            "continuous":      cfg.get("continuous", "1") == "1",
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
        self._stop_event.clear()
        self._pause_event.set()
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
        self._latest_batch_result = None
        db = await get_db()
        try:
            await db.execute("DELETE FROM results")
            await db.execute("DELETE FROM flags")
            await db.commit()
        finally:
            await db.close()
        await self._set_tick(0)
        broadcaster.publish({"type": "status", "data": await self.get_status()})

    async def get_latest_batch(self) -> Optional[dict]:
        if self._latest_batch_result:
            return self._latest_batch_result
        db = await get_db()
        try:
            async with db.execute(
                "SELECT tick, exploit_id, exploit_name FROM results ORDER BY id DESC LIMIT 1"
            ) as cur:
                last_row = await cur.fetchone()
            if not last_row:
                return None
            tick = last_row["tick"]
            eid = last_row["exploit_id"]
            ename = last_row["exploit_name"]

            async with db.execute(
                "SELECT * FROM results WHERE tick = ? AND exploit_id = ? ORDER BY id",
                (tick, eid)
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

            summary_results = []
            flags_retrieved = 0
            flags_submitted = 0
            accepted = 0
            rejected = 0
            duplicate = 0
            errors = 0

            for r in rows:
                col_st = r.get("collection_status") or ("Flag Retrieved" if r.get("flag") else "Flag Not Retrieved")
                sub = r.get("submission_status") or "Not submitted"
                if col_st == "Flag Retrieved" or r.get("flag"):
                    flags_retrieved += 1
                if sub == "Accepted":
                    accepted += 1
                    flags_submitted += 1
                elif sub == "Invalid":
                    rejected += 1
                    flags_submitted += 1
                elif sub == "Already submitted":
                    duplicate += 1
                    flags_submitted += 1
                elif sub == "Submission Error":
                    errors += 1
                    flags_submitted += 1

                summary_results.append({
                    "target_id": r["target_id"],
                    "target_host": r["target_host"],
                    "target_name": r["target_name"],
                    "status": r["status"],
                    "collection_status": col_st,
                    "submission_status": sub,
                    "flag": r.get("flag", ""),
                    "duration_ms": r.get("duration_ms", 0),
                    "exit_code": r.get("exit_code"),
                    "error": r.get("stderr", "") if r.get("status") != "SUCCESS" else "",
                })

            total_ms = sum(r.get("duration_ms", 0) for r in rows)
            return {
                "tick": tick,
                "exploit_id": eid,
                "exploit_name": ename,
                "stage": "COMPLETED",
                "duration_ms": total_ms,
                "targets_count": len(rows),
                "flags_retrieved": flags_retrieved,
                "flags_submitted": flags_submitted,
                "accepted": accepted,
                "rejected": rejected,
                "duplicate": duplicate,
                "errors": errors,
                "results": summary_results,
            }
        finally:
            await db.close()

    # ── Scheduler loop ────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = await self._get_config()
        tick_count    = int(cfg.get("tick_count", 100))
        tick_interval = int(cfg.get("tick_interval", 60))
        timeout       = int(cfg.get("exploit_timeout", 30))
        # A zero tick count has always been labeled "continuous" in the UI.
        # Honor that configuration as well as the explicit continuous toggle.
        continuous    = cfg.get("continuous", "1") == "1" or tick_count == 0

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
            semaphore = self._semaphore or asyncio.Semaphore(int(cfg.get("concurrency", 20)))
            tick_task = asyncio.create_task(
                self._execute_tick(tick, timeout, semaphore),
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
        cfg = await self._get_config()
        db = await get_db()
        try:
            async with db.execute(
                "SELECT * FROM exploits WHERE enabled = 1 ORDER BY id"
            ) as cur:
                exploits = [dict(r) for r in await cur.fetchall()]
            async with db.execute(
                "SELECT * FROM targets WHERE enabled = 1 ORDER BY id"
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

        # Own team protection: exclude own team IP(s) from attack targets
        own_ip_raw = (cfg.get("team_ip") or "").strip()
        own_ips = {x.strip() for x in own_ip_raw.split(",") if x.strip()}
        if own_ips:
            opponents = [t for t in targets if t["host"] not in own_ips]
            skipped_count = len(targets) - len(opponents)
            if skipped_count > 0:
                logger.info("Tick %d: excluded %d own-team target(s) (%s)", tick, skipped_count, own_ips)
        else:
            opponents = targets

        if not opponents:
            logger.warning("Tick %d: no opponent targets to attack", tick)
            return

        # Execute service-by-service (batch collection then batch submission per service)
        for exploit in exploits:
            if self._stop_event.is_set():
                break
            await self._pause_event.wait()
            if self._stop_event.is_set():
                break

            mode = exploit.get("target_mode", "all")
            if mode == "all":
                job_targets = opponents
            else:
                ids = {int(x) for x in mode.split(",") if x.strip().isdigit()}
                job_targets = [t for t in opponents if t["id"] in ids]

            if not job_targets:
                continue

            await self._execute_service_batch(
                tick, exploit, job_targets, timeout, semaphore, cfg
            )

    # ── Service batch execution (Collect first, submit later) ─────────────────

    async def _execute_service_batch(
        self,
        tick: int,
        exploit: dict,
        targets: list[dict],
        timeout: int,
        semaphore: asyncio.Semaphore,
        cfg: dict,
    ) -> None:
        """
        Executes an exploit against all target IPs in batch:
          Phase 1: Concurrently run exploit on all target IPs and collect all flags.
                   No flags are submitted while any exploit is still running.
          Phase 2: Once collection from all targets completes, batch submit the collected flags.
          Phase 3: Record and broadcast per-IP collection & submission statuses.
        """
        t_service_start = time.monotonic()
        exploit_id = exploit["id"]
        service_name = exploit["name"]
        effective_timeout = int(exploit.get("timeout") or timeout or 30)
        collection_deadline = max(0, int(cfg.get("collection_deadline", effective_timeout) or 0))
        configured_concurrency = int(cfg.get("concurrency", 20) or 20)
        if configured_concurrency < len(targets):
            logger.warning(
                "Service %s has %d targets but concurrency=%d; targets beyond the limit wait for a slot. "
                "Set concurrency >= target count for fully parallel collection.",
                service_name, len(targets), configured_concurrency,
            )

        # ── STAGE 1: EXPLOITATION / COLLECTION ─────────────────────────────────
        broadcaster.publish({
            "type": "service_batch_start",
            "data": {
                "tick": tick,
                "exploit_id": exploit_id,
                "exploit_name": service_name,
                "stage": "COLLECTING",
                "targets_count": len(targets),
            }
        })

        target_results: dict[int, dict] = {}
        collected_flags_by_target: dict[int, list[str]] = {}
        all_collected_flags: list[str] = []

        async def _record_target_failure(target: dict, status: str, error: str) -> dict:
            now = datetime.now(timezone.utc).isoformat()
            res = self._make_result(
                tick, exploit, target,
                status=status,
                exit_code=None,
                stdout="",
                stderr=error,
                started_at=now,
                finished_at=now,
                duration_ms=0,
                flag="",
                collection_status="Flag Not Retrieved",
                submission_status="Not submitted",
            )
            await self._save_result(res)
            target_results[target["id"]] = res
            collected_flags_by_target[target["id"]] = []
            broadcaster.publish({"type": "job_done", "data": res})
            broadcaster.publish({
                "type": "service_target_done",
                "data": {
                    "tick": tick,
                    "exploit_id": exploit_id,
                    "exploit_name": service_name,
                    "target_id": target["id"],
                    "target_host": target["host"],
                    "target_name": target["name"],
                    "collection_status": "Flag Not Retrieved",
                    "submission_status": "Not submitted",
                    "flag": "",
                    "duration_ms": 0,
                    "status": status,
                }
            })
            return res

        async def _run_target_exploit(target: dict):
            if self._stop_event.is_set():
                return None
            async with semaphore:
                self._running_jobs += 1
                broadcaster.publish({
                    "type": "job_start",
                    "data": {
                        "tick": tick,
                        "exploit_id": exploit_id,
                        "exploit_name": service_name,
                        "target_id": target["id"],
                        "target_host": target["host"],
                        "target_name": target["name"],
                    }
                })
                try:
                    res = await self._execute_exploit(tick, exploit, target, effective_timeout, cfg)

                    # Flags are meaningful only after a successful script run.
                    # Protocol records are preferred; regex-only stdout remains
                    # supported for existing registered scripts.
                    from submitter import extract_flags
                    from exploit_output import extract_script_flags
                    pattern = cfg.get("flag_regex", "")
                    if res["status"] == JobStatus.SUCCESS:
                        flags, protocol_errors, _used_protocol = extract_script_flags(
                            res.get("stdout", ""), pattern, extract_flags
                        )
                        if protocol_errors:
                            detail = "Invalid AUFARM_RESULT record(s): " + "; ".join(protocol_errors)
                            res["stderr"] = (res.get("stderr", "") + "\n" + detail).strip()
                    else:
                        flags = []

                    if flags:
                        res["collection_status"] = "Flag Retrieved"
                        res["flag"] = ", ".join(flags)
                        collected_flags_by_target[target["id"]] = flags
                        all_collected_flags.extend(flags)
                        res["submission_status"] = "Submitting..."
                    else:
                        res["collection_status"] = "Flag Not Retrieved"
                        res["flag"] = ""
                        collected_flags_by_target[target["id"]] = []
                        res["submission_status"] = "Not submitted"

                    # Save execution result to SQLite
                    await self._save_result(res)
                    target_results[target["id"]] = res

                    broadcaster.publish({"type": "job_done", "data": res})
                    broadcaster.publish({
                        "type": "service_target_done",
                        "data": {
                            "tick": tick,
                            "exploit_id": exploit_id,
                            "exploit_name": service_name,
                            "target_id": target["id"],
                            "target_host": target["host"],
                            "target_name": target["name"],
                            "collection_status": res["collection_status"],
                            "submission_status": res["submission_status"],
                            "flag": res["flag"],
                            "duration_ms": res.get("duration_ms", 0),
                            "status": res["status"],
                        }
                    })
                    return res
                except Exception as exc:
                    logger.exception("Exploit crashed on target %s: %s", target["host"], exc)
                    now = datetime.now(timezone.utc).isoformat()
                    res = self._make_result(
                        tick, exploit, target,
                        status=JobStatus.CRASHED,
                        exit_code=None,
                        stdout="",
                        stderr=str(exc),
                        started_at=now,
                        finished_at=now,
                        duration_ms=0,
                        flag="",
                        collection_status="Flag Not Retrieved",
                        submission_status="Not submitted",
                    )
                    await self._save_result(res)
                    target_results[target["id"]] = res
                    collected_flags_by_target[target["id"]] = []
                    broadcaster.publish({"type": "job_done", "data": res})
                    broadcaster.publish({
                        "type": "service_target_done",
                        "data": {
                            "tick": tick,
                            "exploit_id": exploit_id,
                            "exploit_name": service_name,
                            "target_id": target["id"],
                            "target_host": target["host"],
                            "target_name": target["name"],
                            "collection_status": "Flag Not Retrieved",
                            "submission_status": "Not submitted",
                            "flag": "",
                            "duration_ms": 0,
                            "status": JobStatus.CRASHED,
                        }
                    })
                    return res
                finally:
                    self._running_jobs -= 1

        # Execute concurrently across all targets for this service. The semaphore only
        # enforces the configured upper bound; all target tasks are launched at once.
        tasks = [
            asyncio.create_task(
                _run_target_exploit(tgt),
                name=f"tick-{tick}-e{exploit_id}-t{tgt['id']}"
            )
            for tgt in targets
        ]

        if collection_deadline > 0:
            _done, pending = await asyncio.wait(tasks, timeout=collection_deadline)
            if pending:
                logger.warning(
                    "Service %s collection deadline reached after %ds; cancelling %d unfinished target(s)",
                    service_name, collection_deadline, len(pending),
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        gather_results = await asyncio.gather(*tasks, return_exceptions=True)
        for target, task_result in zip(targets, gather_results):
            if isinstance(task_result, BaseException):
                logger.warning(
                    "Service %s target %s ended with unhandled task exception: %r",
                    service_name, target["host"], task_result,
                )
                if target["id"] not in target_results:
                    status = JobStatus.CANCELLED if isinstance(task_result, asyncio.CancelledError) else JobStatus.CRASHED
                    error = "Collection deadline reached before this target completed" if status == JobStatus.CANCELLED else str(task_result)
                    await _record_target_failure(target, status, error)
            elif task_result is None and target["id"] not in target_results and not self._stop_event.is_set():
                await _record_target_failure(target, JobStatus.CANCELLED, "Target task did not produce a result")

        if self._stop_event.is_set():
            return

        # ── STAGE 2: BATCH SUBMISSION ──────────────────────────────────────────
        # All targets for this service/vulnerability have finished!
        # Mark the scheduler-owned submission phase before queueing flags so the
        # background submitter cannot pick up this batch first.
        self._batch_submission_active = True
        flag_outcomes: dict[str, tuple[str, str]] = {}
        try:
            from submitter import queue_flags
            targets_by_id = {t["id"]: t for t in targets}
            for tid, t_flags in collected_flags_by_target.items():
                if t_flags and tid in targets_by_id:
                    tgt = targets_by_id[tid]
                    await queue_flags(
                        t_flags,
                        exploit_name=service_name,
                        target_host=tgt["host"],
                        target_name=tgt["name"],
                        tick=tick,
                    )

            unique_flags = list(dict.fromkeys(all_collected_flags))
            submit_enabled = cfg.get("submit_enabled", "0") == "1"

            broadcaster.publish({
                "type": "service_batch_collect_done",
                "data": {
                    "tick": tick,
                    "exploit_id": exploit_id,
                    "exploit_name": service_name,
                    "stage": "SUBMITTING",
                    "flags_retrieved": len(unique_flags),
                    "targets_count": len(targets),
                }
            })

            if unique_flags:
                if submit_enabled:
                    try:
                        from submitter import submit_service_batch
                        flag_outcomes = await submit_service_batch(unique_flags, cfg)
                    except Exception as exc:
                        logger.error("Batch submission failed for service %s: %s", service_name, exc)
                        for f in unique_flags:
                            flag_outcomes[f] = ("ERROR", str(exc))
                else:
                    for f in unique_flags:
                        flag_outcomes[f] = ("QUEUED", "Auto-submit disabled")
        finally:
            self._batch_submission_active = False

        # Map submission outcomes back to targets
        from submitter import FlagStatus
        for target in targets:
            tid = target["id"]
            res = target_results.get(tid)
            if not res:
                continue
            t_flags = collected_flags_by_target.get(tid, [])
            if not t_flags:
                sub_status = "Not submitted"
            elif not submit_enabled:
                sub_status = "Not submitted (auto-submit disabled)"
            else:
                statuses = [flag_outcomes.get(f, (FlagStatus.ERROR, ""))[0] for f in t_flags]
                if any(s == FlagStatus.ACCEPTED for s in statuses):
                    sub_status = "Accepted"
                elif any(s == FlagStatus.DUPLICATE for s in statuses):
                    sub_status = "Already submitted"
                elif any(s == FlagStatus.REJECTED for s in statuses):
                    sub_status = "Invalid"
                else:
                    sub_status = "Submission Error"

            res["submission_status"] = sub_status
            if "id" in res:
                await self._update_result_submission(res["id"], sub_status)

        # ── STAGE 3: COMPLETED RESULTS ────────────────────────────────────────
        service_duration_ms = int((time.monotonic() - t_service_start) * 1000)

        accepted_count = sum(1 for f, (st, _) in flag_outcomes.items() if st == FlagStatus.ACCEPTED)
        rejected_count = sum(1 for f, (st, _) in flag_outcomes.items() if st == FlagStatus.REJECTED)
        dup_count = sum(1 for f, (st, _) in flag_outcomes.items() if st == FlagStatus.DUPLICATE)
        err_count = sum(1 for f, (st, _) in flag_outcomes.items() if st == FlagStatus.ERROR)

        summary_results = []
        for target in targets:
            tid = target["id"]
            res = target_results.get(tid, {})
            summary_results.append({
                "target_id": tid,
                "target_host": target["host"],
                "target_name": target["name"],
                "status": res.get("status", "FAILED"),
                "collection_status": res.get("collection_status", "Flag Not Retrieved"),
                "submission_status": res.get("submission_status", "Not submitted"),
                "flag": res.get("flag", ""),
                "duration_ms": res.get("duration_ms", 0),
                "exit_code": res.get("exit_code"),
                "error": res.get("stderr", "") if res.get("status") != "SUCCESS" else "",
            })

        batch_summary = {
            "tick": tick,
            "exploit_id": exploit_id,
            "exploit_name": service_name,
            "stage": "COMPLETED",
            "duration_ms": service_duration_ms,
            "targets_count": len(targets),
            "flags_retrieved": len(unique_flags),
            "flags_submitted": len(unique_flags) if submit_enabled else 0,
            "accepted": accepted_count,
            "rejected": rejected_count,
            "duplicate": dup_count,
            "errors": err_count,
            "results": summary_results,
        }

        self._latest_batch_result = batch_summary
        broadcaster.publish({
            "type": "service_batch_complete",
            "data": batch_summary,
        })

    # ── Exploit Process Execution ─────────────────────────────────────────────

    async def _execute_exploit(
        self,
        tick: int,
        exploit: dict,
        target: dict,
        timeout: int,
        cfg: Optional[dict] = None,
    ) -> dict:
        if cfg is None:
            cfg = await self._get_config()

        script_path = os.path.join(EXPLOITS_DIR, exploit["filename"])
        runtime = exploit.get("runtime", "python3").strip()
        extra_args = exploit.get("args", "").strip()

        # Stable interface: <runtime> <script> <host> <port> [extra_args...].
        # shlex allows configured arguments such as --label "blue team".
        cmd = []
        if runtime:
            cmd.append(runtime)
        cmd.append(script_path)
        cmd.append(target["host"])
        cmd.append(str(target.get("port", 0)))
        try:
            if extra_args:
                cmd.extend(shlex.split(extra_args))
        except ValueError as exc:
            return self._make_result(
                tick, exploit, target,
                status=JobStatus.CRASHED,
                exit_code=None,
                stdout="",
                stderr=f"Invalid configured extra arguments: {exc}",
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=0,
            )

        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "TARGET_HOST": target["host"],
            "TARGET_PORT": str(target.get("port", 0)),
            "TARGET_NAME": target.get("name", ""),
            "TARGET_SERVICE": target.get("service", ""),
            "AUFARM_TICK": str(tick),
            "TEAM_ID": cfg.get("team_id", "1"),
            "TEAM_NAME": cfg.get("team_name", "MyTeam"),
            "TEAM_IP": cfg.get("team_ip", ""),
        }

        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        status = JobStatus.FAILED
        exit_code = None
        stdout = ""
        stderr = ""
        proc: Optional[asyncio.subprocess.Process] = None

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
                start_new_session=True,
            )
            async with self._procs_lock:
                self._running_procs.append(proc)

            communicate_task = asyncio.create_task(proc.communicate())
            try:
                raw_stdout, raw_stderr = await asyncio.wait_for(
                    asyncio.shield(communicate_task), timeout=float(timeout)
                )
                exit_code = proc.returncode
                stdout = raw_stdout[:STDOUT_LIMIT].decode(errors="replace")
                stderr = raw_stderr[:STDERR_LIMIT].decode(errors="replace")
                status = JobStatus.SUCCESS if exit_code == 0 else JobStatus.FAILED

            except asyncio.TimeoutError:
                status = JobStatus.TIMEOUT
                await self._terminate_proc_tree(proc)
                raw_stdout, raw_stderr = await communicate_task
                stdout = raw_stdout[:STDOUT_LIMIT].decode(errors="replace")
                stderr = (raw_stderr[:STDERR_LIMIT].decode(errors="replace") + f"\nTimed out after {timeout}s").strip()
                exit_code = proc.returncode
            except asyncio.CancelledError:
                status = JobStatus.CANCELLED
                await self._terminate_proc_tree(proc)
                raw_stdout, raw_stderr = await communicate_task
                stdout = raw_stdout[:STDOUT_LIMIT].decode(errors="replace")
                stderr = (raw_stderr[:STDERR_LIMIT].decode(errors="replace") + "\nCancelled by collection deadline or farm stop").strip()
                exit_code = proc.returncode

        except asyncio.CancelledError:
            status = JobStatus.CANCELLED
            stderr = "Cancelled before exploit process completed"
            if proc is not None:
                await self._terminate_proc_tree(proc)
        except FileNotFoundError:
            status = JobStatus.CRASHED
            stderr = f"Runtime not found: {runtime}"
        except Exception as exc:
            status = JobStatus.CRASHED
            stderr = str(exc)
        finally:
            if proc is not None:
                async with self._procs_lock:
                    try:
                        self._running_procs.remove(proc)
                    except ValueError:
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
        flag: str = "",
        collection_status: str = "",
        submission_status: str = "Not submitted",
    ) -> dict:
        return {
            "tick":              tick,
            "exploit_id":        exploit["id"],
            "exploit_name":      exploit["name"],
            "target_id":         target["id"],
            "target_host":       target["host"],
            "target_name":       target["name"],
            "status":            status,
            "exit_code":         exit_code,
            "duration_ms":       duration_ms,
            "stdout":            stdout,
            "stderr":            stderr,
            "flag":              flag,
            "collection_status": collection_status,
            "submission_status": submission_status,
            "started_at":        started_at,
            "finished_at":       finished_at,
        }

    async def _save_result(self, r: dict) -> int:
        db = await get_db()
        try:
            cur = await db.execute(
                """
                INSERT INTO results
                    (tick, exploit_id, exploit_name, target_id, target_host, target_name,
                     status, exit_code, duration_ms, stdout, stderr, flag,
                     collection_status, submission_status, started_at, finished_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["tick"], r["exploit_id"], r["exploit_name"],
                    r["target_id"], r["target_host"], r["target_name"],
                    r["status"], r["exit_code"], r["duration_ms"],
                    r["stdout"], r["stderr"],
                    r.get("flag", ""),
                    r.get("collection_status", ""),
                    r.get("submission_status", "Not submitted"),
                    r["started_at"], r["finished_at"],
                )
            )
            await db.commit()
            r["id"] = cur.lastrowid
            return cur.lastrowid
        finally:
            await db.close()

    async def _update_result_submission(self, result_id: int, submission_status: str) -> None:
        db = await get_db()
        try:
            await db.execute(
                "UPDATE results SET submission_status = ? WHERE id = ?",
                (submission_status, result_id)
            )
            await db.commit()
        finally:
            await db.close()

    # ── Flag extraction helper (legacy / standalone compatibility) ────────────

    async def _queue_flags_from_result(self, result: dict) -> None:
        """Read the flag regex from config and queue any found flags."""
        try:
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
        except Exception as exc:
            logger.warning("Flag extraction error: %s", exc)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def _terminate_proc_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate an exploit process group so child processes do not survive timeouts."""
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.terminate()
            except ProcessLookupError:
                return

        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
            return
        except asyncio.TimeoutError:
            pass

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass

    async def _kill_all_procs(self) -> None:
        async with self._procs_lock:
            procs = list(self._running_procs)
            self._running_procs.clear()
        for proc in procs:
            await self._terminate_proc_tree(proc)


# Global singleton
scheduler = FarmScheduler()
