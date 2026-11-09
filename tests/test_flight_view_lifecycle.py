"""航班查询在注销或离页后不能发布迟到的现场快照。"""

import asyncio
from unittest.mock import Mock

from axiomforge.ui.flights import FlightView


def test_inactive_view_does_not_start_query(monkeypatch):
    async def exercise():
        view = FlightView()
        source = Mock(side_effect=AssertionError("未激活页面不能查询"))
        monkeypatch.setattr("axiomforge.ui.flights.run.io_bound", source)
        await view.refresh()
        source.assert_not_called()

    asyncio.run(exercise())


def test_revocation_during_query_discards_late_snapshot(monkeypatch):
    async def exercise():
        view = FlightView()
        view.mounted = True
        view.redraw = Mock()
        started, release = asyncio.Event(), asyncio.Event()

        async def source(*_args):
            started.set()
            await release.wait()
            return object()

        monkeypatch.setattr("axiomforge.ui.flights.run.io_bound", source)
        pending = asyncio.create_task(view.refresh())
        await started.wait()
        view.deactivate()
        release.set()
        await pending
        assert view.snapshot is None
        assert not view.loading
        assert not view.mounted
        assert view.redraw.call_count == 1

    asyncio.run(exercise())
