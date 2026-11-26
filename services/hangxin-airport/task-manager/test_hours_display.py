"""通过接口验证小时展示精度，不改变实际累计值。"""

from datetime import datetime, timedelta
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


class HoursDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "demo.py").write_text("print('calculated')\n", encoding="utf-8")
        self.config_path = self.root / "tasks.json"
        self.config_path.write_text(json.dumps({
            "ai_mode_enabled": True,
            "tasks": [{
                "id": "demo", "name": "演示策略", "script": "demo.py",
                "interval_seconds": 3600, "enabled": True,
            }],
        }), encoding="utf-8")
        self.start = datetime(2026, 10, 1, tzinfo=BEIJING)

    def tearDown(self):
        self.temp_dir.cleanup()

    def assert_api_hours(self, client, expected):
        response = client.get("/ai-control-mode/runtime")
        self.assertEqual(response.status_code, 200, response.text)
        runtime = response.json()
        values = [
            runtime["monthly_mode_runtime_hours"],
            runtime["total_mode_runtime_hours"],
            runtime["monthly_strategy_runtime_hours"],
            runtime["tasks"][0]["monthly_runtime_hours"],
            runtime["tasks"][0]["total_runtime_hours"],
        ]
        response = client.get("/tasks/overview")
        self.assertEqual(response.status_code, 200, response.text)
        values.append(response.json()["items"][0]["monthly_runtime_hours"])
        for value in values:
            self.assertIsInstance(value, float)
            self.assertEqual(value, expected)

    def test_display_precision_does_not_round_checkpoints_or_later_accumulation(self):
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.start
            with TestClient(create_app(self.config_path)) as client:
                clock.now.return_value = self.start + timedelta(hours=0.26)
                self.assert_api_hours(client, 0.3)
                manager = client.app.state.manager
                manager.checkpoint()
                # 数据库保存实际秒数，显示为 0.3 小时不会将记录变成 1080 秒。
                with sqlite3.connect(self.root / "runtime.sqlite3") as connection:
                    task_seconds = connection.execute(
                        "SELECT seconds FROM monthly_runtime WHERE task_id = ? AND month = ?",
                        ("demo", "2026-10"),
                    ).fetchone()[0]
                    mode_seconds = connection.execute(
                        "SELECT seconds FROM monthly_mode_runtime WHERE month = ?", ("2026-10",)
                    ).fetchone()[0]
                self.assertEqual(task_seconds, 936)
                self.assertEqual(mode_seconds, 936)
                self.assertEqual(manager.runtime()["monthly_mode_runtime_hours"], 0.26)
                self.assertEqual(manager.runtime()["tasks"][0]["monthly_runtime_hours"], 0.26)

                clock.now.return_value = self.start + timedelta(hours=1.02)
                self.assertEqual(manager.runtime()["monthly_mode_runtime_hours"], 1.02)
                self.assertEqual(manager.runtime()["tasks"][0]["monthly_runtime_hours"], 1.02)
                self.assert_api_hours(client, 1.0)

    def test_half_up_boundaries_and_all_hours_have_one_decimal(self):
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.start
            with TestClient(create_app(self.config_path)) as client:
                for actual, displayed in [
                    (0.05, 0.1), (0.093, 0.1), (0.95, 1.0),
                    (1.05, 1.1), (1.2345, 1.2), (3.018, 3.0),
                ]:
                    with self.subTest(actual_hours=actual):
                        clock.now.return_value = self.start + timedelta(hours=actual)
                        self.assert_api_hours(client, displayed)

    def test_overview_keeps_null_when_stats_cannot_be_read(self):
        with patch("app.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.start
            with TestClient(create_app(self.config_path)) as client:
                with (
                    patch.object(RuntimeStats, "hours", side_effect=sqlite3.OperationalError("unavailable")),
                    self.assertLogs("app", level="ERROR"),
                ):
                    response = client.get("/tasks/overview")
                self.assertEqual(response.status_code, 200, response.text)
                item = response.json()["items"][0]
                self.assertTrue(item["enabled"])
                self.assertIsNone(item["monthly_runtime_hours"])


if __name__ == "__main__":
    unittest.main()
