"""服务器每天零点触发美的VRV外机8—11号全关，以关联内机反馈验收。"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import time
import tempfile
from urllib.request import ProxyHandler, build_opener
from zoneinfo import ZoneInfo

from hangxin_start_platform import Platform as ReadPlatform, READ_PATH, point_stamp
from smart_energy_client import SmartEnergyError


ROOT = Path(__file__).resolve().parent
ZONE = ZoneInfo('Asia/Shanghai')
STATE = Path('/var/lib/task-manager/hangxin-vrv-shutdown')
GATE_URL = 'http://127.0.0.1:8090/tasks/vrv_schedule'
HOURS = [0]


# 控制范围来自11月7日实际点位；12号两台未启用全关点，不纳入写权限。
OUTDOORS = {12843+i: {'number':8+i, 'name':f'美的VRV外机{8+i}#',
                     'prefix':f'hl0v6w_vrve{i+1:02d}_'} for i in range(4)}
TARGETS = {spec['prefix']+'offsp':1 for spec in OUTDOORS.values()}
DEVICES = dict(OUTDOORS)
INDOORS = {}
for number, first, count, series in ((8,12797,9,1),(9,12806,9,2),(10,12815,6,3),(11,12821,4,4)):
    members = []
    for offset in range(count):
        did = first+offset
        DEVICES[did] = {'number':number, 'name':f'美的VRV内机{number}-{offset+1}',
                        'prefix':f'hl0v6w_vrvm{series}{offset+1:02d}_'}
        members.append(did)
    INDOORS[number] = members


class TaskError(ValueError):
    """错误不携带平台正文或认证信息。"""


def now():
    """执行日期与时间统一为北京时间。"""
    return datetime.now(ZONE)


def local_time(at):
    """拒绝无时区时钟，避免本地系统时区改变执行日期。"""
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise TaskError('时钟必须带时区')
    return at.astimezone(ZONE)


def point_value(points, did, tag, at):
    """核对真实点义、设备、权限和上报时间；不以查询成功代替在线。"""
    spec = DEVICES[did]
    pid = spec['prefix']+tag
    p = points.get(pid, {})
    name = {'offsp':'全关设定','vrv01':'运行状态','vrv03':'在线状态'}[tag]
    writable = tag == 'offsp'
    group = '设定参数' if writable else '运行状态'
    if (p.get('pointId') != pid or type(p.get('deviceId')) is not int or p.get('deviceId') != did
            or type(p.get('projectId')) is not int or p.get('projectId') != 832
            or p.get('identifierName') != tag or p.get('pointName') != name
            or p.get('deviceName') != spec['name'] or p.get('pointGroupName') != group
            or p.get('unit') != '' or p.get('isWrite') is not writable):
        raise TaskError('点位身份、权限或分组不符')
    v = p.get('value')
    if type(v) not in (int,float) or v not in (0,1) or not math.isfinite(v):
        raise TaskError('状态或触发点数值无效')
    if not 0 <= (local_time(at)-point_stamp(p)).total_seconds() <= 180:
        raise TaskError('设备上报过期或时间异常')
    return int(v)


def validate_command(points, did, at):
    """全关属于触发指令，原值为1、外机已停或故障都不替代本次触发。"""
    point_value(points,did,'offsp',at)
    if point_value(points,did,'vrv03',at) != 1:
        raise TaskError('外机通信未在线')


class Platform(ReadPlatform):
    """继承已验证的单点一次POST；唯一写权限为四个offsp整数1。"""
    def __init__(self, config_path=None):
        ReadPlatform.__init__(self,config_path)

    def prepare_write(self):
        """认证在最终时钟和门控检查前完成，写接口不隐式重登。"""
        self._headers()

    def write(self,pid,value):
        """仅四个全关点允许整数1；触发命令每点一次，没有自动重试。"""
        if not isinstance(pid,str) or pid not in TARGETS or type(value) is not int or value!=1:
            raise TaskError('拒绝授权范围以外的全关下发')
        if self.client._token is None or self.client._token_expires_at<=time.time():
            raise SmartEnergyError('全关下发前认证未准备或已过期')
        headers={'Accept':'application/json','Content-Type':'application/json',
                 'Authorization':'Bearer '+self.client._token}
        path=('/api/GatewayMicro/Point/CommonMqttWritePointData?isWriteHistoryData=false'
              '&operationNode=%E8%AE%BE%E5%A4%87%E8%AF%A6%E6%83%85%E6%89%8B%E5%8A%A8%E6%8E%A7%E5%88%B6')
        response=self.client._post(self.client._endpoint(path),body={pid:1},headers=headers)
        try:
            body=self.client._json(response,'VRV全关单点下发')
            return {'http_status':response.status_code,'accepted':response.status_code==200
                    and body.get('success') is True and str(body.get('code'))=='200'}
        finally:
            response.close()

    def _collect(self, device_ids):
        rows = self._read_request(READ_PATH, {'deviceIdList':device_ids,'isActivate':True})
        result = {}
        for p in rows:
            if not isinstance(p,dict):
                raise TaskError('读取记录格式错误')
            did,pid = p.get('deviceId'),p.get('pointId')
            if (type(did) is not int or did not in device_ids or p.get('projectId') != 832
                    or not isinstance(pid,str) or not pid.startswith(DEVICES[did]['prefix'])
                    or p.get('deviceName') != DEVICES[did]['name'] or pid in result):
                raise TaskError('读取设备范围、身份或标识错误')
            result[pid] = p
        return result

    def read(self):
        """发送前只查询四台目标外机。"""
        return self._collect(list(OUTDOORS))

    def read_feedback(self):
        """仅观察按平台设备名称分组的28台内机，不以外机运行判定全关。"""
        ids = [did for members in INDOORS.values() for did in members]
        result = {}
        for index in range(0,len(ids),16):
            result.update(self._collect(ids[index:index+16]))
        return result


def preflight(platform, clock=now):
    """只读核对命令点与在线反馈；不会因原值为1提前试发。"""
    points = platform.read()
    at = local_time(clock())
    devices = {}
    for did,spec in OUTDOORS.items():
        try:
            validate_command(points,did,at)
            devices[spec['name']] = {'eligible':True,'offsp':point_value(points,did,'offsp',at),
                                     'updated_at':points[spec['prefix']+'offsp']['updateTime']}
        except Exception:
            devices[spec['name']] = {'eligible':False,'reason':'上报、身份或在线校验未通过'}
    return {'status':'readonly_preflight','checked_at':at.isoformat(),'due_at':(at.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).isoformat(),
            'devices':devices,'write_sent':False}


def observe_feedback(points, writes, at):
    """只按发送后28台内机的新运行反馈验收；不以外机或offsp值代替。"""
    result = {}
    for entry in writes:
        did = entry['device_id'];spec = OUTDOORS[did]
        sent = local_time(datetime.fromisoformat(entry['posting_at']))
        states = {}
        for member in INDOORS[spec['number']]:
            member_spec = DEVICES[member]
            try:
                running = point_value(points,member,'vrv01',at)
                online = point_value(points,member,'vrv03',at)
                stamps = [point_stamp(points[member_spec['prefix']+tag]) for tag in ('vrv01','vrv03')]
                fresh_after_send = all(t > sent for t in stamps)
                status = 'stopped' if online == 1 and running == 0 and fresh_after_send else 'running' if online == 1 and running == 1 and fresh_after_send else 'unknown'
                states[member_spec['name']] = {'status':status,'running':running,'online':online,
                                               'updated_at':points[member_spec['prefix']+'vrv01']['updateTime']}
            except Exception:
                states[member_spec['name']] = {'status':'unknown'}
        result[spec['name']] = {'all_stopped_observed':all(s['status']=='stopped' for s in states.values()),
                               'devices':states,'indoor_grouping_basis':'平台设备名称分组'}
    return result



def save_report(path,record):
    """先同步尝试标记再替换文件，避免异常退出导致同日重复触发。"""
    path=Path(path)
    descriptor,temporary=tempfile.mkstemp(prefix='.vrv-shutdown-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump(record,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def gate_allowed(value):
    """策略必须由AI总开关和VRV选择共同生效，停止中不允许后续下发。"""
    return value is True or (isinstance(value,dict) and value.get('id')=='vrv_schedule'
        and value.get('effective_enabled') is True and value.get('status') in {'idle','running'})


def local_gate():
    """只读本机策略状态，接口失败时关闭下发通道。"""
    try:
        with build_opener(ProxyHandler({})).open(GATE_URL,timeout=5) as response:
            return response.status==200 and gate_allowed(json.loads(response.read(65537)))
    except Exception:
        return False


def allowed(gate):
    """门控异常不视为允许。"""
    try:return gate_allowed(gate())
    except Exception:return False


def execute(platform,state_dir=STATE,clock=now,sleep=time.sleep,gate=local_gate):
    """每天零点一轮；同日有记录不重发，次日使用独立记录重新触发。"""
    at=local_time(clock());due=at.replace(hour=0,minute=0,second=0,microsecond=0)
    if not 0<=(at-due).total_seconds()<90:
        return {'status':'skipped_window','due_at':due.isoformat(),'attempted':False,'write_sent':False}
    dispatch_until=due+timedelta(minutes=2);observe_until=due+timedelta(minutes=8)
    state_dir=Path(state_dir);state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (state_dir/'execute.lock').open('a+b') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'already_running','due_at':due.isoformat(),'attempted':False,'write_sent':False}
        day_dir=state_dir/due.date().isoformat();day_dir.mkdir(exist_ok=True,mode=0o700)
        path=day_dir/'result.json'
        if path.exists():
            return {**json.loads(path.read_text()),'skipped_existing':True}
        record={'status':'preparing','day':due.date().isoformat(),'due_at':due.isoformat(),
                'started_at':at.isoformat(),'writes':[],'skipped':{},'feedback':{},
                'all_commands_accepted':False,'all_stopped_observed':False,
                'verification_basis':'仅关联28台内机运行和在线反馈','trigger_value':1,
                'targets':dict(TARGETS),'attempted':False}
        save_report(path,record)
        def save():save_report(path,record)
        if not allowed(gate):
            record.update(status='skipped_gate',finished_at=local_time(clock()).isoformat())
            save();return record
        try:
            for did,spec in OUTDOORS.items():
                pid=spec['prefix']+'offsp'
                if not allowed(gate):
                    record['skipped'][spec['name']]='AI或VRV策略未生效，后续目标停止';save();break
                if not due<=local_time(clock())<dispatch_until:
                    record['skipped'][spec['name']]='已过本轮下发窗口';save();continue
                try:
                    points=platform.read();validate_command(points,did,clock())
                    platform.prepare_write();validate_command(points,did,clock())
                except Exception:
                    record['skipped'][spec['name']]='上报、认证或设备校验失败，未下发';save();continue
                if not allowed(gate):
                    record['skipped'][spec['name']]='准备后策略门控关闭';save();break
                sent_at=local_time(clock())
                if not due<=sent_at<dispatch_until:
                    record['skipped'][spec['name']]='准备后超过下发窗口';save();continue
                validate_command(points,did,sent_at)
                entry={'device_id':did,'device_name':spec['name'],'point_id':pid,'value':1,
                       'before_value':point_value(points,did,'offsp',sent_at),'started_at':sent_at.isoformat()}
                record['writes'].append(entry);record.update(status='attempting',attempted=True);save()
                try:
                    if not allowed(gate):raise TaskError('落盘后门控关闭')
                    posting_at=local_time(clock());validate_command(points,did,posting_at)
                    if not due<=posting_at<dispatch_until:raise TaskError('落盘后超过下发窗口')
                except Exception:
                    record['writes'].pop();record['attempted']=bool(record['writes'])
                    record['skipped'][spec['name']]='落盘后时间、门控或反馈失效，本点未下发';save();continue
                entry['posting_at']=posting_at.isoformat()
                try:
                    response=platform.write(pid,1)
                    entry['response']={k:response[k] for k in ('http_status','accepted') if k in response}
                except Exception:
                    entry['response']={'outcome':'unknown_readback_only'}
                save()
            attempted_points={entry['point_id'] for entry in record['writes']}
            for spec in OUTDOORS.values():
                if spec['prefix']+'offsp' not in attempted_points and spec['name'] not in record['skipped']:
                    record['skipped'][spec['name']]='本轮提前结束，未下发'
            record['all_commands_accepted']=len(record['writes'])==4 and all(w.get('response',{}).get('accepted') is True for w in record['writes'])
            record['status']='observing';save()
            while record['writes'] and due<=local_time(clock())<observe_until:
                try:
                    points=platform.read_feedback();checked=local_time(clock())
                    if not due<=checked<observe_until:break
                    record['feedback']=observe_feedback(points,record['writes'],checked)
                    record['feedback_checked_at']=checked.isoformat();save()
                    if all(g['all_stopped_observed'] for g in record['feedback'].values()):break
                except Exception:
                    record['last_feedback_error']='内机反馈读取或校验失败';save()
                sleep(min(15,max(0,(observe_until-local_time(clock())).total_seconds())))
            record['all_stopped_observed']=len(record['feedback'])==4 and all(g['all_stopped_observed'] for g in record['feedback'].values())
            record['status']='shutdown_observed' if record['all_commands_accepted'] and record['all_stopped_observed'] else 'partial_or_unconfirmed'
        except Exception:
            record.update(status='stopped_unconfirmed',reason='本轮取数、校验或记录失败；已尝试指令不重发')
        record['finished_at']=local_time(clock()).isoformat();save();return record


def main(argv=None):
    """预览及check都不下发；仅TaskManager零点入口传execute执行本日一轮。"""
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group();modes.add_argument('--check',action='store_true');modes.add_argument('--execute',action='store_true')
    parser.add_argument('--state-dir',type=Path,default=STATE)
    parser.add_argument('--config',type=Path,default=ROOT/'load_forecast_config.json')
    args=parser.parse_args(argv)
    if args.execute:
        result=execute(Platform(args.config),args.state_dir)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return 0 if result['status'] in {'shutdown_observed','skipped_window','skipped_gate','already_running'} else 1
    if args.check:
        result=preflight(Platform(args.config))
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return 0 if all(d['eligible'] for d in result['devices'].values()) else 1
    result={'status':'preview','write_sent':False,'hours':HOURS,'timezone':'Asia/Shanghai',
            'strategy_id':'vrv_schedule','targets':TARGETS,'trigger_even_if_value_already_one':True,
            'next_due_at':(now().replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).isoformat(),
            'startup_window_seconds':90,'dispatch_window_seconds':120,'observe_window_minutes':8,
            'verification_basis':'仅关联28台内机'}
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
