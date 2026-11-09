"""用边界样本验证每日窗口、运行口径和统计分母，不读取现场数据。"""

import csv
import io
from datetime import date, datetime, timedelta

import pytest

from axiomforge.ahu_temperature import (
    DEVICES,
    SHANGHAI,
    QueryConditions,
    default_conditions,
    export_notes,
    export_snapshot,
    normalize,
    percentile,
    preset_dates,
    resolve_query,
    restore_conditions,
    state_for,
)

NOW = datetime(2026, 11, 9, 12, tzinfo=SHANGHAI)


def custom(start="2026-10-01", end="2026-10-01", daily=False, first="09:00", last="18:00"):
    return resolve_query(QueryConditions("custom", start, end, daily, first, last), NOW)


def samples(values, start=None):
    start = start or custom().start
    return [
        {"ts": (start + timedelta(minutes=i)).isoformat(), "val": value}
        for i, value in enumerate(values)
    ]


def data(temperature, feedback, frequency, start=None):
    return {
        point.point_id: samples(values, start)
        for point, values in zip(DEVICES[0].points, (temperature, feedback, frequency), strict=True)
    }


def test_daily_half_open_and_overnight_start_date_belonging():
    query = custom("2026-10-01", "2026-10-03", True, "22:00", "06:00")
    assert query.expected == 3 * 8 * 60
    assert query.start == datetime(2026, 10, 1, 22, tzinfo=SHANGHAI)
    assert query.end == datetime(2026, 10, 4, 6, tzinfo=SHANGHAI)
    assert [window.day for window in query.windows] == [date(2026, 10, n) for n in (1, 2, 3)]
    assert query.windows[-1].grid()[-1] == query.end - timedelta(minutes=1)
    assert "次日06:00" in query.summary
    assert custom("2026-10-01", "2026-10-03").expected == 3 * 1440


def test_month_preset_has_no_complete_day_on_first_day():
    with pytest.raises(ValueError, match="本月尚无"):
        preset_dates("month", NOW.replace(day=1))
    assert preset_dates("last7", NOW) == ("2026-11-02", "2026-11-08")
    assert preset_dates("yesterday", NOW) == ("2026-11-08", "2026-11-08")


@pytest.mark.parametrize(
    "args",
    [
        ("2026-10-03", "2026-10-01"),
        ("2026-08-01", "2026-11-01"),
        ("2026-10-01", "2026-10-01", True, "09:00", "09:00"),
        ("2026-10-01", "2026-10-01", True, "bad", "09:00"),
        ("2026-11-09", "2026-11-09"),
    ],
)
def test_invalid_or_unfinished_window_rejected(args):
    with pytest.raises(ValueError):
        custom(*args)


def test_future_overnight_not_silently_truncated():
    conditions = QueryConditions("custom", "2026-11-08", "2026-11-08", True, "22:00", "13:00")
    with pytest.raises(ValueError, match="尚未结束"):
        resolve_query(conditions, NOW)


def test_closed_time_switch_ignores_dormant_times_and_supports_39_days():
    query = custom("2026-10-01", "2026-11-08", False, "bad", "bad")
    assert len(query.windows) == 39
    assert query.expected == 39 * 1440


def test_preferences_roll_relative_but_keep_custom_and_validate_shape():
    default = default_conditions(NOW)
    relative = restore_conditions(default.preference(), NOW + timedelta(days=1))
    assert relative.start == "2026-11-03"
    future = QueryConditions("custom", "2026-12-01", "2026-12-02").preference()
    assert restore_conditions(future, NOW).start == "2026-12-01"
    for bad in [
        None,
        [],
        {**default.preference(), "version": True},
        {**default.preference(), "daily": "false"},
        {**default.preference(), "project": "other"},
        {**default.preference(), "period": "unknown"},
        {**default.preference(), "start": "invalid"},
    ]:
        assert restore_conditions(bad, NOW) == default


@pytest.mark.parametrize(
    "feedback,frequency,state",
    [
        (1, 30, "running"),
        (1, 60, "running"),
        (0, 0, "stopped"),
        (1, 0, "conflict"),
        (0, 30, "conflict"),
        (1, None, "unknown"),
        (None, 30, "unknown"),
        (1, 61, "unknown"),
        (1, -1, "unknown"),
        (2, 20, "unknown"),
        (True, 30, "unknown"),
        (1, True, "unknown"),
    ],
)
def test_operating_state_requires_exact_same_time_feedback_and_frequency(
    feedback, frequency, state
):
    assert state_for(feedback, frequency) == state


def test_temperature_independent_from_status_and_raw_zero_preserved():
    query = custom(daily=True, first="09:00", last="09:06")
    device = normalize(
        query,
        data(
            [0, 20, 30, True, None, 24],
            [1, 1, 0, 1, 1, None],
            [30, None, 0, 30, 30, 30],
            query.start,
        ),
        NOW,
    ).devices[0]
    assert device.all_stats.count == 4
    assert device.all_stats.minimum == 0
    assert device.running_stats.count == 1
    assert device.quality.expected == 6
    assert device.quality.temperature_valid == 4
    assert device.quality.unconfirmed == 2
    assert device.rows[3].temperature_quality == "invalid"
    assert device.rows[4].state == "running"
    assert device.quality.state_conflicts == 0


def test_missing_grids_and_gaps_do_not_span_unselected_daily_time():
    query = custom("2026-10-01", "2026-10-02", True, "14:00", "14:03")
    records = data([20, None, 25], [1] * 3, [30] * 3, query.start)
    result = normalize(query, records, NOW).devices[0]
    assert len(result.rows) == result.quality.expected == 6
    assert len(result.daily) == 2
    assert result.daily[1].all_stats.count == 0
    assert result.quality.no_data_days == (date(2026, 10, 2),)
    assert len(result.quality.gaps) == 2
    assert result.quality.gaps[0].start == query.start + timedelta(minutes=1)
    assert result.quality.gaps[0].end == query.start + timedelta(minutes=2)
    assert result.quality.gaps[1].start == query.windows[1].start
    assert result.quality.gaps[1].end == query.windows[1].end


def test_conflict_cannot_be_revived_and_no_closest_time_matching():
    query = custom(daily=True, first="09:00", last="09:02")
    records = data([20, 22], [1, 1], [30, 30], query.start)
    temperature = DEVICES[0].points[0].point_id
    records[temperature] += [
        records[temperature][0],
        {"ts": query.start.isoformat(), "val": 30},
        {"ts": query.start.isoformat(), "val": 20},
        {"ts": (query.start + timedelta(seconds=30)).isoformat(), "val": 40},
        {"ts": query.end.isoformat(), "val": 50},
    ]
    device = normalize(query, records, NOW).devices[0]
    assert device.rows[0].temperature is None
    assert device.rows[0].temperature_quality == "conflict"
    assert device.quality.conflicts == 1
    assert device.quality.duplicates == 1
    assert device.quality.off_grid == 1
    assert device.quality.outside == 1
    assert device.running_stats.count == 1


def test_nonfinite_invalid_time_and_whole_incompatible_series():
    query = custom(daily=True, first="09:00", last="09:03")
    result = normalize(
        query, data([float("inf"), float("nan"), "20"], [1] * 3, [30] * 3, query.start), NOW
    )
    assert result.devices[0].all_stats.count == 0
    with pytest.raises(ValueError, match="不兼容"):
        normalize(query, {DEVICES[0].points[0].point_id: [{"ts": "not-a-time", "val": 20}]}, NOW)


def test_overall_percentiles_and_mean_are_not_mean_of_daily_statistics():
    query = custom("2026-10-01", "2026-10-02", True, "09:00", "09:04")
    records = data([10, 10, 10, 10], [1] * 4, [30] * 4, query.start)
    second = data([30], [1], [30], query.windows[1].start)
    for pid, values in second.items():
        records[pid] += values
    result = normalize(query, records, NOW).devices[0]
    assert result.running_stats.mean == 14
    assert result.running_stats.p95 == pytest.approx(26)
    assert sum(result.running_stats.bins) == 5
    assert percentile([10, 30], 0.05) == 11


def test_empty_single_sample_and_exports_keep_all_devices_and_empty_days():
    query = custom("2026-10-01", "2026-10-02", True, "09:00", "09:02")
    snapshot = normalize(query, data([25], [1], [30], query.start), NOW)
    stats = snapshot.devices[0].running_stats
    assert stats.minimum == stats.maximum == stats.p05 == stats.p95 == stats.mean == 25
    assert all(device.running_stats.mean is None for device in snapshot.devices[1:])
    for kind, expected in [("summary", 5), ("daily", 10), ("distribution", 70), ("quality", 5)]:
        raw = export_snapshot(snapshot, kind)
        assert raw.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows) == expected
        assert all(row["实际结束不含"] == query.end.isoformat() for row in rows)
        assert rows[0]["分位数算法"] == "linear_(n-1)*p_v1"
    assert "不补零" in export_notes(snapshot).decode("utf-8-sig")


def test_last_supported_date_reports_validation_error_not_overflow():
    with pytest.raises(ValueError, match="超出支持日期"):
        custom("9999-12-31", "9999-12-31")
