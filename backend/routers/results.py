"""
AuFarm — routers/results.py

Execution result history API.
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from database import get_db

router = APIRouter(prefix="/api/results", tags=["results"])


@router.get("")
async def list_results(
    tick:        Optional[int] = None,
    exploit_id:  Optional[int] = None,
    target_id:   Optional[int] = None,
    status:      Optional[str] = None,
    limit:       int = Query(default=200, le=1000),
    offset:      int = Query(default=0, ge=0),
):
    conditions = []
    values = []

    if tick is not None:
        conditions.append("tick = ?")
        values.append(tick)
    if exploit_id is not None:
        conditions.append("exploit_id = ?")
        values.append(exploit_id)
    if target_id is not None:
        conditions.append("target_id = ?")
        values.append(target_id)
    if status:
        conditions.append("status = ?")
        values.append(status.upper())

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    values += [limit, offset]

    db = await get_db()
    try:
        async with db.execute(
            f"""
            SELECT * FROM results
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            values
        ) as cur:
            rows = await cur.fetchall()

        # Total count
        count_values = values[:-2]
        async with db.execute(
            f"SELECT COUNT(*) as cnt FROM results {where}", count_values
        ) as cur:
            cnt_row = await cur.fetchone()
        total = cnt_row["cnt"] if cnt_row else 0

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": [dict(r) for r in rows],
        }
    finally:
        await db.close()


@router.get("/stats")
async def results_stats():
    """Summary stats for the dashboard."""
    db = await get_db()
    try:
        stats = {}
        for st in ("SUCCESS", "FAILED", "TIMEOUT", "CRASHED", "CANCELLED", "SKIPPED"):
            async with db.execute(
                "SELECT COUNT(*) as cnt FROM results WHERE status = ?", (st,)
            ) as cur:
                row = await cur.fetchone()
                stats[st.lower()] = row["cnt"] if row else 0

        async with db.execute("SELECT COUNT(DISTINCT tick) as cnt FROM results") as cur:
            row = await cur.fetchone()
            stats["ticks_executed"] = row["cnt"] if row else 0

        return stats
    finally:
        await db.close()


@router.get("/latest-batch")
async def get_latest_batch():
    """Return the most recent service/vulnerability batch run and per-IP results."""
    from scheduler import scheduler
    batch = await scheduler.get_latest_batch()
    if not batch:
        return {"active": False, "message": "No batch runs recorded yet"}
    return {"active": True, "batch": batch}


@router.get("/{result_id}")
async def get_result(result_id: int):
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM results WHERE id = ?", (result_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Result not found")
        return dict(row)
    finally:
        await db.close()
