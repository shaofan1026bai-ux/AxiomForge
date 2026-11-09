"""真实专题的图表配置；颜色复用工作台，时间戳保持唯一可定位。"""

from typing import Any

from axiomforge.power_temperature import QueryRange, Snapshot
from axiomforge.ui.charts import palette


def trend_options(
    snapshot: Snapshot,
    dark: bool,
    selection: QueryRange | None = None,
    focus: float | None = None,
    zoom: dict[str, float] | None = None,
    legend: dict[str, bool] | None = None,
) -> dict[str, Any]:
    colors = palette(dark)
    axis = {
        "axisLine": {"show": False},
        "axisTick": {"show": False},
        "axisLabel": {"color": colors["text"]},
        "nameTextStyle": {"color": colors["text"]},
        "splitLine": {"lineStyle": {"color": colors["grid"]}},
    }
    marker = [] if focus is None else [{"xAxis": focus}]
    area = (
        []
        if selection is None
        else [
            [
                {"xAxis": selection.start.timestamp() * 1000},
                {"xAxis": selection.end.timestamp() * 1000},
            ]
        ]
    )
    return {
        "animation": False,
        "backgroundColor": "transparent",
        "useUTC": False,
        "textStyle": {"color": colors["text"], "fontFamily": "system-ui"},
        "grid": {"left": 62, "right": 54, "top": 48, "bottom": 65},
        "legend": {"top": 0, "textStyle": {"color": colors["text"]}, "selected": legend or {}},
        "tooltip": {
            "trigger": "axis",
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
            ":formatter": """items => {
              const t = new Date(items[0].value[0]).toLocaleString('zh-CN',
                {timeZone:'Asia/Shanghai',hour12:false});
              return t + '<br>' + items.map(p => p.marker + p.seriesName + ': '
                + (p.value[1] == null ? '缺失' : p.value[1])).join('<br>');
            }""",
        },
        "xAxis": {
            **axis,
            "type": "time",
            "name": "上海时间",
            "splitNumber": 4,
            "axisLabel": {
                "color": colors["text"],
                "hideOverlap": True,
                "lineHeight": 16,
                ":formatter": """value => {
                  const d = new Date(value), z = {timeZone:'Asia/Shanghai'};
                  return d.toLocaleDateString('zh-CN', {...z,month:'2-digit',day:'2-digit'})
                    + '\\n' + d.toLocaleTimeString('zh-CN',
                      {...z,hour:'2-digit',minute:'2-digit',hour12:false});
                }""",
            },
        },
        "yAxis": [
            {**axis, "type": "value", "name": "功率 (kW)", "scale": True},
            {
                **axis,
                "type": "value",
                "name": "室外温度 (℃)",
                "scale": True,
                "splitLine": {"show": False},
            },
        ],
        "dataZoom": [
            {"type": "inside", "filterMode": "none", **(zoom or {})},
            {
                "type": "slider",
                "filterMode": "none",
                "bottom": 5,
                "height": 18,
                "textStyle": {"color": colors["text"]},
                **(zoom or {}),
            },
        ],
        "brush": {
            "xAxisIndex": 0,
            "brushType": False,
            "brushMode": "single",
            "transformable": False,
            "removeOnClick": False,
            "brushStyle": {"color": colors["power"] + "26", "borderColor": colors["power"]},
        },
        "series": [
            {
                "id": "power",
                "name": "功率 (kW)",
                "type": "line",
                "showSymbol": False,
                "connectNulls": False,
                "itemStyle": {"color": colors["power"]},
                "lineStyle": {"width": 2},
                "data": [[row.time.timestamp() * 1000, row.power] for row in snapshot.rows],
                "markArea": {
                    "silent": True,
                    "itemStyle": {"color": colors["power"] + "16"},
                    "data": area,
                },
                "markLine": {
                    "silent": True,
                    "symbol": "none",
                    "data": marker,
                    "label": {"show": False},
                    "lineStyle": {"color": colors["text"]},
                },
            },
            {
                "id": "temperature",
                "name": "室外温度 (℃)",
                "type": "line",
                "showSymbol": False,
                "connectNulls": False,
                "yAxisIndex": 1,
                "itemStyle": {"color": colors["temperature"]},
                "lineStyle": {"width": 2, "type": "dashed"},
                "data": [[row.time.timestamp() * 1000, row.temperature] for row in snapshot.rows],
            },
        ],
    }


def scatter_options(
    snapshot: Snapshot, dark: bool, selection: QueryRange | None = None
) -> dict[str, Any]:
    colors = palette(dark)
    axis = {
        "type": "value",
        "scale": True,
        "axisLabel": {"color": colors["text"]},
        "nameTextStyle": {"color": colors["text"]},
        "splitLine": {"lineStyle": {"color": colors["grid"]}},
    }
    return {
        "animation": False,
        "backgroundColor": "transparent",
        "grid": {"left": 56, "right": 24, "top": 48, "bottom": 65},
        "xAxis": {**axis, "name": "室外温度 (℃)", "nameLocation": "middle", "nameGap": 35},
        "yAxis": {**axis, "name": "功率 (kW)"},
        "tooltip": {
            "trigger": "item",
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
            ":formatter": """p => new Date(p.value[2]).toLocaleString('zh-CN',
              {timeZone:'Asia/Shanghai',hour12:false}) + '<br>室外温度: '
              + p.value[0] + ' ℃<br>功率: ' + p.value[1] + ' kW'""",
        },
        "series": [
            {
                "id": "pairs",
                "name": "功率与室外温度",
                "type": "scatter",
                "symbolSize": 6,
                "itemStyle": {"color": colors["power"], "opacity": 0.7},
                "data": [
                    [row.temperature, row.power, row.time.timestamp() * 1000]
                    for row in snapshot.selected(selection)
                    if row.paired
                ],
            }
        ],
    }
