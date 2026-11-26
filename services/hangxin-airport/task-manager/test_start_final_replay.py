"""17点修正版回放的补执行：使用真实当前时刻，不重写原轮次，不放宽普通调度。"""
from datetime import timedelta
import hashlib,json
from pathlib import Path
import unittest

import hangxin_start_forecast as c
import test_hangxin_start_day_policy as helpers
from test_hangxin_start_forecast import BASE,HOUR,MINUTE,RUNNING


class FinalReplayTests(unittest.TestCase):
    def setUp(self):
        self.helper=helpers.DayPolicyTests('test_final_cold_round_sets_only_first_start_to_noon_without_switch_write')
        self.helper.setUp();self.addCleanup(self.helper.doCleanups)
        self.h=self.helper;self.h.clock.current=BASE.replace(hour=18)
        self.h.platform.values[HOUR]=18;self.h.platform.values[MINUTE]=30
        source=self.h.predict({'evaluated_at':BASE.replace(hour=17).isoformat()},None,{'forecast_horizon_minutes':235})
        source['automatic_adjustment_allowed']=True
        self.report={'source_slot':BASE.replace(hour=17).isoformat(),'forecast':source,
                     'source_model':json.loads((c.ALGORITHM_DIR/'forecast_config.json').read_text())['version'],
                     'expected_start_at':BASE.replace(hour=18,minute=30).isoformat(),'payload_sha256':'a'*64}

    def execute(self,report=None):
        return c.execute(self.h.platform,self.h.root,clock=self.h.clock,sleep=self.h.clock.sleep,gate=lambda:True,
                         predictor=self.h.predictor,weather=self.h.weather,manual=True,final_replay=report or self.report)

    def test_late_replay_uses_real_audit_slot_and_sets_noon_only(self):
        r=self.execute();self.assertEqual(r['status'],'confirmed')
        self.assertEqual(r['slot'],BASE.replace(hour=18).isoformat())
        self.assertEqual(r['replay_source_slot'],BASE.replace(hour=17).isoformat())
        self.assertTrue(r['cold_day_assessment']['allowed']);self.assertTrue(r['replay_source_assessment']['allowed'])
        self.assertEqual([(p,v) for _,p,v in self.h.platform.writes],[(HOUR,12),(MINUTE,0)])

    def test_ordinary_manual_eighteen_remains_outside_schedule(self):
        r=c.execute(self.h.platform,self.h.root,clock=self.h.clock,manual=True,gate=lambda:True)
        self.assertEqual(r['status'],'skipped_window');self.assertEqual(self.h.platform.writes,[])

    def test_exact_twenty_minute_remaining_does_not_send(self):
        self.h.clock.current=BASE.replace(hour=18,minute=10)
        self.assertEqual(self.execute()['status'],'skipped_replay');self.assertEqual(self.h.platform.reads,[])

    def test_changed_current_plan_does_not_override_manual_change(self):
        self.h.platform.values[HOUR]=19
        self.assertEqual(self.execute()['status'],'external_change');self.assertEqual(self.h.platform.writes,[])

    def test_live_hot_forecast_prevents_historical_cancellation(self):
        original=self.h.predict
        def hot(*args):
            r=original(*args);r['group_prediction']['group_curve'][-1]['return_c']=28.3;return r
        self.h.predictor.side_effect=hot
        self.assertEqual(self.execute()['status'],'replay_not_cancelled');self.assertEqual(self.h.platform.writes,[])

    def test_old_forecast_not_supporting_cancellation_does_not_send(self):
        self.report['forecast']['group_prediction']['group_curve'][-1]['return_c']=28.3
        self.assertEqual(self.execute()['status'],'replay_not_cancelled');self.assertEqual(self.h.platform.writes,[])

    def test_wrong_day_or_model_or_forged_slot_is_rejected_before_read(self):
        for key,value in [('source_model','old'),('source_slot',BASE.replace(hour=16).isoformat()),('payload_sha256','bad')]:
            with self.subTest(key=key):
                r=self.execute(dict(self.report,**{key:value}));self.assertEqual(r['status'],'skipped_replay')
        self.assertEqual(self.h.platform.reads,[])

    def test_main_running_prevents_replay_write(self):
        self.h.platform.values[RUNNING]=1
        self.assertEqual(self.execute()['status'],'skipped_conditions');self.assertEqual(self.h.platform.writes,[])

if __name__=='__main__':unittest.main()
