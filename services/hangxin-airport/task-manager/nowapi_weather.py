"""读取NowAPI逐小时预报；保留时刻、单位与来源，不将每日温度冒充逐小时值。"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import threading
from zoneinfo import ZoneInfo

import requests

ZONE=ZoneInfo('Asia/Shanghai')
ROOT=Path(__file__).resolve().parent
ENDPOINT='https://sapi.k780.com/'


class WeatherError(ValueError):
    """对外仅提供固定错误说明，不带请求表单、令牌或上游原文。"""


def local_time(value):
    if isinstance(value,str):value=datetime.fromisoformat(value)
    if not isinstance(value,datetime) or value.tzinfo is None:raise WeatherError('天气时间必须带时区')
    return value.astimezone(ZONE)


def settings(path=ROOT/'nowapi_weather_config.json'):
    try:
        config=json.loads(Path(path).read_text())
        if (not isinstance(config,dict) or not isinstance(config.get('city_name'),str)
            or not 1<=len(config['city_name'])<=40 or not isinstance(config.get('city_aliases'),list)
            or not config['city_aliases'] or any(not isinstance(a,str) or not a for a in config['city_aliases'])
            or config.get('city_id') is not None and not re.fullmatch(r'\d{9}',config['city_id'])
            or type(config.get('cache_seconds'))is not int or not 60<=config['cache_seconds']<=1800
            or type(config.get('maximum_snapshot_age_seconds'))is not int
            or not config['cache_seconds']<=config['maximum_snapshot_age_seconds']<=3600):raise ValueError
        return config
    except Exception:raise WeatherError('天气配置无效') from None


def temperature(value):
    if type(value)is bool or value is None:raise WeatherError('天气温度缺失或无效')
    try:
        if isinstance(value,str) and not re.fullmatch(r'-?\d+(?:\.\d+)?',value.strip()):raise ValueError
        result=float(value)
        if not math.isfinite(result) or not -60<=result<=60:raise ValueError
        return result
    except Exception:raise WeatherError('天气温度缺失或无效') from None


def parse_forecast(payload,config,fetched_at):
    """严格使用futureHour/dateYmdh/wtTemp，供应商未给发布时间时明确留空。"""
    at=local_time(fetched_at)
    try:
        if not isinstance(payload,dict) or str(payload.get('success'))!='1':raise WeatherError('NowAPI鉴权、配额或服务返回失败')
        data=payload.get('result')
        if not isinstance(data,dict):raise WeatherError('NowAPI天气结构无效')
        areas=[data.get(k) for k in ('area_1','area_2','area_3')]
        if not any(a in config['city_aliases'] for a in areas if isinstance(a,str)):raise WeatherError('天气返回城市与配置不符')
        city_id=str(data.get('cityid',''))
        if not re.fullmatch(r'\d{9}',city_id) or config.get('city_id') and config['city_id']!=city_id:raise WeatherError('天气返回城市编号不符')
        raw=data.get('futureHour')
        if not isinstance(raw,list) or not 2<=len(raw)<=48:raise WeatherError('缺少可用的逐小时天气预报')
        hourly=[];seen=set()
        for row in raw:
            stamp=datetime.strptime(row['dateYmdh'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=ZONE)
            if stamp.minute or stamp.second or stamp in seen:raise WeatherError('逐小时预报时刻重复或未对齐')
            seen.add(stamp)
            hourly.append({'time':stamp.isoformat(),'temperature_c':temperature(row.get('wtTemp')),
                           'weather':str(row.get('wtNm',''))[:40]})
        hourly.sort(key=lambda p:p['time'])
        if local_time(hourly[0]['time'])>at+timedelta(hours=1) or local_time(hourly[-1]['time'])<at+timedelta(minutes=30):
            raise WeatherError('逐小时预报未覆盖当前及后续时段')
        daily=[]
        for row in data.get('futureDay',[]) if isinstance(data.get('futureDay',[]),list) else []:
            # 日预报只供展示，缺字段时不阻断有效逐小时数据，也不进入关机判断。
            try:
                day=datetime.strptime(row['dateYmd'],'%Y-%m-%d').date().isoformat()
                daily.append({'date':day,'day_temperature_c':temperature(row['wtTemp1']),
                              'night_temperature_c':temperature(row['wtTemp2'])})
            except Exception:continue
        return {'source':'NowAPI','upstream_app':'weather.realtime','city':config['city_name'],'city_id':city_id,
                'timezone':'Asia/Shanghai','unit':'℃','fetched_at':at.isoformat(),'forecast_issued_at':None,
                'freshness_basis':'请求完成时间及预报覆盖范围；上游未提供预报发布时间',
                'hourly':hourly,'daily':daily,'cached':False}
    except WeatherError:raise
    except Exception:raise WeatherError('NowAPI逐小时预报格式无效') from None


def fetch_forecast(config,clock=lambda:datetime.now(ZONE)):
    key=os.environ.get('NOWAPI_APPKEY','').strip();sign=os.environ.get('NOWAPI_SIGN','').strip()
    if not key or not sign:raise WeatherError('尚未配置NOWAPI_APPKEY和NOWAPI_SIGN')
    form={'app':'weather.realtime','ag':'futureDay,futureHour','format':'json','appkey':key,'sign':sign}
    form.update({'cityId':config['city_id']} if config.get('city_id') else {'cityNm':config['city_name']})
    try:
        # 用HTTPS表单而非URL传凭据；固定域名且拒绝重定向，避免密钥出现在链接中。
        with requests.Session() as session:
            session.trust_env=False
            with session.post(ENDPOINT,data=form,timeout=(3,10),allow_redirects=False) as response:
                if response.status_code!=200 or len(response.content)>500_000:raise WeatherError('NowAPI请求未成功')
                payload=response.json()
        return parse_forecast(payload,config,clock())
    except WeatherError:raise
    except Exception:raise WeatherError('NowAPI连接或响应解析失败') from None


def validate_snapshot(snapshot,at,max_age=1800):
    """缓存不能刷新原取数时间；超龄、错单位或错来源不得用于控制。"""
    if (not isinstance(snapshot,dict) or snapshot.get('source')!='NowAPI'
        or snapshot.get('upstream_app')!='weather.realtime' or snapshot.get('unit')!='℃'
        or snapshot.get('timezone')!='Asia/Shanghai'
        or not 0<=(local_time(at)-local_time(snapshot['fetched_at'])).total_seconds()<=max_age):
        raise WeatherError('天气快照无效或已过期')


def format_forecast_response(snapshot,at):
    """仅为接口补充日期展示；相对日期按响应时刻计算，避免缓存跨零点后误标。"""
    result=deepcopy(snapshot)
    today=local_time(at).date()

    def date_label(day):
        offset=(day-today).days
        relative={-1:'昨天',0:'今天',1:'明天',2:'后天'}.get(offset)
        return f'{relative}（{day.isoformat()}）' if relative else day.isoformat()

    groups={}
    for point in result['hourly']:
        stamp=local_time(point['time'])
        day=stamp.date();key=day.isoformat();label=date_label(day)
        point['date']=key
        point['time_label']=f'{label} {stamp:%H:%M}'
        if key not in groups:
            groups[key]={'date':key,'date_label':label,'hourly':[]}
        groups[key]['hourly'].append(deepcopy(point))
    for point in result['daily']:
        point['date_label']=date_label(datetime.strptime(point['date'],'%Y-%m-%d').date())
    result['reference_date']=today.isoformat()
    result['hourly_by_date']=list(groups.values())
    return result


def interval_temperature(snapshot,start,end,at,threshold_c=27):
    """用逐小时预报线性估计半点，完整覆盖候选至空档结束，不外推缺失区间。"""
    validate_snapshot(snapshot,at)
    start,end=local_time(start),local_time(end)
    if end<=start:raise WeatherError('天气判断区间无效')
    points=[(local_time(p['time']),temperature(p['temperature_c'])) for p in snapshot['hourly']]
    if any(b[0]<=a[0] for a,b in zip(points,points[1:])):raise WeatherError('天气预报时刻重复或倒序')
    def estimate(stamp):
        exact=next((v for t,v in points if t==stamp),None)
        if exact is not None:return exact
        for (a,x),(b,y) in zip(points,points[1:]):
            if a<stamp<b:
                if b-a>timedelta(hours=1):raise WeatherError('天气预报在所需区间缺点')
                return x+(y-x)*(stamp-a).total_seconds()/(b-a).total_seconds()
        raise WeatherError('逐小时天气未覆盖所需区间')
    values=[estimate(start),estimate(end)]
    for (a,x),(b,y) in zip(points,points[1:]):
        if a<end and b>start and b-a>timedelta(hours=1):raise WeatherError('天气预报在所需区间缺点')
    values.extend(v for t,v in points if start<t<end)
    maximum=max(values)
    return {'allowed':maximum<threshold_c,'candidate_temperature_c':round(values[0],3),
            'maximum_temperature_c':round(maximum,3),'threshold_c':threshold_c,
            'range_start':start.isoformat(),'range_end':end.isoformat(),
            'half_hour_method':'逐小时线性估计，不是额外实测值'}


class WeatherService:
    """页面和航班控制器复用同一缓存；失败不伪造成功，不返回过期旧缓存。"""
    def __init__(self,config_path=ROOT/'nowapi_weather_config.json',clock=lambda:datetime.now(ZONE),source=fetch_forecast):
        self.config_path=config_path;self.clock=clock;self.source=source;self._cache=None;self._lock=threading.Lock()

    def get(self):
        with self._lock:
            config=settings(self.config_path);at=self.clock()
            if self._cache:
                try:
                    validate_snapshot(self._cache,at,config['cache_seconds'])
                    if self._cache['city']==config['city_name'] and (not config.get('city_id') or self._cache['city_id']==config['city_id']):
                        result=deepcopy(self._cache);result['cached']=True;return result
                except Exception:pass
            snapshot=self.source(config,self.clock)
            validate_snapshot(snapshot,self.clock(),config['maximum_snapshot_age_seconds'])
            self._cache=deepcopy(snapshot)
            return snapshot
