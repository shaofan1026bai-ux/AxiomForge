"""航班关机平台白名单与现场反馈校验，所有真实网络入口均封锁。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import hangxin_flight_shutdown_platform as adapter
from smart_energy_client import SmartEnergyError


AT = datetime(2026, 11, 5, 18, 0, 5, tzinfo=adapter.ZONE)


def point(tag, name, value, *, writable=False, at=None):
    """只构造脱敏现场身份，时间来自模拟设备而非查询时刻。"""
    return {"pointId": adapter.PREFIX + tag, "deviceId": 12788, "projectId": 832,
            "identifierName": tag, "pointName": name, "unit": "", "isWrite": writable,
            "value": value, "updateTime": (at or AT - timedelta(seconds=5)).isoformat()}


def points():
    """稳定运行的1#机组，首组11:20开机、20:55关机。"""
    rows = [
        point("c00", "主机运行反馈", 1), point("c23", "主机启停输出", 1),
        point("c22", "主机故障反馈", 0),
        point("t01", "开机时间设定1_时", 11, writable=True),
        point("t04", "开机时间设定1_分", 20, writable=True),
        point("t11", "关机时间设定1_时", 20, writable=True),
        point("t14", "关机时间设定1_分", 55, writable=True),
        point("t02", "开机时间设定2_时", 0, writable=True),
    ]
    return {row["pointId"]: row for row in rows}


def response(data=None, *, status=200, code=200, success=True):
    """HTTP响应只在内存使用，不将上游私有正文输出为错误。"""
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({"success": success, "code": code, "data": data or [],
                                  "message": "private-upstream-body"}).encode()
    result._content_consumed = True
    return result


class ShutdownPlatformTests(unittest.TestCase):
    """独立验证关机专用能力，禁止复用其他控制器的写权限。"""

    def setUp(self):
        """每个测试使用独立配置及内存令牌，封锁真实认证与请求。"""
        self.network = patch("smart_energy_client.requests.post", side_effect=AssertionError("测试禁止真实联网"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "config.json"
        self.config.write_text(json.dumps({"base_url": adapter.BASE_URL,
                                          "heads": [{"point_id": "unused-for-energy"}]}))
        self.platform = adapter.Platform(self.config)
        self.platform.client._token = "test-memory-token"
        self.platform.client._token_expires_at = 10 ** 20

    def test_constructor_stays_offline_and_rejects_other_platform(self):
        """构造不登录，地址偏离授权平台立即拒绝。"""
        with patch.object(self.platform.client, "_login") as login:
            adapter.Platform(self.config)
        login.assert_not_called()
        self.config.write_text(json.dumps({"base_url": "https://example.invalid",
                                          "heads": [{"point_id": "unused"}]}))
        with self.assertRaisesRegex(adapter.ShutdownError, "已授权"):
            adapter.Platform(self.config)

    def test_number_preserves_original_report_time_and_finite_numeric_value(self):
        """无时区设备时间按北京时间解释，查询时刻可传UTC。"""
        p = point("c00", "主机运行反馈", 1)
        p["updateTime"] = "2026-11-05 17:59:07"
        self.assertEqual(adapter.point_stamp(p).isoformat(), "2026-11-05T17:59:07+08:00")
        self.assertEqual(adapter.number(p, AT.astimezone(timezone.utc)), 1)
        for value in (True, False, None, "1", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(adapter.ShutdownError):
                adapter.number({**p, "value": value}, AT)

    def test_number_rejects_stale_future_missing_and_naive_times(self):
        """未知状态与时间异常都不能被解释为停机或运行。"""
        for stamp in (AT - timedelta(seconds=181), AT + timedelta(seconds=1)):
            with self.subTest(stamp=stamp), self.assertRaises(adapter.ShutdownError):
                adapter.number(point("c00", "主机运行反馈", 1, at=stamp), AT)
        for data in (None, {}, {"value": 1, "updateTime": "invalid"}):
            with self.subTest(data=data), self.assertRaises(adapter.ShutdownError):
                adapter.number(data, AT)
        with self.assertRaises(adapter.ShutdownError):
            adapter.number(point("c00", "主机运行反馈", 1), AT.replace(tzinfo=None))

    def test_running_and_output_must_agree_with_fault_zero(self):
        """稳定运行返回True，明确停止返回False，切换或故障抛异常。"""
        self.assertTrue(adapter.validate_running(points(), AT))
        data = points()
        data[adapter.PREFIX + "c00"]["value"] = 0
        data[adapter.PREFIX + "c23"]["value"] = 0
        self.assertFalse(adapter.validate_running(data, AT))
        for running, output, fault in ((1, 0, 0), (0, 1, 0), (1, 1, 1), (0, 0, 1)):
            data = points()
            for tag, value in (("c00", running), ("c23", output), ("c22", fault)):
                data[adapter.PREFIX + tag]["value"] = value
            with self.subTest(running=running, output=output, fault=fault), self.assertRaises(adapter.ShutdownError):
                adapter.validate_running(data, AT)

    def test_each_status_requires_legal_fresh_numeric_binary_value(self):
        """布尔、非法枚举、缺测、过期反馈均不放行关机改表。"""
        for tag in ("c00", "c23", "c22"):
            for value in (None, True, False, "0", 2, -1):
                data = points()
                data[adapter.PREFIX + tag]["value"] = value
                with self.subTest(tag=tag, value=value), self.assertRaises(adapter.ShutdownError):
                    adapter.validate_running(data, AT)
            data = points()
            data[adapter.PREFIX + tag]["updateTime"] = (AT - timedelta(seconds=181)).isoformat()
            with self.subTest(tag=tag, reason="stale"), self.assertRaises(adapter.ShutdownError):
                adapter.validate_running(data, AT)
            data.pop(adapter.PREFIX + tag)
            with self.subTest(tag=tag, reason="missing"), self.assertRaises(adapter.ShutdownError):
                adapter.validate_running(data, AT)

    def test_status_point_identity_and_readonly_permission_are_strict(self):
        """不能用其他机组、输出点或可写命令冒充真实运行反馈。"""
        for key, value in (("deviceId", 12789), ("projectId", 833), ("pointId", adapter.PREFIX + "c23"),
                           ("identifierName", "c23"), ("pointName", "主机启停输出"),
                           ("unit", "℃"), ("isWrite", True)):
            data = points()
            data[adapter.PREFIX + "c00"][key] = value
            with self.subTest(key=key), self.assertRaises(adapter.ShutdownError):
                adapter.validate_running(data, AT)

    def test_schedule_fields_use_today_and_preserve_other_timers(self):
        """关机表与开机表保持独立，只保护不回写其他时段。"""
        data = points()
        self.assertEqual(adapter.read_off_schedule(data, AT).isoformat(), "2026-11-05T20:55:00+08:00")
        self.assertEqual(adapter.read_start_schedule(data, AT).isoformat(), "2026-11-05T11:20:00+08:00")
        self.assertEqual(adapter.protected_schedules(data), {
            adapter.PREFIX + "t01": 11, adapter.PREFIX + "t04": 20, adapter.PREFIX + "t02": 0,
        })
        self.assertNotIn(adapter.HOUR_ID, adapter.protected_schedules(data))
        self.assertNotIn(adapter.MINUTE_ID, adapter.protected_schedules(data))

    def test_schedule_requires_correct_identity_writability_range_and_freshness(self):
        """设备上的可写时分仍须合法新鲜，不能把缺值自动补成整点。"""
        for pid, key, value in ((adapter.HOUR_ID, "value", 24), (adapter.MINUTE_ID, "value", 60),
                                (adapter.HOUR_ID, "value", 19.5), (adapter.MINUTE_ID, "value", False),
                                (adapter.HOUR_ID, "isWrite", False), (adapter.HOUR_ID, "pointName", "其他时间"),
                                (adapter.MINUTE_ID, "updateTime", (AT + timedelta(seconds=1)).isoformat())):
            data = points()
            data[pid][key] = value
            with self.subTest(pid=pid, key=key), self.assertRaises(adapter.ShutdownError):
                adapter.read_off_schedule(data, AT)

    def test_read_queries_only_one_authorized_chiller(self):
        """请求只含设备12788，不能扩展到水泵或AHU。"""
        data = points()
        with patch.object(self.platform.client, "_post", return_value=response(list(data.values()))) as post:
            self.assertEqual(self.platform.read(), data)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["body"], {"deviceIdList": [12788], "isActivate": True})
        self.assertTrue(post.call_args.args[0].endswith(adapter.READ_PATH))

    def test_read_rejects_other_devices_projects_and_duplicate_or_mismatched_ids(self):
        """整份响应存在越界身份即拒绝，不静默使用可疑子集。"""
        base = list(points().values())
        cases = []
        for mutation in ({"deviceId": 12789}, {"projectId": 831}, {"pointId": "ewcnad_msahu1_v20"},
                          {"deviceId": "12788"}):
            rows = deepcopy(base)
            rows[0].update(mutation)
            cases.append(rows)
        cases.extend([base + [deepcopy(base[0])], [None]])
        for rows in cases:
            with self.subTest(rows=rows), patch.object(self.platform.client, "_post", return_value=response(rows)):
                with self.assertRaises(adapter.ShutdownError):
                    self.platform.read()

    def test_read_auth_expiry_relogs_only_once_for_http_or_business_failure(self):
        """只读认证过期允许一次重登；多次拒绝直接失败。"""
        def login():
            self.platform.client._token = "new-test-token"
            self.platform.client._token_expires_at = 10 ** 20

        for expired in (response(status=401), response(code=403)):
            with self.subTest(status=expired.status_code), patch.object(self.platform.client, "_login", side_effect=login) as signin:
                with patch.object(self.platform.client, "_post", side_effect=[expired, response(list(points().values()))]) as post:
                    self.assertEqual(self.platform.read(), points())
                self.assertEqual(post.call_count, 2)
                signin.assert_called_once()
        with patch.object(self.platform.client, "_login", side_effect=login) as signin:
            with patch.object(self.platform.client, "_post", side_effect=[response(status=401), response(status=403)]) as post:
                with self.assertRaises(SmartEnergyError):
                    self.platform.read()
            self.assertEqual(post.call_count, 2)
            signin.assert_called_once()

    def test_prepare_write_refreshes_auth_without_device_request(self):
        """认证耗时留在controller最终时钟校验之前。"""
        self.platform.client._token = None
        with patch.object(self.platform.client, "_login") as signin, patch.object(self.platform.client, "_post") as post:
            def login():
                self.platform.client._token = "prepared-test-token"
                self.platform.client._token_expires_at = 10 ** 20
            signin.side_effect = login
            self.platform.prepare_write()
        signin.assert_called_once()
        post.assert_not_called()

    def test_write_only_allows_evening_candidate_integer_fields(self):
        """仅允许18—20小时和0/30分钟，完整候选由控制器判断。"""
        for pid, value in ((adapter.HOUR_ID,18),(adapter.HOUR_ID,19),(adapter.HOUR_ID,20),(adapter.MINUTE_ID,0),(adapter.MINUTE_ID,30)):
            with self.subTest(pid=pid), patch.object(self.platform.client, "_post", return_value=response()) as post:
                self.assertEqual(self.platform.write(pid, value), {"http_status": 200, "accepted": True})
                self.assertEqual(post.call_args.kwargs["body"], {pid: value})
                self.assertEqual(post.call_args.args[0], adapter.BASE_URL + adapter.WRITE_PATH)
                post.assert_called_once()
        forbidden = [(adapter.HOUR_ID, 17),(adapter.HOUR_ID,21), (adapter.HOUR_ID, 20.0), (adapter.MINUTE_ID, False),
                     (adapter.MINUTE_ID, 55), (adapter.MINUTE_ID, 0.0), (adapter.PREFIX + "t01", 20),
                     (adapter.PREFIX + "c17", 8), (adapter.PREFIX + "c00", 0),
                     ("ewcnad_msahu1_v20", 0), ("qsqudq_mspump1_p21", 10), (None, 0)]
        for pid, value in forbidden:
            with self.subTest(pid=pid, value=value), patch.object(self.platform.client, "_post") as post:
                with self.assertRaises(adapter.ShutdownError):
                    self.platform.write(pid, value)
                post.assert_not_called()

    def test_write_never_implicitly_logs_in_or_retries(self):
        """认证无效及401只作为本次失败，不能使网络请求越过最终截止重试。"""
        self.platform.client._token = None
        with patch.object(self.platform.client, "_login") as signin, patch.object(self.platform.client, "_post") as post:
            with self.assertRaises(SmartEnergyError):
                self.platform.write(adapter.MINUTE_ID, 0)
        signin.assert_not_called()
        post.assert_not_called()
        self.platform.client._token = "test-memory-token"
        for failed in (response(status=401), response(code=401), response(status=503)):
            with patch.object(self.platform.client, "_login") as signin:
                with patch.object(self.platform.client, "_post", return_value=failed) as post:
                    self.assertFalse(self.platform.write(adapter.MINUTE_ID, 0)["accepted"])
                    post.assert_called_once()
                signin.assert_not_called()

    def test_transport_failure_is_not_repeated_or_echoed(self):
        """请求结果不明交由controller持久记录及回读，适配器不补发。"""
        with patch.object(self.platform.client, "_post", side_effect=SmartEnergyError("固定超时说明")) as post:
            with self.assertRaises(SmartEnergyError):
                self.platform.write(adapter.MINUTE_ID, 0)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
