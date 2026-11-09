"""AHU统计图只读取成功快照；显示范围与温区分母不受图例影响。"""

import json
from typing import Any

from axiomforge.ahu_temperature import BINS, DeviceResult, Snapshot
from axiomforge.ui.charts import palette


def _base(dark: bool) -> dict[str, Any]:
    colors = palette(dark)
    return {
        "animation": False,
        "backgroundColor": "transparent",
        "textStyle": {"color": colors["text"], "fontFamily": "system-ui"},
        "tooltip": {
            "trigger": "item",
            "backgroundColor": colors["tooltip"],
            "borderColor": colors["grid"],
            "textStyle": {"color": colors["text"]},
        },
        "grid": {"left": 58, "right": 26, "top": 36, "bottom": 42},
    }


def _axis(dark: bool) -> dict[str, Any]:
    colors = palette(dark)
    return {
        "axisLine": {"show": False},
        "axisTick": {"show": False},
        "axisLabel": {"color": colors["text"]},
        "nameTextStyle": {"color": colors["text"]},
        "splitLine": {"lineStyle": {"color": colors["grid"]}},
    }


def range_options(snapshot: Snapshot, dark: bool, running: bool = True) -> dict[str, Any]:
    """细线极值、粗线P05/P95、圆点均值，不冒用箱线图的四分位含义。"""
    color = palette(dark)["power"]
    data = []
    for index, device in enumerate(snapshot.devices):
        stats = device.running_stats if running else device.all_stats
        if stats.count:
            data.append(
                {
                    "name": device.spec.id,
                    "value": [
                        index,
                        stats.minimum,
                        stats.maximum,
                        stats.p05,
                        stats.p95,
                        stats.mean,
                        stats.count,
                    ],
                }
            )
    return {
        **_base(dark),
        "xAxis": {
            **_axis(dark),
            "type": "value",
            "name": "回风温度 (℃)",
            "nameLocation": "middle",
            "nameGap": 28,
            "scale": True,
        },
        "yAxis": {
            **_axis(dark),
            "type": "category",
            "data": [device.spec.id for device in snapshot.devices],
            "inverse": True,
            "splitLine": {"show": False},
        },
        "tooltip": {
            **_base(dark)["tooltip"],
            ":formatter": """p => {
              const d=p.value, f=v=>Number(v).toFixed(1);
              return p.name + '<br>最低—最高：'+f(d[1])+'～'+f(d[2])+' ℃'
                +'<br>P05～P95：'+f(d[3])+'～'+f(d[4])+' ℃'
                +'<br>样本平均：'+f(d[5])+' ℃<br>有效样本：'+d[6];
            }""",
        },
        "series": [
            {
                "id": "ahu-range",
                "type": "custom",
                "encode": {"x": [1, 2], "y": 0},
                ":renderItem": """(params,api) => {
                  const y=api.value(0), coord=n=>api.coord([api.value(n),y]);
                  const lo=coord(1),hi=coord(2),p05=coord(3),p95=coord(4),mean=coord(5);
                  const color=COLOR;
                  return {type:'group',children:[
                    {type:'line',shape:{x1:lo[0],y1:lo[1],x2:hi[0],y2:hi[1]},
                     style:{stroke:color,lineWidth:2}},
                    {type:'line',shape:{x1:p05[0],y1:p05[1],x2:p95[0],y2:p95[1]},
                     style:{stroke:color,lineWidth:9,lineCap:'round'}},
                    {type:'circle',shape:{cx:mean[0],cy:mean[1],r:5},
                     style:{fill:color,stroke:BACKGROUND,lineWidth:2}}
                  ]};
                }""".replace("COLOR", json.dumps(color)).replace(
                    "BACKGROUND", json.dumps(palette(dark)["tooltip"])
                ),
                "data": data,
            }
        ],
    }


def distribution_options(snapshot: Snapshot, dark: bool, running: bool = True) -> dict[str, Any]:
    colors = (
        ["#8cb5d3", "#69b8c2", "#71c6a1", "#a3cc83", "#d4c67b", "#d6a07c", "#d78f9b"]
        if dark
        else ["#5984a3", "#398e9b", "#449b78", "#709e49", "#ae943b", "#ba7548", "#b65e72"]
    )
    series = []
    for index, bucket in enumerate(BINS):
        points = []
        for device in snapshot.devices:
            stats = device.running_stats if running else device.all_stats
            points.append(
                {
                    "value": stats.bins[index] / stats.count * 100 if stats.count else None,
                    "count": stats.bins[index],
                    "total": stats.count,
                }
            )
        series.append(
            {
                "name": bucket.name,
                "type": "bar",
                "stack": "temperature-bins",
                "barMaxWidth": 30,
                "itemStyle": {"color": colors[index]},
                "data": points,
            }
        )
    return {
        **_base(dark),
        "grid": {"left": 58, "right": 22, "top": 30, "bottom": 88},
        "legend": {"bottom": 0, "textStyle": {"color": palette(dark)["text"]}},
        "tooltip": {
            **_base(dark)["tooltip"],
            ":formatter": """p => p.name+' · '+p.seriesName+'<br>'
              +(p.data.total ? Number(p.value).toFixed(1)+'% · '+p.data.count+'/'+p.data.total
              +' 个有效采样' : '无有效采样')""",
        },
        "xAxis": {**_axis(dark), "type": "value", "max": 100, "name": "%"},
        "yAxis": {
            **_axis(dark),
            "type": "category",
            "inverse": True,
            "data": [device.spec.id for device in snapshot.devices],
            "splitLine": {"show": False},
        },
        "series": series,
    }


def daily_options(device: DeviceResult, dark: bool, running: bool = True) -> dict[str, Any]:
    colors = palette(dark)
    statistics = [day.running_stats if running else day.all_stats for day in device.daily]
    return {
        **_base(dark),
        "legend": {
            "top": 0,
            "data": ["最低", "最高", "样本平均"],
            "textStyle": {"color": colors["text"]},
        },
        "tooltip": {**_base(dark)["tooltip"], "trigger": "axis"},
        "xAxis": {
            **_axis(dark),
            "type": "category",
            "data": [day.window.day.isoformat() for day in device.daily],
            "axisLabel": {"color": colors["text"], "hideOverlap": True},
        },
        "yAxis": {**_axis(dark), "type": "value", "name": "回风温度 (℃)", "scale": True},
        "series": [
            {
                "name": "范围基线",
                "type": "line",
                "stack": "range",
                "symbol": "none",
                "silent": True,
                "tooltip": {"show": False},
                "lineStyle": {"opacity": 0},
                "data": [s.minimum for s in statistics],
            },
            {
                "name": "范围带",
                "type": "line",
                "stack": "range",
                "symbol": "none",
                "silent": True,
                "tooltip": {"show": False},
                "lineStyle": {"opacity": 0},
                "areaStyle": {"color": colors["power"], "opacity": 0.12},
                "data": [
                    s.maximum - s.minimum
                    if s.maximum is not None and s.minimum is not None
                    else None
                    for s in statistics
                ],
            },
            *[
                {
                    "name": name,
                    "type": "line",
                    "connectNulls": False,
                    "showSymbol": field == "mean",
                    "symbolSize": 5,
                    "itemStyle": {"color": color},
                    "lineStyle": {"width": 2 if field == "mean" else 1},
                    "data": [getattr(s, field) for s in statistics],
                }
                for name, field, color in (
                    ("最低", "minimum", colors["temperature"]),
                    ("最高", "maximum", colors["temperature"]),
                    ("样本平均", "mean", colors["power"]),
                )
            ],
        ],
    }


def detail_options(device: DeviceResult, day: str, dark: bool) -> dict[str, Any]:
    """仅绘制一个窗口，三条轨道分轴；运行状态未知保留空值。"""
    rows = [row for row in device.rows if row.day.isoformat() == day]
    colors = palette(dark)
    time_label = {
        "color": colors["text"],
        "hideOverlap": True,
        ":formatter": """v => new Date(v).toLocaleTimeString('zh-CN',
          {timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false})""",
    }
    return {
        **_base(dark),
        "grid": [
            {"left": 66, "right": 42, "top": 35, "height": "31%"},
            {"left": 66, "right": 42, "top": "46%", "height": "17%"},
            {"left": 66, "right": 42, "top": "75%", "height": "12%"},
        ],
        "tooltip": {
            **_base(dark)["tooltip"],
            "trigger": "axis",
            ":formatter": """items => {
              if(!items.length)return '';
              return new Date(items[0].value[0]).toLocaleString('zh-CN',
                {timeZone:'Asia/Shanghai',hour12:false})+'<br>'+items.map(p=>{
                  const value = p.value[1]===null ? '缺失' : p.seriesName==='运行判定'
                    ? ['确认停机','确认开机','状态矛盾','状态未知'][p.value[1]] : p.value[1];
                  return p.marker+p.seriesName+': '+value;
                }).join('<br>');
            }""",
        },
        "legend": {"top": 0, "textStyle": {"color": colors["text"]}},
        "xAxis": [
            {**_axis(dark), "type": "time", "gridIndex": i, "axisLabel": time_label}
            for i in range(3)
        ],
        "yAxis": [
            {**_axis(dark), "name": "回风 ℃", "type": "value", "scale": True},
            {**_axis(dark), "name": "频率 Hz", "type": "value", "gridIndex": 1},
            {
                **_axis(dark),
                "name": "反馈",
                "type": "value",
                "gridIndex": 1,
                "min": 0,
                "max": 1,
                "interval": 1,
                "position": "right",
                "splitLine": {"show": False},
            },
            {
                **_axis(dark),
                "type": "value",
                "gridIndex": 2,
                "min": 0,
                "max": 3,
                "interval": 1,
                "axisLabel": {
                    "color": colors["text"],
                    ":formatter": "v => ['停机','开机','矛盾','未知'][v] || ''",
                },
            },
        ],
        "dataZoom": [
            {"type": "inside", "xAxisIndex": [0, 1, 2], "filterMode": "none"},
            {"type": "slider", "xAxisIndex": [0, 1, 2], "bottom": 0, "height": 16},
        ],
        "series": [
            {
                "name": name,
                "type": "line",
                "step": "end" if field == "feedback" else False,
                "xAxisIndex": x_axis,
                "yAxisIndex": y_axis,
                # 1分钟网格可能隔点缺测；保留独立有效点，不能因断线且隐藏标记而整条消失。
                "showSymbol": True,
                "showAllSymbol": True,
                "symbolSize": 3,
                "connectNulls": False,
                "itemStyle": {"color": color},
                "data": [[row.time.timestamp() * 1000, getattr(row, field)] for row in rows],
            }
            for name, field, x_axis, y_axis, color in (
                ("回风温度", "temperature", 0, 0, colors["power"]),
                ("频率", "frequency", 1, 1, colors["temperature"]),
                ("运行反馈", "feedback", 1, 2, "#93a4b9" if dark else "#718198"),
            )
        ]
        + [
            {
                "name": "运行判定",
                "type": "line",
                "step": "end",
                "xAxisIndex": 2,
                "yAxisIndex": 3,
                "showSymbol": False,
                "itemStyle": {"color": colors["power"]},
                "data": [
                    [
                        row.time.timestamp() * 1000,
                        {"stopped": 0, "running": 1, "conflict": 2, "unknown": 3}[row.state],
                    ]
                    for row in rows
                ],
            }
        ],
    }
