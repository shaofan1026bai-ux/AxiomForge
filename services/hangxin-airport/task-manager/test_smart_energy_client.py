"""验证平台认证、重登、请求契约和敏感信息保护，不调用真实接口。"""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import traceback
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from smart_energy_client import SmartEnergyClient, SmartEnergyError


TZ = ZoneInfo("Asia/Shanghai")
CONFIG = {
    "base_url": "https://example.invalid",
    "heads": [{"point_id": "head_one"}, {"point_id": "head_two"}],
    "interval_minutes": 2,
    "timeout_seconds": 10,
}
START = datetime(2026, 10, 29, tzinfo=TZ)
END = START + timedelta(days=1)
HISTORY = {"success": True, "code": 200, "data": []}


def response(payload=None, *, status=200, token=None):
    """构造驻留内存的响应，模拟真实 JSON 和不区分大小写的响应头。"""
    item = requests.Response()
    item.status_code = status
    item._content = json.dumps(payload if payload is not None else HISTORY).encode()
    item._content_consumed = True
    if token is not None:
        item.headers["Access-Token"] = token
    return item


def login_response(token="test-memory-access-token"):
    """提供满足现有登录契约的最小身份与响应头。"""
    return response({"success": True, "code": 200, "data": {"userId": 1, "companyId": 2}}, token=token)


def jwt_with_exp(expiry):
    """构造只用于刷新时机测试的未签名示例，不承担认证校验。"""
    part = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return "example." + part + ".signature"


class SmartEnergyClientTests(unittest.TestCase):
    """覆盖认证状态切换和只读历史接口失败边界。"""

    @classmethod
    def setUpClass(cls):
        """生成测试公钥，便于解密验证密码字段而不涉及真实凭据。"""
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def setUp(self):
        """隔离测试环境中的认证配置，不读取调用者的任何凭据值。"""
        self.env = patch.dict(os.environ, {
            "XIAOMAI_ACCOUNT": " example-account ",
            "XIAOMAI_PASSWORD": " 密码 ${literal} # ",
            "XIAOMAI_PUBLIC_KEY": self.public_key,
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = SmartEnergyClient(CONFIG)

    def test_constructor_does_not_login_and_missing_credentials_fail_on_call(self):
        """缺凭据不会导致初始化取数，真正调用时才给出明确配置错误。"""
        with patch.dict(os.environ, {}, clear=True), patch("smart_energy_client.requests.post") as post:
            client = SmartEnergyClient(CONFIG)
            self.assertIsNone(client._token)
            post.assert_not_called()
            with self.assertRaisesRegex(SmartEnergyError, "XIAOMAI_ACCOUNT"):
                client.fetch_history(START, END)
            post.assert_not_called()

    def test_rsa_login_request_contract_and_history_timezone(self):
        """账号按约定处理，密码加密且保留空格，历史时间统一转北京时间。"""
        with patch("smart_energy_client.requests.post", side_effect=[login_response(), response()]) as post:
            result = self.client.fetch_history(START.astimezone(timezone.utc), END.astimezone(timezone.utc))
        self.assertEqual(result, HISTORY)
        login, history = post.call_args_list
        self.assertEqual(login.args[0], "https://example.invalid/api/UserMicro/User/UserLogin")
        body = login.kwargs["json"]
        self.assertEqual(body["account"], "example-account")
        plaintext = self.private_key.decrypt(base64.b64decode(body["pwd"]), padding.PKCS1v15()).decode()
        self.assertEqual(plaintext, " 密码 ${literal} # ")
        self.assertEqual(body["loginClient"], 0)
        self.assertEqual(body["loginWay"], 0)
        self.assertNotIn("Authorization", login.kwargs["headers"])
        for call in (login, history):
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["timeout"], 10)
            self.assertNotIn("verify", call.kwargs)
        self.assertEqual(history.kwargs["headers"]["Authorization"], "Bearer test-memory-access-token")
        self.assertEqual(history.kwargs["json"], {
            "timeQuantum": {"startDate": "2026-10-29 00:00", "endDate": "2026-10-30 00:00"},
            "pointIdList": ["head_one", "head_two"], "interval": 2,
        })

    def test_token_is_cached_between_history_calls(self):
        """同一进程内的有效令牌供后续请求复用，不重复登录。"""
        with patch.object(self.client, "_post", side_effect=[login_response(), response(), response()]) as post:
            self.client.fetch_history(START, END)
            self.client.fetch_history(START, END)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sum(c.args[0] == self.client.login_url for c in post.call_args_list), 1)

    def test_jwt_near_expiry_is_renewed_before_request(self):
        """读取 exp 仅驱动重登，过期前60秒不继续复用旧令牌。"""
        old, new = jwt_with_exp(1100), jwt_with_exp(3000)
        with patch.object(self.client, "_post", side_effect=[login_response(old), response(), login_response(new), response()]) as post:
            with patch("smart_energy_client.time.time", return_value=1000):
                self.client.fetch_history(START, END)
            with patch("smart_energy_client.time.time", return_value=1041):
                self.client.fetch_history(START, END)
        self.assertEqual(post.call_count, 4)
        self.assertEqual(post.call_args_list[-1].kwargs["headers"]["Authorization"], "Bearer " + new)

    def test_401_and_business_auth_failure_relogin_once(self):
        """HTTP与业务认证失败均只允许一次重登，不无限重试。"""
        for failed in [response(status=401), response({"success": False, "code": 401, "message": "secret-response"})]:
            with self.subTest(status=failed.status_code):
                client = SmartEnergyClient(CONFIG)
                with patch.object(client, "_post", side_effect=[login_response("old"), failed, login_response("new"), response()]) as post:
                    self.assertEqual(client.fetch_history(START, END), HISTORY)
                self.assertEqual(post.call_count, 4)
                self.assertEqual(post.call_args_list[-1].kwargs["headers"]["Authorization"], "Bearer new")
        client = SmartEnergyClient(CONFIG)
        with patch.object(client, "_post", side_effect=[login_response(), response(status=401), login_response(), response(status=401)]) as post:
            with self.assertRaisesRegex(SmartEnergyError, "仍未获准"):
                client.fetch_history(START, END)
        self.assertEqual(post.call_count, 4)
        self.assertIsNone(client._token)

    def test_ordinary_failure_is_not_retried(self):
        """普通业务或服务器错误直接返回固定错误，不触发重复登录。"""
        for failed in [response(status=500), response({"success": False, "code": 400, "message": "secret-response"})]:
            with self.subTest(status=failed.status_code):
                client = SmartEnergyClient(CONFIG)
                with patch.object(client, "_post", side_effect=[login_response(), failed]) as post:
                    with self.assertRaises(SmartEnergyError) as captured:
                        client.fetch_history(START, END)
                self.assertEqual(post.call_count, 2)
                self.assertNotIn("secret-response", str(captured.exception))

    def test_error_tracebacks_never_echo_credentials_or_upstream_body(self):
        """传输、非JSON、跳转和拒绝登录的异常堆栈均不回显敏感正文。"""
        failures = [
            requests.Timeout("private-secret-credential"),
            response({"success": False, "code": 401, "message": "private-secret-credential"}),
            response(status=307),
        ]
        malformed = response()
        malformed._content = b"private-secret-credential"
        failures.append(malformed)
        for failure in failures:
            with self.subTest(failure_type=type(failure).__name__):
                client = SmartEnergyClient(CONFIG)
                effect = failure if isinstance(failure, Exception) else [failure]
                with patch("smart_energy_client.requests.post", side_effect=effect):
                    with self.assertRaises(SmartEnergyError) as captured:
                        client.fetch_history(START, END)
                rendered = "".join(traceback.format_exception(captured.exception))
                self.assertNotIn("private-secret-credential", rendered)
                self.assertNotIn("example-account", rendered)
                self.assertNotIn("${literal}", rendered)

    def test_login_rejects_missing_token_locked_user_and_invalid_identity(self):
        """认证响应必须同时满足身份、公钥登录状态和响应头令牌契约。"""
        cases = [
            response({"success": True, "code": 200, "data": {"userId": 1, "companyId": 2}}),
            response({"success": True, "code": 200, "data": {"userId": 1, "companyId": 2, "isPasswordLocked": True}}, token="token"),
            response({"success": True, "code": 200, "data": {"userId": True, "companyId": 2}}, token="token"),
            login_response("Bearer token-with-prefix"),
        ]
        for item in cases:
            with self.subTest(status=item.status_code):
                client = SmartEnergyClient(CONFIG)
                with patch.object(client, "_post", return_value=item):
                    with self.assertRaises(SmartEnergyError):
                        client.fetch_history(START, END)
                self.assertIsNone(client._token)

    def test_threaded_calls_share_one_login(self):
        """并发读请求共享一次认证，避免同时重登造成令牌相互失效。"""
        calls = []

        def post(url, **kwargs):
            """按路径返回内存响应并计数，不执行网络请求。"""
            calls.append(url)
            return login_response() if url == self.client.login_url else response()

        with patch.object(self.client, "_post", side_effect=post):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.client.fetch_history(START, END), range(8)))
        self.assertTrue(all(item == HISTORY for item in results))
        self.assertEqual(calls.count(self.client.login_url), 1)
        self.assertEqual(calls.count(self.client.history_url), 8)

    def test_configuration_rejects_external_endpoints_and_honors_base_environment(self):
        """环境变量可覆盖平台地址，端点不允许跨域或通过明文HTTP登录。"""
        with patch.dict(os.environ, {"XIAOMAI_BASE_URL": "https://override.invalid"}):
            client = SmartEnergyClient(CONFIG)
            self.assertTrue(client.history_url.startswith("https://override.invalid/"))
        for override in [{"base_url": "http://example.invalid"}, {"login_path": "//other.invalid/login"}, {"history_path": "https://other.invalid/history"}]:
            with self.subTest(override=override):
                with self.assertRaises(SmartEnergyError):
                    SmartEnergyClient({**CONFIG, **override})

    def test_invalid_time_range_is_rejected_without_network(self):
        """时间必须带时区且范围递增，错误输入不触发登录。"""
        with patch.object(self.client, "_post") as post:
            for begin, end in [(START.replace(tzinfo=None), END), (START, START), (END, START)]:
                with self.subTest(begin=begin):
                    with self.assertRaises(SmartEnergyError):
                        self.client.fetch_history(begin, end)
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
