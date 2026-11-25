"""同一时间轴上的区域温度、相对湿度与 AHU 状态。"""

from collections.abc import Sequence
from datetime import datetime
from math import ceil, floor
from typing import Any

from axiomforge.ahu_temperature import BINS, SHANGHAI
from axiomforge.region_climate import Excursion, TemperatureTimeCell, TrendPoint
from axiomforge.ui.ahu_temperature_charts import temperature_bin_colors
from axiomforge.ui.charts import palette


def _milliseconds(at: datetime) -> float:
    return at.timestamp() * 1000


def _area(start: datetime, end: datetime, color: str) -> list[dict[str, Any]]:
    return [
        {"xAxis": _milliseconds(start), "itemStyle": {"color": color}},
        {"xAxis": _milliseconds(end)},
    ]


def _running_areas(points: Sequence[TrendPoint], color: str) -> list[list[dict[str, Any]]]:
    intervals: list[tuple[datetime, datetime]] = []
    for point in points:
        if point.state != "running":
            continue
        if intervals and intervals[-1][1] == point.at:
            intervals[-1] = (intervals[-1][0], point.end)
        else:
            intervals.append((point.at, point.end))
    return [_area(start, end, color) for start, end in intervals]


def _needs_symbols(values: Sequence[float | None]) -> bool:
    return any(
        value is not None
        and (index == 0 or values[index - 1] is None)
        and (index == len(values) - 1 or values[index + 1] is None)
        for index, value in enumerate(values)
    )


def trend_options(
    points: Sequence[TrendPoint],
    starts: Sequence[datetime],
    excursions: Sequence[Excursion],
    low: float,
    high: float,
    dark: bool,
    *,
    label: str = "区域均值",
) -> dict[str, Any]:
    colors = palette(dark)
    temperature = colors["power"]
    humidity = "#5eead4" if dark else "#0f766e"
    hot = "#fb923c" if dark else "#c2410c"
    cold = "#93c5fd" if dark else "#1d4ed8"
    running = "#c4b5fd" if dark else "#7c3aed"
    axis: dict[str, Any] = {
        "axisLine": {"show": False},
        "axisTick": {"show": False},
        "axisLabel": {"color": colors["text"], "hideOverlap": True},
        "nameTextStyle": {"color": colors["text"]},
        "splitLine": {"lineStyle": {"color": colors["grid"]}},
    }

    flags = [
        {"name": "AHU 开启", "xAxis": _milliseconds(at), "lineStyle": {"color": running}}
        for at in starts
    ]
    bounds = [
        {"name": name, "yAxis": value, "lineStyle": {"color": color, "opacity": 0.75}}
        for name, value, color in (("过冷线", low, cold), ("过热线", high, hot))
    ]
    areas = _running_areas(points, running + "10") + [
        _area(item.start, item.end, (hot if item.kind == "hot" else cold) + "23")
        for item in excursions
    ]
    range_limits = (
        {"min": _milliseconds(points[0].at), "max": _milliseconds(points[-1].end)} if points else {}
    )
    temperatures = [point.temperature for point in points]
    humidities = [point.humidity for point in points]
    values = [value for value in temperatures if value is not None]
    temperature_min = floor(min([low, *values]) - 0.25)
    temperature_max = ceil(max([high, *values]) + 0.25)
    end_of_day = (
        _milliseconds(points[-1].end)
        if points and points[-1].end.date() > points[0].at.date()
        else -1
    )
    return {
        "animation": False,
        "aria": {"enabled": True, "label": {"description": f"{label}的温度和相对湿度趋势"}},
        "backgroundColor": "transparent",
        "textStyle": {"color": colors["text"], "fontFamily": "system-ui"},
        "grid": {"left": 48, "right": 51, "top": 65, "bottom": 57},
        "legend": {
            "top": 0,
            "textStyle": {"color": colors["text"]},
            "data": ["温度 (°C)", "相对湿度 (%RH)"],
        },
        "tooltip": {
            "trigger": "axis",
            "confine": True,
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
            ":formatter": """items => {
                if (!items.length) return '';
                const at = new Date(items[0].value[0]);
                const label = new Intl.DateTimeFormat('zh-CN', {
                    timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', hourCycle:'h23'
                }).format(at);
                const rows = items.map(item => {
                    const [time, value, count] = item.value;
                    return item.marker + item.seriesName + ' ' + (value == null
                        ? '缺测' : value.toFixed(1) + ' · ' + count + ' 个探头');
                });
                return [label, ...rows].join('<br>');
            }""",
        },
        "xAxis": {
            **axis,
            **range_limits,
            "type": "time",
            "splitNumber": 6,
            "axisLabel": {
                **axis["axisLabel"],
                ":formatter": f"""value => value === {end_of_day} ? '24:00'
                  : new Intl.DateTimeFormat('zh-CN', {{
                    timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', hourCycle:'h23'
                  }}).format(new Date(value))""",
            },
        },
        "yAxis": [
            {
                **axis,
                "type": "value",
                "name": "温度 °C",
                "min": temperature_min,
                "max": temperature_max,
            },
            {
                **axis,
                "type": "value",
                "name": "相对湿度 %RH",
                "min": 0,
                "max": 100,
                "splitLine": {"show": False},
            },
        ],
        "dataZoom": [
            {"type": "inside", "filterMode": "none", "zoomOnMouseWheel": "ctrl"},
            {"type": "slider", "filterMode": "none", "height": 17, "bottom": 8},
        ],
        "series": [
            {
                "id": "region-temperature",
                "name": "温度 (°C)",
                "type": "line",
                "encode": {"x": 0, "y": 1},
                "symbolSize": 3,
                "showSymbol": _needs_symbols(temperatures),
                "showAllSymbol": True,
                "connectNulls": False,
                "itemStyle": {"color": temperature},
                "lineStyle": {"width": 2.5},
                "data": [
                    [_milliseconds(point.at), point.temperature, point.temperature_count]
                    for point in points
                ],
                "markArea": {"silent": True, "data": areas},
                "markLine": {
                    "silent": True,
                    "symbol": "none",
                    "label": {"show": False},
                    "lineStyle": {"type": "dashed", "width": 1},
                    "data": flags + bounds,
                },
            },
            {
                "id": "region-humidity",
                "name": "相对湿度 (%RH)",
                "type": "line",
                "encode": {"x": 0, "y": 1},
                "yAxisIndex": 1,
                "symbolSize": 3,
                "showSymbol": _needs_symbols(humidities),
                "showAllSymbol": True,
                "connectNulls": False,
                "itemStyle": {"color": humidity},
                "lineStyle": {"width": 2, "type": "dashed"},
                "data": [
                    [_milliseconds(point.at), point.humidity, point.humidity_count]
                    for point in points
                ],
            },
        ],
    }


def temperature_time_options(
    rows: Sequence[tuple[str, Sequence[TemperatureTimeCell]]], dark: bool, *, minutes: int
) -> dict[str, Any]:
    """用与温区占比相同的边界和配色绘制已查询时段。"""
    colors = palette(dark)
    missing = len(BINS)
    labels = [band.name for band in BINS] + ["缺测"]
    band_colors = (*temperature_bin_colors(dark), "#505b6b" if dark else "#d7dce3")
    moments = sorted({cell.start for _, cells in rows for cell in cells})
    moment_indexes = {moment: index for index, moment in enumerate(moments)}
    times = [moment.astimezone(SHANGHAI).strftime("%H:%M") for moment in moments]
    data = []
    for row_index, (name, cells) in enumerate(rows):
        for cell in cells:
            column = moment_indexes[cell.start]
            start = cell.start.astimezone(SHANGHAI)
            end = cell.end.astimezone(SHANGHAI)
            end_label = "24:00" if end.date() > start.date() else end.strftime("%H:%M")
            temperature = cell.temperature
            band_index = (
                next(
                    (
                        index
                        for index, band in enumerate(BINS)
                        if band.lower <= temperature < band.upper
                    ),
                    missing,
                )
                if temperature is not None
                else missing
            )
            data.append(
                {
                    "name": name,
                    "value": [column, row_index, band_index],
                    "time": f"{times[column]}–{end_label}",
                    "temperature": temperature,
                    "band": labels[band_index],
                    "count": cell.count,
                    "expected": cell.expected,
                }
            )

    axis: dict[str, Any] = {
        "type": "category",
        "axisTick": {"show": False},
        "axisLine": {"show": False},
        "splitLine": {"show": False},
        "axisLabel": {"color": colors["text"], "hideOverlap": True},
    }
    return {
        "animation": False,
        "backgroundColor": "transparent",
        "aria": {"enabled": True, "label": {"description": f"温区时间图，每格 {minutes} 分钟"}},
        "grid": {"left": 4, "right": 14, "top": 8, "bottom": 88, "containLabel": True},
        "legend": {
            "data": labels,
            "bottom": 3,
            "left": 4,
            "right": 4,
            "itemWidth": 12,
            "itemHeight": 10,
            "itemGap": 9,
            "icon": "roundRect",
            "selectedMode": False,
            "textStyle": {"color": colors["text"]},
        },
        "tooltip": {
            "trigger": "item",
            "triggerOn": "mousemove|click",
            "confine": True,
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
            ":formatter": """item => {
                const cell = item.data;
                const name = String(cell.name).replace(/[&<>]/g, c => (
                    {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]
                ));
                const reading = cell.temperature == null ? cell.band
                    : new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 3})
                        .format(cell.temperature) + ' ℃ · ' + cell.band;
                return name + '<br>' + cell.time + ' · 北京时间<br>' + reading
                    + '<br>有效 5 分钟样本 ' + cell.count + ' / ' + cell.expected;
            }""",
        },
        "xAxis": {**axis, "data": times, "boundaryGap": True},
        "yAxis": {
            **axis,
            "data": [name for name, _ in rows],
            "inverse": True,
            "axisLabel": {**axis["axisLabel"], "width": 145, "overflow": "truncate"},
        },
        "dataZoom": [
            {
                "id": "temperature-time-inside",
                "type": "inside",
                "xAxisIndex": 0,
                "zoomOnMouseWheel": "ctrl",
            },
            {
                "id": "temperature-time-slider",
                "type": "slider",
                "xAxisIndex": 0,
                "bottom": 48,
                "height": 16,
                "showDataShadow": False,
                "textStyle": {"color": colors["text"]},
            },
        ],
        "visualMap": {
            "type": "piecewise",
            "show": False,
            "seriesIndex": 0,
            "dimension": 2,
            "pieces": [{"value": index, "color": color} for index, color in enumerate(band_colors)],
        },
        "series": [
            {
                "id": "temperature-time-grid",
                "type": "heatmap",
                "name": "温区时间图",
                "data": data,
                "itemStyle": {
                    "borderWidth": 1 if minutes == 30 else 0.5,
                    "borderColor": colors["tooltip"],
                },
                "emphasis": {"itemStyle": {"borderColor": colors["text"], "borderWidth": 1.5}},
            },
            *(
                {"name": label, "type": "scatter", "data": [], "itemStyle": {"color": color}}
                for label, color in zip(labels, band_colors, strict=True)
            ),
        ],
    }
