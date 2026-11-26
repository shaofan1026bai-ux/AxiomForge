"""新风两段的日期、低温日归属与无重叠写入顺序；不发送设备请求。"""
from contextlib import closing
from datetime import datetime, timedelta
from itertools import permutations
import json
from pathlib import Path
import sqlite3

from hangxin_start_platform import ZONE, assert_stopped

START_STATE=Path('/var/lib/task-manager/hangxin-start-forecast/controller.sqlite3')
EARLY_CLOSE_MINUTES=5


def deferral(at,path=None):
    """只读已确认的开机审计；不能仅凭12点、过时或请求已接收判断取消当天开机。"""
    path=Path(path or START_STATE)
    if not path.exists():return None
    with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=3)) as db:
        rows=db.execute('SELECT record FROM runs WHERE slot>=? ORDER BY slot DESC',
                        ((at-timedelta(days=1)).date().isoformat(),)).fetchall()
    records=[json.loads(row[0]) for row in rows]
    for r in records:
        if r.get('operation')!='defer_start_to_noon':continue
        slot=datetime.fromisoformat(r['slot']).astimezone(ZONE)
        if not 0<=(at.date()-slot.date()).days<=1 or slot>at:continue
        if r.get('attempted') and r.get('status') in ('attempted','unconfirmed','preparing'):
            raise ValueError('机组延至次日的写入仍未确认')
        target=slot.replace(hour=12,minute=0,second=0,microsecond=0)
        if (r.get('status') not in ('confirmed','no_change') or r.get('device_id')!=12788
            or r.get('target_at')!=target.isoformat()
            or r.get('effective_next_start_at')!=(target+timedelta(days=1)).isoformat()
            or r.get('cold_day_assessment',{}).get('allowed') is not True):continue
        # 次日已开始新一轮有效预测后，不能把昨天的低温结论套到今天。
        if any(datetime.fromisoformat(v['slot']).date()>slot.date() and v.get('prediction_completed')
               for v in records):return None
        finished=datetime.fromisoformat(r['finished_at']).astimezone(ZONE)
        if finished>at:raise ValueError('开机审计完成时刻来自未来')
        return {'day':slot.date().isoformat(),'slot':r['slot']}
    return None


def pairs(n):
    p=f'ewcnad_msahu{n}_v'
    return {'first_close':(p+'88',p+'94'),'second_open':(p+'77',p+'83'),
            'second_close':(p+'89',p+'95')}


def device_plan(rows,at,state,n,read,marker,capture=False):
    """保存二段原关闭时间；取消日禁用二段，正常日恢复时不覆盖人工改动。"""
    groups=pairs(n);p=f'ewcnad_msahu{n}_v'
    current={pid:read(rows,pid,at) for pair in groups.values() for pid in pair}
    first_open=read(rows,p+'76',at)*60+read(rows,p+'82',at)
    try:start=read(rows,'nfdh9q_mscrk01_t01',at)*60+read(rows,'nfdh9q_mscrk01_t04',at)
    except Exception:start=None
    off=read(rows,'nfdh9q_mscrk01_t11',at)*60+read(rows,'nfdh9q_mscrk01_t14',at)
    active=marker is not None and start==720
    baselines=state.setdefault('second_segment_baselines',{})
    baseline=baselines.get(str(n))
    second_ids=[pid for g in ('second_open','second_close') for pid in groups[g]]
    manual=False
    if baseline and baseline.get('owned') and baseline.get('status') in ('disabling','disabled','restoring'):
        if baseline['status']=='disabled':allowed={pid:{0} for pid in second_ids}
        elif baseline['status']=='disabling':allowed={pid:{0,v} for pid,v in zip(second_ids,baseline['original_second'])}
        else:allowed={pid:set(baseline.get('restore_allowed_values',{}).get(pid,[0])) for pid in second_ids}
        manual=any(current[pid] not in allowed[pid] for pid in second_ids)
        if manual and capture:baseline['status']='manual_override'
    manual=manual or bool(baseline and baseline.get('status')=='manual_override')
    if baseline and baseline.get('originally_disabled') and any(current[pid]!=0 for pid in second_ids):
        manual=True
        if capture:baseline['status']='manual_override'
    if active:
        assert_stopped(rows,at)
        on=read(rows,p+'28',at)*60+read(rows,p+'31',at)
        end=read(rows,p+'34',at)*60+read(rows,p+'37',at)
        if on==end:raise ValueError('风机首段为停用或全天，无法推断有效停机时刻')
        anchor=datetime.fromisoformat(marker['day']).replace(tzinfo=ZONE)
        fan_on=anchor+timedelta(minutes=on);fan_off=anchor+timedelta(minutes=end)
        if fan_off<=fan_on:fan_off+=timedelta(days=1)
        first_at=anchor+timedelta(minutes=first_open)
        if fan_off.date()>anchor.date() and first_at<fan_on:first_at+=timedelta(days=1)
        goal=fan_off-timedelta(minutes=EARLY_CLOSE_MINUTES)
        if goal<=first_at:raise ValueError('停机前5分钟不晚于第一段新风开启')
        second=[current[pid] for group in ('second_open','second_close') for pid in groups[group]]
        if baseline is None or baseline.get('status')=='restored':
            if capture:
                baseline={'day':marker['day'],'original_second':second,'status':'disabling',
                          'owned':False,'originally_disabled':second==[0,0,0,0]}
                baselines[str(n)]=baseline
        wanted={**dict(zip(groups['first_close'],(goal.hour,goal.minute))),
                **{pid:0 for group in ('second_open','second_close') for pid in groups[group]}}
        goals={'first_close':goal,'second_open':anchor,'second_close':anchor}
        mode='cancelled_day_single_segment'
        if manual:
            wanted.update({pid:current[pid] for pid in second_ids})
            mode='cancelled_day_manual_second_preserved'
    else:
        first_at=at.replace(hour=first_open//60,minute=first_open%60,second=0,microsecond=0)
        first_minutes=start if start is not None else current[groups['first_close'][0]]*60+current[groups['first_close'][1]]
        goal=at.replace(hour=first_minutes//60,minute=first_minutes%60,second=0,microsecond=0)
        second_goal=at.replace(hour=off//60,minute=off%60,second=0,microsecond=0)+timedelta(minutes=30)
        wanted={**dict(zip(groups['first_close'],(goal.hour,goal.minute))),
                **dict(zip(groups['second_open'],(second_goal.hour,second_goal.minute))),
                **{pid:current[pid] for pid in groups['second_close']}}
        mode='normal_two_segments'
        if baseline and baseline.get('status') not in ('restored','manual_override'):
            second=[current[pid] for group in ('second_open','second_close') for pid in groups[group]]
            if baseline.get('originally_disabled'):
                wanted.update({pid:0 for group in ('second_open','second_close') for pid in groups[group]})
            elif not baseline.get('owned'):
                if capture:baseline['status']='restored'
            elif baseline.get('status')=='disabled' and second!=[0,0,0,0]:
                if capture:baseline['status']='manual_override'
                wanted.update({pid:current[pid] for group in ('second_open','second_close') for pid in groups[group]})
                mode='manual_second_segment_preserved'
            else:
                wanted.update(dict(zip(groups['second_close'],baseline['original_second'][2:])))
                mode='restore_second_segment'
        elif baseline and baseline.get('status')=='manual_override':
            wanted.update({pid:current[pid] for group in ('second_open','second_close') for pid in groups[group]})
            mode='manual_second_segment_preserved'
        if manual:
            wanted.update({pid:current[pid] for pid in second_ids})
            mode='manual_second_segment_preserved'
        close=wanted[groups['second_close'][0]]*60+wanted[groups['second_close'][1]]
        close_at=at.replace(hour=close//60,minute=close%60,second=0,microsecond=0)
        if close_at<=second_goal:close_at+=timedelta(days=1)
        goals={'first_close':goal,'second_open':second_goal,'second_close':close_at}
    return {'current':current,'wanted':wanted,'goals':goals,'mode':mode,'first_open':first_open,
            'first_open_at':first_at,'groups':groups,'marker':marker if active else None,'first_source_invalid':start is None}


def intervals(values,n,first_open,check_overlap=True):
    """按每日重复区间检查；00:00/00:00已由现场确认表示停用。"""
    groups=pairs(n)
    f=values[groups['first_close'][0]]*60+values[groups['first_close'][1]]
    s=values[groups['second_open'][0]]*60+values[groups['second_open'][1]]
    e=values[groups['second_close'][0]]*60+values[groups['second_close'][1]]
    result=[]
    for opened,closed in ((first_open,f),(s,e)):
        if opened==closed==0:result.append(None);continue
        if opened==closed:raise ValueError('非零的相同开关时刻含义不明')
        result.append((opened,closed if closed>opened else closed+1440))
    a,b=result
    if check_overlap and a and b and any(max(a[0],b[0]+shift)<min(a[1],b[1]+shift) for shift in (-1440,0,1440)):
        raise ValueError('两段新风时间重叠')
    return result


def safe_order(plan,n,at,desired):
    """枚举最多6个字段的顺序，每一步都不能重叠或意外开启原本未运行的二段。"""
    current=plan['current'];changes=[p for p in desired if desired[p]!=current[p]]
    final={**current,**desired};intervals(final,n,plan['first_open'])
    groups=plan['groups'];second_ids=set(groups['second_open']+groups['second_close'])
    original_first,original_second=intervals(current,n,plan['first_open'],check_overlap=False)
    minute=at.hour*60+at.minute+at.second/60
    def active(interval):
        return interval is not None and any(interval[0]<=minute+shift<interval[1] for shift in (-1440,0,1440))
    was_active=active(original_second);first_was_active=active(original_first)
    for seq in permutations(changes):
        v=dict(current);valid=True
        for pid in seq:
            v[pid]=desired[pid]
            try:
                if (plan['mode']=='cancelled_day_single_segment' and pid in groups['first_close']
                    and any(v[q]!=0 for q in second_ids)):
                    raise ValueError('低温日必须先完整停用第二段，再调整第一段')
                first,second=intervals(v,n,plan['first_open'])
                if pid in groups['first_close'] and first_was_active and not active(first):
                    raise ValueError('不能用次日白天的目标提前关闭跨夜首段')
                if pid in second_ids and not was_active and active(second):raise ValueError('中间组合会立即开启二段')
                group=next(g for g,pair in groups.items() if pid in pair)
                pair=groups[group]
                disabled=all(final[p]==0 for g in ('second_open','second_close') for p in groups[g])
                if not (group.startswith('second') and disabled):
                    stamp=at.replace(hour=v[pair[0]],minute=v[pair[1]],second=0,microsecond=0)
                    goal=plan['goals'][group]
                    if goal.date()>at.date() and stamp<=at:stamp+=timedelta(days=1)
                    if stamp<=at+timedelta(minutes=5):raise ValueError('中间组合已过或临近')
            except ValueError:valid=False;break
        if valid:return list(seq)
    return None
