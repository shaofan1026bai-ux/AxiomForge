"""开机预测平台适配：普通写入仅限开机表，低温日专用写入仅限系统时控开关。"""

from __future__ import annotations

from datetime import datetime, timedelta
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
AHU_IDS = {1: 12783, 2: 12784, 4: 12786, 5: 12787}
DEVICE_IDS = [DEVICE_ID, *AHU_IDS.values()]
PREFIX = "nfdh9q_mscrk01_"
HOUR_ID = PREFIX + "t01"
MINUTE_ID = PREFIX + "t04"
CHILLERS = {12788: PREFIX, 12789: 'nfdh9q_mscrk02_'}
TIME_CONTROL_ID = 'ob24v7_mscdfan_s06'
TIME_CONTROL_SPEC = (12796, 's06', '系统时控启停按钮', '')
READ_PATH = "/api/GatewayMicro/Device/GetDevicePoint"
HISTORY_PATH = "/api/GatewayMicro/Point/GetPointHistoryData"
WRITE_PATH = (
    "/api/GatewayMicro/Point/CommonMqttWritePointData?isWriteHistoryData=false"
    "&operationNode=%E8%AE%BE%E5%A4%87%E8%AF%A6%E6%83%85%E6%89%8B%E5%8A%A8%E6%8E%A7%E5%88%B6"
)

# 设备名称、单位和标识同时检查，避免同名点或错误设备进入计算。
SPECS = {
    PREFIX + "c00": (DEVICE_ID, "c00", "主机运行反馈", ""),
    PREFIX + "c23": (DEVICE_ID, "c23", "主机启停输出", ""),
    PREFIX + "c22": (DEVICE_ID, "c22", "主机故障反馈", ""),
    PREFIX + "c21": (DEVICE_ID, "c21", "室外温度", "℃"),
}
for _ahu, _device in AHU_IDS.items():
    for _tag, _name, _unit in (
        ("airin", "回风温度", "℃"), ("v20", "变频器运行I", ""),
        ("v01", "变频器运行频率", "hz"), ("freshv", "新风阀开度", "%"),
    ):
        SPECS[f"ewcnad_msahu{_ahu}_{_tag}"] = (_device, _tag, _name, _unit)


class StartError(ValueError):
    """只包含固定错误说明，不能携带认证数据或原始平台正文。"""


def local_time(value):
    """拒绝无时区判断时间，所有时间边界统一为北京时间。"""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StartError("判断时间必须带有时区")
    return value.astimezone(ZONE)


def _stamp(value):
    """平台无时区原始时间按上海解释，不以请求时间替换。"""
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=ZONE) if result.tzinfo is None else result.astimezone(ZONE)
    except (TypeError, ValueError, OverflowError):
        raise StartError("设备采样时间无效") from None


def point_stamp(point):
    """返回设备自己的上报时间；缺失时不生成默认当前时间。"""
    if not isinstance(point, dict) or "updateTime" not in point:
        raise StartError("设备上报时间缺失")
    return _stamp(point["updateTime"])


def _numeric(value):
    """布尔、字符串、空值和非有限数均不能成为控制测量。"""
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise StartError("点位缺少有效数值")
    return value


def number(point, at, maximum=180):
    """读取新鲜原值；未知、过期和未来上报均不视为有效反馈。"""
    current = local_time(at)
    if not isinstance(point, dict):
        raise StartError("点位缺失")
    value = _numeric(point.get("value"))
    if not 0 <= (current - point_stamp(point)).total_seconds() <= maximum:
        raise StartError("设备上报时间过旧或晚于判断时间")
    return value


def _identity(point, pid, spec, *, writable=None):
    """校验点位所属项目、设备及物理含义，运行点还要求明确只读。"""
    device, tag, name, unit = spec
    if (not isinstance(point, dict) or point.get("pointId") != pid
            or point.get("deviceId") != device or point.get("projectId") != PROJECT_ID
            or point.get("identifierName") != tag or point.get("pointName") != name
            or point.get("unit") != unit
            or (writable is not None and point.get("isWrite") is not writable)):
        raise StartError("点位身份、单位或权限不符")


def assert_stopped(points, at):
    """仅在运行、启动输出及故障反馈均明确为0时允许更改开机表。"""
    if not isinstance(points, dict):
        raise StartError("当前点位结构无效")
    for tag in ("c00", "c23", "c22"):
        pid = PREFIX + tag
        point = points.get(pid)
        _identity(point, pid, SPECS[pid], writable=False)
        value = number(point, at)
        if value not in (0, 1):
            raise StartError("机组状态反馈必须为0或1")
        if value != 0:
            raise StartError("机组已运行、已启动或存在故障")


def eligible_ahus(points, at):
    """单台异常只排除该台；至少两台的组级要求由调用方落实。"""
    local_time(at)
    if not isinstance(points, dict):
        raise StartError("当前点位结构无效")
    eligible = []
    for ahu in AHU_IDS:
        try:
            values = {}
            for tag in ("airin", "v20", "v01", "freshv"):
                pid = f"ewcnad_msahu{ahu}_{tag}"
                point = points.get(pid)
                _identity(point, pid, SPECS[pid], writable=False)
                values[tag] = number(point, at)
            if (values["v20"] == 1 and 0 < values["v01"] <= 60
                    and 10 <= values["airin"] <= 45 and 0 <= values["freshv"] <= 100):
                eligible.append(ahu)
        except StartError:
            continue
    return eligible


def _schedule(points, at, hour_tag, minute_tag, label):
    current = local_time(at)
    if not isinstance(points, dict):
        raise StartError("当前点位结构无效")
    values = []
    for tag, name, upper in ((hour_tag, label + "_时", 23), (minute_tag, label + "_分", 59)):
        pid = PREFIX + tag
        point = points.get(pid)
        _identity(point, pid, (DEVICE_ID, tag, name, ""), writable=True)
        value = number(point, current)
        if int(value) != value or not 0 <= value <= upper:
            raise StartError("机组时间表字段超出合法范围")
        values.append(int(value))
    return current.replace(hour=values[0], minute=values[1], second=0, microsecond=0)


def read_schedule(points, at):
    """读取1#首组开机时分，返回判断当日的带时区时间。"""
    return _schedule(points, at, "t01", "t04", "开机时间设定1")


def off_time(points, at):
    """读取首组关机表以确定预测终点；本策略不修改关机表。"""
    return _schedule(points, at, "t11", "t14", "关机时间设定1")


def protected_schedules(points):
    """快照其余机组时间表；这些字段永远不会交给本模块写入。"""
    if not isinstance(points, dict):
        raise StartError("当前点位结构无效")
    return {pid: point.get("value") for pid, point in points.items()
            if isinstance(pid, str) and pid.startswith(PREFIX + "t")
            and pid not in (HOUR_ID, MINUTE_ID) and isinstance(point, dict)}


def build_payload(points, histories, at, original_start_at):
    """将历史原值与当前上报合并；缺测不补值，同刻冲突拒绝继续计算。"""
    current = local_time(at)
    if not isinstance(points, dict) or not isinstance(histories, dict):
        raise StartError("预测取数结构无效")
    if original_start_at is not None:
        if isinstance(original_start_at, str):
            try:
                original_start_at = datetime.fromisoformat(original_start_at)
            except (TypeError, ValueError):
                raise StartError("原计划开机时间无效") from None
        original_start_at = local_time(original_start_at).isoformat()
    merged = {}
    eligible = set(eligible_ahus(points, current))
    eligible_devices = {AHU_IDS[n] for n in eligible}
    for pid, spec in SPECS.items():
        # 当前停机、过期或身份异常的AHU不能被几分钟前的历史运行值重新纳入。
        if spec[0] in AHU_IDS.values() and spec[0] not in eligible_devices:
            merged[pid] = []
            continue
        rows = histories.get(pid, [])
        if not isinstance(rows, list):
            raise StartError("预测历史序列无效")
        samples = {}
        for row in rows:
            if not isinstance(row, dict) or "at" not in row:
                raise StartError("预测历史样本无效")
            stamp = _stamp(row["at"])
            if stamp >= current or row.get("value") is None:
                continue
            value = _numeric(row["value"])
            if stamp in samples and samples[stamp] != value:
                raise StartError("预测样本同刻数值冲突")
            samples[stamp] = value
        point = points.get(pid)
        if point is not None:
            _identity(point, pid, spec, writable=False)
            stamp = point_stamp(point)
            raw = point.get("value")
            if stamp < current and raw is not None:
                value = _numeric(raw)
                if stamp in samples and samples[stamp] != value:
                    raise StartError("预测样本同刻数值冲突")
                samples[stamp] = value
        merged[pid] = [{"at": stamp.isoformat(), "value": value}
                       for stamp, value in sorted(samples.items())]
    return {
        "evaluated_at": current.isoformat(),
        "original_schedule_start_at": original_start_at,
        "flow_normal": None,
        "circulation_confirmed": None,
        "series": {name: merged[PREFIX + tag] for name, tag in (
            ("outdoor", "c21"), ("fault", "c22"),
            ("running", "c00"), ("start_output", "c23"),
        )},
        "ahus": [{"ahu": ahu, **{
            name: merged[f"ewcnad_msahu{ahu}_{tag}"] for name, tag in (
                ("return", "airin"), ("running", "v20"),
                ("frequency", "v01"), ("fresh", "freshv"),
            )}} for ahu in AHU_IDS],
    }


class Platform:
    """独立限定读取范围和写入字段，不继承其他控制器的温度写入能力。"""

    def __init__(self, config_path=None):
        """认证从进程环境注入；构造时既不登录也不发出请求。"""
        path = Path(config_path) if config_path is not None else ROOT / "load_forecast_config.json"
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raise StartError("平台非敏感配置无法读取") from None
        if not isinstance(config, dict):
            raise StartError("平台非敏感配置结构无效")
        self.client = SmartEnergyClient(config)
        if self.client.base_url != BASE_URL:
            raise StartError("开机预测只允许连接已授权航信平台")

    def _headers(self):
        """只在请求前刷新内存令牌，不将认证信息放入任何返回值。"""
        if self.client._token is None or self.client._token_expires_at <= time.time() + 60:
            self.client._login()
        return {"Accept": "application/json", "Content-Type": "application/json",
                "Authorization": "Bearer " + self.client._token}

    def _read_request(self, path, body):
        """只读接口遇认证过期最多重登一次，其他失败直接停止本轮。"""
        if path not in (READ_PATH, HISTORY_PATH):
            raise StartError("拒绝非只读查询端点")
        for attempt in range(2):
            response = self.client._post(self.client._endpoint(path), body=body, headers=self._headers())
            try:
                if response.status_code in (401, 403):
                    payload, expired = None, True
                else:
                    payload = self.client._json(response, "开机预测只读查询")
                    expired = str(payload.get("code")) in {"401", "403"}
                if expired:
                    self.client._token = None
                    if attempt == 0:
                        continue
                    raise SmartEnergyError("开机预测只读认证失败")
                if (response.status_code != 200 or payload.get("success") is not True
                        or str(payload.get("code")) != "200" or not isinstance(payload.get("data"), list)):
                    raise SmartEnergyError("开机预测只读查询失败")
                return payload["data"]
            finally:
                response.close()
        raise SmartEnergyError("开机预测只读查询失败")

    def read(self):
        """读取固定5台设备；拒绝混入其他项目、设备、重复或错配点位。"""
        rows = self._read_request(READ_PATH, {"deviceIdList": DEVICE_IDS, "isActivate": True})
        points = {}
        prefixes = {DEVICE_ID: PREFIX, **{did: f"ewcnad_msahu{ahu}_" for ahu, did in AHU_IDS.items()}}
        for point in rows:
            if not isinstance(point, dict):
                raise StartError("当前点位含无效记录")
            device, pid = point.get("deviceId"), point.get("pointId")
            if (type(device) is not int or device not in prefixes or point.get("projectId") != PROJECT_ID
                    or not isinstance(pid, str) or not pid.startswith(prefixes[device]) or pid in points):
                raise StartError("当前点位含未知设备或标识重复错配")
            points[pid] = point
        return points

    def history(self, at):
        """一次读取20点的120分钟历史，水温不参与开机预测，空样本不补零。"""
        current = local_time(at)
        minute = current.replace(second=0, microsecond=0)
        start, end = minute - timedelta(minutes=120), minute + timedelta(minutes=1)
        rows = self._read_request(HISTORY_PATH, {
            "timeQuantum": {"startDate": start.strftime("%Y-%m-%d %H:%M"),
                            "endDate": end.strftime("%Y-%m-%d %H:%M")},
            "pointIdList": list(SPECS), "interval": 1,
        })
        histories = {pid: [] for pid in SPECS}
        seen = set()
        for series in rows:
            if not isinstance(series, dict):
                raise StartError("历史点位序列无效")
            pid = series.get("pointId")
            if not isinstance(pid, str) or pid not in SPECS or pid in seen:
                raise StartError("历史点位含未知或重复序列")
            seen.add(pid)
            _identity(series, pid, SPECS[pid])
            values = series.get("pointDataList")
            if values is None:
                values = []
            if not isinstance(values, list):
                raise StartError("历史点位缺少样本数组")
            samples = {}
            for row in values:
                if not isinstance(row, dict) or "ts" not in row:
                    raise StartError("历史样本结构无效")
                stamp = _stamp(row["ts"])
                if not start <= stamp < end:
                    raise StartError("历史样本时间超出请求范围")
                if row.get("val") is None:
                    continue
                value = _numeric(row["val"])
                if stamp in samples and samples[stamp] != value:
                    raise StartError("历史样本同刻数值冲突")
                samples[stamp] = value
            histories[pid] = [{"at": stamp.isoformat(), "value": value}
                              for stamp, value in sorted(samples.items())]
        return histories

    def read_time_control(self):
        """独立读取时控及两台主机；不扩充普通预测的AHU或历史查询范围。"""
        prefixes = {**CHILLERS, 12796: 'ob24v7_mscdfan_'}
        rows = self._read_request(READ_PATH, {'deviceIdList': list(prefixes), 'isActivate': True})
        points = {}
        for point in rows:
            if not isinstance(point, dict):raise StartError('时控上下文含无效记录')
            device,pid=point.get('deviceId'),point.get('pointId')
            if (type(device)is not int or device not in prefixes or point.get('projectId')!=PROJECT_ID
                or not isinstance(pid,str) or not pid.startswith(prefixes[device]) or pid in points):
                raise StartError('时控上下文含未知或重复设备点位')
            points[pid]=point
        return points

    def write(self, pid, value):
        """仅允许首组开机时分单点写入；响应未知时也不自动重复发送。"""
        upper = {HOUR_ID: 23, MINUTE_ID: 59}.get(pid) if isinstance(pid, str) else None
        if upper is None or type(value) is not int or not 0 <= value <= upper:
            raise StartError("拒绝授权范围之外的开机时间下发")
        return self._write_schedule_field(pid, value)

    def write_time_control(self, pid, value):
        """仅允许已核实的时控点写整数0/1，不允许清零任何时间表或写一键启停。"""
        if pid!=TIME_CONTROL_ID or type(value)is not int or value not in (0,1):
            raise StartError('拒绝系统时控开关以外的低温日下发')
        return self._write_schedule_field(pid, value)

    def _write_schedule_field(self, pid, value):
        """单点请求沿用同一认证、无重试和业务确认规则。"""
        response = self.client._post(self.client._endpoint(WRITE_PATH),
                                     body={pid: value}, headers=self._headers())
        try:
            payload = self.client._json(response, "开机时间单点下发")
            return {"http_status": response.status_code,
                    "accepted": response.status_code == 200 and payload.get("success") is True
                    and str(payload.get("code")) == "200"}
        finally:
            response.close()
