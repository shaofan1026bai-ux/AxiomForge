"""本地粗开机预测与整点/半点更新；候选时间不构成启动许可，没有网络或写设备接口。"""
from __future__ import annotations
from datetime import datetime,timedelta
from pathlib import Path
from bisect import bisect_right
import argparse,hashlib,json,math,statistics
from zoneinfo import ZoneInfo

ZONE=ZoneInfo('Asia/Shanghai')
AHUS=(1,2,4,5)
SCHEDULE_FREEZE_MINUTES=15
SCHEDULE_CONFIRMATION_MINUTES=5


class ForecastError(ValueError):
    """只表示离线输入不足或无效，不包含平台上下文。"""


def at(value):
    """所有时刻带时区，统一到北京时间。"""
    t=datetime.fromisoformat(value)
    if t.tzinfo is None:raise ForecastError('时刻必须带时区')
    return t.astimezone(ZONE)


def num(value):
    """不把布尔和非有限值作为测量。"""
    if type(value) not in (int,float) or not math.isfinite(value):raise ForecastError('需要有限数值')
    return value


def ceil_time(t,step):
    """候选时刻向上对齐显示网格，不自动产生设备指令。"""
    midnight=t.replace(hour=0,minute=0,second=0,microsecond=0)
    return midnight+timedelta(minutes=math.ceil((t-midnight).total_seconds()/60/step)*step)


def floor_time(t,step):
    """最迟应启动的预测向前取整，避免显示对齐反而消耗舒适余量。"""
    midnight=t.replace(hour=0,minute=0,second=0,microsecond=0)
    return midnight+timedelta(minutes=math.floor((t-midnight).total_seconds()/60/step)*step)


def slot(t):
    """每小时00、30分是更新格点，重启后不补算已错过的格点。"""
    return t.replace(minute=t.minute//30*30,second=0,microsecond=0)


def samples(items,now):
    """先截断再计算：整分可能稍后上报，当前及未来采样均不使用。"""
    data={}
    for item in items:
        t=at(item['at'])
        if t>=now:continue
        value=num(item['value'])
        if t in data and data[t]!=value:raise ForecastError('同刻样本冲突')
        data[t]=value
    return sorted(data.items())


def latest(items,now,cfg):
    """只接收新鲜的最后原值，不用查询时间刷新旧上报。"""
    s=samples(items,now)
    if not s or (now-s[-1][0]).total_seconds()>cfg['max_signal_age_seconds']:return None
    return s[-1]


def slope(s,minimum_span=10,max_gap=180):
    """用连续原始值估算小时变化；保留零/负斜率，不编造正升温。"""
    if len(s)<4 or (s[-1][0]-s[0][0]).total_seconds()<minimum_span*60:return None
    if any((b[0]-a[0]).total_seconds()>max_gap for a,b in zip(s,s[1:])):return None
    x=[(t-s[0][0]).total_seconds()/3600 for t,_ in s];y=[v for _,v in s]
    xm=sum(x)/len(x);ym=sum(y)/len(y);den=sum((v-xm)**2 for v in x)
    return sum((a-xm)*(b-ym) for a,b in zip(x,y))/den if den else None


def fan_recovery(item,now,cfg):
    """只用已发生的风机/频率确认连续运行段，缺口重计；左侧截断不冒充真实开风时刻。"""
    run=samples(item['running'],now);frequency=samples(item['frequency'],now)
    times=[t for t,_ in frequency];start=None;previous_at=None;was_active=False;was_proven_off=False;edge=False
    for t,value in run:
        j=bisect_right(times,t)-1
        hz=frequency[j][1] if j>=0 and (t-times[j]).total_seconds()<=cfg['max_sample_gap_seconds'] else None
        active=value==1 and hz is not None and 0<hz<=60
        adjacent=previous_at is not None and (t-previous_at).total_seconds()<=cfg['max_sample_gap_seconds']
        if active and not(was_active and adjacent):
            start=t;edge=bool(adjacent and was_proven_off)
        elif not active:start=None;edge=False
        previous_at=t;was_active=active;was_proven_off=value==0 and hz==0
    if start is None:return {'segment_start':None,'edge_confirmed':False,'exclude_until':None}
    return {'segment_start':start.isoformat(),'edge_confirmed':edge,
            'exclude_until':(start+timedelta(minutes=cfg['fan_startup_exclusion_minutes'])).isoformat()}


def recent_trend(item,now,cfg):
    """恢复段只影响斜率样本，不改写当前温度；较近窗口权重更高，不延续早先恢复斜率。"""
    recovery=fan_recovery(item,now,cfg)
    end=at(recovery['exclude_until']) if recovery['exclude_until'] else now
    history=samples(item['return'],now);parts=[];weighted=[]
    for window,weight in zip(cfg['trend_window_minutes'],cfg['trend_window_weights']):
        selected=[x for x in history if x[0]>=max(end,now-timedelta(minutes=window))]
        rate=slope(selected,cfg['minimum_trend_span_minutes'],cfg['max_sample_gap_seconds'])
        parts.append({'window_minutes':window,'rate_c_per_hour':rate,'samples':len(selected),
                      'first_at':selected[0][0].isoformat() if selected else None,
                      'span_minutes':(selected[-1][0]-selected[0][0]).total_seconds()/60 if selected else 0})
        if rate is not None:weighted.append((rate,weight))
    rate=sum(r*w for r,w in weighted)/sum(w for _,w in weighted) if weighted else None
    recovery['phase']='excluding_startup' if end>now else ('waiting_post_recovery_samples' if rate is None else 'post_recovery_trend')
    return rate,recovery,parts


def quantile(pairs,q):
    """相似窗口按距离加权，分位数是粗估参数，不是保证上界。"""
    pairs=sorted(pairs);total=sum(w for _,w in pairs);cumulative=0
    if not pairs or total<=0:raise ForecastError('没有可用历史窗口')
    for value,w in pairs:
        cumulative+=w
        if cumulative>=q*total:return value
    return pairs[-1][0]


def historical_projection(model,n,air,ret,fresh,hz,hour,cfg):
    """不跨新风工况、不向温度范围外外推；匹配不足时明确交给实时趋势回退。"""
    same=[r for r in model['warming_windows'] if r['ahu']==n and (r['fresh_percent']<=5)==(fresh<=5)]
    result={'rate':None,'days':0,'neighbors':0,'supported':False}
    if not same:
        return dict(result,reason='没有相同新风开闭工况的历史样本')
    if not min(r['outdoor_c'] for r in same)<=air<=max(r['outdoor_c'] for r in same):
        return dict(result,reason='预测室外温度超出相同新风工况的历史范围')
    def hour_distance(r):
        d=abs(r['hour']-hour);return min(d,24-d)
    pool=[r for r in same if abs(r['outdoor_c']-air)<=cfg.get('maximum_neighbor_outdoor_distance_c',2)
          and abs(r['return_c']-ret)<=cfg.get('maximum_neighbor_return_distance_c',3)
          and abs(r['fresh_percent']-fresh)<=cfg.get('maximum_neighbor_fresh_difference_percent',25)
          and abs(r['frequency_hz']-hz)<=cfg.get('maximum_neighbor_frequency_difference_hz',10)
          and hour_distance(r)<=cfg.get('maximum_neighbor_hour_distance',3)]
    def distance(r):
        return ((r['outdoor_c']-air)/2)**2+((r['return_c']-ret)/2)**2+(hour_distance(r)/2)**2+((r['fresh_percent']-fresh)/50)**2+((r['frequency_hz']-hz)/15)**2
    chosen=sorted(pool,key=distance)[:cfg['nearest_windows']]
    days=len({r['date'] for r in chosen});result.update(days=days,neighbors=len(chosen))
    if len(chosen)<cfg.get('minimum_history_neighbors',8) or days<cfg['minimum_history_days']:
        return dict(result,reason='相似温度、时刻和风量工况的历史支持不足')
    return dict(result,rate=quantile([(r['rate_c_per_hour'],1/(1+distance(r))) for r in chosen],cfg['history_rate_quantile']),supported=True)


def historical_rate(model,n,air,ret,fresh,hz,hour,cfg):
    """保留诊断调用的三元组接口，不再把不同新风工况当作有效历史。"""
    result=historical_projection(model,n,air,ret,fresh,hz,hour,cfg)
    return result['rate'],result['days'],not result['supported']


def trend_projection(parts,legacy_rate):
    """多窗口中位数抑制单个短窗口尖峰，长基线必须有足够真实采样跨度。"""
    valid=[p for p in parts if p['rate_c_per_hour'] is not None]
    robust=statistics.median(p['rate_c_per_hour'] for p in valid) if valid else legacy_rate
    longer=[p for p in valid if p.get('span_minutes',0)>=45]
    baseline=max(longer,key=lambda p:p['window_minutes'])['rate_c_per_hour'] if longer else None
    shortest=min(valid,key=lambda p:p['window_minutes'])['rate_c_per_hour'] if valid else None
    return robust,baseline,shortest


def projected_rate(history,robust,baseline,minutes,cfg):
    """负斜率可以生效；短时冲击按15分钟衰减，不作为整个未来的升温下限。"""
    if history is not None:
        initial=history if robust is None else robust
        return history+(initial-history)*math.exp(-minutes/cfg.get('history_blend_minutes',60))
    # 仍保留该台当前温度参与组统计，不能因历史不足把已知高温测点删掉。
    # 此类退化输出会被automatic_adjustment_allowed禁止用于控制。
    if robust is None:return 0.0
    if baseline is None:baseline=robust
    return (baseline*math.exp(-minutes/cfg.get('baseline_decay_minutes',90))
            +(robust-baseline)*math.exp(-minutes/cfg.get('trend_decay_minutes',15)))


def first_crossing(curve,target):
    if curve[0][1]>=target:return curve[0][0]
    for (a,x),(b,y) in zip(curve,curve[1:]):
        if x<target<=y:return a+(b-a)*(target-x)/(y-x)
    return None


def aggregate(values,method):
    """支持等权测点均值和中位数，不虚构面积/风量权重。"""
    return statistics.median(values) if method=='median' and len(values)!=2 else statistics.fmean(values)


def aggregate_crossing_fraction(before,after,target,method):
    """组曲线触界按段求解；中位数排序在步长内切换时也不误用端点直线。"""
    breaks={0.0,1.0}
    if method=='median':
        for i in range(len(before)):
            for j in range(i):
                den=(after[i]-before[i])-(after[j]-before[j])
                if den:
                    f=(before[j]-before[i])/den
                    if 0<f<1:breaks.add(f)
    points=sorted(breaks)
    value=lambda f:aggregate([a+(b-a)*f for a,b in zip(before,after)],method)
    for a,b in zip(points,points[1:]):
        va=value(a);vb=value(b)
        if va>=target:return a
        if va<target<=vb:return a+(target-va)/(vb-va)*(b-a)
    return 1.0 if value(1)>=target else None


def curve_value(curve,t):
    """用于热点提示的单台预测，不参与组目标的提前触发。"""
    if t<=curve[0][0]:return curve[0][1]
    for a,b in zip(curve,curve[1:]):
        if a[0]<=t<=b[0]:return a[1]+(b[1]-a[1])*(t-a[0]).total_seconds()/(b[0]-a[0]).total_seconds()
    return None


def hourly_weather(payload,now,cfg,outdoor):
    """逐小时预报只在完整覆盖时使用；锚定现场实测，不外推缺点或错城市数据。"""
    info={'used':False,'source':'现场室外温度及近期趋势'}
    snapshot=payload.get('weather_forecast')
    if snapshot is None:return None,info
    try:
        if (snapshot.get('source')!='NowAPI' or snapshot.get('upstream_app')!='weather.realtime'
            or snapshot.get('city_id')!='101291508' or snapshot.get('city') not in ('芒市','潞西')
            or snapshot.get('timezone')!='Asia/Shanghai' or snapshot.get('unit')!='℃'
            or not 0<=(now-at(snapshot['fetched_at'])).total_seconds()<=1800):raise ForecastError('天气身份或时效无效')
        points=[(at(row['time']),num(row['temperature_c'])) for row in snapshot['hourly']]
        if (len(points)<2 or any(not -60<=v<=60 or t.minute or t.second or t.microsecond for t,v in points)
            or any(b[0]<=a[0] for a,b in zip(points,points[1:]))):raise ForecastError('逐小时天气格式无效')
        def estimate(stamp):
            for t,v in points:
                if t==stamp:return v
            for (a,x),(b,y) in zip(points,points[1:]):
                if a<stamp<b and b-a==timedelta(hours=1):return x+(y-x)*(stamp-a).total_seconds()/3600
            raise ForecastError('逐小时天气没有完整覆盖预测时段')
        anchor=estimate(now);bias=outdoor-anchor
        if abs(bias)>5:raise ForecastError('天气与现场温度偏差超过5℃，不据此取消开机')
        curve={}
        for minute in range(0,cfg['forecast_horizon_minutes']+1,cfg['step_minutes']):
            t=now+timedelta(minutes=minute);curve[t]=estimate(t)+bias
        end=now+timedelta(minutes=cfg['forecast_horizon_minutes'])
        # 即使网格未落在缺点上，也不能跨过两小时以上的预报断档。
        if any(a[0]<end and b[0]>now and b[0]-a[0]!=timedelta(hours=1) for a,b in zip(points,points[1:])):
            raise ForecastError('所需天气时段存在缺点')
        info.update(used=True,source='NowAPI',city_id='101291508',fetched_at=snapshot['fetched_at'],
                    range_start=now.isoformat(),range_end=end.isoformat(),site_bias_c=bias,
                    interpolation='逐小时线性估计，现场实测偏差恒定校正',
                    maximum_outdoor_c=max(curve.values()))
        return curve,info
    except (KeyError,TypeError,ValueError,OverflowError):
        info['reason']='芒市预报无效、过期、不完整或与现场偏差过大，退回现场趋势；禁止据此取消全天开机'
        return None,info


def forecast(payload,model,cfg,freeze_deadline=None):
    """仅用回风及天气粗估候选；冷却水温由现场冷却塔控制负责。"""
    now=at(payload['evaluated_at']);next_at=slot(now)+timedelta(minutes=30)
    out={'evaluated_at':now.isoformat(),'mode':'rough_forecast_dry_run','status':'insufficient_data',
         'tentative_start_at':None,'next_refresh_at':next_at.isoformat(),'confidence':'low',
         'device_writes':[],'applied':False,'compressor_start_permitted':False,
         'normal_fan_start_at':None,'fan_timing_rule':'实际启动成功后600秒，原机保护优先',
         'water_temperature_required':False,'water_c':None,'water_threshold_met_now':None,
         'water_ready_eta':None,'water_and_flow_must_be_rechecked':False,
         'flow_must_be_rechecked':True,'warnings':[],'ahu_forecasts':[]}
    try:
        if cfg['deployment_enabled'] is not False or cfg['refresh_minutes']!=30:raise ForecastError('仅支持本地半小时粗预测')
        objective=cfg.get('planning_objective','post_start_cooling')
        if objective not in ('prestart_return_limit','post_start_cooling'):raise ForecastError('预测目标无效')
        out['planning_objective']=objective
        method=cfg.get('return_aggregation','mean')
        if method not in ('median','mean','earliest'):raise ForecastError('回风统计口径无效')
        minimum=cfg.get('minimum_aggregate_members',2)
        if type(minimum) is not int or minimum<2:raise ForecastError('组统计至少需要两台有效AHU')
        out.update(return_aggregation=method,individual_hotspots=[])
        for key in ('step_minutes','forecast_horizon_minutes','nearest_windows'):
            if type(cfg[key]) is not int or cfg[key]<=0:raise ForecastError('预测步长或样本配置无效')
        if num(cfg['return_limit_c'])!=28.5:raise ForecastError('回风上限固定为28.5℃')
        if not 0<=num(cfg['return_reserve_c'])<2 or not 0<cfg['history_rate_quantile']<=1:raise ForecastError('余量或分位数无效')
        for key in ('weather_trend_cap_c_per_hour','weather_trend_horizon_minutes','startup_allowance_minutes','extra_margin_minutes','fan_startup_exclusion_minutes','minimum_trend_span_minutes','max_signal_age_seconds','max_sample_gap_seconds'):
            if num(cfg[key])<0:raise ForecastError('配置不能为负数')
        windows=cfg['trend_window_minutes'];weights=cfg['trend_window_weights']
        if (len(windows)!=len(weights) or not windows or len(set(windows))!=len(windows)
                or any(type(w) is not int or w<=0 for w in windows) or any(num(w)<=0 for w in weights)):
            raise ForecastError('趋势窗口与权重无效')
        for key in ('trend_decay_minutes','baseline_decay_minutes','history_blend_minutes','short_term_guard_minutes'):
            if not 0<num(cfg.get(key,{'trend_decay_minutes':15,'baseline_decay_minutes':90,'history_blend_minutes':60,'short_term_guard_minutes':15}[key]))<=180:raise ForecastError('趋势衰减及保护时间配置无效')
        cutoff=at(model['data_cutoff'])
        if cutoff>now.replace(hour=0,minute=0,second=0,microsecond=0):raise ForecastError('模型混入当日或未来数据')
        if any(at(r['end'])>=cutoff for r in model['warming_windows']):raise ForecastError('历史标签跨越训练截止')
        for r in model['warming_windows']:
            for key in ('outdoor_c','return_c','hour','fresh_percent','frequency_hz','rate_c_per_hour'):num(r[key])
        for response in model['response_minutes'].values():
            if not 0<=num(response)<=180:raise ForecastError('响应预留无效')
        if model.get('fan_startup_exclusion_minutes')!=cfg['fan_startup_exclusion_minutes']:
            raise ForecastError('历史恢复过滤与在线规则不一致')
        series=payload['series'];fault=latest(series.get('fault',[]),now,cfg);running=latest(series.get('running',[]),now,cfg);output=latest(series.get('start_output',[]),now,cfg)
        if fault is not None and fault[1]==1:
            out.update(status='fault_hold',reason='已有故障，不提出新的启动时间，等待运维处理');return out
        if any(x is not None and x[1]==1 for x in (running,output)):
            out.update(status='already_starting_or_running',reason='已收到启动或运行反馈，不重新预测本轮启动');return out
        outdoor=latest(series['outdoor'],now,cfg)
        if outdoor is None:raise ForecastError('室外温度缺测或过期')
        outdoor_past=[x for x in samples(series['outdoor'],now) if x[0]>=now-timedelta(minutes=60)]
        weather_rate=slope(outdoor_past,max_gap=cfg['max_sample_gap_seconds']) or 0
        cap=cfg['weather_trend_cap_c_per_hour'];weather_rate=max(-cap,min(cap,weather_rate))
        out.update(outdoor_c=outdoor[1],outdoor_trend_c_per_hour=weather_rate)
        weather_curve,weather_info=hourly_weather(payload,now,cfg,outdoor[1])
        out['weather_prediction']=weather_info
        if weather_info.get('reason'):out['warnings'].append(weather_info['reason'])
        flow=payload.get('flow_normal')
        if flow is not None:
            if type(flow['value']) is not bool or not 0<=(now-at(flow['observed_at'])).total_seconds()<=cfg['max_signal_age_seconds']:flow=None
        out['flow_normal_confirmed']=flow['value'] if flow else None
        out['flow_must_be_rechecked']=flow is None or not flow['value']
        if flow is None or not flow['value']:out['warnings'].append('水流未确认正常，候选时间不能用于放行开机')
        deadlines=[];curves={};guards={};historical_responses={};adjustment_supported=True
        for item in payload['ahus']:
            n=item['ahu']
            if n not in AHUS:continue
            row={'ahu':n,'used':False}
            try:
                ret=latest(item['return'],now,cfg);fb=latest(item['running'],now,cfg);hz=latest(item['frequency'],now,cfg);fresh=latest(item['fresh'],now,cfg)
            except ForecastError:
                row['reason']='单台测量格式或数值无效，不参与组统计';out['ahu_forecasts'].append(row);continue
            if any(x is None for x in (ret,fb,hz,fresh)):
                row['reason']='缺测或上报过期';out['ahu_forecasts'].append(row);continue
            if fb[1]!=1 or not 0<hz[1]<=60:
                row['reason']='风机未运行，静止回风不外推';out['ahu_forecasts'].append(row);continue
            if not 10<=ret[1]<=45:
                row['reason']='回风超出输入合理范围，不参与组统计';out['ahu_forecasts'].append(row);continue
            recent_rate,recovery,trend_parts=recent_trend(item,now,cfg)
            if recent_rate is None:out['warnings'].append(f'AHU{n}开风恢复隔离后样本不足，保留当前温度并主要用历史窗口粗估')
            robust,long_rate,short_rate=trend_projection(trend_parts,recent_rate)
            cold_fallback=(long_rate is not None and weather_curve is not None and fresh[1]>=50
                           and max(weather_curve.values())<=outdoor[1]+0.5
                           and max(weather_curve.values())<=ret[1]-2)
            current=ret[1];cross=None;support=[];outside=False;rates=[];curve=[(now,current)];sources=[]
            if ret[1]>=cfg['return_limit_c']:
                out['individual_hotspots'].append({'ahu':n,'return_c':ret[1],'observed_at':ret[0].isoformat(),'effect':'提示，不单独提前组候选'})
            target=cfg['return_limit_c']-cfg['return_reserve_c']
            if current>=target:cross=now
            for minute in range(cfg['step_minutes'],cfg['forecast_horizon_minutes']+1,cfg['step_minutes']):
                t=now+timedelta(minutes=minute)
                air=(weather_curve[t] if weather_curve is not None else
                     outdoor[1]+weather_rate*min(minute,cfg['weather_trend_horizon_minutes'])/60)
                match=historical_projection(model,n,air,current,fresh[1],hz[1],t.hour+t.minute/60,cfg)
                try:rate=projected_rate(match['rate'],robust,long_rate,minute-cfg['step_minutes']/2,cfg)
                except ForecastError:
                    sources.append('insufficient');break
                previous=current;current+=rate*cfg['step_minutes']/60
                support.append(match['days']);outside=outside or not match['supported'];rates.append(rate)
                sources.append('matched_history' if match['supported'] else 'recent_trend_fallback')
                curve.append((t,current))
                if cross is None and current>=target:
                    # 触界在步长内部时插值，不能把第一次超过上限的网格点当作仍安全的开机时刻。
                    elapsed=(target-previous)/rate*60 if rate>0 else cfg['step_minutes']
                    cross=t-timedelta(minutes=cfg['step_minutes'])+timedelta(minutes=elapsed)
            if len(curve)!=cfg['forecast_horizon_minutes']//cfg['step_minutes']+1:
                row['reason']='没有匹配历史或足够的真实长趋势，不能外推该台AHU';out['ahu_forecasts'].append(row)
                adjustment_supported=False;continue
            historical_response=model['response_minutes'].get(str(n),35)
            curves[n]=curve;historical_responses[n]=historical_response
            guard_minutes=cfg.get('short_term_guard_minutes',15)
            current_match=historical_projection(model,n,outdoor[1],ret[1],fresh[1],hz[1],now.hour+now.minute/60,cfg)
            guard_rate=max(0,short_rate or 0,current_match['rate'] or 0)
            guards[n]=[(t,max(value,ret[1]+guard_rate*(t-now).total_seconds()/3600))
                       for t,value in curve if t<=now+timedelta(minutes=guard_minutes)]
            short_cross=first_crossing(guards[n],target)
            if short_cross is not None and (cross is None or short_cross<cross):cross=short_cross
            adjustment_supported=adjustment_supported and (not outside or cold_fallback)
            response=0 if objective=='prestart_return_limit' else historical_response
            margin=0 if objective=='prestart_return_limit' else cfg['extra_margin_minutes']
            if cross is None:
                cross=now+timedelta(minutes=cfg['forecast_horizon_minutes']);row['horizon_fallback']=True
                out['warnings'].append(f'AHU{n}在预测窗内未触及预留边界，末端候选只是暂定参考')
            lead=response+cfg['startup_allowance_minutes']+margin
            deadline=cross-timedelta(minutes=lead);deadlines.append(deadline)
            row.update(used=True,return_c=ret[1],recent_rate_c_per_hour=recent_rate,
                       fan_recovery=recovery,trend_windows=trend_parts,
                       projection_recent_rate_c_per_hour=robust,long_term_rate_c_per_hour=long_rate,
                       short_term_guard_rate_c_per_hour=short_rate,history_supported_steps=sources.count('matched_history'),
                       recent_fallback_steps=sources.count('recent_trend_fallback'),cold_fallback_eligible=cold_fallback,
                       assumed_rate_range_c_per_hour=[min(rates),max(rates)] if rates else None,
                       return_boundary_c=target,boundary_at=cross.isoformat(),response_minutes=response,
                       historical_response_minutes=historical_response,extra_margin_minutes_applied=margin,
                       startup_allowance_minutes=cfg['startup_allowance_minutes'],lead_before_boundary_minutes=lead,
                       comfort_start_deadline=deadline.isoformat(),minimum_neighbor_days=min(support) if support else 0)
            if outside:out['warnings'].append(f'AHU{n}部分预测时段缺少同工况历史，使用会衰减的实时趋势；不跨新风工况外推')
            if support and min(support)<cfg['minimum_history_days']:out['warnings'].append(f'AHU{n}相似日期较少')
            out['ahu_forecasts'].append(row)
        if not deadlines:raise ForecastError('没有有效运行的1F参考AHU')
        if method=='earliest':desired=min(deadlines)
        else:
            if len(curves)<minimum:raise ForecastError('1F有效运行AHU不足两台，不能退化为单台高温触发')
            members=sorted(curves);target=cfg['return_limit_c']-cfg['return_reserve_c']
            temperatures=[curves[n][0][1] for n in members];group_current=aggregate(temperatures,method)
            group_cross=now if group_current>=target else None
            group_curve=[{'at':now.isoformat(),'return_c':group_current}]
            for i in range(1,len(curves[members[0]])):
                t=curves[members[0]][i][0]
                before=[curves[n][i-1][1] for n in members];after=[curves[n][i][1] for n in members]
                group_curve.append({'at':t.isoformat(),'return_c':aggregate(after,method)})
                if group_cross is None:
                    f=aggregate_crossing_fraction(before,after,target,method)
                    if f is not None:group_cross=curves[members[0]][i-1][0]+timedelta(minutes=cfg['step_minutes']*f)
            point_cross=group_cross
            guard_curve=[(t,aggregate([guards[n][i][1] for n in members],method)) for i,(t,_) in enumerate(guards[members[0]])]
            guard_cross=first_crossing(guard_curve,target)
            if guard_cross is not None and (group_cross is None or guard_cross<group_cross):group_cross=guard_cross
            reference=group_cross or curves[members[0]][-1][0]
            lead=cfg['startup_allowance_minutes']
            if objective!='prestart_return_limit':lead+=aggregate(list(historical_responses.values()),method)+cfg['extra_margin_minutes']
            desired=reference-timedelta(minutes=lead)
            out['group_prediction']={'method':method,'members':members,'member_count':len(members),
                'current_return_c':group_current,'return_boundary_c':target,'boundary_at':group_cross.isoformat() if group_cross else None,
                'assessment_end_at':curves[members[0]][-1][0].isoformat(),'horizon_fallback':group_cross is None,
                'lead_before_boundary_minutes':lead,'group_curve':group_curve,
                'point_forecast_boundary_at':point_cross.isoformat() if point_cross else None,
                'short_term_guard_crossing_at':guard_cross.isoformat() if guard_cross else None}
            if len(members)==2:out['warnings'].append('仅两台有效AHU，中位数与均值相同，单个读数仍会影响组统计')
            if group_cross is None:out['warnings'].append('组回风在预测窗内未触及预留边界，候选仅按窗口末端暂定')
            out['warnings'].append('当前约束为1F组回风统计量，不再保证每一台AHU均低于28.5℃')
        out['automatic_adjustment_allowed']=adjustment_supported
        if not adjustment_supported:out['warnings'].append('历史不足且不满足冷天气大新风回退条件，仅展示预测，不授权自动改表或取消开机')
        candidate=max(ceil_time(now,cfg['step_minutes']),floor_time(desired,cfg['step_minutes']))
        if desired<=now:out['warnings'].append('回风侧预测已接近或超过应准备时刻，需尽快核对条件，不能靠继续推迟解决')
        out.update(status='tentative',tentative_start_at=candidate.isoformat(),comfort_start_deadline=desired.isoformat(),
                   reason='相似历史升温窗口、近期回风和天气趋势得到的条件性粗目标，待下一整点/半点修正')
        out['estimated_actual_start_target_at']=(candidate+timedelta(minutes=cfg['startup_allowance_minutes'])).isoformat()
        out['return_side_time_feasible']=candidate<=desired
        out['temperature_error_reserve_c']=cfg['return_reserve_c']
        out['predicted_individual_hotspots']=[]
        for n,curve in curves.items():
            value=curve_value(curve,at(out['estimated_actual_start_target_at']))
            row=next(a for a in out['ahu_forecasts'] if a['ahu']==n)
            row['predicted_return_at_estimated_actual_start_c']=value
            row['individual_limit_enforced_for_group_start']=method=='earliest'
            if value is not None and value>cfg['return_limit_c']:
                out['predicted_individual_hotspots'].append({'ahu':n,'return_c':value,'at':out['estimated_actual_start_target_at'],
                    'effect':'保留单台超温提示，不改变组回风统计的候选时间'})
        if not out['return_side_time_feasible']:out['warnings'].append('候选已晚于模型的回风侧期限，不能据此承诺开机前不超温')
        if freeze_deadline:
            freeze=at(freeze_deadline);out['fixed_freeze_deadline']=freeze.isoformat()
            out['schedule_change_window_open']=(now+timedelta(minutes=SCHEDULE_CONFIRMATION_MINUTES)<freeze
                and now+timedelta(minutes=SCHEDULE_FREEZE_MINUTES+SCHEDULE_CONFIRMATION_MINUTES)<candidate)
            if not out['schedule_change_window_open']:out['warnings'].append('只更新预测，实际改表已受15分钟冻结及5分钟回读预算约束')
        else:
            out['schedule_change_window_open']=False;out['warnings'].append('未提供锁定的原时间表冻结边界，本输出不授权改表')
        return out
    except (KeyError,ValueError,TypeError,OverflowError) as exc:
        out.update(status='insufficient_data',reason=str(exc) if isinstance(exc,ForecastError) else '输入或历史模型无效，不编造全部缺测时的候选',tentative_start_at=None)
        return out


def rolling_tick(payload,model,cfg,state=None):
    """同一半小时格点只计算一次；日切重置，固定原计划冻结截止不跟预测滚动。"""
    now=at(payload['evaluated_at']);current=slot(now);day=now.date().isoformat()
    previous=state if state and state.get('day')==day else {}
    policy=hashlib.sha256(json.dumps(cfg,sort_keys=True,allow_nan=False).encode()).hexdigest()
    if previous.get('last_slot') and at(previous['last_slot'])>current:raise ForecastError('预测格点不能倒退')
    if (previous.get('last_slot')==current.isoformat() and previous.get('policy_signature')==policy
        and previous.get('freeze_minutes')==SCHEDULE_FREEZE_MINUTES):
        # 普通预测半小时更新，已知故障或启动事件无需等到下一格点才停止报候选。
        urgent=any((r is not None and r[1]==1) for r in [latest(payload['series'].get(k,[]),now,cfg) for k in ('fault','running','start_output')])
        if not urgent:return dict(previous['last_forecast'],refresh_due=False,next_state=previous)
    freeze=previous.get('fixed_freeze_deadline')
    original=previous.get('original_schedule_start_at')
    if freeze is not None:
        # 旧状态只有截止时刻，按旧30分钟规则还原原计划，不能改用后来推迟的现场时间。
        old_minutes=previous.get('freeze_minutes',30)
        if type(old_minutes)is not int or old_minutes not in (15,30):raise ForecastError('原冻结规则状态无效')
        start=at(original) if original else at(freeze)+timedelta(minutes=old_minutes)
        original=start.isoformat();freeze=(start-timedelta(minutes=SCHEDULE_FREEZE_MINUTES)).isoformat()
    elif payload.get('original_schedule_start_at'):
        start=at(payload['original_schedule_start_at'])
        if start.date()!=now.date():raise ForecastError('原时间表日期不符')
        original=start.isoformat();freeze=(start-timedelta(minutes=SCHEDULE_FREEZE_MINUTES)).isoformat()
    result=forecast(payload,model,cfg,freeze)
    old_members=previous.get('last_forecast',{}).get('group_prediction',{}).get('members')
    new_members=result.get('group_prediction',{}).get('members')
    if old_members is not None and new_members is not None and old_members!=new_members:
        result['warnings'].append(f'参与组统计的AHU由{old_members}变为{new_members}，需注意成员变化对统计量的影响')
    next_state={'day':day,'last_slot':current.isoformat(),'forecast_version':cfg['version'],'policy_signature':policy,
                'fixed_freeze_deadline':freeze,'freeze_minutes':SCHEDULE_FREEZE_MINUTES,
                'original_schedule_start_at':original,'last_forecast':result}
    return dict(result,refresh_due=True,next_state=next_state)


def main():
    """本地文件入口，支持半小时快照回放，无execute或设备适配器。"""
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True,type=Path);p.add_argument('--model',required=True,type=Path)
    p.add_argument('--config',type=Path,default=Path(__file__).with_name('forecast_config.json'));args=p.parse_args()
    cfg=json.loads(args.config.read_text());model=json.loads(args.model.read_text());document=json.loads(args.input.read_text())
    state=document.get('state');results=[]
    for payload in document.get('snapshots',[document]):
        result=rolling_tick(payload,model,cfg,state);state=result['next_state'];results.append({k:v for k,v in result.items() if k!='next_state'})
    print(json.dumps({'mode':'dry_run','results':results,'next_state':state,'device_writes':[]},ensure_ascii=False,indent=2,allow_nan=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
