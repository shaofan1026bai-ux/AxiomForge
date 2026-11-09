"""图示草稿、有效预览、已保存版本与异步事件之间的边界。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from axiomforge.algorithm_flowcharts import FlowchartDraft, source_hash
from axiomforge.flowchart_store import FlowchartStore
from axiomforge.ui.algorithm_flowcharts import AlgorithmFlowchartsView
from axiomforge.ui.workbench import Workbench

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100"><text>判断</text></svg>'


def view_with_seed(tmp_path):
    store = FlowchartStore(tmp_path / "flows.sqlite3")
    view = AlgorithmFlowchartsView(store)
    view.project_id = "hangxin-airport"
    view._read_revision(store.list_latest(view.project_id)[0])
    view.mounted = True
    view.valid_source = view.draft.source
    view.valid_hash = source_hash(view.valid_source)
    view.valid_ticket = view.ticket
    view.valid_svg = SVG
    view.render_status = lambda: None
    view.render_catalog = lambda: None
    view.render_details = lambda: None
    view.version_select = SimpleNamespace(set_options=lambda *_args, **_kwargs: None)
    return view


def test_metadata_draft_differs_from_source_preview_and_saved_version(tmp_path):
    view = view_with_seed(tmp_path)
    assert not view.dirty and view.can_export and not view.can_save
    original = view.saved
    view.edit("name", "调整名称")
    assert view.dirty and not view.preview_stale and view.can_save
    assert not view.can_export
    assert view.saved is original
    view.discard_draft()
    assert not view.dirty and view.can_export


def test_source_edit_invalidates_pending_result_but_retains_valid_svg(tmp_path):
    view = view_with_seed(tmp_path)
    old_svg, old_hash = view.valid_svg, view.valid_hash
    invalidated = []
    view.canvas = SimpleNamespace(invalidate=invalidated.append)
    view.rendering = True
    ticket = view.ticket
    view.edit("source", "flowchart TD\n Broken[")
    view.on_rendered(
        SimpleNamespace(args={"ticket": ticket, "hash": old_hash, "svg": SVG, "nodes": []})
    )
    assert view.preview_stale and not view.can_save and not view.can_export
    assert not view.rendering
    assert invalidated == [ticket + 1]
    assert view.valid_svg == old_svg and view.valid_hash == old_hash


def test_discard_removes_preview_of_abandoned_source_and_history_derivation(tmp_path):
    view = view_with_seed(tmp_path)
    saved = view.saved
    view.repository.save(
        view.project_id,
        replace(view.draft, name="新版"),
        flowchart_id=saved.flowchart_id,
        expected_revision=1,
    )
    view._read_revision(saved)
    view.derived_from_revision = 1
    view.history_read_only = False
    abandoned = "flowchart TD\nX-->Y"
    view.draft = replace(view.draft, source=abandoned)
    view.valid_source, view.valid_hash, view.valid_svg = abandoned, source_hash(abandoned), SVG
    ticket = view.ticket
    view.discard_draft()
    assert not view.dirty and view.history_read_only
    assert view.derived_from_revision is None
    assert view.draft.source == saved.source
    assert not view.valid_source and not view.valid_svg
    assert view.ticket > ticket


def test_invalid_source_does_not_start_browser_render(tmp_path):
    view = view_with_seed(tmp_path)
    invalidated = []
    view.canvas = SimpleNamespace(invalidate=invalidated.append)
    view.request_preview("sequenceDiagram\n A->>B: hello", preserve=False)
    assert view.error and not view.rendering
    assert view.valid_svg == SVG
    assert invalidated == [view.ticket]


def test_render_failure_and_disconnected_callbacks_keep_last_good_result(tmp_path):
    view = view_with_seed(tmp_path)
    view.on_render_error(SimpleNamespace(args={"ticket": view.ticket - 1, "message": "旧错误"}))
    assert not view.error
    view.on_render_error(SimpleNamespace(args={"ticket": view.ticket, "message": "第2行语法错误"}))
    assert view.error == "第2行语法错误"
    assert view.valid_svg == SVG
    view.deactivate()
    view.on_render_error(SimpleNamespace(args={"ticket": view.ticket, "message": "断线后的错误"}))
    assert view.error == "第2行语法错误"


def test_server_svg_check_precedes_preview_publish(tmp_path):
    view = view_with_seed(tmp_path)
    published = []
    view.canvas = SimpleNamespace(publish=lambda *args: published.append(args))
    view._pending_source = view.draft.source
    view._pending_hash = source_hash(view._pending_source)
    nodes = [{"id": "T", "label": "夜间？", "domId": "flowchart-T-0", "type": "判断"}]
    args = {"ticket": view.ticket, "hash": view._pending_hash, "svg": SVG, "nodes": nodes}
    view.on_rendered(
        SimpleNamespace(args={**args, "svg": SVG.replace("</svg>", "<script/></svg>")})
    )
    assert not published and view.error
    assert view.valid_svg == SVG
    view.on_rendered(SimpleNamespace(args=args))
    assert len(published) == 1 and view.valid_nodes == nodes


def test_node_selection_is_bound_to_current_valid_preview(tmp_path):
    view = view_with_seed(tmp_path)
    view.valid_nodes = [{"id": "T", "label": "夜间？"}]
    view.on_node_selected(SimpleNamespace(args={"id": "T", "hash": "another-document"}))
    assert not view.selected_node
    view.on_node_selected(SimpleNamespace(args={"id": "N", "hash": view.valid_hash}))
    assert not view.selected_node
    view.on_node_selected(
        SimpleNamespace(args={"id": "T", "hash": view.valid_hash, "ticket": view.valid_ticket - 1})
    )
    assert not view.selected_node
    view.on_node_selected(
        SimpleNamespace(args={"id": "T", "hash": view.valid_hash, "ticket": view.valid_ticket})
    )
    assert view.selected_node == "T"


def test_history_edit_creates_draft_even_when_original_source_unchanged(tmp_path):
    view = view_with_seed(tmp_path)
    seed = view.saved
    newer = view.repository.save(
        view.project_id,
        FlowchartDraft("更新名称", seed.description, seed.source, seed.annotations),
        flowchart_id=seed.flowchart_id,
        expected_revision=1,
    )
    assert newer.revision == 2
    view._read_revision(seed)
    view.valid_hash, view.valid_source, view.valid_svg = source_hash(seed.source), seed.source, SVG
    assert view.history_read_only and not view.dirty and view.can_export
    view.edit_history_button = SimpleNamespace(set_visibility=lambda _value: None)
    view.edit_history()
    assert not view.history_read_only and view.dirty and view.can_save
    assert view.derived_from_revision == 1


def test_conflicting_save_retains_second_browser_draft(tmp_path, monkeypatch):
    first = view_with_seed(tmp_path)
    second = view_with_seed(tmp_path)
    first.edit("name", "浏览器一")
    second.edit("name", "浏览器二")

    async def io_bound(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("axiomforge.ui.algorithm_flowcharts.run.io_bound", io_bound)
    monkeypatch.setattr(
        "axiomforge.ui.algorithm_flowcharts.ui.notify", lambda *_args, **_kwargs: None
    )
    assert asyncio.run(first.save())
    assert first.saved.revision == 2 and not first.dirty
    assert not asyncio.run(second.save())
    assert second.error and second.dirty and second.draft.name == "浏览器二"
    assert second.saved.revision == 1


def test_theme_render_ticket_does_not_hide_successful_save(tmp_path, monkeypatch):
    view = view_with_seed(tmp_path)
    view.edit("name", "保存期间切主题")

    async def io_bound(function, *args, **kwargs):
        # 主题重绘只更新渲染代号，保存文档仍为同一份草稿。
        view.ticket += 1
        return function(*args, **kwargs)

    monkeypatch.setattr("axiomforge.ui.algorithm_flowcharts.run.io_bound", io_bound)
    monkeypatch.setattr(
        "axiomforge.ui.algorithm_flowcharts.ui.notify", lambda *_args, **_kwargs: None
    )
    assert asyncio.run(view.save())
    assert view.saved.revision == 2 and view.expected_revision == 2
    assert not view.dirty


def test_svg_export_contains_full_saved_revision_metadata(tmp_path, monkeypatch):
    view = view_with_seed(tmp_path)
    downloads = []
    monkeypatch.setattr(
        "axiomforge.ui.algorithm_flowcharts.ui.download", lambda *args: downloads.append(args)
    )
    monkeypatch.setattr(
        "axiomforge.ui.algorithm_flowcharts.ui.notify", lambda *_args, **_kwargs: None
    )
    view.download()
    assert len(downloads) == 1
    content, name, media_type = downloads[0]
    assert 'viewBox="0 0 200 100"' in content.decode()
    assert '"project": "hangxin-airport"' in content.decode()
    assert '"revision": 1' in content.decode()
    assert "AHU运行策略" in content.decode()
    assert name.endswith("-v1.svg") and media_type == "image/svg+xml"
    view.draft = replace(view.draft, name="未保存名称")
    view.download()
    assert len(downloads) == 1


@pytest.mark.parametrize(
    "choice,allowed", [("cancel", False), ("discard", True), (None, False), ("save", False)]
)
def test_leave_confirmation_preserves_or_discards_draft(tmp_path, monkeypatch, choice, allowed):
    view = view_with_seed(tmp_path)
    view.edit("name", "草稿名称")

    class Dialog:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, _value):
            pass

        def classes(self, *_args):
            return self

        def props(self, *_args):
            return self

        def set_enabled(self, *_args):
            return self

        def __await__(self):
            async def answer():
                return choice

            return answer().__await__()

    monkeypatch.setattr("axiomforge.ui.algorithm_flowcharts.ui.dialog", Dialog)
    for name in ("card", "label", "row", "button"):
        monkeypatch.setattr(
            "axiomforge.ui.algorithm_flowcharts.ui." + name, lambda *_args, **_kwargs: Dialog()
        )

    async def reject_save():
        return False

    view.save = reject_save
    assert asyncio.run(view.confirm_leave()) is allowed
    assert view.dirty is (not allowed)


def test_save_failure_never_allows_switch(tmp_path, monkeypatch):
    view = view_with_seed(tmp_path)

    async def reject():
        return False

    view.save = reject
    view.loading = True
    monkeypatch.setattr(
        "axiomforge.ui.algorithm_flowcharts.ui.notify", lambda *_args, **_kwargs: None
    )
    assert not asyncio.run(view.confirm_leave())


def test_workbench_navigation_cancel_does_not_change_route():
    workbench = Workbench.__new__(Workbench)
    workbench.section = "algorithms"

    async def reject():
        return False

    workbench.flowchart_view = SimpleNamespace(confirm_leave=reject)
    asyncio.run(workbench.navigate("overview"))
    assert workbench.section == "algorithms"


def test_two_browser_views_do_not_share_draft_or_preview(tmp_path):
    first, second = view_with_seed(tmp_path), view_with_seed(tmp_path)
    first.edit("name", "私有草稿")
    first.selected_node = "T"
    assert second.draft.name != first.draft.name and not second.dirty
    assert not second.selected_node
