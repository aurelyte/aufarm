"""
AuFarm — routers/targets.py

CRUD API for competition targets.
"""

from fastapi import APIRouter, HTTPException
from typing import List

from database import get_db
from models import TargetCreate, TargetUpdate, TargetOut

router = APIRouter(prefix="/api/targets", tags=["targets"])


@router.get("", response_model=List[TargetOut])
async def list_targets():
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM targets ORDER BY id") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.post("", response_model=TargetOut, status_code=201)
async def create_target(body: TargetCreate):
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO targets (name, host, port, service, enabled) VALUES (?, ?, ?, ?, ?)",
            (body.name, body.host, body.port, body.service, int(body.enabled))
        )
        await db.commit()
        row_id = cur.lastrowid
        async with db.execute("SELECT * FROM targets WHERE id = ?", (row_id,)) as c:
            row = await c.fetchone()
        return dict(row)
    finally:
        await db.close()


@router.get("/{target_id}", response_model=TargetOut)
async def get_target(target_id: int):
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM targets WHERE id = ?", (target_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Target not found")
        return dict(row)
    finally:
        await db.close()


@router.put("/{target_id}", response_model=TargetOut)
async def update_target(target_id: int, body: TargetUpdate):
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM targets WHERE id = ?", (target_id,)) as cur:
            existing = await cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Target not found")

        fields = body.model_dump(exclude_none=True)
        if not fields:
            return dict(existing)

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [target_id]
        await db.execute(f"UPDATE targets SET {set_clause} WHERE id = ?", values)
        await db.commit()

        async with db.execute("SELECT * FROM targets WHERE id = ?", (target_id,)) as cur:
            row = await cur.fetchone()
        return dict(row)
    finally:
        await db.close()


@router.delete("/{target_id}", status_code=204)
async def delete_target(target_id: int):
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM targets WHERE id = ?", (target_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Target not found")
        await db.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        await db.commit()
    finally:
        await db.close()
