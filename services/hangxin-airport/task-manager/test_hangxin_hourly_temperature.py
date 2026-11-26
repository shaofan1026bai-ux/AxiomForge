"""AHU最高回风温调离线测试；仅使用临时数据库与受控时钟。"""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import hangxin_hourly_temperature as control

ZONE = ZoneInfo('Asia/Shanghai')
BASE = datetime(2026, 11, 8, 11, tzinfo=ZONE)
SP = 'nfdh9q_mscrk01_c17'
OUTDOOR = 'nfdh9q_mscrk01_c21'
RUNNING = 'nfdh9q_mscrk01_c00'
PROTECTED = 'nfdh9q_mscrk01_t01'
AHUS = {1: 12783, 2: 12784, 4: 12786, 5: 12787}


def slot(at):
    return at.replace(minute=0 if at.minute < 30 else 30, second=0, microsecond=0).isoformat()


class FakeClock:
    def __init__(self, current=BASE):
        self.current = current

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError('测试时钟不得倒退')
        self.current += timedelta(seconds=seconds)


def point(pid, device, tag, name, unit, value, at, writable=False):
    return {'pointId': pid, 'deviceId': device, 'projectId': 832,
            'identifierName': tag, 'pointName': name, 'unit': unit,
            'isWrite': writable, 'value': value, 'updateTime': at.isoformat()}


class FakePlatform:
    """模拟更新和响应未知；每次POST检查尝试记录已经持久化。"""
    def __init__(self, clock, state_dir, *, setpoint=9, returns=None, slopes=None,
                 outdoor=24, running=1, apply=True, timeout=False):
        self.clock, self.state_dir = clock, Path(state_dir)
        self.setpoint, self.outdoor, self.running = setpoint, outdoor, running
        self.returns = {n: 25.0 for n in AHUS}; self.returns.update(returns or {})
        self.slopes = {n: 0.0 for n in AHUS}; self.slopes.update(slopes or {})
        self.ahu_running = {n: 1 for n in AHUS}
        self.frequencies = {n: 35 for n in AHUS}
        self.protected_hour = 11
        self.apply, self.timeout = apply, timeout
        self.pending = None
        self.read_count, self.prepare_count, self.prepare_delay = 0, 0, 0
        self.writes, self.durable_attempts, self.history_calls = [], [], []
        self.mutate_read, self.mutate_histories = None, None
        self.raise_history_error = False
        self.outdoor_change_30m = 0.0
        self.setpoint_age, self.running_age = 0, 0
        self.readback_same_stamp = False

    def read(self):
        self.read_count += 1
        at = self.clock()
        if self.pending and at >= self.pending[0]:
            self.setpoint = self.pending[1]; self.pending = None
        sp_stamp = at - timedelta(seconds=self.setpoint_age)
        if self.writes and self.readback_same_stamp:
            sp_stamp = self.writes[-1][0]
        points = {
            SP: point(SP, 12788, 'c17', '冷机出水温度本地设定值', '℃', self.setpoint, sp_stamp, True),
            OUTDOOR: point(OUTDOOR, 12788, 'c21', '室外温度', '℃', self.outdoor, at),
            RUNNING: point(RUNNING, 12788, 'c00', '主机运行反馈', '', self.running,
                           at - timedelta(seconds=self.running_age)),
            PROTECTED: point(PROTECTED, 12788, 't01', '开机时间设定1_时', '', self.protected_hour, at, True),
        }
        for n, device in AHUS.items():
            for tag, name, unit, value in (
                ('airin', '回风温度', '℃', self.returns[n]),
                ('v20', '变频器运行I', '', self.ahu_running[n]),
                ('v01', '变频器运行频率', 'hz', self.frequencies[n]),
            ):
                pid = f'ewcnad_msahu{n}_{tag}'
                points[pid] = point(pid, device, tag, name, unit, value, at)
        if self.mutate_read:
            self.mutate_read(self, self.read_count, points)
        return points

    def history_feedback(self, at):
        self.history_calls.append(at)
        if self.raise_history_error:
            raise control.ControlError('模拟历史缺失')
        histories = {RUNNING: [], OUTDOOR: []}
        for n in AHUS:
            histories.update({f'ewcnad_msahu{n}_{tag}': [] for tag in ('airin', 'v20', 'v01')})
        for age in range(30, -1, -2):
            stamp = at - timedelta(minutes=age)
            histories[RUNNING].append({'at': stamp.isoformat(), 'value': self.running})
            histories[OUTDOOR].append({'at': stamp.isoformat(),
                                      'value': None if self.outdoor is None else self.outdoor-self.outdoor_change_30m*age/30})
            for n in AHUS:
                histories[f'ewcnad_msahu{n}_airin'].append({
                    'at': stamp.isoformat(), 'value': self.returns[n] - self.slopes[n] * age / 60})
                for tag, value in [('v20', self.ahu_running[n]), ('v01', self.frequencies[n])]:
                    histories[f'ewcnad_msahu{n}_{tag}'].append({'at': stamp.isoformat(), 'value': value})
        if self.mutate_histories:
            self.mutate_histories(histories)
        return histories

    def prepare_write(self):
        self.prepare_count += 1
        self.clock.sleep(self.prepare_delay)

    def write(self, pid, target):
        at = self.clock()
        record = control.Store(self.state_dir).get_run(slot(at))
        if not record or not record.get('attempted') or record.get('target') != target:
            raise AssertionError('POST前必须已有完整持久化尝试记录')
        self.durable_attempts.append(deepcopy(record))
        self.writes.append((at, pid, target))
        if self.apply:
            self.pending = (at + timedelta(seconds=1), target)
        if self.timeout:
            raise control.SmartEnergyError('模拟响应未知')
        return {'http_status': 200, 'accepted': True}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / 'state'
        self.clock = FakeClock()
        for target in ('requests.sessions.Session.request', 'socket.create_connection'):
            blocker = patch(target, side_effect=AssertionError('离线测试禁止真实网络'))
            blocker.start(); self.addCleanup(blocker.stop)

    def platform(self, **kwargs):
        return FakePlatform(self.clock, self.state_dir, **kwargs)

    def execute(self, platform, gate=lambda: True):
        return control.execute(platform, self.state_dir, clock=self.clock,
                               sleep=self.clock.sleep, gate=gate)

    def feedback(self, platform, histories=None):
        points = platform.read()
        return control.assess_feedback(points, platform.history_feedback(self.clock()) if histories is None else histories,
                                       self.clock())


class ExecutionTests(OfflineCase):
    def test_raise_is_one_durable_absolute_c17_write_and_new_readback(self):
        platform = self.platform(); result = self.execute(platform)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([write[1:] for write in platform.writes], [(SP, 9.5)])
        self.assertEqual(platform.prepare_count, 1)
        self.assertEqual(platform.durable_attempts[0]['target'], 9.5)
        self.assertTrue(result['other_schedules_unchanged'])
        state = control.Store(self.state_dir).read_state()
        self.assertEqual(state['model'], 'ahu_max_return_v1')
        self.assertEqual(state['last_setpoint'], 9.5)

    def test_current_hot_return_lowers_even_when_history_lookup_fails(self):
        platform = self.platform(returns={1: 26.4}); platform.raise_history_error = True
        self.assertEqual(self.execute(platform)['status'], 'completed')
        self.assertEqual(platform.writes[0][1:], (SP, 8.5))

    def test_outdoor_change_alone_cannot_reverse_room_feedback(self):
        for index, outdoor in enumerate((10, 40, None)):
            with self.subTest(outdoor=outdoor):
                self.state_dir = Path(self.tmp.name)/f'outdoor-{index}'
                platform = self.platform(returns={1: 26.2}, outdoor=outdoor)
                self.assertEqual(self.execute(platform)['status'], 'completed')
                self.assertEqual(platform.writes[0][1:], (SP, 8.5))

    def test_stopped_chiller_skips_history_and_does_not_change_setpoint(self):
        for index, current in enumerate((9, 7, 12)):
            self.state_dir = Path(self.tmp.name)/f'stopped-{index}'
            platform = self.platform(setpoint=current, running=0)
            self.assertEqual(self.execute(platform)['status'], 'skipped_not_running')
            self.assertEqual(platform.history_calls, [])
            self.assertEqual(platform.writes, [])

    def test_invalid_or_stale_running_feedback_cannot_authorize_write(self):
        for index, value in enumerate((None, True, 2, '1')):
            self.state_dir = Path(self.tmp.name)/f'run-{index}'
            platform = self.platform(running=value)
            self.assertFalse(self.execute(platform).get('attempted', False))
            self.assertEqual(platform.writes, [])
        self.state_dir = Path(self.tmp.name)/'stale-run'
        platform = self.platform(); platform.running_age = 301
        self.assertFalse(self.execute(platform).get('attempted', False))
        self.assertEqual(platform.writes, [])

    def test_exact_half_hour_slots_are_separate_and_idempotent(self):
        platform = self.platform()
        self.assertEqual(self.execute(platform)['status'], 'completed')
        self.execute(platform); self.assertEqual(len(platform.writes), 1)
        self.clock.current = BASE.replace(minute=30)
        self.assertEqual(self.execute(platform)['status'], 'completed')
        self.assertEqual([write[2] for write in platform.writes], [9.5, 10.0])
        self.assertIsNotNone(control.Store(self.state_dir).get_run(BASE.replace(minute=30).isoformat()))

    def test_only_1100_through_2100_and_first_two_minutes_of_each_slot(self):
        invalid = (BASE.replace(hour=10, minute=30), BASE.replace(hour=21, minute=30),
                   BASE.replace(hour=22), BASE.replace(minute=2), BASE.replace(minute=29), BASE.replace(minute=32))
        for index, at in enumerate(invalid):
            self.clock.current = at; self.state_dir = Path(self.tmp.name)/f'window-{index}'
            platform = self.platform()
            self.assertEqual(self.execute(platform)['status'], 'skipped_window')
            self.assertEqual(platform.read_count, 0)
        for index, at in enumerate((BASE.replace(minute=30), BASE.replace(hour=21))):
            self.clock.current = at; self.state_dir = Path(self.tmp.name)/f'allowed-{index}'
            self.assertEqual(self.execute(self.platform())['status'], 'completed')

    def test_gate_closed_or_unknown_never_requests_platform(self):
        for index, gate in enumerate((lambda: False, lambda: {'effective_enabled': True, 'status': 'stopping'},
                                     lambda: {'effective_enabled': 'true', 'status': 'idle'})):
            self.state_dir = Path(self.tmp.name)/f'gate-{index}'; platform = self.platform()
            self.assertEqual(self.execute(platform, gate)['status'], 'skipped_gate')
            self.assertEqual(platform.read_count, 0)

    def test_gate_closes_before_post_cancels_write(self):
        platform = self.platform(); calls = [0]
        def gate():
            calls[0] += 1
            return calls[0] == 1
        self.execute(platform, gate); self.assertEqual(platform.writes, [])

    def test_authentication_finishing_outside_slot_cannot_send_late(self):
        platform = self.platform(); platform.prepare_delay = 120
        self.execute(platform); self.assertEqual(platform.writes, [])

    def test_gate_delay_outside_slot_cannot_send_late(self):
        platform = self.platform(); calls = [0]
        def gate():
            calls[0] += 1
            if calls[0] >= 2: self.clock.sleep(120)
            return True
        self.execute(platform, gate); self.assertEqual(platform.writes, [])

    def test_attempt_fsync_delay_outside_slot_cannot_send_late(self):
        platform = self.platform(); original = control.Store.save
        def slow_save(store, record, state=None):
            result = original(store, record, state)
            if record.get('attempted') and not platform.writes: self.clock.sleep(120)
            return result
        with patch.object(control.Store, 'save', slow_save): self.execute(platform)
        self.assertEqual(platform.writes, [])

    def test_prewrite_stop_or_external_schedule_change_cancels_write(self):
        for name, mutate in (
            ('stop', lambda points: points[RUNNING].update(value=0)),
            ('schedule', lambda points: points[PROTECTED].update(value=12)),
            ('manual', lambda points: points[SP].update(value=10)),
        ):
            with self.subTest(name=name):
                self.state_dir = Path(self.tmp.name)/name; platform = self.platform()
                platform.mutate_read = lambda p, i, points, mutate=mutate: mutate(points) if i >= 2 else None
                self.execute(platform); self.assertEqual(platform.writes, [])

    def test_fresh_hot_return_before_post_must_not_keep_old_raise_target(self):
        platform = self.platform()
        def hot_update(p, index, points):
            if index >= 2: points['ewcnad_msahu1_airin']['value'] = 26.3
        platform.mutate_read = hot_update
        self.execute(platform)
        self.assertTrue(all(target <= 9 for _, _, target in platform.writes))

    def test_missing_history_holds_cool_setpoint(self):
        platform = self.platform(); platform.raise_history_error = True
        self.execute(platform); self.assertEqual(platform.writes, [])

    def test_same_slot_unknown_applied_write_is_only_confirmed_not_retried(self):
        platform = self.platform(timeout=True)
        self.assertEqual(self.execute(platform)['status'], 'completed')
        self.execute(platform); self.assertEqual(len(platform.writes), 1)

    def test_unknown_unapplied_request_is_not_resent_next_slot(self):
        platform = self.platform(timeout=True, apply=False)
        self.assertEqual(self.execute(platform)['status'], 'unconfirmed')
        self.clock.current = BASE.replace(minute=30)
        self.assertEqual(self.execute(platform)['status'], 'pending_hold')
        self.assertEqual(len(platform.writes), 1)

    def test_readback_equal_to_post_stamp_does_not_confirm(self):
        platform = self.platform(); platform.readback_same_stamp = True
        self.assertEqual(self.execute(platform)['status'], 'unconfirmed')
        self.assertIsNone(control.Store(self.state_dir).read_state())

    def test_manual_change_rebases_without_overwriting_same_round(self):
        platform = self.platform(); self.execute(platform)
        platform.setpoint = 10.5; self.clock.current = BASE.replace(minute=30)
        self.assertEqual(self.execute(platform)['status'], 'manual_rebase')
        self.assertEqual(len(platform.writes), 1)
        self.assertEqual(control.Store(self.state_dir).read_state()['last_setpoint'], 10.5)

    def test_legacy_weather_residual_is_not_consumed_by_new_control(self):
        store = control.Store(self.state_dir)
        legacy = {'date': BASE.date().isoformat(), 'last_outdoor': 35, 'residual': .9,
                  'last_setpoint': 9, 'last_slot': BASE.replace(hour=10).isoformat()}
        store.save({'slot':BASE.replace(hour=10).isoformat(), 'status':'no_change', 'attempted':False}, legacy)
        platform = self.platform(outdoor=15)
        self.assertEqual(self.execute(platform)['status'], 'completed')
        self.assertEqual(platform.writes[0][2], 9.5)
        self.assertEqual(store.read_state()['model'], 'ahu_max_return_v1')

    def test_legacy_pending_target_can_be_confirmed_before_new_round(self):
        store = control.Store(self.state_dir)
        previous = BASE-timedelta(minutes=30)
        legacy_state = {'date': BASE.date().isoformat(), 'last_outdoor': 25,
                        'residual': .8, 'last_setpoint': 8.5, 'last_slot': previous.isoformat()}
        store.save({'slot': previous.isoformat(), 'status': 'unconfirmed', 'attempted': True,
                    'attempted_at': previous.isoformat(), 'target': 8.5,
                    'protected_schedules': {PROTECTED: 11}, 'candidate_state': legacy_state})
        platform = self.platform(setpoint=8.5)
        self.assertEqual(self.execute(platform)['status'], 'completed')
        self.assertEqual(store.get_run(previous.isoformat())['status'], 'completed')
        self.assertEqual([write[2] for write in platform.writes], [9.0])
        self.assertEqual(store.read_state()['model'], 'ahu_max_return_v1')

    def test_unresolved_legacy_target_holds_without_new_feedback_query(self):
        store = control.Store(self.state_dir)
        previous = BASE-timedelta(minutes=30)
        store.save({'slot': previous.isoformat(), 'status': 'unconfirmed', 'attempted': True,
                    'attempted_at': previous.isoformat(), 'target': 8.5,
                    'protected_schedules': {PROTECTED: 11}, 'candidate_state': {
                        'date': BASE.date().isoformat(), 'last_setpoint': 8.5, 'residual': .8}})
        platform = self.platform(returns={1: 26.3})
        self.assertEqual(self.execute(platform)['status'], 'pending_hold')
        self.assertEqual(platform.history_calls, [])
        self.assertEqual(platform.writes, [])

    def test_signal_ages_past_limit_after_attempt_fsync_cannot_raise(self):
        platform = self.platform()
        def almost_stale(p, i, points):
            for pid, row in points.items():
                if pid.startswith('ewcnad_msahu'):
                    row['updateTime'] = (BASE-timedelta(seconds=179)).isoformat()
        platform.mutate_read = almost_stale
        def older_histories(histories):
            for pid, rows in histories.items():
                if pid.startswith('ewcnad_msahu'):
                    for row in rows:
                        row['at'] = (datetime.fromisoformat(row['at'])-timedelta(seconds=180)).isoformat()
        platform.mutate_histories = older_histories
        original = control.Store.save
        def fsync_delay(store, record, state=None):
            result = original(store, record, state)
            if record.get('attempted') and not platform.writes:
                self.clock.sleep(2)
            return result
        with patch.object(control.Store, 'save', fsync_delay):
            result = self.execute(platform)
        self.assertEqual(result['status'], 'skipped_conditions')
        self.assertFalse(result['attempted'])
        self.assertEqual(platform.writes, [])

    def test_out_of_bounds_manual_setpoint_is_not_large_clamped(self):
        for index, current in enumerate((7, 12)):
            self.state_dir = Path(self.tmp.name)/f'range-{index}'; platform = self.platform(setpoint=current)
            self.assertEqual(self.execute(platform)['status'], 'skipped_setpoint_out_of_bounds')
            self.assertEqual(platform.writes, [])

    def test_platform_write_scope_is_only_c17_absolute_target(self):
        platform = object.__new__(control.Platform); platform.client = Mock()
        for pid, target in ((OUTDOOR, 9), (PROTECTED, 11), (RUNNING, 0), (SP, 7.4), (SP, 11.1), (SP, True)):
            with self.subTest(pid=pid, target=target):
                with self.assertRaises(control.ControlError): platform.write(pid, target)
        self.assertEqual(platform.client.mock_calls, [])


class PlatformScopeTests(OfflineCase):
    def adapter(self):
        adapter = object.__new__(control.Platform)
        adapter._read_request = Mock()
        return adapter

    def test_latest_read_requests_only_chiller_and_four_first_floor_devices(self):
        adapter = self.adapter()
        rows = list(self.platform().read().values())
        adapter._read_request.return_value = rows
        result = adapter.read()
        self.assertEqual(set(result), {row['pointId'] for row in rows})
        body = adapter._read_request.call_args.args[1]
        self.assertEqual(set(body['deviceIdList']), {12788, *AHUS.values()})
        self.assertNotIn(12785, body['deviceIdList'])
        self.assertTrue(body['isActivate'])

    def test_duplicate_or_unrequested_second_floor_device_is_rejected(self):
        for unexpected in ('duplicate', 'second_floor'):
            with self.subTest(unexpected=unexpected):
                adapter = self.adapter()
                rows = list(self.platform().read().values())
                if unexpected == 'duplicate':
                    rows.append(deepcopy(rows[-1]))
                else:
                    rows.append(point('ewcnad_msahu3_airin', 12785, 'airin', '回风温度', '℃', 38, BASE))
                adapter._read_request.return_value = rows
                with self.assertRaises(control.ControlError): adapter.read()

    def test_history_requests_fourteen_feedback_signals_without_second_floor(self):
        from ahu_return_feedback import SPECS, HISTORY_IDS
        adapter = self.adapter()
        fake = self.platform()
        histories = fake.history_feedback(BASE)
        rows = []
        for pid in HISTORY_IDS:
            device, tag, name, unit = SPECS[pid]
            rows.append({'pointId': pid, 'deviceId': device, 'identifierName': tag, 'unit': unit,
                         'pointDataList': [{'ts': row['at'], 'val': row['value']} for row in histories[pid]
                                           if datetime.fromisoformat(row['at']) <= BASE-timedelta(minutes=2)]})
        adapter._read_request.return_value = rows
        result = adapter.history_feedback(BASE)
        self.assertEqual(set(result), set(HISTORY_IDS))
        self.assertEqual(len(result), 14)
        body = adapter._read_request.call_args.args[1]
        self.assertEqual(set(body['pointIdList']), set(HISTORY_IDS))
        self.assertFalse(any('msahu3_' in pid for pid in body['pointIdList']))
        self.assertEqual(body['interval'], 2)
        self.assertEqual(body['timeQuantum']['endDate'], '2026-11-08 11:01')

    def test_expired_write_authentication_does_not_login_or_post(self):
        adapter = object.__new__(control.Platform)
        adapter.client = Mock()
        adapter.client._token = None
        adapter.client._token_expires_at = 0
        with self.assertRaises(control.ControlError): adapter.write(SP, 9.5)
        adapter.client._login.assert_not_called()
        adapter.client._post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
