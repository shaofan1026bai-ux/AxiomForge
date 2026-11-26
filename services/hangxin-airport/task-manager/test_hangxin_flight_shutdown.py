"""航班晚间关机控制的离线集成测试；模拟数据、设备与时钟，不联网。"""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import hangxin_flight_shutdown as control
from smart_energy_client import SmartEnergyError


ZONE = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 11, 5, 18, tzinfo=ZONE)
PREFIX = "nfdh9q_mscrk01_"
HOUR, MINUTE = PREFIX + "t11", PREFIX + "t14"
RUNNING, OUTPUT, FAULT = (PREFIX + key for key in ("c00", "c23", "c22"))
SCHEDULE_NAMES = {
    "t01": "开机时间设定1_时", "t04": "开机时间设定1_分",
    "t11": "关机时间设定1_时", "t14": "关机时间设定1_分",
    "t02": "开机时间设定2_时", "t05": "开机时间设定2_分",
    "t12": "关机时间设定2_时", "t15": "关机时间设定2_分",
    "t03": "开机时间设定3_时", "t06": "开机时间设定3_分",
    "t13": "关机时间设定3_时", "t16": "关机时间设定3_分",
}


class FakeClock:
    """只有显式sleep才推进时间，便于复现反馈与冻结边界竞争。"""

    def __init__(self, current=BASE):
        self.current = current

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("模拟时钟不能倒退")
        self.current += timedelta(seconds=seconds)


def point(pid, tag, name, value, at, writable=False):
    """模拟现场已有的设备、项目、中文点名与上报元数据。"""
    return {"pointId": pid, "deviceId": 12788, "projectId": 832,
            "identifierName": tag, "pointName": name, "unit": "",
            "isWrite": writable, "value": value, "updateTime": at.isoformat()}


class FakePlatform:
    """只实现关机两点的绝对目标，不存在开机、泵或温度写接口。"""

    def __init__(self, clock, state_dir):
        self.clock, self.state_dir = clock, state_dir
        self.values = {PREFIX + tag: 0 for tag in SCHEDULE_NAMES}
        self.values.update({PREFIX + "t01": 11, PREFIX + "t04": 20,
                            HOUR: 21, MINUTE: 55, RUNNING: 1, OUTPUT: 1, FAULT: 0})
        self.reads, self.writes, self.durable_attempts = [], [], []
        self.read_changes = {}
        self.write_times = {}
        self.apply = True
        self.timeout_on_write = None
        self.stale_confirmation = False
        self.prepare_calls = 0
        self.prepare_delay = 0

    def read(self):
        """正常反馈始终新鲜，发送前后仍需新的设备时间戳。"""
        at = self.clock() - timedelta(seconds=1)
        points = {}
        for tag, name in SCHEDULE_NAMES.items():
            pid = PREFIX + tag
            stamp = at
            if self.stale_confirmation and pid in self.write_times:
                stamp = self.write_times[pid] - timedelta(seconds=1)
            points[pid] = point(pid, tag, name, self.values[pid], stamp, True)
        for tag, name in (("c00", "主机运行反馈"), ("c23", "主机启停输出"),
                          ("c22", "主机故障反馈")):
            pid = PREFIX + tag
            points[pid] = point(pid, tag, name, self.values[pid], at)
        change = self.read_changes.get(len(self.reads) + 1)
        if change:
            change(points)
        self.reads.append(deepcopy(points))
        return points

    def prepare_write(self):
        """模拟只刷新认证的预备操作，不能绕过其后的发送时间校验。"""
        self.prepare_calls += 1
        if self.prepare_delay:
            self.clock.sleep(self.prepare_delay)

    def write(self, pid, value):
        """强制检查白名单，并在POST之前读取已经提交的小时审计。"""
        if pid not in (HOUR,MINUTE) or type(value)is not int or value not in ({18,19,20} if pid==HOUR else {0,30}):
            raise AssertionError("控制器不得超出第一组晚间候选关机时分")
        at = self.clock()
        slot = at.replace(minute=at.minute//30*30, second=0, microsecond=0).isoformat()
        self.durable_attempts.append(deepcopy(control.Store(self.state_dir).get_run(slot)))
        self.writes.append((at, pid, value))
        self.write_times[pid] = at
        if self.apply:
            self.values[pid] = value
        if self.timeout_on_write == len(self.writes):
            raise SmartEnergyError("模拟请求超时，实际结果未知")
        return {"http_status": 200, "accepted": True}


class FlightShutdownTests(unittest.TestCase):
    """整点、航班证据、设备反馈和落盘防重共同保护真实控制路径。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.clock = FakeClock()
        self.platform = FakePlatform(self.clock, self.state_dir)
        self.source = Mock(side_effect=self.flight_source)
        self.analyzer = Mock(side_effect=self.analysis)
        self.evidence_mutation = None
        self.metadata_mutation = None
        self.source_delay = 0
        self.source_exception = None
        network = patch("requests.sessions.Session.request",
                        side_effect=AssertionError("离线测试禁止真实网络请求"))
        network.start()
        self.addCleanup(network.stop)

    def flight_source(self, day):
        """完整三日查询的元数据独立于算法计算时刻，不伪造陈旧查询为新鲜。"""
        if self.source_exception:
            raise self.source_exception
        started = self.clock()
        if self.source_delay:
            self.clock.sleep(self.source_delay)
        completed = self.clock()
        metadata = {
            "airport": "LUM", "complete_query_returned": True,
            "plan_dates": [(day + timedelta(days=n)).isoformat() + " 00:00:00"
                           for n in (-1, 0, 1)],
            "query_started_at": started.isoformat(), "query_completed_at": completed.isoformat(),
            "limited": False, "source_completeness_guaranteed": False,
            "device_control_sent": False,
        }
        if self.metadata_mutation:
            self.metadata_mutation(metadata)
        rows = [{"id": name, "flightNum": name, "planDate": day.isoformat() + " 00:00:00",
                 "arrDepFlag": "I", "origAirptCd": "AAA", "destAirptCd": "LUM",
                 "airptIataCd": "LUM", "schdArrvDt": day.isoformat() + " " + moment,
                 "regulatoryPlatformSysDataTime": completed.isoformat(),
                 "fcategory": "0", "fltStat": "计划", "fltStatChnNm": "计划"}
                for name, moment in (("TEST1", "19:30:00"), ("TEST2", "20:55:00"))]
        return rows, metadata

    def analysis(self, rows, day, fetched_at):
        """仿照真实analyze完整层次，只替代纯计算，不替代控制判断。"""
        if isinstance(day, str):
            day = datetime.fromisoformat(day).date()
        assessment = {
            "target_at": datetime(day.year, day.month, day.day, 20, tzinfo=ZONE).isoformat(),
            "status": "inside_gap", "candidate_shutdown_at": datetime(
                day.year, day.month, day.day, 20, tzinfo=ZONE).isoformat(),
            "quality_hold": False, "quality_flags": [],
            "gap_start": datetime(day.year, day.month, day.day, 19, 30, tzinfo=ZONE).isoformat(),
            "gap_end": datetime(day.year, day.month, day.day, 20, 55, tzinfo=ZONE).isoformat(),
            "gap_duration_minutes": 85, "remaining_gap_minutes": 55,
            "shutdown_policy": "evening_cutoff", "later_flights_block_shutdown": False,
            "pump_afterrun": "existing_site_delay", "thermal_capacity_evaluated": False,
        }
        result = {"version": "flight-gap-analysis-v1", "airport": "LUM", "date": day.isoformat(),
                  "timezone": "Asia/Shanghai", "minimum_minutes": 30, "target_hour": 20,
                  "fetched_at": fetched_at.isoformat(), "raw_count": len(rows),
                  "selected_day_flights": len(rows), "quality_flags": [], "warnings": [],
                  "device_control_sent": False, "is_shutdown_permission": False,
                  "modes": {"dynamic": {"areas": {"all": {
                      "label": "全场合并", "quality_flags": [], "selected_day_flights": len(rows),
                      "gap_count": 1, "evening_assessment": assessment,
                      "gaps":[{'start':assessment['gap_start'],'end':assessment['gap_end'],'duration_minutes':85}],
                  }}}}}
        if self.evidence_mutation:
            self.evidence_mutation(result)
        return result

    def execute(self, gate=lambda: True):
        """调用真实控制器，所有外部依赖均通过注入隔离。"""
        return control.execute(self.platform, self.state_dir, clock=self.clock,
                               sleep=self.clock.sleep, gate=gate,
                               flight_source=self.source, analyzer=self.analyzer,weather_source=self.weather_source)

    def weather_source(self):
        from nowapi_weather import parse_forecast,settings
        at=self.clock()
        return parse_forecast({'success':'1','result':{'cityid':'101291508','area_1':'云南','area_2':'芒市',
            'futureHour':[{'dateYmdh':(at.replace(minute=0,second=0,microsecond=0)+timedelta(hours=i)).strftime('%Y-%m-%d %H:%M:%S'),
                           'wtTemp':'24'} for i in range(24)]}},settings(),at)

    def test_real_control_is_fixed_shutdown_pair_and_durable_before_post(self):
        """成功路径只更改首组关机时分，POST之前必须已有持久标记。"""
        record = self.execute()
        self.assertEqual(record["status"], "confirmed")
        self.assertEqual({pid: value for _, pid, value in self.platform.writes}, {HOUR: 20, MINUTE: 0})
        self.assertTrue(all(r and r.get("attempted") for r in self.platform.durable_attempts))
        self.assertTrue(all(pid in (HOUR, MINUTE) for _, pid, _ in self.platform.writes))
        self.assertEqual(self.platform.values[PREFIX + "t01"], 11)
        self.assertEqual(self.platform.values[PREFIX + "t04"], 20)

    def test_ai_or_strategy_gate_off_does_not_query_or_write(self):
        """开关关闭、停止中或门控异常均不进入设备与航班取数。"""
        def broken():
            raise RuntimeError("模拟本地门控不可用")
        for index, gate in enumerate((lambda: False, broken,
                                      lambda: {"id": "flight_linked_control", "effective_enabled": False, "status": "idle"},
                                      lambda: {"id": "flight_linked_control", "effective_enabled": True, "status": "stopping"})):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.execute(gate)
                self.assertEqual(self.platform.reads, [])
                self.assertEqual(self.platform.writes, [])
        self.source.assert_not_called()

    def test_outside_evening_or_missed_slot_never_starts(self):
        """8点、17点以及整点两分钟之后不会补跑。"""
        for index, at in enumerate((BASE.replace(hour=8), BASE.replace(hour=17),
                                    BASE.replace(minute=2), BASE.replace(minute=59))):
            with self.subTest(at=at):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.clock.current = at
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.assertEqual(self.execute()["status"], "skipped_window")
                self.assertEqual(self.platform.reads, [])
                self.assertEqual(self.platform.writes, [])

    def test_last_candidate_and_later_never_write_an_elapsed_target(self):
        """20:30及以后不能补写已经经过的候选；20点仍可选择20:30。"""
        for hour in (20,21,22,23):
            with self.subTest(hour=hour):
                self.state_dir = Path(self.temporary.name) / str(hour)
                self.clock.current = BASE.replace(hour=hour,minute=30)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_stopped_starting_fault_or_inconsistent_feedback_never_writes(self):
        """只有两种运行反馈一致为1且无故障时允许修改关机表。"""
        for index, changes in enumerate(({RUNNING: 0, OUTPUT: 0}, {RUNNING: 0},
                                         {OUTPUT: 0}, {FAULT: 1}, {RUNNING: 2},
                                         {OUTPUT: None}, {RUNNING: True})):
            with self.subTest(changes=changes):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.values.update(changes)
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_stale_future_or_missing_feedback_never_writes(self):
        """反馈的新鲜度由上报时间决定，不能以查询成功代替。"""
        changes = [lambda p: p.pop(RUNNING),
                   lambda p: p[RUNNING].update(updateTime=(BASE-timedelta(seconds=181)).isoformat()),
                   lambda p: p[RUNNING].update(updateTime=(BASE+timedelta(seconds=1)).isoformat()),
                   lambda p: p[RUNNING].update(projectId=999)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.read_changes[1] = change
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_source_failure_or_invalid_metadata_cannot_be_empty_success(self):
        """失败、错机场、截断、陈旧或缺计划日不具备控制证据。"""
        mutations = [lambda m: m.update(complete_query_returned=False),
                     lambda m: m.update(airport="OTHER"),
                     lambda m: m.update(limited=True),
                     lambda m: m.update(plan_dates=m["plan_dates"][1:]),
                     lambda m: m.update(query_completed_at=(BASE-timedelta(seconds=121)).isoformat()),
                     lambda m: m.update(query_completed_at=(BASE+timedelta(seconds=1)).isoformat())]
        for index, change in enumerate(mutations):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.metadata_mutation = change
                self.execute()
                self.assertEqual(self.platform.writes, [])
        self.state_dir = Path(self.temporary.name) / "failed"
        self.metadata_mutation = None
        self.source_exception = RuntimeError("模拟查询失败")
        self.platform = FakePlatform(self.clock, self.state_dir)
        self.execute()
        self.assertEqual(self.platform.writes, [])

    def test_missing_gap_candidate_quality_hold_or_wrong_date_never_writes(self):
        """只有当天dynamic全场20点有效候选能推进，不采纳其他区域。"""
        def area(result):return result['modes']['dynamic']['areas']['all']
        mutations = [lambda r: area(r).update(gaps=[]),
                     lambda r: area(r).update(quality_flags=['missing_event_time']),
                     lambda r: area(r)['gaps'][0].update(start=BASE.replace(day=6,hour=19,minute=30).isoformat()),
                     lambda r: area(r)['gaps'][0].update(duration_minutes=10)]
        for index, change in enumerate(mutations):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.evidence_mutation = change
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_analysis_warnings_scope_rejection_or_empty_counts_hold_control(self):
        """归一化丢弃和缺资料不能因已有候选就被忽略。"""
        mutations = [lambda r: r.update(warnings=["存在缺少计划日的记录"]),
                     lambda r: r.update(normalization_counts={"scope_rejected": 1}),
                     lambda r: r.update(raw_count=0),
                     lambda r: r.update(selected_day_flights=0),
                     lambda r: r.update(timezone="UTC"),
                     lambda r: r.update(minimum_minutes=10),
                     lambda r: r.update(date="2026-11-04")]
        for index, change in enumerate(mutations):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.evidence_mutation = change
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_empty_unrelated_international_area_does_not_block_valid_all_scope(self):
        """设备依据全场空档，不能被国际区域单独无航班的提示误拦截。"""
        def unrelated(result):
            result["quality_flags"] = ["no_selected_day_flights"]
            result["modes"]["dynamic"]["areas"]["international:I"] = {
                "quality_flags": ["no_selected_day_flights"],
                "evening_assessment": {"quality_hold": True, "candidate_shutdown_at": None},
            }
        self.evidence_mutation = unrelated
        self.assertEqual(self.execute()["status"], "confirmed")

    def test_later_flights_do_not_delay_evening_cutoff(self):
        """用户指定20点档口，20点后仍有航班也不取消有效候选。"""
        def later(result):
            evening = result["modes"]["dynamic"]["areas"]["all"]["evening_assessment"]
            evening.update(later_today_flight_count=10)
        self.evidence_mutation = later
        self.assertEqual(self.execute()["status"], "confirmed")

    def test_existing_twenty_schedule_is_read_only(self):
        """已有20:00只核对，不再重复POST。"""
        self.platform.values.update({HOUR: 20, MINUTE: 0})
        self.execute()
        self.assertEqual(self.platform.writes, [])

    def test_same_slot_or_restart_cannot_repeat_writes(self):
        """重复调用同一小时依赖持久记录，无需靠进程内标志。"""
        self.assertEqual(self.execute()["status"], "confirmed")
        count = len(self.platform.writes)
        self.execute()
        self.execute()
        self.assertEqual(len(self.platform.writes), count)

    def test_next_hour_refetches_flights_without_rewriting_same_target(self):
        """每整点刷新判断证据，已确认绝对目标不会累计下发。"""
        self.assertEqual(self.execute()["status"], "confirmed")
        count = len(self.platform.writes)
        self.clock.current = BASE.replace(hour=19)
        self.execute()
        self.assertEqual(len(self.platform.writes), count)
        self.assertEqual(self.source.call_count, 2)

    def test_does_not_postpone_existing_earlier_shutdown(self):
        """没有更早的合格空档时，不把现场18:30或18:35延后到20点。"""
        for index, minute in enumerate((30, 35)):
            with self.subTest(minute=minute):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.values.update({HOUR: 18, MINUTE: minute})
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_intermediate_pair_must_not_form_a_past_shutdown_time(self):
        """18点把19:00提前到18:30，必须先分钟，避免中间出现已过的18:00。"""
        self.platform.values.update({HOUR:19,MINUTE:0})
        def gap(result):
            result['modes']['dynamic']['areas']['all']['gaps']=[{'start':BASE.replace(hour=18,minute=5).isoformat(),
                'end':BASE.replace(hour=19,minute=10).isoformat(),'duration_minutes':65}]
        self.evidence_mutation=gap
        self.assertEqual(self.execute()["status"], "confirmed")
        self.assertEqual(self.platform.writes[0][1], MINUTE)

    def test_prewrite_feedback_gate_and_protected_schedule_changes_cancel(self):
        """发送前发生停机或改表，绝不能沿旧快照继续POST。"""
        mutations = [lambda p: p[RUNNING].update(value=0),
                     lambda p: p[FAULT].update(value=1),
                     lambda p: p[MINUTE].update(value=40),
                     lambda p: p[PREFIX+"t04"].update(value=30)]
        for index, change in enumerate(mutations):
            with self.subTest(index=index):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.platform.read_changes[2] = change
                self.execute()
                self.assertEqual(self.platform.writes, [])
        self.state_dir = Path(self.temporary.name) / "gate"
        self.platform = FakePlatform(self.clock, self.state_dir)
        responses = iter((True, False))
        self.execute(lambda: next(responses))
        self.assertEqual(self.platform.writes, [])

    def test_unknown_unapplied_write_never_retries_or_sends_remaining_field(self):
        """单次请求结果未知只回读，不能盲目补发另一字段。"""
        self.platform.apply = False
        self.platform.timeout_on_write = 1
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 1)
        self.clock.current = BASE.replace(hour=19)
        self.execute()
        self.assertEqual(len(self.platform.writes), 1)

    def test_unknown_applied_write_converges_by_readback_without_retry(self):
        """网络响应未知但设备生效时，以新上报确认继续，不重发同点。"""
        self.platform.timeout_on_write = 1
        self.assertEqual(self.execute()["status"], "confirmed")
        self.assertEqual(len(self.platform.writes), 2)
        self.assertEqual(len({pid for _, pid, _ in self.platform.writes}), 2)

    def test_old_timestamp_cannot_confirm_matching_target(self):
        """设备值相同但时间戳未更新不构成本次生效证明。"""
        self.platform.stale_confirmation = True
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 1)

    def test_partial_write_and_ai_off_does_not_roll_back_or_finish_pair(self):
        """首字段已确认后关闭AI，仅保留部分状态，不自动写剩余字段。"""
        def gate():
            return len(self.platform.writes) == 0
        self.assertEqual(self.execute(gate)["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 1)

    def test_manual_change_persists_day_hold_even_if_old_value_restored(self):
        """人工改表后的日内接管不能仅依赖当前值是否又相等。"""
        self.assertEqual(self.execute()["status"], "confirmed")
        count = len(self.platform.writes)
        self.clock.current = BASE.replace(hour=19)
        self.platform.values.update({HOUR: 21, MINUTE: 0})
        self.execute()
        state = control.Store(self.state_dir).read_state()
        self.assertTrue(state.get("external_hold"))
        self.clock.current = BASE.replace(hour=20)
        self.platform.values.update({HOUR: 20, MINUTE: 0})
        self.execute()
        self.assertEqual(len(self.platform.writes), count)

    def test_slow_source_or_prepared_auth_cannot_cross_dispatch_window(self):
        """取数或认证耗时后必须检查真实时刻，不沿用先前clock。"""
        for index, kind in enumerate(("source", "auth")):
            with self.subTest(kind=kind):
                self.state_dir = Path(self.temporary.name) / str(index)
                self.clock.current = BASE
                self.platform = FakePlatform(self.clock, self.state_dir)
                self.source_delay = 121 if kind == "source" else 0
                self.platform.prepare_delay = 121 if kind == "auth" else 0
                self.execute()
                self.assertEqual(self.platform.writes, [])

    def test_slow_gate_cannot_reuse_a_time_before_its_request(self):
        """门控返回时已过发送窗口，先前取得的sent_at不能继续有效。"""
        calls = 0

        def gate():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.clock.sleep(121)
            return True

        self.execute(gate)
        self.assertEqual(self.platform.writes, [])

    def test_slow_durable_attempt_commit_cannot_send_after_window(self):
        """SQLite落盘可能阻塞，提交后仍须再次校验真实POST时刻。"""
        original = control.Store.save
        delayed = False

        def save(store, record, state=None):
            nonlocal delayed
            result = original(store, record, state)
            if record.get("attempted") and not delayed:
                delayed = True
                self.clock.sleep(121)
            return result

        with patch.object(control.Store, "save", save):
            self.execute()
        self.assertEqual(self.platform.writes, [])

    def test_slow_readback_cannot_confirm_after_five_minute_budget(self):
        """请求回读返回时已经超出确认期限，不得把目标值当作及时完成。"""
        original = self.platform.read
        delayed = False

        def read():
            nonlocal delayed
            if self.platform.writes and not delayed:
                delayed = True
                self.clock.sleep(301)
            return original()

        self.platform.read = read
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.assertEqual(len(self.platform.writes), 1)

    def test_next_day_rebuilds_baseline_but_does_not_forget_unconfirmed_post(self):
        """换日不能抹去未知请求，否则会重复控制未确定的设备目标。"""
        self.platform.apply = False
        self.platform.timeout_on_write = 1
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.clock.current = BASE + timedelta(days=1)
        self.execute()
        self.assertEqual(len(self.platform.writes), 1)

    def test_every_authorized_half_hour_can_be_selected(self):
        """五个候选分别落入空档时都能生效，不再硬编码20点。"""
        for i,clock in enumerate(control.CANDIDATE_TIMES):
            with self.subTest(clock=clock):
                self.state_dir=Path(self.temporary.name)/f'candidate-{i}';self.clock.current=BASE
                self.platform=FakePlatform(self.clock,self.state_dir)
                goal=BASE.replace(hour=int(clock[:2]),minute=int(clock[3:]))
                def mutation(result,goal=goal):
                    result['modes']['dynamic']['areas']['all']['gaps']=[{'start':(goal-timedelta(minutes=20)).isoformat(),
                        'end':(goal+timedelta(minutes=20)).isoformat(),'duration_minutes':40}]
                self.evidence_mutation=mutation
                record=self.execute();self.assertEqual(record['status'],'confirmed')
                self.assertEqual(record['target_at'],goal.isoformat())
                self.assertEqual((self.platform.values[HOUR],self.platform.values[MINUTE]),(goal.hour,goal.minute))
    def test_weather_missing_never_falls_back_to_old_twenty_rule(self):
        from nowapi_weather import WeatherError
        with patch.object(self,'weather_source',side_effect=WeatherError('天气不可用')):
            result=self.execute()
        self.assertEqual(result['status'],'weather_hold');self.assertFalse(self.platform.writes)
    def test_hot_forecast_never_writes_shutdown(self):
        original=self.weather_source
        def hot():
            result=original()
            for p in result['hourly']:p['temperature_c']=28
            return result
        with patch.object(self,'weather_source',side_effect=hot):result=self.execute()
        self.assertEqual(result['status'],'no_candidate');self.assertFalse(self.platform.writes)
    def test_half_hour_is_a_new_durable_slot(self):
        self.assertEqual(self.execute()['status'],'confirmed')
        self.clock.current=BASE.replace(minute=30)
        self.assertEqual(self.execute()['status'],'no_change')
        self.assertIsNotNone(control.Store(self.state_dir).get_run(self.clock().isoformat()))
        self.assertEqual(self.source.call_count,2)
    def test_old_fixed_twenty_assessment_is_not_control_input(self):
        def old_wrong(result):
            result['modes']['dynamic']['areas']['all']['evening_assessment'].update(status='event_at_target',candidate_shutdown_at=None)
        self.evidence_mutation=old_wrong
        self.assertEqual(self.execute()['status'],'confirmed')


if __name__ == "__main__":
    unittest.main()
