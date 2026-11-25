from datetime import UTC, datetime, timedelta

import pytest

from axiomforge.ahu_temperature import PointSpec
from axiomforge.region_climate import (
    TemperatureTimeCell,
    analyze_region,
    probe_distributions,
    probe_temperature_cells,
    region_distribution,
    region_temperature_cells,
)
from axiomforge.region_climate_source import ClimateSnapshot, Probe, Region

START = datetime(2026, 11, 13, tzinfo=UTC)
REGION = Region(
    "test", "2F", "测试区域", "AHU3", (Probe(1, "甲", None, None), Probe(2, "乙", None, None))
)


def at(minute: int) -> datetime:
    return START + timedelta(minutes=minute)


def snapshot(
    temps: dict[int, dict[datetime, float]],
    states: dict[datetime, str] | None = None,
    end: int = 21,
) -> ClimateSnapshot:
    return ClimateSnapshot(
        START.date(), START, at(end), at(end), temps, {2: {at(0): 60}}, {"AHU3": states or {}}
    )


def test_trend_weights_probes_not_measurements_and_keeps_missing_buckets() -> None:
    data = snapshot({1: {at(0): 26, at(1): 28, at(2): 27}, 2: {at(3): 23}})
    analysis = analyze_region(data, REGION)

    assert analysis.trend[0].temperature == 25
    assert analysis.trend[0].temperature_count == 2
    assert analysis.trend[0].humidity == 60
    assert analysis.trend[0].humidity_count == 1
    assert analysis.trend[1].temperature is None
    assert analysis.trend[1].state == "unknown"
    assert analysis.trend[-1].end == at(21)
    assert analyze_region(data, REGION, probe_id=1).trend[0].temperature == 27
    assert analyze_region(data, REGION, probe_id=1).trend[0].humidity is None


def test_start_window_uses_exact_timestamps_and_comparable_probes() -> None:
    data = snapshot(
        {1: {at(2): 29, at(6): 28, at(8): 50, at(10): 26, at(15): 24, at(20): 50}, 2: {at(3): 10}},
        {
            at(1): "stopped",
            at(5): "stopped",
            at(7): "stopped",
            at(10): "running",
            at(14): "running",
            at(18): "running",
            at(20): "stopped",
        },
    )
    (event,) = analyze_region(data, REGION).events

    assert event.at == at(10)
    assert event.probe_count == 1
    assert event.before.start == at(1)
    assert event.before.end == at(8)
    assert (event.before.minimum, event.before.maximum) == (28, 29)
    assert event.after.end == at(19)
    assert (event.after.minimum, event.after.maximum) == (24, 26)


def test_start_does_not_bridge_conflicts_or_long_gaps() -> None:
    data = snapshot(
        {},
        {
            at(0): "running",
            at(2): "stopped",
            at(3): "conflict",
            at(4): "running",
            at(5): "stopped",
            at(11): "running",
            at(12): "stopped",
            at(17): "running",
        },
    )

    assert [event.at for event in analyze_region(data, REGION).events] == [at(17)]
    assert analyze_region(data, REGION).trend[0].state == "conflict"


def test_start_keeps_one_measured_side_and_omits_disjoint_ranges() -> None:
    states = {at(4): "stopped", at(5): "running", at(9): "running"}
    disjoint = snapshot({1: {at(4): 29}, 2: {at(5): 24}}, states)
    (event,) = analyze_region(disjoint, REGION).events

    assert event.probe_count == 0
    assert event.before.minimum is None and event.after.minimum is None

    (event,) = analyze_region(disjoint, REGION, probe_id=1).events
    assert event.probe_count == 0
    assert event.before.minimum == 29 and event.after.minimum is None


def test_excursions_use_strict_thresholds_and_expose_local_hotspots() -> None:
    data = snapshot(
        {
            1: {at(0): 27, at(5): 28, at(15): 28, at(20): 26},
            2: {at(0): 23, at(5): 23, at(15): 22, at(20): 22},
        }
    )
    events = analyze_region(data, REGION).excursions

    assert [(item.start, item.end, item.probe_id, item.kind) for item in events] == [
        (at(0), at(10), 1, "hot"),
        (at(15), at(20), 1, "hot"),
    ]
    assert (events[0].minimum, events[0].maximum) == (27, 28)
    assert analyze_region(data, REGION, lower=24, upper=30).excursions[0].kind == "cold"
    with pytest.raises(ValueError, match="过冷阈值须小于过热阈值"):
        analyze_region(data, REGION, lower=26, upper=26)


def test_region_distribution_counts_valid_bucket_means_with_left_closed_bins() -> None:
    values = (19, 20, 22, 24, 26, 28, 30)
    data = snapshot(
        {1: {at(5 * index): value for index, value in enumerate(values)}},
        end=40,
    )
    trend = analyze_region(data, REGION).trend

    distribution = region_distribution(REGION.name, trend)

    assert distribution.label == REGION.name
    assert distribution.bins == (1, 1, 1, 1, 1, 1, 1)
    assert distribution.count == len(values)
    assert not distribution.pending


def test_probe_distribution_excludes_missing_buckets_and_distinguishes_pending() -> None:
    region = Region(
        "test",
        "2F",
        "测试区域",
        "AHU3",
        (
            Probe(1, "甲", PointSpec("one", 1, "℃", "temperature"), None),
            Probe(2, "乙", PointSpec("two", 2, "℃", "temperature"), None),
            Probe(-1, "待确认", None, None),
        ),
    )
    data = snapshot(
        {
            1: {at(-1): 10, at(0): 19, at(1): 21, at(5): 28, at(10): 30},
            -1: {at(0): 28},
        },
        end=10,
    )

    first, empty, pending = probe_distributions(data, region)

    assert first.bins == (0, 1, 0, 0, 0, 1, 0)
    assert first.count == 2
    assert (empty.count, empty.pending) == (0, False)
    assert (pending.count, pending.pending) == (0, True)


def test_region_time_cells_use_equal_bucket_weights_and_stop_at_query_end() -> None:
    data = snapshot(
        {
            1: {at(0): 20, at(1): 24, at(5): 28, at(15): 21, at(20): 30, at(21): 42},
            2: {at(2): 26, at(5): 28, at(20): 26},
        },
        end=21,
    )
    trend = analyze_region(data, REGION).trend

    assert region_temperature_cells(trend, 5) == (
        TemperatureTimeCell(at(0), at(5), 24, 1, 1),
        TemperatureTimeCell(at(5), at(10), 28, 1, 1),
        TemperatureTimeCell(at(10), at(15), None, 0, 1),
        TemperatureTimeCell(at(15), at(20), 21, 1, 1),
        TemperatureTimeCell(at(20), at(21), 28, 1, 1),
    )
    assert region_temperature_cells(trend, 15) == (
        TemperatureTimeCell(at(0), at(15), 26, 2, 3),
        TemperatureTimeCell(at(15), at(21), 24.5, 2, 2),
    )
    assert region_temperature_cells(trend, 30) == (TemperatureTimeCell(at(0), at(21), 25.25, 4, 5),)


def test_probe_time_cells_keep_shared_timeline_and_pending_rows() -> None:
    region = Region(
        "test",
        "2F",
        "测试区域",
        "AHU3",
        (
            Probe(1, "甲", PointSpec("one", 1, "℃", "temperature"), None),
            Probe(2, "乙", PointSpec("two", 2, "℃", "temperature"), None),
            Probe(-1, "待确认", None, None),
        ),
    )
    data = snapshot(
        {1: {at(1): 20, at(2): 22, at(5): 27, at(16): 30}, 2: {at(20): 18}, -1: {at(6): 28}},
        end=20,
    )

    first, empty, pending = probe_temperature_cells(data, region, 15)

    assert first == (
        TemperatureTimeCell(at(0), at(15), 24, 2, 3),
        TemperatureTimeCell(at(15), at(20), 30, 1, 1),
    )
    missing_cells = (
        TemperatureTimeCell(at(0), at(15), None, 0, 3),
        TemperatureTimeCell(at(15), at(20), None, 0, 1),
    )
    assert empty == pending == missing_cells
    with pytest.raises(ValueError, match="温区时间格须为"):
        probe_temperature_cells(data, region, 10)
