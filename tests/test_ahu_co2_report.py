"""验证范围内跨日事件归属与分布样本口径。"""

from datetime import date, datetime, time, timedelta

from axiomforge.ahu_co2 import Observation
from axiomforge.ahu_co2_report import report_for
from axiomforge.ahu_co2_source import DEVICE_BY_ID, DaySnapshot, RangeSnapshot
from axiomforge.ahu_temperature import SHANGHAI

DAY = date(2026, 11, 11)
MIDNIGHT = datetime.combine(DAY, time.min, SHANGHAI)


def sample(offset: int, state: str, co2: float | None) -> Observation:
    frequency, feedback = (30, 1) if state == "running" else (0, 0)
    return Observation(
        MIDNIGHT + timedelta(minutes=offset), co2, 24.2, frequency, feedback, state, 10, 650
    )


def day(device_id: str, offset: int, *rows: Observation) -> DaySnapshot:
    start = MIDNIGHT + timedelta(days=offset)
    return DaySnapshot(
        DEVICE_BY_ID[device_id],
        start.date(),
        start,
        start + timedelta(days=1),
        rows,
        MIDNIGHT,
        None,
        (),
        "",
    )


def test_range_joins_midnight_per_device_and_uses_event_day_for_details() -> None:
    previous, current = (
        day("AHU1", -1, sample(-2, "stopped", 710)),
        day("AHU1", 0, sample(0, "stopped", 726), sample(2, "running", 704)),
    )
    other = day("AHU2", 0, sample(0, "running", 680))
    report = report_for(RangeSnapshot(previous.day, DAY, (current, other, previous), MIDNIGHT), 700)

    high, start = report.records
    assert high.snapshot is current and high.event.kind == "high_stopped"
    assert high.event.start == previous.samples[0].time and high.event.count == 2
    assert start.snapshot is current and start.event.kind == "start"
    assert start.event.sample is current.samples[0]
    assert report.devices[0].co2_values == (726,)
    assert report.devices[0].high_stopped == 1
    assert report.devices[0].high_peak == 726
    assert len(report.devices) == 5 and report.devices[1].starts == 0


def test_range_keeps_unpaired_starts_out_of_concentration_distribution() -> None:
    snapshot = day("AHU3", 0, sample(3, "stopped", None), sample(5, "running", 690))
    report = report_for(RangeSnapshot(DAY, DAY, (snapshot,), MIDNIGHT), 700)

    assert report.devices[2].starts == 1
    assert not report.devices[2].co2_values
    assert report.records[0].event.sample.co2 is None


def test_start_after_midnight_uses_previous_day_sample() -> None:
    previous = day("AHU4", -1, sample(-2, "stopped", 650))
    current = day("AHU4", 0, sample(0, "running", 653))
    report = report_for(RangeSnapshot(previous.day, DAY, (previous, current), MIDNIGHT), 700)

    (record,) = report.records
    assert record.snapshot is current
    assert record.event.kind == "start"
    assert record.event.sample is previous.samples[0]
