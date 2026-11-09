"""验证异步请求的生命周期，不依赖真实浏览器或上游账号。"""

import asyncio
from types import SimpleNamespace

from test_power_temperature import START, query, samples

from axiomforge.power_temperature import POWER, TEMPERATURE, normalize
from axiomforge.ui.power_temperature import PowerTemperatureView
from axiomforge.xiaomai_source import XiaomaiSourceError


def snapshot():
    return normalize(
        query(),
        {POWER.point_id: samples([1, 2, 3, 4]), TEMPERATURE.point_id: samples([20, 21, 22, 23])},
        START,
    )


def test_leaving_page_discards_late_result(monkeypatch):
    async def exercise():
        view = PowerTemperatureView()
        view.period = "custom"
        view.start, view.end = "2026-01-01T00:00", "2026-01-01T00:08"
        view.mounted = True
        view.render_status = lambda: None
        view.render_results = lambda: None
        started, release = asyncio.Event(), asyncio.Event()

        async def io_bound(*_args):
            started.set()
            await release.wait()
            return snapshot()

        monkeypatch.setattr("axiomforge.ui.power_temperature.run.io_bound", io_bound)
        task = asyncio.create_task(view.refresh())
        await started.wait()
        assert view.loading
        view.deactivate()
        release.set()
        await task
        assert view.snapshot is None
        assert not view.loading

    asyncio.run(exercise())


def test_failed_query_keeps_original_snapshot_and_stale_scope(monkeypatch):
    async def exercise():
        view = PowerTemperatureView()
        old = snapshot()
        view.snapshot = old
        view.period, view.start, view.end = "custom", "2026-01-01T00:00", "2026-01-01T00:08"
        view.submitted_draft = (view.period, view.start, view.end)
        view.mounted = True
        view.render_status = lambda: None

        async def fail(*_args):
            raise XiaomaiSourceError("查询失败")

        monkeypatch.setattr("axiomforge.ui.power_temperature.run.io_bound", fail)
        await view.refresh()
        assert view.snapshot is old
        assert view.error == "查询失败"
        view.end = "2026-01-01T00:06"
        assert view.stale
        assert old.query.end.minute == 8

    asyncio.run(exercise())


def test_brush_selects_time_window_not_zoom_and_reset(monkeypatch):
    view = PowerTemperatureView()
    view.snapshot = snapshot()
    view.update_charts = lambda: None
    view.update_analysis = lambda: None
    start = START.timestamp() * 1000
    event = SimpleNamespace(args={"areas": [{"coordRange": [start + 120000, start + 240000]}]})
    view.brushed(event)
    assert view.selection is None
    view.mode = "select"
    view.brushed(event)
    assert len(view.snapshot.selected(view.selection)) == 2
    assert len(view.snapshot.rows) == 4
    view.reset_selection()
    assert view.selection is None


def test_two_views_do_not_share_snapshot_or_selection():
    first, second = PowerTemperatureView(), PowerTemperatureView()
    first.snapshot = snapshot()
    first.selection = query(4)
    assert second.snapshot is None
    assert second.selection is None


def test_reconnect_restores_only_live_container():
    view = PowerTemperatureView()
    view.container = SimpleNamespace(is_deleted=False)
    rendered = []
    view.render_status = lambda: rendered.append(True)
    view.loading = True
    view.deactivate()
    generation = view.generation
    view.reconnect()
    assert view.mounted and not view.loading
    assert view.generation == generation
    assert rendered == [True]
    view.container.is_deleted = True
    view.deactivate()
    view.reconnect()
    assert not view.mounted
