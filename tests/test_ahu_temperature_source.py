"""假上游覆盖多点身份、全成功发布、停止等待与分片故障。"""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from axiomforge.ahu_temperature import POINTS, PROJECT_ID, SHANGHAI, QueryConditions, resolve_query
from axiomforge.ahu_temperature_source import AhuSourceError, QueryCancelled, load_ahu_temperature
from axiomforge.xiaomai_client import HISTORY_PATH, LOGIN_PATH, POINT_PATH, PROJECT_PATH, Settings


@pytest.fixture
def config():
    return Settings("test-account", "test-password")


def query(days=1):
    return resolve_query(
        QueryConditions("custom", "2026-10-01", f"2026-10-0{days}", True, "14:00", "14:03")
    )


def metadata(point):
    return {
        "pointId": point.point_id,
        "deviceId": point.device_id,
        "projectId": PROJECT_ID,
        "unit": point.unit,
    }


def handler(calls, history=None, point_override=None):
    def respond(request):
        body = json.loads(request.content)
        path = request.url.path
        calls.append((path, body))
        headers = {}
        if path == LOGIN_PATH:
            assert body["pwd"] != "test-password"
            headers = {"Access-Token": "Bearer test-token"}
            data = {"userId": 10, "companyId": 49}
        else:
            assert request.headers["Authorization"] == "Bearer test-token"
            if path == PROJECT_PATH:
                data = [{"projectId": PROJECT_ID}]
            elif path == POINT_PATH:
                data = (
                    point_override
                    if point_override is not None
                    else [metadata(point) for point in POINTS]
                )
            elif path == HISTORY_PATH:
                if history:
                    return history(request)
                stamp = body["timeQuantum"]["startDate"]
                data = [
                    {
                        **metadata(point),
                        "pointDataList": [
                            {
                                "ts": stamp,
                                "val": 25
                                if point.field == "temperature"
                                else 1
                                if point.field == "feedback"
                                else 30,
                            }
                        ],
                    }
                    for point in reversed(POINTS)
                ]
            else:
                raise AssertionError(path)
        return httpx.Response(
            200, json={"success": True, "code": 200, "data": data}, headers=headers
        )

    return respond


def test_all_15_points_one_minute_windows_and_true_progress(config):
    calls, progress = [], []
    result = load_ahu_temperature(
        query(2),
        settings=config,
        transport=httpx.MockTransport(handler(calls)),
        progress=progress.append,
    )
    history = [body for path, body in calls if path == HISTORY_PATH]
    assert len(history) == 2
    assert all(body["interval"] == 1 and len(body["pointIdList"]) == 15 for body in history)
    assert [value.completed for value in progress] == [0, 0, 1, 1, 2]
    assert all(value.total == 2 for value in progress)
    assert len(result.devices) == 5
    assert all(device.running_stats.count == 2 for device in result.devices)
    assert all(device.quality.expected == 6 for device in result.devices)
    assert "test-token" not in repr(result)


def test_missing_series_is_valid_missing_not_failure(config):
    def empty(_):
        return httpx.Response(200, json={"success": True, "code": 200, "data": []})

    result = load_ahu_temperature(
        query(), settings=config, transport=httpx.MockTransport(handler([], history=empty))
    )
    assert all(device.all_stats.count == 0 and len(device.rows) == 3 for device in result.devices)


@pytest.mark.parametrize(
    "field,value", [("projectId", 999), ("deviceId", 1), ("unit", "kW"), ("pointId", "foreign")]
)
def test_foreign_identity_rejected_at_history_boundary(config, field, value):
    def wrong(_):
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 200,
                "data": [{**metadata(POINTS[0]), field: value, "pointDataList": []}],
            },
        )

    with pytest.raises(AhuSourceError, match="不一致"):
        load_ahu_temperature(
            query(), settings=config, transport=httpx.MockTransport(handler([], history=wrong))
        )


def test_dictionary_mismatch_prevents_any_history_query(config):
    calls = []
    with pytest.raises(AhuSourceError, match="不一致"):
        load_ahu_temperature(
            query(),
            settings=config,
            transport=httpx.MockTransport(handler(calls, point_override=[metadata(POINTS[0])])),
        )
    assert not any(path == HISTORY_PATH for path, _ in calls)


def test_partial_failure_never_returns_snapshot_or_sensitive_error(config):
    count = 0

    def fail_second(request):
        nonlocal count
        count += 1
        if count == 2:
            raise httpx.ReadTimeout("test-token test-password private-url", request=request)
        return httpx.Response(200, json={"success": True, "code": 200, "data": []})

    with pytest.raises(AhuSourceError, match="超时") as error:
        load_ahu_temperature(
            query(2),
            settings=config,
            transport=httpx.MockTransport(handler([], history=fail_second)),
        )
    assert count == 2
    assert "test-token" not in str(error.value)
    assert "test-password" not in str(error.value)


def test_cancel_before_auth_and_between_windows(config):
    calls = []
    with pytest.raises(QueryCancelled):
        load_ahu_temperature(
            query(),
            settings=config,
            transport=httpx.MockTransport(handler(calls)),
            cancelled=lambda: True,
        )
    assert calls == []
    stopped = False

    def progress(value):
        nonlocal stopped
        if value.completed == 1:
            stopped = True

    with pytest.raises(QueryCancelled):
        load_ahu_temperature(
            query(2),
            settings=config,
            transport=httpx.MockTransport(handler(calls)),
            progress=progress,
            cancelled=lambda: stopped,
        )
    assert sum(path == HISTORY_PATH for path, _ in calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        lambda _: httpx.Response(302, headers={"location": "https://foreign.invalid"}),
        lambda _: httpx.Response(200, text="private-response"),
        lambda _: httpx.Response(200, json={"success": False, "code": 500, "data": []}),
        lambda _: httpx.Response(
            200,
            json={
                "success": True,
                "code": 200,
                "data": [{**metadata(POINTS[0]), "pointDataList": [{"ts": "bad", "val": 20}]}],
            },
        ),
    ],
)
def test_protocol_errors_do_not_become_empty_success(config, response):
    with pytest.raises(AhuSourceError) as error:
        load_ahu_temperature(
            query(), settings=config, transport=httpx.MockTransport(handler([], history=response))
        )
    assert "private" not in str(error.value)
    assert "foreign" not in str(error.value)


def test_deadline_during_response_and_unfinished_window_are_rejected(config):
    ticks = iter([0, 0, 1, 2, 3, 901])
    with pytest.raises(AhuSourceError, match="900秒"):
        load_ahu_temperature(
            query(),
            settings=config,
            transport=httpx.MockTransport(handler([])),
            clock=lambda: next(ticks, 999),
        )
    future = resolve_query(
        QueryConditions("custom", "2026-11-01", "2026-11-01"), datetime(2027, 1, 1, tzinfo=SHANGHAI)
    )
    # 构造未来查询需在真实时钟之后，源边界再次拒绝，而不是先登录。
    from axiomforge.ahu_temperature import Query, Window

    begin = datetime.now(SHANGHAI) + timedelta(days=1)
    future = Query(future.conditions, (Window(begin.date(), begin, begin + timedelta(hours=1)),))
    calls = []
    with pytest.raises(AhuSourceError, match="尚未结束"):
        load_ahu_temperature(future, settings=config, transport=httpx.MockTransport(handler(calls)))
    assert calls == []


def test_manually_constructed_window_cannot_bypass_query_conditions(config):
    from axiomforge.ahu_temperature import Query, Window

    original = query()
    window = original.windows[0]
    altered = Query(
        original.conditions, (Window(window.day, window.start, window.end + timedelta(hours=1)),)
    )
    calls = []
    with pytest.raises(AhuSourceError, match="不一致"):
        load_ahu_temperature(
            altered, settings=config, transport=httpx.MockTransport(handler(calls))
        )
    assert calls == []
