"""航信 AHU 的已核实点位及日期范围只读历史。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from time import monotonic

import httpx

from axiomforge.ahu_co2 import Observation
from axiomforge.ahu_temperature import (
    DEVICES,
    PROJECT_ID,
    SHANGHAI,
    DeviceSpec,
    PointSpec,
    Query,
    QueryConditions,
    Window,
    normalize_series,
    state_for,
)
from axiomforge.flight_source import (
    MAX_FLIGHT_RANGE_DAYS,
    FlightSourceError,
    load_flights,
    load_flights_range,
)
from axiomforge.flights import Flight, FlightSnapshot
from axiomforge.xiaomai_client import (
    Settings,
    XiaomaiClient,
    XiaomaiSourceError,
    http_client,
    load_settings,
)

SOURCE_TIMEOUT = 60
MAX_RANGE_DAYS = MAX_FLIGHT_RANGE_DAYS
REGIONS = {
    "AHU1": "迎客厅、出港手续厅",
    "AHU2": "综合厅、国际迎送客厅",
    "AHU3": "二层近机位候机厅及周边餐饮商业",
    "AHU4": "远机位、机动候机厅及中转商业",
    "AHU5": "国内进出港行李分拣",
}
DEVICE_BY_ID = {device.id: device for device in DEVICES}
FLIGHT_SCOPES = {
    "AHU1": "全场进出港",
    "AHU2": "全场进出港",
    "AHU3": "出港 4～8 号门（7～8 号保守纳入）及非国际未明确门",
    "AHU4": "出港 1～3 号门、国际出港及非国际未明确门",
}


class AhuCo2SourceError(ValueError):
    """可直接呈现的脱敏查询错误。"""


@dataclass(frozen=True, slots=True)
class FlightWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class DaySnapshot:
    device: DeviceSpec
    day: date
    start: datetime
    end: datetime
    samples: tuple[Observation, ...]
    queried_at: datetime
    flights: FlightSnapshot | None
    flight_windows: tuple[FlightWindow, ...]
    flight_error: str

    @property
    def flight_scope(self) -> str | None:
        return FLIGHT_SCOPES.get(self.device.id)


@dataclass(frozen=True, slots=True)
class RangeSnapshot:
    start_day: date
    end_day: date
    days: tuple[DaySnapshot, ...]
    queried_at: datetime


def points_for(device: DeviceSpec) -> tuple[PointSpec, ...]:
    """按已核实的完整编号登记，不从现场名称推断归属。"""
    prefix = f"ewcnad_ms{device.id.lower()}"
    return device.points + tuple(
        PointSpec(f"{prefix}_{suffix}", device.device_id, unit, field)
        for suffix, unit, field in (
            ("co2", "ppm", "co2"),
            ("freshv", "%", "damper"),
            ("v56", "ppm", "setpoint"),
        )
    )


def _in_research_scope(flight: Flight, device_id: str) -> bool:
    if device_id in {"AHU1", "AHU2"}:
        return True
    if flight.direction != "O":
        return False
    gate = int(flight.gate) if flight.gate.isdecimal() and len(flight.gate) <= 2 else None
    unknown_gate = flight.region != "international" and gate not in range(1, 9)
    if device_id == "AHU3":
        return gate in range(4, 9) or unknown_gate
    return gate in range(1, 4) or flight.region == "international" or unknown_gate


def research_windows(snapshot: FlightSnapshot, device_id: str) -> tuple[FlightWindow, ...]:
    """基准研究口径。覆盖原计划旅客到场和延误后的实际流程。"""
    if device_id not in FLIGHT_SCOPES:
        return ()
    windows = []
    for flight in snapshot.flights:
        event = flight.event_time("dynamic")
        if event is None or not _in_research_scope(flight, device_id):
            continue
        begin = min(event, flight.planned or event)
        if flight.direction == "O":
            before = 120 if flight.region == "domestic" else 180
            after = 15
        else:
            before, after = 15, 60
        windows.append(
            FlightWindow(begin - timedelta(minutes=before), event + timedelta(minutes=after))
        )

    merged: list[FlightWindow] = []
    for window in sorted(windows, key=lambda item: item.start):
        if merged and window.start <= merged[-1].end:
            previous = merged[-1]
            merged[-1] = FlightWindow(previous.start, max(previous.end, window.end))
        else:
            merged.append(window)
    return tuple(merged)


def _observations(
    points: tuple[PointSpec, ...],
    records: Mapping[str, Sequence[Mapping[str, object]]],
    query: Query,
) -> tuple[Observation, ...]:
    series = {
        point.field: normalize_series(records.get(point.point_id, ()), query)[0] for point in points
    }
    rows = []
    for moment in query.windows[0].grid():
        values = {
            field: points_by_time.get(moment, (None, "missing"))
            for field, points_by_time in series.items()
        }
        feedback, feedback_quality = values["feedback"]
        frequency, frequency_quality = values["frequency"]
        state = state_for(feedback, frequency)
        state_invalid = any(
            quality in {"invalid", "conflict"} for quality in (feedback_quality, frequency_quality)
        ) or (
            (feedback is not None and feedback not in (0, 1))
            or (frequency is not None and not 0 <= frequency <= 60)
        )
        co2 = values["co2"][0]
        setpoint = values["setpoint"][0]
        damper = values["damper"][0]
        rows.append(
            Observation(
                moment,
                co2 if co2 is not None and co2 > 0 else None,
                values["temperature"][0],
                frequency,
                feedback,
                state,
                damper if damper is not None and 0 <= damper <= 100 else None,
                setpoint if setpoint is not None and setpoint > 0 else None,
                state_invalid,
            )
        )
    return tuple(rows)


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise AhuCo2SourceError("已停止等待，本次查询未发布结果。")


def _load_history(
    devices: Sequence[DeviceSpec],
    queries: tuple[Query, ...],
    *,
    settings: Settings | None,
    transport: httpx.BaseTransport | None,
    cancelled: Callable[[], bool],
    progress: Callable[[int, int], None] | None = None,
) -> dict[tuple[date, str], tuple[Observation, ...]]:
    points_by_device = {device.id: points_for(device) for device in devices}
    points = tuple(point for device_points in points_by_device.values() for point in device_points)
    samples = {}
    # 每多一个自然日，按小麦历史单次请求的超时额度增加总时限。
    seconds = SOURCE_TIMEOUT + 15 * (len(queries) - 1)
    _check_cancelled(cancelled)
    try:
        config = settings or load_settings()
        with http_client(transport) as client:
            source = XiaomaiClient(client, monotonic() + seconds, seconds)
            source.authenticate(config, PROJECT_ID)
            _check_cancelled(cancelled)
            source.validate_points(points, PROJECT_ID)
            for completed, query in enumerate(queries, 1):
                _check_cancelled(cancelled)
                records = source.history(
                    points,
                    PROJECT_ID,
                    f"{query.start:%Y-%m-%d %H:%M}",
                    f"{query.end:%Y-%m-%d %H:%M}",
                    1,
                )
                _check_cancelled(cancelled)
                for device_id, device_points in points_by_device.items():
                    samples[query.windows[0].day, device_id] = _observations(
                        device_points, records, query
                    )
                if progress is not None:
                    progress(completed, len(queries))
    except XiaomaiSourceError as error:
        raise AhuCo2SourceError(str(error)) from None
    except httpx.HTTPError:
        raise AhuCo2SourceError("小麦历史请求失败，请检查网络后重试。") from None
    except AhuCo2SourceError:
        raise
    except (ValueError, OSError):
        raise AhuCo2SourceError("小麦历史采样无法解析。") from None
    _check_cancelled(cancelled)
    return samples


def _queries_for_range(start_day: date, end_day: date, now: datetime | None) -> tuple[Query, ...]:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if start_day <= date.min or end_day >= date.max or end_day > current.date():
        raise AhuCo2SourceError("请选择今天或之前的有效日期。")
    if end_day < start_day:
        raise AhuCo2SourceError("结束日期不能早于开始日期。")
    total_days = (end_day - start_day).days + 1
    if total_days > MAX_RANGE_DAYS:
        raise AhuCo2SourceError(f"AHU CO₂ 查询范围最多 {MAX_RANGE_DAYS} 天。")
    cutoff = current.replace(second=0, microsecond=0)
    queries = []
    for offset in range(total_days):
        day = start_day + timedelta(days=offset)
        start = datetime.combine(day, time.min, SHANGHAI)
        end = min(start + timedelta(days=1), cutoff)
        if end <= start:
            raise AhuCo2SourceError("今天尚无完整分钟，请选择之前的日期。")
        queries.append(
            Query(
                QueryConditions("custom", day.isoformat(), day.isoformat()),
                (Window(day, start, end),),
            )
        )
    return tuple(queries)


def _load_flight_context(
    loader: Callable[[], FlightSnapshot],
    devices: Sequence[DeviceSpec],
) -> tuple[FlightSnapshot | None, dict[str, tuple[FlightWindow, ...]], str]:
    try:
        flights = loader()
        return flights, {device.id: research_windows(flights, device.id) for device in devices}, ""
    except FlightSourceError as error:
        return None, {}, str(error)
    except (ValueError, OSError, OverflowError):
        return None, {}, "航班连接配置或时刻无法解析，设备历史已保留。"


def load_ahu_co2(
    device_id: str,
    day: date,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    flight_loader: Callable[[date], FlightSnapshot] = load_flights,
    now: datetime | None = None,
) -> DaySnapshot:
    """按北京时间查单台单日，只纳入已结束的分钟。"""
    device = DEVICE_BY_ID.get(device_id)
    if device is None:
        raise AhuCo2SourceError("请选择航信机场的 AHU1～5。")
    queries = _queries_for_range(day, day, now)
    samples = _load_history(
        (device,), queries, settings=settings, transport=transport, cancelled=cancelled
    )
    flights, flight_windows, flight_error = _load_flight_context(
        lambda: flight_loader(day), (device,)
    )
    _check_cancelled(cancelled)
    return DaySnapshot(
        device=device,
        day=day,
        start=queries[0].start,
        end=queries[0].end,
        samples=samples[day, device.id],
        queried_at=datetime.now(SHANGHAI),
        flights=flights,
        flight_windows=flight_windows.get(device.id, ()),
        flight_error=flight_error,
    )


def load_ahu_co2_range(
    start_day: date,
    end_day: date,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int], None] | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    flight_loader: Callable[[date, date], FlightSnapshot] = load_flights_range,
    now: datetime | None = None,
) -> RangeSnapshot:
    """首尾日期均纳入，每个自然日批查全部设备，按日期和设备顺序返回。"""
    queries = _queries_for_range(start_day, end_day, now)
    samples = _load_history(
        DEVICES,
        queries,
        settings=settings,
        transport=transport,
        cancelled=cancelled,
        progress=progress,
    )
    flights, flight_windows, flight_error = _load_flight_context(
        lambda: flight_loader(start_day, end_day), DEVICES
    )
    _check_cancelled(cancelled)
    queried_at = datetime.now(SHANGHAI)
    days = []
    for query in queries:
        day = query.windows[0].day
        # 时序只展示本日航班事件，同时保留本次源查询的质量警示。
        daily_flights = (
            replace(
                flights,
                flights=tuple(
                    flight
                    for flight in flights.flights
                    if (moment := flight.event_time("dynamic")) is not None
                    and query.start <= moment < query.end
                ),
            )
            if flights is not None
            else None
        )
        # 相邻计划日的完整窗口可跨午夜，仍可作为窗外判定的依据。
        adjacent_start = query.start - timedelta(days=1)
        adjacent_day = day + timedelta(days=1)
        for device in DEVICES:
            days.append(
                DaySnapshot(
                    device=device,
                    day=day,
                    start=query.start,
                    end=query.end,
                    samples=samples[day, device.id],
                    queried_at=queried_at,
                    flights=daily_flights,
                    flight_windows=tuple(
                        window
                        for window in flight_windows.get(device.id, ())
                        if window.start.date() <= adjacent_day and window.end > adjacent_start
                    ),
                    flight_error=flight_error,
                )
            )
    _check_cancelled(cancelled)
    return RangeSnapshot(start_day, end_day, tuple(days), queried_at)
