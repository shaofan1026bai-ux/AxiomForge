"""航信七个区域的室内探头与 AHU 状态，只读查询小麦历史。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import batched
from time import monotonic

import httpx

from axiomforge.ahu_temperature import (
    DEVICES,
    PROJECT_ID,
    SHANGHAI,
    PointSpec,
    Query,
    QueryConditions,
    Window,
    normalize_series,
    state_for,
)
from axiomforge.xiaomai_client import (
    Settings,
    XiaomaiClient,
    XiaomaiSourceError,
    http_client,
    load_settings,
)

_HISTORY_BATCH_SIZE = 12
_TOTAL_SECONDS = 150


@dataclass(frozen=True, slots=True)
class Probe:
    id: int
    name: str
    temperature: PointSpec | None
    humidity: PointSpec | None


@dataclass(frozen=True, slots=True)
class Region:
    id: str
    floor: str
    name: str
    ahu_id: str
    probes: tuple[Probe, ...]


@dataclass(frozen=True, slots=True)
class ClimateSnapshot:
    day: date
    start: datetime
    end: datetime
    queried_at: datetime
    temperatures: dict[int, dict[datetime, float]]
    humidities: dict[int, dict[datetime, float]]
    ahu_states: dict[str, dict[datetime, str]]


def _probe(device_id: int, name: str, prefix: str) -> Probe:
    return Probe(
        device_id,
        name,
        PointSpec(f"bfby5y_{prefix}_temp", device_id, "℃", "temperature"),
        PointSpec(f"bfby5y_{prefix}_rh", device_id, "%", "humidity"),
    )


# 制冷归属取现场提供的七区清单，测点编号来自 2026-11-10 小麦点表。
# 两个待确认的平台名称保留独立选项，不用近似名称绑定设备。
REGIONS = (
    Region(
        "departure",
        "1F",
        "迎客／出港厅",
        "AHU1",
        (
            _probe(12685, "出入口2", "12014b"),
            _probe(12686, "安检通道3", "120161"),
            _probe(12696, "行李问询", "12016d"),
            Probe(-1, "值机大厅", None, None),
        ),
    ),
    Region(
        "international",
        "1F",
        "国际迎送厅",
        "AHU2",
        (
            _probe(12697, "值机大厅1", "12016c"),
            _probe(12698, "值机大厅2", "12016e"),
        ),
    ),
    Region(
        "transfer",
        "1F",
        "中转／候机／商业厅",
        "AHU4",
        (
            _probe(12666, "1～2号厅1", "120139"),
            _probe(12667, "1～2号厅2", "120164"),
            Probe(-2, "1～3号厅3", None, None),
            _probe(12669, "1～2号厅4", "120156"),
            _probe(12670, "3号厅1", "120169"),
            _probe(12671, "3号厅2", "120131"),
        ),
    ),
    Region(
        "baggage",
        "1F",
        "国内进出港行李分拣",
        "AHU5",
        (
            _probe(12693, "国内到达行李提取1", "12015f"),
            _probe(12694, "国内到达行李提取2", "120175"),
            _probe(12695, "国内到达行李提取3", "120176"),
        ),
    ),
    Region(
        "near-gates",
        "2F",
        "近机位候机厅",
        "AHU3",
        (
            _probe(12672, "4～6号厅1", "12015c"),
            _probe(12673, "4～6号厅2", "12013e"),
            _probe(12674, "4～6号厅3", "120138"),
            _probe(12675, "4～6号厅4", "120140"),
            _probe(12676, "4～6号厅5", "120170"),
        ),
    ),
    Region(
        "gates-7-8",
        "2F",
        "候机厅",
        "AHU3",
        (
            _probe(12677, "7～8号厅1", "12013b"),
            _probe(12678, "7～8号厅2", "120163"),
            _probe(12679, "7～8号厅3", "120167"),
        ),
    ),
    Region(
        "boarding",
        "2F",
        "登机通道",
        "AHU3",
        (
            _probe(12680, "登机通道1", "120136"),
            _probe(12681, "登机通道2", "120143"),
            _probe(12682, "登机通道3", "120144"),
            _probe(12683, "登机通道4", "120146"),
            _probe(12684, "登机通道5", "120157"),
        ),
    ),
)

_PROBES = tuple(probe for region in REGIONS for probe in region.probes)
_AHU_POINTS = {
    device.id: {point.field: point for point in device.points if point.field != "temperature"}
    for device in DEVICES
}
_POINTS = tuple(point for points in _AHU_POINTS.values() for point in points.values()) + tuple(
    point for probe in _PROBES for point in (probe.temperature, probe.humidity) if point is not None
)


def load_region_climate(
    day: date,
    *,
    cancelled: Callable[[], bool] | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ClimateSnapshot:
    """读取一个北京时间自然日，今日截止到已完成的分钟，所有分片成功才发布。"""
    now = datetime.now(SHANGHAI)
    if type(day) is not date or day > now.date():
        raise ValueError("请选择今天或之前的有效日期。")
    start = datetime.combine(day, time.min, tzinfo=SHANGHAI)
    end = now.replace(second=0, microsecond=0) if day == now.date() else start + timedelta(days=1)
    if start == end:
        raise ValueError("今天尚无完整分钟，请稍后查询。")
    window = Window(day, start, end)
    query = Query(
        QueryConditions(period="custom", start=day.isoformat(), end=day.isoformat()), (window,)
    )
    series: dict[str, dict[datetime, float]] = {}
    state_evidence: dict[str, set[datetime]] = {}

    def check_cancelled() -> None:
        if cancelled is not None and cancelled():
            raise XiaomaiSourceError("已停止等待，本次查询未发布结果。")

    try:
        check_cancelled()
        config = settings or load_settings()
        with http_client(transport) as client:
            source = XiaomaiClient(client, monotonic() + _TOTAL_SECONDS, _TOTAL_SECONDS)
            source.authenticate(config, PROJECT_ID)
            check_cancelled()
            source.validate_points(_POINTS, PROJECT_ID)
            for batch in batched(_POINTS, _HISTORY_BATCH_SIZE):
                check_cancelled()
                records = source.history(
                    batch,
                    PROJECT_ID,
                    start.strftime("%Y-%m-%d %H:%M"),
                    end.strftime("%Y-%m-%d %H:%M"),
                    1,
                )
                for point in batch:
                    normalized, _ = normalize_series(records.get(point.point_id, ()), query)
                    series[point.point_id] = {
                        stamp: value
                        for stamp, (value, quality) in normalized.items()
                        if value is not None and quality == "valid"
                    }
                    if point.field in {"feedback", "frequency"}:
                        state_evidence[point.point_id] = {
                            stamp
                            for stamp, (_, quality) in normalized.items()
                            if quality != "missing"
                        }
            check_cancelled()
            source.check_deadline()
    except XiaomaiSourceError:
        raise
    except httpx.TimeoutException:
        raise XiaomaiSourceError("区域温湿度历史请求超时，请稍后重试。") from None
    except httpx.HTTPError:
        raise XiaomaiSourceError("小麦连接或证书校验失败，请检查服务连接。") from None
    except (ValueError, TypeError, OverflowError, OSError):
        raise XiaomaiSourceError("区域温湿度历史或连接配置无法解析。") from None

    temperatures = {
        probe.id: series[probe.temperature.point_id] if probe.temperature else {}
        for probe in _PROBES
    }
    humidities = {
        probe.id: {
            stamp: value
            for stamp, value in series[probe.humidity.point_id].items()
            if 0 <= value <= 100
        }
        if probe.humidity
        else {}
        for probe in _PROBES
    }
    ahu_states = {}
    for ahu_id, points in _AHU_POINTS.items():
        feedback_id = points["feedback"].point_id
        frequency_id = points["frequency"].point_id
        feedback, frequency = series[feedback_id], series[frequency_id]
        # 网格的空桶留空，实际读到的异常或单侧数据保留为状态障碍。
        stamps = sorted(state_evidence[feedback_id] | state_evidence[frequency_id])
        ahu_states[ahu_id] = {
            stamp: state_for(feedback.get(stamp), frequency.get(stamp)) for stamp in stamps
        }
    check_cancelled()
    return ClimateSnapshot(
        day, start, end, datetime.now(SHANGHAI), temperatures, humidities, ahu_states
    )
