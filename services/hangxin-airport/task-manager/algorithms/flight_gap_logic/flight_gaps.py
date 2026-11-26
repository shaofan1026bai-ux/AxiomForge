"""航班字段与本场事件的统一解释；不生成设备控制指令。"""

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
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


VERSION = "1.0.0"


def main(argv: list[str] | None = None) -> int:
    """读取原始航班JSON，按当前页面同一口径输出各区域空档。"""
    parser = argparse.ArgumentParser(
        description="芒市LUM航班空档判断：离线JSON输入，不连接数据库。"
    )
    parser.add_argument("input", type=Path, help="原始flight记录的JSON数组；含相邻计划日")
    parser.add_argument("--date", required=True, help="业务计划日，YYYY-MM-DD")
    parser.add_argument(
        "--mode",
        choices=("planned", "dynamic"),
        default="planned",
        help="planned=计划；dynamic=实际优先，其次预计，再用计划",
    )
    parser.add_argument(
        "--area",
        choices=("each", *FLIGHT_AREAS),
        default="each",
        help="each=同时输出全场和四区域；默认each",
    )
    parser.add_argument(
        "--minimum-minutes", type=float, default=30, help="最短间隔分钟，1到1440，默认30"
    )
    parser.add_argument("--output", type=Path, help="可选JSON输出文件；省略时输出到终端")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    try:
        day = date.fromisoformat(args.date)
        if not math.isfinite(args.minimum_minutes) or not 1 <= args.minimum_minutes <= 1440:
            raise ValueError("minimum-minutes必须在1到1440之间")
        rows = json.loads(args.input.read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("输入必须为原始flight记录的JSON数组，不能使用页面快照或SDK响应外壳")
        snapshot = normalize_flights(rows, datetime.now(SHANGHAI))
        selected = [f for f in snapshot.flights if f.day == day]
        areas = FLIGHT_AREAS if args.area == "each" else {args.area: FLIGHT_AREAS[args.area]}
        warnings = list(snapshot.warnings)
        if any(f.region == "unknown" for f in snapshot.flights):
            warnings.append("存在国内／国际属性未知的航班，同方向区域将保守地把它们作为阻断项。")
        if any(
            f.planned is None and f.estimated is None and f.actual is None for f in snapshot.flights
        ):
            warnings.append("部分航班没有可用本场时刻，不能据此确认空档完整性。")
        result: dict[str, Any] = {
            "version": VERSION,
            "airport": AIRPORT,
            "timezone": "Asia/Shanghai",
            "date": day.isoformat(),
            "mode": args.mode,
            "minimum_minutes": args.minimum_minutes,
            "computed_at": snapshot.fetched_at.isoformat(),
            "raw_count": snapshot.raw_count,
            "selected_day_flights": len(selected),
            "input_plan_days": sorted({f.day.isoformat() for f in snapshot.flights}),
            "normalization_counts": dict(snapshot.counts),
            "warnings": warnings,
            "limitations": [
                "只报告输入中已知相邻航班之间的间隔，不保证源数据完整。",
                "输入应包含所选日及相邻计划日，不要使用LIMIT或列表搜索过滤后的子集。",
                "fcategory的0／1为当前LUM样本映射，正式枚举字典待确认。",
                "尚未扣除保障、准备或预冷时间，结果不是可停机时段或设备控制指令。",
            ],
            "areas": {},
        }

        def endpoint(f: Flight) -> dict[str, Any]:
            return {
                "flight_number": f.number,
                "direction": f.direction,
                "region": f.region,
                "plan_date": f.day.isoformat(),
                "route": f.route,
            }

        for area, label in areas.items():
            gaps = flight_gaps(snapshot.flights, day, args.mode, args.minimum_minutes, area=area)
            result["areas"][area] = {
                "label": label,
                "selected_day_flights": len(flights_for_area(selected, area)),
                "gap_count": len(gaps),
                "gaps": [
                    {
                        "start": g.start.isoformat(),
                        "end": g.end.isoformat(),
                        "duration_minutes": round(g.minutes, 3),
                        "before": [endpoint(f) for f in g.before],
                        "after": [endpoint(f) for f in g.after],
                    }
                    for g in gaps
                ],
            }
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
    except (OSError, ValueError) as exc:
        print(f"输入或输出错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
