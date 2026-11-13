"""核对单日与跨日的点位归属、采样裁剪及航班降级。"""

import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from axiomforge.ahu_co2 import events_for
from axiomforge.ahu_co2_source import (
    DEVICE_BY_ID,
    MAX_RANGE_DAYS,
    AhuCo2SourceError,
    FlightWindow,
    load_ahu_co2,
    load_ahu_co2_range,
    points_for,
    research_windows,
)
from axiomforge.ahu_temperature import PROJECT_ID, SHANGHAI
from axiomforge.flight_source import FlightSourceError
from axiomforge.flights import Flight, FlightSnapshot
from axiomforge.xiaomai_client import HISTORY_PATH, LOGIN_PATH, POINT_PATH, PROJECT_PATH, Settings

DAY = date(2026, 11, 11)
START = datetime(2026, 11, 11, tzinfo=SHANGHAI)


def _transport(calls, *, wrong_device=False):
    points = points_for(DEVICE_BY_ID["AHU3"])
    metadata = [
        {
            "pointId": point.point_id,
            "projectId": PROJECT_ID,
            "deviceId": point.device_id,
            "unit": point.unit,
        }
        for point in points
    ]
    values = {
        "temperature": (23.1, None, 23.2),
        "feedback": (0, None, 1),
        "frequency": (0, None, 35),
        "co2": (712, None, 722),
        "damper": (0, None, 35),
        "setpoint": (650, None, 650),
    }

    def handler(request):
        path = request.url.path
        payload = json.loads(request.content)
        calls.append(path)
        headers = {}
        if path == LOGIN_PATH:
            data = {"userId": 1, "companyId": 1}
            headers = {"Access-Token": "test-token"}
        elif path == PROJECT_PATH:
            data = [{"projectId": PROJECT_ID}]
        elif path == POINT_PATH:
            assert payload == {"deviceIdList": [12785], "isActivate": True}
            data = metadata
        elif path == HISTORY_PATH:
            assert payload["interval"] == 1
            assert payload["pointIdList"] == [point.point_id for point in points]
            assert payload["timeQuantum"] == {
                "startDate": "2026-11-11 00:00",
                "endDate": "2026-11-11 00:03",
            }
            data = [
                {
                    **identity,
                    "pointDataList": [
                        {"ts": (START + timedelta(minutes=i)).isoformat(), "val": value}
                        for i, value in enumerate(values[point.field])
                    ],
                }
                for point, identity in zip(points, metadata, strict=True)
            ]
            if wrong_device:
                data[3]["deviceId"] = 12784
        else:
            raise AssertionError(path)
        return httpx.Response(
            200, json={"success": True, "code": 200, "data": data}, headers=headers
        )

    return httpx.MockTransport(handler)


def test_real_series_contract_and_failed_optional_flights():
    def broken_flights(_day):
        raise ValueError("private connection details")

    result = load_ahu_co2(
        "AHU3",
        DAY,
        now=START + timedelta(minutes=3, seconds=24),
        settings=Settings("test-account", "test-password"),
        transport=_transport([]),
        flight_loader=broken_flights,
    )
    assert result.end == START + timedelta(minutes=3)
    assert len(result.samples) == 3
    assert result.samples[1].co2 is None
    assert result.samples[1].state == "unknown"
    high, start = events_for(result.samples, 700)
    assert high.kind == "high_stopped" and high.sample.co2 == 712
    assert start.kind == "start" and start.sample.co2 == 712 and start.sample.setpoint == 650
    assert result.flights is None and result.flight_error
    assert "private connection details" not in result.flight_error


def test_wrong_co2_history_identity_rejects_snapshot_before_flights():
    def no_flights(_day):
        raise AssertionError("设备历史失败时不应发起航班请求")

    with pytest.raises(AhuCo2SourceError, match="归属"):
        load_ahu_co2(
            "AHU3",
            DAY,
            now=START + timedelta(minutes=3),
            settings=Settings("test-account", "test-password"),
            transport=_transport([], wrong_device=True),
            flight_loader=no_flights,
        )


def _flight(hour, direction, gate="", region="domestic", delay=0):
    planned = START + timedelta(hours=hour)
    return Flight(
        str(hour),
        str(hour),
        DAY,
        direction,
        "",
        "",
        planned,
        None,
        planned + timedelta(minutes=delay),
        "",
        gate,
        region=region,
    )


def test_research_windows_use_device_scope_and_keep_delayed_original_start():
    flights = (
        _flight(6, "I"),
        _flight(9, "O", "04", delay=30),
        _flight(12, "O", "01"),
        _flight(16, "O", "06", "international"),
        _flight(22, "O"),
    )
    snapshot = FlightSnapshot(flights, START, len(flights), {}, ())

    def window(begin: float, end: float) -> FlightWindow:
        return FlightWindow(START + timedelta(hours=begin), START + timedelta(hours=end))

    assert research_windows(snapshot, "AHU1")[0] == window(5.75, 9.75)
    assert research_windows(snapshot, "AHU3") == (
        window(7, 9.75),
        window(13, 16.25),
        window(20, 22.25),
    )
    assert research_windows(snapshot, "AHU4") == (
        window(10, 12.25),
        window(13, 16.25),
        window(20, 22.25),
    )
    assert research_windows(snapshot, "AHU5") == ()


def _range_transport(calls, *, fail_on=None):
    devices = tuple(DEVICE_BY_ID.values())
    points = tuple(point for device in devices for point in points_for(device))
    metadata = [
        {
            "pointId": point.point_id,
            "projectId": PROJECT_ID,
            "deviceId": point.device_id,
            "unit": point.unit,
        }
        for point in points
    ]
    co2_by_device = {device.device_id: 600 + number for number, device in enumerate(devices, 1)}

    def handler(request):
        path = request.url.path
        payload = json.loads(request.content)
        calls.append((path, payload.get("timeQuantum")))
        headers = {}
        if path == LOGIN_PATH:
            data = {"userId": 1, "companyId": 1}
            headers = {"Access-Token": "test-token"}
        elif path == PROJECT_PATH:
            data = [{"projectId": PROJECT_ID}]
        elif path == POINT_PATH:
            assert payload == {
                "deviceIdList": [device.device_id for device in devices],
                "isActivate": True,
            }
            data = metadata
        elif path == HISTORY_PATH:
            assert payload["interval"] == 1
            assert payload["pointIdList"] == [point.point_id for point in points]
            start = payload["timeQuantum"]["startDate"]
            if start == fail_on:
                return httpx.Response(503)
            data = [
                {
                    **identity,
                    "pointDataList": [{"ts": start, "val": co2_by_device[point.device_id]}],
                }
                for point, identity in zip(points, metadata, strict=True)
                if point.field == "co2"
            ]
        else:
            raise AssertionError(path)
        return httpx.Response(
            200, json={"success": True, "code": 200, "data": data}, headers=headers
        )

    return httpx.MockTransport(handler)


def test_range_batches_all_devices_by_day_and_shares_flight_context():
    calls = []
    progress = []
    flight_calls = []
    previous = DAY - timedelta(days=1)
    early_departure = _flight(1, "O", "04")
    midnight_departure = _flight(0, "O", "01")

    def flights_for(start_day, end_day):
        flight_calls.append((start_day, end_day))
        return FlightSnapshot((early_departure, midnight_departure), START, 2, {}, ())

    result = load_ahu_co2_range(
        previous,
        DAY,
        now=START + timedelta(minutes=3, seconds=24),
        settings=Settings("test-account", "test-password"),
        transport=_range_transport(calls),
        flight_loader=flights_for,
        progress=lambda done, total: progress.append((done, total)),
    )
    assert (result.start_day, result.end_day) == (previous, DAY)
    assert [(item.day, item.device.id) for item in result.days] == [
        (day, device_id) for day in (previous, DAY) for device_id in DEVICE_BY_ID
    ]
    assert calls == [
        (LOGIN_PATH, None),
        (PROJECT_PATH, None),
        (POINT_PATH, None),
        (HISTORY_PATH, {"startDate": "2026-11-10 00:00", "endDate": "2026-11-11 00:00"}),
        (HISTORY_PATH, {"startDate": "2026-11-11 00:00", "endDate": "2026-11-11 00:03"}),
    ]
    assert progress == [(1, 2), (2, 2)]
    assert flight_calls == [(previous, DAY)]
    yesterday, today = result.days[:5], result.days[5:]
    assert all(len(item.samples) == 1440 for item in yesterday)
    assert all(len(item.samples) == 3 for item in today)
    assert [item.samples[0].co2 for item in today] == [601, 602, 603, 604, 605]
    assert all(item.queried_at == result.queried_at for item in result.days)
    assert yesterday[2].flights is yesterday[0].flights
    assert yesterday[2].flights is not None and not yesterday[2].flights.flights
    assert yesterday[2].flight_windows == (
        FlightWindow(START - timedelta(hours=1), START + timedelta(hours=1, minutes=15)),
    )
    assert today[4].flights is today[0].flights
    assert today[4].flights is not None and today[4].flights.flights == (midnight_departure,)
    assert today[4].flight_windows == ()


@pytest.mark.parametrize(
    ("failure", "message"),
    [("second_batch", "小麦服务请求失败"), ("cancelled", "已停止等待")],
)
def test_range_never_publishes_partial_results(failure, message):
    calls = []
    progress = []

    def no_flights(_start_day, _end_day):
        raise AssertionError("设备历史失败或取消时不应发起航班请求")

    with pytest.raises(AhuCo2SourceError, match=message):
        load_ahu_co2_range(
            DAY - timedelta(days=1),
            DAY,
            now=START + timedelta(minutes=3),
            settings=Settings("test-account", "test-password"),
            transport=_range_transport(
                calls, fail_on="2026-11-11 00:00" if failure == "second_batch" else None
            ),
            flight_loader=no_flights,
            cancelled=lambda: failure == "cancelled" and bool(progress),
            progress=lambda done, total: progress.append((done, total)),
        )
    assert progress == [(1, 2)]
    assert sum(path == HISTORY_PATH for path, _ in calls) == (2 if failure == "second_batch" else 1)


def test_range_retains_all_equipment_history_when_flights_fail():
    def failed_flights(_start_day, _end_day):
        raise FlightSourceError("航班数据暂不可用。")

    result = load_ahu_co2_range(
        DAY - timedelta(days=1),
        DAY,
        now=START + timedelta(minutes=3),
        settings=Settings("test-account", "test-password"),
        transport=_range_transport([]),
        flight_loader=failed_flights,
    )
    assert len(result.days) == 10
    assert all(item.flights is None and not item.flight_windows for item in result.days)
    assert {item.flight_error for item in result.days} == {"航班数据暂不可用。"}
    assert all(item.samples[0].co2 is not None for item in result.days)


@pytest.mark.parametrize(
    ("start_day", "end_day", "message"),
    [
        (DAY - timedelta(days=MAX_RANGE_DAYS), DAY, "最多 31 天"),
        (DAY, DAY - timedelta(days=1), "结束日期不能早于开始日期"),
        (DAY, DAY + timedelta(days=1), "今天或之前"),
    ],
)
def test_range_rejects_invalid_dates_before_reading_sources(start_day, end_day, message):
    calls = []
    with pytest.raises(AhuCo2SourceError, match=message):
        load_ahu_co2_range(
            start_day,
            end_day,
            now=START + timedelta(minutes=3),
            settings=Settings("test-account", "test-password"),
            transport=_range_transport(calls),
        )
    assert not calls
