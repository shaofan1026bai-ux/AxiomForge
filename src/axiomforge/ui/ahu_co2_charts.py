"""AHU 开机前浓度分布，以及共用北京时间轴的事件和航班参考窗。"""

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from axiomforge.ahu_co2 import Event
from axiomforge.ahu_co2_source import DaySnapshot
from axiomforge.ahu_temperature import DEVICES
from axiomforge.ui.charts import palette

METRICS = {
    "temperature": ("回风温度", "℃"),
    "frequency": ("风机频率", "Hz"),
    "damper": ("新风阀开度", "%"),
}
AXIS_COUNT = 4
PLOT_LEFT = 60
PLOT_RIGHT = 18
TIME_LABEL = """v => new Date(v).toLocaleTimeString('zh-CN', {
  timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'
})"""
TOOLTIP = r"""items => {
  if (!items.length) return '';
  const time = new Date(items[0].axisValue).toLocaleTimeString('zh-CN', {
    timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'
  });
  const rows = items.flatMap(p => {
    const value = p.value[1];
    if (value == null) return [];
    if (p.seriesId === 'state') return ['停机', '运行', '异常'][value] || [];
    if (p.seriesId === 'flights') return p.value[2];
    return p.seriesName + ' ' + Number(value).toFixed(1);
  });
  return [time, ...(rows.length ? rows : ['无有效读数'])].join('\n');
}"""


def _milliseconds(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _focus(snapshot: DaySnapshot, event: Event | None) -> dict[str, int]:
    if event is None:
        return {
            "startValue": _milliseconds(snapshot.start),
            "endValue": _milliseconds(snapshot.end),
        }
    span = timedelta(minutes=30)
    return {
        "startValue": _milliseconds(max(snapshot.start, event.time - span)),
        "endValue": _milliseconds(min(snapshot.end, event.time + span)),
    }


def _flight_events(snapshot: DaySnapshot) -> list[list[int | str]]:
    data: list[list[int | str]] = []
    if snapshot.flights is None:
        return data
    for flight in snapshot.flights.flights:
        moment = flight.event_time("dynamic")
        if moment is not None and snapshot.start <= moment < snapshot.end:
            direction = "进港" if flight.direction == "I" else "出港"
            basis = "实际" if flight.actual else "预计" if flight.estimated else "计划"
            data.append(
                [
                    _milliseconds(moment),
                    int(flight.direction == "O"),
                    f"{flight.number} {direction} · {basis}",
                ]
            )
    return data


def distribution_options(values: Mapping[str, Sequence[float]], dark: bool) -> dict[str, Any]:
    colors = palette(dark)
    axis: dict[str, Any] = {
        "axisLine": {"show": False},
        "axisTick": {"show": False},
        "axisLabel": {"color": colors["text"], "fontSize": 11},
        "splitLine": {"show": True, "lineStyle": {"color": colors["grid"]}},
    }
    points: list[dict[str, Any]] = []
    for device in DEVICES:
        for ppm, count in Counter(values.get(device.id, ())).items():
            # 等值点仅在设备行内纵向错开，保留实际浓度坐标。
            step = min(8, 24 / max(1, count - 1))
            for index in range(count):
                points.append(
                    {
                        "value": [ppm, device.id],
                        "symbolOffset": [0, (index - (count - 1) / 2) * step],
                        "repeats": count,
                    }
                )
    return {
        "animation": False,
        "backgroundColor": "transparent",
        "textStyle": {"color": colors["text"], "fontFamily": "system-ui, sans-serif"},
        "grid": {"left": PLOT_LEFT, "right": 22, "top": 8, "bottom": 48},
        "xAxis": {
            **axis,
            "type": "value",
            "scale": True,
            "boundaryGap": ["8%", "8%"],
            "min": None if points else 0,
            "max": None if points else 1000,
            "splitNumber": 5,
            "minInterval": 1,
            "name": "CO₂（ppm）",
            "nameLocation": "middle",
            "nameGap": 29,
            "nameTextStyle": {"color": colors["text"], "fontSize": 11},
            "axisLabel": {**axis["axisLabel"], "hideOverlap": True},
        },
        "yAxis": {
            **axis,
            "type": "category",
            "inverse": True,
            "data": [device.id for device in DEVICES],
            "axisLabel": {**axis["axisLabel"], "interval": 0},
        },
        "tooltip": {
            "trigger": "item",
            "renderMode": "richText",
            "confine": True,
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
            ":formatter": r"""p => {
  const [ppm, device] = p.value;
  const concentration = Number(ppm).toLocaleString('zh-CN', {maximumFractionDigits: 2});
  const repeated = p.data.repeats > 1 ? '\n同一浓度 ' + p.data.repeats + ' 次' : '';
  return device + ' · ' + concentration + ' ppm' + repeated;
}""",
        },
        "series": [
            {
                "id": "startup-co2",
                "type": "scatter",
                "symbolSize": 8,
                "itemStyle": {"color": "#a5a0ff" if dark else "#4f46e5", "opacity": 0.84},
                "data": points,
            }
        ],
    }


def detail_options(
    snapshot: DaySnapshot, event: Event | None, metric: str, threshold: float, dark: bool
) -> dict[str, Any]:
    colors = palette(dark)
    co2_color = "#a5a0ff" if dark else "#4f46e5"
    metric_color = "#65d6c0" if dark else "#0f766e"
    flight_color = "#91b6ed" if dark else "#3b6bab"
    metric_name, unit = METRICS[metric]
    axis: dict[str, Any] = {
        "axisLine": {"show": False},
        "axisTick": {"show": False},
        "axisLabel": {"color": colors["text"], "fontSize": 11},
        "splitLine": {"lineStyle": {"color": colors["grid"]}},
    }
    top = (30, 184, 306, 392)
    heights = (111, 78, 42, 38)
    titles = ("CO₂ · ppm", f"{metric_name} · {unit}", "风机状态", "本场航班")
    flight_bands = [
        [
            {"xAxis": _milliseconds(max(snapshot.start, window.start))},
            {"xAxis": _milliseconds(min(snapshot.end, window.end))},
        ]
        for window in snapshot.flight_windows
        if window.start < snapshot.end and window.end > snapshot.start
    ]
    marks: dict[str, Any] = {
        "silent": True,
        "symbol": "none",
        "label": {"show": False},
        "lineStyle": {"color": colors["text"], "type": "dashed"},
        "data": [] if event is None else [{"xAxis": _milliseconds(event.time)}],
    }
    # 使用离散有效点；一分钟网格中的空位保留断点。
    series: list[dict[str, Any]] = [
        {
            "id": "co2" if index == 0 else "metric",
            "name": name,
            "type": "line",
            "xAxisIndex": index,
            "yAxisIndex": index,
            "showSymbol": True,
            "showAllSymbol": True,
            "symbolSize": 4,
            "connectNulls": False,
            "itemStyle": {"color": color},
            "lineStyle": {"color": color, "width": 2},
            "data": [[_milliseconds(row.time), getattr(row, field)] for row in snapshot.samples],
            "markLine": marks,
        }
        for index, (field, name, color) in enumerate(
            (("co2", "CO₂ · ppm", co2_color), (metric, f"{metric_name} · {unit}", metric_color))
        )
    ]
    series[0]["markArea"] = {
        "silent": True,
        "itemStyle": {"color": "rgba(84, 137, 208, 0.14)"},
        "data": flight_bands,
    }
    series[0]["markLine"] = {
        **marks,
        "data": [
            *marks["data"],
            {
                "yAxis": threshold,
                "lineStyle": {"color": "#a86e22" if not dark else "#f2c272"},
                "label": {
                    "show": True,
                    "position": "insideEndTop",
                    "formatter": f"观察 {threshold:g} ppm",
                    "color": "#87561a" if not dark else "#f2c272",
                },
            },
        ],
    }
    series[0]["markPoint"] = {
        "silent": True,
        "symbol": "circle",
        "symbolSize": 12,
        "label": {"show": False},
        "data": []
        if event is None
        or event.sample.co2 is None
        or not snapshot.start <= event.sample.time < snapshot.end
        else [
            {
                "coord": [_milliseconds(event.sample.time), event.sample.co2],
                "itemStyle": {
                    "color": co2_color,
                    "borderColor": colors["tooltip"],
                    "borderWidth": 2,
                },
            }
        ],
    }
    state_levels = {"stopped": 0, "running": 1, "conflict": 2}
    state_colors = {0: colors["text"], 1: metric_color, 2: "#dc8e36"}
    series.extend(
        (
            {
                "id": "state",
                "name": "风机状态",
                "type": "scatter",
                "xAxisIndex": 2,
                "yAxisIndex": 2,
                "symbolSize": 4,
                "data": [
                    {
                        "value": [_milliseconds(row.time), level],
                        "itemStyle": {"color": state_colors[level]},
                    }
                    for row in snapshot.samples
                    if (level := 2 if row.state_invalid else state_levels.get(row.state))
                    is not None
                ],
                "markLine": marks,
            },
            {
                "id": "flights",
                "name": "本场航班",
                "type": "scatter",
                "xAxisIndex": 3,
                "yAxisIndex": 3,
                "symbol": "diamond",
                "symbolSize": 6,
                "itemStyle": {"color": flight_color},
                "data": _flight_events(snapshot),
                "markLine": marks,
            },
        )
    )
    focus = _focus(snapshot, event)
    co2_axis = {
        **axis,
        "type": "value",
        "scale": True,
        "splitNumber": 4,
        "min": None,
        "max": None,
    }
    values = [row.co2 for row in snapshot.samples if row.co2 is not None]
    if values and threshold > max(values):
        co2_axis["max"] = threshold + 10
    if values and threshold < min(values):
        co2_axis["min"] = max(0, threshold - 10)
    return {
        "animation": False,
        "backgroundColor": "transparent",
        "textStyle": {"color": colors["text"], "fontFamily": "system-ui, sans-serif"},
        "grid": [
            {"left": PLOT_LEFT, "right": PLOT_RIGHT, "top": y, "height": h}
            for y, h in zip(top, heights, strict=True)
        ],
        # 标题占用轨道上方的独立留白，并对齐绘图区左边缘。
        "graphic": [
            {
                "id": f"track-title-{index}",
                "type": "text",
                "left": PLOT_LEFT,
                "top": y - 24,
                "silent": True,
                "style": {
                    "text": title,
                    "fill": colors["text"],
                    "font": "12px system-ui, sans-serif",
                    "lineHeight": 16,
                },
            }
            for index, (title, y) in enumerate(zip(titles, top, strict=True))
        ],
        "xAxis": [
            {
                **axis,
                "type": "time",
                "gridIndex": index,
                "min": _milliseconds(snapshot.start),
                "max": _milliseconds(snapshot.end),
                "splitNumber": 6,
                "axisLabel": {
                    **axis["axisLabel"],
                    "show": index == AXIS_COUNT - 1,
                    "hideOverlap": True,
                    ":formatter": TIME_LABEL,
                },
                "axisPointer": {"label": {"show": False}},
            }
            for index in range(AXIS_COUNT)
        ],
        "yAxis": [
            co2_axis,
            {
                **axis,
                "type": "value",
                "gridIndex": 1,
                "scale": metric == "temperature",
                "splitNumber": 3,
            },
            {
                **axis,
                "type": "value",
                "gridIndex": 2,
                "min": 0,
                "max": 2,
                "interval": 1,
                "axisLabel": {**axis["axisLabel"], ":formatter": "v => ['停机','运行','异常'][v]"},
            },
            {
                **axis,
                "type": "value",
                "gridIndex": 3,
                "min": 0,
                "max": 1,
                "interval": 1,
                "axisLabel": {**axis["axisLabel"], ":formatter": "v => ['进港','出港'][v]"},
            },
        ],
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "tooltip": {
            "trigger": "axis",
            "renderMode": "richText",
            "confine": True,
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
            ":formatter": TOOLTIP,
        },
        "dataZoom": [
            {
                "id": "event-inside",
                "type": "inside",
                "xAxisIndex": list(range(AXIS_COUNT)),
                "filterMode": "none",
                **focus,
            },
            {
                "id": "event-slider",
                "type": "slider",
                "xAxisIndex": list(range(AXIS_COUNT)),
                "filterMode": "none",
                "bottom": 16,
                "height": 17,
                "left": PLOT_LEFT,
                "right": PLOT_RIGHT,
                "showDetail": False,
                "textStyle": {"color": colors["text"]},
                **focus,
            },
        ],
        "series": series,
    }
