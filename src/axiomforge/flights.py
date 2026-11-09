"""航班字段与本场事件的统一解释；不生成设备控制指令。"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
AIRPORT = "LUM"
FLIGHT_AREAS = {
    "all": "全场合并",
    "domestic:I": "国内进港",
    "domestic:O": "国内出港",
    "international:I": "国际进港",
    "international:O": "国际出港",
}
REGION_LABELS = {"domestic": "国内", "international": "国际", "unknown": "待确认"}


def text(value: Any) -> str:
    """缺失值保持缺失，不把 None 当作业务文本。"""
    return "" if value is None else str(value).strip()


def local_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # 源库无偏移量的业务时刻为北京时间；带偏移量的值先转换。
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


@dataclass(frozen=True)
class Flight:
    key: str
    number: str
    day: date
    direction: str
    origin: str
    destination: str
    planned: datetime | None
    estimated: datetime | None
    actual: datetime | None
    status: str
    gate: str
    aliases: tuple[str, ...] = ()
    region: str = "unknown"

    def event_time(self, mode: str) -> datetime | None:
        return self.planned if mode == "planned" else self.actual or self.estimated or self.planned

    @property
    def route(self) -> str:
        return f"{self.origin} → {self.destination}"


@dataclass(frozen=True)
class FlightSnapshot:
    flights: tuple[Flight, ...]
    fetched_at: datetime
    raw_count: int
    counts: Mapping[str, int]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class FlightGap:
    start: datetime
    end: datetime
    before: tuple[Flight, ...]
    after: tuple[Flight, ...]

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def normalize_flights(rows: Sequence[Mapping[str, Any]], fetched_at: datetime) -> FlightSnapshot:
    """先取最新版本，再解释删除和共享号，避免旧版本或别名制造虚假空档。"""
    latest: dict[tuple[str, ...], Mapping[str, Any]] = {}
    counts: Counter[str] = Counter()
    warnings: set[str] = set()
    for row in rows:
        direction = text(row.get("arrDepFlag"))
        airport_field = "destAirptCd" if direction == "I" else "origAirptCd"
        if direction not in {"I", "O"} or text(row.get(airport_field)) != AIRPORT:
            counts["scope_rejected"] += 1
            warnings.add("存在方向或本场归属不符的记录，已排除。")
            continue
        key = tuple(
            text(row.get(k))
            for k in ("planDate", "flightNum", "arrDepFlag", "origAirptCd", "destAirptCd")
        )
        if not key[0] or not key[1]:
            warnings.add("存在计划日或航班号缺失记录，不能据此确认空档完整性。")
            continue
        old = latest.get(key)
        floor = datetime.min.replace(tzinfo=SHANGHAI)
        rank = (local_time(row.get("regulatoryPlatformSysDataTime")) or floor, text(row.get("id")))
        old_rank = (
            (local_time(old.get("regulatoryPlatformSysDataTime")) or floor, text(old.get("id")))
            if old
            else None
        )
        if old_rank is None or rank > old_rank:
            latest[key] = row
    counts["old_versions"] = len(rows) - counts["scope_rejected"] - len(latest)
    aliases: dict[tuple[str, ...], list[str]] = {}
    shared_keys: set[tuple[str, ...]] = set()
    for key, row in latest.items():
        if text(row.get("dataOperation")).upper() == "DELETE" or "非营运" in {
            text(row.get("fltStat")),
            text(row.get("fltStatChnNm")),
        }:
            continue
        main = text(row.get("fltShrInd"))
        if main and main != key[1]:
            target = (key[0], main, *key[2:])
            if target in latest and not text(latest[target].get("fltShrInd")):
                aliases.setdefault(target, []).append(key[1])
                shared_keys.add(key)
            else:
                warnings.add("存在未匹配主号的共享记录，暂保留展示；间隔需复核。")
    counts["shared_aliases"] = len(shared_keys)
    flights: list[Flight] = []
    for key, row in latest.items():
        if key in shared_keys:
            continue
        if text(row.get("dataOperation")).upper() == "DELETE":
            counts["deleted"] += 1
            continue
        raw_status = text(row.get("fltStat"))
        label = text(row.get("fltStatChnNm"))
        if "非营运" in {raw_status, label}:
            counts["non_operating"] += 1
            continue
        day = local_time(row.get("planDate"))
        if day is None:
            warnings.add("存在无法解析的计划日，已排除。")
            continue
        incoming = key[2] == "I"
        suffix = "ArrvDt" if incoming else "DptDt"
        planned = local_time(row.get("schd" + suffix))
        estimated = local_time(row.get("estmt" + suffix))
        actual = local_time(row.get("arrvDt" if incoming else "dptDt"))
        if planned is None:
            warnings.add("部分航班缺少本场计划时刻，计划间隔不能确认完整。")
        if actual:
            status = "已到达芒市" if incoming else "已从芒市起飞"
        elif incoming and "起飞" in {raw_status, label}:
            status = "已从始发站起飞"
        elif "取消" in {raw_status, label}:
            status = "取消（待复核）"
        elif label in {"催促登机", "正在登机", "登机", "延误"}:
            status = label
        else:
            status = "计划" if raw_status in {"", "计划"} else "状态待复核"
        if raw_status != label:
            counts["status_conflicts"] += 1
        flights.append(
            Flight(
                key="|".join(key),
                number=key[1],
                day=day.date(),
                direction=key[2],
                origin=text(row.get("depCtyChnNm")) or key[3],
                destination=text(row.get("arrvCtyChnNm")) or key[4],
                planned=planned,
                estimated=estimated,
                actual=actual,
                status=status,
                gate=text(row.get("gate")) if not incoming else "",
                aliases=tuple(sorted(aliases.get(key, []))),
                # 仅使用当前LUM样本核对的枚举，不从城市名或I/O猜测区域。
                region={"0": "domestic", "1": "international"}.get(
                    text(row.get("fcategory")), "unknown"
                ),
            )
        )
    flights.sort(key=lambda f: (f.planned or datetime.max.replace(tzinfo=SHANGHAI), f.number))
    return FlightSnapshot(
        tuple(flights), fetched_at, len(rows), dict(counts), tuple(sorted(warnings))
    )


def flights_for_area(
    flights: Sequence[Flight], area: str, *, include_unknown: bool = False
) -> list[Flight]:
    """未知属性可作同方向区域的保守阻断项，但不能标记为国内或国际。"""
    if area not in FLIGHT_AREAS:
        raise ValueError("不支持的航班区域")
    if area == "all":
        return list(flights)
    region, direction = area.split(":")
    return [
        f
        for f in flights
        if f.direction == direction
        and (f.region == region or (include_unknown and f.region == "unknown"))
    ]


def flight_gaps(
    flights: Sequence[Flight], day: date, mode: str, minimum: float, *, area: str = "all"
) -> list[FlightGap]:
    """邻日也参与分割；同刻航班分组，不外推无数据的首末边界。"""
    groups: dict[datetime, list[Flight]] = {}
    for flight in flights_for_area(flights, area, include_unknown=True):
        moment = flight.event_time(mode)
        if moment is not None:
            groups.setdefault(moment, []).append(flight)
    times = sorted(groups)
    gaps: list[FlightGap] = []
    for start, end in zip(times, times[1:], strict=False):
        before, after = groups[start], groups[end]
        if not any(f.day == day for f in before + after):
            continue
        gap = FlightGap(start, end, tuple(before), tuple(after))
        if gap.minutes >= minimum:
            gaps.append(gap)
    return sorted(gaps, key=lambda g: (-g.minutes, g.start))


def format_moment(moment: datetime | None, day: date) -> str:
    if moment is None:
        return "—"
    delta = (moment.date() - day).days
    prefix = "次日 " if delta == 1 else "前日 " if delta == -1 else ""
    if abs(delta) > 1:
        prefix = moment.strftime("%m-%d ")
    return prefix + moment.strftime("%H:%M")
