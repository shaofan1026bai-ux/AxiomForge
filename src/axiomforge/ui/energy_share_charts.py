"""八项分母固定；图例高亮不删除项目，缺测日期保留。"""

import math
from typing import Any

from axiomforge.energy_share import GROUPS, Snapshot, format_number
from axiomforge.ui.charts import palette

COLORS = ("#7566d8", "#a399e9", "#5a9bd4", "#67b6be", "#519e80", "#dda851", "#d17d69", "#7791b0")


def daily_options(
    snapshot: Snapshot, dark: bool, mode: str = "share", highlight: str = "", viewport: int = 14
) -> dict[str, Any]:
    """仅改变显示方式；y轴上限基于完整快照，百分比使用领域结果。"""
    colors = palette(dark)
    days = snapshot.daily
    maximum = max((float(day.known_subtotal) for day in days), default=0)
    upper = max(1, math.ceil(maximum / 100) * 100)
    series = []
    for index, group in enumerate(GROUPS):
        data = []
        for day in days:
            item = next(item for item in day.items if item.group.key == group.key)
            value = item.share if mode == "share" else item.kwh
            data.append(
                {
                    "value": float(value) if value is not None else None,
                    "kwh": format_number(item.kwh),
                    "share": format_number(item.share),
                    "quality": item.reason or ("短时边界对齐" if item.estimated else "未观察回退"),
                }
            )
        series.append(
            {
                "name": group.name,
                "id": group.key,
                "type": "bar",
                "stack": "fixed-eight",
                "barMaxWidth": 48,
                "emphasis": {"focus": "series"},
                "itemStyle": {
                    "color": COLORS[index],
                    "opacity": 1 if not highlight or highlight == group.key else 0.28,
                },
                "data": data,
            }
        )
    annotations = []
    if mode == "share":
        for index, day in enumerate(days):
            if day.total is None or day.total == 0:
                annotations.append(
                    {
                        "value": [index, 3],
                        "label": {
                            "show": True,
                            "position": "top",
                            "formatter": "占比不可确定" if day.total is None else "总量为0",
                            "fontSize": 10,
                            "color": colors["text"],
                        },
                    }
                )
    series.append(
        {
            "name": "日状态",
            "type": "scatter",
            "symbol": "emptyCircle",
            "symbolSize": 5,
            "itemStyle": {"color": colors["text"]},
            "data": annotations,
            "tooltip": {"show": False},
        }
    )
    end = len(days) - 1
    start = max(0, end - viewport + 1)
    return {
        "animation": False,
        "backgroundColor": "transparent",
        "textStyle": {"color": colors["text"], "fontFamily": "system-ui"},
        "grid": {"left": 58, "right": 18, "top": 32, "bottom": 75},
        "tooltip": {
            "trigger": "item",
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
            ":formatter": """p => p.name+'<br>'+p.marker+p.seriesName+'<br>'
                +p.data.kwh+' kWh · '+p.data.share+'%<br>'+p.data.quality""",
        },
        "xAxis": {
            "type": "category",
            "data": [day.day.isoformat() if day.day else "" for day in days],
            "axisLabel": {"color": colors["text"], "hideOverlap": True},
            "axisTick": {"show": False},
            "axisLine": {"lineStyle": {"color": colors["grid"]}},
        },
        "yAxis": {
            "type": "value",
            "min": 0,
            "max": 100 if mode == "share" else upper,
            "name": "%" if mode == "share" else "kWh",
            "nameTextStyle": {"color": colors["text"]},
            "axisLabel": {"color": colors["text"]},
            "splitLine": {"lineStyle": {"color": colors["grid"]}},
        },
        "dataZoom": [
            {"type": "inside", "filterMode": "none", "startValue": start, "endValue": end},
            {
                "type": "slider",
                "filterMode": "none",
                "bottom": 5,
                "height": 18,
                "startValue": start,
                "endValue": end,
                "textStyle": {"color": colors["text"]},
            },
        ],
        "series": series,
    }
