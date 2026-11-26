"""温升修正的边界测试；人工样本，不读取现场数据或网络。"""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import unittest
from algorithms.hangxin_start_forecast_mean import forecast as f

AT=datetime(2026,11,9,17,tzinfo=f.ZONE)


def fixture():
    def series(value):return [{'at':(AT-timedelta(minutes=m)).isoformat(),'value':value} for m in range(120,0,-2)]
    cfg=json.loads(Path('algorithms/hangxin_start_forecast_mean/forecast_config.json').read_text())
    cfg['forecast_horizon_minutes']=150
    p={'evaluated_at':AT.isoformat(),'series':{k:series(v) for k,v in [('outdoor',20),('running',0),('start_output',0),('fault',0)]},
       'ahus':[{'ahu':n,**{k:series(v) for k,v in [('return',26),('running',1),('frequency',50),('fresh',100)]}} for n in (1,4)],
       'weather_forecast':{'source':'NowAPI','upstream_app':'weather.realtime','city':'芒市','city_id':'101291508','timezone':'Asia/Shanghai','unit':'℃','fetched_at':(AT-timedelta(seconds=1)).isoformat(),
                           'hourly':[{'time':(AT+timedelta(hours=i)).isoformat(),'temperature_c':20-2*i} for i in range(5)]}}
    rows=[{'ahu':n,'date':f'2026-11-{d:02d}','end':f'2026-11-{d:02d}T17:00:00+08:00','hour':17,'outdoor_c':24,'return_c':26,'fresh_percent':100,'frequency_hz':50,'rate_c_per_hour':1}
          for n in (1,4) for d in range(1,7) for _ in range(2)]
    return p,{'data_cutoff':'2026-11-08T00:00:00+08:00','fan_startup_exclusion_minutes':30,'warming_windows':rows,'response_minutes':{}},cfg


class RobustProjectionTests(unittest.TestCase):
    def test_open_fresh_never_borrows_closed_fresh_history(self):
        p,m,c=fixture()
        for row in m['warming_windows']:row['fresh_percent']=0
        result=f.historical_projection(m,1,24,26,100,50,17,c)
        self.assertIsNone(result['rate']);self.assertIn('相同新风',result['reason'])

    def test_cold_weather_outside_training_is_not_a_positive_rate(self):
        p,m,c=fixture();r=f.historical_projection(m,1,20,26,100,50,17,c)
        self.assertIsNone(r['rate']);self.assertFalse(r['supported'])
        result=f.forecast(p,m,c);self.assertTrue(result['automatic_adjustment_allowed'])
        self.assertAlmostEqual(result['group_prediction']['group_curve'][-1]['return_c'],26)

    def test_recent_decline_can_override_history_in_near_future(self):
        self.assertLess(f.projected_rate(1,-.7,-.1,2.5,fixture()[2]),0)

    def test_short_spike_is_not_a_150_minute_positive_floor(self):
        cfg=fixture()[2]
        early=f.projected_rate(None,1,0,2.5,cfg);late=f.projected_rate(None,1,0,150,cfg)
        self.assertGreater(early,.5);self.assertLess(late,.001)

    def test_fast_near_term_rise_still_blocks_cancellation(self):
        p,m,c=fixture()
        for item in p['ahus']:
            for row in item['return']:
                minutes=(AT-datetime.fromisoformat(row['at'])).total_seconds()/60
                row['value']=27.9-2*(minutes-2)/60 if minutes<=14 else 27.9
        result=f.forecast(p,m,c)
        self.assertIsNotNone(result['group_prediction']['short_term_guard_crossing_at'])
        from hangxin_start_day_policy import cancellation_assessment
        self.assertFalse(cancellation_assessment(result,AT,AT.replace(hour=19,minute=30))['allowed'])

    def test_unsupported_warming_weather_is_display_only(self):
        p,m,c=fixture()
        for row in p['series']['outdoor']:row['value']=28
        for i,row in enumerate(p['weather_forecast']['hourly']):row['temperature_c']=28+i
        result=f.forecast(p,m,c)
        self.assertFalse(result['automatic_adjustment_allowed'])

    def test_unsupported_without_long_trend_does_not_remove_known_hot_sensor(self):
        p,m,c=fixture();hot=deepcopy(p['ahus'][0]);hot['ahu']=2
        for k in ('return','running','frequency','fresh'):hot[k]=hot[k][-2:]
        for row in hot['return']:row['value']=31
        p['ahus'].append(hot);r=f.forecast(p,m,c)
        self.assertIn(2,r['group_prediction']['members']);self.assertFalse(r['automatic_adjustment_allowed'])
        self.assertIn(2,[v['ahu'] for v in r['individual_hotspots']])

    def test_morning_history_is_not_evening_support(self):
        p,m,c=fixture()
        for row in m['warming_windows']:row['hour']=10
        self.assertFalse(f.historical_projection(m,1,24,26,100,50,17,c)['supported'])

    def test_valid_same_regime_history_is_still_used(self):
        p,m,c=fixture();r=f.historical_projection(m,1,24,26,100,50,17,c)
        self.assertTrue(r['supported']);self.assertEqual(r['rate'],1)

    def test_after_evaluation_measurements_cannot_change_forecast(self):
        p,m,c=fixture();before=f.forecast(p,m,c)
        for item in p['ahus']:item['return'].append({'at':(AT+timedelta(minutes=30)).isoformat(),'value':40})
        self.assertEqual(before,f.forecast(p,m,c))

if __name__=='__main__':unittest.main()
