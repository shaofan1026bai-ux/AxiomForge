"""17点低温延至次日12点的离线验证；不联网、不调用设备接口。"""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import hangxin_start_forecast as control
import hangxin_start_day_policy as policy
from hangxin_start_platform import TIME_CONTROL_ID, TIME_CONTROL_SPEC, CHILLERS
from algorithms.hangxin_start_forecast_mean import forecast as model
from test_hangxin_start_forecast import (
    BASE, PREFIX, HOUR, MINUTE, RUNNING, OUTPUT, FAULT, FakeClock, FakePlatform, point,
    SCHEDULE_NAMES, install_synthetic_forecast_model,
)


def weather(at,end_hours=24,value=20):
    start=at.replace(minute=0,second=0,microsecond=0)
    return {'source':'NowAPI','upstream_app':'weather.realtime','city_id':'101291508','city':'芒市',
            'timezone':'Asia/Shanghai','unit':'℃','fetched_at':at.isoformat(),
            'hourly':[{'time':(start+timedelta(hours=i)).isoformat(),'temperature_c':value} for i in range(end_hours)]}


class DayPlatform(FakePlatform):
    def __init__(self,clock,state_dir):
        super().__init__(clock,state_dir)
        self.values[TIME_CONTROL_ID]=1
        self.context_reads=[];self.context_changes={}
        for tag in ('c00','c23','c22'):self.values[CHILLERS[12789]+tag]=0
        for tag in SCHEDULE_NAMES:self.values[CHILLERS[12789]+tag]=0

    def read_time_control(self):
        at=self.clock()-timedelta(seconds=1);points={}
        for device,prefix in CHILLERS.items():
            for tag,name in SCHEDULE_NAMES.items():
                pid=prefix+tag
                stamp=self.write_times.get(pid,at)-timedelta(seconds=1) if self.stale_confirmation and pid in self.write_times else at
                points[pid]=point(pid,device,name,tag,'',self.values[pid],stamp,True)
            for tag,name in (('c00','主机运行反馈'),('c23','主机启停输出'),('c22','主机故障反馈')):
                pid=prefix+tag;points[pid]=point(pid,device,name,tag,'',self.values[pid],at)
        stamp=self.write_times.get(TIME_CONTROL_ID,at)-timedelta(seconds=1) if self.stale_confirmation and TIME_CONTROL_ID in self.write_times else at
        points[TIME_CONTROL_ID]=point(TIME_CONTROL_ID,12796,'系统时控启停按钮','s06','',self.values[TIME_CONTROL_ID],stamp,True)
        points[TIME_CONTROL_ID].update(pointGroupName='自控系统参数',pointEnumName='运行停止',dataType=1,
            pointEnumItemList=[{'pointEnumItemKey':'停止','pointEnumItemValue':0.0},{'pointEnumItemKey':'运行','pointEnumItemValue':1.0}])
        change=self.context_changes.get(len(self.context_reads)+1)
        if change:change(points)
        self.context_reads.append(deepcopy(points));return points

    def write_time_control(self,pid,value):
        if pid!=TIME_CONTROL_ID or type(value)is not int or value not in (0,1):raise AssertionError('越界写入')
        at=self.clock();slot=at.replace(minute=0,second=0,microsecond=0).isoformat()
        self.durable_attempts.append(deepcopy(control.Store(self.state_dir).get_run(slot)))
        self.writes.append((at,pid,value));self.write_times[pid]=at
        if self.apply:self.values[pid]=value
        if self.timeout_on_write==len(self.writes):raise RuntimeError('模拟请求结果未知')
        return {'http_status':200,'accepted':True}


class DayPolicyTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        install_synthetic_forecast_model(self, self.root)
        self.clock = FakeClock(BASE.replace(hour=17))
        self.platform=DayPlatform(self.clock,self.root)
        self.platform.values.update({HOUR:18,MINUTE:30,PREFIX+'t11':20,PREFIX+'t14':55})
        self.before=deepcopy(self.platform.values)
        self.predictor=Mock(side_effect=self.predict)
        self.weather=Mock(side_effect=lambda:weather(self.clock()))
        self.guard=patch('requests.sessions.Session.request',side_effect=AssertionError('禁止网络'))
        self.guard.start();self.addCleanup(self.guard.stop)

    def predict(self,payload,_,cfg,state=None):
        at=control.local_time(payload['evaluated_at']);end=at+timedelta(minutes=cfg['forecast_horizon_minutes'])
        return {'evaluated_at':at.isoformat(),'status':'tentative','tentative_start_at':(end-timedelta(minutes=10)).isoformat(),
            'weather_prediction':{'used':True,'city_id':'101291508','range_start':at.isoformat(),'range_end':end.isoformat()},
            'group_prediction':{'method':'mean','members':[1,2,4,5],'member_count':4,'return_boundary_c':28.2,
               'group_curve':[{'at':(at+timedelta(minutes=i)).isoformat(),'return_c':24}
                              for i in range(0,cfg['forecast_horizon_minutes']+1,5)]},
            'individual_hotspots':[],'schedule_change_window_open':False,'next_state':{}}

    def execute(self,gate=lambda:True):
        return control.execute(self.platform,self.root,clock=self.clock,sleep=self.clock.sleep,
                               gate=gate,predictor=self.predictor,weather=self.weather)

    def test_final_cold_round_sets_only_first_start_to_noon_without_switch_write(self):
        result=self.execute()
        self.assertEqual((result['status'],result['operation']),('confirmed','defer_start_to_noon'))
        self.assertEqual(result['effective_next_start_at'],'2026-11-06T12:00:00+08:00')
        self.assertEqual([(pid,v) for _,pid,v in self.platform.writes],[(HOUR,12),(MINUTE,0)])
        self.assertEqual(self.platform.values[TIME_CONTROL_ID],1)
        self.assertEqual(self.platform.context_reads,[])
        after=deepcopy(self.platform.values);after.update({HOUR:self.before[HOUR],MINUTE:self.before[MINUTE]})
        self.assertEqual(after,self.before)
        self.assertEqual(control.Store(self.root).read_state()['expected_start_at'],BASE.replace(hour=12).isoformat())
        self.assertNotIn('time_control_hold',control.Store(self.root).read_state())
        self.assertGreaterEqual(self.predictor.call_args.args[2]['forecast_horizon_minutes'],235)
        self.assertTrue(self.platform.durable_attempts[0]['attempted'])
        self.assertLess(self.platform.writes[0][0],self.platform.writes[1][0])

    def test_midnight_is_not_scheduled_and_makes_no_requests(self):
        self.clock.current=BASE.replace(day=6,hour=0)
        self.assertEqual(self.execute()['status'],'skipped_window')
        self.assertEqual(self.platform.reads,[]);self.assertEqual(self.platform.writes,[])
        self.predictor.assert_not_called();self.weather.assert_not_called()

    def test_next_day_eight_predicts_again_without_restoring_switch(self):
        self.execute();self.platform.writes.clear();self.clock.current=BASE.replace(day=6,hour=8)
        result=self.execute()
        self.assertTrue(result['prediction_completed']);self.assertEqual(self.predictor.call_count,2)
        self.assertEqual(self.platform.context_reads,[]);self.assertEqual(self.platform.writes,[])
        state=control.Store(self.root).read_state()
        self.assertEqual(state['day'],'2026-11-06')
        self.assertEqual(state['original_start_at'],'2026-11-06T12:00:00+08:00')

    def test_cold_fifteen_and_sixteen_continue_prediction_without_noon_deferral(self):
        for hour in (15,16):
            with self.subTest(hour=hour),tempfile.TemporaryDirectory() as root:
                self.root=Path(root);self.clock.current=BASE.replace(hour=hour);self.platform=DayPlatform(self.clock,self.root)
                self.platform.values.update({HOUR:18,MINUTE:30,PREFIX+'t11':20,PREFIX+'t14':55})
                result=self.execute();self.assertTrue(result['prediction_completed'])
                self.assertNotIn('cold_day_assessment',result);self.assertEqual(self.platform.writes,[])

    def test_existing_noon_does_not_send_duplicate_commands(self):
        self.platform.values.update({HOUR:12,MINUTE:0})
        self.assertEqual(self.execute()['status'],'no_change');self.assertEqual(self.platform.writes,[])

    def test_only_different_fields_are_written(self):
        self.platform.values.update({HOUR:12,MINUTE:30})
        self.assertEqual(self.execute()['status'],'confirmed')
        self.assertEqual([(pid,v) for _,pid,v in self.platform.writes],[(MINUTE,0)])

    def test_same_round_does_not_repeat_any_field(self):
        self.execute();self.execute();self.assertEqual(len(self.platform.writes),2)
        self.assertEqual(self.predictor.call_count,1)

    def test_noon_deferral_is_separate_from_normal_future_start_freeze(self):
        # 原计划已过仍可跳过今天，普通未来改表仍受原有冻结保护。
        self.platform.values.update({HOUR:16,MINUTE:45})
        self.assertEqual(self.execute()['status'],'confirmed')
        self.assertEqual(self.platform.values[HOUR],12)

    def test_not_cold_or_invalid_weather_never_uses_noon_deferral(self):
        for change in ('hot','weather','hotspot','gap','members'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as root:
                self.root=Path(root);self.platform=DayPlatform(self.clock,self.root)
                self.platform.values.update({HOUR:18,MINUTE:30,PREFIX+'t11':20,PREFIX+'t14':55})
                def invalid(*args):
                    result=self.predict(*args)
                    if change=='hot':result['group_prediction']['group_curve'][-1]['return_c']=28.2
                    if change=='weather':result['weather_prediction']['used']=False
                    if change=='hotspot':result['individual_hotspots']=[{'ahu':1,'return_c':29}]
                    if change=='gap':result['group_prediction']['group_curve'].pop(2)
                    if change=='members':result['group_prediction']['member_count']=1
                    return result
                self.predictor.side_effect=invalid;result=self.execute()
                self.assertFalse(result['cold_day_assessment']['allowed']);self.assertEqual(self.platform.writes,[])

    def test_running_starting_fault_or_disabled_gate_prevents_changes(self):
        for pid in (RUNNING,OUTPUT,FAULT):
            with self.subTest(pid=pid),tempfile.TemporaryDirectory() as root:
                self.root=Path(root);self.platform=DayPlatform(self.clock,self.root);self.platform.values[pid]=1
                self.execute();self.assertEqual(self.platform.writes,[])
        self.assertEqual(self.execute(lambda:False)['status'],'skipped_gate')

    def test_unknown_hour_result_does_not_write_minute_or_resend_next_day(self):
        self.platform.apply=False
        self.assertEqual(self.execute()['status'],'unconfirmed');self.assertEqual(len(self.platform.writes),1)
        self.clock.current=BASE.replace(day=6,hour=8)
        result=self.execute();self.assertTrue(result['prediction_completed'])
        self.assertEqual(result['status'],'pending_hold');self.assertEqual(len(self.platform.writes),1)

    def test_applied_timeout_is_confirmed_without_retry(self):
        self.platform.timeout_on_write=1
        self.assertEqual(self.execute()['status'],'confirmed');self.execute()
        self.assertEqual(len(self.platform.writes),2)

    def test_stale_reports_block_remaining_minute(self):
        self.platform.stale_confirmation=True
        self.assertEqual(self.execute()['status'],'unconfirmed');self.assertEqual(len(self.platform.writes),1)

    def test_external_table_change_during_weather_keeps_prediction_without_writes(self):
        def changed():
            self.platform.values[PREFIX+'t11']=21
            return weather(self.clock())
        self.weather.side_effect=changed;result=self.execute()
        self.assertEqual(result['status'],'external_change');self.assertTrue(result['prediction_completed'])
        self.assertEqual(self.platform.writes,[])

    def test_legacy_time_control_hold_requires_review_without_any_switch_write(self):
        store=control.Store(self.root)
        store.save({'slot':'2026-11-04T17:00:00+08:00','status':'confirmed','attempted':False},
                   {'time_control_hold':{'status':'disabled','disabled_on':'2026-11-04'}})
        result=self.execute();self.assertTrue(result['prediction_completed'])
        self.assertEqual(result['status'],'legacy_time_control_hold');self.assertEqual(self.platform.writes,[])

    def test_next_schedule_goes_from_seventeen_directly_to_next_day_eight(self):
        self.assertEqual(control.next_evaluation(BASE.replace(hour=16)),BASE.replace(hour=17))
        self.assertEqual(control.next_evaluation(BASE.replace(hour=17)),BASE.replace(day=6,hour=8))
        self.assertEqual(control.RUN_HOURS,list(range(8,18)))

    def test_manual_earlier_round_cannot_apply_final_noon_deferral(self):
        at=BASE.replace(hour=13,minute=43)
        prediction=self.predict({'evaluated_at':at.isoformat()},None,{'forecast_horizon_minutes':360})
        self.assertFalse(policy.cancellation_assessment(prediction,at,at.replace(hour=19),manual=True)['allowed'])

class HourlyWeatherTests(unittest.TestCase):
    def setUp(self):
        self.at=BASE
        self.cfg=json.loads((control.ALGORITHM_DIR/'forecast_config.json').read_text())

    def test_hourly_values_change_future_weather_and_anchor_current_measurement(self):
        snapshot=weather(self.at,value=20);snapshot['hourly'][1]['temperature_c']=24
        curve,info=model.hourly_weather({'weather_forecast':snapshot},self.at,self.cfg,22)
        self.assertTrue(info['used']);self.assertEqual(curve[self.at],22)
        self.assertAlmostEqual(curve[self.at+timedelta(minutes=30)],24)
        self.assertEqual(curve[self.at+timedelta(hours=1)],26)

    def test_wrong_city_stale_missing_gap_and_large_bias_reject_forecast(self):
        for change in ('city','stale','future','gap','end','duplicate','bias','nan','bool','source'):
            with self.subTest(change=change):
                snapshot=weather(self.at,value=20)
                if change=='city':snapshot['city_id']='101200101'
                if change=='stale':snapshot['fetched_at']=(self.at-timedelta(seconds=1801)).isoformat()
                if change=='future':snapshot['fetched_at']=(self.at+timedelta(seconds=1)).isoformat()
                if change=='gap':snapshot['hourly'].pop(2)
                if change=='end':snapshot['hourly']=snapshot['hourly'][:3]
                if change=='duplicate':snapshot['hourly'].insert(2,deepcopy(snapshot['hourly'][1]))
                if change=='bias':snapshot['hourly'][0]['temperature_c']=10
                if change=='nan':snapshot['hourly'][1]['temperature_c']=float('nan')
                if change=='bool':snapshot['hourly'][1]['temperature_c']=True
                if change=='source':snapshot['source']='daily'
                curve,info=model.hourly_weather({'weather_forecast':snapshot},self.at,self.cfg,22)
                self.assertIsNone(curve);self.assertFalse(info['used'])

    def test_real_predictor_consumes_hourly_future_not_only_current_outdoor(self):
        cfg=self.cfg
        def series(v):return [{'at':(self.at-timedelta(minutes=i)).isoformat(),'value':v} for i in range(60,0,-2)]
        payload={'evaluated_at':self.at.isoformat(),'series':{k:series(v) for k,v in [('outdoor',20),('running',0),('fault',0),('start_output',0)]},
                 'ahus':[{'ahu':n,**{k:series(v) for k,v in [('return',25),('running',1),('frequency',50),('fresh',100)]}} for n in (1,4)]}
        fixture={'data_cutoff':'2026-11-05T00:00:00+08:00','fan_startup_exclusion_minutes':30,'response_minutes':{},
                 'warming_windows':[{'ahu':1,'date':'2026-11-01','end':'2026-11-01T10:00:00+08:00',
                     'outdoor_c':20,'return_c':25,'hour':12,'fresh_percent':100,'frequency_hz':50,'rate_c_per_hour':0}]}
        with patch.object(model,'historical_projection',side_effect=lambda m,n,air,*a:
                          {'rate':max(0,(air-20)/3),'days':5,'neighbors':10,'supported':True}):
            cold=model.forecast(dict(payload,weather_forecast=weather(self.at,value=20)),fixture,cfg)
            hot_snapshot=weather(self.at,value=28);hot_snapshot['hourly'][0]['temperature_c']=20
            hot=model.forecast(dict(payload,weather_forecast=hot_snapshot),fixture,cfg)
        self.assertTrue(hot['weather_prediction']['used'])
        self.assertLess(control.local_time(hot['tentative_start_at']),control.local_time(cold['tentative_start_at']))


if __name__=='__main__':unittest.main()
