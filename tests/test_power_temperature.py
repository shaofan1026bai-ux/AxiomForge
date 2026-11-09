"""验证真实分析口径，避免缺失、时间边界或选区造成统计失真。"""

import csv
import io
from datetime import datetime, timedelta

import pytest

from axiomforge.power_temperature import (
    POWER,
    SHANGHAI,
    TEMPERATURE,
    QueryRange,
    Row,
    export_snapshot,
    normalize,
    parse_range,
    statistics,
)
from axiomforge.ui.power_temperature_charts import scatter_options, trend_options

START = datetime(2026, 1, 1, tzinfo=SHANGHAI)


def query(minutes=8):
    return QueryRange(START, START + timedelta(minutes=minutes))


def samples(values, start=START):
    return [
        {"ts": (start + timedelta(minutes=i * 2)).strftime("%Y-%m-%d %H:%M:%S"), "val": v}
        for i, v in enumerate(values)
    ]


def test_missing_zero_and_independent_summary():
    snapshot = normalize(
        query(),
        {
            POWER.point_id: samples([0, 2, 4, None]),
            TEMPERATURE.point_id: samples([None, 20, 22, 24]),
        },
        START,
    )
    stats = statistics(snapshot.rows)
    assert (stats.expected, stats.pairs, stats.power_count, stats.temperature_count) == (4, 2, 3, 3)
    assert stats.average_power == 2
    assert stats.max_power == 4
    assert (stats.min_temperature, stats.max_temperature) == (20, 24)
    assert stats.correlation is None
    assert snapshot.rows[0].power == 0
    assert snapshot.rows[-1].power_status == "缺失"


@pytest.mark.parametrize("powers, expected", [([1, 2, 3, 4], 1), ([4, 3, 2, 1], -1)])
def test_known_correlation(powers, expected):
    snapshot = normalize(
        query(),
        {POWER.point_id: samples(powers), TEMPERATURE.point_id: samples([10, 20, 30, 40])},
        START,
    )
    assert statistics(snapshot.rows).correlation == pytest.approx(expected)
    assert statistics(snapshot.selected(query(4))).correlation is None


def test_constant_and_empty_sequences_are_not_weak_correlation():
    snapshot = normalize(
        query(),
        {POWER.point_id: samples([3] * 4), TEMPERATURE.point_id: samples([10, 20, 30, 40])},
        START,
    )
    assert statistics(snapshot.rows).correlation is None
    assert "恒定" in statistics(snapshot.rows).correlation_reason
    empty = normalize(query(), {}, START)
    assert statistics(empty.rows).average_power is None
    assert statistics(empty.rows).pairs == 0


def test_finite_extreme_values_do_not_overflow_statistics():
    rows = [
        Row(START + timedelta(minutes=2 * i), x, x) for i, x in enumerate([1e308, 8e307, 6e307])
    ]
    result = statistics(rows)
    assert result.average_power == pytest.approx(8e307)
    assert result.correlation == pytest.approx(1)


def test_duplicate_conflict_grid_and_end_boundary():
    data = samples([0, 2, 4, 6, 8])
    data += [
        {"ts": "2026-01-01 00:02:00", "val": 2},
        {"ts": "2026-01-01 00:04:00", "val": 5},
        {"ts": "2026-01-01 00:01:00", "val": 8},
        {"ts": "invalid", "val": 10},
    ]
    snapshot = normalize(query(), {POWER.point_id: list(reversed(data))}, START)
    assert [r.time for r in snapshot.rows] == list(query().grid())
    assert snapshot.rows[2].power is None
    assert snapshot.rows[2].power_status == "重复冲突"
    assert snapshot.quality.duplicates == 2
    assert snapshot.quality.conflicts == 1
    assert snapshot.quality.outside == 1
    assert snapshot.quality.off_grid == 1
    assert snapshot.quality.invalid_times == 1


def test_per_series_time_errors_do_not_contaminate_other_series():
    power = samples([1]) + [{"ts": "invalid", "val": 0}]
    temperature = [{"ts": "2025-12-31 23:58:00", "val": 20}]
    snapshot = normalize(query(), {POWER.point_id: power, TEMPERATURE.point_id: temperature}, START)
    assert snapshot.rows[0].power == 1
    assert all(row.temperature is None for row in snapshot.rows)


@pytest.mark.parametrize(
    "data", [[{"ts": "wrong", "val": 0}], [{"ts": "2026-01-01 00:01:00", "val": 1}]]
)
def test_entire_incompatible_timestamps_are_protocol_errors(data):
    with pytest.raises(ValueError, match="网格"):
        normalize(query(), {POWER.point_id: data}, START)


def test_nonfinite_bool_and_numeric_strings_are_invalid():
    snapshot = normalize(
        query(), {POWER.point_id: samples([float("nan"), True, "2", float("inf")])}, START
    )
    assert all(row.power is None for row in snapshot.rows)
    assert {row.power_status for row in snapshot.rows} == {"非有限值", "无效值"}


def test_ranges_and_chunks_preserve_odd_minute_grid():
    start = START + timedelta(minutes=1)
    ranged = QueryRange(start, start + timedelta(days=7))
    chunks = ranged.chunks()
    assert len(chunks) == 7
    assert all(len(chunk.grid()) == 720 for chunk in chunks)
    assert all(chunk.start.minute == 1 for chunk in chunks)
    assert chunks[-1].end == ranged.end
    assert len(QueryRange(START, START + timedelta(minutes=3)).grid()) == 2
    with pytest.raises(ValueError, match="7天"):
        QueryRange(START, START + timedelta(days=7, minutes=1))
    with pytest.raises(ValueError, match="早于"):
        QueryRange(START, START)
    with pytest.raises(ValueError, match="当前"):
        ranged.validate_now(START)
    with pytest.raises(ValueError, match="有效"):
        parse_range("bad", "date")


def test_peak_ties_use_first_and_csv_retains_full_scope():
    snapshot = normalize(
        query(),
        {POWER.point_id: samples([8, 8, 1, None]), TEMPERATURE.point_id: samples([10, 20, 30, 40])},
        START,
    )
    assert statistics(snapshot.rows).max_at == START
    selected = snapshot.selected(query(2))
    assert len(selected) == 1
    csv_bytes = export_snapshot(snapshot)
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    assert len(rows) == 4
    assert rows[-1]["power_kW"] == ""
    assert rows[0]["power_point_id"] == POWER.point_id
    assert rows[0]["source"] == "xiaomai_readonly"
    assert rows[0]["query_end_exclusive"] == "2026-01-01 00:08"


def test_chart_keeps_missing_and_unique_timestamp_for_equal_scatter_coords():
    snapshot = normalize(
        query(),
        {POWER.point_id: samples([1, 1, None, 2]), TEMPERATURE.point_id: samples([20, 20, 30, 40])},
        START,
    )
    trend = trend_options(snapshot, True)
    assert trend["series"][0]["data"][2][1] is None
    assert not trend["series"][0]["connectNulls"]
    assert "min" not in trend["yAxis"][1]
    scatter = scatter_options(snapshot, False)
    dots = scatter["series"][0]["data"]
    assert dots[0][:2] == dots[1][:2]
    assert dots[0][2] != dots[1][2]
    assert len(scatter_options(snapshot, False, query(2))["series"][0]["data"]) == 1
