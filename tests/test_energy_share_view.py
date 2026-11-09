"""验证固定分母展示、完整快照导出及异步客户端生命周期。"""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axiomforge.energy_share import (
    POINTS,
    SHANGHAI,
    Conditions,
    default_conditions,
    encode_conditions,
    normalize,
    resolve_query,
)
from axiomforge.ui.energy_share import TABS, EnergyShareView, group_status
from axiomforge.ui.energy_share_charts import daily_options

NOW = datetime(2026, 11, 9, 12, tzinfo=SHANGHAI)
CONDITIONS = Conditions("custom", "2026-10-24", "2026-10-25")


def snapshot(missing=False, zero=False):
    query = resolve_query(CONDITIONS, NOW)
    data = {}
    for index, meter in enumerate(POINTS):
        data[meter.point_id] = [
            {
                "ts": f"2026-10-{day}T00:00:00+08:00",
                "val": 1000 + (0 if zero else offset * (index + 1)),
            }
            for offset, day in enumerate((24, 25, 26))
            if not (missing and index == 12 and day == 25)
        ]
    return normalize(query, data, NOW)


def prepared():
    view = EnergyShareView()
    view.conditions = CONDITIONS
    view.mounted = True
    view.render_results = Mock()
    view.render_status = Mock()
    view.save_preferences = Mock()
    return view


def test_preferences_restore_without_query_and_relative_presets_roll(monkeypatch):
    async def exercise():
        view = EnergyShareView()

        async def read(_script):
            return encode_conditions(CONDITIONS)

        source = Mock(side_effect=AssertionError("不得自动取数"))
        monkeypatch.setattr("axiomforge.ui.energy_share.ui.run_javascript", read)
        monkeypatch.setattr("axiomforge.ui.energy_share.load_energy_share", source)
        await view.initialize_preferences()
        assert view.conditions == CONDITIONS and view.snapshot is None
        source.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("payload", [None, [], {}, {"version": True}, "bad-json"])
def test_bad_preferences_fall_back(monkeypatch, payload):
    async def exercise():
        view = EnergyShareView()

        async def read(_script):
            return payload

        monkeypatch.setattr("axiomforge.ui.energy_share.ui.run_javascript", read)
        await view.initialize_preferences()
        assert view.conditions == default_conditions()
        assert view.snapshot is None

    asyncio.run(exercise())


def test_late_preference_read_does_not_replace_edited_draft(monkeypatch):
    async def exercise():
        view = prepared()
        started, release = asyncio.Event(), asyncio.Event()

        async def read(_script):
            started.set()
            await release.wait()
            return encode_conditions(replace(CONDITIONS, start_date="2026-10-01"))

        monkeypatch.setattr("axiomforge.ui.energy_share.ui.run_javascript", read)
        task = asyncio.create_task(view.initialize_preferences())
        await started.wait()
        view.conditions = replace(CONDITIONS, start_date="2026-10-20")
        view.edited()
        release.set()
        await task
        assert view.conditions.start_date == "2026-10-20"

    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["stop", "leave"])
def test_cancelled_or_unmounted_query_keeps_old_snapshot_and_blocks_late_results(
    monkeypatch, action
):
    async def exercise():
        view = prepared()
        old = snapshot()
        view.snapshot, view.submitted_conditions = old, CONDITIONS
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*_args, **_kwargs):
            started.set()
            await release.wait()
            return snapshot(missing=True)

        monkeypatch.setattr("axiomforge.ui.energy_share.run.io_bound", delayed)
        task = asyncio.create_task(view.refresh())
        await started.wait()
        if action == "stop":
            view.stop_waiting()
        else:
            view.deactivate()
            view.container = SimpleNamespace(is_deleted=False)
            view.reconnect()
        release.set()
        await task
        assert view.snapshot is old and not view.can_export and not view.loading
        # 清除表单提示也不能让中断查询的旧快照重新变为可导出。
        view.edited()
        assert not view.can_export

    asyncio.run(exercise())


def test_success_empty_snapshot_replaces_old_and_selects_last_requested_day(monkeypatch):
    async def exercise():
        view = prepared()
        view.snapshot = snapshot()
        result = normalize(resolve_query(CONDITIONS, NOW), {}, NOW)

        async def load(*_args, **_kwargs):
            return result

        monkeypatch.setattr("axiomforge.ui.energy_share.run.io_bound", load)
        await view.refresh()
        assert view.snapshot is result and view.can_export
        assert view.detail_day == "2026-10-25"
        assert all(day.total is None for day in result.daily)

    asyncio.run(exercise())


def test_chart_missing_denominator_zero_and_highlight_preserve_values():
    result = snapshot(missing=True)
    percentage = daily_options(result, False)
    assert percentage["yAxis"]["max"] == 100
    assert percentage["xAxis"]["data"] == ["2026-10-24", "2026-10-25"]
    assert all(
        item["value"] is None for series in percentage["series"][:8] for item in series["data"]
    )
    energy = daily_options(result, False, "kwh")
    assert energy["series"][0]["data"][0]["value"] == 1
    assert energy["series"][7]["data"][0]["value"] is None
    focused = daily_options(result, True, "kwh", "midea1")
    assert [s["data"] for s in energy["series"][:8]] == [s["data"] for s in focused["series"][:8]]
    assert "legend" not in focused  # 产品图例只高亮，不能删系列改分母。
    zero = daily_options(snapshot(zero=True), False)
    assert all(s["data"][0]["value"] is None for s in zero["series"][:8])
    assert zero["series"][-1]["data"][0]["label"]["formatter"] == "总量为0"


def test_export_keeps_full_snapshot_after_date_focus_and_blocks_stale(monkeypatch):
    view = prepared()
    view.snapshot, view.submitted_conditions = snapshot(), CONDITIONS
    downloads, notices = [], []
    monkeypatch.setattr(
        "axiomforge.ui.energy_share.ui.download", lambda *args, **kw: downloads.append(args)
    )
    monkeypatch.setattr(
        "axiomforge.ui.energy_share.ui.notify", lambda *args, **kw: notices.append(args)
    )
    view.detail_day = "2026-10-24"
    view.highlight = "midea1"
    view.download("daily")
    csv = downloads[0][0].decode("utf-8-sig")
    assert "2026-10-24" in csv and "2026-10-25" in csv
    view.conditions = replace(CONDITIONS, start_date="2026-10-23")
    view.download("daily")
    assert len(downloads) == 1 and notices


def test_display_controls_and_theme_do_not_query_or_change_snapshot(monkeypatch):
    view = prepared()
    old = snapshot()
    view.snapshot = old
    view.tab_buttons = {tab: SimpleNamespace(classes=Mock()) for tab in TABS}
    source = Mock(side_effect=AssertionError("展示操作不取数"))
    monkeypatch.setattr("axiomforge.ui.energy_share.load_energy_share", source)
    view.set_theme(True)
    view.change_mode(SimpleNamespace(value="kwh"))
    view.change_day(SimpleNamespace(value="2026-10-24"))
    view.change_tab(TABS[1])
    view.focus_group("ahu")
    assert view.snapshot is old and view.dark and view.mode == "kwh"
    source.assert_not_called()


def test_date_and_highlight_changes_preserve_dragged_chart_viewport():
    view = prepared()
    view.snapshot = snapshot()
    view.zoomed(SimpleNamespace(args={"batch": [{"start": 0.0, "end": 45.0}]}))
    view.change_day(SimpleNamespace(value="2026-10-24"))
    view.focus_group("ahu")
    view.change_mode(SimpleNamespace(value="kwh"))
    options = view.daily_chart_options()
    assert all(z["start"] == 0 and z["end"] == 45 for z in options["dataZoom"])
    assert all("startValue" not in z for z in options["dataZoom"])
    view.change_viewport(SimpleNamespace(value=7))
    assert not view.zoom
    assert all("startValue" in z for z in view.daily_chart_options()["dataZoom"])


def test_missing_boundary_displays_available_support_evidence():
    from datetime import timedelta
    from decimal import Decimal

    from axiomforge.energy_share import Boundary, Reading

    view = prepared()
    target = resolve_query(CONDITIONS, NOW).start
    before = Reading(target - timedelta(hours=2), Decimal("100"))
    after = Reading(target + timedelta(hours=2), Decimal("105"))
    boundary = Boundary(POINTS[0], target, "missing", None, before, after, "超过5分钟")
    text = view.boundary_text(boundary)
    assert "2026-10-23 22:00" in text and "2026-10-24 02:00" in text
    assert "14400秒" in text and "缺失" in text
    single = replace(boundary, after=None)
    assert "仅前侧支撑" in view.boundary_text(single)


def test_eight_row_status_is_concise_but_keeps_full_evidence_in_snapshot():
    result = snapshot(missing=True)
    ahu = result.daily[0].items[-1]
    original_reason = ahu.reason
    assert group_status(ahu) == "日界缺失，增量不可确定"
    assert "期初" in original_reason or "期末" in original_reason
    assert ahu.reason == original_reason and ahu.meters[-1].reason
    assert group_status(result.daily[0].items[0]) == "精确日界"
