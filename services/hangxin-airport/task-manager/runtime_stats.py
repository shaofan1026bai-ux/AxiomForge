"""按北京时间累计模式、策略开启时长，并保存开关状态。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")


def _local(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带有时区")
    return value.astimezone(TIMEZONE)


def _month(value: datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _seconds(start: datetime, finish: datetime) -> float:
    return (
        finish.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    ).total_seconds()


def _segments(started_at: datetime, finished_at: datetime) -> list[tuple[str, float]]:
    start = _local(started_at)
    finish = _local(finished_at)
    if _seconds(start, finish) <= 0:
        return []

    rows = []
    while _month(start) != _month(finish):
        year = start.year + (start.month == 12)
        month = 1 if start.month == 12 else start.month + 1
        boundary = datetime(year, month, 1, tzinfo=TIMEZONE)
        rows.append((_month(start), _seconds(start, boundary)))
        start = boundary
    if _seconds(start, finish) > 0:
        rows.append((_month(start), _seconds(start, finish)))
    return rows


class RuntimeStats:
    def __init__(self, path: Path):
        self.path = Path(path)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS monthly_runtime (
                        task_id TEXT NOT NULL,
                        month TEXT NOT NULL,
                        seconds REAL NOT NULL,
                        PRIMARY KEY (task_id, month)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS monthly_mode_runtime (
                        month TEXT PRIMARY KEY,
                        seconds REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS manager_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        mode_enabled INTEGER NOT NULL CHECK (mode_enabled IN (0, 1)),
                        last_checkpoint_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_state (
                        task_id TEXT PRIMARY KEY NOT NULL,
                        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
                    )
                    """
                )

    @staticmethod
    def _add_task_runtime(connection, rows) -> None:
        connection.executemany(
            """
            INSERT INTO monthly_runtime (task_id, month, seconds)
            VALUES (?, ?, ?)
            ON CONFLICT (task_id, month)
            DO UPDATE SET seconds = seconds + excluded.seconds
            """,
            rows,
        )

    def record(
        self, task_id: str, started_at: datetime, finished_at: datetime
    ) -> None:
        rows = [(task_id, month, seconds) for month, seconds in _segments(started_at, finished_at)]
        if not rows:
            return
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                self._add_task_runtime(connection, rows)

    def load_state(
        self, default_mode: bool, default_tasks: dict[str, bool]
    ) -> tuple[bool, dict[str, bool]]:
        """只初始化没有保存过的开关，不根据上次检查点补计停服时间。"""
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO manager_state (id, mode_enabled) VALUES (1, ?)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (int(default_mode),),
                )
                connection.executemany(
                    """
                    INSERT INTO task_state (task_id, enabled) VALUES (?, ?)
                    ON CONFLICT (task_id) DO NOTHING
                    """,
                    [(task_id, int(enabled)) for task_id, enabled in default_tasks.items()],
                )
                mode = connection.execute(
                    "SELECT mode_enabled FROM manager_state WHERE id = 1"
                ).fetchone()[0]
                tasks = dict(connection.execute("SELECT task_id, enabled FROM task_state"))
        return bool(mode), {task_id: bool(tasks[task_id]) for task_id in default_tasks}

    def checkpoint(
        self,
        at: datetime,
        mode_since: datetime | None,
        task_since: dict[str, datetime],
        mode_enabled: bool,
        task_enabled: dict[str, bool],
    ) -> None:
        """原子保存旧有效时段及新开关；成功后调用方将有效段起点推进至 at。"""
        at = _local(at)
        mode_rows = _segments(mode_since, at) if mode_since is not None else []
        task_rows = [
            (task_id, month, seconds)
            for task_id, since in task_since.items()
            for month, seconds in _segments(since, at)
        ]
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO monthly_mode_runtime (month, seconds) VALUES (?, ?)
                    ON CONFLICT (month)
                    DO UPDATE SET seconds = seconds + excluded.seconds
                    """,
                    mode_rows,
                )
                self._add_task_runtime(connection, task_rows)
                connection.execute(
                    """
                    INSERT INTO manager_state (id, mode_enabled, last_checkpoint_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        mode_enabled = excluded.mode_enabled,
                        last_checkpoint_at = excluded.last_checkpoint_at
                    """,
                    (int(mode_enabled), at.isoformat()),
                )
                connection.executemany(
                    """
                    INSERT INTO task_state (task_id, enabled) VALUES (?, ?)
                    ON CONFLICT (task_id) DO UPDATE SET enabled = excluded.enabled
                    """,
                    [(task_id, int(enabled)) for task_id, enabled in task_enabled.items()],
                )

    def _hours(
        self,
        query: str,
        parameters: tuple,
        at: datetime,
        active_since: datetime | None,
        *,
        monthly: bool,
    ) -> float:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(query, parameters).fetchone()
        seconds = row[0] if row is not None and row[0] is not None else 0.0
        if active_since is not None:
            start = _local(active_since)
            if monthly:
                start = max(start, datetime(at.year, at.month, 1, tzinfo=TIMEZONE))
            seconds += max(0.0, _seconds(start, at))
        return round(seconds / 3600, 4)

    def hours(
        self,
        task_id: str,
        at: datetime,
        active_since: datetime | None = None,
    ) -> float:
        at = _local(at)
        return self._hours(
            "SELECT seconds FROM monthly_runtime WHERE task_id = ? AND month = ?",
            (task_id, _month(at)),
            at,
            active_since,
            monthly=True,
        )

    def total_hours(
        self,
        task_id: str,
        at: datetime,
        active_since: datetime | None = None,
    ) -> float:
        return self._hours(
            "SELECT SUM(seconds) FROM monthly_runtime WHERE task_id = ?",
            (task_id,),
            _local(at),
            active_since,
            monthly=False,
        )

    def mode_hours(self, at: datetime, active_since: datetime | None = None) -> float:
        at = _local(at)
        return self._hours(
            "SELECT seconds FROM monthly_mode_runtime WHERE month = ?",
            (_month(at),),
            at,
            active_since,
            monthly=True,
        )

    def mode_total_hours(self, at: datetime, active_since: datetime | None = None) -> float:
        return self._hours(
            "SELECT SUM(seconds) FROM monthly_mode_runtime",
            (),
            _local(at),
            active_since,
            monthly=False,
        )
