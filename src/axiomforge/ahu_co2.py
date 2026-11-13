"""从设备分钟采样识别 AHU 开机及高 CO₂ 停机证据。"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

MAX_STATE_GAP = timedelta(minutes=4)
CO2_LOOKBACK = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Observation:
    time: datetime
    co2: float | None
    temperature: float | None
    frequency: float | None
    feedback: float | None
    state: str
    damper: float | None
    setpoint: float | None
    state_invalid: bool = False


@dataclass(frozen=True, slots=True)
class Event:
    kind: Literal["start", "high_stopped"]
    time: datetime
    sample: Observation
    previous_stopped: datetime | None = None
    end: datetime | None = None
    count: int = 1
    start: datetime | None = None


def _has_co2(row: Observation) -> bool:
    value = row.co2
    return value is not None and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _start_events(rows: Sequence[Observation]) -> list[Event]:
    events: list[Event] = []
    last_co2: Observation | None = None
    last_state: Observation | None = None
    stopped_sample: Observation | None = None
    for row in rows:
        if _has_co2(row):
            last_co2 = row
        if row.state_invalid or row.state == "conflict":
            last_state = None
            stopped_sample = None
        elif row.state == "stopped":
            # 在停机证据出现时锁定候选浓度，避免采用启停边界内的读数。
            stopped_sample = (
                last_co2
                if last_co2 is not None and row.time - last_co2.time <= CO2_LOOKBACK
                else replace(row, co2=None)
            )
            last_state = row
        elif row.state == "running":
            if (
                last_state is not None
                and stopped_sample is not None
                and row.time - last_state.time <= MAX_STATE_GAP
            ):
                events.append(Event("start", row.time, stopped_sample, last_state.time))
            last_state = row
            stopped_sample = None
    return events


def _high_stopped_event(group: Sequence[Observation]) -> Event:
    # 等值峰取首次出现，首末时刻仅表示观察到高值停机的采样边界。
    peak = max(group, key=lambda row: row.co2 or 0)
    return Event(
        "high_stopped",
        peak.time,
        peak,
        end=group[-1].time,
        count=len(group),
        start=group[0].time,
    )


def _high_stopped_events(rows: Sequence[Observation], threshold: float) -> list[Event]:
    events: list[Event] = []
    group: list[Observation] = []
    for row in rows:
        co2 = row.co2 if _has_co2(row) else None
        breaks_group = row.state_invalid or row.state in {"running", "conflict"}
        if group and (
            breaks_group
            or (co2 is not None and co2 < threshold)
            or row.time - group[-1].time > MAX_STATE_GAP
        ):
            events.append(_high_stopped_event(group))
            group.clear()
        if not breaks_group and row.state == "stopped" and co2 is not None and co2 >= threshold:
            group.append(row)
    if group:
        events.append(_high_stopped_event(group))
    return events


def events_for(rows: Sequence[Observation], threshold: float) -> tuple[Event, ...]:
    """用同一台设备按时间排序的采样，按事件代表时刻返回证据。"""
    if isinstance(threshold, bool) or not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("CO₂ 对照浓度需为有限正数。")
    events = _start_events(rows) + _high_stopped_events(rows, threshold)
    return tuple(sorted(events, key=lambda event: event.time))
