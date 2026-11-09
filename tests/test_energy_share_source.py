"""假小麦服务验证13点批量、缓冲、完整发布、取消及脱敏失败。"""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest

from axiomforge.energy_share import POINTS, PROJECT_ID, SHANGHAI, Conditions, resolve_query
from axiomforge.energy_share_source import (
    EnergySourceError,
    QueryCancelled,
    load_energy_share,
    query_chunks,
)
from axiomforge.xiaomai_client import HISTORY_PATH, LOGIN_PATH, POINT_PATH, PROJECT_PATH, Settings


def query(days=1):
    return resolve_query(Conditions("custom", "2026-10-01", f"2026-10-0{days}"))


def metadata(point):
    return {
        "pointId": point.point_id,
        "deviceId": point.device_id,
        "projectId": PROJECT_ID,
        "unit": point.unit,
    }


def handler(calls, history=None, dictionary=None):
    def respond(request):
        body = json.loads(request.content)
        path = request.url.path
        calls.append((path, body))
        headers = {}
        if path == LOGIN_PATH:
            assert body["pwd"] != "private-password"
            headers = {"Access-Token": "Bearer private-token"}
            data = {"userId": 10, "companyId": 49}
        else:
            assert request.headers["Authorization"] == "Bearer private-token"
            if path == PROJECT_PATH:
                data = [{"projectId": PROJECT_ID}]
            elif path == POINT_PATH:
                data = dictionary if dictionary is not None else [metadata(p) for p in POINTS]
            elif path == HISTORY_PATH:
                if history:
                    return history(request)
                # 片边界允许重复，但同刻同值不冲突。
                q = query(2)
                data = [
                    {
                        **metadata(p),
                        "pointDataList": [
                            {"ts": (q.start + timedelta(days=i)).isoformat(), "val": 100 + i}
                            for i in range(3)
                        ],
                    }
                    for p in reversed(POINTS)
                ]
            else:
                raise AssertionError(path)
        return httpx.Response(
            200, json={"success": True, "code": 200, "data": data}, headers=headers
        )

    return respond


def run(calls, **kwargs):
    return load_energy_share(
        query(2),
        settings=Settings("private-account", "private-password"),
        transport=httpx.MockTransport(handler(calls, **kwargs)),
    )


def test_13_points_one_minute_contiguous_max_24h_with_padding_and_progress():
    calls, progress = [], []
    result = load_energy_share(
        query(2),
        settings=Settings("private-account", "private-password"),
        transport=httpx.MockTransport(handler(calls)),
        progress=progress.append,
    )
    requests = [body for path, body in calls if path == HISTORY_PATH]
    assert len(requests) == 3
    assert requests[0]["timeQuantum"]["startDate"] == "2026-09-30 23:50"
    assert requests[-1]["timeQuantum"]["endDate"] == "2026-10-03 00:10"
    assert all(r["interval"] == 1 and len(r["pointIdList"]) == 13 for r in requests)
    ranges = [
        (
            datetime.fromisoformat(r["timeQuantum"]["startDate"]),
            datetime.fromisoformat(r["timeQuantum"]["endDate"]),
        )
        for r in requests
    ]
    assert all(end - start <= timedelta(days=1) for start, end in ranges)
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:], strict=False))
    assert [p.completed for p in progress] == [0, 0, 1, 1, 2, 2, 3]
    assert all(p.total == 3 for p in progress)
    assert result.interval.total == 26
    assert len(result.daily) == 2
    assert "private-" not in repr(result)


def test_padding_is_clipped_to_current_minute_at_recent_midnight():
    q = query(1)
    chunks = query_chunks(q, q.end + timedelta(seconds=45))
    assert chunks[-1][1] == q.end
    chunks = query_chunks(q, q.end + timedelta(minutes=2, seconds=59))
    assert chunks[-1][1] == q.end + timedelta(minutes=2)


def test_minimum_calendar_date_padding_does_not_overflow():
    q = resolve_query(Conditions("custom", "0001-01-01", "0001-01-01"))
    assert query_chunks(q, datetime.now(SHANGHAI))[0][0] == q.start


def test_success_empty_series_is_missing_not_fake_zero():
    result = run(
        [], history=lambda _: httpx.Response(200, json={"success": True, "code": 200, "data": []})
    )
    assert result.interval.total is None
    assert all(d.total is None and d.known_count == 0 for d in result.daily)
    assert result.complete_days is None


@pytest.mark.parametrize(
    "field,value", [("deviceId", 1), ("projectId", 1), ("unit", "kW"), ("pointId", "foreign")]
)
def test_history_identity_mismatch_rejects_entire_snapshot(field, value):
    def wrong(_):
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 200,
                "data": [{**metadata(POINTS[0]), field: value, "pointDataList": []}],
            },
        )

    with pytest.raises(EnergySourceError, match="不一致"):
        run([], history=wrong)


def test_dictionary_mismatch_stops_before_history():
    calls = []
    with pytest.raises(EnergySourceError, match="不一致"):
        run(calls, dictionary=[metadata(POINTS[0])])
    assert not any(path == HISTORY_PATH for path, _ in calls)


def test_partial_failure_is_not_empty_success_and_error_is_sanitized():
    count = 0

    def fail(request):
        nonlocal count
        count += 1
        if count == 2:
            raise httpx.ReadTimeout("private-password private-token", request=request)
        return httpx.Response(200, json={"success": True, "code": 200, "data": []})

    with pytest.raises(EnergySourceError, match="超时") as caught:
        run([], history=fail)
    assert count == 2
    assert "private-" not in str(caught.value)


def test_cancel_after_first_piece_prevents_next_request_and_publish():
    calls = []

    def cancel():
        return sum(path == HISTORY_PATH for path, _ in calls) >= 1

    with pytest.raises(QueryCancelled):
        load_energy_share(
            query(2),
            settings=Settings("a", "b"),
            cancelled=cancel,
            transport=httpx.MockTransport(handler(calls)),
        )
    assert sum(path == HISTORY_PATH for path, _ in calls) == 1


def test_cancel_before_auth_performs_no_io():
    calls = []
    with pytest.raises(QueryCancelled):
        load_energy_share(
            query(), cancelled=lambda: True, transport=httpx.MockTransport(handler(calls))
        )
    assert calls == []


def test_forged_query_rejected_before_auth():
    calls = []
    q = query()
    with pytest.raises(EnergySourceError, match="统计窗口"):
        load_energy_share(
            replace(q, end=q.end - timedelta(minutes=1)),
            transport=httpx.MockTransport(handler(calls)),
        )
    assert calls == []


def test_total_deadline_is_checked_before_publish():
    calls = []
    counter = 0

    def clock():
        nonlocal counter
        counter += 1
        return 0 if counter < 7 else 1000

    with pytest.raises(EnergySourceError, match="900秒"):
        load_energy_share(
            query(),
            settings=Settings("a", "b"),
            clock=clock,
            transport=httpx.MockTransport(handler(calls)),
        )
