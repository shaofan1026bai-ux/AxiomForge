"""验证真实数据主接口兼容最初版字段、时间轴和累计口径。"""

from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from load_forecast import make_load_forecast
from load_response import format_load_response


ZONE = ZoneInfo("Asia/Shanghai")


def samples():
    start = datetime(2026, 10, 30, tzinfo=ZONE)
    return {
        "date": "2026-10-30", "timezone": "Asia/Shanghai",
        "scope": "single_chiller", "source": "live_api", "interval_minutes": 2,
        "load_unit": "kW", "energy_unit": "kWh",
        "actual_energy_kwh": None, "predicted_energy_kwh": None,
        "time_semantics": "sample_time", "forecast_status": "ready",
        "forecast_metadata": {"fixed_snapshot": True},
        "points": [{"time": (start + timedelta(minutes=2 * i)).isoformat(),
                    "actual_load_kw": 300.0, "predicted_load_kw": 330.0}
                   for i in range(720)],
    }


class LoadResponseCompatibilityTests(unittest.TestCase):
    def test_exact_original_keys_and_720_interval_end_timestamps(self):
        at = datetime(2026, 10, 30, 12, tzinfo=ZONE)
        original = make_load_forecast(at)
        result = format_load_response(samples(), at)
        self.assertEqual(list(result), list(original))
        self.assertEqual(len(result["points"]), 720)
        self.assertEqual([p["time"] for p in result["points"]],
                         [p["time"] for p in original["points"]])
        self.assertEqual(result["points"][0]["time"], "2026-10-30T00:02:00+08:00")
        self.assertEqual(result["points"][-1]["time"], "2026-10-31T00:00:00+08:00")
        self.assertTrue(all(list(p) == list(original["points"][0]) for p in result["points"]))
        self.assertEqual(result["source"], "live_api")
        self.assertEqual(result["scope"], "single_chiller")

    def test_only_completed_intervals_have_actual_and_contribute_energy(self):
        at = datetime(2026, 10, 30, 0, 5, tzinfo=ZONE)
        result = format_load_response(samples(), at)
        self.assertEqual([p["actual_load_kw"] for p in result["points"][:3]], [300, 300, None])
        self.assertEqual(result["actual_energy_kwh"], 20)
        self.assertEqual(result["predicted_energy_kwh"], 22)
        self.assertEqual(result["points"][-1]["predicted_load_kw"], 330)

    def test_midnight_and_first_interval_boundary(self):
        at = datetime(2026, 10, 30, 0, 1, 59, tzinfo=ZONE)
        result = format_load_response(samples(), at)
        self.assertTrue(all(p["actual_load_kw"] is None for p in result["points"]))
        self.assertEqual(result["actual_energy_kwh"], 0)
        self.assertEqual(result["predicted_energy_kwh"], 0)
        result = format_load_response(samples(), at.replace(minute=2, second=0))
        self.assertEqual(result["points"][0]["actual_load_kw"], 300)
        self.assertIsNone(result["points"][1]["actual_load_kw"])

    def test_empty_window_is_zero_even_when_forecast_missing(self):
        source = samples()
        for point in source["points"]:
            point["predicted_load_kw"] = None
        result = format_load_response(source, datetime(2026, 10, 30, 0, 1, tzinfo=ZONE))
        self.assertEqual(result["actual_energy_kwh"], 0)
        self.assertEqual(result["predicted_energy_kwh"], 0)

    def test_utc_clock_and_future_missing_samples(self):
        from datetime import timezone
        source = samples()
        for point in source["points"][2:]:
            point["actual_load_kw"] = None
        result = format_load_response(source, datetime(2026, 10, 29, 16, 4, tzinfo=timezone.utc))
        self.assertEqual(result["date"], "2026-10-30")
        self.assertEqual(result["actual_energy_kwh"], 20)
        self.assertEqual(result["predicted_energy_kwh"], 22)

    def test_missing_completed_actual_does_not_become_zero_total(self):
        source = samples()
        source["points"][0]["actual_load_kw"] = None
        result = format_load_response(source, datetime(2026, 10, 30, 0, 4, tzinfo=ZONE), details=True)
        self.assertIsNone(result["actual_energy_kwh"])
        self.assertEqual(result["predicted_energy_kwh"], 22)
        self.assertIsNone(result["points"][0]["actual_load_kw"])
        self.assertEqual(result["energy_coverage"]["actual_valid_interval_count"], 1)

    def test_missing_prediction_keeps_curve_and_total_unavailable(self):
        source = samples()
        for point in source["points"]:
            point["predicted_load_kw"] = None
        result = format_load_response(source, datetime(2026, 10, 30, 12, tzinfo=ZONE))
        self.assertIsNone(result["predicted_energy_kwh"])
        self.assertTrue(all(p["predicted_load_kw"] is None for p in result["points"]))

    def test_past_full_day_energy_and_zero_are_valid(self):
        at = datetime(2026, 10, 31, 12, tzinfo=ZONE)
        result = format_load_response(samples(), at)
        self.assertEqual(result["actual_energy_kwh"], 7200)
        self.assertEqual(result["predicted_energy_kwh"], 7920)
        source = samples()
        for point in source["points"]:
            point["actual_load_kw"] = 0
        result = format_load_response(source, at)
        self.assertEqual(result["actual_energy_kwh"], 0)
        self.assertTrue(all(p["actual_load_kw"] == 0 for p in result["points"]))

    def test_details_are_separate_and_source_is_unchanged(self):
        source = samples()
        before = deepcopy(source)
        at = datetime(2026, 10, 30, 12, tzinfo=ZONE)
        primary = format_load_response(source, at)
        detailed = format_load_response(source, at, details=True)
        self.assertNotIn("forecast_metadata", primary)
        self.assertEqual(detailed["forecast_metadata"], {"fixed_snapshot": True})
        self.assertEqual(detailed["time_semantics"], "interval_end")
        self.assertEqual(detailed["source_time_semantics"], "sample_time")
        self.assertEqual(detailed["energy_method"], "left_sample_hold_estimate")
        self.assertEqual(primary["points"], detailed["points"])
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
