"""新风同步的离线验证，不登录、不连接真实设备。"""
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
import hangxin_second_fresh_schedule as c
class Clock:
    def __init__(self):self.at=datetime(2026,11,8,18,tzinfo=c.ZONE)
    def __call__(self):return self.at
    def sleep(self,n):self.at+=timedelta(seconds=n)
class Fake:
    def __init__(self,clock):
        self.clock=clock;self.writes=[];self.apply=True;self.fail=False;self.points={}
        for pid,(device,tag,name,group,upper) in c.SPECS.items():
            v=({c.MAIN[0]:19,c.MAIN[1]:30,c.START[0]:11,c.START[1]:50}.get(pid,
                {'v77':20,'v83':0,'v88':11,'v94':50,'v76':8,'v82':5}.get(tag,0)))
            self.points[pid]={'pointId':pid,'deviceId':device,'projectId':832,'identifierName':tag,'pointName':name,'pointGroupName':group,'unit':'','isWrite':True,'value':v}
    def read(self):
        if self.writes:self.clock.at+=timedelta(seconds=1)
        points=deepcopy(self.points)
        for p in points.values():p['updateTime']=self.clock().isoformat()
        return points
    def prepare_write(self):pass
    def write(self,pid,value):
        self.writes.append((pid,value))
        if self.apply:self.points[pid]['value']=value
        if self.fail:raise TimeoutError()
        return {'http_status':200,'accepted':True}
class Cases(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.clock=Clock();self.p=Fake(self.clock)
        mock=patch('requests.sessions.Session.request',side_effect=AssertionError('禁止联网'));mock.start();self.addCleanup(mock.stop)
        source=patch.object(c.policy,'deferral',return_value=None);source.start();self.addCleanup(source.stop)
        state=patch.object(c,'STATE',self.path);state.start();self.addCleanup(state.stop)
    def run_cycle(self,gate=lambda:True):return c.execute(self.p,self.path,self.clock,self.clock.sleep,gate)
    def test_target_adds_thirty_minutes(self):self.assertEqual(c.target(self.p.read(),self.clock()).strftime('%H:%M'),'20:00')
    def test_matching_table_does_not_write(self):self.assertEqual(self.run_cycle()['status'],'no_change');self.assertFalse(self.p.writes)
    def test_five_changed_minutes_are_synced(self):
        self.p.points[c.MAIN[0]]['value']=20;self.p.points[c.MAIN[1]]['value']=0
        result=self.run_cycle();self.assertEqual(result['status'],'confirmed');self.assertEqual(len(self.p.writes),5)
        self.assertTrue(all(pid.endswith('v83') and value==30 for pid,value in self.p.writes))
    def test_two_fields_ordered_and_same_slot_not_repeated(self):
        h,m=c.PAIRS[4];self.p.points[h]['value']=0;self.p.points[m]['value']=15
        result=self.run_cycle();self.assertEqual(result['status'],'confirmed');self.assertEqual(self.p.writes,[(h,20),(m,0)])
        self.assertTrue(self.run_cycle()['skipped_existing']);self.assertEqual(len(self.p.writes),2)
    def test_unknown_applied_is_confirmed_without_resend(self):
        self.p.points[c.PAIRS[0][0]]['value']=0;self.p.fail=True
        self.assertEqual(self.run_cycle()['status'],'confirmed');self.assertEqual(len(self.p.writes),1)
    def test_unknown_not_applied_holds_next_cycle(self):
        self.p.points[c.PAIRS[0][0]]['value']=0;self.p.fail=True;self.p.apply=False
        self.assertEqual(self.run_cycle()['status'],'unconfirmed');self.clock.at+=timedelta(minutes=5)
        self.assertEqual(self.run_cycle()['status'],'pending_hold');self.assertEqual(len(self.p.writes),1)
    def test_gate_off_never_writes(self):self.assertEqual(self.run_cycle(lambda:False)['status'],'skipped_gate');self.assertFalse(self.p.writes)
    def test_past_target_not_rewritten(self):
        self.clock.at=self.clock.at.replace(hour=20,minute=1);self.p.points[c.PAIRS[0][0]]['value']=0
        self.assertEqual(self.run_cycle()['status'],'skipped_conditions');self.assertFalse(self.p.writes)
    def test_invalid_time_value_rejected(self):
        rows=self.p.read();rows[c.MAIN[0]]['value']=24
        with self.assertRaises(ValueError):c.target(rows,self.clock())
    def test_stale_device_is_skipped(self):
        original=self.p.read
        def read():
            rows=original();rows[c.PAIRS[4][0]]['updateTime']=(self.clock()-timedelta(minutes=10)).isoformat();return rows
        self.p.read=read;result=self.run_cycle();self.assertIn('second_open:AHU5',result['skipped']);self.assertFalse(self.p.writes)
    def test_changed_main_table_cancels_remaining_writes(self):
        self.p.points[c.PAIRS[0][0]]['value']=0;original=self.p.write
        def write(pid,value):
            result=original(pid,value);self.p.points[c.MAIN[1]]['value']=0;return result
        self.p.write=write;self.assertEqual(self.run_cycle()['status'],'unconfirmed');self.assertEqual(len(self.p.writes),1)
    def test_cross_day_returns_explicit_skip(self):
        self.p.points[c.MAIN[0]]['value']=23;self.p.points[c.MAIN[1]]['value']=45
        self.assertEqual(self.run_cycle()['status'],'skipped_conditions');self.assertFalse(self.p.writes)
    def test_morning_first_close_tracks_main_start_for_all_five(self):
        self.clock.at=self.clock.at.replace(hour=9)
        self.p.points[c.START[1]]['value']=45
        result=self.run_cycle();self.assertEqual(result['status'],'confirmed')
        self.assertEqual(set(self.p.writes),{(pair[1],45) for pair in c.FIRST_PAIRS})
        self.assertEqual(result['target_times']['first_close'][11:16],'11:45')
    def test_morning_can_sync_both_groups(self):
        self.clock.at=self.clock.at.replace(hour=9)
        self.p.points[c.START[0]]['value']=12;self.p.points[c.START[1]]['value']=0
        self.p.points[c.MAIN[0]]['value']=20;self.p.points[c.MAIN[1]]['value']=0
        self.assertEqual(self.run_cycle()['status'],'confirmed');self.assertEqual(len(self.p.writes),15)
    def test_first_close_uses_safe_minute_then_hour_order(self):
        self.clock.at=self.clock.at.replace(hour=12,minute=30)
        self.p.points[c.START[0]]['value']=12;self.p.points[c.START[1]]['value']=50
        for h,m in c.FIRST_PAIRS:self.p.points[h]['value']=12;self.p.points[m]['value']=50
        h,m=c.FIRST_PAIRS[0];self.p.points[h]['value']=13;self.p.points[m]['value']=0
        self.assertEqual(self.run_cycle()['status'],'confirmed');self.assertEqual(self.p.writes,[(m,50),(h,12)])
    def test_past_first_close_does_not_block_evening_second_open(self):
        self.p.points[c.START[1]]['value']=45;self.p.points[c.MAIN[0]]['value']=20;self.p.points[c.MAIN[1]]['value']=0
        result=self.run_cycle();self.assertEqual(result['status'],'partial')
        self.assertTrue(all(pid.endswith('v83') and value==30 for pid,value in self.p.writes));self.assertEqual(len(self.p.writes),5)
        self.assertIn('first_close:AHU1',result['skipped'])
    def test_next_day_reloads_main_daily_table(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45
        self.assertEqual(self.run_cycle()['status'],'confirmed')
        self.clock.at+=timedelta(days=1);self.p.points[c.START[0]]['value']=12;self.p.points[c.START[1]]['value']=10
        self.assertEqual(self.run_cycle()['status'],'confirmed')
        self.assertTrue(all(self.p.points[pair[0]]['value']==12 and self.p.points[pair[1]]['value']==10 for pair in c.FIRST_PAIRS))
    def test_same_day_reforecast_updates_first_close_on_next_slot(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45
        self.run_cycle();self.clock.at+=timedelta(minutes=5);self.p.points[c.START[1]]['value']=55
        result=self.run_cycle();self.assertEqual(result['status'],'confirmed');self.assertEqual(len(self.p.writes),10)
    def test_main_start_change_during_write_stops_remaining(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45;original=self.p.write
        def write(pid,value):
            result=original(pid,value);self.p.points[c.START[1]]['value']=40;return result
        self.p.write=write;self.assertEqual(self.run_cycle()['status'],'unconfirmed');self.assertEqual(len(self.p.writes),1)
    def test_first_open_second_close_and_percentage_are_preserved(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45
        for n in range(1,6):
            for tag,v in (('v89',0),('v95',0),('v71',100)):
                pid=f'ewcnad_msahu{n}_{tag}'
                if pid in self.p.points:self.p.points[pid]['value']=v
                else:self.p.points[pid]={'identifierName':tag,'value':v}
        before={pid:p['value'] for pid,p in self.p.points.items() if pid not in c.TARGET_IDS}
        self.assertEqual(self.run_cycle()['status'],'confirmed')
        self.assertEqual(before,{pid:p['value'] for pid,p in self.p.points.items() if pid not in c.TARGET_IDS})
        self.assertTrue(all(pid.endswith('v94') for pid,_ in self.p.writes))
    def test_close_before_preserved_first_open_is_skipped(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45
        for pair in c.FIRST_OPEN_PAIRS:self.p.points[pair[0]]['value']=12
        result=self.run_cycle();self.assertEqual(result['status'],'skipped_conditions');self.assertFalse(self.p.writes)
        self.assertIn('首段关闭目标不晚于',result['skipped']['first_close:AHU1'])
    def test_first_close_with_exact_five_minute_margin_is_skipped(self):
        self.clock.at=self.clock.at.replace(hour=11,minute=40);self.p.points[c.START[1]]['value']=45
        self.assertEqual(self.run_cycle()['status'],'skipped_conditions');self.assertFalse(self.p.writes)
    def test_invalid_main_start_does_not_disable_second_open(self):
        self.p.points[c.START[0]]['value']=24;self.p.points[c.MAIN[0]]['value']=20;self.p.points[c.MAIN[1]]['value']=0
        result=self.run_cycle();self.assertEqual(result['status'],'partial');self.assertEqual(len(self.p.writes),5)
        self.assertIn('first_close',result['skipped'])
    def test_changed_preserved_first_open_stops_following_writes(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45;original=self.p.write
        def write(pid,value):
            result=original(pid,value);self.p.points[c.FIRST_OPEN_PAIRS[0][1]]['value']=10;return result
        self.p.write=write;self.assertEqual(self.run_cycle()['status'],'unconfirmed');self.assertEqual(len(self.p.writes),1)
    def test_crash_after_attempt_mark_is_readback_only(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45
        def crash(pid,value):self.p.writes.append((pid,value));raise SystemExit('模拟进程终止')
        self.p.write=crash
        with self.assertRaises(SystemExit):self.run_cycle()
        self.p.write=Fake.write.__get__(self.p,Fake)
        self.clock.at+=timedelta(minutes=5)
        self.assertEqual(self.run_cycle()['status'],'partial_pending')
        self.assertEqual(self.p.writes.count((c.FIRST_PAIRS[0][1],45)),1)
        self.assertEqual(len(self.p.writes),5,'其余四台应继续同步，不重发进程中断的第一台')
    def test_legacy_interrupted_preparing_record_is_readback_only(self):
        pid=c.PAIRS[0][0];self.p.points[pid]['value']=0
        c.FreshStore(self.path).save({'slot':(self.clock()-timedelta(minutes=5)).isoformat(),'status':'preparing','attempted':True,
            'writes':[{'point_id':pid,'value':20,'posting_at':(self.clock()-timedelta(minutes=5)).isoformat()}],'targets':{pid:20}})
        self.assertEqual(self.run_cycle()['status'],'pending_hold');self.assertFalse(self.p.writes)
    def test_gate_changes_before_request_prevents_send(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45;calls=[]
        def gate():calls.append(1);return len(calls)<3
        result=self.run_cycle(gate);self.assertFalse(result['attempted']);self.assertFalse(self.p.writes)
    def test_stale_first_close_device_only_skips_its_group_member(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45;original=self.p.read
        def read():
            rows=original();rows[c.FIRST_PAIRS[4][0]]['updateTime']=(self.clock()-timedelta(minutes=10)).isoformat();return rows
        self.p.read=read;result=self.run_cycle();self.assertEqual(result['status'],'partial');self.assertEqual(len(self.p.writes),4)
    def test_wrong_point_identity_cannot_be_written(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45
        self.p.points[c.FIRST_PAIRS[4][0]]['pointName']='新风2段关时'
        result=self.run_cycle();self.assertEqual(result['status'],'partial');self.assertEqual(len(self.p.writes),4)
    def test_check_reports_both_groups_without_writing(self):
        with patch.object(c,'now',self.clock):result=c.check(self.p)
        self.assertEqual(set(result['groups']),{'first_close','second_open'});self.assertFalse(result['write_sent']);self.assertFalse(self.p.writes)

    def seed_pending(self,pid,wanted,group,goal,minutes=10):
        slot=(self.clock()-timedelta(minutes=minutes)).isoformat()
        old={'slot':slot,'status':'unconfirmed','attempted':True,
            'target_times':{group:goal.isoformat()},'targets':{pid:wanted},
            'writes':[{'point_id':pid,'value':wanted,'posting_at':slot,
                       'response':{'outcome':'unknown_readback_only'}}]}
        c.FreshStore(self.path).save(old);return slot

    def test_latest_goal_already_observed_retires_old_unknown_without_resending(self):
        self.clock.at=self.clock.at.replace(hour=14)
        self.p.points[c.START[0]]['value']=16;self.p.points[c.START[1]]['value']=45
        for h,m in c.FIRST_PAIRS:self.p.points[h]['value']=16;self.p.points[m]['value']=45
        pid=c.FIRST_PAIRS[1][1]
        slot=self.seed_pending(pid,15,'first_close',self.clock().replace(hour=14,minute=15))
        result=self.run_cycle();self.assertEqual(result['status'],'no_change');self.assertFalse(self.p.writes)
        old=c.FreshStore(self.path).get_run(slot)
        self.assertEqual(old['status'],'reconciled')
        self.assertEqual(old['reconciliation'][pid]['resolution'],'latest_target_observed')
        self.assertEqual(old['writes'][0]['response']['outcome'],'unknown_readback_only')

    def test_pending_first_segment_does_not_block_second_or_other_ahu(self):
        self.clock.at=self.clock.at.replace(hour=9)
        self.p.points[c.START[1]]['value']=45
        self.p.points[c.MAIN[0]]['value']=20;self.p.points[c.MAIN[1]]['value']=0
        pid=c.FIRST_PAIRS[0][1]
        self.seed_pending(pid,45,'first_close',self.clock().replace(hour=11,minute=45))
        result=self.run_cycle();self.assertEqual(result['status'],'partial_pending')
        self.assertNotIn((pid,45),self.p.writes)
        self.assertEqual(len(self.p.writes),9,'其他四台首段和五台二段都应同步')

    def test_two_distinct_reports_allow_bounded_new_plan_after_settle(self):
        self.p.points[c.PAIRS[0][0]]['value']=0
        pid=c.PAIRS[0][0];slot=self.seed_pending(pid,20,'second_open',self.clock().replace(hour=20))
        self.assertEqual(self.run_cycle()['status'],'pending_hold');self.assertFalse(self.p.writes)
        self.clock.at+=timedelta(minutes=5)
        self.assertEqual(self.run_cycle()['status'],'confirmed');self.assertEqual(self.p.writes,[(pid,20)])
        old=c.FreshStore(self.path).get_run(slot)
        self.assertEqual(old['reconciliation'][pid]['resolution'],'not_observed_replan_allowed')

    def test_repeated_http_read_of_same_device_report_cannot_release_old_request(self):
        self.p.points[c.PAIRS[0][0]]['value']=0;pid=c.PAIRS[0][0]
        self.seed_pending(pid,20,'second_open',self.clock().replace(hour=20))
        frozen=self.p.read();original=self.p.read;self.p.read=lambda:deepcopy(frozen)
        self.assertEqual(self.run_cycle()['status'],'pending_hold')
        self.clock.at+=timedelta(minutes=5)
        # 旧报告已过180秒：绝不当作第二次设备新报告，也不重试。
        self.run_cycle();self.assertFalse(self.p.writes)
        self.p.read=original

    def test_attempt_budget_stops_same_goal_but_new_goal_can_proceed(self):
        pid=c.PAIRS[0][0];self.p.points[pid]['value']=0
        for minutes in (30,20,10):
            slot=self.seed_pending(pid,20,'second_open',self.clock().replace(hour=20),minutes)
            old=c.FreshStore(self.path).get_run(slot);old['status']='reconciled';c.FreshStore(self.path).save(old)
        self.assertEqual(self.run_cycle()['status'],'pending_hold');self.assertFalse(self.p.writes)
        self.clock.at+=timedelta(minutes=5);self.p.points[c.MAIN[0]]['value']=20;self.p.points[c.MAIN[1]]['value']=30
        result=self.run_cycle();self.assertNotEqual(result['status'],'pending_hold')
        self.assertIn((pid,21),self.p.writes)

    def test_late_old_response_on_blocked_pair_does_not_interrupt_other_devices(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45
        pid=c.FIRST_PAIRS[0][1];self.seed_pending(pid,40,'first_close',self.clock().replace(hour=11,minute=40))
        original=self.p.write
        def late_write(other,wanted):
            result=original(other,wanted);self.p.points[pid]['value']=40;return result
        self.p.write=late_write
        self.assertEqual(self.run_cycle()['status'],'partial_pending')
        self.assertEqual(len(self.p.writes),4);self.assertFalse(any(p==pid for p,_ in self.p.writes))

    def test_stale_pending_member_does_not_block_healthy_other_members(self):
        self.clock.at=self.clock.at.replace(hour=9);self.p.points[c.START[1]]['value']=45
        pid=c.FIRST_PAIRS[0][1];self.seed_pending(pid,45,'first_close',self.clock().replace(hour=11,minute=45))
        original=self.p.read
        def stale():
            rows=original();rows[pid]['updateTime']=(self.clock()-timedelta(minutes=10)).isoformat();return rows
        self.p.read=stale
        self.assertEqual(self.run_cycle()['status'],'partial_pending');self.assertEqual(len(self.p.writes),4)
if __name__=='__main__':unittest.main()
