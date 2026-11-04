"""Per-client chart configuration; never change a global chart theme."""

from typing import Any

from axiomforge.demo import Parameters, Sample


def palette(dark: bool) -> dict[str, str]:
    return {
        "text": "#b3bfd2" if dark else "#64748b",
        "grid": "#293549" if dark else "#e8edf4",
        "power": "#729cff" if dark else "#4266ef",
        "temperature": "#c49aff" if dark else "#9560df",
        "tooltip": "#182234" if dark else "#ffffff",
    }


def chart_options(
    samples: list[Sample],
    params: Parameters,
    dark: bool,
    *,
    scatter: bool = False,
) -> dict[str, Any]:
    colors = palette(dark)
    common: dict[str, Any] = {
        "animation": False,
        "backgroundColor": "transparent",
        "textStyle": {"fontFamily": "system-ui, sans-serif", "color": colors["text"]},
        "tooltip": {
            "trigger": "item" if scatter else "axis",
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
        },
        "grid": {"top": 42, "left": 56, "right": 50 if not scatter else 24, "bottom": 52},
    }
    axis = {
        "axisLine": {"show": False},
        "axisTick": {"show": False},
        "axisLabel": {"color": colors["text"], "fontSize": 12},
        "nameTextStyle": {"color": colors["text"], "fontSize": 11},
        "splitLine": {"lineStyle": {"color": colors["grid"]}},
    }
    if scatter:
        common.update(
            {
                "xAxis": {
                    **axis,
                    "type": "value",
                    "name": "室外温度 (°C)",
                    "nameLocation": "middle",
                    "nameGap": 32,
                    "scale": True,
                },
                "yAxis": {**axis, "type": "value", "name": "功率 (kW)", "scale": True},
                "series": [
                    {
                        "name": "功率与温度",
                        "type": "scatter",
                        "symbolSize": 5,
                        "itemStyle": {"color": colors["power"], "opacity": 0.76},
                        "data": [[s.temperature, s.power] for s in samples],
                    }
                ],
            }
        )
    else:
        series: list[dict[str, Any]] = [
            {
                "name": "功率 (kW)",
                "type": "line",
                "showSymbol": False,
                "lineStyle": {"width": 2.3},
                "itemStyle": {"color": colors["power"]},
                "data": [s.power for s in samples],
                "markLine": {
                    "silent": True,
                    "symbol": "none",
                    "lineStyle": {"color": colors["text"], "opacity": 0.45},
                    "label": {"show": False},
                    "data": [{"yAxis": params.threshold}],
                },
            }
        ]
        # Keep both series present so ECharts merge removes old temperature values reliably.
        series.append(
            {
                "name": "室外温度 (°C)",
                "type": "line",
                "showSymbol": False,
                "yAxisIndex": 1,
                "lineStyle": {"width": 1.8, "type": "dashed"},
                "itemStyle": {"color": colors["temperature"]},
                "data": [s.temperature for s in samples] if params.temperature else [],
            }
        )
        common.update(
            {
                "legend": {
                    "top": 0,
                    "right": 18,
                    "itemWidth": 18,
                    "textStyle": {"color": colors["text"], "fontSize": 11},
                },
                "xAxis": {
                    **axis,
                    "type": "category",
                    "boundaryGap": False,
                    "data": [s.time.strftime("%m-%d %H:%M") for s in samples],
                    "axisLabel": {
                        "color": colors["text"],
                        "fontSize": 11,
                        "interval": max(0, len(samples) // 6 - 1),
                    },
                    "splitLine": {
                        "show": True,
                        "interval": max(0, len(samples) // 6 - 1),
                        "lineStyle": {"color": colors["grid"]},
                    },
                },
                "yAxis": [
                    {
                        **axis,
                        "type": "value",
                        "name": "功率 (kW)",
                        "nameLocation": "middle",
                        "nameGap": 40,
                    },
                    {
                        **axis,
                        "type": "value",
                        "name": "温度 (°C)",
                        "min": 0,
                        "max": 35,
                        "nameLocation": "middle",
                        "nameGap": 35,
                        "splitLine": {"show": False},
                    },
                ],
                "dataZoom": [{"type": "inside", "filterMode": "none"}],
                "series": series,
            }
        )
    return common
