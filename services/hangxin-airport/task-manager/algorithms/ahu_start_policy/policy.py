"""航信AHU启停纯判断：26℃，北京时间23:00至次日07:00夜间停机。"""
from datetime import datetime
import math
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
SETPOINT_C = 26.0
MAX_AGE_SECONDS = 180


def local_time(value):
    """拒绝无时区时间，避免跨午夜与服务器时区误判。"""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('时间必须带时区')
    return value.astimezone(ZONE)


def sample_value(sample, at, boolean=False):
    """未知、过期、未来值不补0；布尔信号不接受数值或字符串冒充。"""
    if not isinstance(sample, dict):
        return None
    try:
        age = (at - local_time(sample['updated_at'])).total_seconds()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not 0 <= age <= MAX_AGE_SECONDS:
        return None
    value = sample.get('value')
    if boolean:
        return value if type(value) is bool else None
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def any_known(values):
    """三值或：任一明确为真即可放行；全为假才判断为假。"""
    if any(value is True for value in values):
        return True
    if values and all(value is False for value in values):
        return False
    return None


def decide_ahu(ahu, at, chillers):
    """返回目标状态，None表示暂不判断该输出；本函数不执行任何现场写入。"""
    at = local_time(at)
    result = {'ahu_id': ahu['ahu_id'], 'at': at.isoformat(), 'setpoint_c': SETPOINT_C,
              'ahu_run': None, 'fresh_air_open': None, 'reason': '', 'device_control_sent': False}

    def finish(fan, fresh, reason):
        result.update(ahu_run=fan, fresh_air_open=fresh, reason=reason)
        return result

    if at.hour >= 23 or at.hour < 7:
        return finish(False, False, 'night_shutdown')
    demand = any_known([sample_value(ahu.get(key), at, boolean=True)
                        for key in ('occupied', 'flight_support', 'precooling')])
    if demand is False:
        return finish(False, False, 'no_occupancy_or_service')
    if demand is None:
        return finish(None, None, 'service_data_unknown')
    temperature = sample_value(ahu.get('return_temperature_c'), at)
    if temperature is None:
        return finish(None, None, 'return_temperature_unknown')
    result['return_temperature_c'] = temperature
    if temperature < SETPOINT_C:
        return finish(False, False, 'below_setpoint')
    running = any_known([sample_value(sample, at, boolean=True) for sample in chillers])
    if running is None:
        return finish(True, None, 'chiller_feedback_unknown')
    return finish(True, not running, 'mechanical_cooling' if running else 'fresh_air_cooling')


def evaluate(payload):
    """按各AHU区域需求与各自回风独立判断，共享所服务冷水机组的实际运行反馈。"""
    at = local_time(payload['at'])
    ahus = payload['ahus']
    chillers = payload['chillers_running']
    if not isinstance(ahus, list) or not isinstance(chillers, list):
        raise ValueError('AHU与冷机反馈必须为列表')
    ids = [ahu['ahu_id'] for ahu in ahus]
    if any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
        raise ValueError('AHU标识必须为非空且不重复的字符串')
    return {'at': at.isoformat(), 'decisions': [decide_ahu(ahu, at, chillers) for ahu in ahus],
            'device_control_sent': False}


if __name__ == '__main__':
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='带时间戳的JSON输入文件')
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.input.read_text())), ensure_ascii=False, indent=2))
