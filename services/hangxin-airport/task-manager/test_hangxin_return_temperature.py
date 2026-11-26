"""1F最高回风与趋势边界测试；全部输入来自离线Fake。"""

from datetime import datetime, timedelta
import unittest

import hangxin_hourly_temperature as control
from test_hangxin_hourly_temperature import OfflineCase, AHUS, RUNNING, OUTDOOR


class FeedbackTests(OfflineCase):
    def test_supported_scope_excludes_second_floor(self):
        self.assertEqual(control.AHU_IDS, AHUS)
        self.assertNotIn(3, control.AHU_IDS)

    def test_highest_return_is_used_instead_of_comfortable_group_mean(self):
        feedback = self.feedback(self.platform(returns={1: 23, 2: 23, 4: 23, 5: 26.2}))
        self.assertEqual(feedback['max_return_c'], 26.2)
        self.assertEqual(control.calculate(9, feedback)['target'], 8.5)

    def test_cool_stable_running_ahus_allow_one_raise(self):
        feedback = self.feedback(self.platform(returns={1: 25.5}))
        self.assertTrue(feedback['raise_allowed'])
        self.assertEqual(set(feedback['members']), set(AHUS))
        result = control.calculate(9, feedback)
        self.assertEqual(result['target'], 9.5)
        self.assertEqual(result['direction'], 'raise')

    def test_25_9_and_exact_26_stable_do_not_raise(self):
        for value in (25.9, 26.0):
            with self.subTest(value=value):
                result = control.calculate(9, self.feedback(self.platform(returns={1: value})))
                self.assertEqual(result['target'], 9)
                self.assertEqual(result['direction'], 'hold')

    def test_rising_below_buffer_does_not_raise(self):
        feedback = self.feedback(self.platform(returns={1: 25.3}, slopes={1: 0.2}))
        self.assertFalse(feedback['raise_allowed'])
        self.assertEqual(control.calculate(9, feedback)['target'], 9)

    def test_reliable_projection_above_26_lowers_before_current_limit(self):
        feedback = self.feedback(self.platform(returns={1: 25.9}, slopes={1: 0.8}))
        self.assertGreater(feedback['projected_max_return_c'], 26)
        self.assertEqual(control.calculate(9, feedback)['target'], 8.5)

    def test_stopped_ahu_is_excluded_even_if_its_return_is_hot(self):
        platform = self.platform(returns={5: 38})
        platform.ahu_running[5], platform.frequencies[5] = 0, 0
        feedback = self.feedback(platform)
        self.assertNotIn(5, feedback['members'])
        self.assertEqual(feedback['max_return_c'], 25)
        self.assertEqual(control.calculate(9, feedback)['target'], 9.5)

    def test_unknown_running_cannot_be_silently_dropped_for_a_raise(self):
        platform = self.platform(); platform.ahu_running[5] = None
        feedback = self.feedback(platform)
        self.assertFalse(feedback['raise_allowed'])
        self.assertTrue(feedback['raise_blockers'])
        self.assertEqual(control.calculate(9, feedback)['target'], 9)

    def test_missing_or_stale_active_return_blocks_raise(self):
        for mutation in ('missing', 'stale', 'future', 'bad_identity', 'nan'):
            with self.subTest(mutation=mutation):
                platform = self.platform()
                def change(p, index, points, mutation=mutation):
                    pid = 'ewcnad_msahu5_airin'
                    if mutation == 'missing': points.pop(pid)
                    elif mutation == 'stale': points[pid]['updateTime'] = (self.clock()-timedelta(seconds=181)).isoformat()
                    elif mutation == 'future': points[pid]['updateTime'] = (self.clock()+timedelta(seconds=1)).isoformat()
                    elif mutation == 'bad_identity': points[pid]['deviceId'] = 12785
                    else: points[pid]['value'] = float('nan')
                platform.mutate_read = change
                feedback = self.feedback(platform)
                self.assertFalse(feedback['raise_allowed'])
                self.assertEqual(control.calculate(9, feedback)['target'], 9)

    def test_known_hot_ahu_still_lowers_when_another_return_is_missing(self):
        platform = self.platform(returns={1: 26.4})
        platform.mutate_read = lambda p, i, points: points.pop('ewcnad_msahu5_airin')
        self.assertEqual(control.calculate(9, self.feedback(platform))['target'], 8.5)

    def test_missing_history_never_authorizes_raise_but_hot_current_can_lower(self):
        cool = self.feedback(self.platform(), histories={})
        self.assertFalse(cool['raise_allowed'])
        self.assertEqual(control.calculate(9, cool)['target'], 9)
        self.assertEqual(control.calculate(9, self.feedback(self.platform(returns={1: 26.1}), histories={}))['target'], 8.5)

    def test_recent_chiller_or_fan_start_prevents_a_raise(self):
        for target in (RUNNING, 'ewcnad_msahu1_v20', 'ewcnad_msahu1_v01'):
            with self.subTest(target=target):
                platform = self.platform()
                def history_change(histories, target=target):
                    for row in histories[target]:
                        if datetime.fromisoformat(row['at']) < self.clock()-timedelta(minutes=8): row['value'] = 0
                platform.mutate_histories = history_change
                feedback = self.feedback(platform)
                self.assertFalse(feedback['raise_allowed'])
                self.assertEqual(control.calculate(9, feedback)['target'], 9)

    def test_gapped_history_does_not_look_like_a_stable_trend(self):
        platform = self.platform()
        platform.mutate_histories = lambda histories: histories.update({
            'ewcnad_msahu1_airin': [row for row in histories['ewcnad_msahu1_airin']
                                   if datetime.fromisoformat(row['at']) < self.clock()-timedelta(minutes=20)]})
        feedback = self.feedback(platform)
        self.assertFalse(feedback['raise_allowed'])
        self.assertEqual(control.calculate(9, feedback)['target'], 9)

    def test_outdoor_rise_of_one_degree_blocks_raise_without_stacking_steps(self):
        for change, expected in ((.9, 9.5), (1.0, 9), (1.1, 9), (3.0, 9)):
            with self.subTest(change=change):
                platform = self.platform()
                platform.outdoor_change_30m = change
                feedback = self.feedback(platform)
                self.assertEqual(control.calculate(9, feedback)['target'], expected)
                self.assertEqual(feedback['outdoor']['valid'], True)
                self.assertAlmostEqual(feedback['outdoor']['change_30m_c'], change)

    def test_missing_or_stale_weather_blocks_raise_but_does_not_block_hot_lower(self):
        for name in ('history_missing', 'current_stale', 'current_missing'):
            with self.subTest(name=name):
                platform = self.platform()
                if name == 'history_missing':
                    platform.mutate_histories = lambda histories: histories.pop(OUTDOOR)
                elif name == 'current_missing':
                    platform.mutate_read = lambda p, i, points: points.pop(OUTDOOR)
                else:
                    platform.mutate_read = lambda p, i, points: points[OUTDOOR].update(
                        updateTime=(self.clock()-timedelta(seconds=301)).isoformat())
                feedback = self.feedback(platform)
                self.assertFalse(feedback['raise_allowed'])
                self.assertEqual(control.calculate(9, feedback)['target'], 9)
                platform.returns[1] = 26.4
                self.assertEqual(control.calculate(9, self.feedback(platform))['target'], 8.5)

    def test_outdoor_cooling_cannot_override_return_buffer_or_hot_limit(self):
        for current_return, expected in ((25.9, 9), (26.2, 8.5)):
            with self.subTest(current_return=current_return):
                platform = self.platform(returns={1: current_return})
                platform.outdoor_change_30m = -3
                self.assertEqual(control.calculate(9, self.feedback(platform))['target'], expected)

    def test_bounds_never_bank_or_stack_steps(self):
        for current, returns, expected in ((11, {}, 11), (7.5, {1: 26.2}, 7.5),
                                           (10.8, {}, 11), (7.7, {1: 26.2}, 7.5)):
            with self.subTest(current=current):
                result = control.calculate(current, self.feedback(self.platform(returns=returns)))
                self.assertEqual(result['target'], expected)
                self.assertLessEqual(abs(result['target']-current), .500001)
        for current in (7, 12, True, float('nan')):
            with self.subTest(current=current):
                with self.assertRaises(control.ControlError): control.calculate(current, self.feedback(self.platform()))


if __name__ == '__main__':
    unittest.main()
