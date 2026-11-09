"""本机流程图版本库；每次操作独立连接，事务保护并发与版本不可变性。"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from axiomforge.algorithm_flowcharts import (
    FlowchartConflictError,
    FlowchartDraft,
    FlowchartError,
    FlowchartRevision,
    content_hash,
    seed_draft,
    validate_draft,
    validate_project,
)

SEED_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "axiomforge:hangxin-airport:ahu-running-strategy"))
SCHEMA_VERSION = "1"


class FlowchartStore:
    """所有查询携带项目归属；未知ID与跨项目ID使用同一种未找到错误。"""

    def __init__(self, path: Path | None = None) -> None:
        try:
            self.path = (
                Path(
                    path
                    or os.environ.get("AXIOMFORGE_FLOWCHART_DB")
                    or Path.cwd() / ".nicegui/algorithm-flowcharts.sqlite3"
                )
                .expanduser()
                .resolve()
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except FlowchartError:
            raise
        except (OSError, ValueError, TypeError):
            raise FlowchartError("图示版本库无法打开，请检查本地存储配置。") from None

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except FlowchartError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError):
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise FlowchartError("图示版本库操作失败，原版本和当前草稿均未被覆盖。") from None
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            # 不使用executescript，避免其隐式提交破坏建表与种子导入的事务边界。
            for statement in (
                """CREATE TABLE IF NOT EXISTS flowchart_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS flowcharts (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    latest_revision INTEGER NOT NULL CHECK(latest_revision > 0),
                    seed_key TEXT UNIQUE
                )""",
                """CREATE TABLE IF NOT EXISTS flowchart_revisions (
                    flowchart_id TEXT NOT NULL REFERENCES flowcharts(id),
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    name TEXT NOT NULL, description TEXT NOT NULL, source TEXT NOT NULL,
                    annotations_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, derived_from_revision INTEGER,
                    PRIMARY KEY(flowchart_id, revision),
                    FOREIGN KEY(flowchart_id, derived_from_revision)
                        REFERENCES flowchart_revisions(flowchart_id, revision)
                )""",
                """CREATE TRIGGER IF NOT EXISTS flowchart_revision_no_update
                   BEFORE UPDATE ON flowchart_revisions BEGIN
                     SELECT RAISE(ABORT, 'immutable_revision'); END""",
                """CREATE TRIGGER IF NOT EXISTS flowchart_revision_no_delete
                   BEFORE DELETE ON flowchart_revisions BEGIN
                     SELECT RAISE(ABORT, 'immutable_revision'); END""",
                """CREATE TRIGGER IF NOT EXISTS flowchart_scope_no_update
                   BEFORE UPDATE OF id, project_id, created_at, seed_key ON flowcharts BEGIN
                     SELECT RAISE(ABORT, 'immutable_scope'); END""",
                "CREATE INDEX IF NOT EXISTS flowchart_project ON flowcharts(project_id)",
            ):
                connection.execute(statement)
            version = connection.execute(
                "SELECT value FROM flowchart_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version is not None and version["value"] != SCHEMA_VERSION:
                raise FlowchartError("图示版本库格式不受当前程序支持，请保留原文件。")
            connection.execute(
                """INSERT OR IGNORE INTO flowchart_metadata (key, value)
                   VALUES ('schema_version', ?)""",
                (SCHEMA_VERSION,),
            )
            seed = connection.execute(
                "SELECT id, project_id FROM flowcharts WHERE seed_key = 'hangxin-ahu'"
            ).fetchone()
            if seed is not None:
                if seed["id"] != SEED_ID or seed["project_id"] != "hangxin-airport":
                    raise FlowchartError("图示种子记录归属异常，请保留原文件并检查版本库。")
            else:
                self._insert_new(
                    connection, "hangxin-airport", seed_draft(), SEED_ID, seed_key="hangxin-ahu"
                )

    @staticmethod
    def _validate_id(flowchart_id: str) -> None:
        try:
            if not isinstance(flowchart_id, str) or str(uuid.UUID(flowchart_id)) != flowchart_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise FlowchartError("图示ID无效。") from None

    @staticmethod
    def _validate_revision(revision: int | None) -> None:
        if revision is not None and (
            not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
        ):
            raise FlowchartError("图示版本号无效。")

    @staticmethod
    def _decode(row: sqlite3.Row) -> FlowchartRevision:
        try:
            annotations = json.loads(row["annotations_json"])
            draft = FlowchartDraft(row["name"], row["description"], row["source"], annotations)
            if content_hash(draft) != row["content_hash"]:
                raise FlowchartError("图示版本内容校验失败，请保留原文件。")
            created_at = datetime.fromisoformat(row["created_at"])
            if created_at.tzinfo is None:
                raise ValueError
            revision, derived = row["revision"], row["derived_from_revision"]
            if not isinstance(revision, int) or revision < 1:
                raise ValueError
            if derived is not None and (
                not isinstance(derived, int) or not 1 <= derived < revision
            ):
                raise ValueError
            return FlowchartRevision(
                row["flowchart_id"],
                row["project_id"],
                revision,
                draft.name,
                draft.description,
                draft.source,
                draft.annotations,
                row["content_hash"],
                created_at,
                derived,
            )
        except FlowchartError:
            raise
        except (ValueError, TypeError, KeyError, IndexError):
            raise FlowchartError("图示版本记录无法解析，请保留原文件。") from None

    @staticmethod
    def _find(
        connection: sqlite3.Connection,
        project_id: str,
        flowchart_id: str,
        revision: int | None = None,
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT r.*, f.project_id FROM flowchart_revisions r
               JOIN flowcharts f ON f.id = r.flowchart_id
               WHERE f.project_id = ? AND f.id = ?
               AND r.revision = COALESCE(?, f.latest_revision)""",
            (project_id, flowchart_id, revision),
        ).fetchone()
        if row is None:
            raise FlowchartError("当前项目中未找到该图示或版本。")
        return row

    def list_latest(self, project_id: str) -> list[FlowchartRevision]:
        validate_project(project_id)
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT r.*, f.project_id FROM flowcharts f
                   LEFT JOIN flowchart_revisions r ON f.id = r.flowchart_id
                        AND r.revision = f.latest_revision
                   WHERE f.project_id = ? ORDER BY f.created_at, f.id""",
                (project_id,),
            ).fetchall()
            return [self._decode(row) for row in rows]

    def get(
        self, project_id: str, flowchart_id: str, revision: int | None = None
    ) -> FlowchartRevision:
        validate_project(project_id)
        self._validate_id(flowchart_id)
        self._validate_revision(revision)
        with self._connection() as connection:
            return self._decode(self._find(connection, project_id, flowchart_id, revision))

    def list_revisions(self, project_id: str, flowchart_id: str) -> list[FlowchartRevision]:
        validate_project(project_id)
        self._validate_id(flowchart_id)
        with self._connection() as connection:
            self._find(connection, project_id, flowchart_id)
            rows = connection.execute(
                """SELECT r.*, f.project_id FROM flowchart_revisions r
                   JOIN flowcharts f ON f.id = r.flowchart_id
                   WHERE f.project_id = ? AND f.id = ? ORDER BY r.revision DESC""",
                (project_id, flowchart_id),
            ).fetchall()
            return [self._decode(row) for row in rows]

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        flowchart_id: str,
        revision: int,
        draft: FlowchartDraft,
        created_at: str,
        derived_from_revision: int | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO flowchart_revisions
               (flowchart_id, revision, name, description, source, annotations_json,
                content_hash, created_at, derived_from_revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                flowchart_id,
                revision,
                draft.name,
                draft.description,
                draft.source,
                json.dumps(draft.annotations, ensure_ascii=False, sort_keys=True),
                content_hash(draft),
                created_at,
                derived_from_revision,
            ),
        )

    def _insert_new(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        draft: FlowchartDraft,
        flowchart_id: str,
        *,
        seed_key: str | None = None,
    ) -> FlowchartRevision:
        created_at = datetime.now(UTC).isoformat()
        connection.execute(
            """INSERT INTO flowcharts (id, project_id, created_at, latest_revision, seed_key)
               VALUES (?, ?, ?, 1, ?)""",
            (flowchart_id, project_id, created_at, seed_key),
        )
        self._insert_revision(connection, flowchart_id, 1, draft, created_at)
        return self._decode(self._find(connection, project_id, flowchart_id))

    def save(
        self,
        project_id: str,
        draft: FlowchartDraft,
        *,
        flowchart_id: str | None = None,
        expected_revision: int | None = None,
        derived_from_revision: int | None = None,
    ) -> FlowchartRevision:
        validate_project(project_id)
        validate_draft(draft)
        self._validate_revision(expected_revision)
        self._validate_revision(derived_from_revision)
        # 对输入做一次独立快照，保存期间调用方不能修改嵌套注释来改变内容指纹。
        frozen = FlowchartDraft(
            draft.name,
            draft.description,
            draft.source,
            json.loads(json.dumps(draft.annotations, ensure_ascii=False)),
        )
        with self._connection(write=True) as connection:
            if flowchart_id is None:
                if expected_revision is not None or derived_from_revision is not None:
                    raise FlowchartError("新建图示不能指定已有版本基线。")
                return self._insert_new(connection, project_id, frozen, str(uuid.uuid4()))
            self._validate_id(flowchart_id)
            latest = self._decode(self._find(connection, project_id, flowchart_id))
            if expected_revision is None or latest.revision != expected_revision:
                raise FlowchartConflictError(
                    "图示已有新版本，请保留草稿并重新载入最新版本后再保存。"
                )
            if derived_from_revision is not None:
                self._find(connection, project_id, flowchart_id, derived_from_revision)
            if latest.content_hash == content_hash(frozen):
                return latest
            revision = latest.revision + 1
            self._insert_revision(
                connection,
                flowchart_id,
                revision,
                frozen,
                datetime.now(UTC).isoformat(),
                derived_from_revision,
            )
            connection.execute(
                "UPDATE flowcharts SET latest_revision = ? WHERE id = ? AND project_id = ?",
                (revision, flowchart_id, project_id),
            )
            return self._decode(self._find(connection, project_id, flowchart_id))
