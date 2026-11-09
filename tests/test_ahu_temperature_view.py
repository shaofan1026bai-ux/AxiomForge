"""验证查询条件恢复、快照发布与取消，不访问真实账号和现场源。"""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axiomforge.ahu_temperature import (
    DEVICES,
    SHANGHAI,
    QueryConditions,
    default_conditions,
    normalize,
    resolve_query,
)
from axiomforge.ahu_temperature_source import AhuSourceError, Progress
from axiomforge.ui.ahu_temperature import TABS, AhuTemperatureView
from axiomforge.ui.ahu_temperature_charts import (
    daily_options,
    detail_options,
    distribution_options,
    range_options,
)

NOW = datetime(2026, 1, 3, 12, tzinfo=SHANGHAI)
CONDITIONS = QueryConditions("custom", "2026-01-01", "2026-01-01", True, "09:00", "09:03")


def snapshot():
    query = resolve_query(CONDITIONS, NOW)
    series = {}
    for device in DEVICES:
        for point in device.points:
            values = {"temperature": [22, 24, 26], "feedback": [1, 1, 0], "frequency": [30, 30, 0]}[
                point.field
            ]
            series[point.point_id] = [
                {"ts": stamp.isoformat(), "val": value}
                for stamp, value in zip(query.windows[0].grid(), values, strict=True)
            ]
    return normalize(query, series, NOW)


def prepared_view():
    view = AhuTemperatureView()
    view.conditions = CONDITIONS
    view.mounted = True
    view.render_status = Mock()
    view.render_results = Mock()
    view.save_preferences = Mock()
    return view


def test_restore_conditions_does_not_query_or_restore_snapshot(monkeypatch):
    async def exercise():
        view = AhuTemperatureView()

        async def preferences(_script):
            return CONDITIONS.preference()

        source = Mock(side_effect=AssertionError("不应请求上游"))
        monkeypatch.setattr("axiomforge.ui.ahu_temperature.ui.run_javascript", preferences)
        monkeypatch.setattr("axiomforge.ui.ahu_temperature.load_ahu_temperature", source)
        await view.initialize_preferences()
        assert view.conditions == CONDITIONS
        assert view.snapshot is None
        assert not view.loading
        source.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("value", [None, {}, [], {"version": True}, "bad-json"])
def test_bad_preferences_fall_back_without_request(monkeypatch, value):
    async def exercise():
        view = AhuTemperatureView()

        async def preferences(_script):
            return value

        monkeypatch.setattr("axiomforge.ui.ahu_temperature.ui.run_javascript", preferences)
        await view.initialize_preferences()
        assert view.conditions == default_conditions()
        assert view.snapshot is None

    asyncio.run(exercise())


def test_late_preferences_cannot_overwrite_user_edit(monkeypatch):
    async def exercise():
        view = prepared_view()
        view.sync_form = Mock()
        started, release = asyncio.Event(), asyncio.Event()

        async def preferences(_script):
            started.set()
            await release.wait()
            return replace(CONDITIONS, start="2025-12-25").preference()

        monkeypatch.setattr("axiomforge.ui.ahu_temperature.ui.run_javascript", preferences)
        task = asyncio.create_task(view.initialize_preferences())
        await started.wait()
        view.change_date("start", "2025-12-30")
        release.set()
        await task
        assert view.conditions.start == "2025-12-30"
        assert view.dirty

    asyncio.run(exercise())


def test_relative_preferences_roll_and_inactive_clock_does_not_query(monkeypatch):
    async def exercise():
        view = AhuTemperatureView()
        saved = QueryConditions("last7", "2020-01-01", "2020-01-07", False, "bad", "bad")

        async def preferences(_script):
            return saved.preference()

        monkeypatch.setattr("axiomforge.ui.ahu_temperature.ui.run_javascript", preferences)
        await view.initialize_preferences()
        assert view.conditions.start == default_conditions().start
        assert view.conditions.start_time == "bad"
        assert not view.conditions.daily
        assert view.snapshot is None

    asyncio.run(exercise())


def test_success_freezes_submitted_query_and_enables_export(monkeypatch):
    async def exercise():
        view = prepared_view()
        result = snapshot()

        async def io_bound(_function, query, **kwargs):
            assert query.conditions == CONDITIONS
            kwargs["progress"](Progress(1, 1, "完成", 0.1))
            assert not kwargs["cancelled"]()
            return result

        monkeypatch.setattr("axiomforge.ui.ahu_temperature.run.io_bound", io_bound)
        await view.refresh()
        assert view.snapshot is result
        assert view.submitted_conditions == CONDITIONS
        assert view.can_export and not view.loading and not view.dirty
        view.save_preferences.assert_called_once_with(CONDITIONS)
        view.conditions = replace(CONDITIONS, end_time="09:02")
        assert view.stale and not view.can_export
        assert result.query.end.minute == 3

    asyncio.run(exercise())


def test_invalid_query_never_saves_preferences_or_calls_source(monkeypatch):
    async def exercise():
        view = prepared_view()
        view.conditions = replace(CONDITIONS, end_time="09:00")
        call = Mock(side_effect=AssertionError("非法窗口不应提交"))
        monkeypatch.setattr("axiomforge.ui.ahu_temperature.run.io_bound", call)
        await view.refresh()
        assert view.error
        view.save_preferences.assert_not_called()
        call.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("method", ["stop_waiting", "deactivate"])
def test_cancel_or_leave_page_discards_late_progress_and_result(monkeypatch, method):
    async def exercise():
        view = prepared_view()
        old = snapshot()
        view.snapshot = old
        view.submitted_conditions = CONDITIONS
        started, release = asyncio.Event(), asyncio.Event()

        async def io_bound(_function, _query, **kwargs):
            started.set()
            await release.wait()
            assert kwargs["cancelled"]()
            kwargs["progress"](Progress(1, 1, "迟到进度", 2))
            return snapshot()

        monkeypatch.setattr("axiomforge.ui.ahu_temperature.run.io_bound", io_bound)
        task = asyncio.create_task(view.refresh())
        await started.wait()
        getattr(view, method)()
        release.set()
        await task
        assert view.snapshot is old
        assert view.progress is None
        assert not view.loading
        view.render_results.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("error", [AhuSourceError("查询失败"), ValueError("secret-token")])
def test_error_preserves_old_snapshot_and_redacts_unexpected_exception(monkeypatch, error):
    async def exercise():
        view = prepared_view()
        old = snapshot()
        view.snapshot = old
        view.submitted_conditions = CONDITIONS

        async def fail(*_args, **_kwargs):
            raise error

        monkeypatch.setattr("axiomforge.ui.ahu_temperature.run.io_bound", fail)
        await view.refresh()
        assert view.snapshot is old
        assert view.error and "secret-token" not in view.error
        assert not view.can_export
        view.render_results.assert_not_called()

    asyncio.run(exercise())


def test_new_request_is_not_replaced_by_older_cancelled_result(monkeypatch):
    async def exercise():
        view = prepared_view()
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def io_bound(_function, _query, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return snapshot()

        monkeypatch.setattr("axiomforge.ui.ahu_temperature.run.io_bound", io_bound)
        old_task = asyncio.create_task(view.refresh())
        await started.wait()
        view.stop_waiting()
        await view.refresh()
        new_snapshot = view.snapshot
        release.set()
        await old_task
        assert view.snapshot is new_snapshot
        assert view.can_export
        assert view.render_results.call_count == 1

    asyncio.run(exercise())


def test_theme_tab_and_display_scope_do_not_request_or_mutate_snapshot(monkeypatch):
    view = prepared_view()
    view.snapshot = snapshot()
    old = view.snapshot
    source = Mock(side_effect=AssertionError("展示操作不取数"))
    monkeypatch.setattr("axiomforge.ui.ahu_temperature.load_ahu_temperature", source)
    view.tab_buttons = {name: SimpleNamespace(classes=Mock()) for name in TABS}
    view.set_theme(True)
    view.change_tab(TABS[1])
    view.change_running(SimpleNamespace(value=False))
    assert view.snapshot is old and view.dark
    assert not view.running
    source.assert_not_called()


def test_export_is_complete_snapshot_and_blocked_for_dirty_or_failure(monkeypatch):
    view = prepared_view()
    view.snapshot = snapshot()
    view.submitted_conditions = CONDITIONS
    view.device_id = "AHU3"
    downloads = []
    notices = []
    monkeypatch.setattr(
        "axiomforge.ui.ahu_temperature.ui.download", lambda *a, **kw: downloads.append(a)
    )
    monkeypatch.setattr(
        "axiomforge.ui.ahu_temperature.ui.notify", lambda *a, **kw: notices.append(a)
    )
    view.download("summary")
    assert len(downloads) == 1
    csv = downloads[0][0].decode("utf-8-sig")
    assert all(device.id in csv for device in DEVICES)
    view.conditions = replace(CONDITIONS, end_time="09:02")
    view.download("summary")
    assert len(downloads) == 1 and notices


def test_views_are_isolated_and_reconnect_requires_live_container():
    first, second = prepared_view(), prepared_view()
    first.snapshot = snapshot()
    first.conditions = replace(CONDITIONS, start_time="08:00")
    assert second.snapshot is None and second.conditions == CONDITIONS
    first.deactivate()
    first.container = SimpleNamespace(is_deleted=False)
    first.reconnect()
    assert first.mounted and not first.loading
    first.container.is_deleted = True
    first.deactivate()
    first.reconnect()
    assert not first.mounted


def test_charts_keep_five_devices_and_use_explicit_range_semantics():
    result = snapshot()
    options = range_options(result, False)
    assert options["yAxis"]["data"] == [device.id for device in DEVICES]
    assert options["series"][0]["type"] == "custom"
    assert options["series"][0]["data"][0]["value"][1:6] == [22.0, 24.0, 22.1, 23.9, 23.0]
    distribution = distribution_options(result, False)
    assert sum(series["data"][0]["value"] for series in distribution["series"]) == 100
    original = len(result.devices[0].rows)
    detail_options(result.devices[0], "2026-01-01", True)
    daily_options(result.devices[0], False)
    assert len(result.devices[0].rows) == original


def test_summary_sort_keeps_missing_statistics_last_in_both_directions():
    view = prepared_view()
    result = snapshot()
    first, second, *others = result.devices
    missing = replace(first, running_stats=replace(first.running_stats, mean=None, p95=None))
    lower = replace(second, running_stats=replace(second.running_stats, mean=20, p95=21))
    view.snapshot = replace(result, devices=(missing, lower, *others))
    for order in ("mean_asc", "mean_desc", "p95_asc", "p95_desc"):
        view.sort_by = order
        assert view.sorted_devices()[-1].spec.id == first.spec.id
    view.sort_by = "mean_asc"
    assert view.sorted_devices()[0].spec.id == second.spec.id
    assert result.devices[0].running_stats.mean is not None


def test_detail_keeps_isolated_samples_visible_without_bridging_missing_values():
    """隔点缺测时仍展示有效采样点，同时保持缺测断线。"""
    device = snapshot().devices[0]
    first, middle, last = device.rows[:3]
    missing = replace(middle, temperature=None, feedback=None, frequency=None)
    device = replace(device, rows=(first, missing, last))
    options = detail_options(device, first.day.isoformat(), False)
    for series in options["series"][:3]:
        assert series["showSymbol"] and series["showAllSymbol"]
        assert series["connectNulls"] is False
        assert series["data"][1][1] is None
        assert series["data"][0][1] is not None and series["data"][2][1] is not None
