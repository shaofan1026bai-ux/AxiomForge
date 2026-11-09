"""文件持久化、真实SQLite事务与并发回归，不访问任何现场接口。"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from axiomforge.algorithm_flowcharts import (
    FlowchartConflictError,
    FlowchartDraft,
    FlowchartError,
    seed_draft,
)
from axiomforge.flowchart_store import SEED_ID, FlowchartStore

HANGXIN = "hangxin-airport"
ECNU = "ecnu-library"
RTMART = "rt-mart-donghuan"


@pytest.fixture
def store(tmp_path):
    return FlowchartStore(tmp_path / "diagrams.sqlite3")


def draft(name="新流程图"):
    return FlowchartDraft(
        name, "测试说明", "flowchart LR\nA[开始] --> B[结束]", {"A": {"description": "起点"}}
    )


def test_seed_is_once_only_and_project_scoped(store):
    rows = store.list_latest(HANGXIN)
    assert len(rows) == 1 and rows[0].flowchart_id == SEED_ID
    assert rows[0].source == seed_draft().source
    assert store.list_latest(ECNU) == []
    assert store.list_latest(RTMART) == []
    again = FlowchartStore(store.path)
    assert len(again.list_revisions(HANGXIN, SEED_ID)) == 1


def test_versions_survive_restart_and_seed_never_overwrites_user_content(store):
    initial = store.get(HANGXIN, SEED_ID)
    edited = replace(seed_draft(), name="用户编辑版")
    saved = store.save(HANGXIN, edited, flowchart_id=SEED_ID, expected_revision=1)
    assert saved.revision == 2 and saved.created_at.tzinfo is not None
    reopened = FlowchartStore(store.path)
    assert reopened.get(HANGXIN, SEED_ID).name == "用户编辑版"
    assert reopened.get(HANGXIN, SEED_ID, 1) == initial
    assert [row.revision for row in reopened.list_revisions(HANGXIN, SEED_ID)] == [2, 1]


def test_new_chart_and_annotation_copy_are_independent(store):
    original = draft()
    saved = store.save(ECNU, original)
    original.annotations["A"]["description"] = "本地后来修改"
    saved.annotations["A"]["description"] = "调用方修改返回对象"
    reopened = store.get(ECNU, saved.flowchart_id)
    assert reopened.annotations["A"]["description"] == "起点"
    assert [row.flowchart_id for row in store.list_latest(ECNU)] == [saved.flowchart_id]


def test_unchanged_content_does_not_create_empty_revision(store):
    first = store.save(ECNU, draft())
    repeated = store.save(ECNU, draft(), flowchart_id=first.flowchart_id, expected_revision=1)
    assert repeated == first
    assert len(store.list_revisions(ECNU, first.flowchart_id)) == 1


def test_stale_baseline_conflicts_even_if_draft_matches_new_latest(store):
    first = store.save(ECNU, draft())
    second = store.save(ECNU, draft("新名称"), flowchart_id=first.flowchart_id, expected_revision=1)
    with pytest.raises(FlowchartConflictError):
        store.save(ECNU, draft("新名称"), flowchart_id=first.flowchart_id, expected_revision=1)
    assert store.get(ECNU, first.flowchart_id) == second
    with pytest.raises(FlowchartConflictError):
        store.save(ECNU, draft(), flowchart_id=first.flowchart_id)


def test_history_derivation_appends_after_latest_without_rewriting_history(store):
    first = store.save(ECNU, draft())
    store.save(ECNU, draft("新版"), flowchart_id=first.flowchart_id, expected_revision=1)
    restored = store.save(
        ECNU, draft(), flowchart_id=first.flowchart_id, expected_revision=2, derived_from_revision=1
    )
    assert restored.revision == 3 and restored.derived_from_revision == 1
    assert store.get(ECNU, first.flowchart_id, 1) == first
    with pytest.raises(FlowchartError):
        store.save(
            ECNU,
            draft("不存在的派生"),
            flowchart_id=first.flowchart_id,
            expected_revision=3,
            derived_from_revision=99,
        )
    assert store.get(ECNU, first.flowchart_id).revision == 3


def test_project_isolation_applies_to_every_read_and_write(store):
    for operation in [
        lambda: store.get(ECNU, SEED_ID),
        lambda: store.get(ECNU, SEED_ID, 1),
        lambda: store.list_revisions(ECNU, SEED_ID),
        lambda: store.save(ECNU, draft(), flowchart_id=SEED_ID, expected_revision=1),
    ]:
        with pytest.raises(FlowchartError, match="未找到"):
            operation()
    with pytest.raises(FlowchartError, match="项目"):
        store.list_latest("unknown")
    with pytest.raises(FlowchartError, match="ID"):
        store.get(HANGXIN, "' OR 1=1 --")
    with pytest.raises(FlowchartError, match="版本号"):
        store.get(HANGXIN, SEED_ID, True)


def test_actual_concurrent_transactions_have_exactly_one_winner(store):
    original = store.save(ECNU, draft())
    barrier = threading.Barrier(2)

    def save(index):
        other = FlowchartStore(store.path)
        barrier.wait(timeout=5)
        try:
            return other.save(
                ECNU, draft(f"并发{index}"), flowchart_id=original.flowchart_id, expected_revision=1
            )
        except FlowchartConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert [row.revision for row in store.list_revisions(ECNU, original.flowchart_id)] == [2, 1]


def test_concurrent_first_open_inserts_seed_once(tmp_path):
    path = tmp_path / "shared.sqlite3"
    barrier = threading.Barrier(2)

    def initialize(_):
        barrier.wait(timeout=5)
        return FlowchartStore(path).get(HANGXIN, SEED_ID).revision

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(initialize, [1, 2])) == [1, 1]
    assert len(FlowchartStore(path).list_revisions(HANGXIN, SEED_ID)) == 1


def test_sqlite_triggers_prevent_revision_update_and_delete(store):
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE flowchart_revisions SET name = '改写' WHERE flowchart_id = ?", (SEED_ID,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM flowchart_revisions WHERE flowchart_id = ?", (SEED_ID,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE flowcharts SET project_id = ? WHERE id = ?", (ECNU, SEED_ID))


def test_failed_write_rolls_back_latest_and_keeps_original(store):
    original = store.save(ECNU, draft())
    with sqlite3.connect(store.path) as connection:
        connection.execute("""CREATE TRIGGER force_write_failure
                            BEFORE UPDATE OF latest_revision ON flowcharts
                            BEGIN SELECT RAISE(ABORT, 'private SQL details'); END""")
    with pytest.raises(FlowchartError) as error:
        store.save(ECNU, draft("改名"), flowchart_id=original.flowchart_id, expected_revision=1)
    assert "private" not in str(error.value)
    assert str(store.path) not in str(error.value)
    assert store.get(ECNU, original.flowchart_id) == original
    assert len(store.list_revisions(ECNU, original.flowchart_id)) == 1


def test_bad_database_or_path_yields_safe_error(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_text("private invalid database")
    with pytest.raises(FlowchartError) as error:
        FlowchartStore(path)
    assert "private" not in str(error.value) and str(path) not in str(error.value)
    parent = tmp_path / "parent-file"
    parent.write_text("not a directory")
    with pytest.raises(FlowchartError):
        FlowchartStore(parent / "child.sqlite3")


def test_schema_version_failure_does_not_reset_database(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE flowchart_metadata SET value='99' WHERE key='schema_version'")
    with pytest.raises(FlowchartError, match="格式"):
        FlowchartStore(store.path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM flowchart_revisions").fetchone()[0] == 1


def test_missing_latest_is_not_silently_omitted_from_directory(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE flowcharts SET latest_revision=99 WHERE id=?", (SEED_ID,))
    with pytest.raises(FlowchartError):
        store.list_latest(HANGXIN)


def test_env_override_is_explicit_and_separate_from_source_config(tmp_path, monkeypatch):
    path = tmp_path / "local" / "flowcharts.sqlite3"
    monkeypatch.setenv("AXIOMFORGE_FLOWCHART_DB", str(path))
    store = FlowchartStore()
    assert store.path == path
    assert store.get(HANGXIN, SEED_ID).revision == 1
