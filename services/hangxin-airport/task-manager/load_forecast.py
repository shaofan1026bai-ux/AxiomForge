"""全部空调主机合计负荷的确定性模拟数据。

每个点表示其时间戳之前完整两分钟区间的平均负荷（kW）。
时间戳为区间结束时间：首点 00:02，完整一天的末点为次日 00:00。
始终返回全天预测，未结束区间的实际值为 None。
两个累计能量（kWh）字段均只统计已结束区间，保持同一时间窗口。
已结束区间的预测值暂时等于实际值，所有数据均为模拟数据。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
INTERVAL_MINUTES = 2
POINTS_PER_DAY = 24 * 60 // INTERVAL_MINUTES


def _simulated_load_kw(day: date, interval_index: int) -> float:
    """按日期和区间位置生成稳定、平滑的模拟平均负荷。"""
    minute = (interval_index + 0.5) * INTERVAL_MINUTES
    hour = minute / 60
    phase = (day.toordinal() % 31) * math.tau / 31
    daily_scale = 0.94 + (day.toordinal() % 13) * 0.01
    daytime = 300 * math.exp(-((hour - 14) / 5) ** 2)
    evening = 100 * math.exp(-((hour - 21) / 2.5) ** 2)
    ripple = 12 * math.sin(math.tau * minute / 180 + phase)
    ripple += 5 * math.sin(math.tau * minute / 32 + phase)
    return round((430 + daytime + evening + ripple) * daily_scale, 2)


def make_load_forecast(at: datetime, target_date: date | None = None) -> dict:
    """返回指定北京时间日期的实际／预测负荷及已完成区间累计能量。

    默认查询今日，今日和过去日期均返回全天 720 个预测点。只有在 ``at``
    之前已完成的区间才有实际值，未来实际值为 None。未来日期或无时区的
    ``at`` 会引发 ValueError。
    同一日期、同一区间的模拟值固定，不随刷新和进程重启变化。
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("当前时间必须带有时区")
    local_at = at.astimezone(TIMEZONE)
    day = target_date if target_date is not None else local_at.date()
    if day > local_at.date():
        raise ValueError("不能查询未来日期的实际负荷")

    start = datetime.combine(day, time.min, tzinfo=TIMEZONE)
    points = []
    for index in range(POINTS_PER_DAY):
        interval_end = start + timedelta(minutes=(index + 1) * INTERVAL_MINUTES)
        predicted = _simulated_load_kw(day, index)
        actual = predicted if interval_end <= local_at else None
        points.append({
            "time": interval_end.isoformat(),
            "actual_load_kw": actual,
            "predicted_load_kw": predicted,
        })
    energy = round(math.fsum(
        point["actual_load_kw"] for point in points if point["actual_load_kw"] is not None
    ) / 30, 4)
    return {
        "date": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "scope": "all_chillers",
        "source": "mock",
        "interval_minutes": INTERVAL_MINUTES,
        "load_unit": "kW",
        "energy_unit": "kWh",
        "actual_energy_kwh": energy,
        "predicted_energy_kwh": energy,
        "points": points,
    }
