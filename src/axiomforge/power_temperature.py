"""功率与室外温度的只读领域契约；统计和导出均基于同一份快照。"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ID = 832
PROJECT_KEY = "hangxin-airport"
INTERVAL = timedelta(minutes=2)


@dataclass(frozen=True)
class PointSpec:
    """测点归属独立于页面展示名，避免按名称猜测设备关系。"""

    device_id: int
    device_name: str
    point_id: str
    point_name: str
    unit: str


POWER = PointSpec(12744, "1#空调机组电表", "wh171k_mspm03_totalpower", "总有功功率", "kW")
TEMPERATURE = PointSpec(12788, "1#空调冷水机组", "nfdh9q_mscrk01_c21", "室外温度", "℃")
POINTS = (POWER, TEMPERATURE)


@dataclass(frozen=True)
class QueryRange:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("查询时间必须包含时区。")
        if self.start.second or self.start.microsecond or self.end.second or self.end.microsecond:
            raise ValueError("查询时间需精确到分钟。")
        if self.start >= self.end:
            raise ValueError("开始时间必须早于结束时间。")
        if self.end - self.start > timedelta(days=7):
            raise ValueError("一次最多查询连续7天。")

    def validate_now(self, now: datetime | None = None) -> None:
        if self.end > (now or datetime.now(SHANGHAI)).replace(second=0, microsecond=0):
            raise ValueError("结束时间不能晚于当前分钟。")

    def grid(self) -> tuple[datetime, ...]:
        count = math.ceil((self.end - self.start) / INTERVAL)
        return tuple(self.start + i * INTERVAL for i in range(count))

    def chunks(self) -> tuple[QueryRange, ...]:
        chunks = []
        start = self.start
        while start < self.end:
            end = min(start + timedelta(days=1), self.end)
            chunks.append(QueryRange(start, end))
            start = end
        return tuple(chunks)


def parse_range(start: str, end: str) -> QueryRange:
    """浏览器日期输入是现场时间，不能由执行主机的时区解释。"""
    try:
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if first.tzinfo is not None or last.tzinfo is not None:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("请输入有效的开始和结束日期时间。") from None
    query = QueryRange(first.replace(tzinfo=SHANGHAI), last.replace(tzinfo=SHANGHAI))
    query.validate_now()
    return query


def format_time(moment: datetime) -> str:
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class Row:
    time: datetime
    power: float | None
    temperature: float | None
    power_status: str = "有效"
    temperature_status: str = "有效"

    @property
    def paired(self) -> bool:
        return self.power is not None and self.temperature is not None


@dataclass(frozen=True)
class Quality:
    invalid_times: int = 0
    outside: int = 0
    off_grid: int = 0
    duplicates: int = 0
    conflicts: int = 0


@dataclass(frozen=True)
class Snapshot:
    query: QueryRange
    queried_at: datetime
    rows: tuple[Row, ...]
    quality: Quality = Quality()

    def selected(self, selection: QueryRange | None = None) -> tuple[Row, ...]:
        if selection is None:
            return self.rows
        return tuple(row for row in self.rows if selection.start <= row.time < selection.end)


@dataclass(frozen=True)
class Statistics:
    expected: int
    pairs: int
    power_count: int
    temperature_count: int
    average_power: float | None
    max_power: float | None
    max_at: datetime | None
    min_temperature: float | None
    max_temperature: float | None
    correlation: float | None
    correlation_reason: str


def statistics(rows: Sequence[Row]) -> Statistics:
    powers = [(row.time, row.power) for row in rows if row.power is not None]
    temperatures = [row.temperature for row in rows if row.temperature is not None]
    pairs = [(row.temperature, row.power) for row in rows if row.paired]
    correlation = None
    reason = "有效配对少于3个"
    if len(pairs) >= 3:
        xs = [x for x, _ in pairs if x is not None]
        ys = [y for _, y in pairs if y is not None]
        # 先缩放再居中，避免有限但极大的上游值导致方差乘积溢出。
        x_scale, y_scale = max(abs(x) for x in xs), max(abs(y) for y in ys)
        xs = [x / x_scale for x in xs] if x_scale else xs
        ys = [y / y_scale for y in ys] if y_scale else ys
        xm, ym = math.fsum(xs) / len(xs), math.fsum(ys) / len(ys)
        dx, dy = [x - xm for x in xs], [y - ym for y in ys]
        xx, yy = math.fsum(x * x for x in dx), math.fsum(y * y for y in dy)
        if xx and yy:
            correlation = max(
                -1.0,
                min(
                    1.0, math.fsum(x * y for x, y in zip(dx, dy, strict=True)) / math.sqrt(xx * yy)
                ),
            )
            reason = ""
        else:
            reason = "功率或温度序列恒定"
    peak = max(powers, key=lambda item: item[1]) if powers else None
    return Statistics(
        len(rows),
        len(pairs),
        len(powers),
        len(temperatures),
        math.fsum(value / len(powers) for _, value in powers) if powers else None,
        peak[1] if peak else None,
        peak[0] if peak else None,
        min(temperatures) if temperatures else None,
        max(temperatures) if temperatures else None,
        correlation,
        reason,
    )


def finite_value(value: object) -> tuple[float | None, str]:
    if value is None or value == "":
        return None, "缺失"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "无效值"
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None, "无效值"
    return (number, "有效") if math.isfinite(number) else (None, "非有限值")


def normalize(
    query: QueryRange,
    series: Mapping[str, Sequence[Mapping[str, object]]],
    queried_at: datetime,
) -> Snapshot:
    """按时间精确配对；冲突失效，不插值，也不把缺失写成0。"""
    grid = query.grid()
    times = set(grid)
    values: dict[str, dict[datetime, tuple[float | None, str]]] = {}
    invalid = outside = off_grid = duplicates = conflicts = 0
    for spec in POINTS:
        decoded: dict[datetime, tuple[float | None, str]] = {}
        compatible = 0
        local_invalid = local_off_grid = 0
        raw = series.get(spec.point_id, ())
        for item in raw:
            try:
                stamp = item.get("ts")
                if not isinstance(stamp, str):
                    raise ValueError
                time = datetime.fromisoformat(stamp)
                time = (
                    time.replace(tzinfo=SHANGHAI)
                    if time.tzinfo is None
                    else time.astimezone(SHANGHAI)
                )
            except (TypeError, ValueError):
                invalid += 1
                local_invalid += 1
                continue
            if time < query.start or time >= query.end:
                outside += 1
                continue
            if time not in times:
                off_grid += 1
                local_off_grid += 1
                continue
            compatible += 1
            cell = finite_value(item.get("val"))
            if time in decoded:
                duplicates += 1
                if decoded[time] != cell and decoded[time][1] != "重复冲突":
                    decoded[time] = (None, "重复冲突")
                    conflicts += 1
            else:
                decoded[time] = cell
        if raw and not compatible and (local_invalid or local_off_grid):
            raise ValueError("历史时间结构与查询网格不兼容。")
        values[spec.point_id] = decoded
    rows = []
    for time in grid:
        power, ps = values[POWER.point_id].get(time, (None, "缺少记录"))
        temperature, ts = values[TEMPERATURE.point_id].get(time, (None, "缺少记录"))
        rows.append(Row(time, power, temperature, ps, ts))
    return Snapshot(
        query, queried_at, tuple(rows), Quality(invalid, outside, off_grid, duplicates, conflicts)
    )


def export_snapshot(snapshot: Snapshot) -> bytes:
    """始终导出完整快照，图表选区和当前草稿都不能改写来源。"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "time_Asia_Shanghai",
            "power_kW",
            "outdoor_temperature_C",
            "power_status",
            "temperature_status",
            "project_key",
            "project_id",
            "source",
            "query_start_inclusive",
            "query_end_exclusive",
            "queried_at",
            "interval_minutes",
            "power_device_id",
            "power_point_id",
            "temperature_device_id",
            "temperature_point_id",
        ]
    )
    for row in snapshot.rows:
        writer.writerow(
            [
                format_time(row.time),
                row.power,
                row.temperature,
                row.power_status,
                row.temperature_status,
                PROJECT_KEY,
                PROJECT_ID,
                "xiaomai_readonly",
                format_time(snapshot.query.start),
                format_time(snapshot.query.end),
                snapshot.queried_at.astimezone(SHANGHAI).isoformat(),
                2,
                POWER.device_id,
                POWER.point_id,
                TEMPERATURE.device_id,
                TEMPERATURE.point_id,
            ]
        )
    return output.getvalue().encode("utf-8-sig")
