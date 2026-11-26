"""每日零点VRV全关的离线回归：隔离日期、门控、设备与网络。"""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import hangxin_vrv_daily_shutdown as control
from smart_energy_client import SmartEnergyError


ZONE = ZoneInfo('Asia/Shanghai')
BASE = datetime(2026, 11, 9, tzinfo=ZONE)


class FakeClock:
    """只推进模拟时间，不等待真实午夜或设备上报。"""

    def __init__(self, current=BASE):
        self.current = current

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError('模拟时钟不能倒退')
        self.current += timedelta(seconds=seconds)


class FakePlatform:
    """外机始终运行、全关原值始终1；停机验收只能依据内机。"""

    def __init__(self, clock, state):
        self.clock, self.state = clock, state
        self.reads, self.writes, self.feedback_reads = [], [], []
        self.applied, self.write_times = set(), {}
        self.offline, self.stale_commands = set(), set()
        self.unknown_response, self.never_apply = set(), set()
        self.running_indoor = set()
        self.stop_feedback = True
        self.stale_feedback = False
        self.prepare_delay = 0

    def record_path(self):
        """审计按北京时间业务日期隔离。"""
        day = self.clock().astimezone(ZONE).date().isoformat()
        return self.state/day/'result.json'

    def points(self, device_ids, *, feedback=False):
        """模拟真实身份及上报；外机运行值故意不跟随内机停机变化。"""
        at = self.clock().astimezone(ZONE)
        stamp = at-timedelta(seconds=1)
        result = {}
        for did in device_ids:
            spec = control.DEVICES[did]
            group_applied = any(control.OUTDOORS[out]['number'] == spec['number'] for out in self.applied)
            running = 0 if self.stop_feedback and group_applied else 1
            if did in control.OUTDOORS or did in self.running_indoor:
                running = 1
            tags = [('vrv01', '运行状态', running),
                    ('vrv03', '在线状态', 0 if did in self.offline else 1)]
            if did in control.OUTDOORS:
                tags.append(('offsp', '全关设定', 1))
            for tag, name, value in tags:
                report = stamp
                if did in self.stale_commands or (feedback and self.stale_feedback):
                    report = at-timedelta(seconds=181)
                pid = spec['prefix']+tag
                result[pid] = {
                    'pointId':pid, 'deviceId':did, 'projectId':832,
                    'identifierName':tag, 'pointName':name, 'deviceName':spec['name'],
                    'pointGroupName':'设定参数' if tag == 'offsp' else '运行状态',
                    'unit':'', 'isWrite':tag == 'offsp', 'value':value,
                    'updateTime':report.isoformat(),
                }
        return result

    def read(self):
        self.reads.append(self.clock())
        return self.points(control.OUTDOORS)

    def read_feedback(self):
        self.feedback_reads.append(self.clock())
        # 故意同时返回外机，验证验收代码没有把外机状态混入内机结论。
        return self.points(control.DEVICES, feedback=True)

    def prepare_write(self):
        if self.prepare_delay:
            self.clock.sleep(self.prepare_delay)

    def write(self, pid, value):
        """真实副作用前必须已有当日持久记录，且仅允许四个整数1。"""
        if pid not in control.TARGETS or type(value) is not int or value != 1:
            raise AssertionError('模拟平台拒绝越权写入')
        saved = json.loads(self.record_path().read_text())
        entry = next(w for w in saved['writes'] if w['point_id'] == pid)
        self.writes.append((self.clock(),pid,value))
        self.write_times[entry['device_id']] = self.clock()
        if entry['device_id'] not in self.never_apply:
            self.applied.add(entry['device_id'])
        if entry['device_id'] in self.unknown_response:
            raise SmartEnergyError('模拟响应未知')
        return {'http_status':200,'accepted':True}


class VrvDailyShutdownTests(unittest.TestCase):
    """在完全离线环境检查每日触发一次及内机验收。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)/'state'
        self.clock = FakeClock()
        self.platform = FakePlatform(self.clock,self.state)
        for name in ('requests.sessions.Session.request',
                     'urllib.request.OpenerDirector.open','socket.create_connection'):
            guard = patch(name,side_effect=AssertionError('离线测试禁止真实网络'))
            guard.start();self.addCleanup(guard.stop)

    def execute(self, gate=lambda:True):
        return control.execute(self.platform,self.state,self.clock,self.clock.sleep,gate)

    def test_four_unique_one_triggers_even_when_all_values_already_one(self):
        result = self.execute()
        self.assertEqual(result['status'],'shutdown_observed')
        self.assertEqual(len(self.platform.writes),4)
        self.assertEqual({pid:value for _,pid,value in self.platform.writes},control.TARGETS)
        self.assertEqual({w['before_value'] for w in result['writes']},{1})
        self.assertTrue(result['all_commands_accepted'])
        self.assertTrue(result['all_stopped_observed'])
        self.assertTrue(all(BASE <= t < BASE+timedelta(minutes=2) for t,_,_ in self.platform.writes))

    def test_authorized_scope_is_exactly_four_outdoor_commands_and_28_indoor_observers(self):
        self.assertEqual(set(control.OUTDOORS),set(range(12843,12847)))
        self.assertEqual(control.TARGETS,{f'hl0v6w_vrve{i:02d}_offsp':1 for i in range(1,5)})
        self.assertEqual({number:len(members) for number,members in control.INDOORS.items()},
                         {8:9,9:9,10:6,11:4})

    def test_only_28_indoor_units_participate_in_shutdown_acceptance(self):
        result = self.execute()
        devices = [name for group in result['feedback'].values() for name in group['devices']]
        self.assertEqual(len(devices),28)
        self.assertTrue(all('内机' in name for name in devices))
        self.assertTrue(result['all_stopped_observed'])
        self.assertTrue(all(self.platform.points(control.OUTDOORS)[spec['prefix']+'vrv01']['value']==1
                            for spec in control.OUTDOORS.values()))

    def test_same_date_restart_never_repeats_post(self):
        self.execute()
        count = len(self.platform.writes)
        try:
            result = self.execute()
            self.assertTrue(result.get('skipped_existing') or result['status']=='skipped_existing')
        except ValueError:
            pass
        self.assertEqual(len(self.platform.writes),count)

    def test_parallel_execution_lock_does_not_query_or_trigger(self):
        self.state.mkdir()
        with (self.state/'execute.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=self.execute()
        self.assertEqual(result['status'],'already_running')
        self.assertEqual(self.platform.reads,[])
        self.assertEqual(self.platform.writes,[])

    def test_new_day_allows_new_round_and_preserves_yesterdays_audit(self):
        first = self.execute()
        previous_path = self.platform.record_path()
        previous = previous_path.read_bytes()
        self.clock.current = BASE+timedelta(days=1)
        self.platform.applied.clear()
        second = self.execute()
        self.assertEqual(first['status'],'shutdown_observed')
        self.assertEqual(second['status'],'shutdown_observed')
        self.assertEqual(len(self.platform.writes),8)
        self.assertEqual(previous_path.read_bytes(),previous)
        self.assertTrue((self.state/'2026-11-10/result.json').is_file())

    def test_unknown_yesterday_does_not_replay_same_day_but_new_day_has_new_authorized_round(self):
        self.platform.unknown_response.add(12843)
        first = self.execute()
        self.assertFalse(first['all_commands_accepted'])
        self.assertEqual(len(self.platform.writes),4)
        try:self.execute()
        except ValueError:pass
        self.assertEqual(len(self.platform.writes),4)
        self.clock.current = BASE+timedelta(days=1)
        self.platform.applied.clear()
        self.execute()
        self.assertEqual(len(self.platform.writes),8)

    def test_outside_midnight_start_window_has_no_platform_reads_or_writes(self):
        for current in (BASE-timedelta(microseconds=1), BASE+timedelta(seconds=90),
                        BASE.replace(hour=10,minute=12), BASE.replace(hour=23,minute=59)):
            with self.subTest(current=current):
                self.clock.current = current
                result = self.execute()
                self.assertEqual(result['status'],'skipped_window')
        self.assertEqual(self.platform.reads,[])
        self.assertEqual(self.platform.writes,[])

    def test_utc_clock_uses_shanghai_date_for_execution_and_audit(self):
        self.clock.current = BASE.astimezone(timezone.utc)
        result = self.execute()
        self.assertEqual(result['status'],'shutdown_observed')
        self.assertTrue((self.state/'2026-11-09/result.json').is_file())

    def test_naive_clock_never_reads_or_writes(self):
        self.clock.current = BASE.replace(tzinfo=None)
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual(self.platform.reads,[])
        self.assertEqual(self.platform.writes,[])

    def test_mode_or_selection_gate_false_has_no_device_queries(self):
        result = self.execute(lambda:False)
        self.assertFalse(result.get('attempted',False))
        self.assertEqual(result['writes'],[])
        self.assertEqual(self.platform.reads,[])
        self.assertEqual(self.platform.writes,[])

    def test_switch_off_after_first_trigger_prevents_remaining_commands(self):
        result = self.execute(lambda:len(self.platform.writes)<1)
        self.assertEqual(len(self.platform.writes),1)
        self.assertFalse(result['all_commands_accepted'])

    def test_gate_exception_is_closed(self):
        def broken():raise RuntimeError('模拟本机门控不可读')
        result = self.execute(broken)
        self.assertEqual(result['writes'],[])
        self.assertEqual(self.platform.writes,[])

    def test_unknown_response_does_not_retry_with_or_without_application(self):
        for applied in (True,False):
            with self.subTest(applied=applied), tempfile.TemporaryDirectory() as folder:
                self.state = Path(folder)/'state';self.clock.current = BASE
                self.platform = FakePlatform(self.clock,self.state)
                self.platform.unknown_response.add(12843)
                if not applied:self.platform.never_apply.add(12843)
                result = self.execute()
                self.assertEqual(len(self.platform.writes),4)
                self.assertEqual(len({pid for _,pid,_ in self.platform.writes}),4)
                self.assertFalse(result['all_commands_accepted'])
                self.assertEqual(result['writes'][0]['response']['outcome'],'unknown_readback_only')
                self.assertEqual(result['all_stopped_observed'],applied)

    def test_one_running_indoor_prevents_all_stopped_even_when_others_stopped(self):
        self.platform.running_indoor.add(control.INDOORS[10][0])
        result = self.execute()
        self.assertFalse(result['all_stopped_observed'])
        group = result['feedback']['美的VRV外机10#']
        self.assertEqual(group['devices']['美的VRV内机10-1']['status'],'running')
        self.assertFalse(group['all_stopped_observed'])

    def test_stale_zero_indoor_feedback_cannot_confirm_all_off(self):
        self.platform.stale_feedback = True
        result = self.execute()
        self.assertFalse(result['all_stopped_observed'])
        self.assertTrue(all(s['status']=='unknown' for g in result['feedback'].values() for s in g['devices'].values()))

    def test_feedback_between_durable_marker_and_post_does_not_confirm_shutdown(self):
        did = 12843;self.platform.applied.add(did)
        self.clock.current = BASE+timedelta(seconds=6)
        points = self.platform.points(control.DEVICES,feedback=True)
        entry={'device_id':did,'started_at':BASE.isoformat(),
               'posting_at':(BASE+timedelta(seconds=10)).isoformat()}
        self.clock.current = BASE+timedelta(seconds=20)
        result=control.observe_feedback(points,[entry],self.clock())
        self.assertFalse(result['美的VRV外机8#']['all_stopped_observed'])
        for p in points.values():p['updateTime']=(BASE+timedelta(seconds=11)).isoformat()
        result=control.observe_feedback(points,[entry],self.clock())
        self.assertTrue(result['美的VRV外机8#']['all_stopped_observed'])

    def test_offline_or_stale_outdoor_is_skipped_without_blocking_other_three(self):
        for field in ('offline','stale_commands'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as folder:
                self.state=Path(folder)/'state';self.clock.current=BASE
                self.platform=FakePlatform(self.clock,self.state)
                getattr(self.platform,field).add(12844)
                result=self.execute()
                self.assertEqual(len(self.platform.writes),3)
                self.assertIn('美的VRV外机9#',result['skipped'])
                self.assertTrue(all('vrve02_offsp' not in pid for _,pid,_ in self.platform.writes))

    def test_authentication_cannot_cross_dispatch_deadline(self):
        self.platform.prepare_delay=121
        result=self.execute()
        self.assertEqual(self.platform.writes,[])
        self.assertFalse(result['all_commands_accepted'])

    def test_slow_durable_save_cannot_cross_dispatch_deadline(self):
        original=control.save_report;delayed=False
        def save(path,record):
            nonlocal delayed
            original(path,record)
            if record.get('writes') and not delayed:
                delayed=True;self.clock.sleep(121)
        with patch.object(control,'save_report',side_effect=save):self.execute()
        self.assertEqual(self.platform.writes,[])

    def test_switch_off_during_durable_save_cancels_unsent_trigger(self):
        original=control.save_report;gate=[True]
        def save(path,record):
            original(path,record)
            if record.get('writes'):gate[0]=False
        with patch.object(control,'save_report',side_effect=save):
            self.execute(lambda:gate[0])
        self.assertEqual(self.platform.writes,[])

    def test_preflight_never_sends_trigger(self):
        result=control.preflight(self.platform,self.clock)
        self.assertFalse(result['write_sent'])
        self.assertTrue(all(d['eligible'] for d in result['devices'].values()))
        self.assertEqual(self.platform.writes,[])

    def test_command_identity_and_values_must_be_exact(self):
        did=12843;pid=control.OUTDOORS[did]['prefix']+'offsp'
        baseline=self.platform.points(control.OUTDOORS)
        for mutation in ({'deviceId':12847},{'projectId':833},{'identifierName':'vrv01'},
                         {'pointName':'全开设定'},{'isWrite':False},{'unit':'%'},
                         {'value':True},{'value':2},{'value':None}):
            points=deepcopy(baseline);points[pid].update(mutation)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                control.validate_command(points,did,self.clock())

    def test_real_adapter_rejects_any_other_point_or_noninteger_one(self):
        adapter=object.__new__(control.Platform)
        first=next(iter(control.TARGETS))
        for pid,value in ((first,0),(first,True),(first,1.0),
                          ('hl0v6w_vrve05_offsp',1),('hl0v6w_vrve06_offsp',1),
                          ('hl0v6w_vrve01_vrv01',0),('nfdh9q_mscrk01_t11',19)):
            with self.subTest(pid=pid,value=value),self.assertRaises(ValueError):
                adapter.write(pid,value)


class MidnightTaskManagerTests(unittest.TestCase):
    """真实调度器在临时配置里计划次日零点，绝不执行真实脚本。"""

    def test_enable_and_restart_do_not_replay_daytime_or_missed_midnight(self):
        from app import TaskManager
        from apscheduler.triggers.cron import CronTrigger
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root=Path(folder)
            script=Path(__file__).resolve().parent/'scripts/hangxin_vrv_daily_shutdown.py'
            self.assertTrue(script.is_file())
            config=root/'tasks.json';config.write_text(json.dumps({'ai_mode_enabled':True,'tasks':[
                {'id':'vrv_schedule','script':str(script),'interval_seconds':86400,'execution_hours':[0],'enabled':False}]}))
            current=[BASE.replace(hour=10,minute=12)]
            def clock(zone=None):return current[0].astimezone(zone) if zone else current[0].replace(tzinfo=None)
            app_time=stack.enter_context(patch('app.datetime',wraps=datetime));app_time.now.side_effect=clock
            scheduler_time=stack.enter_context(patch('apscheduler.schedulers.base.datetime',wraps=datetime));scheduler_time.now.side_effect=clock
            popen=stack.enter_context(patch('app.subprocess.Popen'))
            manager=TaskManager(config);stack.callback(manager.shutdown);manager.start()
            state=manager.enable('vrv_schedule')
            self.assertEqual(state['next_run_time'],'2026-11-10T00:00:00+08:00')
            job=manager.scheduler.get_job('vrv_schedule');self.assertIsInstance(job.trigger,CronTrigger)
            self.assertEqual(str(job.trigger.timezone),'Asia/Shanghai')
            manager.shutdown();current[0]=BASE+timedelta(days=1,minutes=3)
            later=TaskManager(config);stack.callback(later.shutdown);later.start()
            self.assertTrue(later.snapshot('vrv_schedule')['effective_enabled'])
            self.assertEqual(later.snapshot('vrv_schedule')['next_run_time'],'2026-11-11T00:00:00+08:00')
            popen.assert_not_called()


if __name__=='__main__':unittest.main()
