"""使用临时脚本验证接口和真实子进程的生命周期。"""

import json
import os
from pathlib import Path
import signal
import tempfile
import textwrap
import time
import unittest
from unittest.mock import call, patch

from fastapi.testclient import TestClient

from app import TaskManager, create_app


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def task(self, task_id, script, *, interval=0.15, enabled=False):
        script_path = self.root / f"{task_id}.py"
        script_path.write_text(textwrap.dedent(script), encoding="utf-8")
        return {
            "id": task_id,
            "name": task_id,
            "script": script_path.name,
            "interval_seconds": interval,
            "enabled": enabled,
        }

    def client(self, *tasks):
        config_path = self.root / "tasks.json"
        config_path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
        return TestClient(create_app(config_path))

    def wait_for(self, client, task_id, predicate, *, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = client.get(f"/tasks/{task_id}")
            self.assertEqual(response.status_code, 200, response.text)
            state = response.json()
            if predicate(state):
                return state
            time.sleep(0.025)
        self.fail(f"任务状态未在 {timeout} 秒内符合预期，最后状态：{state}")

    def wait_for_file(self, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists() and path.read_text(encoding="utf-8").strip():
                return path.read_text(encoding="utf-8").splitlines()
            time.sleep(0.025)
        self.fail(f"脚本未写入执行标记：{path}")

    def assert_process_gone(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.025)
        self.fail(f"进程仍然存在：{pid}")

    def test_disabled_by_default_and_unknown_task(self):
        task = self.task("demo", "print('demo')")
        with self.client(task) as client:
            response = client.get("/tasks")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()), 1)
            state = response.json()[0]
            self.assertFalse(state["enabled"])
            self.assertFalse(state["running"])
            self.assertEqual(state["status"], "disabled")
            self.assertEqual(client.get("/tasks?running=true").json(), [])
            for method, url in [
                ("get", "/tasks/missing"),
                ("post", "/tasks/missing/enable"),
                ("post", "/tasks/missing/disable"),
            ]:
                self.assertEqual(getattr(client, method)(url).status_code, 404)

    def test_enable_is_idempotent_no_overlap_and_disable_stops_process(self):
        task = self.task(
            "long_job",
            """
            import os
            from pathlib import Path
            import time
            with Path(__file__).with_suffix('.runs').open('a') as log:
                log.write(str(os.getpid()) + '\\n')
            print('long task started', flush=True)
            time.sleep(30)
            """,
        )
        marker = self.root / "long_job.runs"
        with self.client(task) as client:
            self.assertEqual(client.put("/ai-control-mode", json={"enabled": True}).status_code, 200)
            for _ in range(3):
                response = client.post("/tasks/long_job/enable")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["enabled"])

            self.wait_for_file(marker)
            running = self.wait_for(client, "long_job", lambda state: state["running"])
            pid = running["pid"]
            self.assertIsInstance(pid, int)
            self.assertEqual(running["status"], "running")
            self.assertEqual(
                [state["id"] for state in client.get("/tasks?running=true").json()],
                ["long_job"],
            )

            # 经过多个触发周期仍只有一个脚本实例。
            time.sleep(0.5)
            self.assertEqual(len(marker.read_text().splitlines()), 1)

            response = client.post("/tasks/long_job/disable")
            self.assertEqual(response.status_code, 200, response.text)
            stopped = response.json()
            self.assertFalse(stopped["enabled"])
            self.assertFalse(stopped["running"])
            self.assertEqual(stopped["status"], "disabled")
            self.assertEqual(stopped["last_result"], "stopped")
            self.assert_process_gone(pid)

            response = client.post("/tasks/long_job/disable")
            self.assertEqual(response.status_code, 200, response.text)
            time.sleep(0.35)
            self.assertEqual(len(marker.read_text().splitlines()), 1)
            self.assertEqual(client.get("/tasks?running=true").json(), [])

    def test_success_and_failed_exit_are_recorded(self):
        success = self.task("success", "print('calculated value: 42')", interval=0.5)
        failure = self.task(
            "failure", "import sys\nprint('failed calculation')\nsys.exit(7)", interval=0.5
        )
        with self.client(success, failure) as client:
            self.assertEqual(client.put("/ai-control-mode", json={"enabled": True}).status_code, 200)
            for task_id in ["success", "failure"]:
                self.assertEqual(client.post(f"/tasks/{task_id}/enable").status_code, 200)

            state = self.wait_for(
                client, "success", lambda state: state["last_result"] == "success"
            )
            self.assertEqual(state["last_exit_code"], 0)
            state = self.wait_for(
                client, "failure", lambda state: state["last_result"] == "failed"
            )
            self.assertEqual(state["last_exit_code"], 7)

    def test_service_shutdown_stops_running_script(self):
        task = self.task(
            "shutdown_job",
            """
            from pathlib import Path
            import os
            import time
            Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))
            time.sleep(30)
            """,
            enabled=True,
        )
        with self.client(task) as client:
            self.assertEqual(client.put("/ai-control-mode", json={"enabled": True}).status_code, 200)
            self.wait_for_file(self.root / "shutdown_job.pid")
            state = self.wait_for(client, "shutdown_job", lambda state: state["running"])
            pid = state["pid"]
        self.assert_process_gone(pid)

    @unittest.skipUnless(os.name == "posix", "SIGTERM/SIGKILL 测试适用于 macOS/Linux")
    def test_disable_kills_script_that_ignores_terminate(self):
        task = self.task(
            "ignore_term",
            """
            from pathlib import Path
            import os
            import signal
            import time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))
            time.sleep(30)
            """,
        )
        with self.client(task) as client:
            self.assertEqual(client.put("/ai-control-mode", json={"enabled": True}).status_code, 200)
            self.assertEqual(client.post("/tasks/ignore_term/enable").status_code, 200)
            self.wait_for_file(self.root / "ignore_term.pid")
            state = self.wait_for(client, "ignore_term", lambda state: state["running"])
            pid = state["pid"]
            response = client.post("/tasks/ignore_term/disable")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["running"])
            self.assertEqual(response.json()["last_result"], "stopped")
            self.assert_process_gone(pid)

    @unittest.skipUnless(os.name == "posix", "进程组测试适用于 macOS/Linux")
    def test_disable_kills_child_even_when_parent_exits_on_terminate(self):
        child_pid_file = self.root / "child.pid"
        child_code = textwrap.dedent(
            """
            from pathlib import Path
            import os
            import signal
            import sys
            import time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(sys.argv[1]).write_text(str(os.getpid()))
            time.sleep(30)
            """
        )
        parent_code = f"""
            from pathlib import Path
            import subprocess
            import sys
            import time
            subprocess.Popen([sys.executable, '-c', {child_code!r}, {str(child_pid_file)!r}])
            time.sleep(30)
            """
        task = self.task("parent", parent_code)
        with self.client(task) as client:
            self.assertEqual(client.put("/ai-control-mode", json={"enabled": True}).status_code, 200)
            self.assertEqual(client.post("/tasks/parent/enable").status_code, 200)
            child_pid = int(self.wait_for_file(child_pid_file)[0])
            try:
                parent = self.wait_for(client, "parent", lambda state: state["running"])
                response = client.post("/tasks/parent/disable")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["running"])
                self.assert_process_gone(parent["pid"])
                self.assert_process_gone(child_pid)
            finally:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_shutdown_continues_after_one_task_fails_to_stop(self):
        first = self.task("first", "print('first')")
        second = self.task("second", "print('second')")
        config_path = self.root / "tasks.json"
        config_path.write_text(json.dumps({"tasks": [first, second]}), encoding="utf-8")
        manager = TaskManager(config_path)
        manager.start()
        try:
            with (
                patch.object(manager, "_stop_task", side_effect=[RuntimeError("stop failed"), None]) as stop,
                patch.object(manager.scheduler, "pause") as pause,
                patch.object(manager.scheduler, "shutdown") as shutdown,
                self.assertLogs("app", level="ERROR"),
            ):
                manager.shutdown()
            self.assertTrue(manager.closing)
            self.assertEqual(stop.call_args_list, [call("first"), call("second")])
            pause.assert_called_once_with()
            shutdown.assert_called_once_with(wait=False)
        finally:
            if manager.scheduler.running:
                manager.scheduler.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
