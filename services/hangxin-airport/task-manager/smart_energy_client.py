"""航信平台只读历史客户端：自动登录，令牌只在当前进程内缓存。"""

from __future__ import annotations

import base64
from datetime import datetime
import json
import math
import os
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_LOGIN_PATH = "/api/UserMicro/User/UserLogin"
DEFAULT_HISTORY_PATH = "/api/GatewayMicro/Point/GetPointHistoryData"
# 平台公开登录公钥，和已验证的既有数据导出程序一致，不是账号凭据。
DEFAULT_LOGIN_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9yFhlGWb/RIbIL03vmRrC57kJ
g08nLLoxqeVU+/nQHxhmlvdfZlL7OJpg+BaKDJftwcdEBnQaawJRRQfIGPRlPJGn
muQQ/wGsfYaWFJaRrL9rYYAgPkTAePlbJJxRpkUxy2jEWt7zQrcMuIQbRRC7o8KV
tnBuCCvW+MEfcz23HQIDAQAB
-----END PUBLIC KEY-----"""


class SmartEnergyError(RuntimeError):
    """只携带安全的固定错误信息，不暴露上游响应或认证上下文。"""


class SmartEnergyClient:
    """按环境变量登录并读取能量历史，初始化不会登录或请求网络。"""

    def __init__(self, config: dict):
        """复制非敏感配置，校验地址和采样参数，建立线程安全缓存。"""
        self.config = dict(config)
        base_url = os.environ.get("XIAOMAI_BASE_URL", config.get("base_url", ""))
        if not isinstance(base_url, str):
            raise SmartEnergyError("平台地址必须为 HTTPS 字符串")
        parsed = urlsplit(base_url.strip())
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise SmartEnergyError("平台地址必须为有效且不含凭据的 HTTPS 地址")
        self.base_url = base_url.strip().rstrip("/")
        self.login_url = self._endpoint(config.get("login_path", DEFAULT_LOGIN_PATH))
        self.history_url = self._endpoint(config.get("history_path", DEFAULT_HISTORY_PATH))
        heads = config.get("heads", [])
        if not isinstance(heads, list) or not heads:
            raise SmartEnergyError("至少需要配置一个机头能量点位")
        self.point_ids = [head.get("point_id") if isinstance(head, dict) else None for head in heads]
        if any(not isinstance(point, str) or not point.strip() for point in self.point_ids):
            raise SmartEnergyError("机头能量点位 point_id 无效")
        if len(set(self.point_ids)) != len(self.point_ids):
            raise SmartEnergyError("机头能量点位不能重复")
        self.interval_minutes = config.get("interval_minutes", 2)
        if type(self.interval_minutes) is not int or self.interval_minutes <= 0:
            raise SmartEnergyError("采样间隔必须为正整数分钟")
        timeout = config.get("timeout_seconds", 10)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise SmartEnergyError("请求超时时间必须为正数")
        self.timeout_seconds = float(timeout)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.RLock()

    def _endpoint(self, path: str) -> str:
        """端点只允许同平台相对路径，防止认证被带到其他域名。"""
        if (not isinstance(path, str) or not path.startswith("/")
                or path.startswith("//") or urlsplit(path).scheme
                or urlsplit(path).netloc or urlsplit(path).fragment):
            raise SmartEnergyError("平台端点必须为同域相对路径")
        return self.base_url + path

    def _post(self, url: str, *, body: dict, headers: dict) -> requests.Response:
        """请求保留 TLS 校验并禁止跳转，传输异常不附带原始内容。"""
        try:
            return requests.post(
                url, json=body, headers=headers,
                timeout=self.timeout_seconds, allow_redirects=False,
            )
        except requests.RequestException:
            raise SmartEnergyError("平台连接失败或请求超时") from None

    @staticmethod
    def _json(response: requests.Response, operation: str) -> dict:
        """仅接受对象 JSON，避免错误正文或响应片段进入异常信息。"""
        try:
            payload = response.json()
        except (ValueError, requests.RequestException):
            raise SmartEnergyError(f"{operation}响应不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise SmartEnergyError(f"{operation}响应结构无效")
        return payload

    @staticmethod
    def _encrypted_password(password: str, public_key: str) -> str:
        """按平台 JSEncrypt 约定对密码作 RSA PKCS#1 v1.5 加密。"""
        try:
            key = serialization.load_pem_public_key(public_key.replace("\\n", "\n").encode())
        except (ValueError, TypeError, UnsupportedAlgorithm, AttributeError):
            raise SmartEnergyError("平台登录公钥无效") from None
        if not isinstance(key, rsa.RSAPublicKey):
            raise SmartEnergyError("平台登录公钥必须为 RSA 公钥")
        plaintext = password.encode("utf-8")
        if not plaintext or len(plaintext) > key.key_size // 8 - 11:
            raise SmartEnergyError("平台登录密码长度无效")
        return base64.b64encode(key.encrypt(plaintext, padding.PKCS1v15())).decode("ascii")

    @staticmethod
    def _expiry_hint(token: str, now: float) -> float:
        """JWT 的 exp 仅用于提前重登；不把未验签载荷当作授权依据。"""
        try:
            segments = token.split(".")
            if len(segments) != 3 or len(segments[1]) > 16384:
                return now + 1800
            raw = base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4))
            payload = json.loads(raw)
            expiry = payload.get("exp")
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                return now + 1800
            return float(expiry)
        except (ValueError, TypeError, AttributeError, UnicodeError):
            return now + 1800

    def _login(self) -> None:
        """每次重登重新读取环境变量，认证凭据和返回令牌均不落盘。"""
        account = os.environ.get("XIAOMAI_ACCOUNT", "").strip()
        password = os.environ.get("XIAOMAI_PASSWORD", "")
        if not account or not password:
            raise SmartEnergyError("平台自动登录尚未配置 XIAOMAI_ACCOUNT 和 XIAOMAI_PASSWORD")
        public_key = os.environ.get("XIAOMAI_PUBLIC_KEY") or DEFAULT_LOGIN_PUBLIC_KEY
        body = {
            "account": account,
            "pwd": self._encrypted_password(password, public_key),
            "loginClient": 0,
            "loginWay": 0,
        }
        response = self._post(
            self.login_url, body=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            if not 200 <= response.status_code < 300:
                raise SmartEnergyError(f"平台登录 HTTP 状态异常：{response.status_code}")
            payload = self._json(response, "平台登录")
            if payload.get("success") is not True or str(payload.get("code")) != "200":
                raise SmartEnergyError("平台自动登录失败，请检查认证配置或账号状态")
            data = payload.get("data")
            if not isinstance(data, dict) or data.get("isPasswordLocked"):
                raise SmartEnergyError("平台登录身份无效或账号已锁定")
            for field in ("userId", "companyId"):
                text = str(data.get(field, ""))
                if not text.isascii() or not text.isdecimal() or int(text) <= 0:
                    raise SmartEnergyError("平台登录响应缺少有效身份")
            token = requests.structures.CaseInsensitiveDict(response.headers).get("access-token", "").strip()
            if not token or any(char.isspace() for char in token):
                raise SmartEnergyError("平台登录响应缺少有效访问令牌")
            self._token = token
            self._token_expires_at = self._expiry_hint(token, time.time())
        finally:
            response.close()

    def fetch_history(self, start: datetime, end: datetime) -> dict[str, Any]:
        """读取指定北京时间范围的机头历史，认证失败只重新登录重试一次。"""
        if (not isinstance(start, datetime) or not isinstance(end, datetime)
                or start.tzinfo is None or end.tzinfo is None
                or start.utcoffset() is None or end.utcoffset() is None):
            raise SmartEnergyError("历史查询起止时间必须带有时区")
        local_start = start.astimezone(TIMEZONE)
        local_end = end.astimezone(TIMEZONE)
        if local_end <= local_start:
            raise SmartEnergyError("历史查询结束时间必须晚于开始时间")
        body = {
            "timeQuantum": {
                "startDate": local_start.strftime("%Y-%m-%d %H:%M"),
                "endDate": local_end.strftime("%Y-%m-%d %H:%M"),
            },
            "pointIdList": list(self.point_ids),
            "interval": self.interval_minutes,
        }
        with self._lock:
            if self._token is None or self._token_expires_at <= time.time() + 60:
                self._login()
            for attempt in range(2):
                response = self._post(
                    self.history_url, body=body,
                    headers={"Accept": "application/json", "Content-Type": "application/json",
                             "Authorization": f"Bearer {self._token}"},
                )
                try:
                    auth_failure = response.status_code in (401, 403)
                    if not auth_failure and not 200 <= response.status_code < 300:
                        raise SmartEnergyError(f"平台历史接口 HTTP 状态异常：{response.status_code}")
                    payload = None if auth_failure else self._json(response, "平台历史接口")
                    if payload is not None:
                        auth_failure = str(payload.get("code")) in {"401", "403"}
                    if auth_failure:
                        self._token, self._token_expires_at = None, 0.0
                        if attempt == 0:
                            self._login()
                            continue
                        raise SmartEnergyError("平台重新登录后仍未获准读取历史数据")
                    if (payload.get("success") is not True or str(payload.get("code")) != "200"
                            or not isinstance(payload.get("data"), list)):
                        raise SmartEnergyError("平台历史接口未成功返回有效数据")
                    return payload
                finally:
                    response.close()
        raise SmartEnergyError("平台历史查询失败")
