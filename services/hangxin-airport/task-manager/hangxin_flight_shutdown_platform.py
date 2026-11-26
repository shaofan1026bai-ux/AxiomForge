"""航班联动关机适配：仅操作1#第一组晚间候选关机时分。"""

from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from smart_energy_client import SmartEnergyClient, SmartEnergyError


ZONE = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
BASE_URL = "https://smart-energy.yntravelsky.net.cn:36443"
PROJECT_ID = 832
DEVICE_ID = 12788
PREFIX = "nfdh9q_mscrk01_"
HOUR_ID = PREFIX + "t11"
MINUTE_ID = PREFIX + "t14"
TARGETS = {HOUR_ID: {18,19,20}, MINUTE_ID: {0,30}}
READ_PATH = "/api/GatewayMicro/Device/GetDevicePoint"
# 复用已经验证的设备详情单点写入端点，查询参数不是模拟开关。
WRITE_PATH = (
    "/api/GatewayMicro/Point/CommonMqttWritePointData?isWriteHistoryData=false"
    "&operationNode=%E8%AE%BE%E5%A4%87%E8%AF%A6%E6%83%85%E6%89%8B%E5%8A%A8%E6%8E%A7%E5%88%B6"
)
SPECS = {
    "c00": "主机运行反馈",
    "c23": "主机启停输出",
    "c22": "主机故障反馈",
}


class ShutdownError(ValueError):
    """只携带固定错误，不序列化平台正文或认证上下文。"""


def local_time(value):
    """执行边界必须带时区，不能受服务器系统时区影响。"""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ShutdownError("判断时间必须带有时区")
    return value.astimezone(ZONE)


def point_stamp(point):
    """解析设备实际上报时间，不以请求完成时间刷新旧值。"""
    try:
        result = datetime.fromisoformat(point["updateTime"])
        return result.replace(tzinfo=ZONE) if result.tzinfo is None else result.astimezone(ZONE)
    except (TypeError, KeyError, ValueError, OverflowError):
        raise ShutdownError("设备上报时间无效") from None


def number(point, at, maximum=180):
    """仅接收新鲜有限数字，布尔和缺测不能被当作0/1。"""
    at = local_time(at)
    if not isinstance(point, dict):
        raise ShutdownError("点位缺失")
    value = point.get("value")
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise ShutdownError("点位缺少有效数值")
    if not 0 <= (at - point_stamp(point)).total_seconds() <= maximum:
        raise ShutdownError("设备上报时间过旧或晚于判断时间")
    return value


def _identity(point, pid, tag, name, writable):
    """校验所属项目、设备、单位及物理含义，避免同名点误写。"""
    if (not isinstance(point, dict) or point.get("pointId") != pid
            or type(point.get("deviceId")) is not int or point.get("deviceId") != DEVICE_ID
            or type(point.get("projectId")) is not int or point.get("projectId") != PROJECT_ID
            or point.get("identifierName") != tag or point.get("pointName") != name
            or point.get("unit") != "" or point.get("isWrite") is not writable):
        raise ShutdownError("点位身份、单位或权限不符")


def _schedule(points, at, hour_tag, minute_tag, label):
    """以当日日期解释首组时分，拒绝空值、非整数及过期时间表。"""
    current = local_time(at)
    if not isinstance(points, dict):
        raise ShutdownError("当前点位结构无效")
    values = []
    for tag, suffix, upper in ((hour_tag, "_时", 23), (minute_tag, "_分", 59)):
        pid = PREFIX + tag
        point = points.get(pid)
        _identity(point, pid, tag, label + suffix, True)
        value = number(point, current)
        if int(value) != value or not 0 <= value <= upper:
            raise ShutdownError("时间表字段超出合法范围")
        values.append(int(value))
    return current.replace(hour=values[0], minute=values[1], second=0, microsecond=0)


def read_off_schedule(points, at):
    """读取1#第一组关机时分，不授权写入任何其他时段。"""
    return _schedule(points, at, "t11", "t14", "关机时间设定1")


def read_start_schedule(points, at):
    """读取首组开机时间用于确认本日顺序，该字段只读不写。"""
    return _schedule(points, at, "t01", "t04", "开机时间设定1")


def validate_running(points, at):
    """运行与输出必须一致、故障为0；未知或正在切换不能当作稳定运行。"""
    if not isinstance(points, dict):
        raise ShutdownError("当前点位结构无效")
    values = {}
    for tag, name in SPECS.items():
        pid = PREFIX + tag
        point = points.get(pid)
        _identity(point, pid, tag, name, False)
        value = number(point, at)
        if value not in (0, 1):
            raise ShutdownError("机组状态反馈必须为0或1")
        values[tag] = value
    if values["c22"] != 0:
        raise ShutdownError("机组故障反馈非0，停止自动改表")
    if values["c00"] != values["c23"]:
        raise ShutdownError("机组运行与启停输出不一致，停止自动改表")
    return values["c00"] == 1


def protected_schedules(points):
    """保护开机、其他时段等时间表，泵延时不在本适配器读取或控制范围。"""
    if not isinstance(points, dict):
        raise ShutdownError("当前点位结构无效")
    return {pid: point.get("value") for pid, point in points.items()
            if isinstance(pid, str) and pid.startswith(PREFIX + "t")
            and pid not in TARGETS and isinstance(point, dict)}


class Platform:
    """独立收窄设备和写入范围，不继承开机或温调控制器能力。"""

    def __init__(self, config_path=None):
        """构造时不登录，凭据只从既有服务进程环境读取。"""
        path = Path(config_path) if config_path is not None else ROOT / "load_forecast_config.json"
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raise ShutdownError("平台非敏感配置无法读取") from None
        if not isinstance(config, dict):
            raise ShutdownError("平台非敏感配置结构无效")
        self.client = SmartEnergyClient(config)
        if self.client.base_url != BASE_URL:
            raise ShutdownError("关机策略只允许连接已授权航信平台")

    def _headers(self, *, refresh=True):
        """登录放在只读或显式预备阶段，设备POST不会隐式重登或重试。"""
        if refresh and (self.client._token is None or self.client._token_expires_at <= time.time() + 60):
            self.client._login()
        if self.client._token is None or self.client._token_expires_at <= time.time():
            raise SmartEnergyError("设备下发前认证尚未准备或已经过期")
        return {"Accept": "application/json", "Content-Type": "application/json",
                "Authorization": "Bearer " + self.client._token}

    def prepare_write(self):
        """只准备认证，调用方之后仍须紧邻设备POST重新校验时间及反馈。"""
        self._headers()

    def read(self):
        """仅读取1#主机；认证过期最多重登一次，拒绝混入其他设备或重复点。"""
        for attempt in range(2):
            response = self.client._post(
                self.client._endpoint(READ_PATH), body={"deviceIdList": [DEVICE_ID], "isActivate": True},
                headers=self._headers(),
            )
            try:
                if response.status_code in (401, 403):
                    payload, expired = None, True
                else:
                    payload = self.client._json(response, "关机策略只读查询")
                    expired = str(payload.get("code")) in {"401", "403"}
                if expired:
                    self.client._token = None
                    if attempt == 0:
                        continue
                    raise SmartEnergyError("关机策略只读认证失败")
                if (response.status_code != 200 or payload.get("success") is not True
                        or str(payload.get("code")) != "200" or not isinstance(payload.get("data"), list)):
                    raise SmartEnergyError("关机策略只读查询失败")
                points = {}
                for point in payload["data"]:
                    if not isinstance(point, dict):
                        raise ShutdownError("当前点位含无效记录")
                    pid = point.get("pointId")
                    if (type(point.get("deviceId")) is not int or point.get("deviceId") != DEVICE_ID
                            or type(point.get("projectId")) is not int or point.get("projectId") != PROJECT_ID
                            or not isinstance(pid, str) or not pid.startswith(PREFIX) or pid in points):
                        raise ShutdownError("当前点位含未知设备或标识重复错配")
                    points[pid] = point
                return points
            finally:
                response.close()
        raise SmartEnergyError("关机策略只读查询失败")

    def write(self, pid, value):
        """小时仅18—20，分钟仅0或30；完整候选及安全中间组合由控制器核验。"""
        if not isinstance(pid, str) or pid not in TARGETS or type(value) is not int or value not in TARGETS[pid]:
            raise ShutdownError("拒绝授权范围之外的关机时间下发")
        response = self.client._post(self.client._endpoint(WRITE_PATH),
                                     body={pid: value}, headers=self._headers(refresh=False))
        try:
            payload = self.client._json(response, "关机时间单点下发")
            return {"http_status": response.status_code,
                    "accepted": response.status_code == 200 and payload.get("success") is True
                    and str(payload.get("code")) == "200"}
        finally:
            response.close()
