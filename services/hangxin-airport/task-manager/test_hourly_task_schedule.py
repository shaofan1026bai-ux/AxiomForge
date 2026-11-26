"""验证整点与固定时刻策略、上海时区、开关门控和重启不补发。"""

from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import TaskConfig, TaskManager, create_app


BEIJING = ZoneInfo("Asia/Shanghai")
HOURS = list(range(11, 22))
HALF_HOUR_TIMES = [f"{hour:02d}:{minute:02d}" for hour in range(11, 21)
                   for minute in (0, 30)] + ["21:00"]


class HourlyTaskScheduleTests(unittest.TestCase):
    """使用临时配置与模拟时钟验证受管理的整点任务。"""

    def setUp(self):
        """隔离配置、SQLite和脚本，并模拟调度器与服务的北京时间。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / "safe_test.py"
        self.script.write_text("raise RuntimeError('测试不应真正启动脚本')\n", encoding="utf-8")
        self.config_path = self.root / "tasks.json"
        self.clock_at = datetime(2026, 11, 3, 10, 37, 23, tzinfo=BEIJING)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.app_clock = self.stack.enter_context(patch("app.datetime", wraps=datetime))
        self.scheduler_clock = self.stack.enter_context(patch("apscheduler.schedulers.base.datetime", wraps=datetime))
        self.app_clock.now.side_effect = self._clock_now
        self.scheduler_clock.now.side_effect = self._clock_now
        self.popen = self.stack.enter_context(patch("app.subprocess.Popen"))
        self.managers = []
        self.addCleanup(self._close_managers)

    def _clock_now(self, zone=None):
        """让服务与调度器看到同一时刻，避免测试触发真实子进程。"""
        return self.clock_at.astimezone(zone) if zone is not None else self.clock_at.replace(tzinfo=None)

    def _close_managers(self):
        """每次测试结束正常关闭线程和SQLite统计生命周期。"""
        for manager in reversed(self.managers):
            manager.shutdown()

    def _manager(self, *, hours=HOURS, times=None, selected=True, mode=True):
        """创建一个真实TaskManager，但任务脚本始终不执行。"""
        task = {"id": "hourly_strategy", "script": self.script.name,
                "interval_seconds": 1800 if times is not None else 3600, "enabled": selected}
        if times is not None:
            task["execution_times"] = times
        elif hours is not None:
            task["execution_hours"] = hours
        self.config_path.write_text(json.dumps({"ai_mode_enabled": mode, "tasks": [task]}), encoding="utf-8")
        manager = TaskManager(self.config_path)
        self.managers.append(manager)
        manager.start()
        return manager

    def test_daily_eleven_fires_from_eleven_to_twenty_one_in_shanghai(self):
        """11至21时每天恰有11个整点，时区固定为上海。"""
        manager = self._manager()
        job = manager.scheduler.get_job("hourly_strategy")
        self.assertIsInstance(job.trigger, CronTrigger)
        self.assertEqual(str(job.trigger.timezone), "Asia/Shanghai")
        self.assertEqual(job.misfire_grace_time, 60)
        self.assertEqual(job.max_instances, 1)
        self.assertTrue(job.coalesce)
        after = datetime(2026, 11, 3, tzinfo=BEIJING)
        previous = None
        fires = []
        while True:
            fire = job.trigger.get_next_fire_time(previous, after)
            if fire.date() != after.date():
                break
            fires.append(fire)
            previous = fire
            after = fire + timedelta(microseconds=1)
        self.assertEqual(len(fires), 11)
        self.assertEqual(fires[0], datetime(2026, 11, 3, 11, tzinfo=BEIJING))
        self.assertEqual(fires[-1], datetime(2026, 11, 3, 21, tzinfo=BEIJING))
        self.assertTrue(all(fire.minute == fire.second == fire.microsecond == 0 for fire in fires))
        self.assertEqual(fire, datetime(2026, 11, 4, 11, tzinfo=BEIJING))
        self.popen.assert_not_called()

    def test_non_hour_initialization_waits_until_next_eleven(self):
        """10:37初始化仅计划11:00，不立即运行，也不把间隔偏移当整点。"""
        manager = self._manager()
        state = manager.snapshot("hourly_strategy")
        self.assertEqual(state["next_run_time"], "2026-11-03T11:00:00+08:00")
        self.assertEqual(state["execution_hours"], HOURS)
        self.assertEqual(state["interval_seconds"], 3600)
        self.assertFalse(state["running"])
        self.popen.assert_not_called()

    def test_restart_does_not_replay_missed_hours(self):
        """重启恢复已选开关，但只计划当前时刻之后的下一整点。"""
        early = self._manager()
        early.shutdown()
        self.clock_at = datetime(2026, 11, 3, 14, 27, tzinfo=BEIJING)
        later = self._manager()
        self.assertTrue(later.snapshot("hourly_strategy")["effective_enabled"])
        self.assertEqual(later.snapshot("hourly_strategy")["next_run_time"], "2026-11-03T15:00:00+08:00")
        self.popen.assert_not_called()

    def test_reenable_skips_old_slots_and_invalidates_old_generation(self):
        """关闭后重新开启重建cron作业，旧队列代号不能偷跑或补发。"""
        manager = self._manager()
        original_generation = manager.get("hourly_strategy").generation
        manager.disable("hourly_strategy")
        self.clock_at = datetime(2026, 11, 3, 14, 27, tzinfo=BEIJING)
        restored = manager.enable("hourly_strategy")
        self.assertEqual(restored["next_run_time"], "2026-11-03T15:00:00+08:00")
        self.assertGreater(manager.get("hourly_strategy").generation, original_generation)
        manager._run("hourly_strategy", original_generation)
        self.popen.assert_not_called()

    def test_task_and_ai_switches_each_suppress_hourly_job(self):
        """策略关闭或AI关闭都会暂停cron，保存的策略选择维持原语义。"""
        manager = self._manager()
        manager.disable("hourly_strategy")
        task = manager.get("hourly_strategy")
        self.assertIsNone(manager.scheduler.get_job(task.config.id).next_run_time)
        self.assertFalse(manager.snapshot(task.config.id)["effective_enabled"])
        manager._run(task.config.id, task.generation)
        manager.enable(task.config.id)
        active_generation = task.generation
        manager.set_mode(False)
        state = manager.snapshot(task.config.id)
        self.assertTrue(state["enabled"])
        self.assertFalse(state["effective_enabled"])
        self.assertEqual(state["status"], "paused")
        self.assertIsNone(manager.scheduler.get_job(task.config.id).next_run_time)
        manager._run(task.config.id, active_generation)
        manager._run(task.config.id, task.generation)
        self.clock_at = datetime(2026, 11, 3, 14, 27, tzinfo=BEIJING)
        manager.set_mode(True)
        self.assertEqual(manager.snapshot(task.config.id)["next_run_time"], "2026-11-03T15:00:00+08:00")
        self.popen.assert_not_called()

    def test_interval_tasks_keep_existing_behavior_and_response_keys(self):
        """未配置小时列表的任务继续按间隔运行，不增加响应字段。"""
        manager = self._manager(hours=None)
        job = manager.scheduler.get_job("hourly_strategy")
        self.assertIsInstance(job.trigger, IntervalTrigger)
        self.assertEqual(job.trigger.interval, timedelta(seconds=3600))
        self.assertNotIn("execution_hours", manager.snapshot("hourly_strategy"))
        self.popen.assert_not_called()

    def test_invalid_hour_lists_are_rejected_strictly(self):
        """空列表、重复、越界、非整数及布尔值不能进入调度配置。"""
        base = {"id": "test", "script": "test.py", "interval_seconds": 3600}
        for hours in [[], [11, 11], [-1], [24], [11.0], ["11"], [True], "11", [None]]:
            with self.subTest(hours=hours):
                with self.assertRaises(ValidationError):
                    TaskConfig.model_validate({**base, "execution_hours": hours})
        self.assertIsNone(TaskConfig.model_validate(base).execution_hours)
        self.assertIsNone(TaskConfig.model_validate({**base, "execution_hours": None}).execution_hours)
        self.assertEqual(TaskConfig.model_validate({**base, "execution_hours": [0, 23]}).execution_hours, [0, 23])

    def test_half_hour_times_have_exactly_twenty_one_fires_and_stop_at_twenty_one(self):
        """11:00至21:00含两端共21轮，不能额外产生21:30。"""
        manager = self._manager(times=HALF_HOUR_TIMES)
        job = manager.scheduler.get_job("hourly_strategy")
        self.assertIsInstance(job.trigger, OrTrigger)
        self.assertEqual(len(job.trigger.triggers), 2)
        self.assertTrue(all(str(trigger.timezone) == "Asia/Shanghai"
                            for trigger in job.trigger.triggers))
        self.assertEqual(job.misfire_grace_time, 60)
        self.assertEqual(job.max_instances, 1)
        self.assertTrue(job.coalesce)
        day_start = self.clock_at.replace(hour=0, minute=0, second=0, microsecond=0)
        previous, after, fires = None, day_start, []
        while True:
            fire = job.trigger.get_next_fire_time(previous, after)
            if fire.date() != day_start.date():
                break
            fires.append(fire)
            previous, after = fire, fire + timedelta(microseconds=1)
        self.assertEqual([value.strftime("%H:%M") for value in fires], HALF_HOUR_TIMES)
        self.assertEqual(fire, day_start.replace(hour=11) + timedelta(days=1))
        self.assertTrue(all(value.second == value.microsecond == 0 for value in fires))
        self.popen.assert_not_called()

    def test_half_hour_snapshot_and_restart_wait_for_next_valid_slot(self):
        """重启后按墙上时刻等待14:30，不按启动时刻偏移或补跑14:00。"""
        manager = self._manager(times=HALF_HOUR_TIMES)
        state = manager.snapshot("hourly_strategy")
        self.assertEqual(state["execution_times"], HALF_HOUR_TIMES)
        self.assertEqual(state["execution_hours"], HOURS)
        self.assertEqual(state["interval_seconds"], 1800)
        self.assertEqual(state["next_run_time"], "2026-11-03T11:00:00+08:00")
        manager.shutdown()
        self.clock_at = self.clock_at.replace(hour=14, minute=27)
        later = self._manager(times=HALF_HOUR_TIMES)
        self.assertEqual(later.snapshot("hourly_strategy")["next_run_time"],
                         "2026-11-03T14:30:00+08:00")
        self.assertTrue(later.snapshot("hourly_strategy")["effective_enabled"])
        self.popen.assert_not_called()

    def test_half_hour_reenable_and_ai_gate_preserve_selection_and_skip_old_slot(self):
        """两级开关继续使同一策略失效，恢复后只安排未来半点。"""
        manager = self._manager(times=HALF_HOUR_TIMES)
        original_generation = manager.get("hourly_strategy").generation
        manager.disable("hourly_strategy")
        self.clock_at = self.clock_at.replace(hour=14, minute=27)
        restored = manager.enable("hourly_strategy")
        self.assertEqual(restored["next_run_time"], "2026-11-03T14:30:00+08:00")
        manager._run("hourly_strategy", original_generation)
        manager.set_mode(False)
        state = manager.snapshot("hourly_strategy")
        self.assertTrue(state["enabled"])
        self.assertFalse(state["effective_enabled"])
        self.assertIsNone(state["next_run_time"])
        self.clock_at = self.clock_at.replace(minute=44)
        manager.set_mode(True)
        self.assertEqual(manager.snapshot("hourly_strategy")["next_run_time"],
                         "2026-11-03T15:00:00+08:00")
        self.popen.assert_not_called()

    def test_task_api_exposes_exact_times_and_compatible_hour_summary(self):
        """列表、详情和开关接口均返回相同的精确时刻，小时概要保留兼容。"""
        manager = self._manager(times=HALF_HOUR_TIMES)
        manager.shutdown()
        with TestClient(create_app(self.config_path)) as client:
            for response in (client.get("/tasks/hourly_strategy"),
                             client.get("/tasks"),
                             client.post("/tasks/hourly_strategy/disable"),
                             client.post("/tasks/hourly_strategy/enable")):
                self.assertEqual(response.status_code, 200)
                state = response.json()
                if isinstance(state, list):
                    state = state[0]
                self.assertEqual(state["execution_times"], HALF_HOUR_TIMES)
                self.assertEqual(state["execution_hours"], HOURS)
                self.assertEqual(state["interval_seconds"], 1800)
            self.assertEqual(client.get("/tasks/hourly_strategy").json()["next_run_time"],
                             "2026-11-03T11:00:00+08:00")
        self.popen.assert_not_called()

    def test_after_last_slot_waits_until_tomorrow_instead_of_twenty_one_thirty(self):
        """21:00之后初始化不追加21:30，也不立即补执行最后一轮。"""
        self.clock_at = self.clock_at.replace(hour=21, minute=1)
        manager = self._manager(times=HALF_HOUR_TIMES)
        self.assertEqual(manager.snapshot("hourly_strategy")["next_run_time"],
                         "2026-11-04T11:00:00+08:00")
        self.popen.assert_not_called()

    def test_single_minute_group_uses_one_cron_and_honors_midnight(self):
        """单一时刻不需合并触发器，跨午夜仍按北京时间日历运行。"""
        self.clock_at = self.clock_at.replace(hour=23, minute=59)
        manager = self._manager(times=["00:00"])
        job = manager.scheduler.get_job("hourly_strategy")
        self.assertIsInstance(job.trigger, CronTrigger)
        self.assertEqual(manager.snapshot("hourly_strategy")["next_run_time"],
                         "2026-11-04T00:00:00+08:00")
        self.popen.assert_not_called()

    def test_invalid_exact_times_and_conflicting_schedules_are_rejected(self):
        """精确时刻严格为HH:MM，不接受隐式转换、重复或与整点列表并用。"""
        base = {"id": "test", "script": "test.py", "interval_seconds": 1800}
        invalid = [[], ["11:00", "11:00"], ["24:00"], ["11:60"], ["1:30"],
                   ["11:0"], ["11:00:00"], [" 11:00"], ["11:00 "], ["１１:００"],
                   [11], [True], [None], "11:00"]
        for times in invalid:
            with self.subTest(times=times):
                with self.assertRaises(ValidationError):
                    TaskConfig.model_validate({**base, "execution_times": times})
        with self.assertRaises(ValidationError):
            TaskConfig.model_validate({**base, "execution_hours": [11], "execution_times": ["11:30"]})
        self.assertIsNone(TaskConfig.model_validate(base).execution_times)
        self.assertIsNone(TaskConfig.model_validate({**base, "execution_times": None}).execution_times)
        self.assertEqual(TaskConfig.model_validate({**base, "execution_times": ["00:00", "23:59"]}).execution_times,
                         ["00:00", "23:59"])

    def test_shipped_temperature_and_flight_use_half_hours_other_cron_tasks_keep_hours(self):
        """温调和航班按半点，开机策略仅08至17点，VRV保持午夜整点。"""
        shipped = json.loads(Path(__file__).with_name("tasks.json").read_text(encoding="utf-8"))
        tasks = {item["id"]: TaskConfig.model_validate(item) for item in shipped["tasks"]}
        temperature = tasks["chiller_temperature"]
        self.assertEqual(temperature.execution_times, HALF_HOUR_TIMES)
        self.assertIsNone(temperature.execution_hours)
        self.assertEqual(temperature.interval_seconds, 1800)
        for task_id, hours in (("chiller_start_stop", list(range(8, 18))),
                               ("vrv_schedule", [0])):
            with self.subTest(task_id=task_id):
                self.assertEqual(tasks[task_id].execution_hours, hours)
                self.assertIsNone(tasks[task_id].execution_times)
        self.assertEqual(tasks['flight_linked_control'].execution_times,['18:00','18:30','19:00','19:30','20:00','20:30'])
        self.assertEqual(tasks['flight_linked_control'].interval_seconds,1800)

    def test_flight_half_hour_trigger_covers_all_six_evening_rounds(self):
        """18点开始半小时执行，次日不补发20:30之后已错过的轮次。"""
        times=['18:00','18:30','19:00','19:30','20:00','20:30']
        manager=self._manager(times=times);trigger=manager.scheduler.get_job('hourly_strategy').trigger
        at=datetime(2026,11,3,17,59,tzinfo=BEIJING);previous=None;fires=[]
        for _ in range(7):
            fire=trigger.get_next_fire_time(previous,at);fires.append(fire)
            previous=fire;at=fire+timedelta(microseconds=1)
        self.assertEqual([v.strftime('%H:%M') for v in fires[:6]],times)
        self.assertEqual(fires[-1],datetime(2026,11,4,18,tzinfo=BEIJING))


if __name__ == "__main__":
    unittest.main()
