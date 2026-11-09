"""固定八项电量的自然日统计；复用日界，缺测不摊日、不重算分母。"""

import csv
import io
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ID = 832
MAX_DAYS = 62
PREFERENCE_KEY = "axiomforge.hangxin.daily-energy-share.query.v1"
SOURCE_VERSION = "xiaomai_readonly_totalenergy_v1"
MAPPING_VERSION = "hangxin-eight-meters-2026-11-09"
PERIODS = {
    "yesterday": "昨天",
    "last7": "近7个完整日",
    "month": "本月截至昨天",
    "last_month": "上月",
    "custom": "自定义",
}
ASSUMPTION = "未观察到回退不等于证明缺口内没有清零或换表；平台公式核对不等于现场接线验证。"


@dataclass(frozen=True, slots=True)
class Meter:
    point_id: str
    device_id: int
    name: str
    unit: str = "kWh"


# 明确登记现场映射；不能从返回顺序或同名表推断计量边界。
POINTS = (
    Meter("wh171k_mspm16_totalenergy", 12738, "美的空调1电表"),
    Meter("wh171k_mspm17_totalenergy", 12739, "美的空调2电表"),
    Meter("wh171k_mspm18_totalenergy", 12740, "美的空调3电表"),
    Meter("wh171k_mspm19_totalenergy", 12741, "美的空调4电表"),
    Meter("wh171k_mspm20_totalenergy", 12742, "美的空调5电表"),
    Meter("wh171k_mspm21_totalenergy", 12743, "三菱外机电表"),
    Meter("wh171k_mspm03_totalenergy", 12744, "冷水主机1#电表"),
    Meter("wh171k_mspm04_totalenergy", 12745, "冷水主机2#电表"),
    Meter("wh171k_mspm11_totalenergy", 12733, "AHU1电表"),
    Meter("wh171k_mspm12_totalenergy", 12734, "AHU2电表"),
    Meter("wh171k_mspm13_totalenergy", 12735, "AHU3电表"),
    Meter("wh171k_mspm14_totalenergy", 12736, "AHU4电表"),
    Meter("wh171k_mspm15_totalenergy", 12737, "AHU5电表"),
)


@dataclass(frozen=True, slots=True)
class Group:
    key: str
    name: str
    platform_id: int
    point_ids: tuple[str, ...]


GROUPS = (
    Group("midea1", "美的空调1电表", 8103, ("wh171k_mspm16_totalenergy",)),
    Group("midea2", "美的空调2电表", 8104, ("wh171k_mspm17_totalenergy",)),
    Group("midea3", "美的空调3电表", 8105, ("wh171k_mspm18_totalenergy",)),
    Group("midea4", "美的空调4电表", 8106, ("wh171k_mspm19_totalenergy",)),
    Group("midea5", "美的空调5电表", 8107, ("wh171k_mspm20_totalenergy",)),
    Group("mitsubishi", "三菱外机电表", 8108, ("wh171k_mspm21_totalenergy",)),
    Group(
        "chiller", "冷水主机总表", 8123, ("wh171k_mspm03_totalenergy", "wh171k_mspm04_totalenergy")
    ),
    Group(
        "ahu",
        "AHU总表",
        8122,
        (
            "wh171k_mspm11_totalenergy",
            "wh171k_mspm12_totalenergy",
            "wh171k_mspm13_totalenergy",
            "wh171k_mspm14_totalenergy",
            "wh171k_mspm15_totalenergy",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Conditions:
    period: str = "last7"
    start_date: str = ""
    end_date: str = ""


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
    elif period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    else:
        raise ValueError("日期预设无效。")
    return start.isoformat(), end.isoformat()


def default_conditions(now: datetime | None = None) -> Conditions:
    return Conditions("last7", *preset_dates("last7", now))


@dataclass(frozen=True, slots=True)
class Query:
    conditions: Conditions
    days: tuple[date, ...]
    start: datetime
    end: datetime


def resolve_query(conditions: Conditions, now: datetime | None = None) -> Query:
    current = _now(now)
    if not isinstance(conditions, Conditions) or conditions.period not in PERIODS:
        raise ValueError("查询条件格式无效。")
    if conditions.period != "custom":
        conditions = Conditions(conditions.period, *preset_dates(conditions.period, current))
    try:
        start = date.fromisoformat(conditions.start_date)
        end = date.fromisoformat(conditions.end_date)
    except (TypeError, ValueError):
        raise ValueError("日期格式应为YYYY-MM-DD。") from None
    if start.isoformat() != conditions.start_date or end.isoformat() != conditions.end_date:
        raise ValueError("日期格式应为YYYY-MM-DD。")
    count = (end - start).days + 1
    if not 1 <= count <= MAX_DAYS:
        raise ValueError(f"起止日期需按顺序选择，最多{MAX_DAYS}个完整日。")
    if end >= current.date():
        raise ValueError("只能统计已结束的完整自然日，请选择昨天或更早日期。")
    finish = datetime.combine(end + timedelta(days=1), time(), SHANGHAI)
    if finish > current:
        raise ValueError("只能统计已结束的完整自然日，请选择昨天或更早日期。")
    return Query(
        conditions,
        tuple(start + timedelta(days=i) for i in range(count)),
        datetime.combine(start, time(), SHANGHAI),
        finish,
    )


def encode_conditions(conditions: Conditions) -> dict[str, object]:
    return {
        "version": 1,
        "project": "hangxin-airport",
        "period": conditions.period,
        "start_date": conditions.start_date,
        "end_date": conditions.end_date,
    }


def decode_conditions(value: object, now: datetime | None = None) -> Conditions:
    try:
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
            or value.get("project") != "hangxin-airport"
            or any(not isinstance(value.get(k), str) for k in ("period", "start_date", "end_date"))
        ):
            raise ValueError
        return resolve_query(
            Conditions(value["period"], value["start_date"], value["end_date"]), now
        ).conditions
    except (ValueError, TypeError, KeyError):
        return default_conditions(now)


@dataclass(frozen=True, slots=True)
class Reading:
    timestamp: datetime
    value: Decimal


@dataclass(frozen=True, slots=True)
class Issue:
    start: datetime
    end: datetime
    kind: str
    reason: str
    point_id: str


@dataclass(frozen=True, slots=True)
class Boundary:
    meter: Meter
    target: datetime
    status: str
    value: Decimal | None
    before: Reading | None = None
    after: Reading | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class MeterResult:
    meter: Meter
    start: Boundary
    end: Boundary
    kwh: Decimal | None
    reason: str
    estimated: bool
    issues: tuple[Issue, ...] = ()
    observed_count: int = 0
    missing_count: int = 0


@dataclass(frozen=True, slots=True)
class GroupResult:
    group: Group
    kwh: Decimal | None
    share: Decimal | None
    estimated: bool
    reason: str
    meters: tuple[MeterResult, ...]


@dataclass(frozen=True, slots=True)
class Aggregate:
    day: date | None
    items: tuple[GroupResult, ...]
    total: Decimal | None
    known_subtotal: Decimal
    known_count: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    query: Query
    queried_at: datetime
    daily: tuple[Aggregate, ...]
    interval: Aggregate
    complete_days: Aggregate | None
    included_days: tuple[date, ...]
    excluded_days: tuple[date, ...]
    boundaries: tuple[Boundary, ...]
    issues: tuple[Issue, ...]


@dataclass(frozen=True, slots=True)
class _Sample:
    timestamp: datetime
    value: Decimal | None
    status: str


@dataclass(frozen=True, slots=True)
class _Series:
    samples: tuple[_Sample, ...]
    times: tuple[datetime, ...]
    valid: tuple[Reading, ...]
    valid_times: tuple[datetime, ...]
    issues: tuple[Issue, ...]


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("电量历史时间格式异常。")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("电量历史时间格式异常。") from None
    return stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp.astimezone(SHANGHAI)


def _number(value: object) -> tuple[Decimal | None, str]:
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None, "invalid"
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None, "invalid"
    if not result.is_finite() or result < 0:
        return None, "invalid"
    return result, "valid"


def _parse(meter: Meter, records: Sequence[Mapping[str, object]]) -> _Series:
    values: dict[datetime, tuple[Decimal | None, str]] = {}
    for record in records:
        stamp = _timestamp(record.get("ts"))
        value, status = _number(record.get("val"))
        old = values.get(stamp)
        # 空网格与有效返回可在分片接缝重叠；不让空网格盖过真实读数。
        if old is None or old[1] == "missing":
            values[stamp] = value, status
        elif status != "missing" and old != (value, status):
            values[stamp] = None, "conflict"
    samples = tuple(_Sample(stamp, *value) for stamp, value in sorted(values.items()))
    valid = tuple(
        Reading(s.timestamp, s.value)
        for s in samples
        if s.status == "valid" and s.value is not None
    )
    issues = [
        Issue(
            s.timestamp,
            s.timestamp,
            s.status,
            "同刻累计读数冲突" if s.status == "conflict" else "非法累计读数（含负值或非有限值）",
            meter.point_id,
        )
        for s in samples
        if s.status in {"invalid", "conflict"}
    ]
    issues.extend(
        Issue(a.timestamp, b.timestamp, "rollback", "观察到累计回退／疑似重置", meter.point_id)
        for a, b in zip(valid, valid[1:], strict=False)
        if b.value < a.value
    )
    return _Series(
        samples,
        tuple(s.timestamp for s in samples),
        valid,
        tuple(r.timestamp for r in valid),
        tuple(issues),
    )


def _overlap(issue: Issue, start: datetime, end: datetime) -> bool:
    if issue.start == issue.end:
        return start <= issue.start <= end
    return issue.start < end and issue.end > start


def _boundary(meter: Meter, series: _Series, target: datetime) -> Boundary:
    index = bisect_left(series.times, target)
    if index < len(series.samples) and series.times[index] == target:
        sample = series.samples[index]
        if sample.status == "valid":
            return Boundary(meter, target, "exact", sample.value)
        if sample.status in {"conflict", "invalid"}:
            return Boundary(
                meter,
                target,
                sample.status,
                None,
                reason="午夜原始累计读数冲突或非法，不能用邻近值掩盖。",
            )
    right = bisect_left(series.valid_times, target)
    before = series.valid[right - 1] if right > 0 else None
    after = series.valid[right] if right < len(series.valid) else None
    if before is None or after is None:
        return Boundary(
            meter,
            target,
            "missing",
            None,
            before,
            after,
            "缺少包围日界的两侧有效读数，禁止单侧替代。",
        )
    gap = (after.timestamp - before.timestamp).total_seconds()
    if gap > 300:
        return Boundary(
            meter,
            target,
            "missing",
            None,
            before,
            after,
            "两侧支撑总间隔超过5分钟，不做跨缺口插值。",
        )
    if any(_overlap(issue, before.timestamp, after.timestamp) for issue in series.issues):
        return Boundary(
            meter,
            target,
            "invalid",
            None,
            before,
            after,
            "边界支撑范围内观察到非法、冲突或累计回退。",
        )
    fraction = Decimal(str((target - before.timestamp).total_seconds())) / Decimal(str(gap))
    value = before.value + (after.value - before.value) * fraction
    return Boundary(
        meter, target, "aligned", value, before, after, "包围日界且总间隔≤5分钟，线性对齐。"
    )


def _delta(meter: Meter, series: _Series, start: Boundary, end: Boundary) -> MeterResult:
    support_start = (
        start.before.timestamp if start.status == "aligned" and start.before else start.target
    )
    support_end = end.after.timestamp if end.status == "aligned" and end.after else end.target
    issues = tuple(i for i in series.issues if _overlap(i, support_start, support_end))
    left, right = bisect_left(series.times, start.target), bisect_right(series.times, end.target)
    observed = sum(s.status == "valid" for s in series.samples[left:right])
    missing = sum(s.status == "missing" for s in series.samples[left:right])
    value = None
    if start.value is None or end.value is None:
        reason = "期初：" + (start.reason or start.status) + "；期末：" + (end.reason or end.status)
    elif issues:
        reason = "；".join(dict.fromkeys(i.reason for i in issues))
    elif end.value < start.value:
        reason = "累计差为负，不能裁剪为0或自动补偿重置。"
    else:
        value = end.value - start.value
        reason = "未观察到回退；" + (
            "含短时日界对齐" if "aligned" in {start.status, end.status} else "精确日界"
        )
    return MeterResult(
        meter,
        start,
        end,
        value,
        reason,
        "aligned" in {start.status, end.status},
        issues,
        observed,
        missing,
    )


def _aggregate(day: date | None, meters: Mapping[str, MeterResult]) -> Aggregate:
    items = []
    for group in GROUPS:
        members = tuple(meters[p] for p in group.point_ids)
        kwh: Decimal | None = sum((m.kwh for m in members if m.kwh is not None), Decimal(0))
        if any(m.kwh is None for m in members):
            kwh = None
        reason = "；".join(dict.fromkeys(m.reason for m in members if m.kwh is None))
        if not reason:
            reason = "短时日界对齐" if any(m.estimated for m in members) else "精确日界"
        items.append(
            GroupResult(group, kwh, None, any(m.estimated for m in members), reason, members)
        )
    return _finish_aggregate(day, tuple(items))


def _finish_aggregate(day: date | None, items: tuple[GroupResult, ...]) -> Aggregate:
    subtotal = sum((item.kwh for item in items if item.kwh is not None), Decimal(0))
    count = sum(item.kwh is not None for item in items)
    total = subtotal if count == len(GROUPS) else None
    if total is not None and total > 0:
        items = tuple(
            replace(item, share=item.kwh / total * 100) if item.kwh is not None else item
            for item in items
        )
    return Aggregate(day, items, total, subtotal, count)


def normalize(
    query: Query, series: Mapping[str, Sequence[Mapping[str, object]]], queried_at: datetime
) -> Snapshot:
    """读取完整已观察序列，但不把内部空网格等同于用电缺失。"""
    # 同时防止手工构造Query绕过62天、日期与未来边界校验。
    checked = resolve_query(replace(query.conditions, period="custom"), queried_at)
    if (query.days, query.start, query.end) != (checked.days, checked.start, checked.end):
        raise ValueError("统计窗口与查询条件不一致。")
    parsed = {meter.point_id: _parse(meter, series.get(meter.point_id, ())) for meter in POINTS}
    targets = tuple(query.start + timedelta(days=i) for i in range(len(query.days) + 1))
    boundary_map = {
        (meter.point_id, target): _boundary(meter, parsed[meter.point_id], target)
        for meter in POINTS
        for target in targets
    }

    def result(start: datetime, end: datetime, day: date | None) -> Aggregate:
        return _aggregate(
            day,
            {
                meter.point_id: _delta(
                    meter,
                    parsed[meter.point_id],
                    boundary_map[meter.point_id, start],
                    boundary_map[meter.point_id, end],
                )
                for meter in POINTS
            },
        )

    daily = tuple(result(targets[i], targets[i + 1], day) for i, day in enumerate(query.days))
    complete = tuple(day for day in daily if day.total is not None)
    complete_sum = None
    if complete:
        complete_sum = _finish_aggregate(
            None,
            tuple(
                GroupResult(
                    group,
                    sum((day.items[index].kwh or Decimal(0) for day in complete), Decimal(0)),
                    None,
                    any(day.items[index].estimated for day in complete),
                    f"共同可计算日期合计（{len(complete)}/{len(daily)}日）",
                    (),
                )
                for index, group in enumerate(GROUPS)
            ),
        )
    return Snapshot(
        query,
        _now(queried_at),
        daily,
        result(query.start, query.end, None),
        complete_sum,
        tuple(d.day for d in complete if d.day is not None),
        tuple(d.day for d in daily if d.total is None and d.day is not None),
        tuple(boundary_map.values()),
        tuple(i for s in parsed.values() for i in s.issues),
    )


def format_number(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _csv(snapshot: Snapshot, header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    metadata = ["时区", "来源", "映射版本", "查询时刻", "业务开始", "业务结束_不含"]
    writer.writerow([*header, *metadata])
    tail = [
        "Asia/Shanghai",
        SOURCE_VERSION,
        MAPPING_VERSION,
        snapshot.queried_at.isoformat(),
        snapshot.query.start.isoformat(),
        snapshot.query.end.isoformat(),
    ]
    for row in rows:
        writer.writerow([*(_cell(v) for v in row), *tail])
    return stream.getvalue().encode("utf-8-sig")


def export_daily(snapshot: Snapshot) -> bytes:
    rows = [
        (
            day.day,
            i.group.name,
            i.group.platform_id,
            i.kwh,
            i.share,
            day.total,
            day.known_subtotal,
            i.estimated,
            i.reason,
            "完整" if day.total is not None else "八项分母不可确定",
        )
        for day in snapshot.daily
        for i in day.items
    ]
    return _csv(
        snapshot,
        [
            "日期",
            "电表",
            "平台表ID",
            "用电量_kWh",
            "占比_%",
            "八项合计_kWh",
            "已知小计_kWh",
            "边界含对齐",
            "状态原因",
            "分母状态",
        ],
        rows,
    )


def export_summary(snapshot: Snapshot) -> bytes:
    rows = [
        (
            d.day,
            d.total,
            d.known_subtotal,
            d.known_count,
            "缺测_占比不可确定"
            if d.total is None
            else "总量为0_占比无定义"
            if d.total == 0
            else "可计算",
        )
        for d in snapshot.daily
    ]
    return _csv(
        snapshot, ["日期", "八项合计_kWh", "已知小计_非八项总量_kWh", "已知项目数", "状态"], rows
    )


def export_intervals(snapshot: Snapshot) -> bytes:
    rows: list[tuple[object, ...]] = []
    for name, aggregate in (
        ("区间累计差", snapshot.interval),
        ("可计算日合计", snapshot.complete_days),
    ):
        for group in GROUPS:
            item = (
                next((i for i in aggregate.items if i.group == group), None) if aggregate else None
            )
            rows.append(
                (
                    name,
                    group.name,
                    item.kwh if item else None,
                    item.share if item else None,
                    aggregate.total if aggregate else None,
                    aggregate.known_subtotal if aggregate else None,
                    len(snapshot.included_days),
                    len(snapshot.daily),
                    ";".join(d.isoformat() for d in snapshot.excluded_days),
                    item.reason if item else "没有所有八项均可计算的日期",
                )
            )
    return _csv(
        snapshot,
        [
            "口径",
            "电表",
            "用电量_kWh",
            "占比_%",
            "八项合计_kWh",
            "已知小计_kWh",
            "可计算日数",
            "请求日数",
            "排除日期",
            "状态原因",
        ],
        rows,
    )


def export_quality(snapshot: Snapshot) -> bytes:
    rows: list[tuple[object, ...]] = []
    for boundary in snapshot.boundaries:
        before, after = boundary.before, boundary.after
        rows.append(
            (
                "日界",
                boundary.meter.name,
                boundary.meter.device_id,
                boundary.meter.point_id,
                boundary.target,
                boundary.value,
                boundary.status,
                before.timestamp if before else None,
                before.value if before else None,
                after.timestamp if after else None,
                after.value if after else None,
                boundary.reason,
                None,
                None,
                None,
                None,
            )
        )
    for aggregate in (*snapshot.daily, snapshot.interval):
        for item in aggregate.items:
            for meter in item.meters:
                rows.append(
                    (
                        "自然日差" if aggregate.day else "区间差",
                        meter.meter.name,
                        meter.meter.device_id,
                        meter.meter.point_id,
                        meter.start.target,
                        None,
                        "可计算" if meter.kwh is not None else "不可计算",
                        None,
                        None,
                        None,
                        None,
                        meter.reason,
                        meter.end.target,
                        meter.kwh,
                        meter.observed_count,
                        meter.missing_count,
                    )
                )
    for issue in snapshot.issues:
        rows.append(
            (
                "观察异常",
                "",
                "",
                issue.point_id,
                issue.start,
                None,
                issue.kind,
                None,
                None,
                None,
                None,
                issue.reason,
                issue.end,
                None,
                None,
                None,
            )
        )
    return _csv(
        snapshot,
        [
            "依据类型",
            "物理表",
            "设备ID",
            "累计点ID",
            "目标或开始时刻",
            "日界读数_kWh",
            "状态",
            "前支撑时刻",
            "前支撑值_kWh",
            "后支撑时刻",
            "后支撑值_kWh",
            "原因",
            "结束时刻",
            "应用增量_kWh",
            "有效返回数",
            "空网格返回数",
        ],
        rows,
    )


def export_notes(snapshot: Snapshot) -> bytes:
    notes = (
        "八项合计只含指定八组13块物理表，不代表机场总量。",
        "北京时间自然日左闭右开；精确午夜优先，仅两侧包围总间隔≤5分钟允许线性对齐。",
        "缺测保留已知电量；任何组未知则八项总量与全部占比为空；真实0保留。",
        "区间累计差与共同可计算日合计独立；不平均每日百分比、不分摊长缺口。",
        "百分比内部按未舍入电量计算；显示两位小数可能合计99.99%或100.01%。",
        "采样空网格数不是电量缺失比例；首版不自动生成长缺口条件上下界。",
        ASSUMPTION,
    )
    return _csv(snapshot, ["口径说明"], [(note,) for note in notes])
