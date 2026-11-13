"""五台AHU的纯统计合同；每日窗口独立，缺测和未知状态不补值。"""

import csv
import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, TypeGuard

from axiomforge.power_temperature import SHANGHAI

PROJECT_ID = 832
INTERVAL = timedelta(minutes=1)
MAX_DAYS = 62
PREFERENCE_KEY = "axiomforge.hangxin.ahu-return-temperature.query.v1"
PERIODS = {"yesterday": "昨天", "last7": "近7个完整日", "month": "本月截至昨天", "custom": "自定义"}
STATE_LABELS = {
    "running": "确认开机",
    "stopped": "确认停机",
    "conflict": "状态矛盾",
    "unknown": "状态未知",
}


@dataclass(frozen=True, slots=True)
class PointSpec:
    point_id: str
    device_id: int
    unit: str
    field: str


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    id: str
    name: str
    device_id: int
    points: tuple[PointSpec, ...]


DEVICES = tuple(
    DeviceSpec(
        f"AHU{n}",
        f"芒市AHU{n}",
        12782 + n,
        tuple(
            PointSpec(f"ewcnad_msahu{n}_{suffix}", 12782 + n, unit, field)
            for suffix, unit, field in (
                ("airin", "℃", "temperature"),
                ("v20", "", "feedback"),
                ("v01", "hz", "frequency"),
            )
        ),
    )
    for n in range(1, 6)
)
POINTS = tuple(point for device in DEVICES for point in device.points)


@dataclass(frozen=True, slots=True)
class Bin:
    name: str
    lower: float
    upper: float


BINS = tuple(
    Bin(name, lower, upper)
    for name, lower, upper in (
        ("低于20℃", -math.inf, 20),
        ("20～22℃", 20, 22),
        ("22～24℃", 22, 24),
        ("24～26℃", 24, 26),
        ("26～28℃", 26, 28),
        ("28～30℃", 28, 30),
        ("30℃及以上", 30, math.inf),
    )
)


@dataclass(frozen=True, slots=True)
class QueryConditions:
    period: str = "last7"
    start: str = ""
    end: str = ""
    daily: bool = False
    start_time: str = "09:00"
    end_time: str = "18:00"

    def preference(self) -> dict[str, object]:
        return {
            "version": 1,
            "project": "hangxin-airport",
            "period": self.period,
            "start": self.start,
            "end": self.end,
            "daily": self.daily,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(SHANGHAI)
    if value.tzinfo is None:
        raise ValueError("当前时间必须包含时区。")
    return value.astimezone(SHANGHAI)


def preset_dates(period: str, now: datetime | None = None) -> tuple[str, str]:
    today = _now(now).date()
    end = today - timedelta(days=1)
    if period == "yesterday":
        start = end
    elif period == "last7":
        start = today - timedelta(days=7)
    elif period == "month":
        start = today.replace(day=1)
        if start > end:
            raise ValueError("本月尚无完整日期，请选择其他范围。")
    else:
        raise ValueError("日期预设无效。")
    return start.isoformat(), end.isoformat()


def default_conditions(now: datetime | None = None) -> QueryConditions:
    start, end = preset_dates("last7", now)
    return QueryConditions(start=start, end=end)


def _clock(value: str) -> time:
    if len(value) != 5 or value[2] != ":":
        raise ValueError("时段需精确到分钟。")
    result = time.fromisoformat(value)
    if result.tzinfo is not None or result.second or result.microsecond:
        raise ValueError("时段需使用北京时间的小时和分钟。")
    return result


def restore_conditions(value: object, now: datetime | None = None) -> QueryConditions:
    """偏好解码不请求上游；合法自定义日期即使尚未结束也保留供修改。"""
    try:
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
            or value.get("project") != "hangxin-airport"
        ):
            raise ValueError
        if (
            any(
                not isinstance(value.get(k), str)
                for k in ("period", "start", "end", "start_time", "end_time")
            )
            or type(value.get("daily")) is not bool
        ):
            raise ValueError
        conditions = QueryConditions(
            **{k: value[k] for k in ("period", "start", "end", "daily", "start_time", "end_time")}
        )
        _validate_conditions(conditions)
        if conditions.period != "custom":
            start, end = preset_dates(conditions.period, now)
            conditions = QueryConditions(
                conditions.period,
                start,
                end,
                conditions.daily,
                conditions.start_time,
                conditions.end_time,
            )
        return conditions
    except (ValueError, TypeError, KeyError):
        return default_conditions(now)


def _validate_conditions(conditions: QueryConditions) -> None:
    if conditions.period not in PERIODS or type(conditions.daily) is not bool:
        raise ValueError("查询条件格式无效。")
    start, end = date.fromisoformat(conditions.start), date.fromisoformat(conditions.end)
    if start.isoformat() != conditions.start or end.isoformat() != conditions.end:
        raise ValueError("日期格式应为YYYY-MM-DD。")
    if not 1 <= (end - start).days + 1 <= MAX_DAYS:
        raise ValueError(f"起止日期需按顺序选择，最多{MAX_DAYS}个开始日期。")
    # 关闭每日时段后仍保留上次时刻，但它们不影响本次窗口。
    if conditions.daily and _clock(conditions.start_time) == _clock(conditions.end_time):
        raise ValueError("起止时刻不能相同；全天请关闭每日固定时段。")


@dataclass(frozen=True, slots=True)
class Window:
    day: date
    start: datetime
    end: datetime

    @property
    def expected(self) -> int:
        return int((self.end - self.start) / INTERVAL)

    def grid(self) -> tuple[datetime, ...]:
        return tuple(self.start + i * INTERVAL for i in range(self.expected))


@dataclass(frozen=True, slots=True)
class Query:
    conditions: QueryConditions
    windows: tuple[Window, ...]

    @property
    def start(self) -> datetime:
        return self.windows[0].start

    @property
    def end(self) -> datetime:
        return self.windows[-1].end

    @property
    def expected(self) -> int:
        return sum(window.expected for window in self.windows)

    @property
    def summary(self) -> str:
        c = self.conditions
        slot = (
            f"每天{c.start_time}—{'次日' if c.end_time < c.start_time else ''}{c.end_time}"
            if c.daily
            else "全天"
        )
        return (
            f"{c.start}—{c.end} · {slot} · "
            f"{len(self.windows)}个窗口／{self.expected / 60:g}小时 · "
            f"最后结束{self.end:%Y-%m-%d %H:%M} · 北京时间"
        )


def resolve_query(conditions: QueryConditions, now: datetime | None = None) -> Query:
    if conditions.period != "custom":
        start, end = preset_dates(conditions.period, now)
        conditions = QueryConditions(
            conditions.period,
            start,
            end,
            conditions.daily,
            conditions.start_time,
            conditions.end_time,
        )
    _validate_conditions(conditions)
    first_day, last_day = date.fromisoformat(conditions.start), date.fromisoformat(conditions.end)
    windows = []
    for i in range((last_day - first_day).days + 1):
        day = first_day + timedelta(days=i)
        first = datetime.combine(
            day, _clock(conditions.start_time) if conditions.daily else time(), SHANGHAI
        )
        last = datetime.combine(
            day, _clock(conditions.end_time) if conditions.daily else time(), SHANGHAI
        )
        if last <= first:
            try:
                last += timedelta(days=1)
            except OverflowError:
                # 日期控件可输入9999年；把溢出作为可显示的校验错误。
                raise ValueError("统计结束超出支持日期范围，请调整结束日期。") from None
        windows.append(Window(day, first, last))
    query = Query(conditions, tuple(windows))
    if query.end > _now(now):
        raise ValueError("最后统计窗口尚未结束，请调整日期或每日时段。")
    return query


@dataclass(frozen=True, slots=True)
class Sample:
    time: datetime
    day: date
    temperature: float | None
    feedback: float | None
    frequency: float | None
    state: str
    temperature_quality: str = "valid"
    feedback_quality: str = "valid"
    frequency_quality: str = "valid"


@dataclass(frozen=True, slots=True)
class Statistics:
    count: int
    minimum: float | None
    maximum: float | None
    p05: float | None
    p95: float | None
    mean: float | None
    bins: tuple[int, ...]
    minimum_time: datetime | None = None
    maximum_time: datetime | None = None
    minimum_count: int = 0
    maximum_count: int = 0


def percentile(values: Sequence[float], p: float) -> float | None:
    if not 0 <= p <= 1:
        raise ValueError("分位数范围无效。")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def statistics(rows: Sequence[Sample], running: bool = False) -> Statistics:
    selected = [
        row
        for row in rows
        if row.temperature is not None and (not running or row.state == "running")
    ]
    values = [row.temperature for row in selected if row.temperature is not None]
    if not values:
        return Statistics(0, None, None, None, None, None, (0,) * len(BINS))
    low, high = min(values), max(values)
    return Statistics(
        len(values),
        low,
        high,
        percentile(values, 0.05),
        percentile(values, 0.95),
        math.fsum(values) / len(values),
        tuple(sum(b.lower <= v < b.upper for v in values) for b in BINS),
        next(row.time for row in selected if row.temperature == low),
        next(row.time for row in selected if row.temperature == high),
        values.count(low),
        values.count(high),
    )


@dataclass(frozen=True, slots=True)
class Gap:
    day: date
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class Quality:
    expected: int
    temperature_valid: int
    status_valid: int
    unconfirmed: int
    conflicts: int
    duplicates: int
    outside: int
    invalid_times: int
    off_grid: int
    first_valid: datetime | None
    last_valid: datetime | None
    no_data_days: tuple[date, ...]
    gaps: tuple[Gap, ...]
    state_conflicts: int = 0


@dataclass(frozen=True, slots=True)
class DailyResult:
    window: Window
    all_stats: Statistics
    running_stats: Statistics


@dataclass(frozen=True, slots=True)
class DeviceResult:
    spec: DeviceSpec
    rows: tuple[Sample, ...]
    all_stats: Statistics
    running_stats: Statistics
    daily: tuple[DailyResult, ...]
    quality: Quality


@dataclass(frozen=True, slots=True)
class Snapshot:
    query: Query
    queried_at: datetime
    devices: tuple[DeviceResult, ...]


def state_for(feedback: float | None, frequency: float | None) -> str:
    if (
        not _finite(feedback)
        or not _finite(frequency)
        or feedback not in (0, 1)
        or not 0 <= frequency <= 60
    ):
        return "unknown"
    if feedback == 1 and frequency > 0:
        return "running"
    if feedback == 0 and frequency == 0:
        return "stopped"
    return "conflict"


def _finite(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_series(
    records: Sequence[Mapping[str, object]], query: Query
) -> tuple[dict[datetime, tuple[float | None, str]], dict[str, int]]:
    """按查询网格规范化单个测点，保留缺测、无效和冲突的质量状态。"""
    result: dict[datetime, tuple[float | None, str]] = {}
    counts = dict.fromkeys(("duplicates", "conflicts", "outside", "invalid_times", "off_grid"), 0)
    compatible = 0
    for record in records:
        stamp = record.get("ts")
        try:
            if not isinstance(stamp, str):
                raise ValueError
            parsed = datetime.fromisoformat(stamp)
            parsed = (
                parsed.replace(tzinfo=SHANGHAI)
                if parsed.tzinfo is None
                else parsed.astimezone(SHANGHAI)
            )
        except (ValueError, TypeError):
            counts["invalid_times"] += 1
            continue
        if not query.start <= parsed < query.end:
            counts["outside"] += 1
            continue
        window = next(
            (window for window in query.windows if window.start <= parsed < window.end), None
        )
        if window is None:
            counts["outside"] += 1
            continue
        if (parsed - window.start) % INTERVAL:
            counts["off_grid"] += 1
            continue
        compatible += 1
        value = record.get("val")
        field = (
            (float(value), "valid")
            if _finite(value)
            else (None, "missing" if value is None else "invalid")
        )
        if parsed in result:
            if result[parsed] == field:
                counts["duplicates"] += 1
            elif result[parsed][1] != "conflict":
                result[parsed] = (None, "conflict")
                counts["conflicts"] += 1
        else:
            result[parsed] = field
    if records and not compatible and (counts["invalid_times"] or counts["off_grid"]):
        raise ValueError("历史时间结构与请求窗口不兼容。")
    return result, counts


def _device_result(
    spec: DeviceSpec, rows: tuple[Sample, ...], query: Query, counters: Mapping[str, int]
) -> DeviceResult:
    daily, gaps = [], []
    first, last = None, None
    for window in query.windows:
        day_rows = tuple(row for row in rows if row.day == window.day)
        daily.append(DailyResult(window, statistics(day_rows), statistics(day_rows, True)))
        gap_start = None
        for row in day_rows:
            if row.temperature is None:
                if gap_start is None:
                    gap_start = row.time
            else:
                if first is None:
                    first = row.time
                last = row.time
                if gap_start is not None:
                    gaps.append(Gap(window.day, gap_start, row.time))
                    gap_start = None
        if gap_start is not None:
            gaps.append(Gap(window.day, gap_start, window.end))
    quality = Quality(
        query.expected,
        sum(row.temperature is not None for row in rows),
        sum(
            row.feedback in (0, 1) and row.frequency is not None and 0 <= row.frequency <= 60
            for row in rows
        ),
        sum(row.temperature is not None and row.state in ("unknown", "conflict") for row in rows),
        counters.get("conflicts", 0),
        counters.get("duplicates", 0),
        counters.get("outside", 0),
        counters.get("invalid_times", 0),
        counters.get("off_grid", 0),
        first,
        last,
        tuple(day.window.day for day in daily if not day.all_stats.count),
        tuple(gaps),
        sum(row.state == "conflict" for row in rows),
    )
    return DeviceResult(spec, rows, statistics(rows), statistics(rows, True), tuple(daily), quality)


def normalize(
    query: Query, series: Mapping[str, Sequence[Mapping[str, object]]], queried_at: datetime
) -> Snapshot:
    """生成完整选中网格；缺失窗口之外的时间不进入分母。"""
    devices = []
    for spec in DEVICES:
        fields, counters = (
            {},
            dict.fromkeys(("duplicates", "conflicts", "outside", "invalid_times", "off_grid"), 0),
        )
        for point in spec.points:
            values, counts = normalize_series(series.get(point.point_id, ()), query)
            fields[point.field] = values
            for key, value in counts.items():
                counters[key] += value
        rows = []
        for window in query.windows:
            for stamp in window.grid():
                temperature, tq = fields["temperature"].get(stamp, (None, "missing"))
                feedback, rq = fields["feedback"].get(stamp, (None, "missing"))
                frequency, fq = fields["frequency"].get(stamp, (None, "missing"))
                rows.append(
                    Sample(
                        stamp,
                        window.day,
                        temperature,
                        feedback,
                        frequency,
                        state_for(feedback, frequency),
                        tq,
                        rq,
                        fq,
                    )
                )
        devices.append(_device_result(spec, tuple(rows), query, counters))
    return Snapshot(query, queried_at, tuple(devices))


def combine_snapshots(query: Query, parts: Sequence[Snapshot], queried_at: datetime) -> Snapshot:
    """分片只保留归一化样本，避免跨月查询积累全部原始JSON。"""
    results = []
    for index, spec in enumerate(DEVICES):
        rows = tuple(row for part in parts for row in part.devices[index].rows)
        counters = {
            key: sum(getattr(part.devices[index].quality, key) for part in parts)
            for key in ("duplicates", "conflicts", "outside", "invalid_times", "off_grid")
        }
        results.append(_device_result(spec, rows, query, counters))
    return Snapshot(query, queried_at, tuple(results))


def _metadata(snapshot: Snapshot) -> dict[str, object]:
    c = snapshot.query.conditions
    return {
        "项目": "航信机场/832",
        "开始日期": c.start,
        "结束日期": c.end,
        "每日窗口": f"{c.start_time}—{'次日' if c.end_time < c.start_time else ''}{c.end_time}"
        if c.daily
        else "全天",
        "实际开始": snapshot.query.start.isoformat(),
        "实际结束不含": snapshot.query.end.isoformat(),
        "查询时间": snapshot.queried_at.isoformat(),
        "时区": "Asia/Shanghai",
        "查询网格分钟": 1,
        "来源": "xiaomai_readonly",
        "开机定义": "同刻反馈=1且0<频率≤60Hz",
        "分位数算法": "linear_(n-1)*p_v1",
        "比例口径": "有效采样占比，非精确时长",
    }


def _stats_columns(stats: Statistics, prefix: str) -> dict[str, object]:
    return {
        f"{prefix}样本数": stats.count,
        f"{prefix}最低℃": stats.minimum,
        f"{prefix}最高℃": stats.maximum,
        f"{prefix}P05℃": stats.p05,
        f"{prefix}P95℃": stats.p95,
        f"{prefix}平均℃": stats.mean,
        f"{prefix}最低首次时间": stats.minimum_time.isoformat() if stats.minimum_time else "",
        f"{prefix}最高首次时间": stats.maximum_time.isoformat() if stats.maximum_time else "",
    }


def export_snapshot(snapshot: Snapshot, kind: str = "summary") -> bytes:
    metadata = _metadata(snapshot)
    records: list[dict[str, Any]] = []
    for device in snapshot.devices:
        identity = {
            "设备": device.spec.id,
            "设备ID": device.spec.device_id,
            "回风测点": device.spec.points[0].point_id,
            "运行反馈测点": device.spec.points[1].point_id,
            "频率测点": device.spec.points[2].point_id,
        }
        if kind == "summary":
            records.append(
                {
                    **metadata,
                    **identity,
                    **_stats_columns(device.all_stats, "不限启停"),
                    **_stats_columns(device.running_stats, "开机"),
                }
            )
        elif kind == "daily":
            for day in device.daily:
                records.append(
                    {
                        **metadata,
                        **identity,
                        "归属日期": day.window.day.isoformat(),
                        "窗口开始": day.window.start.isoformat(),
                        "窗口结束不含": day.window.end.isoformat(),
                        **_stats_columns(day.all_stats, "不限启停"),
                        **_stats_columns(day.running_stats, "开机"),
                    }
                )
        elif kind == "distribution":
            for label, stats in (
                ("不限启停", device.all_stats),
                ("确认开机", device.running_stats),
            ):
                for bin_, count in zip(BINS, stats.bins, strict=True):
                    records.append(
                        {
                            **metadata,
                            **identity,
                            "运行口径": label,
                            "温区左闭右开": bin_.name,
                            "样本数": count,
                            "有效样本分母": stats.count,
                            "有效采样占比": count / stats.count if stats.count else None,
                        }
                    )
        elif kind == "quality":
            q = device.quality
            records.append(
                {
                    **metadata,
                    **identity,
                    "期望网格数": q.expected,
                    "温度有效数": q.temperature_valid,
                    "温度有效率": q.temperature_valid / q.expected if q.expected else None,
                    "状态字段共同有效数": q.status_valid,
                    "温度有效但状态未确认数": q.unconfirmed,
                    "状态矛盾数": q.state_conflicts,
                    "冲突时间戳字段数": q.conflicts,
                    "重复数": q.duplicates,
                    "窗口外记录数": q.outside,
                    "非法时间数": q.invalid_times,
                    "非网格时间数": q.off_grid,
                    "最早有效温度时间": q.first_valid.isoformat() if q.first_valid else "",
                    "最晚有效温度时间": q.last_valid.isoformat() if q.last_valid else "",
                    "无数据日期": ";".join(day.isoformat() for day in q.no_data_days),
                    "连续缺测区间": ";".join(
                        f"{gap.start.isoformat()}至{gap.end.isoformat()}(结束不含)"
                        for gap in q.gaps
                    ),
                }
            )
        else:
            raise ValueError("不支持的导出类型。")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(records[0]))
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue().encode("utf-8-sig")


def export_notes(snapshot: Snapshot) -> bytes:
    lines = [f"{key}：{value}" for key, value in _metadata(snapshot).items()]
    lines.extend(
        (
            "每日窗口按开始日期归属，末日跨午夜窗口允许延伸次日；窗口左闭右开。",
            "仅使用有限数值；不补零、不插值、不前向填充、不自动剔除温度极值。",
            "主要范围是P05～P95，不是设定范围；平均是样本平均，温区比例不是精确时长。",
            "缺测仅针对选中窗口，不跨每日未选时段拼接；空网格不直接诊断现场故障。",
        )
    )
    return ("\n".join(lines) + "\n").encode("utf-8-sig")
