"""验证后台每日时区调度、补跑及固定预测/实时实际的API集成。"""

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient

from app import create_app
from forecast_runtime import CATCHUP_JOB_ID, DAILY_JOB_ID, RETRY_JOB_ID, ForecastRuntime
from smart_energy_client import SmartEnergyError


ZONE = ZoneInfo("Asia/Shanghai")
AT = datetime(2026, 10, 30, 12, tzinfo=ZONE)


class FakeClient:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.energy = 60
        self.failure = None

    def fetch_history(self, start, end):
        self.calls.append((start, end))
        if self.failure:
            raise self.failure
        return {"success": True, "code": 200, "data": [
            {"deviceId": 12788, "identifierName": head["identifier_name"],
             "pointId": head["point_id"], "unit": "%", "pointDataList": [
                 {"ts": (start + timedelta(minutes=2 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                  "val": self.energy}
                 for i in range(720)]}
            for head in self.config["heads"]]}


class ForecastRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = json.loads(Path(__file__).with_name("load_forecast_config.json").read_text())
        self.config["enabled"] = True
        self.path = self.root / "load_forecast_config.json"
        self.path.write_text(json.dumps(self.config))
        self.client = FakeClient(self.config)
        self.runtime = ForecastRuntime(self.path, client=self.client, clock=lambda: AT)

    def tearDown(self):
        self.temp.cleanup()

    def test_daily_trigger_is_0005_shanghai_and_does_not_fetch_on_install(self):
        scheduler = BackgroundScheduler(timezone=ZONE)
        self.runtime.install(scheduler)
        job = scheduler.get_job(DAILY_JOB_ID)
        self.assertEqual(job.trigger.get_next_fire_time(None, AT), datetime(2026, 10, 31, 0, 5, tzinfo=ZONE))
        self.assertEqual(self.client.calls, [])

    def test_internal_job_ids_cannot_replace_user_strategy_jobs(self):
        for reserved in (DAILY_JOB_ID, CATCHUP_JOB_ID, RETRY_JOB_ID):
            with self.subTest(reserved=reserved):
                scheduler = BackgroundScheduler(timezone=ZONE)
                sentinel = lambda: None
                scheduler.add_job(sentinel, "interval", seconds=10, id=reserved)
                with self.assertRaises(ValueError):
                    self.runtime.install(scheduler)
                self.assertIs(scheduler.get_job(reserved).func, sentinel)

    def test_startup_catches_up_only_after_0005_and_only_without_snapshot(self):
        scheduler = BackgroundScheduler(timezone=ZONE)
        self.runtime.install(scheduler)
        self.runtime.clock = lambda: AT.replace(hour=0, minute=4)
        self.runtime.catch_up()
        self.assertIsNone(scheduler.get_job(CATCHUP_JOB_ID))
        self.runtime.clock = lambda: AT
        self.runtime.catch_up()
        self.assertIsNotNone(scheduler.get_job(CATCHUP_JOB_ID))
        scheduler.remove_job(CATCHUP_JOB_ID)
        self.runtime.generate(AT.date(), AT)
        self.runtime.catch_up()
        self.assertIsNone(scheduler.get_job(CATCHUP_JOB_ID))

    def test_failed_login_persists_safe_status_without_success_snapshot(self):
        self.client.failure = SmartEnergyError("平台自动登录尚未配置环境变量")
        result = self.runtime.generate(AT.date(), AT)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(self.runtime.service.read_forecast(AT.date()))
        saved = json.loads(self.runtime.status_path.read_text())
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], "平台自动登录尚未配置环境变量")

    def test_fallback_success_is_visible_in_status_and_details_metadata(self):
        original=self.client.fetch_history
        def fetch(start,end):
            result=original(start,end)
            if start.date()==AT.date()-timedelta(days=1):
                for series in result['data']:
                    for row in series['pointDataList'][:141]:row['val']=None
            return result
        self.runtime.service.fetch_history=fetch
        result=self.runtime.generate(AT.date(),AT)
        self.assertTrue(result['fallback_used'])
        status=self.runtime.status()
        self.assertEqual(status['forecast_status'],'ready');self.assertTrue(status['fallback_used'])
        self.assertEqual(status['reference_date'],(AT.date()-timedelta(days=2)).isoformat())
        metadata=self.runtime.response(AT)['forecast_metadata']
        self.assertTrue(metadata['degraded']);self.assertEqual(len(metadata['reference_attempts']),2)
        self.assertEqual(self.runtime.last_attempt['status'],'success')

    def test_all_bad_reference_days_persist_diagnostics_without_success_cache(self):
        original=self.client.fetch_history
        def fetch(start,end):
            result=original(start,end)
            for series in result['data']:
                for row in series['pointDataList'][:141]:row['val']=None
            return result
        self.runtime.service.fetch_history=fetch
        self.runtime.generate(AT.date(),AT)
        saved=json.loads(self.runtime.status_path.read_text())
        self.assertEqual(saved['status'],'failed');self.assertEqual(len(saved['reference_attempts']),7)
        self.assertEqual(self.runtime.status()['forecast_status'],'missing')

    def test_failed_daily_run_has_bounded_retries_and_does_not_cross_dates(self):
        scheduler = BackgroundScheduler(timezone=ZONE)
        self.runtime.install(scheduler)
        self.client.failure = SmartEnergyError("参考数据尚未齐全")
        first = AT.replace(hour=0, minute=5)
        self.runtime.clock = lambda: first
        self.runtime.generate_today()
        job = scheduler.get_job(RETRY_JOB_ID)
        self.assertEqual(job.trigger.run_date, first + timedelta(minutes=5))
        scheduler.remove_job(RETRY_JOB_ID)
        self.runtime.clock = lambda: first + timedelta(minutes=5)
        self.runtime._scheduled_attempt(first.date(), 1)
        self.assertEqual(scheduler.get_job(RETRY_JOB_ID).trigger.run_date, first + timedelta(minutes=15))
        scheduler.remove_job(RETRY_JOB_ID)
        self.runtime._scheduled_attempt(first.date(), 2)
        self.assertIsNone(scheduler.get_job(RETRY_JOB_ID))
        before = len(self.client.calls)
        self.runtime.clock = lambda: first + timedelta(days=1)
        self.assertEqual(self.runtime._scheduled_attempt(first.date(), 1)["status"], "skipped")
        self.assertEqual(len(self.client.calls), before)

    def test_live_api_reads_fixed_forecast_and_fetches_only_actual_day(self):
        self.runtime.generate(AT.date(), AT)
        expected = deepcopy(self.runtime.service.read_forecast(AT.date())["points"])
        self.client.calls.clear()
        self.client.energy = 35
        tasks = self.root / "tasks.json"
        tasks.write_text('{"tasks": []}')
        with patch("app.create_forecast_runtime", return_value=self.runtime):
            with patch("app.datetime", wraps=datetime) as clock:
                clock.now.return_value = AT
                with TestClient(create_app(tasks)) as api:
                    result = api.get("/load-forecast")
                    self.assertEqual(result.status_code, 200, result.text)
                    body = result.json()
                    self.assertEqual(body["source"], "live_api")
                    self.assertEqual(body["scope"], "single_chiller")
                    self.assertNotIn("forecast_status", body)
                    self.assertNotIn("time_semantics", body)
                    self.assertEqual(len(body), 10)
                    self.assertIsInstance(body["actual_energy_kwh"], (int, float))
                    self.assertEqual(body["points"][0]["time"], "2026-10-30T00:02:00+08:00")
                    self.assertEqual(body["points"][-1]["time"], "2026-10-31T00:00:00+08:00")
                    self.assertAlmostEqual(body["points"][0]["actual_load_kw"], 834.4 * 0.35)
                    self.assertEqual([p["predicted_load_kw"] for p in body["points"]],
                                     [p["predicted_load_kw"] for p in expected])
                    self.assertIsNone(body["points"][-1]["actual_load_kw"])
                    before = len(self.client.calls)
                    status = api.get("/load-forecast/status")
                    self.assertEqual(status.status_code, 200)
                    self.assertTrue(status.json()["scheduled"])
                    self.assertEqual(status.json()["forecast_status"], "ready")
                    self.assertEqual(len(self.client.calls), before)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0][0].date(), AT.date())

    def test_missing_forecast_does_not_generate_during_get(self):
        tasks = self.root / "tasks.json"
        tasks.write_text('{"tasks": []}')
        # 避免该测试的服务启动补跑；只观察GET自身的行为。
        with patch.object(self.runtime, "catch_up"):
            with patch("app.create_forecast_runtime", return_value=self.runtime):
                with patch("app.datetime", wraps=datetime) as clock:
                    clock.now.return_value = AT
                    with TestClient(create_app(tasks)) as api:
                        response = api.get("/load-forecast")
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertNotIn("forecast_status", response.json())
                        self.assertEqual(api.get("/load-forecast/status").json()["forecast_status"], "missing")
                        self.assertTrue(all(p["predicted_load_kw"] is None for p in response.json()["points"]))
                        self.assertEqual(len(self.client.calls), 1)
                        self.assertEqual(self.client.calls[0][0].date(), AT.date())

    def test_detailed_route_keeps_metadata_and_does_not_modify_cache(self):
        self.runtime.generate(AT.date(), AT)
        cache = self.root / self.config["cache_directory"] / '2026-10-30.json'
        original = cache.read_bytes()
        self.client.calls.clear()
        tasks = self.root / 'tasks.json'
        tasks.write_text('{"tasks": []}')
        with patch('app.create_forecast_runtime', return_value=self.runtime):
            with patch('app.datetime', wraps=datetime) as clock:
                clock.now.return_value = AT
                with TestClient(create_app(tasks)) as api:
                    body = api.get('/load-forecast/details').json()
                    self.assertEqual(body['forecast_status'], 'ready')
                    self.assertEqual(body['time_semantics'], 'interval_end')
                    self.assertEqual(body['source_time_semantics'], 'sample_time')
                    self.assertEqual(body['energy_method'], 'left_sample_hold_estimate')
                    self.assertEqual(body['points'][0]['time'], '2026-10-30T00:02:00+08:00')
                    self.assertTrue(body['forecast_metadata']['fixed_snapshot'])
                    self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(cache.read_bytes(), original)

    def test_upstream_failure_is_503_not_simulated_success(self):
        self.client.failure = SmartEnergyError("平台连接失败或请求超时")
        tasks = self.root / "tasks.json"
        tasks.write_text('{"tasks": []}')
        with patch.object(self.runtime, "catch_up"):
            with patch("app.create_forecast_runtime", return_value=self.runtime):
                with TestClient(create_app(tasks)) as api:
                    result = api.get("/load-forecast")
                    self.assertEqual(result.status_code, 503)
                    self.assertEqual(result.json()["detail"], "平台连接失败或请求超时")

    def test_disabled_live_mode_does_not_return_mock_success(self):
        self.runtime.enabled = False
        tasks = self.root / "tasks.json"
        tasks.write_text('{"tasks": []}')
        with patch("app.create_forecast_runtime", return_value=self.runtime):
            with TestClient(create_app(tasks)) as api:
                result = api.get("/load-forecast")
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()["detail"], "真实负荷功能已停用")
                self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
