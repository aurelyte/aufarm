"""
AuFarm — models.py

Pydantic models for request/response validation and internal data structures.
"""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel


# ── Target Models ─────────────────────────────────────────────────────────────

class TargetCreate(BaseModel):
    name: str
    host: str
    port: int = 0
    service: str = ""
    enabled: bool = True


class TargetUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    service: Optional[str] = None
    enabled: Optional[bool] = None


class TargetOut(BaseModel):
    id: int
    name: str
    host: str
    port: int
    service: str
    enabled: bool
    created_at: str


# ── Exploit Models ─────────────────────────────────────────────────────────────

class ExploitCreate(BaseModel):
    name: str
    filename: str
    runtime: str = "python3"
    args: str = ""
    enabled: bool = True
    timeout: int = 30
    target_mode: str = "all"   # "all" or comma-separated target IDs


class ExploitUpdate(BaseModel):
    name: Optional[str] = None
    runtime: Optional[str] = None
    args: Optional[str] = None
    enabled: Optional[bool] = None
    timeout: Optional[int] = None
    target_mode: Optional[str] = None
    source: Optional[str] = None   # updated source code (written to file)


class ExploitOut(BaseModel):
    id: int
    name: str
    filename: str
    runtime: str
    args: str
    enabled: bool
    timeout: int
    target_mode: str
    created_at: str
    updated_at: str


# ── Farm Config Models ─────────────────────────────────────────────────────────

class FarmConfig(BaseModel):
    tick_count: int = 100
    tick_interval: int = 60
    exploit_timeout: int = 30
    concurrency: int = 20
    continuous: bool = False


class FarmStatus(BaseModel):
    status: str           # STOPPED | RUNNING | PAUSED
    current_tick: int
    tick_count: int
    tick_interval: int
    exploit_timeout: int
    concurrency: int
    continuous: bool
    running_jobs: int
    stats: dict


# ── Result Models ─────────────────────────────────────────────────────────────

class ResultOut(BaseModel):
    id: int
    tick: int
    exploit_id: int
    exploit_name: str
    target_id: int
    target_host: str
    target_name: str
    status: str
    exit_code: Optional[int]
    duration_ms: Optional[int]
    stdout: str
    stderr: str
    started_at: Optional[str]
    finished_at: Optional[str]
    created_at: str
