"""按航信现场回风和冷机运行反馈试算AHU启停，仅记录判断结果。"""

import json
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from algorithms.ahu_start_policy.policy import ZONE, evaluate, sample_value
from hangxin_start_platform import CHILLERS, READ_PATH, Platform, _identity, point_stamp

AHU_IDS = {f'AHU{n}': 12782 + n for n in range(1, 6)}


def collect_feedback():
    """一次查询五台AHU和两台冷机，校验点位身份后保留设备上报时间。"""
    rows = Platform()._read_request(
        READ_PATH, {'deviceIdList': [*AHU_IDS.values(), *CHILLERS], 'isActivate': True}
    )
    points = {}
    duplicates = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('pointId'), str):
            continue
        pid = row['pointId']
        if pid in points:
            duplicates.add(pid)
        points[pid] = row

    def sample(pid, spec, boolean=False):
        if pid in duplicates:
            return None
        point = points.get(pid)
        try:
            _identity(point, pid, spec)
            updated_at = point_stamp(point).isoformat()
        except (ValueError, TypeError, KeyError):
            return None
        value = point.get('value')
        if boolean:
            if type(value) not in (int, float) or value not in (0, 1):
                return None
            value = bool(value)
        return {'value': value, 'updated_at': updated_at}

    ahus = []
    for ahu, device in AHU_IDS.items():
        pid = f'ewcnad_msahu{device - 12782}_airin'
        ahus.append({'ahu_id': ahu, 'return_temperature_c': sample(pid, (device, 'airin', '回风温度', '℃'))})
    chillers = [
        sample(prefix + 'c00', (device, 'c00', '主机运行反馈', ''), boolean=True)
        for device, prefix in CHILLERS.items()
    ]
    return ahus, chillers


def main():
    """缺少经确认的区域需求信号时，保留未知并输出实测数据可用情况。"""
    ahus = [{'ahu_id': ahu} for ahu in AHU_IDS]
    chillers = []
    error = None
    try:
        ahus, chillers = collect_feedback()
    except Exception as exc:
        error = type(exc).__name__

    at = datetime.now(ZONE)
    result = evaluate({'at': at, 'ahus': ahus, 'chillers_running': chillers})
    result['mode'] = 'preview'
    result['missing_inputs'] = ['occupied', 'flight_support', 'precooling']
    result['live_return_temperature_c'] = {
        ahu['ahu_id']: sample_value(ahu.get('return_temperature_c'), at) for ahu in ahus
    }
    result['live_chillers_running'] = {
        str(device): sample_value(sample, at, boolean=True) for device, sample in zip(CHILLERS, chillers)
    }
    if error:
        result['read_error'] = error
    print(json.dumps(result, ensure_ascii=False))
    return 1 if error else 0


if __name__ == '__main__':
    raise SystemExit(main())
