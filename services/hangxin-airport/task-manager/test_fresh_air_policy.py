"""验证低温日单段、次日恢复、跨午夜及每次中间写入均不重叠。"""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import fresh_air_policy as policy
import hangxin_second_fresh_schedule as c
from test_hangxin_second_fresh_schedule import Clock,Fake
from test_hangxin_start_platform import points


class ColdDayTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name);self.clock=Clock();self.p=Fake(self.clock)
        self.p.points[c.START[0]]['value']=12;self.p.points[c.START[1]]['value']=0
        for pid,p in points().items():
            if pid.endswith(('c00','c23','c22')):self.p.points[pid]=p
        stops=[(21,30),(19,45),(22,51),(23,0),(22,0)]
        for n,(h,m) in enumerate(stops,1):
            for tag,v in ((28,8),(31,0),(34,h),(37,m),(76,8),(82,5),(88,18),(94,55),(89,22),(95,50)):
                self.p.points[f'ewcnad_msahu{n}_v{tag}']['value']=v
        self.marker={'day':'2026-11-08','slot':'2026-11-08T17:00:00+08:00'}
        self.mock=patch.object(policy,'deferral',side_effect=lambda at:self.marker);self.mock.start();self.addCleanup(self.mock.stop)
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('禁止网络'));self.network.start();self.addCleanup(self.network.stop)

    def cycle(self):return c.execute(self.p,self.path,self.clock,self.clock.sleep,lambda:True)

    def test_cold_day_disables_second_before_extending_first_for_each_ahu(self):
        original=self.p.write;observations=[]
        def write(pid,v):
            result=original(pid,v)
            for n in range(1,6):
                plan=policy.device_plan(self.p.read(),self.clock(),c.FreshStore(self.path).read_state(),n,c.value,self.marker)
                policy.intervals(plan['current'],n,plan['first_open'])
            observations.append(pid);return result
        self.p.write=write;result=self.cycle();self.assertEqual(result['status'],'confirmed')
        self.assertEqual([tuple(self.p.points[p]['value'] for p in pair) for pair in c.FIRST_PAIRS],[(21,25),(19,40),(22,46),(22,55),(21,55)])
        self.assertTrue(all(self.p.points[p]['value']==0 for pair in [*c.PAIRS,*c.SECOND_CLOSE_PAIRS] for p in pair))
        for n in (1,3,4,5):
            first=min(observations.index(p) for p in c.FIRST_PAIRS[n-1] if p in observations)
            self.assertLess(max(observations.index(p) for pair in (c.PAIRS[n-1],c.SECOND_CLOSE_PAIRS[n-1]) for p in pair if p in observations),first)
        self.assertTrue(all(v['original_second'][2:]==[22,50] and v['status']=='disabled' for v in c.FreshStore(self.path).read_state()['second_segment_baselines'].values()))

    def test_next_normal_day_restores_saved_second_close_and_dynamic_start(self):
        self.cycle();self.p.writes.clear();self.marker=None
        self.clock.at=self.clock.at.replace(day=9,hour=9,minute=0,second=0)
        self.p.points[c.START[0]]['value']=14
        r=self.cycle();self.assertEqual(r['status'],'confirmed')
        self.assertTrue(all(tuple(self.p.points[p]['value'] for p in pair)==(22,50) for pair in c.SECOND_CLOSE_PAIRS))
        self.assertTrue(all(tuple(self.p.points[p]['value'] for p in pair)==(20,0) for pair in c.PAIRS))
        self.assertTrue(all(tuple(self.p.points[p]['value'] for p in pair)==(14,0) for pair in c.FIRST_PAIRS))

    def test_plain_noon_without_confirmed_deferral_does_not_disable_second(self):
        self.marker=None;r=self.cycle()
        self.assertNotIn('cancelled_day_single_segment',r['policy_modes'].values())
        self.assertFalse(any(pid in {p for pair in c.SECOND_CLOSE_PAIRS for p in pair} for pid,_ in self.p.writes))

    def test_manual_second_change_is_not_overwritten_by_restore(self):
        self.cycle();self.p.writes.clear();self.marker=None
        self.clock.at=self.clock.at.replace(day=9,hour=9,minute=0,second=0);self.p.points[c.START[0]]['value']=14
        for n in range(1,6):
            for tag,v in ((77,21),(83,0),(89,23),(95,0)):self.p.points[f'ewcnad_msahu{n}_v{tag}']['value']=v
        self.cycle()
        self.assertFalse(any(pid in {p for pair in [*c.PAIRS,*c.SECOND_CLOSE_PAIRS] for p in pair} for pid,_ in self.p.writes))

    def test_originally_disabled_second_is_not_enabled_next_day(self):
        for pair in [*c.PAIRS,*c.SECOND_CLOSE_PAIRS]:
            for pid in pair:self.p.points[pid]['value']=0
        self.cycle();self.p.writes.clear();self.marker=None
        self.clock.at=self.clock.at.replace(day=9,hour=9,minute=0,second=0);self.p.points[c.START[0]]['value']=14
        self.cycle();self.assertTrue(all(self.p.points[p]['value']==0 for pair in [*c.PAIRS,*c.SECOND_CLOSE_PAIRS] for p in pair))

    def test_cross_midnight_stop_remains_on_correct_date(self):
        for tag,v in ((34,1),(37,0)):self.p.points[f'ewcnad_msahu1_v{tag}']['value']=v
        r=self.cycle();self.assertEqual(r['status'],'confirmed')
        self.assertEqual(r['segment_target_times']['first_close:AHU1'],'2026-11-09T00:55:00+08:00')
        self.assertEqual(tuple(self.p.points[p]['value'] for p in c.FIRST_PAIRS[0]),(0,55))
        self.clock.at=self.clock.at.replace(day=9,hour=0,minute=10,second=0);self.p.writes.clear()
        self.cycle();self.assertEqual(self.p.writes,[])

    def test_overnight_intervals_detect_overlap_on_both_calendar_sides(self):
        n=1;groups=policy.pairs(n)
        v={groups['first_close'][0]:1,groups['first_close'][1]:0,groups['second_open'][0]:23,groups['second_open'][1]:30,groups['second_close'][0]:2,groups['second_close'][1]:0}
        with self.assertRaises(ValueError):policy.intervals(v,n,20*60)
        for p in groups['second_open']+groups['second_close']:v[p]=0
        policy.intervals(v,n,20*60)

    def test_running_main_cannot_extend_first_as_cancelled_day(self):
        self.p.points['nfdh9q_mscrk01_c00']['value']=1
        r=self.cycle();self.assertEqual(r['status'],'skipped_conditions');self.assertFalse(self.p.writes)

    def test_unknown_old_request_does_not_block_healthy_other_ahu(self):
        self.p.apply=False;r=self.cycle();self.assertEqual(r['status'],'unconfirmed')
        before=list(self.p.writes);self.clock.at+=timedelta(minutes=5);self.cycle()
        self.assertEqual(self.p.writes,before)


class DeferralSourceTests(unittest.TestCase):
    def test_only_confirmed_deferral_and_following_forecast_are_recognized(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'main.sqlite3';at=datetime(2026,11,8,18,tzinfo=c.ZONE)
            row={'slot':'2026-11-08T17:00:00+08:00','status':'confirmed','device_id':12788,'operation':'defer_start_to_noon',
                 'target_at':'2026-11-08T12:00:00+08:00','effective_next_start_at':'2026-11-09T12:00:00+08:00',
                 'finished_at':'2026-11-08T17:03:00+08:00','cold_day_assessment':{'allowed':True},'attempted':True}
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE runs(slot TEXT,record TEXT)');db.execute('INSERT INTO runs VALUES(?,?)',(row['slot'],json.dumps(row)))
            self.assertEqual(policy.deferral(at,path)['day'],'2026-11-08')
            self.assertIsNotNone(policy.deferral(at.replace(day=9,hour=0),path))
            with sqlite3.connect(path) as db:
                db.execute('INSERT INTO runs VALUES(?,?)',('2026-11-09T08:00:00+08:00',json.dumps({'slot':'2026-11-09T08:00:00+08:00','prediction_completed':True})))
            self.assertIsNone(policy.deferral(at.replace(day=9,hour=9),path))

    def test_unconfirmed_deferral_is_not_used_to_change_fresh_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'main.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE runs(slot TEXT,record TEXT)')
                row={'slot':'2026-11-08T17:00:00+08:00','operation':'defer_start_to_noon','status':'unconfirmed','attempted':True}
                db.execute('INSERT INTO runs VALUES(?,?)',(row['slot'],json.dumps(row)))
            with self.assertRaises(ValueError):policy.deferral(datetime(2026,11,8,18,tzinfo=c.ZONE),path)

if __name__=='__main__':unittest.main()
