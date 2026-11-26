"""1F AHU回风温调的只读数据校验与趋势判断，不包含认证或控制请求。"""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo


ZONE = ZoneInfo('Asia/Shanghai')
AHU_IDS = {1: 12783, 2: 12784, 4: 12786, 5: 12787}
DEVICE_IDS = [12788, *AHU_IDS.values()]
MASTER_RUNNING_ID = 'nfdh9q_mscrk01_c00'
OUTDOOR_ID = 'nfdh9q_mscrk01_c21'
SPECS = {MASTER_RUNNING_ID: (12788, 'c00', '主机运行反馈', ''),
         OUTDOOR_ID: (12788, 'c21', '室外温度', '℃')}
for _ahu, _device in AHU_IDS.items():
    for _tag, _name, _unit in (
        ('airin', '回风温度', '℃'),
        ('v20', '变频器运行I', ''),
        ('v01', '变频器运行频率', 'hz'),
    ):
        SPECS[f'ewcnad_msahu{_ahu}_{_tag}'] = (_device, _tag, _name, _unit)
HISTORY_IDS = list(SPECS)
MAX_AGE_SECONDS = 180
MAX_GAP_SECONDS = 180


class FeedbackError(ValueError):
    """固定校验错误，不携带平台响应或认证信息。"""


def _time(raw):
    try:
        value = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
        if not isinstance(value, datetime):
            raise ValueError
        return value.replace(tzinfo=ZONE) if value.tzinfo is None else value.astimezone(ZONE)
    except (ValueError, TypeError, OverflowError):
        raise FeedbackError('采样时间无效') from None


def _at(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FeedbackError('判断时刻必须带时区')
    return value.astimezone(ZONE)


def _number(raw):
    try:
        valid = type(raw) in (int, float) and math.isfinite(raw)
    except (ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        raise FeedbackError('采样值不是有效有限数字')
    return raw


def _current(points, pid, at, maximum=MAX_AGE_SECONDS):
    spec = SPECS[pid]
    point = points.get(pid) if isinstance(points, dict) else None
    device, tag, name, unit = spec
    if (not isinstance(point, dict) or point.get('pointId') != pid
            or point.get('deviceId') != device or point.get('projectId') != 832
            or point.get('identifierName') != tag or point.get('pointName') != name
            or point.get('unit') != unit or point.get('isWrite') is not False):
        raise FeedbackError('当前测点身份、单位或权限不符')
    value = _number(point.get('value'))
    stamp = _time(point.get('updateTime'))
    if not 0 <= (at - stamp).total_seconds() <= maximum:
        raise FeedbackError('当前测点上报陈旧或晚于判断时刻')
    return value, stamp


def parse_histories(rows, at):
    """仅解析固定14个历史序列，保留缺测；最终判断另行隔离未来样本。"""
    at = _at(at)
    start = (at - timedelta(minutes=35)).replace(second=0, microsecond=0)
    end = (at + timedelta(minutes=1)).replace(second=0, microsecond=0)
    if not isinstance(rows, list):
        raise FeedbackError('历史序列结构无效')
    result = {}
    for series in rows:
        if not isinstance(series, dict):
            raise FeedbackError('历史序列结构无效')
        pid = series.get('pointId')
        if pid not in SPECS or pid in result:
            raise FeedbackError('历史含未知或重复序列')
        device, tag, _, unit = SPECS[pid]
        if (series.get('deviceId') != device or series.get('identifierName') != tag
                or series.get('unit') != unit or not isinstance(series.get('pointDataList'), list)):
            raise FeedbackError('历史测点身份或单位不符')
        samples, seen = [], set()
        for row in series['pointDataList']:
            if not isinstance(row, dict):
                raise FeedbackError('历史样本结构无效')
            stamp = _time(row.get('ts'))
            if stamp in seen or not start <= stamp <= end:
                raise FeedbackError('历史采样时间重复或越界')
            seen.add(stamp)
            value = row.get('val')
            if value is not None:
                _number(value)
            samples.append({'at': stamp.isoformat(), 'value': value})
        result[pid] = sorted(samples, key=lambda sample: sample['at'])
    return result


def _samples(histories, pid, at, current, minutes, *, cover_start=False):
    """真实当前采样作为末点；内部缺测和未来非空历史均阻止上调。"""
    source = histories.get(pid, []) if isinstance(histories, dict) else []
    if not isinstance(source, list) or not source:
        raise FeedbackError('历史序列缺失')
    current_value, current_at = current
    start = at - timedelta(minutes=minutes)
    lower = start - timedelta(seconds=MAX_GAP_SECONDS) if cover_start else start
    samples = {}
    for row in source:
        if not isinstance(row, dict):
            raise FeedbackError('历史样本结构无效')
        stamp = _time(row.get('at'))
        if stamp > at or stamp > current_at:
            # 平台会返回尚未形成设备采样的尾部空桶，仅排除，不补值或改时间。
            if row.get('value') is None:
                continue
            raise FeedbackError('历史含晚于当前真实采样的值')
        if stamp < lower:
            continue
        value = row.get('value')
        if value is None:
            raise FeedbackError('有效窗口内存在历史缺测')
        value = _number(value)
        if stamp in samples and samples[stamp] != value:
            raise FeedbackError('同刻历史采样冲突')
        samples[stamp] = value
    if current_at in samples and samples[current_at] != current_value:
        raise FeedbackError('历史与当前同刻采样冲突')
    samples[current_at] = current_value
    ordered = sorted(samples.items())
    if cover_start:
        previous = [row for row in ordered if row[0] <= start]
        if not previous or (start - previous[-1][0]).total_seconds() > MAX_GAP_SECONDS:
            raise FeedbackError('连续运行窗口缺少起点样本')
        ordered = [previous[-1], *[row for row in ordered if row[0] > start]]
    if len(ordered) < 2 or any((right[0] - left[0]).total_seconds() > MAX_GAP_SECONDS
                               for left, right in zip(ordered, ordered[1:])):
        raise FeedbackError('历史采样不连续')
    return ordered


def _stable_running(histories, pid, at, current, *, frequency=False):
    samples = _samples(histories, pid, at, current, 15, cover_start=True)
    if frequency:
        if any(not 0 < value <= 60 for _, value in samples):
            raise FeedbackError('近15分钟实际频率未连续有效运行')
    elif any(value != 1 for _, value in samples):
        raise FeedbackError('近15分钟运行反馈未连续为1')
    return True


def _trend(histories, pid, at, current):
    samples = _samples(histories, pid, at, current, 30)
    if len(samples) < 3 or (samples[-1][0] - samples[0][0]).total_seconds() < 15 * 60:
        raise FeedbackError('回风趋势样本不足3个或跨度不足15分钟')
    if any(not 10 <= value <= 45 for _, value in samples):
        raise FeedbackError('历史回风温度超出有效范围')
    origin = samples[0][0]
    xs = [(stamp - origin).total_seconds() / 3600 for stamp, _ in samples]
    ys = [float(value) for _, value in samples]
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    divisor = sum((x - x_mean) ** 2 for x in xs)
    if divisor <= 0:
        raise FeedbackError('趋势时间跨度无效')
    # 消除常数序列OLS中的浮点舍入尾差，不把极小正数误判为持续升温。
    slope = round(sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / divisor, 9)
    projection = float(current[0]) + max(0.0, slope) * 0.5
    return {'slope_c_per_hour': slope, 'projected_return_c': projection,
            'trend_sample_count': len(samples), 'trend_start_at': samples[0][0].isoformat(),
            'trend_end_at': samples[-1][0].isoformat()}


def _outdoor_feedback(points, histories, at):
    """室外只限制上调，半小时升温门槛不独立触发降低出水设定。"""
    result = {'valid': False, 'current_c': None, 'change_30m_c': None,
              'span_minutes': None, 'reason': '室外数据未通过校验'}
    try:
        current = _current(points, OUTDOOR_ID, at, maximum=300)
        if not -30 <= current[0] <= 60:
            raise FeedbackError('室外当前温度超出有效范围')
        result['current_c'] = float(current[0])
        samples = _samples(histories, OUTDOOR_ID, at, current, 30)
        span = (samples[-1][0] - samples[0][0]).total_seconds() / 60
        if len(samples) < 3 or span < 25:
            raise FeedbackError('室外趋势样本跨度不足25分钟')
        if any(not -30 <= value <= 60 for _, value in samples):
            raise FeedbackError('历史室外温度超出有效范围')
        # 直接取实际窗口首末净变化，不把短窗口的变化倍增外推。
        change = round(float(samples[-1][1]) - float(samples[0][1]), 9)
        result.update(valid=True, change_30m_c=change, span_minutes=span,
                      start_at=samples[0][0].isoformat(), end_at=samples[-1][0].isoformat(),
                      reason='室外半小时净升温达到1℃' if change >= 1 else '室外半小时净升温小于1℃')
    except FeedbackError as exc:
        result['reason'] = str(exc)
    return result


def assess_feedback(points, histories, at):
    """取运行1F AHU最高回风，历史不足仍保留当前超温以供主控降水温。"""
    at = _at(at)
    blockers, members, temperatures, projections, details = [], [], [], [], {}
    outdoor = _outdoor_feedback(points, histories, at)
    if not outdoor['valid'] or outdoor['change_30m_c'] >= 1:
        blockers.append('室外:' + outdoor['reason'])
    try:
        master = _current(points, MASTER_RUNNING_ID, at)
        if master[0] != 1:
            raise FeedbackError('主机未明确运行')
        _stable_running(histories, MASTER_RUNNING_ID, at, master)
    except FeedbackError as exc:
        blockers.append('主机:' + str(exc))
    for ahu, device in AHU_IDS.items():
        prefix = f'ewcnad_msahu{ahu}_'
        item = {'ahu': ahu, 'device_id': device, 'status': 'invalid', 'return_c': None,
                'frequency_hz': None, 'sample_at': None, 'slope_c_per_hour': None,
                'projected_return_c': None, 'trend_valid': False, 'continuous_running': False,
                'projection_reliable': False,
                'raise_blockers': []}
        details[str(ahu)] = item
        try:
            running = _current(points, prefix + 'v20', at)
            if running[0] not in (0, 1):
                raise FeedbackError('AHU运行反馈不是0或1')
            if running[0] == 0:
                item.update(status='stopped', running=False)
                continue
            item['running'] = True
            frequency = _current(points, prefix + 'v01', at)
            temperature = _current(points, prefix + 'airin', at)
            if not 0 < frequency[0] <= 60:
                raise FeedbackError('AHU实际频率无效')
            if not 10 <= temperature[0] <= 45:
                raise FeedbackError('AHU当前回风温度超出有效范围')
            item.update(status='running', return_c=float(temperature[0]), frequency_hz=float(frequency[0]),
                        sample_at=temperature[1].isoformat())
            members.append(ahu)
            temperatures.append(float(temperature[0]))
        except FeedbackError as exc:
            item['raise_blockers'].append(str(exc))
            blockers.append(f'AHU{ahu}:' + str(exc))
            continue
        try:
            _stable_running(histories, prefix + 'v20', at, running)
            _stable_running(histories, prefix + 'v01', at, frequency, frequency=True)
            item['continuous_running'] = True
        except FeedbackError as exc:
            item['raise_blockers'].append(str(exc))
            blockers.append(f'AHU{ahu}:' + str(exc))
        try:
            item.update(_trend(histories, prefix + 'airin', at, temperature), trend_valid=True)
            # 刚启风机或运行历史不完整的斜率只作诊断，不能触发预测超温调节。
            if item['continuous_running']:
                item['projection_reliable'] = True
                projections.append(item['projected_return_c'])
            if item['slope_c_per_hour'] > 0:
                item['raise_blockers'].append('回风趋势上升')
                blockers.append(f'AHU{ahu}:回风趋势上升')
        except FeedbackError as exc:
            item['raise_blockers'].append(str(exc))
            blockers.append(f'AHU{ahu}:' + str(exc))
    if not members:
        blockers.append('没有有效运行的1F AHU')
    return {'members': members, 'max_return_c': max(temperatures) if temperatures else None,
            'projected_max_return_c': max(projections) if projections else None,
            'raise_allowed': bool(members) and not blockers,
            'raise_blockers': blockers, 'ahus': details, 'outdoor': outdoor}
