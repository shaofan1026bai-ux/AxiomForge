"""每五分钟按1#机组每日时间表同步首段新风关闭与第二段新风开启。"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timedelta
import fcntl
import json
from pathlib import Path
import sqlite3
import time
from urllib.request import ProxyHandler,build_opener

from hangxin_start_platform import Platform as ReadPlatform,READ_PATH,WRITE_PATH,ZONE,point_stamp
from hangxin_hourly_temperature import Store
import fresh_air_policy as policy

ROOT=Path(__file__).resolve().parent
STATE=Path('/var/lib/task-manager/hangxin-second-fresh')
TASK='ahu_fresh_air_schedule'
MODEL='fresh-schedule-v4-single-cold-day'
PENDING_SETTLE_SECONDS=300
MAX_ATTEMPTS_PER_TARGET=3
SECOND_OPEN_DELAY_MINUTES=30
START=('nfdh9q_mscrk01_t01','nfdh9q_mscrk01_t04')
MAIN=('nfdh9q_mscrk01_t11','nfdh9q_mscrk01_t14')
FIRST_PAIRS=[(f'ewcnad_msahu{n}_v88',f'ewcnad_msahu{n}_v94') for n in range(1,6)]
FIRST_OPEN_PAIRS=[(f'ewcnad_msahu{n}_v76',f'ewcnad_msahu{n}_v82') for n in range(1,6)]
PAIRS=[(f'ewcnad_msahu{n}_v77',f'ewcnad_msahu{n}_v83') for n in range(1,6)]
SECOND_CLOSE_PAIRS=[(f'ewcnad_msahu{n}_v89',f'ewcnad_msahu{n}_v95') for n in range(1,6)]
GROUPS={'first_close':FIRST_PAIRS,'second_open':PAIRS,'second_close':SECOND_CLOSE_PAIRS}
TARGET_IDS={p for pairs in GROUPS.values() for pair in pairs for p in pair}
SPECS={START[0]:(12788,'t01','开机时间设定1_时','时控参数',23),
       START[1]:(12788,'t04','开机时间设定1_分','时控参数',59),
       MAIN[0]:(12788,'t11','关机时间设定1_时','时控参数',23),
       MAIN[1]:(12788,'t14','关机时间设定1_分','时控参数',59)}
for n,(hour,minute) in enumerate(PAIRS,1):
    SPECS[hour]=(12782+n,'v77','新风2段开时','新风时控参数',23)
    SPECS[minute]=(12782+n,'v83','新风2段开分','新风时控参数',59)
for n,(hour,minute) in enumerate(FIRST_PAIRS,1):
    SPECS[hour]=(12782+n,'v88','新风1段关时','新风时控参数',23)
    SPECS[minute]=(12782+n,'v94','新风1段关分','新风时控参数',59)
for n,(hour,minute) in enumerate(FIRST_OPEN_PAIRS,1):
    SPECS[hour]=(12782+n,'v76','新风1段开时','新风时控参数',23)
    SPECS[minute]=(12782+n,'v82','新风1段开分','新风时控参数',59)


for n in range(1,6):
    for tag,name,group,upper in ((28,'风机1段开时','风机时控参数',23),(31,'风机1段开分','风机时控参数',59),
            (34,'风机1段停时','风机时控参数',23),(37,'风机1段停分','风机时控参数',59),
            (89,'新风2段关时','新风时控参数',23),(95,'新风2段关分','新风时控参数',59)):
        SPECS[f'ewcnad_msahu{n}_v{tag}']=(12782+n,f'v{tag}',name,group,upper)


def now():return datetime.now(ZONE)


def value(points,pid,at):
    p=points.get(pid,{});device,tag,name,group,upper=SPECS[pid]
    if (p.get('pointId')!=pid or p.get('deviceId')!=device or p.get('projectId')!=832
        or p.get('identifierName')!=tag or p.get('pointName')!=name
        or p.get('pointGroupName')!=group or p.get('unit')!='' or p.get('isWrite') is not True):
        raise ValueError('时间点位身份或权限不符')
    v=p.get('value')
    if type(v) not in (int,float) or not 0<=v<=upper or int(v)!=v:
        raise ValueError('时间点位数值无效')
    if not 0<=(at-point_stamp(p)).total_seconds()<=180:raise ValueError('时间点位上报陈旧或来自未来')
    return int(v)


def target(points,at,group='second_open'):
    """每天重新读取机组第一组时间表，不用昨日结果或实际启停反馈推算。"""
    if group not in GROUPS:raise ValueError('未知的新风联动段')
    pair=START if group=='first_close' else MAIN
    stamp=at.replace(hour=value(points,pair[0],at),minute=value(points,pair[1],at),second=0,microsecond=0)
    return stamp if group=='first_close' else stamp+timedelta(minutes=SECOND_OPEN_DELAY_MINUTES)


def valid_segment(points,at,group,n,goal):
    """首段关闭必须晚于保留的晨间开启时间，不擅自构造跨夜首段。"""
    if group!='first_close':return True
    hour,minute=FIRST_OPEN_PAIRS[n-1]
    opened=value(points,hour,at)*60+value(points,minute,at)
    return opened<goal.hour*60+goal.minute


def protected(points,active=TARGET_IDS):
    """保护所有非本轮目标；跳过的联动段也不能在另一段执行时被改变。"""
    result={}
    for pid,p in points.items():
        tag=p.get('identifierName','')
        if pid in active:continue
        if pid.startswith('nfdh9q_mscrk01_t'):
            result[pid]=p.get('value')
        elif pid.startswith('ewcnad_msahu') and tag.startswith('v') and tag[1:].isdigit():
            stable=set(range(28,40))|set(range(56,61))|set(range(62,68))|set(range(70,100))
            if int(tag[1:]) in stable:result[pid]=p.get('value')
    return result


class FreshStore(Store):
    def pending_runs(self):
        """兼容旧版发送前仍记preparing的记录，进程被中断后也只能回读。"""
        with closing(self._connect()) as connection:
            rows=connection.execute(
                "SELECT record FROM runs WHERE attempted=1 AND status IN ('preparing','attempted','unconfirmed') ORDER BY slot"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def attempt_counts(self):
        """按点位和完整目标时刻计数，变更目标不会继承旧目标的重试额度。"""
        with closing(self._connect()) as connection:
            rows=connection.execute('SELECT record FROM runs WHERE attempted=1').fetchall()
        counts={}
        for row in rows:
            old=json.loads(row[0])
            for entry in old.get('writes',[]):
                pid=entry.get('point_id')
                if pid not in POINT_PAIRS:continue
                group,n,_=POINT_PAIRS[pid]
                goal=(old.get('segment_target_times') or {}).get(f'{group}:AHU{n}') or (old.get('target_times') or {}).get(group)
                if goal is None and group=='second_open':goal=old.get('target_at')
                if goal is not None:
                    key=(pid,goal,entry.get('value'));counts[key]=counts.get(key,0)+1
        return counts


POINT_PAIRS={pid:(group,n,pair) for group,pairs in GROUPS.items()
             for n,pair in enumerate(pairs,1) for pid in pair}


def reconcile_pending(store,rows,at,confirmed,resolver=None):
    """逐设备逐段核对旧请求；只读证据充分后重规划，未知结果不伪装成成功。"""
    blocked={};observations=[];ignored=set()
    for pending in store.pending_runs():
        resolutions=pending.setdefault('reconciliation',{})
        for entry in pending.get('writes',[]):
            pid=entry.get('point_id')
            if pid not in POINT_PAIRS:raise ValueError('遗留请求包含未知写点，禁止自动恢复')
            if type(entry.get('value'))is not int or not 0<=entry['value']<=SPECS[pid][4]:
                raise ValueError('遗留请求值不合法，禁止自动恢复')
            if resolutions.get(pid,{}).get('resolution'):continue
            group,n,pair=POINT_PAIRS[pid];key=f'{group}:AHU{n}'
            if confirmed(rows,pid,entry['value'],entry):
                resolutions[pid]={'resolution':'requested_value_observed','observed_at':at.isoformat()}
                continue
            try:
                posted=datetime.fromisoformat(entry['posting_at'])
                if posted.tzinfo is None or posted>at:raise ValueError('旧请求时刻无效')
                current={p:value(rows,p,at) for p in pair}
                stamps=[point_stamp(rows[p]) for p in pair]
                if min(stamps)<=posted:raise ValueError('尚无请求之后的新设备报告')
                if resolver:
                    latest=resolver(rows,n);goal=latest['goals'][group]
                    wanted={pid:latest['wanted'][pid] for pid in pair}
                else:
                    goal=target(rows,at,group)
                    if not valid_segment(rows,at,group,n,goal):raise ValueError('最新首段时序无效')
                    wanted={pair[0]:goal.hour,pair[1]:goal.minute}
                settled=(at-posted).total_seconds()>=PENDING_SETTLE_SECONDS
                if settled and current==wanted:
                    resolutions[pid]={'resolution':'latest_target_observed','observed_at':at.isoformat(),
                        'latest_goal_at':goal.isoformat(),'observed_values':current,
                        'device_report_times':[t.isoformat() for t in stamps]}
                    continue
                evidence=resolutions.setdefault(pid,{'observations':[]})
                samples=evidence.setdefault('observations',[])
                sample={'observed_at':at.isoformat(),'values':current,'latest_goal_at':goal.isoformat(),
                        'minimum_report_at':min(stamps).isoformat(),'maximum_report_at':max(stamps).isoformat()}
                # 必须是两次不同的设备真实上报，不能把重复HTTP读取当作新证据。
                if not samples or (samples[-1]['latest_goal_at']==goal.isoformat()
                    and min(stamps)>datetime.fromisoformat(samples[-1]['maximum_report_at'])):
                    samples.append(sample);evidence['observations']=samples[-2:]
                elif samples[-1]['latest_goal_at']!=goal.isoformat():
                    evidence['observations']=[sample]
                if settled and len(evidence['observations'])>=2:
                    evidence.update(resolution='not_observed_replan_allowed',resolved_at=at.isoformat(),
                        note='旧请求效果未观察到；保留原未知结果，按最新绝对目标重新规划，受次数上限约束')
                    continue
                blocked[key]='旧请求仍在等候期或缺少两次独立新上报，暂停该台该段，其他计划继续'
            except (ValueError,KeyError,TypeError,OverflowError):
                blocked[key]='旧请求的设备/目标或写后新上报尚未有效，只暂停该台该段'
            ignored.update(pair)
        resolved=all(resolutions.get(e['point_id'],{}).get('resolution') for e in pending.get('writes',[]))
        if resolved:
            all_original=all(resolutions[e['point_id']]['resolution']=='requested_value_observed' for e in pending.get('writes',[]))
            pending.update(status=('confirmed' if all(confirmed(rows,pid,wanted) for pid,wanted in pending.get('targets',{}).items())
                                   else 'partial_confirmed') if all_original else 'reconciled',finished_at=at.isoformat())
        store.save(pending)
        observations.append({'slot':pending['slot'],'status':pending['status'],'reconciliation':resolutions})
    return blocked,ignored,observations


class Platform(ReadPlatform):
    def read(self):
        rows=self._read_request(READ_PATH,{'deviceIdList':[12788,*range(12783,12788)],'isActivate':True})
        result={}
        for p in rows:
            did=p.get('deviceId');pid=p.get('pointId')
            prefix='nfdh9q_mscrk01_' if did==12788 else f'ewcnad_msahu{did-12782}_' if type(did)is int and 12783<=did<=12787 else None
            if not prefix or p.get('projectId')!=832 or not isinstance(pid,str) or not pid.startswith(prefix) or pid in result:
                raise ValueError('设备读取范围或标识重复异常')
            result[pid]=p
        return result

    def prepare_write(self):self._headers()

    def write(self,pid,goal):
        if pid not in TARGET_IDS or type(goal)is not int or not 0<=goal<=SPECS[pid][4]:raise ValueError('写入范围越界')
        if self.client._token is None or self.client._token_expires_at<=time.time():raise ValueError('认证未预备')
        response=self.client._post(self.client._endpoint(WRITE_PATH),body={pid:goal},headers={'Accept':'application/json','Content-Type':'application/json','Authorization':'Bearer '+self.client._token})
        try:
            body=self.client._json(response,'新风时段同步')
            return {'http_status':response.status_code,'accepted':response.status_code==200 and body.get('success')is True and str(body.get('code'))=='200'}
        finally:response.close()


def gate():
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:8090/tasks/'+TASK,timeout=5) as r:
            p=json.loads(r.read(65537));return r.status==200 and p.get('id')==TASK and p.get('effective_enabled')is True and p.get('status')!='stopping'
    except Exception:return False


def order(before,pair,goal,at):
    """选择所有分字段中间时刻均在五分钟以后的一种顺序。"""
    wanted={pair[0]:goal.hour,pair[1]:goal.minute}
    changes=[pid for pid in pair if before[pid]!=wanted[pid]]
    if not changes:return []
    candidates=[]
    for seq in (changes,list(reversed(changes))):
        h,m=before[pair[0]],before[pair[1]];moments=[]
        for pid in seq:
            if pid==pair[0]:h=wanted[pid]
            else:m=wanted[pid]
            moments.append(at.replace(hour=h,minute=m,second=0,microsecond=0))
        if min(moments)>at+timedelta(minutes=5):candidates.append((min(moments),seq))
    return max(candidates,key=lambda x:x[0])[1] if candidates else None


def execute(p,state_dir=STATE,clock=now,sleep=time.sleep,allowed=gate):
    """逐AHU整体校验两段；低温日先停用二段，次日按拥有记录恢复。"""
    at=clock();slot=at.replace(minute=at.minute//5*5,second=0,microsecond=0).isoformat()
    state_dir=Path(state_dir);state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (state_dir/'execute.lock').open('a+b') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'already_running','slot':slot,'attempted':False}
        store=FreshStore(state_dir)
        if not allowed():return {'status':'skipped_gate','slot':slot,'attempted':False}
        old=store.get_run(slot)
        if old:return {**old,'skipped_existing':True}
        state=store.read_state() or {}
        rows=None;snapshots={}
        r={'slot':slot,'model':MODEL,'status':'preparing','attempted':False,'writes':[],
           'started_at':clock().isoformat(),'skipped':{},'targets':{},'target_times':{},
           'segment_target_times':{},'policy_modes':{}}
        def finish(status,reason=None):
            # 单台已确认的归属状态立即结算，不让另一台的超时破坏其人工接管检查。
            if rows is not None:
                for n in snapshots:
                    baseline=state.get('second_segment_baselines',{}).get(str(n))
                    if not baseline:continue
                    try:
                        latest=resolve(rows,n)
                        ids=[pid for g in ('second_open','second_close') for pid in latest['groups'][g]]
                        if all(confirmed(rows,pid,latest['wanted'][pid],next((e for e in r['writes'] if e['point_id']==pid),None)) for pid in ids):
                            if latest['mode']=='cancelled_day_single_segment':baseline['status']='disabled'
                            elif latest['mode']=='restore_second_segment':baseline['status']='restored'
                    except Exception:pass
            r.update(status=status,finished_at=clock().isoformat())
            if reason:r['reason']=reason
            store.save(r,state);return r
        def confirmed(rows,pid,wanted,entry=None):
            try:return value(rows,pid,clock())==wanted and (entry is None or point_stamp(rows[pid])>datetime.fromisoformat(entry['posting_at']))
            except Exception:return False
        def resolve(rows,n,capture=False):
            return policy.device_plan(rows,clock(),state,n,value,policy.deferral(clock()),capture)
        store.save(r,state)
        try:
            rows=p.read()
            blocked,ignored,observations=reconcile_pending(store,rows,clock(),confirmed,resolver=resolve)
            r['pending_reconciliation']=observations;r['blocked_segments']=blocked
            counts=store.attempt_counts();before={};plans=[];snapshots={};deadlines=[]
            for group in ('first_close','second_open'):
                try:r['target_times'][group]=target(rows,clock(),group).isoformat()
                except Exception:pass
            for n in range(1,6):
                try:
                    plan=resolve(rows,n,True);desired={};r['policy_modes'][str(n)]=plan['mode']
                    if plan['first_source_invalid']:r['skipped']['first_close']='主机开机时间无效，保留首段，其他合法计划继续'
                    for group,pair in plan['groups'].items():
                        key=f'{group}:AHU{n}';goal=plan['goals'][group]
                        r['segment_target_times'][key]=goal.isoformat()
                        if key in blocked:
                            r['skipped'][key]=blocked[key];continue
                        wanted={pid:plan['wanted'][pid] for pid in pair}
                        if all(plan['current'][pid]==wanted[pid] for pid in pair):continue
                        if any(counts.get((pid,goal.isoformat(),wanted[pid]),0)>=MAX_ATTEMPTS_PER_TARGET
                               for pid in pair if plan['current'][pid]!=wanted[pid]):
                            blocked[key]='同一完整目标的单点请求已达三次上限';r['skipped'][key]=blocked[key];ignored.update(pair);continue
                        disabled=all(plan['wanted'][pid]==0 for g in ('second_open','second_close') for pid in plan['groups'][g])
                        if group=='first_close' and goal<=plan['first_open_at']:
                            r['skipped'][key]='首段关闭目标不晚于保留的开启时间，未自动改表';continue
                        if not (group.startswith('second') and disabled) and goal<=clock()+timedelta(minutes=5):
                            r['skipped'][key]='目标已过或中间时刻不足五分钟，未自动改表';continue
                        desired.update(wanted)
                    seq=policy.safe_order(plan,n,clock(),desired)
                    if seq is None:
                        r['skipped'][f'AHU{n}']='没有无重叠且安全的分字段写入顺序';continue
                    if not seq:continue
                    snapshots[n]=plan;plans.append((n,seq))
                    for pid in seq:
                        before[pid]=plan['current'][pid];r['targets'][pid]=desired[pid]
                        group,_,_=POINT_PAIRS[pid]
                        disabled=all(plan['wanted'][q]==0 for g in ('second_open','second_close') for q in plan['groups'][g])
                        if not (group.startswith('second') and disabled):deadlines.append(plan['goals'][group]-timedelta(minutes=5))
                except Exception:
                    for group in GROUPS:r['skipped'].setdefault(f'{group}:AHU{n}','两段时序、设备点位、机组取消记录或新鲜度未通过')
            r['target_at']=r['target_times'].get('second_open')
            r['protected']=protected(rows,set(r['targets'])|ignored)
            if not plans:return finish('pending_hold' if blocked else 'no_change' if not r['skipped'] else 'skipped_conditions')
            store.save(r,state);until=min([clock()+timedelta(minutes=8),*deadlines])
            def guard(rows,n):
                if clock()>=until:raise ValueError('已过同步窗口')
                latest=resolve(rows,n)
                for pid in snapshots[n]['current']:
                    if pid in r['targets'] and latest['wanted'][pid]!=snapshots[n]['wanted'][pid]:raise ValueError('目标或低温模式变化')
                if latest['first_open_at']!=snapshots[n]['first_open_at']:raise ValueError('首段开启时间或归属日期改变')
                if protected(rows,set(r['targets'])|ignored)!=r['protected']:raise ValueError('其他设定发生外部变化')
                if any(POINT_PAIRS[e['point_id']][1]==n and 'response' in e for e in r['writes']):
                    policy.intervals(latest['current'],n,latest['first_open'])
                for pid,wanted in r['targets'].items():
                    entry=next((e for e in r['writes'] if e['point_id']==pid),None)
                    actual=value(rows,pid,clock())
                    if actual not in ({before[pid],wanted} if entry else {before[pid]}):raise ValueError('目标被其他操作改变')
                    if entry and entry.get('confirmed_at') and (actual!=wanted or point_stamp(rows[pid])<=datetime.fromisoformat(entry['posting_at'])):raise ValueError('已确认值或报告时间回退')
            while clock()<until:
                rows=p.read()
                for entry in r['writes']:
                    if not entry.get('confirmed_at') and confirmed(rows,entry['point_id'],entry['value'],entry):
                        entry['confirmed_at']=clock().isoformat();store.save(r,state)
                for n,seq in plans:
                    guard(rows,n)
                    for index,pid in enumerate(seq):
                        if any(e['point_id']==pid for e in r['writes']):continue
                        if any(not confirmed(rows,dep,r['targets'][dep],next((e for e in r['writes'] if e['point_id']==dep),None)) for dep in seq[:index]):break
                        latest=p.read();guard(latest,n)
                        if confirmed(latest,pid,r['targets'][pid]):continue
                        p.prepare_write();latest=p.read();guard(latest,n)
                        if not allowed():raise ValueError('策略已关闭')
                        # 重新验证本字段的即时组合，不能只信几分钟前规划的安全性。
                        candidate=dict(resolve(latest,n)['current']);candidate[pid]=r['targets'][pid]
                        current_plan=resolve(latest,n)
                        if policy.safe_order({**current_plan,'current':candidate},n,clock(),{}) is None:raise ValueError('写入组合不安全')
                        entry={'point_id':pid,'value':r['targets'][pid],'posting_at':clock().isoformat()}
                        previous_baseline=deepcopy(state.get('second_segment_baselines',{}).get(str(n)))
                        if current_plan['mode']=='cancelled_day_single_segment' and POINT_PAIRS[pid][0].startswith('second'):
                            state['second_segment_baselines'][str(n)]['owned']=True
                        if current_plan['mode']=='restore_second_segment' and POINT_PAIRS[pid][0].startswith('second'):
                            baseline=state['second_segment_baselines'][str(n)];baseline['status']='restoring'
                            values=baseline.setdefault('restore_allowed_values',{}).setdefault(pid,[0])
                            if entry['value'] not in values:values.append(entry['value'])
                        r['writes'].append(entry);r['attempted']=True;r['status']='attempted';store.save(r,state)
                        if not allowed():
                            r['writes'].pop();r['attempted']=bool(r['writes']);store.save(r,state)
                            if previous_baseline is not None:state['second_segment_baselines'][str(n)]=previous_baseline
                            raise ValueError('持久化后策略关闭，本字段未发送')
                        guard(latest,n)
                        try:entry['response']=p.write(pid,entry['value'])
                        except Exception:entry['response']={'outcome':'unknown_readback_only'}
                        store.save(r,state);break
                rows=p.read()
                for n,_ in plans:guard(rows,n)
                if all(confirmed(rows,pid,wanted,next((e for e in r['writes'] if e['point_id']==pid),None)) for pid,wanted in r['targets'].items()):break
                sleep(5)
            else:return finish('unconfirmed','本轮未完整确认，后续逐设备核查，不重发旧请求')
            return finish('partial_pending' if blocked else 'confirmed' if not r['skipped'] else 'partial')
        except Exception:return finish('unconfirmed' if r['attempted'] else 'skipped_conditions','时间、设备、门控或两段参数变化，停止后续请求；不重放旧请求')

def check(p):
    """只读列出每台完整的两段目标，不创建数据库或获取控制归属。"""
    rows=p.read();at=now();state={};path=STATE/'controller.sqlite3'
    if path.exists():
        with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=3)) as db:
            row=db.execute('SELECT data FROM state WHERE id=1').fetchone()
            if row:state=json.loads(row[0])
    groups={g:{'ahus':{}} for g in ('first_close','second_open')};devices={}
    for group in groups:
        try:groups[group]['target_at']=target(rows,at,group).isoformat()
        except Exception:groups[group]['reason']='主机对应时间表未通过校验'
    for n in range(1,6):
        try:
            plan=policy.device_plan(rows,at,state,n,value,policy.deferral(at))
            desired={}
            for group,pair in plan['groups'].items():
                if plan['goals'][group]>at+timedelta(minutes=5) or plan['mode']=='cancelled_day_single_segment':
                    if group!='first_close' or plan['goals'][group]>plan['first_open_at']:
                        desired.update({pid:plan['wanted'][pid] for pid in pair})
            seq=policy.safe_order(plan,n,at,desired)
            devices[str(n)]={'mode':plan['mode'],'desired':plan['wanted'],
                             'target_times':{g:t.isoformat() for g,t in plan['goals'].items()},
                             'safe_sequence':seq,'eligible':seq is not None}
            for group in groups:
                pair=plan['groups'][group]
                groups[group]['ahus'][str(n)]={'hour':plan['current'][pair[0]],'minute':plan['current'][pair[1]],
                    'target_at':plan['goals'][group].isoformat(),'eligible':seq is not None}
        except Exception:
            devices[str(n)]={'eligible':False,'reason':'两段区间、来源记录或设备点位未通过校验'}
            for group in groups:groups[group]['ahus'][str(n)]={'eligible':False}
    return {'status':'readonly_check','model':MODEL,'checked_at':at.isoformat(),'groups':groups,
            'devices':devices,'delay_minutes':SECOND_OPEN_DELAY_MINUTES,'write_sent':False}


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);m=parser.add_mutually_exclusive_group();m.add_argument('--execute',action='store_true');m.add_argument('--check',action='store_true');a=parser.parse_args(argv)
    try:
        result=execute(Platform()) if a.execute else check(Platform()) if a.check else {'status':'preview','model':MODEL,'strategy_id':TASK,'interval_seconds':300,'rules':{'first_close':'1#机组第一组开机时间','second_open':'正常日主机关机＋30分钟；低温取消日开关均00:00停用','cancelled_day':'第一段至AHU停机前5分钟，次日恢复二段原关闭时间'},'delay_minutes':SECOND_OPEN_DELAY_MINUTES,'ahu_ids':[1,2,3,4,5],'write_points':sorted(TARGET_IDS),'write_sent':False}
        print(json.dumps(result,ensure_ascii=False),flush=True);return 1 if result['status'] in ('unconfirmed','pending_hold','partial_pending') else 0
    except Exception:
        print(json.dumps({'status':'failed','write_sent':False,'reason':'配置、认证或点位校验失败'},ensure_ascii=False));return 1

if __name__=='__main__':raise SystemExit(main())
