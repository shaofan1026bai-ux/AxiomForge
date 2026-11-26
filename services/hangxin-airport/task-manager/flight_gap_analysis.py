"""对真实航班做只读空档分析，20点候选只是解释结果，不构成关机许可。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import math
from typing import Any

from algorithms.flight_gap_logic.flight_gaps import (
    AIRPORT, FLIGHT_AREAS, SHANGHAI, Flight, FlightGap,
    flight_gaps, flights_for_area, local_time, normalize_flights, text,
)


LIMITATIONS = [
    "只报告输入中已知相邻航班之间的间隔，不保证上游查询完整或来源数据及时。",
    "输入应包含所选日及相邻计划日的完整航班，不能使用搜索过滤或LIMIT截断后的子集。",
    "fcategory的0／1沿用当前LUM样本映射，未知属性作为同方向区域的保守阻断项。",
    "尚未扣除到港保障、出港准备、预冷等时间；候选不是可停机时段或设备控制许可。",
    "20点为晚间关机档口，后续航班仅作信息，不自动推迟关机或要求重启。",
    "泵沿用现场已有延时；本次未计算余冷能力，不据此新增或改变泵延时。",
    "首班之前、末班之后或空数据不会被推断为可停机空档。",
]


def _endpoint(flight: Flight, mode: str) -> dict[str, Any]:
    """完整保留前后事件的方向、计划日和各类时刻，避免跨日展示丢失日期。"""
    moment = flight.event_time(mode)
    return {
        "flight_number": flight.number,
        "direction": flight.direction,
        "region": flight.region,
        "plan_date": flight.day.isoformat(),
        "route": flight.route,
        "status": flight.status,
        "aliases": list(flight.aliases),
        "event_at": moment.isoformat() if moment is not None else None,
        "planned_at": flight.planned.isoformat() if flight.planned is not None else None,
        "estimated_at": flight.estimated.isoformat() if flight.estimated is not None else None,
        "actual_at": flight.actual.isoformat() if flight.actual is not None else None,
    }


def _gap(gap: FlightGap, mode: str) -> dict[str, Any]:
    """间隔端点沿用原算法，输出使用标准JSON可表示的类型。"""
    return {
        "start": gap.start.isoformat(),
        "end": gap.end.isoformat(),
        "duration_minutes": round(gap.minutes, 3),
        "before": [_endpoint(flight, mode) for flight in gap.before],
        "after": [_endpoint(flight, mode) for flight in gap.after],
    }


def _inactive(row: Mapping[str, Any]) -> bool:
    """质量检查使用与原脚本相同的删除和非营运口径，不额外解释取消状态。"""
    return text(row.get("dataOperation")).upper() == "DELETE" or "非营运" in {
        text(row.get("fltStat")), text(row.get("fltStatChnNm")),
    }


def _areas_for_row(row: Mapping[str, Any]) -> set[str]:
    """未知区域关联同方向两类，不能被默认为某一个区域。"""
    direction = text(row.get("arrDepFlag"))
    region = {"0": "domestic", "1": "international"}.get(text(row.get("fcategory")))
    if region is not None:
        return {"all", f"{region}:{direction}"}
    return {"all", f"domestic:{direction}", f"international:{direction}"}


def _shared_quality_holds(rows: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """额外识别有效共享号被失效主号吞并的情况，保留原始计算但取消分析候选。"""
    latest: dict[tuple[str, ...], Mapping[str, Any]] = {}
    floor = datetime.min.replace(tzinfo=SHANGHAI)
    for row in rows:
        direction = text(row.get("arrDepFlag"))
        airport_key = "destAirptCd" if direction == "I" else "origAirptCd"
        if direction not in {"I", "O"} or text(row.get(airport_key)) != AIRPORT:
            continue
        key = tuple(text(row.get(name)) for name in (
            "planDate", "flightNum", "arrDepFlag", "origAirptCd", "destAirptCd",
        ))
        if not key[0] or not key[1]:
            continue
        previous = latest.get(key)
        rank = (local_time(row.get("regulatoryPlatformSysDataTime")) or floor, text(row.get("id")))
        old_rank = None if previous is None else (
            local_time(previous.get("regulatoryPlatformSysDataTime")) or floor,
            text(previous.get("id")),
        )
        if old_rank is None or rank > old_rank:
            latest[key] = row
    holds = {area: set() for area in FLIGHT_AREAS}
    for key, row in latest.items():
        main = text(row.get("fltShrInd"))
        if _inactive(row) or not main or main == key[1]:
            continue
        main_row = latest.get((key[0], main, *key[2:]))
        if main_row is None or text(main_row.get("fltShrInd")) or not _inactive(main_row):
            continue
        for area in _areas_for_row(row) | _areas_for_row(main_row):
            holds[area].add("active_shared_flight_with_inactive_main")
    return holds


def _assessment(
    flights: Sequence[Flight], day: date, mode: str, area: str,
    minimum: float, target: datetime, flags: set[str],
) -> dict[str, Any]:
    """只比较选定日的20点，计算时间早晚不会改变判定目标。"""
    relevant = flights_for_area(flights, area, include_unknown=True)
    if any(flight.event_time(mode) is None for flight in relevant):
        flags.add("missing_event_time")
    if not any(flight.day == day for flight in relevant):
        flags.add("no_selected_day_flights")
    moments: dict[datetime, list[Flight]] = {}
    for flight in relevant:
        moment = flight.event_time(mode)
        if moment is not None:
            moments.setdefault(moment, []).append(flight)
    # 低于阈值的相邻事件也用于解释剩余时间，但不会升级为合格空档。
    intervals = flight_gaps(flights, day, mode, 0, area=area)
    enclosing = next((gap for gap in intervals if gap.start < target < gap.end), None)
    next_moment = min((moment for moment in moments if moment > target), default=None)
    later_today = sum(
        len(group) for moment, group in moments.items()
        if moment > target and moment.date() == day
    )
    at_target = moments.get(target, [])
    if at_target:
        status = "event_at_target"
    elif enclosing is not None and enclosing.minutes >= minimum:
        status = "inside_gap"
    elif flags:
        status = "insufficient_data"
    else:
        status = "no_qualifying_gap"
    remaining = (enclosing.end - target).total_seconds() / 60 if enclosing is not None else None
    notices = []
    if remaining is not None and remaining < minimum:
        notices.append("20点后剩余间隔短于最小空档阈值；总空档满足阈值时仍按测试口径标记候选。")
    if later_today:
        notices.append("20点为晚间关机档口，后续航班仅作信息，不自动推迟关机或要求重启；泵沿用现场已有延时，本次未计算余冷能力。")
    candidate = target.isoformat() if status == "inside_gap" and not flags else None
    return {
        "target_at": target.isoformat(),
        "status": status,
        "candidate_shutdown_at": candidate,
        "shutdown_policy": "evening_cutoff",
        "later_flights_block_shutdown": False,
        "pump_afterrun": "existing_site_delay",
        "thermal_capacity_evaluated": False,
        "quality_hold": bool(flags),
        "quality_flags": sorted(flags),
        "gap_start": enclosing.start.isoformat() if enclosing is not None else None,
        "gap_end": enclosing.end.isoformat() if enclosing is not None else None,
        "gap_duration_minutes": round(enclosing.minutes, 3) if enclosing is not None else None,
        "remaining_gap_minutes": round(remaining, 3) if remaining is not None else None,
        "remaining_below_minimum": remaining is not None and remaining < minimum,
        "flights_at_target": [_endpoint(flight, mode) for flight in at_target],
        "next_flights": [_endpoint(flight, mode) for flight in moments.get(next_moment, [])],
        "later_today_flight_count": later_today,
        "notices": notices,
    }


def analyze(
    rows: Sequence[Mapping[str, Any]], day: date | str, fetched_at: datetime,
    minimum_minutes: float = 30, target_hour: int = 20,
) -> dict[str, Any]:
    """同时输出两种时刻口径和五个区域，任何结果都不授权设备动作。"""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("业务计划日必须为date或YYYY-MM-DD字符串")
    if (not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None
            or fetched_at.utcoffset() is None):
        raise ValueError("取数时刻必须带时区")
    fetched_at = fetched_at.astimezone(SHANGHAI)
    if (type(minimum_minutes) not in (int, float) or not math.isfinite(minimum_minutes)
            or not 1 <= minimum_minutes <= 1440):
        raise ValueError("最小空档必须是1至1440之间的有限分钟数")
    if type(target_hour) is not int or not 0 <= target_hour <= 23:
        raise ValueError("目标小时必须是0至23之间的整数")
    if (not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray))
            or any(not isinstance(row, Mapping) for row in rows)):
        raise ValueError("输入必须是原始航班记录数组")
    snapshot = normalize_flights(rows, fetched_at)
    selected = [flight for flight in snapshot.flights if flight.day == day]
    shared_holds = _shared_quality_holds(rows)
    target = datetime(day.year, day.month, day.day, target_hour, tzinfo=SHANGHAI)
    warnings = list(snapshot.warnings)
    if any(flight.region == "unknown" for flight in snapshot.flights):
        warnings.append("存在区域属性未知航班，将作为同方向区域的保守阻断项。")
    if any(shared_holds.values()):
        warnings.append("有效共享号对应的主号已删除或非营运，原始空档仅供展示，相关区域暂停候选判断。")
    result: dict[str, Any] = {
        "version": "flight-gap-analysis-v1",
        "airport": AIRPORT,
        "date": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "fetched_at": fetched_at.isoformat(),
        "raw_count": snapshot.raw_count,
        "selected_day_flights": len(selected),
        "input_plan_days": sorted({flight.day.isoformat() for flight in snapshot.flights}),
        "normalization_counts": dict(snapshot.counts),
        "warnings": sorted(set(warnings)),
        "limitations": list(LIMITATIONS),
        "quality_flags": [],
        "minimum_minutes": minimum_minutes,
        "target_hour": target_hour,
        "device_control_sent": False,
        "is_shutdown_permission": False,
        "modes": {},
    }
    all_flags: set[str] = set()
    for mode in ("planned", "dynamic"):
        areas = {}
        for area, label in FLIGHT_AREAS.items():
            flags = set(shared_holds[area])
            assessment = _assessment(snapshot.flights, day, mode, area, minimum_minutes, target, flags)
            all_flags.update(flags)
            areas[area] = {
                "label": label,
                "selected_day_flights": len(flights_for_area(selected, area)),
                "gap_count": 0,
                "gaps": [_gap(gap, mode) for gap in flight_gaps(
                    snapshot.flights, day, mode, minimum_minutes, area=area,
                )],
                "quality_flags": sorted(flags),
                "evening_assessment": assessment,
            }
            areas[area]["gap_count"] = len(areas[area]["gaps"])
        result["modes"][mode] = {"areas": areas}
    result["quality_flags"] = sorted(all_flags)
    return result
