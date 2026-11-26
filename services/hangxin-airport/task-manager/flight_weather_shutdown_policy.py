"""按候选半点、全场航班空档和未来室外温度选择最早提前关机时刻。"""
from datetime import datetime, timedelta
import json
from pathlib import Path

from nowapi_weather import ZONE, WeatherError, interval_temperature
from hangxin_flight_shutdown_platform import ShutdownError

ROOT=Path(__file__).resolve().parent
CANDIDATE_TIMES=['18:30','19:00','19:30','20:00','20:30']
EXECUTION_TIMES=['18:00','18:30','19:00','19:30','20:00','20:30']


def policy_config(path=ROOT/'flight_shutdown_config.json'):
    try:
        cfg=json.loads(Path(path).read_text())
        if (cfg.get('version')!='flight-weather-half-hour-v2'
            or not cfg.get('candidate_times') or sorted(set(cfg['candidate_times']))!=cfg['candidate_times']
            or not set(cfg['candidate_times']).issubset(CANDIDATE_TIMES)
            or type(cfg.get('minimum_gap_minutes'))is not int or not 30<=cfg['minimum_gap_minutes']<=180
            or type(cfg.get('outdoor_temperature_limit_c'))not in (int,float) or not 15<=cfg['outdoor_temperature_limit_c']<=35
            or cfg.get('freeze_minutes')!=5 or cfg.get('confirmation_budget_minutes')!=5):raise ValueError
        return cfg
    except Exception:raise ShutdownError('航班天气关机配置无效') from None


def stamp(value):
    if isinstance(value,str):value=datetime.fromisoformat(value)
    if not isinstance(value,datetime) or value.tzinfo is None:raise ShutdownError('航班空档时间必须带时区')
    return value.astimezone(ZONE)


def select_shutdown(result,day,weather,at,current=None,config=None):
    """候选严格落在真实相邻航班之间；不外推首末航班之外的空档。"""
    cfg=config or policy_config();at=stamp(at)
    area=result['modes']['dynamic']['areas']['all']
    if (result.get('date')!=day.isoformat() or result.get('airport')!='LUM'
        or result.get('timezone')!='Asia/Shanghai' or result.get('minimum_minutes')!=30
        or result.get('selected_day_flights',0)<1 or result.get('raw_count',0)<1
        or result.get('warnings') or result.get('normalization_counts',{}).get('scope_rejected',0)
        or area.get('quality_flags')):raise ShutdownError('航班资料或全场空档质量未满足自动改表条件')
    checks=[]
    for clock in cfg['candidate_times']:
        target=datetime.combine(day,datetime.strptime(clock,'%H:%M').time(),ZONE)
        check={'target_at':target.isoformat()};checks.append(check)
        if target<=at+timedelta(minutes=cfg['freeze_minutes']+cfg['confirmation_budget_minutes']):
            check['reason']='目标已过或不足改表提前量';continue
        if current is not None and target>stamp(current):
            check['reason']='仅提前关机，不延后现场当前关机时间';continue
        gap=None
        for item in area.get('gaps',[]):
            start,end=stamp(item['start']),stamp(item['end'])
            duration=(end-start).total_seconds()/60
            if abs(duration-item['duration_minutes'])>0.001:raise ShutdownError('航班空档长度与端点不符')
            if start<target<end and duration>cfg['minimum_gap_minutes']:gap=item;break
        if gap is None:
            check['reason']='不在超过阈值的航班空档内部';continue
        try:assessment=interval_temperature(weather,target,stamp(gap['end']),at,cfg['outdoor_temperature_limit_c'])
        except (WeatherError,KeyError,TypeError,ValueError):
            check['reason']='天气数据无效、过期或未覆盖到空档结束';continue
        check.update(weather=assessment,gap_start=gap['start'],gap_end=gap['end'],gap_duration_minutes=gap['duration_minutes'])
        if not assessment['allowed']:
            check['reason']='候选至空档结束的预测温度未全程低于阈值';continue
        check['reason']='满足全场空档及预测温度条件'
        return target,{'status':'eligible','selected':check,'candidates':checks,'weather_fetched_at':weather['fetched_at'],
                       'shutdown_policy':cfg['version'],'later_flights_block_shutdown':False}
    return None,{'status':'no_candidate','candidates':checks,'shutdown_policy':cfg['version']}
