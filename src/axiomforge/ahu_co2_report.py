"""从日期范围快照生成各台 AHU 的开机分布和高值停机明细。"""

from collections import defaultdict
from dataclasses import dataclass

from axiomforge.ahu_co2 import Event, events_for
from axiomforge.ahu_co2_source import DaySnapshot, RangeSnapshot
from axiomforge.ahu_temperature import DEVICES, SHANGHAI


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    device_id: str
    starts: int
    co2_values: tuple[float, ...]
    high_stopped: int
    high_peak: float | None


@dataclass(frozen=True, slots=True)
class EventRecord:
    snapshot: DaySnapshot
    event: Event


@dataclass(frozen=True, slots=True)
class Report:
    devices: tuple[DeviceSummary, ...]
    records: tuple[EventRecord, ...]


def report_for(snapshot: RangeSnapshot, threshold: float) -> Report:
    """同一台设备连续识别事件，按代表时刻关联当天的时序和航班。"""
    by_device: dict[str, list[DaySnapshot]] = defaultdict(list)
    for day in snapshot.days:
        by_device[day.device.id].append(day)

    summaries: list[DeviceSummary] = []
    records: list[EventRecord] = []
    for device in DEVICES:
        days = sorted(by_device[device.id], key=lambda item: item.start)
        day_by_date = {day.day: day for day in days}
        # 自然日是接口分片，跨午夜的有效采样仍按同一设备的相邻记录处理。
        events = events_for(tuple(row for day in days for row in day.samples), threshold)
        starts = [event for event in events if event.kind == "start"]
        stopped = [event for event in events if event.kind == "high_stopped"]
        summaries.append(
            DeviceSummary(
                device_id=device.id,
                starts=len(starts),
                co2_values=tuple(
                    event.sample.co2 for event in starts if event.sample.co2 is not None
                ),
                high_stopped=len(stopped),
                high_peak=max(
                    (event.sample.co2 for event in stopped if event.sample.co2 is not None),
                    default=None,
                ),
            )
        )
        records.extend(
            EventRecord(day_by_date[event.time.astimezone(SHANGHAI).date()], event)
            for event in events
        )
    return Report(tuple(summaries), tuple(sorted(records, key=lambda record: record.event.time)))
