"""开机预测改表的离线集成验证；模拟设备与时钟，禁止真实网络和下发。"""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import hangxin_start_forecast as control
from smart_energy_client import SmartEnergyError


ZONE = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 11, 5, 12, tzinfo=ZONE)
PREFIX = "nfdh9q_mscrk01_"
HOUR, MINUTE = PREFIX + "t01", PREFIX + "t04"
RUNNING, OUTPUT, FAULT = (PREFIX + name for name in ("c00", "c23", "c22"))
SCHEDULE_NAMES = {
    "t01": "开机时间设定1_时", "t04": "开机时间设定1_分",
    "t11": "关机时间设定1_时", "t14": "关机时间设定1_分",
    "t02": "开机时间设定2_时", "t05": "开机时间设定2_分",
    "t12": "关机时间设定2_时", "t15": "关机时间设定2_分",
    "t03": "开机时间设定3_时", "t06": "开机时间设定3_分",
    "t13": "关机时间设定3_时", "t16": "关机时间设定3_分",
}


class FakeClock:
    def __init__(self, current=BASE):
        self.current = current

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("模拟时钟不能倒退")
        self.current += timedelta(seconds=seconds)


def point(pid, device, name, identifier, unit, value, at, writable=False):
    return {"pointId": pid, "deviceId": device, "projectId": 832,
            "pointName": name, "identifierName": identifier, "unit": unit,
            "isWrite": writable, "value": value, "updateTime": at.isoformat()}


class FakePlatform:
    def __init__(self, clock, state_dir):
        self.clock, self.state_dir = clock, state_dir
        self.values = {PREFIX + key: 0 for key in SCHEDULE_NAMES}
        self.values.update({HOUR: 14, MINUTE: 30, PREFIX + "t11": 20,
                            PREFIX + "t14": 55, RUNNING: 0, OUTPUT: 0, FAULT: 0,
                            PREFIX + "c21": 28, PREFIX + "c08": 24.5})
        for n in (1, 2, 4, 5):
            for key, value in {"airin": 26, "v20": 1, "v01": 50, "freshv": 0}.items():
                self.values[f"ewcnad_msahu{n}_{key}"] = value
        self.reads, self.histories, self.writes, self.durable_attempts = [], [], [], []
        self.read_changes = {}
        self.apply = True
        self.timeout_on_write = None
        self.stale_confirmation = False
        self.write_times = {}

    def read(self):
        current = self.clock()
        at = current - timedelta(seconds=1)
        points = {}
        for key, name in SCHEDULE_NAMES.items():
            pid = PREFIX + key
            stamp = at
            if self.stale_confirmation and pid in self.write_times:
                stamp = self.write_times[pid] - timedelta(seconds=1)
            points[pid] = point(pid, 12788, name, key, "", self.values[pid], stamp, True)
        for key, name, unit in (("c00", "主机运行反馈", ""), ("c23", "主机启停输出", ""),
                                ("c22", "主机故障反馈", ""), ("c21", "室外温度", "℃"),
                                ("c08", "冷却水进水温度", "℃")):
            pid = PREFIX + key
            points[pid] = point(pid, 12788, name, key, unit, self.values[pid], at)
        for n in (1, 2, 4, 5):
            for key, name, unit in (("airin", "回风温度", "℃"), ("v20", "变频器运行I", ""),
                                    ("v01", "变频器运行频率", "hz"), ("freshv", "新风阀开度", "%")):
                pid = f"ewcnad_msahu{n}_{key}"
                points[pid] = point(pid, 12782 + n, name, key, unit, self.values[pid], at)
        change = self.read_changes.get(len(self.reads) + 1)
        if change:
            change(points)
        self.reads.append(deepcopy(points))
        return points

    def history(self, at):
        self.histories.append(at)
        ids = [PREFIX + key for key in ("c21", "c08", "c22", "c00", "c23")]
        ids += [f"ewcnad_msahu{n}_{key}" for n in (1, 2, 4, 5)
                for key in ("airin", "v20", "v01", "freshv")]
        return {pid: [{"at": (at - timedelta(minutes=minute)).isoformat(),
                       "value": self.values[pid]} for minute in range(90, 0, -2)]
                for pid in ids}

    def write(self, pid, value):
        if pid not in (HOUR, MINUTE) or type(value) is not int:
            raise AssertionError("执行器不得写第一组开机时分之外的点位")
        at = self.clock()
        slot = at.replace(minute=0, second=0, microsecond=0).isoformat()
        self.durable_attempts.append(deepcopy(control.Store(self.state_dir).get_run(slot)))
        self.writes.append((at, pid, value))
        self.write_times[pid] = at
        if self.apply:
            self.values[pid] = value
        if self.timeout_on_write == len(self.writes):
            raise SmartEnergyError("模拟写入超时，结果未知")
        return {"http_status": 200, "accepted": True}


def install_synthetic_forecast_model(test_case, directory):
    """执行器测试使用临时模型，预测结果仍由各用例提供的替身返回。"""
    algorithm_dir = directory / "forecast-model"
    algorithm_dir.mkdir()
    config = control.ALGORITHM_DIR / "forecast_config.json"
    (algorithm_dir / config.name).write_bytes(config.read_bytes())
    model = {"warming_windows": [], "response_minutes": {}}
    (algorithm_dir / "model.json").write_text(json.dumps(model), encoding="utf-8")
    patcher = patch.object(control, "ALGORITHM_DIR", algorithm_dir)
    patcher.start()
    test_case.addCleanup(patcher.stop)


class StartForecastExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        install_synthetic_forecast_model(self, root)
        self.state_dir = root / "state"
        self.clock = FakeClock()
        self.platform = FakePlatform(self.clock, self.state_dir)
        self.candidate = BASE.replace(hour=13, minute=45)
        self.predictor = Mock(side_effect=self.predict)
        network = patch("requests.sessions.Session.request",
                        side_effect=AssertionError("离线验证禁止任何网络请求"))
        network.start()
        self.addCleanup(network.stop)

    def predict(self, payload, model, config, state=None):
        at = datetime.fromisoformat(payload["evaluated_at"])
        freeze = ((state or {}).get("fixed_freeze_deadline")
                  or (datetime.fromisoformat(payload["original_schedule_start_at"])
                      - timedelta(minutes=15)).isoformat())
        result = {"status": "tentative", "tentative_start_at": self.candidate.isoformat(),
                  "fixed_freeze_deadline": freeze, "schedule_change_window_open": True,
                  "group_prediction": {"members": [1, 2, 4, 5], "member_count": 4},
                  "device_writes": [], "applied": False, "compressor_start_permitted": False,
                  "warnings": [], "refresh_due": True}
        next_state = {"day": at.date().isoformat(),
                      "last_slot": at.replace(minute=0, second=0, microsecond=0).isoformat(),
                      "fixed_freeze_deadline": freeze, "last_forecast": deepcopy(result)}
        return dict(result, next_state=next_state)

    def execute(self, gate=lambda: True):
        return control.execute(self.platform, self.state_dir, clock=self.clock,
                               sleep=self.clock.sleep, gate=gate, predictor=self.predictor)

    def test_confirmed_change_writes_only_two_absolute_fields_and_is_durable(self):
        record = self.execute()
        self.assertEqual(record["status"], "confirmed")
        self.assertEqual({pid: value for _, pid, value in self.platform.writes},
                         {HOUR: 13, MINUTE: 45})
        self.assertTrue(all(item is not None for item in self.platform.durable_attempts),
                        "必须先落盘尝试记录，再调用真实写接口")
        self.assertTrue(all(item.get("attempted") for item in self.platform.durable_attempts))

    def test_gate_off_or_unavailable_skips_all_platform_requests(self):
        def failed():
            raise RuntimeError("模拟本地策略接口不可用")
        for index, gate in enumerate((lambda: False, failed,
                                      lambda: {"id": "chiller_start_stop", "effective_enabled": False,
                                               "status": "idle"},
                                      lambda: {"id": "chiller_start_stop", "effective_enabled": True,
                                               "status": "stopping"})):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / f"off-{index}"
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.assertEqual(self.execute(gate)["status"], "skipped_gate")
                self.assertEqual(self.platform.reads, [])
                self.assertEqual(self.platform.histories, [])
                self.assertEqual(self.platform.writes, [])
        self.predictor.assert_not_called()

    def test_missing_stale_future_and_invalid_chiller_feedback_never_predicts(self):
        mutations = [lambda p: p.pop(RUNNING),
                     lambda p: p[RUNNING].update(updateTime=(BASE-timedelta(seconds=181)).isoformat()),
                     lambda p: p[RUNNING].update(updateTime=(BASE+timedelta(seconds=1)).isoformat())]
        mutations.extend(lambda p, value=value: p[RUNNING].update(value=value)
                         for value in (None, True, False, "0", 2, -1, float("nan")))
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / f"bad-feedback-{index}"
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.read_changes[1] = mutate
                self.execute()
                self.assertEqual(self.platform.histories, [])
                self.assertEqual(self.platform.writes, [])
        self.predictor.assert_not_called()

    def test_running_starting_and_fault_hold_do_not_predict(self):
        for pid in (RUNNING, OUTPUT, FAULT):
            with self.subTest(pid=pid):
                self.state_dir = Path(self.temporary.name) / pid
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.values[pid] = 1
                self.execute()
                self.assertEqual(self.platform.histories, [])
                self.assertEqual(self.platform.writes, [])
        self.predictor.assert_not_called()

    def test_at_least_two_fresh_running_reference_ahus_are_required(self):
        for n in (2, 4, 5):
            self.platform.values[f"ewcnad_msahu{n}_v20"] = 0
        self.execute()
        self.assertEqual(self.platform.writes, [])
        self.predictor.assert_not_called()

    def test_same_slot_and_restart_do_not_repeat_control(self):
        self.assertEqual(self.execute()["status"], "confirmed")
        count = len(self.platform.writes)
        self.execute()
        self.execute()
        self.assertEqual(len(self.platform.writes), count)
        self.assertEqual(self.predictor.call_count, 1)

    def test_only_scheduled_slots_and_two_minute_dispatch_window_are_accepted(self):
        for index, at in enumerate((BASE.replace(hour=6), BASE.replace(hour=18),
                                    BASE.replace(minute=2), BASE.replace(minute=30),
                                    BASE.replace(minute=59, second=59))):
            with self.subTest(at=at):
                self.state_dir = Path(self.temporary.name) / f"window-{index}"
                self.clock.current = at
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.assertEqual(self.execute()["status"], "skipped_window")
                self.assertEqual(self.platform.reads, [])
                self.assertEqual(self.platform.writes, [])

    def test_first_eight_and_last_seventeen_slots_are_both_included(self):
        for hour in (8, 16, 17):
            with self.subTest(hour=hour):
                self.state_dir = Path(self.temporary.name) / f"included-{hour}"
                self.clock.current = BASE.replace(hour=hour)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.values[HOUR] = hour + 2
                self.candidate = BASE.replace(hour=hour + 1, minute=45)
                self.assertEqual(self.execute()["status"], "confirmed")
                self.assertEqual(len(self.platform.writes), 2)

    def test_missing_algorithm_candidate_never_changes_device_schedule(self):
        self.predictor = Mock(return_value={"status": "insufficient_data",
                                            "tentative_start_at": None,
                                            "next_state": {"day": BASE.date().isoformat()}})
        self.assertEqual(self.execute()["status"], "insufficient_data")
        self.assertEqual(self.platform.writes, [])

    def test_existing_schedule_equal_to_prediction_does_not_write(self):
        self.candidate = BASE.replace(hour=14, minute=30)
        self.assertEqual(self.execute()["status"], "no_change")
        self.assertEqual(self.platform.writes, [])

    def test_manual_dispatch_uses_real_non_hour_time_and_keeps_freeze_checks(self):
        self.clock.current=BASE.replace(minute=7,second=13)
        record=control.execute(self.platform,self.state_dir,clock=self.clock,sleep=self.clock.sleep,
            gate=lambda:True,predictor=self.predictor,manual=True)
        self.assertEqual(record['status'],'confirmed');self.assertTrue(record['manual'])
        self.assertEqual(record['slot'],BASE.replace(minute=7,second=13).isoformat())
        self.assertTrue(all(entry['attempted_at']>=record['slot'] for entry in record['writes']))

    def test_manual_dispatch_does_not_bypass_twenty_minute_budget_or_pending_requests(self):
        self.clock.current=BASE.replace(minute=7)
        self.platform.values.update({HOUR:12,MINUTE:25})
        record=control.execute(self.platform,self.state_dir,clock=self.clock,sleep=self.clock.sleep,
            gate=lambda:True,predictor=self.predictor,manual=True)
        self.assertEqual(record['status'],'frozen');self.assertEqual(self.platform.writes,[])

    def test_candidate_same_day_before_shutdown_and_after_freeze_budget(self):
        candidates = (BASE.replace(day=6, hour=13), BASE.replace(hour=20, minute=55),
                      BASE.replace(hour=21), BASE.replace(hour=12, minute=15),
                      BASE.replace(hour=12, minute=20))
        for index, candidate in enumerate(candidates):
            with self.subTest(candidate=candidate):
                self.state_dir = Path(self.temporary.name) / f"candidate-{index}"
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.candidate = candidate
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_original_schedule_freeze_also_reserves_five_minutes(self):
        for index, minute in enumerate((15, 20)):
            with self.subTest(minute=minute):
                self.state_dir = Path(self.temporary.name) / f"freeze-{index}"
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.values.update({HOUR: 12, MINUTE: minute})
                self.assertEqual(self.execute()["status"], "frozen")
                self.assertEqual(self.platform.writes, [])

    def test_schedule_twenty_one_minutes_away_is_allowed_but_twenty_is_frozen(self):
        """验证真实改表入口的15+5边界，不只验证计算公式。"""
        self.platform.values.update({HOUR:12,MINUTE:21})
        self.assertEqual(self.execute()['status'],'confirmed')
        self.assertTrue(self.platform.writes)
        self.root_reset = Path(self.temporary.name)/'exact-twenty'
        self.state_dir=self.root_reset;self.clock.current=BASE
        self.platform=FakePlatform(self.clock,self.state_dir)
        self.platform.values.update({HOUR:12,MINUTE:20})
        self.assertEqual(self.execute()['status'],'frozen');self.assertEqual(self.platform.writes,[])

    def test_legacy_freeze_state_migrates_from_original_without_moving_baseline(self):
        store=control.Store(self.state_dir)
        store.save({'slot':BASE.replace(hour=11).isoformat(),'status':'no_change','attempted':False},
            {'day':BASE.date().isoformat(),'original_start_at':BASE.replace(hour=14,minute=30).isoformat(),
             'expected_start_at':BASE.replace(hour=14,minute=30).isoformat(),
             'fixed_freeze_deadline':BASE.replace(hour=14).isoformat(),'forecast_state':None})
        self.assertEqual(self.execute()['status'],'confirmed')
        state=store.read_state()
        self.assertEqual(state['original_start_at'],BASE.replace(hour=14,minute=30).isoformat())
        self.assertEqual(state['fixed_freeze_deadline'],BASE.replace(hour=14,minute=15).isoformat())
        self.assertEqual(state['freeze_minutes'],15)

    def test_freeze_deadline_does_not_move_after_later_candidate(self):
        self.candidate = BASE.replace(hour=15, minute=0)
        self.assertEqual(self.execute()["status"], "confirmed")
        state = control.Store(self.state_dir).read_state()
        expected = state["fixed_freeze_deadline"]
        self.assertEqual(expected, BASE.replace(hour=14,minute=15).isoformat())
        self.clock.current = BASE.replace(hour=13)
        self.candidate = BASE.replace(hour=16)
        self.execute()
        self.assertEqual(control.Store(self.state_dir).read_state()["fixed_freeze_deadline"], expected)

    def test_manual_schedule_change_continues_prediction_and_automatic_adjustment(self):
        """外部新表成为新基准；每轮都计算，后续自动改表仍受冻结保护。"""
        self.clock.current = BASE.replace(hour=8)
        self.assertEqual(self.execute()["status"], "confirmed")
        count = len(self.platform.writes)
        self.clock.current = BASE.replace(hour=9)
        self.platform.values.update({HOUR: 14, MINUTE: 0})
        result=self.execute()
        self.assertEqual(result['status'],'confirmed');self.assertTrue(result['prediction_completed'])
        self.assertIn('external_schedule_rebased',result)
        state=control.Store(self.state_dir).read_state()
        self.assertFalse(state.get('external_hold'));self.assertEqual(state['original_start_at'],BASE.replace(hour=14).isoformat())
        self.clock.current = BASE.replace(hour=10)
        self.platform.values.update({HOUR: 13, MINUTE: 45})
        self.candidate = BASE.replace(hour=14, minute=0)
        self.assertEqual(self.execute()['status'],'confirmed')
        self.assertEqual(self.predictor.call_count,3);self.assertGreater(len(self.platform.writes),count)
        self.assertEqual(control.Store(self.state_dir).read_state()['original_start_at'],BASE.replace(hour=14).isoformat(),
                         '算法自己后移不能继续重置原计划基准')

    def test_next_day_rebuilds_baseline_after_external_schedule_adjustment(self):
        """每日按现场表重建，人工表不会阻止当天及次日的预测。"""
        self.clock.current = BASE.replace(hour=8)
        self.assertEqual(self.execute()["status"], "confirmed")
        self.clock.current = BASE.replace(hour=9)
        self.platform.values.update({HOUR: 14, MINUTE: 0})
        self.assertEqual(self.execute()["status"], "confirmed")
        self.clock.current = BASE.replace(day=6, hour=8)
        self.candidate = BASE.replace(day=6, hour=15, minute=0)
        self.assertEqual(self.execute()["status"], "confirmed")
        self.assertFalse(control.Store(self.state_dir).read_state().get("external_hold", False))

    def test_legacy_external_hold_still_calculates_at_fifteen_and_can_adjust(self):
        self.clock.current=BASE.replace(hour=15)
        self.platform.values.update({HOUR:16,MINUTE:45})
        original=BASE.replace(hour=11,minute=45).isoformat()
        control.Store(self.state_dir).save({'slot':BASE.replace(hour=14).isoformat(),'status':'external_change','attempted':False},
            {'day':BASE.date().isoformat(),'original_start_at':original,
             'expected_start_at':BASE.replace(hour=14,minute=15).isoformat(),'external_hold':True,
             'fixed_freeze_deadline':BASE.replace(hour=11,minute=15).isoformat(),'forecast_state':None})
        self.candidate=BASE.replace(hour=17,minute=45)
        result=self.execute()
        self.assertEqual(result['status'],'confirmed');self.assertTrue(result['prediction_completed'])
        self.assertEqual(self.predictor.call_count,1);self.assertTrue(self.platform.writes)
        self.assertEqual(result['external_schedule_rebased']['original_start_at'],original)
        state=control.Store(self.state_dir).read_state()
        self.assertEqual(state['original_start_at'],BASE.replace(hour=16,minute=45).isoformat())
        self.assertFalse(state.get('external_hold'))

    def test_external_schedule_inside_twenty_minutes_is_predicted_but_not_written(self):
        control.Store(self.state_dir).save({'slot':BASE.replace(hour=11).isoformat(),'status':'external_change','attempted':False},
            {'day':BASE.date().isoformat(),'original_start_at':BASE.replace(hour=14,minute=30).isoformat(),
             'expected_start_at':BASE.replace(hour=14,minute=30).isoformat(),'external_hold':True,'forecast_state':None})
        self.platform.values.update({HOUR:12,MINUTE:20})
        result=self.execute()
        self.assertEqual(result['status'],'frozen');self.assertTrue(result['prediction_completed'])
        self.predictor.assert_called_once();self.assertEqual(self.platform.writes,[])

    def test_manual_change_during_history_still_predicts_and_next_slot_can_control(self):
        history=self.platform.history
        def changed(at):
            value=history(at);self.platform.values.update({HOUR:15,MINUTE:0});return value
        self.platform.history=changed
        result=self.execute()
        self.assertEqual(result['status'],'external_change');self.assertTrue(result['prediction_completed'])
        self.assertEqual(self.platform.writes,[])
        self.platform.history=history;self.clock.current=BASE.replace(hour=13)
        self.assertEqual(self.execute()['status'],'confirmed');self.assertTrue(self.platform.writes)

    def test_unconfirmed_previous_request_blocks_control_but_not_new_prediction(self):
        self.platform.apply=False;self.platform.timeout_on_write=1
        self.assertEqual(self.execute()['status'],'unconfirmed')
        self.clock.current=BASE.replace(hour=13)
        result=self.execute()
        self.assertEqual(result['status'],'pending_hold');self.assertTrue(result['prediction_completed'])
        self.assertEqual(self.predictor.call_count,2);self.assertEqual(len(self.platform.writes),1)
        self.assertTrue(result['control_blockers'])

    def test_recheck_running_ahu_and_external_schedule_before_writing(self):
        mutations = (
            lambda p: p[RUNNING].update(value=1),
            lambda p: p[FAULT].update(value=1),
            lambda p: p[MINUTE].update(value=20),
            lambda p: p[PREFIX+"t11"].update(value=19),
            lambda p: [p[f"ewcnad_msahu{n}_v20"].update(value=0) for n in (2, 4, 5)],
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / f"recheck-{index}"
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.read_changes[2] = mutation
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_gate_rechecked_immediately_before_write(self):
        answers = iter((True, False))
        self.execute(lambda: next(answers))
        self.assertEqual(self.platform.writes, [])

    def test_slow_gate_cannot_extend_first_dispatch_window(self):
        calls = 0

        def gate():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.clock.sleep(121)
            return True

        self.execute(gate)
        self.assertEqual(self.platform.writes, [], "门控返回后必须重新判断真实下发时刻")

    def test_slow_gate_cannot_extend_remaining_field_confirmation_budget(self):
        calls = 0

        def gate():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.clock.sleep(301)
            return True

        self.assertEqual(self.execute(gate)["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 1)

    def test_slow_durable_save_cannot_extend_dispatch_window(self):
        original_save = control.Store.save
        advanced = False

        def slow_save(store, record, state=None):
            nonlocal advanced
            result = original_save(store, record, state)
            if record.get("attempted") and not advanced:
                advanced = True
                self.clock.sleep(121)
            return result

        with patch.object(control.Store, "save", slow_save):
            self.execute()
        self.assertEqual(self.platform.writes, [], "落盘延迟也不能授权过期的设备下发")

    def test_unknown_first_write_does_not_send_other_field_or_retry(self):
        self.platform.timeout_on_write = 1
        self.platform.apply = False
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 1)
        self.execute()
        self.clock.current = BASE.replace(hour=13)
        self.execute()
        self.assertEqual(len(self.platform.writes), 1,
                         "未确认或部分改表后不得在下一小时重新生成计划")

    def test_unknown_second_write_leaves_partial_plan_blocked_next_hour(self):
        self.platform.timeout_on_write = 2
        original_write = self.platform.write

        def second_not_applied(pid, value):
            if len(self.platform.writes) == 1:
                self.platform.apply = False
            return original_write(pid, value)

        self.platform.write = second_not_applied
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 2)
        self.clock.current = BASE.replace(hour=13)
        self.execute()
        self.assertEqual(len(self.platform.writes), 2)

    def test_dispatch_deadline_is_rechecked_after_slow_history_query(self):
        original_history = self.platform.history

        def slow_history(at):
            result = original_history(at)
            self.clock.sleep(121)
            return result

        self.platform.history = slow_history
        self.execute()
        self.assertEqual(self.platform.writes, [])

    def test_stale_ahu_measurements_cannot_authorize_with_only_one_current_member(self):
        def stale(points):
            for n in (2, 4, 5):
                points[f"ewcnad_msahu{n}_airin"].update(
                    updateTime=(BASE-timedelta(seconds=181)).isoformat())

        self.platform.read_changes[1] = stale
        self.execute()
        self.assertEqual(self.platform.writes, [])
        self.predictor.assert_not_called()

    def test_value_without_new_device_timestamp_does_not_confirm(self):
        self.platform.stale_confirmation = True
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 1)

    def test_late_readback_response_cannot_confirm_after_five_minute_budget(self):
        original_read = self.platform.read

        def slow_readback():
            if len(self.platform.reads) == 3:
                self.clock.sleep(301)
            return original_read()

        self.platform.read = slow_readback
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 1)

    def test_only_changed_field_is_written(self):
        self.platform.values[MINUTE] = 45
        self.assertEqual(self.execute()["status"], "confirmed")
        self.assertEqual([(pid, value) for _, pid, value in self.platform.writes], [(HOUR, 13)])

    def test_other_schedule_fields_remain_unchanged(self):
        before = deepcopy(self.platform.values)
        self.execute()
        for pid, value in before.items():
            if pid.startswith(PREFIX+"t") and pid not in (HOUR, MINUTE):
                self.assertEqual(self.platform.values[pid], value)


if __name__ == "__main__":
    unittest.main()
