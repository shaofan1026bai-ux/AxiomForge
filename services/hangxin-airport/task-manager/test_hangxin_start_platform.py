"""开机预测取数与写入白名单的离线验证，所有真实网络入口均禁止调用。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import hangxin_start_platform as adapter
from smart_energy_client import SmartEnergyError


AT = datetime(2026, 11, 5, 10, 0, 5, tzinfo=adapter.ZONE)


def point(pid, spec, value, *, at=None, writable=False):
    """按现场只读清单构造固定身份，不使用任何真实认证信息。"""
    device, tag, name, unit = spec
    return {"pointId": pid, "deviceId": device, "projectId": 832,
            "identifierName": tag, "pointName": name, "unit": unit,
            "isWrite": writable, "value": value,
            "updateTime": (at or AT - timedelta(seconds=5)).isoformat()}


def points():
    """正常快照包含4台运行AHU、停机主机和首组开关机时间。"""
    data = {}
    for pid, spec in adapter.SPECS.items():
        tag = spec[1]
        value = {"c00": 0, "c23": 0, "c22": 0, "c21": 25.0,
                 "airin": 26.0, "v20": 1, "v01": 40.0, "freshv": 30.0}[tag]
        data[pid] = point(pid, spec, value)
    for tag, name, value in (("t01", "开机时间设定1_时", 14), ("t04", "开机时间设定1_分", 30),
                             ("t11", "关机时间设定1_时", 21), ("t14", "关机时间设定1_分", 0)):
        pid = adapter.PREFIX + tag
        data[pid] = point(pid, (12788, tag, name, ""), value, writable=True)
    return data


def response(data=None, *, status=200, code=200, success=True):
    """仅在内存创建HTTP对象，响应正文不能参与任何真实请求。"""
    item = requests.Response()
    item.status_code = status
    item._content = json.dumps({"success": success, "code": code, "data": data or [],
                               "message": "private-upstream-body"}).encode()
    item._content_consumed = True
    return item


def histories():
    """历史返回保留平台元数据，样本包含原始非整秒时间与缺测。"""
    result = []
    for pid, spec in adapter.SPECS.items():
        item = point(pid, spec, points()[pid]["value"])
        item.pop("isWrite")
        item.pop("value")
        item.pop("updateTime")
        item["pointDataList"] = [
            {"ts": (AT - timedelta(minutes=2, seconds=7)).replace(tzinfo=None).isoformat(),
             "val": points()[pid]["value"]},
            {"ts": (AT - timedelta(minutes=1)).isoformat(), "val": None},
        ]
        result.append(item)
    return result


class PlatformTests(unittest.TestCase):
    """范围、时间、空数据、认证及单次写入契约均在离线环境验证。"""

    def setUp(self):
        self.network = patch("smart_energy_client.requests.post", side_effect=AssertionError("测试禁止真实联网"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "config.json"
        self.config.write_text(json.dumps({"base_url": adapter.BASE_URL,
                                         "heads": [{"point_id": "unused-for-energy"}]}))
        self.platform = adapter.Platform(self.config)
        self.platform.client._token = "test-memory-token"
        self.platform.client._token_expires_at = 10 ** 20

    def test_constructor_stays_offline_and_rejects_other_platform(self):
        self.config.write_text(json.dumps({"base_url": "https://example.invalid",
                                         "heads": [{"point_id": "x"}]}))
        with self.assertRaisesRegex(adapter.StartError, "已授权"):
            adapter.Platform(self.config)

    def test_time_control_writer_allows_only_verified_switch_and_integer_zero_one(self):
        for value in (0,1):
            with patch.object(self.platform.client,'_post',return_value=response()) as post:
                self.assertTrue(self.platform.write_time_control(adapter.TIME_CONTROL_ID,value)['accepted'])
                self.assertEqual(post.call_args.kwargs['body'],{adapter.TIME_CONTROL_ID:value})
                post.assert_called_once()
        for pid,value in ((adapter.HOUR_ID,0),(adapter.PREFIX+'t11',0),('ob24v7_mscdfan_s05',1),
                          (adapter.TIME_CONTROL_ID,True),(adapter.TIME_CONTROL_ID,1.0),
                          (adapter.TIME_CONTROL_ID,2),(adapter.TIME_CONTROL_ID,'0')):
            with self.subTest(pid=pid,value=value),patch.object(self.platform.client,'_post') as post:
                with self.assertRaises(adapter.StartError):self.platform.write_time_control(pid,value)
                post.assert_not_called()
        self.assertFalse(hasattr(self.platform,'write_day_schedule'),'旧清零入口应已移除')

    def test_time_control_read_is_separate_from_forecast_scope_and_rejects_wrong_device(self):
        p=point(adapter.TIME_CONTROL_ID,adapter.TIME_CONTROL_SPEC,1,writable=True)
        with patch.object(self.platform.client,'_post',return_value=response([p])) as post:
            self.assertIn(adapter.TIME_CONTROL_ID,self.platform.read_time_control())
            self.assertEqual(post.call_args.kwargs['body'],{'deviceIdList':[12788,12789,12796],'isActivate':True})
        for rows in ([p,p],[dict(p,deviceId=12783)],[dict(p,projectId=999)],
                     [dict(p,pointId='nfdh9q_mscrk01_c00')]):
            with self.subTest(rows=rows),patch.object(self.platform.client,'_post',return_value=response(rows)):
                with self.assertRaises(adapter.StartError):self.platform.read_time_control()

    def test_number_preserves_report_time_and_rejects_bad_measurements(self):
        p = points()[adapter.PREFIX + "c21"]
        p["updateTime"] = "2026-11-05 09:59:07"
        self.assertEqual(adapter.point_stamp(p).isoformat(), "2026-11-05T09:59:07+08:00")
        self.assertEqual(adapter.number(p, AT.astimezone(timezone.utc)), 25)
        for value in (None, True, "25", float("inf"), float("nan"), 10 ** 1000):
            with self.subTest(value_type=type(value).__name__):
                p["value"] = value
                with self.assertRaises(adapter.StartError):
                    adapter.number(p, AT)

    def test_number_rejects_stale_future_and_naive_evaluation(self):
        p = points()[adapter.PREFIX + "c21"]
        for stamp in (AT - timedelta(seconds=181), AT + timedelta(seconds=1)):
            p["updateTime"] = stamp.isoformat()
            with self.assertRaises(adapter.StartError):
                adapter.number(p, AT)
        with self.assertRaises(adapter.StartError):
            adapter.number(p, AT.replace(tzinfo=None))

    def test_stopped_requires_fresh_zero_running_output_and_fault(self):
        adapter.assert_stopped(points(), AT)
        for tag in ("c00", "c23", "c22"):
            for value in (1, 2, -1, None, False):
                with self.subTest(tag=tag, value=value):
                    data = points()
                    data[adapter.PREFIX + tag]["value"] = value
                    with self.assertRaises(adapter.StartError):
                        adapter.assert_stopped(data, AT)

    def test_status_identity_mismatch_stale_and_missing_are_rejected(self):
        for key, value in (("deviceId", 12789), ("projectId", 1), ("identifierName", "c23"),
                           ("pointName", "主机启停输出"), ("unit", "℃"), ("isWrite", True),
                           ("updateTime", (AT - timedelta(seconds=181)).isoformat())):
            data = points()
            data[adapter.PREFIX + "c00"][key] = value
            with self.subTest(key=key), self.assertRaises(adapter.StartError):
                adapter.assert_stopped(data, AT)
        data = points()
        data.pop(adapter.PREFIX + "c22")
        with self.assertRaises(adapter.StartError):
            adapter.assert_stopped(data, AT)

    def test_ahu_requires_all_fresh_values_and_running_frequency_feedback(self):
        self.assertEqual(adapter.eligible_ahus(points(), AT), [1, 2, 4, 5])
        for tag, key, value in (("v20", "value", 0), ("v01", "value", 0),
                                ("v01", "value", 61), ("airin", "value", 46),
                                ("airin", "value", 9), ("freshv", "value", 101),
                                ("freshv", "value", -1), ("v20", "value", 2),
                                ("airin", "updateTime", (AT - timedelta(seconds=181)).isoformat()),
                                ("v20", "deviceId", 12784)):
            data = points()
            data[f"ewcnad_msahu1_{tag}"][key] = value
            with self.subTest(tag=tag, key=key, value=value):
                self.assertEqual(adapter.eligible_ahus(data, AT), [2, 4, 5])

    def test_schedule_uses_today_validates_fields_and_protects_other_timers(self):
        data = points()
        self.assertEqual(adapter.read_schedule(data, AT).isoformat(), "2026-11-05T14:30:00+08:00")
        self.assertEqual(adapter.off_time(data, AT).isoformat(), "2026-11-05T21:00:00+08:00")
        self.assertEqual(adapter.protected_schedules(data), {adapter.PREFIX + "t11": 21,
                                                          adapter.PREFIX + "t14": 0})
        for pid, key, value in ((adapter.HOUR_ID, "value", 24), (adapter.MINUTE_ID, "value", 60),
                                (adapter.MINUTE_ID, "value", 1.5), (adapter.HOUR_ID, "isWrite", False),
                                (adapter.HOUR_ID, "pointName", "其他时间")):
            bad = deepcopy(data)
            bad[pid][key] = value
            with self.subTest(pid=pid, key=key), self.assertRaises(adapter.StartError):
                adapter.read_schedule(bad, AT)

    def test_read_queries_only_fixed_devices(self):
        data = points()
        with patch.object(self.platform.client, "_post", return_value=response(list(data.values()))) as post:
            self.assertEqual(self.platform.read(), data)
        self.assertEqual(post.call_args.kwargs["body"], {"deviceIdList": [12788, 12783, 12784, 12786, 12787],
                                                       "isActivate": True})
        self.assertTrue(post.call_args.args[0].endswith(adapter.READ_PATH))

    def test_read_rejects_extra_devices_project_and_duplicate_identifiers(self):
        good = list(points().values())
        for replacement in ({"deviceId": 12789}, {"projectId": 99},
                            {"pointId": "ewcnad_msahu2_airin"}):
            rows = deepcopy(good)
            rows[0].update(replacement)
            with patch.object(self.platform.client, "_post", return_value=response(rows)):
                with self.assertRaises(adapter.StartError):
                    self.platform.read()
        with patch.object(self.platform.client, "_post", return_value=response(good + [good[0]])):
            with self.assertRaises(adapter.StartError):
                self.platform.read()

    def test_read_auth_expiry_retries_once_without_leaking_response(self):
        def login():
            self.platform.client._token = "renewed-test-token"
            self.platform.client._token_expires_at = 10 ** 20
        for failed in (response(status=401), response(code=403, success=False)):
            with self.subTest(status=failed.status_code):
                with patch.object(self.platform.client, "_login", side_effect=login) as sign_in:
                    with patch.object(self.platform.client, "_post", side_effect=[failed, response()]) as post:
                        self.assertEqual(self.platform.read(), {})
                    self.assertEqual(post.call_count, 2)
                    sign_in.assert_called_once()
        with patch.object(self.platform.client, "_login", side_effect=login):
            with patch.object(self.platform.client, "_post", side_effect=[response(status=401), response(status=403)]) as post:
                with self.assertRaises(SmartEnergyError) as captured:
                    self.platform.read()
                self.assertNotIn("private-upstream-body", str(captured.exception))
                self.assertEqual(post.call_count, 2)

    def test_read_non_auth_failure_does_not_retry(self):
        with patch.object(self.platform.client, "_post", return_value=response(status=500)) as post:
            with self.assertRaises(SmartEnergyError):
                self.platform.read()
        post.assert_called_once()

    def test_history_queries_20_points_without_water_once_and_keeps_original_times(self):
        raw = histories()
        with patch.object(self.platform.client, "_post", return_value=response(raw)) as post:
            result = self.platform.history(AT)
        post.assert_called_once()
        body = post.call_args.kwargs["body"]
        self.assertEqual(body["timeQuantum"], {"startDate": "2026-11-05 08:00", "endDate": "2026-11-05 10:01"})
        self.assertEqual(body["interval"], 1)
        self.assertEqual(set(body["pointIdList"]), set(adapter.SPECS))
        self.assertEqual(len(body["pointIdList"]), 20)
        self.assertNotIn(adapter.PREFIX + "c08", body["pointIdList"])
        self.assertEqual(result[adapter.PREFIX + "c21"], [{"at": "2026-11-05T09:57:58+08:00", "value": 25.0}])

    def test_history_empty_and_absent_ahu_series_do_not_fail_or_fill(self):
        raw = [r for r in histories() if not r["pointId"].startswith("ewcnad_msahu5_")]
        raw[0]["pointDataList"] = []
        raw[1]["pointDataList"] = None
        with patch.object(self.platform.client, "_post", return_value=response(raw)):
            result = self.platform.history(AT)
        self.assertEqual(len(result), 20)
        self.assertEqual(result[raw[0]["pointId"]], [])
        self.assertEqual(result[raw[1]["pointId"]], [])
        self.assertEqual(result["ewcnad_msahu5_airin"], [])

    def test_history_rejects_wrong_identity_time_conflict_and_nonfinite(self):
        changes = (
            lambda row: row.update(deviceId=12789),
            lambda row: row.update(projectId=999),
            lambda row: row.update(identifierName="c23"),
            lambda row: row.update(unit="%"),
            lambda row: row.update(pointId="unknown-point"),
            lambda row: row["pointDataList"][0].update(ts="2026-11-05 07:59:00"),
            lambda row: row["pointDataList"][0].update(ts="2026-11-05 10:01:00"),
            lambda row: row["pointDataList"][0].update(val=float("inf")),
            lambda row: row["pointDataList"].append({**row["pointDataList"][0], "val": 999}),
        )
        for i, change in enumerate(changes):
            raw = histories()
            change(raw[0])
            with self.subTest(case=i), patch.object(self.platform.client, "_post", return_value=response(raw)):
                with self.assertRaises(adapter.StartError):
                    self.platform.history(AT)

    def test_payload_maps_original_series_and_keeps_unknown_flow_null(self):
        data = points()
        old = AT - timedelta(minutes=2, seconds=7)
        h = {pid: [{"at": old.isoformat(), "value": p["value"]},
                   {"at": (AT - timedelta(minutes=1)).isoformat(), "value": None},
                   {"at": AT.isoformat(), "value": 999}]
             for pid, p in data.items() if pid in adapter.SPECS}
        payload = adapter.build_payload(data, h, AT, adapter.read_schedule(data, AT))
        self.assertEqual(payload["evaluated_at"], AT.isoformat())
        self.assertEqual(payload["original_schedule_start_at"], "2026-11-05T14:30:00+08:00")
        self.assertIsNone(payload["flow_normal"])
        self.assertIsNone(payload["circulation_confirmed"])
        self.assertEqual(set(payload["series"]), {"outdoor", "fault", "running", "start_output"})
        self.assertEqual(payload["series"]["outdoor"], [
            {"at": old.isoformat(), "value": 25.0},
            {"at": "2026-11-05T10:00:00+08:00", "value": 25.0},
        ])
        self.assertEqual([x["ahu"] for x in payload["ahus"]], [1, 2, 4, 5])

    def test_unused_water_metadata_and_history_cannot_block_payload(self):
        """平台仍返回水温点时，坏水温身份、数值和时间都不进入预测转换。"""
        baseline=adapter.build_payload(points(),{},AT,None)
        data=points();pid=adapter.PREFIX+'c08'
        self.assertNotIn(pid,adapter.SPECS)
        data[pid]=point(pid,(12788,'c08','不再使用的水温点','unknown'),'invalid')
        data[pid]['updateTime']='invalid-time'
        with patch.object(self.platform.client,'_post',return_value=response(list(data.values()))):
            read=self.platform.read()
        payload=adapter.build_payload(read,{pid:[{'at':'invalid-time','value':'invalid'}]},AT,None)
        self.assertEqual(payload,baseline);self.assertNotIn('inlet_water',payload['series'])

    def test_payload_clears_current_ineligible_ahu_despite_recent_running_history(self):
        data = points()
        h = {pid: [{"at": (AT - timedelta(minutes=1)).isoformat(), "value": p["value"]}]
             for pid, p in data.items() if pid in adapter.SPECS}
        data["ewcnad_msahu5_v20"]["value"] = 0
        result = adapter.build_payload(data, h, AT, None)
        ahu5 = next(x for x in result["ahus"] if x["ahu"] == 5)
        self.assertEqual(ahu5, {"ahu": 5, "return": [], "running": [], "frequency": [], "fresh": []})
        self.assertTrue(result["ahus"][0]["return"])

    def test_payload_rejects_conflicting_current_and_historical_values(self):
        data = points()
        pid = adapter.PREFIX + "c21"
        h = {pid: [{"at": data[pid]["updateTime"], "value": 999}]}
        with self.assertRaisesRegex(adapter.StartError, "冲突"):
            adapter.build_payload(data, h, AT, None)
        h = {pid: [{"at": "2026-11-05T09:59:00+08:00", "value": 25},
                   {"at": "2026-11-05T09:59:00+08:00", "value": 26}]}
        with self.assertRaisesRegex(adapter.StartError, "冲突"):
            adapter.build_payload(data, h, AT, None)

    def test_write_allows_only_two_integer_fields_and_sends_single_point(self):
        for pid, value in ((adapter.HOUR_ID, 0), (adapter.HOUR_ID, 23),
                           (adapter.MINUTE_ID, 0), (adapter.MINUTE_ID, 59)):
            with patch.object(self.platform.client, "_post", return_value=response()) as post:
                self.assertEqual(self.platform.write(pid, value), {"http_status": 200, "accepted": True})
            self.assertEqual(post.call_args.kwargs["body"], {pid: value})
            self.assertTrue(post.call_args.args[0].endswith(adapter.WRITE_PATH))
            post.assert_called_once()
        for pid, value in ((adapter.PREFIX + "c17", 8), (adapter.PREFIX + "t11", 21),
                           (adapter.HOUR_ID, 24), (adapter.MINUTE_ID, 60), (adapter.MINUTE_ID, -1),
                           (adapter.MINUTE_ID, True), (adapter.MINUTE_ID, 30.0),
                           (adapter.MINUTE_ID, "30"), ([], 30)):
            with self.subTest(pid=pid, value=value), patch.object(self.platform.client, "_post") as post:
                with self.assertRaises(adapter.StartError):
                    self.platform.write(pid, value)
                post.assert_not_called()

    def test_write_never_retries_on_auth_business_or_transport_failure(self):
        for failed in (response(status=401), response(code=403, success=False),
                       response(status=500), response(code=500, success=False)):
            with patch.object(self.platform.client, "_post", return_value=failed) as post:
                result = self.platform.write(adapter.HOUR_ID, 14)
                self.assertFalse(result["accepted"])
                self.assertEqual(set(result), {"http_status", "accepted"})
                post.assert_called_once()
        with patch.object(self.platform.client, "_post", side_effect=SmartEnergyError("模拟超时")) as post:
            with self.assertRaises(SmartEnergyError):
                self.platform.write(adapter.MINUTE_ID, 30)
            post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
