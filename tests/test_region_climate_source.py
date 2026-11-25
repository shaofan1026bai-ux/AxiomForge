"""区域历史用假传输验证测点分片、日界与实测状态对齐。"""

import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from axiomforge.ahu_temperature import DEVICES, PROJECT_ID, SHANGHAI
from axiomforge.region_climate_source import REGIONS, load_region_climate
from axiomforge.xiaomai_client import (
    HISTORY_PATH,
    LOGIN_PATH,
    POINT_PATH,
    PROJECT_PATH,
    Settings,
    XiaomaiSourceError,
)


def test_one_day_queries_registered_probes_in_batches_and_preserves_state_gaps():
    day = date(2026, 11, 10)
    start = datetime(2026, 11, 10, tzinfo=SHANGHAI)
    registered = {
        point.point_id: {
            "pointId": point.point_id,
            "projectId": PROJECT_ID,
            "deviceId": point.device_id,
            "unit": point.unit,
        }
        for point in [
            *(
                point
                for region in REGIONS
                for probe in region.probes
                for point in (probe.temperature, probe.humidity)
                if point is not None
            ),
            *(
                point
                for device in DEVICES
                for point in device.points
                if point.field in {"feedback", "frequency"}
            ),
        ]
    }
    # 两台同名设备只登记温湿度探头；异常湿度和次日首点均不进入趋势。
    actual = {
        "bfby5y_12014b_temp": [(0, 24), (1440, 25)],
        "bfby5y_12014b_rh": [(0, 0), (2, 101)],
        "ewcnad_msahu1_v20": [(0, 0), (2, 1), (4, 1), (6, True)],
        "ewcnad_msahu1_v01": [(0, 0), (2, 30), (6, 0)],
    }
    batches: list[list[str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path
        headers = {}
        if path == LOGIN_PATH:
            headers = {"Access-Token": "Bearer test-token"}
            data = {"userId": 10, "companyId": 49}
        elif path == PROJECT_PATH:
            data = [{"projectId": PROJECT_ID}]
        elif path == POINT_PATH:
            assert set(body["deviceIdList"]) == {row["deviceId"] for row in registered.values()}
            data = list(registered.values())
        else:
            assert path == HISTORY_PATH
            assert body["interval"] == 1
            assert body["timeQuantum"] == {
                "startDate": "2026-11-10 00:00",
                "endDate": "2026-11-11 00:00",
            }
            batches.append(body["pointIdList"])
            data = [
                {
                    **registered[point_id],
                    "pointDataList": [
                        {"ts": (start + timedelta(minutes=minute)).isoformat(), "val": value}
                        for minute, value in actual[point_id]
                    ],
                }
                for point_id in body["pointIdList"]
                if point_id in actual
            ]
        return httpx.Response(
            200, json={"success": True, "code": 200, "data": data}, headers=headers
        )

    snapshot = load_region_climate(
        day,
        settings=Settings("test-account", "test-password"),
        transport=httpx.MockTransport(respond),
    )
    requested = [point for batch in batches for point in batch]
    assert len(requested) == 62 and set(requested) == registered.keys()
    assert len(batches) > 1 and max(map(len, batches)) <= 12
    assert snapshot.temperatures[12685] == {start: 24}
    assert snapshot.humidities[12685] == {start: 0}
    assert snapshot.temperatures[-1] == snapshot.temperatures[-2] == {}
    assert snapshot.ahu_states["AHU1"] == {
        start + timedelta(minutes=minute): state
        for minute, state in ((0, "stopped"), (2, "running"), (4, "unknown"), (6, "unknown"))
    }
    assert snapshot.ahu_states["AHU2"] == {}


def test_future_date_and_cancelled_query_stop_before_authentication():
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("查询不应访问上游")

    transport = httpx.MockTransport(unexpected)
    settings = Settings("test-account", "test-password")
    today = datetime.now(SHANGHAI).date()
    with pytest.raises(ValueError, match="有效日期"):
        load_region_climate(today + timedelta(days=1), settings=settings, transport=transport)
    with pytest.raises(XiaomaiSourceError, match="已停止等待"):
        load_region_climate(
            today - timedelta(days=1),
            cancelled=lambda: True,
            settings=settings,
            transport=transport,
        )
