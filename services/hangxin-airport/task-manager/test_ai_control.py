"""AI 总开关、策略选择及持久化计时的集成测试。"""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import create_app


BEIJING = ZoneInfo("Asia/Shanghai")


class AIControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "tasks.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def configure(self, task_ids=("first", "second"), *, selected=(), ai_mode=False, long=False):
        tasks = []
        for task_id in task_ids:
            script = self.root / f"{task_id}.py"
            script.write_text(
                "import os\nfrom pathlib import Path\nimport time\n"
                "with Path(__file__).with_suffix('.runs').open('a') as log:\n"
                "    log.write(str(os.getpid()) + '\\n')\n"
                + ("time.sleep(30)\n" if long else "print('calculated')\n"),
                encoding="utf-8",
            )
            tasks.append({
                "id": task_id,
                "name": f"策略 {task_id}",
                "script": script.name,
                "interval_seconds": 0.15 if long else 3600,
                "enabled": task_id in selected,
            })
        self.config_path.write_text(
            json.dumps({"ai_mode_enabled": ai_mode, "tasks": tasks}), encoding="utf-8"
        )

    def client(self, config_path=None):
        return TestClient(create_app(config_path or self.config_path))

    def mode(self, client, enabled):
        response = client.put("/ai-control-mode", json={"enabled": enabled})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def select(self, client, task_id, enabled=True):
        action = "enable" if enabled else "disable"
        response = client.post(f"/tasks/{task_id}/{action}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def runtime(self, client):
        response = client.get("/ai-control-mode/runtime")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_for_runs(self, task_id, count):
        marker = self.root / f"{task_id}.runs"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.exists():
                runs = marker.read_text().splitlines()
                if len(runs) >= count:
                    return int(runs[-1])
            time.sleep(0.025)
        self.fail(f"任务 {task_id} 未执行到第 {count} 次")

    def assert_process_gone(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.025)
        self.fail(f"进程仍然存在：{pid}")

    def test_mode_contract_strict_boolean_and_empty_runtime(self):
        self.configure(task_ids=())
        with self.client() as client:
            for invalid in ["true", "false", 1, 0, None]:
                self.assertEqual(
                    client.put("/ai-control-mode", json={"enabled": invalid}).status_code, 422
                )
            self.assertEqual(client.put("/ai-control-mode", json={}).status_code, 422)
            mode = self.mode(client, True)
            self.assertEqual(
                set(mode), {"enabled", "status", "selected_task_count", "active_task_count", "updated_at"}
            )
            self.assertTrue(mode["enabled"])
            self.assertEqual(mode["status"], "enabled")
            self.assertEqual(mode["selected_task_count"], 0)
            self.assertEqual(mode["active_task_count"], 0)
            self.assertIsNotNone(datetime.fromisoformat(mode["updated_at"]).tzinfo)
            runtime = self.runtime(client)
            self.assertEqual(set(runtime), {
                "enabled", "month", "timezone", "monthly_mode_runtime_hours",
                "total_mode_runtime_hours", "monthly_strategy_runtime_hours",
                "tasks",
            })
            self.assertNotIn("monthly_strategy_runtime_days", runtime)
            self.assertTrue(all(key.endswith("_hours") for key in runtime if "runtime" in key))
            self.assertEqual(runtime["timezone"], "Asia/Shanghai")
            self.assertEqual(runtime["monthly_strategy_runtime_hours"], 0)
            self.assertEqual(runtime["tasks"], [])

    def test_mode_off_stops_script_preserves_selection_and_restores_only_selected(self):
        self.configure(selected=("first",), long=True)
        with self.client() as client:
            paused = client.get("/tasks/first").json()
            self.assertTrue(paused["enabled"])
            self.assertFalse(paused["effective_enabled"])
            self.assertEqual(paused["status"], "paused")
            self.select(client, "second")
            self.select(client, "second", False)
            time.sleep(0.25)
            self.assertFalse((self.root / "first.runs").exists())
            self.assertFalse((self.root / "second.runs").exists())

            mode = self.mode(client, True)
            self.assertEqual(mode["selected_task_count"], 1)
            self.assertEqual(mode["active_task_count"], 1)
            pid = self.wait_for_runs("first", 1)
            self.assertTrue(client.get("/tasks/first").json()["running"])

            mode = self.mode(client, False)
            self.assertFalse(mode["enabled"])
            self.assertEqual(mode["selected_task_count"], 1)
            self.assertEqual(mode["active_task_count"], 0)
            self.assert_process_gone(pid)
            paused = client.get("/tasks/first").json()
            self.assertTrue(paused["enabled"])
            self.assertFalse(paused["effective_enabled"])
            self.assertEqual(paused["status"], "paused")
            self.assertTrue(all(not item["enabled"] for item in client.get("/tasks/overview").json()["items"]))
            time.sleep(0.3)
            self.assertEqual(len((self.root / "first.runs").read_text().splitlines()), 1)
            self.mode(client, True)
            self.wait_for_runs("first", 2)
            self.assertFalse((self.root / "second.runs").exists())

    def test_stopping_one_strategy_does_not_block_another_strategy_execution(self):
        self.configure(selected=("first", "second"), ai_mode=True)
        (self.root / "first.py").write_text(
            "import os\nfrom pathlib import Path\nimport time\n"
            "Path(__file__).with_suffix('.runs').write_text(str(os.getpid()))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        with self.client() as client:
            manager = client.app.state.manager
            first_runner = threading.Thread(
                target=manager._run,
                args=("first", manager.get("first").generation),
                daemon=True,
            )
            first_runner.start()
            first_pid = self.wait_for_runs("first", 1)
            entered = threading.Event()
            release = threading.Event()
            stop_errors = []
            original_stop = manager._stop_process

            def delayed_stop(process):
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("测试未及时释放停止操作")
                original_stop(process)

            def disable_first():
                try:
                    manager.disable("first")
                except Exception as exc:
                    stop_errors.append(exc)

            with patch.object(manager, "_stop_process", side_effect=delayed_stop):
                stopper = threading.Thread(target=disable_first, daemon=True)
                stopper.start()
                try:
                    self.assertTrue(entered.wait(timeout=3))
                    self.assertTrue(manager.get("first").stopping)
                    # second 的周期为一小时；直接触发当前代号，排除自主调度的干扰。
                    manager._run("second", manager.get("second").generation)
                    self.assertTrue((self.root / "second.runs").exists())
                    self.assertEqual(manager.snapshot("second")["last_result"], "success")
                finally:
                    release.set()
                    stopper.join(timeout=5)
            first_runner.join(timeout=5)
            self.assertFalse(stopper.is_alive())
            self.assertFalse(first_runner.is_alive())
            self.assertEqual(stop_errors, [])
            self.assert_process_gone(first_pid)

    def test_parallel_strategy_hours_use_max_and_mode_toggles_are_idempotent(self):
        self.configure()
        start = datetime(2026, 10, 1, tzinfo=BEIJING)
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = start - timedelta(hours=2)
            with self.client() as client:
                self.select(client, "first")
                clock.now.return_value = start - timedelta(hours=1)
                runtime = self.runtime(client)
                self.assertEqual(runtime["total_mode_runtime_hours"], 0)
                self.assertEqual(runtime["tasks"][0]["total_runtime_hours"], 0)
                self.select(client, "first", False)
                clock.now.return_value = start
                self.mode(client, True)
                clock.now.return_value = start + timedelta(hours=1)
                self.select(client, "first")
                clock.now.return_value = start + timedelta(hours=2)
                self.select(client, "second")
                self.select(client, "first")
                clock.now.return_value = start + timedelta(hours=3)
                self.mode(client, True)
                clock.now.return_value = start + timedelta(hours=4)
                runtime = self.runtime(client)
                self.assertEqual(runtime["monthly_mode_runtime_hours"], 4)
                self.assertEqual(runtime["total_mode_runtime_hours"], 4)
                self.assertEqual(runtime["monthly_strategy_runtime_hours"], 3)
                self.assertEqual(
                    [task["monthly_runtime_hours"] for task in runtime["tasks"]], [3, 2]
                )
                for task in runtime["tasks"]:
                    self.assertEqual(set(task), {
                        "id", "name", "enabled", "effective_enabled",
                        "monthly_runtime_hours", "total_runtime_hours",
                    })
                    self.assertTrue(task["enabled"])
                    self.assertTrue(task["effective_enabled"])

                self.mode(client, False)
                clock.now.return_value = start + timedelta(hours=6)
                self.mode(client, False)
                runtime = self.runtime(client)
                self.assertEqual(runtime["monthly_mode_runtime_hours"], 4)
                self.assertEqual(runtime["monthly_strategy_runtime_hours"], 3)
                self.assertTrue(all(task["enabled"] for task in runtime["tasks"]))
                self.assertTrue(all(not task["effective_enabled"] for task in runtime["tasks"]))

    def test_monthly_and_lifetime_hours_cross_beijing_month_boundary(self):
        self.configure(task_ids=("first",), selected=("first",))
        start = datetime(2026, 10, 31, 23, tzinfo=BEIJING)
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = start
            with self.client() as client:
                self.mode(client, True)
                clock.now.return_value = start + timedelta(hours=3)
                runtime = self.runtime(client)
                self.assertEqual(runtime["month"], "2026-11")
                self.assertEqual(runtime["monthly_mode_runtime_hours"], 2)
                self.assertEqual(runtime["total_mode_runtime_hours"], 3)
                self.assertEqual(runtime["monthly_strategy_runtime_hours"], 2)
                self.assertEqual(runtime["tasks"][0]["monthly_runtime_hours"], 2)
                self.assertEqual(runtime["tasks"][0]["total_runtime_hours"], 3)
                self.mode(client, False)
                clock.now.return_value = datetime(2026, 12, 1, tzinfo=BEIJING)
                runtime = self.runtime(client)
                self.assertEqual(runtime["monthly_mode_runtime_hours"], 0)
                self.assertEqual(runtime["total_mode_runtime_hours"], 3)
                self.assertEqual(runtime["tasks"][0]["monthly_runtime_hours"], 0)
                self.assertEqual(runtime["tasks"][0]["total_runtime_hours"], 3)

    def test_checkpoint_recovers_durable_state_without_counting_crash_or_downtime(self):
        self.configure(task_ids=("first",))
        recovered = self.root / "recovered"
        recovered.mkdir()
        recovered_config = recovered / "tasks.json"
        recovered_config.write_bytes(self.config_path.read_bytes())
        (recovered / "first.py").write_bytes((self.root / "first.py").read_bytes())
        start = datetime(2026, 10, 1, tzinfo=BEIJING)
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = start
            with self.client() as client:
                self.mode(client, True)
                self.select(client, "first")
                clock.now.return_value = start + timedelta(hours=1)
                client.app.state.manager.checkpoint()
                # 保存最后一次检查点的数据库镜像，模拟随后异常退出丢失尚未落盘的一小段。
                with sqlite3.connect(self.root / "runtime.sqlite3") as source:
                    with sqlite3.connect(recovered / "runtime.sqlite3") as target:
                        source.backup(target)
                clock.now.return_value = start + timedelta(hours=1, seconds=5)
                self.assertGreater(client.app.state.manager.runtime()["total_mode_runtime_hours"], 1)
                self.assertEqual(self.runtime(client)["total_mode_runtime_hours"], 1.0)

            clock.now.return_value = start + timedelta(hours=4)
            with self.client(recovered_config) as client:
                runtime = self.runtime(client)
                self.assertTrue(runtime["enabled"])
                self.assertEqual(runtime["total_mode_runtime_hours"], 1)
                self.assertTrue(runtime["tasks"][0]["enabled"])
                self.assertTrue(runtime["tasks"][0]["effective_enabled"])
                self.assertEqual(runtime["tasks"][0]["total_runtime_hours"], 1)
                clock.now.return_value = start + timedelta(hours=4, minutes=30)
                runtime = self.runtime(client)
                self.assertEqual(runtime["total_mode_runtime_hours"], 1.5)
                self.assertEqual(runtime["tasks"][0]["total_runtime_hours"], 1.5)


if __name__ == "__main__":
    unittest.main()
