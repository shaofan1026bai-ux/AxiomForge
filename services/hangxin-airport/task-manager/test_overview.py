"""验证任务汇总接口及按北京时间月份累计的开启时长。"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import create_app
from runtime_stats import RuntimeStats


BEIJING = ZoneInfo("Asia/Shanghai")


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "tasks.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def configure(self, task_ids):
        script = self.root / "calculate.py"
        script.write_text("print('calculated')\n", encoding="utf-8")
        tasks = [
            {
                "id": task_id,
                "name": f"任务 {task_id}",
                "script": script.name,
                "interval_seconds": 3600,
                "enabled": False,
            }
            for task_id in task_ids
        ]
        self.config_path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")

    def client(self):
        return TestClient(create_app(self.config_path))

    def overview(self, client, suffix=""):
        response = client.get(f"/tasks/overview{suffix}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_overview_route_default_limit_total_and_exact_fields(self):
        self.configure(["zeta", "alpha", "beta", "delta", "epsilon"])
        at = datetime(2026, 10, 29, 12, tzinfo=BEIJING)
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = at
            with self.client() as client:
                # 固定路径必须先于 /tasks/{task_id} 匹配。
                result = self.overview(client)
                self.assertEqual(set(result), {"total", "month", "items"})
                self.assertEqual(result["total"], 5)
                self.assertEqual(result["month"], "2026-10")
                self.assertEqual([item["id"] for item in result["items"]], ["zeta", "alpha", "beta"])
                for item in result["items"]:
                    self.assertEqual(
                        set(item), {"id", "name", "enabled", "monthly_runtime_hours"}
                    )
                    self.assertEqual(item["name"], f"任务 {item['id']}")
                    self.assertIs(item["enabled"], False)
                    self.assertEqual(item["monthly_runtime_hours"], 0)

                result = self.overview(client, "?limit=1")
                self.assertEqual(result["total"], 5)
                self.assertEqual(len(result["items"]), 1)
                result = self.overview(client, "?limit=100")
                self.assertEqual(result["total"], 5)
                self.assertEqual(len(result["items"]), 5)
                for invalid in ["0", "101", "text"]:
                    self.assertEqual(client.get(f"/tasks/overview?limit={invalid}").status_code, 422)

    def test_overview_with_no_tasks(self):
        self.configure([])
        at = datetime(2026, 11, 1, tzinfo=BEIJING)
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = at
            with self.client() as client:
                self.assertEqual(
                    self.overview(client), {"total": 0, "month": "2026-11", "items": []}
                )

    def test_stats_read_failure_returns_null_hours(self):
        self.configure(["demo"])
        with self.client() as client:
            with (
                patch.object(RuntimeStats, "hours", side_effect=sqlite3.OperationalError("read failed")),
                self.assertLogs("app", level="ERROR"),
            ):
                result = self.overview(client)
            self.assertEqual(result["total"], 1)
            self.assertIs(result["items"][0]["enabled"], False)
            self.assertIsNone(result["items"][0]["monthly_runtime_hours"])

    def test_enabled_is_independent_of_running_and_toggles_are_idempotent(self):
        self.configure(["demo"])
        start = datetime(2026, 10, 1, 8, tzinfo=BEIJING)
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = start
            with self.client() as client:
                self.assertEqual(client.put("/ai-control-mode", json={"enabled": True}).status_code, 200)
                self.assertEqual(client.post("/tasks/demo/enable").status_code, 200)
                detail = client.get("/tasks/demo").json()
                self.assertTrue(detail["enabled"])
                self.assertFalse(detail["running"])
                item = self.overview(client)["items"][0]
                self.assertTrue(item["enabled"])
                self.assertEqual(item["monthly_runtime_hours"], 0)

                clock.now.return_value = start + timedelta(minutes=30)
                self.assertEqual(client.post("/tasks/demo/enable").status_code, 200)
                self.assertEqual(self.overview(client)["items"][0]["monthly_runtime_hours"], 0.5)

                clock.now.return_value = start + timedelta(hours=1)
                self.assertEqual(client.post("/tasks/demo/disable").status_code, 200)
                item = self.overview(client)["items"][0]
                self.assertFalse(item["enabled"])
                self.assertEqual(item["monthly_runtime_hours"], 1)

                clock.now.return_value = start + timedelta(hours=2)
                self.assertEqual(client.post("/tasks/demo/disable").status_code, 200)
                self.assertEqual(self.overview(client)["items"][0]["monthly_runtime_hours"], 1)

    def test_disable_and_shutdown_persist_hours_without_counting_downtime(self):
        self.configure(["demo"])
        start = datetime(2026, 10, 1, 8, tzinfo=BEIJING)
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = start
            with self.client() as client:
                self.assertEqual(client.put("/ai-control-mode", json={"enabled": True}).status_code, 200)
                self.assertEqual(client.post("/tasks/demo/enable").status_code, 200)
                clock.now.return_value = start + timedelta(hours=1)
                self.assertEqual(client.post("/tasks/demo/disable").status_code, 200)
                clock.now.return_value = start + timedelta(hours=2)
                self.assertEqual(client.post("/tasks/demo/enable").status_code, 200)
                clock.now.return_value = start + timedelta(hours=3)
                # 退出上下文触发正常 shutdown，保存第二段开启时长。

            clock.now.return_value = start + timedelta(hours=6)
            with self.client() as client:
                item = self.overview(client)["items"][0]
                self.assertTrue(item["enabled"])
                self.assertEqual(item["monthly_runtime_hours"], 2)
                clock.now.return_value = start + timedelta(hours=7)
                self.assertEqual(client.post("/tasks/demo/enable").status_code, 200)
                clock.now.return_value = start + timedelta(hours=7, minutes=30)
                self.assertEqual(self.overview(client)["items"][0]["monthly_runtime_hours"], 3.5)


class RuntimeStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "runtime.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_record_splits_at_beijing_month_boundary_and_persists(self):
        stats = RuntimeStats(self.path)
        # UTC 15:30 至 17:30 对应北京时间 10月31日23:30 至 11月1日01:30。
        stats.record(
            "demo",
            datetime(2026, 10, 31, 15, 30, tzinfo=timezone.utc),
            datetime(2026, 10, 31, 17, 30, tzinfo=timezone.utc),
        )
        reopened = RuntimeStats(self.path)
        self.assertEqual(
            reopened.hours("demo", datetime(2026, 10, 31, 23, 59, tzinfo=BEIJING)), 0.5
        )
        self.assertEqual(
            reopened.hours("demo", datetime(2026, 11, 2, tzinfo=BEIJING)), 1.5
        )
        self.assertEqual(reopened.hours("other", datetime(2026, 11, 2, tzinfo=BEIJING)), 0)

    def test_active_segment_is_clipped_to_current_month_and_added_once(self):
        stats = RuntimeStats(self.path)
        stats.record(
            "demo",
            datetime(2026, 11, 1, 0, 10, tzinfo=BEIJING),
            datetime(2026, 11, 1, 0, 25, tzinfo=BEIJING),
        )
        at = datetime(2026, 11, 1, 3, tzinfo=BEIJING)
        self.assertEqual(
            stats.hours(
                "cross_month", at,
                active_since=datetime(2026, 10, 31, 23, 30, tzinfo=BEIJING),
            ),
            3,
        )
        active_since = datetime(2026, 11, 1, 1, tzinfo=BEIJING)
        for _ in range(2):
            self.assertEqual(stats.hours("demo", at, active_since=active_since), 2.25)
        self.assertEqual(stats.hours("demo", at), 0.25)
        self.assertEqual(
            stats.hours("rounding", at + timedelta(seconds=13), active_since=at), 0.0036
        )


if __name__ == "__main__":
    unittest.main()
