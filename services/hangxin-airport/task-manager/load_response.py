"""把真实采样曲线转换为最初版负荷接口的区间结束时间结构。

兼容层将左端采样负荷作为接下来一个区间的平均负荷估计；这是一种
分段保持近似，并非平台提供了已计量的区间平均值。缓存本身不改写。
"""

from copy import deepcopy
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")


def format_load_response(samples: dict, at: datetime, *, details: bool = False) -> dict:
    """主响应保持原10个字段，诊断元数据仅在details中返回。"""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("当前时间必须带有时区")
    local_at = at.astimezone(TIMEZONE)
    minutes = samples["interval_minutes"]
    interval = timedelta(minutes=minutes)
    points = []
    actual_completed = []
    predicted_completed = []
    for source in samples["points"]:
        start = datetime.fromisoformat(source["time"]).astimezone(TIMEZONE)
        end = start + interval
        completed = end <= local_at
        actual = source["actual_load_kw"] if completed else None
        predicted = source["predicted_load_kw"]
        points.append({"time": end.isoformat(),
                       "actual_load_kw": actual,
                       "predicted_load_kw": predicted})
        if completed:
            actual_completed.append(actual)
            predicted_completed.append(predicted)

    def energy(values):
        # 尚未完成任何区间时积分为空集，累计为0；缺测不能当成0累计。
        if any(value is None for value in values):
            return None
        return round(math.fsum(value * minutes / 60 for value in values), 4)

    result = {
        "date": samples["date"],
        "timezone": samples["timezone"],
        "scope": samples["scope"],
        "source": samples["source"],
        "interval_minutes": minutes,
        "load_unit": samples["load_unit"],
        "energy_unit": samples["energy_unit"],
        "actual_energy_kwh": energy(actual_completed),
        "predicted_energy_kwh": energy(predicted_completed),
        "points": points,
    }
    if details:
        # 深拷贝诊断字段，防止调用者意外改变源响应或缓存元数据。
        result.update({key: deepcopy(value) for key, value in samples.items()
                       if key not in result})
        result.update({
            "time_semantics": "interval_end",
            "source_time_semantics": "sample_time",
            "energy_method": "left_sample_hold_estimate",
            "energy_note": "每个区间采用左端采样负荷作平均负荷估计；累计仅到已完成区间。有已完成区间缺测时，对应累计为null。",
            "energy_coverage": {
                "completed_interval_count": len(actual_completed),
                "actual_valid_interval_count": sum(value is not None for value in actual_completed),
                "predicted_valid_interval_count": sum(value is not None for value in predicted_completed),
                "completed_minutes": len(actual_completed) * minutes,
            },
        })
    return result
