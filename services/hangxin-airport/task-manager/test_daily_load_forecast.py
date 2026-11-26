"""固定预测快照、平台数据边界和实际数据独立刷新的回归测试。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from zoneinfo import ZoneInfo

from daily_load_forecast import DailyLoadForecast, ForecastCacheError, HistoryValidationError


SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 10, 30)
AT = datetime(2026, 10, 30, 0, 5, tzinfo=SHANGHAI)
CONFIG = {
    "device_id": 12788, "device_name": "1#空调冷水机组",
    "rated_cooling_capacity_kw": 834.4, "interval_minutes": 2,
    "smoothing_alpha": 0.85, "adjustment_factor": 0.985,
    "heads": [
        {"identifier_name": "c09", "point_id": "nfdh9q_mscrk01_c09", "rated_cooling_capacity_kw": 417.2},
        {"identifier_name": "c10", "point_id": "nfdh9q_mscrk01_c10", "rated_cooling_capacity_kw": 417.2},
    ],
}


def payload(day, first=99.0, second=100.0):
    start = datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI)
    return {"success": True, "code": 200, "data": [
        {"deviceId": 12788, "identifierName": head["identifier_name"],
         "pointId": head["point_id"], "unit": "%", "pointDataList": [
             {"ts": (start + timedelta(minutes=index * 2)).strftime("%Y-%m-%d %H:%M:%S"), "val": value}
             for index in range(720)]}
        for head, value in zip(CONFIG["heads"], [first, second])
    ]}


class DailyLoadForecastTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)
        self.calls = []
        self.returned = {}

        def fetch(start, end):
            self.calls.append((start, end))
            return deepcopy(self.returned.get(start.date(), payload(start.date())))

        # 原有严格校验场景继续验证可选的零插值、单日策略。
        self.service = DailyLoadForecast({**CONFIG,'reference_lookback_days':1,'maximum_interpolation_gap_minutes':0}, self.cache, fetch)

    def test_generate_queries_only_yesterday_and_uses_sample_times(self):
        result = self.service.generate(DAY, AT)
        self.assertEqual(self.calls, [(datetime(2026, 10, 29, tzinfo=SHANGHAI),
                                      datetime(2026, 10, 30, tzinfo=SHANGHAI))])
        self.assertEqual(result["reference_date"], "2026-10-29")
        self.assertEqual(result["generated_at"], AT.isoformat())
        self.assertEqual(result["source"], "previous_day_baseline")
        self.assertEqual(result["time_semantics"], "sample_time")
        self.assertEqual(result["scope"], "single_chiller")
        self.assertFalse(result["model_trained"])
        self.assertEqual(len(result["points"]), 720)
        self.assertEqual(result["points"][0]["time"], "2026-10-30T00:00:00+08:00")
        self.assertEqual(result["points"][-1]["time"], "2026-10-30T23:58:00+08:00")
        self.assertEqual(result["points"][0]["predicted_load_kw"], 817.77458)

    def test_smoothing_is_causal_in_reference_day(self):
        source = payload(DAY - timedelta(days=1), 0, 0)
        source["data"][0]["pointDataList"][1]["val"] = 100
        self.returned[DAY - timedelta(days=1)] = source
        result = self.service.generate(DAY, AT)
        values = [p["predicted_load_kw"] for p in result["points"][:3]]
        self.assertEqual(values, [0.0, round(417.2 * 0.85 * 0.985, 6),
                                  round(417.2 * 0.85 * 0.15 * 0.985, 6)])

    def test_utc_and_late_run_preserve_reference_day_and_real_issue_time(self):
        issued = datetime(2026, 10, 29, 16, 5, tzinfo=timezone.utc)
        result = self.service.generate(DAY, issued)
        self.assertEqual(result["generated_at"], AT.isoformat())
        next_day = DAY + timedelta(days=1)
        late = datetime(2026, 10, 31, 18, tzinfo=SHANGHAI)
        result = self.service.generate(next_day, late)
        self.assertEqual(result["reference_date"], DAY.isoformat())
        self.assertEqual(result["generated_at"], late.isoformat())

    def test_successful_snapshot_is_idempotent_without_fetch(self):
        first = self.service.generate(DAY, AT)
        original_bytes = (self.cache / f"{DAY}.json").read_bytes()
        self.returned[DAY - timedelta(days=1)] = payload(DAY - timedelta(days=1), 0, 0)
        second = self.service.generate(DAY, AT + timedelta(hours=8))
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(original_bytes, (self.cache / f"{DAY}.json").read_bytes())

    def test_bad_reference_rejects_generation_without_success_file(self):
        previous = DAY - timedelta(days=1)

        def change_value(value):
            return lambda source: source["data"][0]["pointDataList"][0].update(val=value)

        mutations = {
            "null": change_value(None), "nan": change_value(float("nan")),
            "infinity": change_value(float("inf")), "negative": change_value(-1),
            "over100": change_value(101), "bool": change_value(True), "string": change_value("99"),
            "missing": lambda source: source["data"][0]["pointDataList"].pop(),
            "duplicate": lambda source: source["data"][0]["pointDataList"].append(deepcopy(source["data"][0]["pointDataList"][0])),
            "wrong_device": lambda source: source["data"][0].update(deviceId=1),
            "wrong_point": lambda source: source["data"][0].update(pointId="wrong"),
            "wrong_unit": lambda source: source["data"][0].update(unit="kW"),
            "wrong_day": lambda source: source["data"][0]["pointDataList"][0].update(ts="2026-10-30 00:00:00"),
            "off_grid": lambda source: source["data"][0]["pointDataList"][0].update(ts="2026-10-29 00:01:00"),
            "failed_api": lambda source: source.update(success=False),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                source = payload(previous)
                mutate(source)
                self.returned[previous] = source
                with self.assertRaises(HistoryValidationError):
                    self.service.generate(DAY, AT)
                self.assertFalse((self.cache / f"{DAY}.json").exists())

    def test_both_heads_missing_same_sample_are_rejected(self):
        previous = DAY - timedelta(days=1)
        source = payload(previous)
        for series in source["data"]:
            series["pointDataList"].pop(12)
        self.returned[previous] = source
        with self.assertRaises(HistoryValidationError):
            self.service.generate(DAY, AT)

    def test_corrupt_or_wrong_date_cache_is_not_silently_replaced(self):
        path = self.cache / f"{DAY}.json"
        path.write_text("{broken", encoding="utf-8")
        for operation in [lambda: self.service.read_forecast(DAY), lambda: self.service.generate(DAY, AT)]:
            with self.assertRaises(ForecastCacheError):
                operation()
        self.assertFalse(self.calls)
        path.unlink()
        result = self.service.generate(DAY, AT)
        result["date"] = "2026-10-28"
        path.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(ForecastCacheError):
            self.service.read_forecast(DAY)

    def test_incomplete_cached_parameters_are_explicit_cache_errors(self):
        result = self.service.generate(DAY, AT)
        del result["parameters"]
        (self.cache / f"{DAY}.json").write_text(json.dumps(result), encoding="utf-8")
        self.calls.clear()
        with self.assertRaises(ForecastCacheError):
            self.service.response(AT)
        self.assertFalse(self.calls)

    def test_missing_cache_does_not_generate_and_only_fetches_current_day(self):
        source = payload(DAY, 0, 0)
        source["data"][0]["pointDataList"][1]["val"] = None
        self.returned[DAY] = source
        result = self.service.response(AT)
        self.assertEqual(result["forecast_status"], "missing")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0].date(), DAY)
        self.assertTrue(all(point["predicted_load_kw"] is None for point in result["points"]))
        self.assertEqual(result["points"][0]["actual_load_kw"], 0)
        self.assertIsNone(result["points"][1]["actual_load_kw"])
        self.assertEqual(result["points"][2]["actual_load_kw"], 0)
        self.assertIsNone(result["points"][3]["actual_load_kw"])
        self.assertIsNone(result["actual_energy_kwh"])
        self.assertIsNone(result["predicted_energy_kwh"])
        self.assertEqual(result["energy_method"], "not_computed_from_point_samples")
        self.assertFalse((self.cache / f"{DAY}.json").exists())

    def test_refresh_changes_only_actuals_not_fixed_prediction(self):
        snapshot = self.service.generate(DAY, AT)
        self.calls.clear()
        first = self.service.response(AT)
        self.returned[DAY] = payload(DAY, 20, 30)
        second = self.service.response(AT + timedelta(minutes=2))
        expected = [point["predicted_load_kw"] for point in snapshot["points"]]
        self.assertEqual([point["predicted_load_kw"] for point in first["points"]], expected)
        self.assertEqual([point["predicted_load_kw"] for point in second["points"]], expected)
        self.assertNotEqual(first["points"][0]["actual_load_kw"], second["points"][0]["actual_load_kw"])
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(start.date() == DAY for start, _ in self.calls))

    def test_actual_invalid_samples_become_null_and_are_flagged(self):
        source = payload(DAY)
        source["data"][0]["pointDataList"][0]["val"] = 101
        source["data"][1]["pointDataList"].pop(1)
        self.returned[DAY] = source
        result = self.service.response(AT)
        self.assertIsNone(result["points"][0]["actual_load_kw"])
        self.assertIsNone(result["points"][1]["actual_load_kw"])
        self.assertEqual(result["actual_quality"]["quality_flag_counts"]["out_of_range:c09"], 1)
        self.assertEqual(result["actual_quality"]["quality_flag_counts"]["missing_timestamp:c10"], 1)

    def test_response_uses_shanghai_date_at_utc_midnight_boundary(self):
        result = self.service.response(datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(result["date"], DAY.isoformat())
        self.assertEqual(result["points"][0]["actual_load_kw"], 830.228)
        self.assertTrue(all(point["actual_load_kw"] is None for point in result["points"][1:]))
        self.assertEqual(self.calls[0][0].date(), DAY)

    def test_rejects_naive_time_and_future_reference(self):
        with self.assertRaises(ValueError):
            self.service.generate(DAY, datetime(2026, 10, 30))
        with self.assertRaises(ValueError):
            self.service.generate(DAY + timedelta(days=1), AT)
        with self.assertRaises(ValueError):
            self.service.response(AT, DAY + timedelta(days=1))
        self.assertFalse(self.calls)

    def test_concurrent_generators_fetch_once_and_return_same_atomic_snapshot(self):
        calls = []
        guard = threading.Lock()

        def fetch(start, end):
            with guard:
                calls.append((start, end))
            time.sleep(0.04)
            return payload(start.date())

        services = [DailyLoadForecast(CONFIG, self.cache, fetch) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(service.generate, DAY, AT + timedelta(minutes=index))
                       for index, service in enumerate(services)]
            results = [future.result() for future in futures]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.service.read_forecast(DAY), results[0])
        self.assertFalse(list(self.cache.glob("*.tmp")))

    def test_configuration_capacity_and_sampling_grid_are_validated(self):
        for updates in [{"rated_cooling_capacity_kw": 900}, {"interval_minutes": 7},
                        {"smoothing_alpha": 0}, {"adjustment_factor": float("nan")}]:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                DailyLoadForecast({**CONFIG, **updates}, self.cache, lambda start, end: {})


class ForecastToleranceTests(unittest.TestCase):
    """验证生产默认的有限修复、最近日回退、旧缓存兼容及实际数据隔离。"""
    def setUp(self):
        DailyLoadForecastTests.setUp(self)
        self.service=DailyLoadForecast(CONFIG,self.cache,self.service.fetch_history)

    def missing(self,day,indices):
        source=payload(day)
        for series in source['data']:
            for index in indices:series['pointDataList'][index]['val']=None
        self.returned[day]=source

    def test_short_internal_gap_is_repaired_and_explicitly_marked(self):
        self.missing(DAY-timedelta(days=1),[10,11])
        result=self.service.generate(DAY,AT)
        self.assertFalse(result['fallback_used']);self.assertEqual(result['reference_quality']['interpolated_sample_count'],2)
        self.assertEqual(result['reference_quality']['observed_sample_count'],718)
        self.assertTrue(all(p['predicted_load_kw']>0 for p in result['points']))

    def test_large_missing_reference_falls_back_to_nearest_complete_day(self):
        self.missing(DAY-timedelta(days=1),range(141))
        result=self.service.generate(DAY,AT)
        self.assertTrue(result['fallback_used']);self.assertEqual(result['source'],'historical_day_baseline')
        self.assertEqual(result['reference_date'],(DAY-timedelta(days=2)).isoformat())
        self.assertEqual(len(result['points']),720);self.assertEqual(len(self.calls),2)
        self.assertEqual(result['reference_attempts'][0]['quality']['valid_sample_count'],579)
        self.assertEqual(self.service.read_forecast(DAY),result)

    def test_single_long_gap_is_not_repaired_even_with_high_coverage(self):
        self.missing(DAY-timedelta(days=1),range(10,16))
        result=self.service.generate(DAY,AT)
        self.assertTrue(result['fallback_used']);self.assertIn('连续缺口',result['reference_attempts'][0]['reason'])

    def test_edge_gap_is_not_extrapolated(self):
        for n,index in enumerate((0,719)):
            with self.subTest(index=index):
                self.cache=Path(self.temporary.name)/f'edge-{n}'
                self.service=DailyLoadForecast(CONFIG,self.cache,self.service.fetch_history)
                self.missing(DAY-timedelta(days=1),[index])
                result=self.service.generate(DAY,AT);self.assertTrue(result['fallback_used'])
                self.assertIn('边缘缺测',result['reference_attempts'][0]['reason'])

    def test_coverage_threshold_is_inclusive_but_not_relaxed(self):
        for count,expected_fallback in ((36,False),(37,True)):
            with self.subTest(count=count):
                self.cache=Path(self.temporary.name)/f'coverage-{count}'
                self.service=DailyLoadForecast(CONFIG,self.cache,self.service.fetch_history)
                self.missing(DAY-timedelta(days=1),[5+i*10 for i in range(count)])
                result=self.service.generate(DAY,AT);self.assertEqual(result['fallback_used'],expected_fallback)

    def test_all_seven_days_bad_is_explicit_failure_without_fake_curve(self):
        for age in range(1,8):self.missing(DAY-timedelta(days=age),range(141))
        with self.assertRaises(HistoryValidationError) as error:self.service.generate(DAY,AT)
        self.assertEqual(len(error.exception.reference_attempts),7);self.assertEqual(len(self.calls),7)
        self.assertFalse((self.cache/f'{DAY}.json').exists())

    def test_multiple_bad_days_stop_at_first_valid_older_day(self):
        for age in range(1,4):self.missing(DAY-timedelta(days=age),range(141))
        result=self.service.generate(DAY,AT)
        self.assertEqual(result['reference_date'],(DAY-timedelta(days=4)).isoformat());self.assertEqual(len(self.calls),4)

    def test_actual_missing_points_remain_null_when_prediction_is_repaired(self):
        self.missing(DAY-timedelta(days=1),[10]);self.service.generate(DAY,AT)
        self.missing(DAY,[10])
        result=self.service.response(AT+timedelta(hours=1))
        self.assertIsNone(result['points'][10]['actual_load_kw'])
        self.assertIsNotNone(result['points'][10]['predicted_load_kw'])
        self.assertTrue(result['forecast_metadata']['degraded'])
        self.assertEqual(result['forecast_metadata']['reference_quality']['interpolated_sample_count'],1)

    def test_fallback_success_is_fixed_and_never_overwritten_after_recovery(self):
        self.missing(DAY-timedelta(days=1),range(141));first=self.service.generate(DAY,AT)
        before=(self.cache/f'{DAY}.json').read_bytes();self.calls.clear();self.returned.clear()
        self.assertEqual(self.service.generate(DAY,AT+timedelta(hours=3)),first)
        self.assertEqual((self.cache/f'{DAY}.json').read_bytes(),before);self.assertFalse(self.calls)

    def test_version_one_successful_cache_is_still_readable(self):
        result=self.service.generate(DAY,AT);result['version']=1
        for key in ('fallback_used','reference_policy','reference_attempts'):result.pop(key)
        (self.cache/f'{DAY}.json').write_text(json.dumps(result))
        self.assertEqual(self.service.read_forecast(DAY),result)

    def test_future_reference_and_too_old_fallback_cache_are_rejected(self):
        result=self.service.generate(DAY,AT)
        for reference in (DAY,DAY-timedelta(days=8)):
            broken={**result,'reference_date':reference.isoformat()}
            (self.cache/f'{DAY}.json').write_text(json.dumps(broken))
            with self.assertRaises(ForecastCacheError):self.service.read_forecast(DAY)

    def test_invalid_values_are_recorded_as_gaps_not_clamped_to_percent_limit(self):
        source=payload(DAY-timedelta(days=1))
        source['data'][0]['pointDataList'][10]['val']=150
        self.returned[DAY-timedelta(days=1)]=source
        result=self.service.generate(DAY,AT)
        self.assertEqual(result['reference_quality']['quality_flag_counts']['out_of_range:c09'],1)
        self.assertEqual(result['reference_quality']['interpolated_sample_count'],1)

    def test_transport_failure_does_not_masquerade_as_an_empty_history_day(self):
        def failed(start,end):raise RuntimeError('模拟网络不可用')
        service=DailyLoadForecast(CONFIG,self.cache,failed)
        with self.assertRaises(RuntimeError):service.generate(DAY,AT)
        self.assertFalse((self.cache/f'{DAY}.json').exists())

    def test_tolerance_configuration_bounds_are_checked(self):
        for values in ({'reference_lookback_days':0},{'reference_lookback_days':True},{'reference_lookback_days':31},
                       {'minimum_reference_coverage':0.5},{'minimum_reference_coverage':float('nan')},
                       {'maximum_interpolation_gap_minutes':-1},{'maximum_interpolation_gap_minutes':61},
                       {'maximum_interpolation_gap_minutes':True}):
            with self.subTest(values=values),self.assertRaises(ValueError):DailyLoadForecast({**CONFIG,**values},self.cache,lambda a,b:{})


if __name__ == "__main__":
    unittest.main()
