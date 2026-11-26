"""验证两分钟模拟负荷的时间范围、对齐和累计能量。"""

from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from load_forecast import make_load_forecast


BEIJING = ZoneInfo("Asia/Shanghai")


class LoadForecastTests(unittest.TestCase):
    def test_today_has_full_forecast_and_actual_only_for_completed_intervals(self):
        at = datetime(2026, 10, 30, 9, 45, 18, tzinfo=BEIJING)
        result = make_load_forecast(at)
        self.assertEqual(result["date"], "2026-10-30")
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(result["scope"], "all_chillers")
        self.assertEqual(result["source"], "mock")
        self.assertEqual(result["interval_minutes"], 2)
        self.assertEqual(result["load_unit"], "kW")
        self.assertEqual(result["energy_unit"], "kWh")
        points = result["points"]
        self.assertEqual(len(points), 720)
        self.assertEqual(points[0]["time"], "2026-10-30T00:02:00+08:00")
        self.assertEqual(points[-1]["time"], "2026-10-31T00:00:00+08:00")
        self.assertEqual(sum(point["actual_load_kw"] is not None for point in points), 292)
        previous = datetime(2026, 10, 30, tzinfo=BEIJING)
        for point in points:
            timestamp = datetime.fromisoformat(point["time"])
            self.assertEqual(timestamp - previous, timedelta(minutes=2))
            self.assertGreater(point["predicted_load_kw"], 0)
            if timestamp <= at:
                self.assertGreater(point["actual_load_kw"], 0)
                self.assertEqual(point["predicted_load_kw"], point["actual_load_kw"])
            else:
                self.assertIsNone(point["actual_load_kw"])
            previous = timestamp

    def test_energy_uses_completed_two_minute_intervals(self):
        result = make_load_forecast(datetime(2026, 10, 30, 10, 1, tzinfo=BEIJING))
        expected = round(math.fsum(
            p["actual_load_kw"] * 2 / 60
            for p in result["points"] if p["actual_load_kw"] is not None
        ), 4)
        self.assertEqual(result["actual_energy_kwh"], expected)
        self.assertEqual(result["predicted_energy_kwh"], expected)
        full_forecast_energy = math.fsum(p["predicted_load_kw"] * 2 / 60 for p in result["points"])
        self.assertLess(result["predicted_energy_kwh"], full_forecast_energy)

    def test_refresh_preserves_all_previous_values(self):
        early_at = datetime(2026, 10, 30, 9, 45, tzinfo=BEIJING)
        early = make_load_forecast(early_at)
        later = make_load_forecast(early_at + timedelta(minutes=20))
        self.assertEqual(early, make_load_forecast(early_at))
        self.assertEqual(
            [p["predicted_load_kw"] for p in early["points"]],
            [p["predicted_load_kw"] for p in later["points"]],
        )
        for earlier_point, later_point in zip(early["points"], later["points"]):
            self.assertEqual(earlier_point["time"], later_point["time"])
            if earlier_point["actual_load_kw"] is not None:
                self.assertEqual(earlier_point["actual_load_kw"], later_point["actual_load_kw"])
        self.assertIsNone(early["points"][292]["actual_load_kw"])
        self.assertEqual(later["points"][292]["actual_load_kw"], early["points"][292]["predicted_load_kw"])

    def test_past_day_returns_full_720_intervals(self):
        result = make_load_forecast(
            datetime(2026, 10, 30, 10, tzinfo=BEIJING), date(2026, 10, 29)
        )
        self.assertEqual(len(result["points"]), 720)
        self.assertEqual(result["date"], "2026-10-29")
        self.assertEqual(result["points"][0]["time"], "2026-10-29T00:02:00+08:00")
        self.assertEqual(result["points"][-1]["time"], "2026-10-30T00:00:00+08:00")
        self.assertTrue(all(p["actual_load_kw"] is not None for p in result["points"]))
        later = make_load_forecast(
            datetime(2026, 10, 30, 20, tzinfo=BEIJING), date(2026, 10, 29)
        )
        self.assertEqual(result, later)

    def test_midnight_has_no_completed_intervals(self):
        for minute, second in [(0, 0), (1, 59)]:
            with self.subTest(minute=minute, second=second):
                result = make_load_forecast(
                    datetime(2026, 10, 30, 0, minute, second, tzinfo=BEIJING)
                )
                self.assertEqual(len(result["points"]), 720)
                self.assertTrue(all(p["actual_load_kw"] is None for p in result["points"]))
                self.assertTrue(all(p["predicted_load_kw"] > 0 for p in result["points"]))
                self.assertEqual(result["actual_energy_kwh"], 0)
                self.assertEqual(result["predicted_energy_kwh"], 0)
        boundary = make_load_forecast(datetime(2026, 10, 30, 0, 2, tzinfo=BEIJING))
        self.assertEqual(len(boundary["points"]), 720)
        self.assertIsNotNone(boundary["points"][0]["actual_load_kw"])
        self.assertTrue(all(p["actual_load_kw"] is None for p in boundary["points"][1:]))

    def test_utc_input_uses_beijing_day_and_offset(self):
        result = make_load_forecast(datetime(2026, 10, 29, 16, 4, tzinfo=timezone.utc))
        self.assertEqual(result["date"], "2026-10-30")
        self.assertEqual(len(result["points"]), 720)
        self.assertEqual(sum(p["actual_load_kw"] is not None for p in result["points"]), 2)
        self.assertTrue(all(p["time"].endswith("+08:00") for p in result["points"]))

    def test_rejects_future_day_and_naive_time(self):
        with self.assertRaises(ValueError):
            make_load_forecast(
                datetime(2026, 10, 30, 10, tzinfo=BEIJING), date(2026, 10, 31)
            )
        with self.assertRaises(ValueError):
            make_load_forecast(datetime(2026, 10, 30, 10))

    def test_api_response_accepts_future_actual_nulls(self):
        from fastapi.testclient import TestClient
        from app import create_app

        at = datetime(2026, 10, 30, 9, 45, tzinfo=BEIJING)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
            with patch("app.datetime", wraps=datetime) as clock:
                clock.now.return_value = at
                with TestClient(create_app(path)) as client:
                    response = client.get("/load-forecast")
                    self.assertEqual(response.status_code, 200, response.text)
                    points = response.json()["points"]
                    self.assertEqual(len(points), 720)
                    self.assertIsNotNone(points[291]["actual_load_kw"])
                    self.assertIsNone(points[292]["actual_load_kw"])
                    self.assertIsNotNone(points[-1]["predicted_load_kw"])


if __name__ == "__main__":
    unittest.main()
