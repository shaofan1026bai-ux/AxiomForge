"""小麦只读连接底层；单次认证、截止与脱敏响应供多个专题共用。"""

import base64
import json
import os
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import truststore
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dotenv import dotenv_values

BASE_URL = "https://smart-energy.yntravelsky.net.cn:36443"
LOGIN_PATH = "/api/UserMicro/User/UserLogin"
PROJECT_PATH = "/api/UserMicro/UserSubarea/GetUserOwnProject"
POINT_PATH = "/api/GatewayMicro/Device/GetDevicePoint"
HISTORY_PATH = "/api/GatewayMicro/Point/GetPointHistoryData"
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9yFhlGWb/RIbIL03vmRrC57kJ
g08nLLoxqeVU+/nQHxhmlvdfZlL7OJpg+BaKDJftwcdEBnQaawJRRQfIGPRlPJGn
muQQ/wGsfYaWFJaRrL9rYYAgPkTAePlbJJxRpkUxy2jEWt7zQrcMuIQbRRC7o8KV
tnBuCCvW+MEfcz23HQIDAQAB
-----END PUBLIC KEY-----"""


class XiaomaiSourceError(ValueError):
    """可安全显示给用户的错误；不包含原始响应或底层异常。"""


@dataclass(frozen=True)
class Settings:
    account: str = field(repr=False)
    password: str = field(repr=False)
    public_key: str = field(default=PUBLIC_KEY, repr=False)


def load_settings() -> Settings:
    path = os.environ.get("AXIOMFORGE_XIAOMAI_ENV_FILE", "")
    if path and not Path(path).expanduser().is_file():
        raise XiaomaiSourceError("小麦配置文件不可读，请检查服务启动配置。")
    values = dotenv_values(Path(path).expanduser(), interpolate=False) if path else {}
    settings = {**{k: v for k, v in values.items() if v is not None}, **os.environ}
    account = settings.get("XIAOMAI_ACCOUNT", "")
    password = settings.get("XIAOMAI_PASSWORD", "")
    if not account or not password:
        raise XiaomaiSourceError("尚未配置小麦账号，请在服务端指定外部连接配置。")
    url = urlsplit(settings.get("XIAOMAI_LOGIN_URL", BASE_URL + LOGIN_PATH))
    if (
        url.scheme != "https"
        or url.hostname != "smart-energy.yntravelsky.net.cn"
        or url.port != 36443
        or url.path != LOGIN_PATH
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise XiaomaiSourceError("小麦登录地址必须使用已登记的航信域名、端口和登录路径。")
    return Settings(
        account, password, settings.get("XIAOMAI_PUBLIC_KEY", PUBLIC_KEY).replace("\\n", "\n")
    )


def encrypt_password(password: str, public_key: str) -> str:
    try:
        key = serialization.load_pem_public_key(public_key.encode())
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError
        return base64.b64encode(key.encrypt(password.encode(), padding.PKCS1v15())).decode()
    except Exception:
        raise XiaomaiSourceError("小麦密码加密失败，请检查公钥及密码长度。") from None


def _id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise XiaomaiSourceError("小麦响应结构异常，原快照已保留。")
    return value


class RegisteredPoint(Protocol):
    @property
    def point_id(self) -> str: ...
    @property
    def device_id(self) -> int: ...
    @property
    def unit(self) -> str: ...


def matches_point(row: dict[str, Any], spec: RegisteredPoint, project_id: int) -> bool:
    return (
        row.get("pointId") == spec.point_id
        and row.get("projectId") == project_id
        and row.get("deviceId") == spec.device_id
        and row.get("unit") == spec.unit
    )


class XiaomaiClient:
    """调用者拥有HTTP上下文与总截止；不缓存账号或令牌到全局状态。"""

    def __init__(
        self,
        client: httpx.Client,
        deadline: float,
        seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client, self.deadline, self.seconds, self.clock = client, deadline, seconds, clock
        self.token = ""

    def check_deadline(self) -> None:
        if self.clock() > self.deadline:
            raise XiaomaiSourceError(f"小麦查询超过{self.seconds}秒，请稍后手动重试。")

    def post(
        self, path: str, body: dict[str, Any], *, authenticated: bool = True
    ) -> tuple[dict[str, Any], httpx.Headers]:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise XiaomaiSourceError(f"小麦查询超过{self.seconds}秒，请稍后手动重试。")
        headers = {"Authorization": f"Bearer {self.token}"} if authenticated else {}
        with self.client.stream(
            "POST", BASE_URL + path, json=body, headers=headers, timeout=min(15, remaining)
        ) as response:
            if response.status_code in {401, 403}:
                raise XiaomaiSourceError("小麦认证或权限校验失败，请检查账号后手动刷新。")
            if response.status_code != 200:
                raise XiaomaiSourceError("小麦服务请求失败，原快照已保留。")
            content = bytearray()
            for chunk in response.iter_bytes():
                self.check_deadline()
                content.extend(chunk)
                if len(content) > 4_000_000:
                    raise XiaomaiSourceError("小麦单段响应超出预期，请缩小时间范围。")
            payload = _mapping(json.loads(content))
            if payload.get("success") is not True or payload.get("code") != 200:
                raise XiaomaiSourceError("小麦业务查询未成功，请检查账号和接口状态。")
            return payload, response.headers

    def authenticate(self, settings: Settings, project_id: int) -> None:
        login, headers = self.post(
            LOGIN_PATH,
            {
                "account": settings.account,
                "pwd": encrypt_password(settings.password, settings.public_key),
                "loginClient": 0,
                "loginWay": 0,
            },
            authenticated=False,
        )
        user = _mapping(login.get("data"))
        if not _id(user.get("userId")) or not _id(user.get("companyId")):
            raise XiaomaiSourceError("小麦登录未返回有效身份。")
        token = headers.get("Access-Token", "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise XiaomaiSourceError("小麦登录响应缺少用户凭证。")
        self.token = token
        owned, _ = self.post(
            PROJECT_PATH, {"userId": user["userId"], "companyId": user["companyId"]}
        )
        projects = owned.get("data")
        if not isinstance(projects, list):
            raise XiaomaiSourceError("小麦项目权限响应结构异常。")
        if not any(
            isinstance(row, dict) and row.get("projectId") == project_id for row in projects
        ):
            raise XiaomaiSourceError("该小麦账号无芒市机场项目权限。")

    def validate_points(self, points: Sequence[RegisteredPoint], project_id: int) -> None:
        metadata, _ = self.post(
            POINT_PATH,
            {"deviceIdList": list(dict.fromkeys(p.device_id for p in points)), "isActivate": True},
        )
        values = metadata.get("data")
        if not isinstance(values, list):
            raise XiaomaiSourceError("小麦测点字典响应结构异常。")
        for point in points:
            matches = [
                row
                for row in values
                if isinstance(row, dict) and row.get("pointId") == point.point_id
            ]
            if len(matches) != 1 or not matches_point(matches[0], point, project_id):
                raise XiaomaiSourceError("小麦目标测点的归属或单位与登记不一致。")

    def history(
        self,
        points: Sequence[RegisteredPoint],
        project_id: int,
        start: str,
        end: str,
        interval: int,
    ) -> dict[str, list[dict[str, object]]]:
        history, _ = self.post(
            HISTORY_PATH,
            {
                "timeQuantum": {"startDate": start, "endDate": end},
                "indicatorNoList": [],
                "pointIdList": [p.point_id for p in points],
                "interval": interval,
            },
        )
        data = history.get("data")
        if not isinstance(data, list):
            raise XiaomaiSourceError("小麦历史响应结构异常。")
        registered = {p.point_id: p for p in points}
        result: dict[str, list[dict[str, object]]] = {}
        for value in data:
            row = _mapping(value)
            pid = row.get("pointId")
            if (
                not isinstance(pid, str)
                or pid not in registered
                or pid in result
                or not matches_point(row, registered[pid], project_id)
            ):
                raise XiaomaiSourceError("小麦历史测点、归属或单位与登记不一致。")
            records = row.get("pointDataList")
            if not isinstance(records, list) or any(
                not isinstance(record, dict) for record in records
            ):
                raise XiaomaiSourceError("小麦历史数值结构异常。")
            result[pid] = records
        return result


def http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.Client(verify=context, transport=transport, follow_redirects=False)
