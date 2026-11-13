from datetime import datetime, timedelta

import pytest

from axiomforge.ahu_co2 import Observation, events_for
from axiomforge.ahu_temperature import SHANGHAI

BASE = datetime(2026, 11, 11, 8, tzinfo=SHANGHAI)


def at(minute: int) -> datetime:
    return BASE + timedelta(minutes=minute)


def row(minute: int, state: str, co2: float | None = None, *, invalid: bool = False) -> Observation:
    frequency, feedback = {
        "stopped": (0.0, 0.0),
        "running": (32.0, 1.0),
        "conflict": (0.0, 1.0),
        "unknown": (None, None),
    }[state]
    return Observation(at(minute), co2, 24.6, frequency, feedback, state, None, None, invalid)


def test_start_uses_last_reading_at_or_before_confirmed_stop():
    rows = [
        row(0, "running", 598),
        row(1, "unknown", 625),
        row(6, "stopped"),
        row(8, "unknown", 849),
        row(10, "running", 920),
        row(12, "running", 916),
    ]

    (event,) = events_for(rows, 1000)
    assert event.kind == "start"
    assert event.time == at(10)
    assert event.previous_stopped == at(6)
    assert event.sample is rows[1]


def test_start_with_no_recent_co2_retains_event_and_stop_sample():
    rows = [row(0, "unknown", 701), row(6, "stopped"), row(8, "running", 705)]

    (event,) = events_for(rows, 1000)
    assert event.time == at(8)
    assert event.sample == rows[1]
    assert event.sample.co2 is None


@pytest.mark.parametrize(
    ("intervening", "running_minute"),
    [(None, 5), (row(1, "conflict"), 2), (row(1, "unknown", invalid=True), 2)],
)
def test_start_requires_recent_stop_and_no_invalid_state_between(
    intervening: Observation | None, running_minute: int
):
    rows = [row(0, "stopped", 650)]
    if intervening is not None:
        rows.append(intervening)
    rows.append(row(running_minute, "running", 657))

    assert events_for(rows, 1000) == ()


def test_high_stopped_requires_same_minute_evidence_and_splits_on_low_co2():
    rows = [
        row(0, "stopped", 800),
        row(1, "unknown", 1040),
        row(2, "stopped", 915),
        row(4, "stopped", 901),
        row(5, "unknown", 799),
        row(6, "stopped", 980),
        row(11, "stopped", 1040),
        row(12, "unknown", 1500),
    ]

    first, second, third = events_for(rows, 800)
    assert first.kind == second.kind == third.kind == "high_stopped"
    assert (first.start, first.time, first.end, first.count) == (at(0), at(2), at(4), 3)
    assert first.sample.co2 == 915
    assert (second.start, second.end, second.count) == (at(6), at(6), 1)
    assert (third.start, third.end, third.count) == (at(11), at(11), 1)


def test_high_stopped_is_broken_by_start_conflict_and_invalid():
    rows = [
        row(0, "stopped", 800),
        row(1, "running", 820),
        row(2, "stopped", 833),
        row(3, "conflict", 1200),
        row(4, "stopped", 856),
        row(5, "stopped", 1220, invalid=True),
        row(6, "stopped", 872),
    ]

    events = events_for(rows, 800)
    assert [(event.kind, event.time) for event in events] == [
        ("high_stopped", at(0)),
        ("start", at(1)),
        ("high_stopped", at(2)),
        ("high_stopped", at(4)),
        ("high_stopped", at(6)),
    ]


@pytest.mark.parametrize("threshold", [0, -1, float("nan"), float("inf"), True])
def test_invalid_threshold_is_rejected(threshold: float):
    with pytest.raises(ValueError, match="有限正数"):
        events_for([], threshold)
