"""区域温湿度日趋势、AHU 开机前后温区和温度越线的纯计算。"""

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean
from typing import Literal

from axiomforge.ahu_temperature import BINS, INTERVAL
from axiomforge.region_climate_source import ClimateSnapshot, Region

BUCKET = timedelta(minutes=5)
TIME_CELL_MINUTES = (5, 15, 30)
MAX_STATE_GAP = timedelta(minutes=5)
EVENT_WINDOW = timedelta(minutes=60)
DEFAULT_LOWER = 22.0
DEFAULT_UPPER = 26.0
CONFIRMED_STATES = {"running", "stopped"}
ExcursionKind = Literal["cold", "hot"]
SamplesByProbe = Mapping[int, Mapping[datetime, float]]


@dataclass(frozen=True, slots=True)
class TrendPoint:
    at: datetime
    end: datetime
    temperature: float | None
    humidity: float | None
    temperature_count: int
    humidity_count: int
    state: str


@dataclass(frozen=True, slots=True)
class TemperatureDistribution:
    label: str
    bins: tuple[int, ...]
    pending: bool = False

    @property
    def count(self) -> int:
        return sum(self.bins)


@dataclass(frozen=True, slots=True)
class TemperatureTimeCell:
    start: datetime
    end: datetime
    temperature: float | None
    count: int
    expected: int


@dataclass(frozen=True, slots=True)
class TemperatureRange:
    start: datetime
    end: datetime
    minimum: float | None
    maximum: float | None
    sample_count: int
    probe_count: int


@dataclass(frozen=True, slots=True)
class StartComparison:
    at: datetime
    before: TemperatureRange
    after: TemperatureRange
    probe_count: int


@dataclass(frozen=True, slots=True)
class Excursion:
    start: datetime
    end: datetime
    kind: ExcursionKind
    minimum: float
    maximum: float
    probe_id: int | None


@dataclass(frozen=True, slots=True)
class RegionAnalysis:
    trend: tuple[TrendPoint, ...]
    events: tuple[StartComparison, ...]
    excursions: tuple[Excursion, ...]


def _bucket_means(
    data: SamplesByProbe,
    probe_ids: Sequence[int],
    start: datetime,
    end: datetime,
) -> dict[int, dict[int, float]]:
    """先按探头求桶内均值，再由调用方合成等权区域均值。"""
    result: dict[int, dict[int, float]] = defaultdict(dict)
    for probe_id in probe_ids:
        buckets: dict[int, list[float]] = defaultdict(list)
        for stamp, value in data.get(probe_id, {}).items():
            if start <= stamp < end:
                buckets[(stamp - start) // BUCKET].append(value)
        for index, values in buckets.items():
            result[index][probe_id] = fmean(values)
    return result


def _temperature_bins(values: Iterable[float]) -> tuple[int, ...]:
    counts = [0] * len(BINS)
    for value in values:
        for index, interval in enumerate(BINS):
            if interval.lower <= value < interval.upper:
                counts[index] += 1
                break
    return tuple(counts)


def region_distribution(label: str, trend: Sequence[TrendPoint]) -> TemperatureDistribution:
    """各有效五分钟区域均温各计一次，缺测桶不计入分母。"""
    return TemperatureDistribution(
        label,
        _temperature_bins(point.temperature for point in trend if point.temperature is not None),
    )


def _probe_bucket_temperatures(
    snapshot: ClimateSnapshot, region: Region
) -> dict[int, dict[int, float]]:
    temperatures = _bucket_means(
        snapshot.temperatures,
        [probe.id for probe in region.probes if probe.temperature is not None],
        snapshot.start,
        snapshot.end,
    )
    values_by_probe: dict[int, dict[int, float]] = defaultdict(dict)
    for index, values in temperatures.items():
        for probe_id, value in values.items():
            values_by_probe[probe_id][index] = value
    return values_by_probe


def probe_distributions(
    snapshot: ClimateSnapshot, region: Region
) -> tuple[TemperatureDistribution, ...]:
    """用与单探头趋势相同的日窗口和五分钟均温统计各设备。"""
    values_by_probe = _probe_bucket_temperatures(snapshot, region)
    return tuple(
        TemperatureDistribution(
            probe.name,
            _temperature_bins(values_by_probe[probe.id].values()),
            probe.temperature is None,
        )
        for probe in region.probes
    )


def _time_cell_width(minutes: int) -> int:
    if minutes not in TIME_CELL_MINUTES:
        raise ValueError("温区时间格须为 5、15 或 30 分钟。")
    return timedelta(minutes=minutes) // BUCKET


def _temperature_time_cells(
    start: datetime, end: datetime, temperatures: Mapping[int, float], width: int
) -> tuple[TemperatureTimeCell, ...]:
    total = math.ceil((end - start) / BUCKET)
    cells = []
    for first in range(0, total, width):
        last = min(first + width, total)
        # 各有效五分钟桶等权，缺测桶计入覆盖数的分母。
        values = [temperatures[index] for index in range(first, last) if index in temperatures]
        cells.append(
            TemperatureTimeCell(
                start + first * BUCKET,
                min(start + last * BUCKET, end),
                fmean(values) if values else None,
                len(values),
                last - first,
            )
        )
    return tuple(cells)


def region_temperature_cells(
    trend: Sequence[TrendPoint], minutes: int
) -> tuple[TemperatureTimeCell, ...]:
    """将成功快照中的区域均温对齐至日内时段。"""
    width = _time_cell_width(minutes)
    if not trend:
        return ()
    start = trend[0].at
    temperatures = {
        (point.at - start) // BUCKET: point.temperature
        for point in trend
        if point.temperature is not None
    }
    return _temperature_time_cells(start, trend[-1].end, temperatures, width)


def probe_temperature_cells(
    snapshot: ClimateSnapshot, region: Region, minutes: int
) -> tuple[tuple[TemperatureTimeCell, ...], ...]:
    """按探头登记顺序返回温区时间格，点位待确认的探头保留缺测格。"""
    width = _time_cell_width(minutes)
    temperatures = _probe_bucket_temperatures(snapshot, region)
    return tuple(
        _temperature_time_cells(snapshot.start, snapshot.end, temperatures[probe.id], width)
        for probe in region.probes
    )


def _trend(
    snapshot: ClimateSnapshot,
    temperatures: Mapping[int, Mapping[int, float]],
    humidities: Mapping[int, Mapping[int, float]],
    states: Mapping[datetime, str],
) -> tuple[TrendPoint, ...]:
    start, end = snapshot.start, snapshot.end
    states_by_bucket: dict[int, set[str]] = defaultdict(set)
    for stamp, state in states.items():
        if start <= stamp < end:
            states_by_bucket[(stamp - start) // BUCKET].add(state)
    count = math.ceil((end - start) / BUCKET)
    result = []
    for index in range(count):
        at = start + index * BUCKET
        temp_values = tuple(temperatures.get(index, {}).values())
        rh_values = tuple(humidities.get(index, {}).values())
        bucket_states = states_by_bucket.get(index, set())
        if "conflict" in bucket_states:
            state = "conflict"
        elif CONFIRMED_STATES <= bucket_states:
            state = "mixed"
        else:
            state = next(iter(bucket_states)) if len(bucket_states) == 1 else "unknown"
        result.append(
            TrendPoint(
                at,
                min(at + BUCKET, end),
                fmean(temp_values) if temp_values else None,
                fmean(rh_values) if rh_values else None,
                len(temp_values),
                len(rh_values),
                state,
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class _StatePeriod:
    state: str
    first: datetime
    last: datetime


def _periods(states: Mapping[datetime, str], start: datetime, end: datetime) -> list[_StatePeriod]:
    periods: list[_StatePeriod] = []
    for stamp, state in sorted(states.items()):
        if not start <= stamp < end:
            continue
        if (
            periods
            and state in CONFIRMED_STATES
            and periods[-1].state == state
            and stamp - periods[-1].last <= MAX_STATE_GAP
        ):
            previous = periods[-1]
            periods[-1] = _StatePeriod(state, previous.first, stamp)
        else:
            periods.append(_StatePeriod(state, stamp, stamp))
    return periods


def _temperature_range(
    buckets: Mapping[int, Mapping[int, float]],
    start: datetime,
    end: datetime,
    common: set[int],
) -> TemperatureRange:
    means = []
    represented: set[int] = set()
    for values in buckets.values():
        # 同一对比中的区域均温只使用共同探头，缺少任一探头的时间桶留空。
        if common and not common <= values.keys():
            continue
        used = common or values.keys()
        means.append(fmean(values[probe_id] for probe_id in used))
        represented.update(used)
    return TemperatureRange(
        start,
        end,
        min(means, default=None),
        max(means, default=None),
        len(means),
        len(represented),
    )


def _events(
    snapshot: ClimateSnapshot, probe_ids: Sequence[int], states: Mapping[datetime, str]
) -> tuple[StartComparison, ...]:
    periods = _periods(states, snapshot.start, snapshot.end)
    result = []
    for index, (previous, running) in enumerate(zip(periods, periods[1:], strict=False), start=1):
        at = running.first
        if (
            previous.state != "stopped"
            or running.state != "running"
            or at - previous.last > MAX_STATE_GAP
        ):
            continue
        # 停机与启动报告之间可能缺少采样，前段仅到最后确认停机的分钟为止。
        before_start = max(previous.first, at - EVENT_WINDOW)
        before_end = min(at, previous.last + INTERVAL)
        after_end = min(snapshot.end, at + EVENT_WINDOW, running.last + INTERVAL)
        if index + 1 < len(periods):
            after_end = min(after_end, periods[index + 1].first)
        before = _bucket_means(snapshot.temperatures, probe_ids, before_start, before_end)
        after = _bucket_means(snapshot.temperatures, probe_ids, at, after_end)
        before_ids = {probe_id for values in before.values() for probe_id in values}
        after_ids = {probe_id for values in after.values() for probe_id in values}
        common = before_ids & after_ids
        if before_ids and after_ids and not common:
            # 两段都取到温度却没有共同探头，分别求值会掩盖取样位置的差异。
            before, after = {}, {}
        before_range = _temperature_range(before, before_start, before_end, common)
        after_range = _temperature_range(after, at, after_end, common)
        result.append(
            StartComparison(
                at,
                before_range,
                after_range,
                len(common) if before_range.sample_count and after_range.sample_count else 0,
            )
        )
    return tuple(result)


def _excursions(
    trend: Sequence[TrendPoint],
    temperatures: Mapping[int, Mapping[int, float]],
    probe_id: int | None,
    lower: float,
    upper: float,
) -> list[Excursion]:
    result: list[Excursion] = []
    active: Excursion | None = None
    for index, point in enumerate(trend):
        value = point.temperature if probe_id is None else temperatures.get(index, {}).get(probe_id)
        kind: ExcursionKind | None = None
        if value is not None:
            if value < lower:
                kind = "cold"
            elif value > upper:
                kind = "hot"
        if active is not None and kind != active.kind:
            result.append(active)
            active = None
        if kind is not None and value is not None:
            active = Excursion(
                active.start if active else point.at,
                point.end,
                kind,
                min(value, active.minimum) if active else value,
                max(value, active.maximum) if active else value,
                probe_id,
            )
    if active is not None:
        result.append(active)
    return result


def analyze_region(
    snapshot: ClimateSnapshot,
    region: Region,
    *,
    lower: float = DEFAULT_LOWER,
    upper: float = DEFAULT_UPPER,
    probe_id: int | None = None,
) -> RegionAnalysis:
    """从同一日只读快照计算当前区域，探头和阈值切换均无需重新取数。"""
    if (
        isinstance(lower, bool)
        or isinstance(upper, bool)
        or not math.isfinite(lower)
        or not math.isfinite(upper)
        or lower >= upper
    ):
        raise ValueError("过冷阈值和过热阈值须为有限数值，且过冷阈值须小于过热阈值。")
    probe_ids = [probe.id for probe in region.probes]
    if probe_id is not None:
        if probe_id not in probe_ids:
            raise ValueError("所选探头不属于当前区域。")
        probe_ids = [probe_id]
    temperatures = _bucket_means(snapshot.temperatures, probe_ids, snapshot.start, snapshot.end)
    humidities = _bucket_means(snapshot.humidities, probe_ids, snapshot.start, snapshot.end)
    states = snapshot.ahu_states.get(region.ahu_id, {})
    trend = _trend(snapshot, temperatures, humidities, states)
    # 区域均值与单探头越线分别呈现，便于发现区域均值掩盖的局部偏差。
    excursions = _excursions(trend, temperatures, None, lower, upper) if probe_id is None else []
    for individual_id in probe_ids:
        excursions.extend(_excursions(trend, temperatures, individual_id, lower, upper))
    excursions.sort(key=lambda item: item.start)
    return RegionAnalysis(trend, _events(snapshot, probe_ids, states), tuple(excursions))
