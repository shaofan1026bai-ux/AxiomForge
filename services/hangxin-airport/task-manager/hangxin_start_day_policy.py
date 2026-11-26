"""17点低温时将开机表移至已过的12点；保留旧时控记录的只读确认。"""
from datetime import datetime, timedelta
import json
import math
from pathlib import Path

from hangxin_start_platform import (
    StartError, eligible_ahus, point_stamp, ZONE, number, _identity,
    TIME_CONTROL_ID, TIME_CONTROL_SPEC, CHILLERS,
)

OPERATIONS={'disable_time_control','restore_time_control'}
FIRST_HOUR=8
FINAL_HOUR=17
CONFIG_PATH=Path(__file__).with_name('start_day_policy_config.json')


def local(value):
    if isinstance(value,str):value=datetime.fromisoformat(value)
    if not isinstance(value,datetime) or value.tzinfo is None:raise StartError('时控操作时刻必须带时区')
    return value.astimezone(ZONE)


def settings(path=CONFIG_PATH):
    try:
        cfg=json.loads(Path(path).read_text())
        if (cfg.get('version')!='start-noon-deferral-v3' or cfg.get('final_hour')!=FINAL_HOUR
            or cfg.get('defer_start_hour')!=12 or cfg.get('defer_start_minute')!=0
            or cfg.get('schedule_trigger_semantics')!='point_in_time_no_catch_up'
            or type(cfg.get('confirmation_seconds'))is not int or not 180<=cfg['confirmation_seconds']<=600
            or cfg.get('maximum_final_horizon_minutes')!=540):raise ValueError
        return cfg
    except Exception:raise StartError('低温日延至次日配置无效') from None


def control_value(points,at):
    """同时核对开关身份、可写权限、运行停止枚举和设备原始上报。"""
    point=points.get(TIME_CONTROL_ID)
    _identity(point,TIME_CONTROL_ID,TIME_CONTROL_SPEC,writable=True)
    if (point.get('pointGroupName')!='自控系统参数' or point.get('pointEnumName')!='运行停止'
        or point.get('dataType')!=1):raise StartError('系统时控点分组或枚举不符')
    enums=point.get('pointEnumItemList')
    if not isinstance(enums,list) or len(enums)!=2:raise StartError('时控枚举缺失')
    parsed={}
    for row in enums:
        value=row.get('pointEnumItemValue')
        if type(value)not in (int,float) or value not in (0,1):raise StartError('时控枚举值无效')
        if int(value) in parsed:raise StartError('时控枚举重复')
        parsed[int(value)]=row.get('pointEnumItemKey')
    if parsed!={0:'停止',1:'运行'}:raise StartError('时控运行/停止枚举与核验依据不符')
    value=number(point,at)
    if value not in (0,1):raise StartError('系统时控值必须为0或1')
    return int(value)


def assert_all_stopped(points,at):
    """系统开关影响冷站主机，1#、2#任一启动、运行、故障或缺测均不操作。"""
    for device,prefix in CHILLERS.items():
        for tag,name in (('c00','主机运行反馈'),('c23','主机启停输出'),('c22','主机故障反馈')):
            pid=prefix+tag;point=points.get(pid)
            _identity(point,pid,(device,tag,name,''),writable=False)
            if number(point,at)!=0:raise StartError('受时控影响的主机已运行、启动或故障，停止操作')


def schedules(points,at):
    """保存并保护两台主机全部三组时表，不授予这些字段写权限。"""
    result={}
    for device,prefix in CHILLERS.items():
        for index in (1,2,3):
            for tag,name,upper in ((f't0{index}',f'开机时间设定{index}_时',23),
                 (f't0{index+3}',f'开机时间设定{index}_分',59),
                 (f't1{index}',f'关机时间设定{index}_时',23),
                 (f't1{index+3}',f'关机时间设定{index}_分',59)):
                pid=prefix+tag;point=points.get(pid)
                _identity(point,pid,(device,tag,name,''),writable=True)
                value=number(point,at)
                if int(value)!=value or not 0<=value<=upper:raise StartError('受保护时间表字段越界')
                result[pid]=int(value)
    return result


def cancellation_assessment(prediction,at,stop_at,*,manual=False,authorized_final_replay=False):
    """判断到当天原关机时刻，不把四小时未触界直接当作全天不需要制冷。"""
    at,stop_at=local(at),local(stop_at)
    result={'allowed':False,'assessment_start':at.isoformat(),'assessment_end':stop_at.isoformat()}
    try:
        group=prediction['group_prediction'];weather=prediction['weather_prediction']
        boundary=group['return_boundary_c'];curve=group['group_curve']
        in_window=at.hour==FINAL_HOUR and (manual is True or 0<=at.minute*60+at.second<120)
        if authorized_final_replay is True and manual is True and at.hour>=FINAL_HOUR:in_window=True
        if (not in_window or stop_at<=at or prediction.get('automatic_adjustment_allowed') is False
            or stop_at.date()!=at.date() or prediction.get('status')!='tentative'
            or group.get('method')!='mean' or group.get('member_count',0)<2
            or len(set(group['members']))!=group['member_count'] or not set(group['members']).issubset({1,2,4,5})
            or weather.get('used')is not True or weather.get('city_id')!='101291508'
            or local(weather['range_start'])>at or local(weather['range_end'])<stop_at
            or local(prediction['evaluated_at'])!=at or type(boundary) not in (int,float)
            or not math.isfinite(boundary) or abs(boundary-28.2)>1e-8
            or prediction.get('individual_hotspots') or group.get('short_term_guard_crossing_at')):raise ValueError
        rows=[(local(row['at']),row['return_c']) for row in curve]
        if not rows or rows[0][0]!=at or rows[-1][0]<stop_at:raise ValueError
        if any(type(v) not in (int,float) or not math.isfinite(v) or not 10<=v<=45 for _,v in rows):raise ValueError
        if any(b[0]-a[0]!=timedelta(minutes=5) for a,b in zip(rows,rows[1:])):raise ValueError
        # 末端向上对齐的额外数分钟也必须低于边界，避免截断低估升温。
        maximum=max(v for _,v in rows)
        result.update(allowed=maximum<boundary,maximum_group_return_c=maximum,return_boundary_c=boundary,
                      weather=weather,reason='至关机时刻全程低于开机预留边界' if maximum<boundary else '剩余时段仍预测需要开机')
    except (KeyError,ValueError,TypeError,OverflowError):result['reason']='预测、天气覆盖或当前温度未满足取消当天开机的完整条件'
    return result


def full_confirmed(points,record,at):
    """唯一控制点必须有写后新回报，全部主机时间表须保持不变。"""
    try:
        writes=record['writes']
        return (len(writes)==1 and record['changes']==[{'point_id':TIME_CONTROL_ID,'value':record['control_target']}]
            and writes[0]['point_id']==TIME_CONTROL_ID and writes[0]['value']==record['control_target']
            and control_value(points,at)==record['control_target']
            and schedules(points,at)==record['protected_schedules']
            and point_stamp(points[TIME_CONTROL_ID])>local(writes[0]['attempted_at']))
    except (KeyError,TypeError,ValueError,OverflowError):return False


def confirmation_state(state,record,at):
    """只有算法真实从1改0并确认后才拥有次日恢复资格；人工原本停用不接管。"""
    state=dict(state or {})
    if record['operation']=='disable_time_control':
        state['time_control_hold']={'version':'start-time-control-v2','status':'disabled',
            'disabled_on':local(record['slot']).date().isoformat(),'disabled_at':local(at).isoformat(),
            'point_id':TIME_CONTROL_ID,'before_value':1,'protected_schedules':record['protected_schedules']}
    else:
        state['last_time_control_restore']={'restored_at':local(at).isoformat(),'disabled_on':record['disabled_on']}
        state.pop('time_control_hold',None)
    return state
