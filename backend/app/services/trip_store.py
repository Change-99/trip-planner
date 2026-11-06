"""旅行计划与任务的 SQLite 持久化存储。

这里只存储结构化请求、经校验的最终计划和工作流阶段；
浏览器不再是行程数据的唯一来源。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.models.schemas import TripPlan, TripRequest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TripRepository:
    """用短连接和进程内锁实现的轻量 SQLite Repository。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        backend_root = Path(__file__).resolve().parents[2]
        self.db_path = Path(db_path or backend_root / "data" / "trip_planner.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trip_plans (
                    plan_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_plan_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trip_jobs (
                    job_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    error TEXT,
                    revision INTEGER NOT NULL,
                    parent_plan_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def create_job(
        self,
        request: TripRequest,
        *,
        parent_plan_id: str | None = None,
        revision: int = 1,
    ) -> dict[str, Any]:
        job_id = uuid4().hex
        plan_id = uuid4().hex
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trip_jobs (
                    job_id, plan_id, request_json, status, stage, progress,
                    error, revision, parent_plan_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    plan_id,
                    request.model_dump_json(),
                    "queued",
                    "已提交规划任务",
                    0,
                    revision,
                    parent_plan_id,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_job(job_id) or {}

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        progress: int,
        error: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE trip_jobs
                   SET status = ?, stage = ?, progress = ?, error = ?, updated_at = ?
                 WHERE job_id = ?
                """,
                (
                    status,
                    stage,
                    min(max(progress, 0), 100),
                    error,
                    _now(),
                    job_id,
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trip_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_plan(
        self,
        plan_id: str,
        request: TripRequest,
        plan: TripPlan,
        *,
        parent_plan_id: str | None = None,
        revision: int = 1,
    ) -> None:
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trip_plans (
                    plan_id, request_json, plan_json, revision,
                    parent_plan_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    request.model_dump_json(),
                    plan.model_dump_json(),
                    revision,
                    parent_plan_id,
                    timestamp,
                    timestamp,
                ),
            )

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trip_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["request"] = json.loads(record.pop("request_json"))
        record["data"] = json.loads(record.pop("plan_json"))
        return record


_repository: TripRepository | None = None


def get_trip_repository() -> TripRepository:
    global _repository
    if _repository is None:
        _repository = TripRepository()
    return _repository
