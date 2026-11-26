"""粗预测的离线行为测试，验证候选、半小时修正、时间截断和准入隔离。"""
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
import json,unittest
from unittest.mock import patch
from forecast import forecast,rolling_tick,ForecastError,fan_recovery,recent_trend,aggregate_crossing_fraction

ROOT=Path(__file__).resolve().parent
NOW=datetime.fromisoformat('2026-11-05T10:00:00+08:00')


def make_series(now,value):
    """连续原始样本只在判断时刻之前，避免把整分桶当作提前已知。"""
    return [{'at':(now-timedelta(minutes=m)).isoformat(),'value':value} for m in range(60,0,-2)]


def fixture(now=NOW):
    """人工历史有多个日期，和生产点位/账号无关。"""
    cfg=json.loads((ROOT/'forecast_config.json').read_text())
    p={'evaluated_at':now.isoformat(),'series':{k:make_series(now,v) for k,v in [('outdoor',22),('fault',0),('running',0),('start_output',0)]},
       'ahus':[{'ahu':1,**{k:make_series(now,v) for k,v in [('return',26),('running',1),('frequency',50),('fresh',100)]}}],
       'flow_normal':None,'circulation_confirmed':False,'original_schedule_start_at':now.replace(hour=12).isoformat()}
    windows=[{'ahu':1,'date':f'2026-10-{d:02d}','end':f'2026-10-{d:02d}T11:30:00+08:00','hour':11,
              'outdoor_c':22,'return_c':26,'fresh_percent':100,'frequency_hz':50,'rate_c_per_hour':.8} for d in range(20,30)]
    p['ahus'].append(dict(deepcopy(p['ahus'][0]),ahu=4))
    windows.extend([dict(r,ahu=4) for r in list(windows)])
    model={'data_cutoff':'2026-11-05T00:00:00+08:00','fan_startup_exclusion_minutes':cfg['fan_startup_exclusion_minutes'],
           'warming_windows':windows,'response_minutes':{'1':30,'4':30}}
    return p,model,cfg


def fan_case(now,started_minutes=50):
    """包含明确停→运行边沿，用于恢复隔离及真实持续温升的验证。"""
    records=list(range(120,0,-2));start=now-timedelta(minutes=started_minutes)
    item={'ahu':1,'return':[],'running':[],'frequency':[],'fresh':[]}
    for m in records:
        t=now-timedelta(minutes=m);active=t>=start
        for key,value in [('return',26-m*.2/60),('running',int(active)),('frequency',50 if active else 0),('fresh',100)]:
            item[key].append({'at':t.isoformat(),'value':value})
    return item


def pair(item):
    """两个相同参考仅用于斜率边界测试，满足组统计最小数量。"""
    return [item,dict(deepcopy(item),ahu=4)]


class ForecastTests(unittest.TestCase):
    """结果不含真实许可和命令；水温彻底不参与热侧候选或时间调整。"""
    def setUp(self):
        self.p,self.model,self.cfg=fixture()
        guard=patch('socket.socket',side_effect=AssertionError('禁止联网'));guard.start();self.addCleanup(guard.stop)
    def check(self,p=None,model=None):
        r=forecast(p or self.p,model or self.model,self.cfg)
        self.assertEqual(r['device_writes'],[]);self.assertFalse(r['applied']);self.assertFalse(r['compressor_start_permitted'])
        return r
    def test_missing_water_gives_candidate_and_keeps_flow_unknown(self):
        """不再需要水温，水流诊断及真实启动许可仍独立保留。"""
        r=self.check();self.assertEqual(r['status'],'tentative');self.assertIsNotNone(r['tentative_start_at'])
        self.assertFalse(r['water_temperature_required']);self.assertFalse(r['water_and_flow_must_be_rechecked'])
        for field in ('water_c','water_threshold_met_now','water_ready_eta'):self.assertIsNone(r[field])
        self.assertIsNone(r['flow_normal_confirmed']);self.assertTrue(r['flow_must_be_rechecked'])
        self.assertTrue(any('水流' in warning for warning in r['warnings']))
        self.assertFalse(any('水温' in warning or '25.5' in warning for warning in r['warnings']))
    def test_low_high_stale_or_invalid_water_never_changes_thermal_forecast(self):
        """旧调用方即使继续传入错误水温，也不能使热侧预测失效或推迟。"""
        baseline=self.check()
        cases=[make_series(NOW,0),make_series(NOW,40),[],None,'invalid',
               [{'at':'invalid-time','value':float('nan')}],
               [{'at':(NOW-timedelta(days=1)).isoformat(),'value':20}],
               [{'at':(NOW+timedelta(hours=1)).isoformat(),'value':True}]]
        for water in cases:
            p=deepcopy(self.p);p['series']['inlet_water']=water
            with self.subTest(water_type=type(water).__name__):self.assertEqual(self.check(p),baseline)
    def test_verified_circulating_water_eta_never_delays_thermal_candidate(self):
        """已有水流与连续循环证明，也不再启用旧水温ETA调整分支。"""
        self.p.update(flow_normal={'value':True,'observed_at':(NOW-timedelta(seconds=1)).isoformat()},circulation_confirmed=True)
        for item in self.p['ahus']:item['return']=make_series(NOW,28.15)
        baseline=self.check();self.assertEqual(baseline['tentative_start_at'],NOW.isoformat())
        self.p['series']['inlet_water']=[{'at':(NOW-timedelta(minutes=m)).isoformat(),'value':25.4-m*.02} for m in range(30,0,-2)]
        self.p['circulating_water_samples']=[dict(row,flow_normal=True,cooling_pump_running=True) for row in self.p['series']['inlet_water']]
        result=self.check();self.assertEqual(result,baseline)
        self.assertIsNone(result['water_ready_eta']);self.assertTrue(result['flow_normal_confirmed']);self.assertFalse(result['flow_must_be_rechecked'])
    def test_water_threshold_configuration_is_no_longer_required_or_validated(self):
        """移除旧阈值配置不会丢失候选；残留的无关字段不会成为隐藏门槛。"""
        self.assertNotIn('water_threshold_c',self.cfg)
        baseline=self.check()
        for value in (None,0,25.5,100,'invalid'):
            cfg=dict(self.cfg,water_threshold_c=value)
            with self.subTest(value=value):self.assertEqual(forecast(self.p,self.model,cfg),baseline)
    def test_water_policy_upgrade_discards_same_slot_legacy_cache(self):
        """配置版本改变后重算同格点，不能复用带旧水温延期的缓存。"""
        legacy=dict(self.cfg,version='rough-start-forecast-v4.1-mean',water_threshold_c=25.5)
        self.assertNotEqual(self.cfg['version'],legacy['version'])
        first=rolling_tick(self.p,self.model,legacy)
        first['next_state']['last_forecast'].update(tentative_start_at='2026-11-05T15:00:00+08:00',water_ready_eta='2026-11-05T15:00:00+08:00',water_c=24)
        p,_,_=fixture(NOW+timedelta(minutes=5))
        updated=rolling_tick(p,self.model,self.cfg,first['next_state'])
        self.assertTrue(updated['refresh_due']);self.assertNotEqual(updated['tentative_start_at'],'2026-11-05T15:00:00+08:00')
        self.assertIsNone(updated['water_ready_eta']);self.assertIsNone(updated['water_c'])
        self.assertEqual(updated['fixed_freeze_deadline'],first['fixed_freeze_deadline'])
        self.assertNotEqual(updated['next_state']['policy_signature'],first['next_state']['policy_signature'])
    def test_future_measurements_cannot_change_current_prediction(self):
        first=self.check();future={'at':(NOW+timedelta(minutes=30)).isoformat(),'value':40}
        for s in self.p['series'].values():s.append(future)
        for s in self.p['ahus'][0].values():
            if isinstance(s,list):s.append(future)
        self.assertEqual(first,self.check())
    def test_current_minute_sample_is_not_assumed_already_arrived(self):
        first=self.check();self.p['series']['outdoor'].append({'at':NOW.isoformat(),'value':35})
        self.assertEqual(first,self.check())
    def test_future_training_labels_are_rejected(self):
        self.model['warming_windows'][0]['end']='2026-11-05T10:30:00+08:00'
        self.assertEqual(self.check()['status'],'insufficient_data')
    def test_scheduled_updates_are_exactly_whole_or_half_hour(self):
        r=rolling_tick(self.p,self.model,self.cfg);self.assertEqual(r['next_refresh_at'],'2026-11-05T10:30:00+08:00')
        p,_,_=fixture(NOW+timedelta(minutes=15));cached=rolling_tick(p,self.model,self.cfg,r['next_state'])
        self.assertFalse(cached['refresh_due']);self.assertEqual(cached['evaluated_at'],self.p['evaluated_at'])
        p,_,_=fixture(NOW+timedelta(minutes=30));updated=rolling_tick(p,self.model,self.cfg,cached['next_state'])
        self.assertTrue(updated['refresh_due']);self.assertEqual(updated['next_refresh_at'],'2026-11-05T11:00:00+08:00')
    def test_new_temperature_changes_next_half_hour_prediction(self):
        first=rolling_tick(self.p,self.model,self.cfg)
        p,_,_=fixture(NOW+timedelta(minutes=30));p['ahus'][0]['return']=make_series(NOW+timedelta(minutes=30),27.8)
        second=rolling_tick(p,self.model,self.cfg,first['next_state'])
        self.assertLess(second['tentative_start_at'],first['tentative_start_at'])
    def test_native_fault_stops_forecast_even_inside_same_slot(self):
        first=rolling_tick(self.p,self.model,self.cfg);p,_,_=fixture(NOW+timedelta(minutes=5));p['series']['fault']=make_series(NOW+timedelta(minutes=5),1)
        r=rolling_tick(p,self.model,self.cfg,first['next_state']);self.assertEqual(r['status'],'fault_hold');self.assertIsNone(r['tentative_start_at'])
    def test_start_output_or_running_stops_new_start_forecasts(self):
        for k in ('running','start_output'):
            p=deepcopy(self.p);p['series'][k]=make_series(NOW,1);self.assertEqual(self.check(p)['status'],'already_starting_or_running')
    def test_fan_off_or_stale_ahu_is_not_extrapolated(self):
        self.p['ahus'][0]['running']=make_series(NOW,0);self.assertIsNone(self.check()['tentative_start_at'])
        self.p,_,_=fixture();self.p['ahus'][0]['return']=make_series(NOW-timedelta(hours=1),26)
        self.assertIsNone(self.check()['tentative_start_at'])
    def test_ahu3_never_used_as_1f_temperature_reference(self):
        item=deepcopy(self.p['ahus'][0]);item['ahu']=3;item['return']=make_series(NOW,35)
        self.p['ahus'].append(item);r=self.check();self.assertEqual([x['ahu'] for x in r['ahu_forecasts']],[1,4])
    def test_original_freeze_does_not_move_with_new_candidate(self):
        first=rolling_tick(self.p,self.model,self.cfg)
        p,_,_=fixture(NOW+timedelta(minutes=30));p['original_schedule_start_at']='2026-11-05T15:00:00+08:00'
        second=rolling_tick(p,self.model,self.cfg,first['next_state']);self.assertEqual(second['fixed_freeze_deadline'],'2026-11-05T11:45:00+08:00')
    def test_old_thirty_minute_state_migrates_without_using_postponed_input(self):
        state=rolling_tick(self.p,self.model,self.cfg)['next_state']
        state.pop('freeze_minutes');state.pop('original_schedule_start_at')
        state['fixed_freeze_deadline']='2026-11-05T11:30:00+08:00'
        state['forecast_version']='rough-start-forecast-v5-mean-hourly-weather'
        p,_,_=fixture(NOW+timedelta(minutes=30));p['original_schedule_start_at']='2026-11-05T15:00:00+08:00'
        result=rolling_tick(p,self.model,self.cfg,state)
        self.assertEqual(result['fixed_freeze_deadline'],'2026-11-05T11:45:00+08:00')
        self.assertEqual(result['next_state']['original_schedule_start_at'],'2026-11-05T12:00:00+08:00')
        self.assertEqual(result['next_state']['freeze_minutes'],15)
    def test_current_time_plus_twenty_minutes_is_the_candidate_boundary(self):
        """用固定预测候选验证核心同时保留5分钟确认，不受温度模型浮动干扰。"""
        from forecast import SCHEDULE_FREEZE_MINUTES,SCHEDULE_CONFIRMATION_MINUTES
        self.assertEqual((SCHEDULE_FREEZE_MINUTES,SCHEDULE_CONFIRMATION_MINUTES),(15,5))
        # 令模型候选为10:20；即使原计划12点很远，也不能在只剩20分钟时放行。
        with patch('forecast.floor_time',return_value=NOW+timedelta(minutes=20)):
            result=forecast(self.p,self.model,self.cfg,'2026-11-05T11:45:00+08:00')
        self.assertFalse(result['schedule_change_window_open'])
        with patch('forecast.floor_time',return_value=NOW+timedelta(minutes=25)):
            result=forecast(self.p,self.model,self.cfg,'2026-11-05T11:45:00+08:00')
        self.assertTrue(result['schedule_change_window_open'])
    def test_predictions_continue_after_schedule_freeze_without_commands(self):
        r=forecast(self.p,self.model,self.cfg,'2026-11-05T09:30:00+08:00')
        self.assertIsNotNone(r['tentative_start_at']);self.assertFalse(r['schedule_change_window_open']);self.assertFalse(r['device_writes'])
    def test_json_persistent_state_reuses_cache_and_rollbacks_rejected(self):
        first=rolling_tick(self.p,self.model,self.cfg);state=json.loads(json.dumps(first['next_state']))
        self.assertFalse(rolling_tick(self.p,self.model,self.cfg,state)['refresh_due'])
        p,_,_=fixture(NOW-timedelta(minutes=30))
        with self.assertRaises(ForecastError):rolling_tick(p,self.model,self.cfg,state)
    def test_weather_trend_is_bounded(self):
        self.p['series']['outdoor']=[{'at':(NOW-timedelta(minutes=m)).isoformat(),'value':22+(60-m)*.2} for m in range(60,0,-2)]
        self.assertLessEqual(self.check()['outdoor_trend_c_per_hour'],2)
    def test_daily_rollover_resets_original_freeze(self):
        first=rolling_tick(self.p,self.model,self.cfg);p,_,_=fixture(NOW+timedelta(days=1))
        r=rolling_tick(p,self.model,self.cfg,first['next_state']);self.assertTrue(r['fixed_freeze_deadline'].startswith('2026-11-06'))
    def test_startup_temperature_recovery_does_not_change_forecast_slope(self):
        item=fan_case(NOW);self.p['ahus']=pair(item);baseline=self.check()
        cutoff=NOW-timedelta(minutes=20)
        for r in item['return']:
            if NOW-timedelta(minutes=50)<=datetime.fromisoformat(r['at'])<cutoff:r['value']=30
        changed=self.check();self.assertEqual(baseline,changed)
        self.assertAlmostEqual(changed['ahu_forecasts'][0]['recent_rate_c_per_hour'],.2)
    def test_exclusion_keeps_latest_hot_temperature_without_single_veto(self):
        item=fan_case(NOW,20);item['return'][-1]['value']=28.6;self.p['ahus'].insert(0,dict(item,ahu=2))
        self.model['warming_windows'].extend([dict(r,ahu=2) for r in self.model['warming_windows'] if r['ahu']==1])
        r=self.check();a=next(x for x in r['ahu_forecasts'] if x['ahu']==2)
        self.assertEqual(a['return_c'],28.6);self.assertIsNone(a['recent_rate_c_per_hour'])
        self.assertEqual(a['fan_recovery']['phase'],'excluding_startup');self.assertGreater(r['tentative_start_at'],NOW.isoformat())
        self.assertEqual(r['individual_hotspots'][0]['ahu'],2)
    def test_short_post_recovery_segment_uses_history_without_false_slope(self):
        self.p['ahus']=pair(fan_case(NOW,38));r=self.check();a=r['ahu_forecasts'][0]
        self.assertEqual(a['fan_recovery']['phase'],'waiting_post_recovery_samples');self.assertIsNone(a['recent_rate_c_per_hour'])
        self.assertIsNotNone(r['tentative_start_at'])
    def test_persistent_fast_warming_after_recovery_still_advances_candidate(self):
        item=fan_case(NOW,110);self.p['ahus']=pair(item);slow=self.check()
        for r in item['return']:
            m=(NOW-datetime.fromisoformat(r['at'])).total_seconds()/60;r['value']=26-2*m/60
        fast=self.check();self.assertAlmostEqual(fast['ahu_forecasts'][0]['recent_rate_c_per_hour'],2)
        self.assertLess(fast['tentative_start_at'],slow['tentative_start_at'])
    def test_latest_windows_reduce_weight_of_earlier_fast_segment(self):
        item=fan_case(NOW,110)
        for r in item['return']:
            m=(NOW-datetime.fromisoformat(r['at'])).total_seconds()/60
            r['value']=26-.2*min(m,20)/60-2*max(0,m-20)/60
        rate,recovery,parts=recent_trend(item,NOW,self.cfg)
        self.assertEqual(recovery['phase'],'post_recovery_trend');self.assertLess(rate,parts[-1]['rate_c_per_hour'])
        self.assertGreater(rate,parts[0]['rate_c_per_hour'])
    def test_running_data_gap_resets_recovery_without_inventing_start_edge(self):
        item=fan_case(NOW,110)
        item['running']=[r for r in item['running'] if not NOW-timedelta(minutes=30)<datetime.fromisoformat(r['at'])<NOW-timedelta(minutes=18)]
        recovery=fan_recovery(item,NOW,self.cfg)
        self.assertEqual(recovery['segment_start'],(NOW-timedelta(minutes=18)).isoformat());self.assertFalse(recovery['edge_confirmed'])
    def test_missing_frequency_is_not_an_observed_off_to_on_event(self):
        item=fan_case(NOW,110)
        item['frequency']=[r for r in item['frequency'] if not NOW-timedelta(minutes=18)<datetime.fromisoformat(r['at'])<NOW-timedelta(minutes=4)]
        recovery=fan_recovery(item,NOW,self.cfg);self.assertFalse(recovery['edge_confirmed'])
        self.assertEqual(recovery['segment_start'],(NOW-timedelta(minutes=4)).isoformat())
    def test_history_recovery_policy_must_match_current_policy(self):
        for value in (10,None):
            self.model['fan_startup_exclusion_minutes']=value
            self.assertEqual(self.check()['status'],'insufficient_data')
    def test_prestart_objective_removes_poststart_cooling_lead(self):
        modern=self.check();legacy=deepcopy(self.cfg);legacy.update(planning_objective='post_start_cooling',extra_margin_minutes=10)
        earlier=forecast(self.p,self.model,legacy)
        delta=(datetime.fromisoformat(modern['tentative_start_at'])-datetime.fromisoformat(earlier['tentative_start_at'])).total_seconds()/60
        self.assertEqual(delta,40)
        row=modern['ahu_forecasts'][0];self.assertEqual(row['response_minutes'],0);self.assertEqual(row['extra_margin_minutes_applied'],0)
        self.assertEqual(row['startup_allowance_minutes'],10)
    def test_historical_cooling_response_no_longer_changes_prestart_candidate(self):
        first=self.check();self.model['response_minutes']['1']=150
        cfg=deepcopy(self.cfg);cfg['extra_margin_minutes']=50
        r=forecast(self.p,self.model,cfg)
        self.assertEqual(first['tentative_start_at'],r['tentative_start_at']);self.assertEqual(r['ahu_forecasts'][0]['lead_before_boundary_minutes'],10)
    def test_crossing_interpolation_does_not_schedule_actual_start_after_limit(self):
        cfg=deepcopy(self.cfg);cfg['return_reserve_c']=0
        # 输入持续0.8℃/小时的真实趋势，验证插值，不把旧的全程历史斜率当作新模型契约。
        for item in self.p['ahus']:
            for row in item['return']:
                minutes=(NOW-datetime.fromisoformat(row['at'])).total_seconds()/60
                row['value']=26-.8*(minutes-2)/60
        r=forecast(self.p,self.model,cfg);row=r['ahu_forecasts'][0]
        boundary=NOW+timedelta(minutes=(28.5-26)/.8*60)
        self.assertEqual(datetime.fromisoformat(row['boundary_at']),boundary)
        actual=datetime.fromisoformat(r['estimated_actual_start_target_at'])
        self.assertLessEqual(actual,boundary);self.assertLessEqual(26+.8*(actual-NOW).total_seconds()/3600,28.5)
    def test_startup_delay_can_make_current_temperature_window_infeasible(self):
        cfg=deepcopy(self.cfg);cfg['return_reserve_c']=0
        for item in self.p['ahus']:item['return']=make_series(NOW,28.4)
        r=forecast(self.p,self.model,cfg)
        self.assertEqual(r['tentative_start_at'],NOW.isoformat());self.assertFalse(r['return_side_time_feasible']);self.assertFalse(r['compressor_start_permitted'])
    def test_policy_upgrade_refreshes_same_slot_without_moving_freeze(self):
        legacy=deepcopy(self.cfg);legacy.update(version='rough-start-forecast-v2',planning_objective='post_start_cooling',extra_margin_minutes=10)
        first=rolling_tick(self.p,self.model,legacy)
        p,_,_=fixture(NOW+timedelta(minutes=5));p['original_schedule_start_at']='2026-11-05T15:00:00+08:00'
        new=rolling_tick(p,self.model,self.cfg,first['next_state'])
        self.assertTrue(new['refresh_due']);self.assertEqual(new['planning_objective'],'prestart_return_limit')
        self.assertEqual(new['fixed_freeze_deadline'],first['fixed_freeze_deadline'])
    def test_removing_only_temperature_reserve_moves_theoretical_boundary_later(self):
        reserved=self.check();cfg=deepcopy(self.cfg);cfg['return_reserve_c']=0
        tight=forecast(self.p,self.model,cfg)
        self.assertGreaterEqual(tight['tentative_start_at'],reserved['tentative_start_at']);self.assertEqual(tight['temperature_error_reserve_c'],0)
    def add_hot_ahu(self,value=31,now=NOW,p=None):
        """同一楼层两个正常参考和一个偏高参考，检验中位数不退回单台触发。"""
        p=self.p if p is None else p
        item=dict(deepcopy(p['ahus'][0]),ahu=2);item['return']=make_series(now,value);p['ahus'].append(item)
        self.model['warming_windows'].extend([dict(r,ahu=2) for r in self.model['warming_windows'] if r['ahu']==1])
        self.model['response_minutes']['2']=30
        return item
    def test_single_high_ahu_is_an_alert_not_median_start_veto(self):
        self.cfg['return_aggregation']='median'
        hot=self.add_hot_ahu(31);first=self.check();hot['return']=make_series(NOW,35);second=self.check()
        self.assertEqual(first['tentative_start_at'],second['tentative_start_at'])
        self.assertEqual(first['group_prediction']['group_curve'],second['group_prediction']['group_curve'])
        self.assertEqual(second['group_prediction']['current_return_c'],26)
        self.assertEqual(second['individual_hotspots'][0]['ahu'],2)
        self.assertIn(2,[x['ahu'] for x in second['predicted_individual_hotspots']])
    def test_mean_and_median_modes_are_explicit_and_differ_with_one_high_ahu(self):
        self.assertEqual(self.cfg['return_aggregation'],'mean')
        self.add_hot_ahu();mean=self.check();cfg=deepcopy(self.cfg);cfg['return_aggregation']='median'
        med=forecast(self.p,self.model,cfg)
        self.assertAlmostEqual(mean['group_prediction']['current_return_c'],(26+26+31)/3)
        self.assertLess(mean['tentative_start_at'],med['tentative_start_at'])
    def test_two_member_median_equals_mean(self):
        self.cfg['return_aggregation']='median'
        self.p['ahus'][1]['return']=make_series(NOW,27);median=self.check()
        cfg=deepcopy(self.cfg);cfg['return_aggregation']='mean';mean=forecast(self.p,self.model,cfg)
        self.assertEqual(median['tentative_start_at'],mean['tentative_start_at'])
        self.assertEqual(median['group_prediction']['group_curve'],mean['group_prediction']['group_curve'])
    def test_group_mode_never_falls_back_to_one_member(self):
        self.p['ahus']=self.p['ahus'][:1];r=self.check()
        self.assertEqual(r['status'],'insufficient_data');self.assertIsNone(r['tentative_start_at'])
    def test_group_members_remain_fixed_when_one_is_high(self):
        self.add_hot_ahu();r=self.check();self.assertEqual(r['group_prediction']['members'],[1,2,4])
        self.assertTrue(all(not x['individual_limit_enforced_for_group_start'] for x in r['ahu_forecasts'] if x.get('used')))
        self.p['ahus'].reverse();again=self.check()
        self.assertEqual(r['tentative_start_at'],again['tentative_start_at']);self.assertEqual(r['group_prediction'],again['group_prediction'])
    def test_group_crossing_handles_rank_change_inside_time_step(self):
        f=aggregate_crossing_fraction([27,28,29],[29,27,30],28.5,'median')
        self.assertAlmostEqual(f,.75)
        peak=aggregate_crossing_fraction([27,28,30],[30,28,27],28.4,'median')
        self.assertAlmostEqual(peak,(28.4-27)/3)
    def test_aggregation_config_change_refreshes_same_slot(self):
        self.cfg['return_aggregation']='median'
        first=rolling_tick(self.p,self.model,self.cfg);cfg=deepcopy(self.cfg);cfg['return_aggregation']='mean'
        r=rolling_tick(self.p,self.model,cfg,first['next_state'])
        self.assertTrue(r['refresh_due']);self.assertEqual(r['return_aggregation'],'mean')
        self.assertEqual(r['fixed_freeze_deadline'],first['fixed_freeze_deadline'])
    def test_member_change_is_visible_in_rolling_forecast(self):
        first=rolling_tick(self.p,self.model,self.cfg);p,_,_=fixture(NOW+timedelta(minutes=30))
        self.add_hot_ahu(27,NOW+timedelta(minutes=30),p);r=rolling_tick(p,self.model,self.cfg,first['next_state'])
        self.assertEqual(r['group_prediction']['members'],[1,2,4]);self.assertTrue(any('参与组统计的AHU由' in x for x in r['warnings']))
    def test_invalid_single_sensor_is_excluded_if_two_other_references_exist(self):
        self.add_hot_ahu(100);r=self.check()
        self.assertEqual(r['group_prediction']['members'],[1,4]);self.assertIsNotNone(r['tentative_start_at'])


if __name__=='__main__':unittest.main()
