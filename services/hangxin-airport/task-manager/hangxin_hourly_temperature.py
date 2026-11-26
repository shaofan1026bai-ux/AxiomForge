"""1#冷机每半小时按1F运行AHU最高回风调节本地出水设定，默认只预览。

只写设备12788的c17，范围7.5～11℃。执行依赖现有策略的本地生效开关；
仅在新鲜的主机运行反馈为1时调节；每半小时只尝试一次，未确认的MQTT写入
不会按增量重发。账号与令牌不落盘。
"""

from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.request import ProxyHandler, build_opener
from zoneinfo import ZoneInfo

from smart_energy_client import SmartEnergyClient, SmartEnergyError
from ahu_return_feedback import (AHU_IDS, DEVICE_IDS, HISTORY_IDS, FeedbackError as ControlError,
                                 assess_feedback, parse_histories)


ZONE = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
STATE_DIR = Path("/var/lib/task-manager/hangxin-hourly-temperature")
DEVICE_ID = 12788
PROJECT_ID = 832
SETPOINT_ID = "nfdh9q_mscrk01_c17"
OUTDOOR_ID = "nfdh9q_mscrk01_c21"
RUNNING_ID = "nfdh9q_mscrk01_c00"
LOWER = Decimal("7.5")
UPPER = Decimal("11")
BASE_URL = "https://smart-energy.yntravelsky.net.cn:36443"
GATE_URL = "http://127.0.0.1:8090/tasks/chiller_temperature"
WRITE_PATH = (
    "/api/GatewayMicro/Point/CommonMqttWritePointData?isWriteHistoryData=false"
    "&operationNode=%E8%AE%BE%E5%A4%87%E8%AF%A6%E6%89%8B%E5%8A%A8%E6%8E%A7%E5%88%B6"
)
PENDING_STATUSES = {"attempted", "unconfirmed"}
MODEL = "ahu_max_return_v1"
EXECUTION_TIMES = [f"{hour:02d}:{minute:02d}" for hour in range(11, 22)
                   for minute in (0, 30) if hour < 21 or minute == 0]



def now():
    """返回带上海时区的当前时间。"""
    return datetime.now(ZONE)


def local_time(value):
    """拒绝无时区时间，统一转换到北京时间。"""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ControlError("时间必须带有时区")
    return value.astimezone(ZONE)


def decimal_value(value):
    """仅接收有限数字，避免浮点阈值和布尔值被误用于控制。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ControlError("控制输入必须为有限数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ControlError("控制输入必须为有限数值") from None
    if not result.is_finite():
        raise ControlError("控制输入必须为有限数值")
    return result


def calculate(current_setpoint, feedback):
    """按最高回风与趋势给出唯一目标，每轮最多变化0.5℃。"""
    current = decimal_value(current_setpoint)
    if not LOWER <= current <= UPPER or current != current.quantize(Decimal("0.1")):
        raise ControlError("当前出水设定越界或精度异常，需要人工核对")
    if not isinstance(feedback, dict):
        raise ControlError("回风判断结构无效")
    maximum = feedback.get("max_return_c")
    projected = feedback.get("projected_max_return_c")
    maximum = decimal_value(maximum) if maximum is not None else None
    projected = decimal_value(projected) if projected is not None else None
    step, reason = Decimal(0), "insufficient_return_feedback"
    if maximum is not None:
        if maximum > Decimal("26"):
            step, reason = Decimal("-0.5"), "return_over_limit"
        elif projected is not None and projected > Decimal("26"):
            step, reason = Decimal("-0.5"), "return_forecast_over_limit"
        elif maximum <= Decimal("25.5") and feedback.get("raise_allowed") is True:
            step, reason = Decimal("0.5"), "return_below_buffer_stable"
        elif maximum > Decimal("25.5"):
            reason = "insufficient_temperature_margin"
        else:
            reason = "trend_not_stable_or_incomplete"
    proposed = current + step
    target = max(LOWER, min(UPPER, proposed))
    actual_step = target - current
    return {"target": float(target), "direction": "raise" if actual_step > 0 else "lower" if actual_step < 0 else "hold",
            "step_c": float(actual_step), "reason": reason, "saturated": target != proposed}


def point_stamp(point):
    """设备无时区的上报时间按上海时间解释。"""
    try:
        stamp = datetime.fromisoformat(point["updateTime"])
        return stamp.replace(tzinfo=ZONE) if stamp.tzinfo is None else stamp.astimezone(ZONE)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ControlError("设备上报时间无效") from None


def validate_points(points, at):
    """出水设定为必需输入；室外数据由反馈层独立校验，缺失仅禁止上调。"""
    at = local_time(at)
    if not isinstance(points, dict):
        raise ControlError("当前点位结构无效")
    for pid, point in points.items():
        if (not isinstance(point, dict) or point.get("deviceId") not in DEVICE_IDS
                or point.get("pointId", pid) != pid):
            raise ControlError("当前点位设备或标识不符")
    point = points.get(SETPOINT_ID)
    if (not isinstance(point, dict) or point.get("pointId") != SETPOINT_ID
            or point.get("deviceId") != DEVICE_ID or point.get("projectId") != PROJECT_ID
            or point.get("identifierName") != "c17" or point.get("pointName") != "冷机出水温度本地设定值"
            or point.get("unit") != "℃" or point.get("isWrite") is not True):
        raise ControlError("出水设定身份、单位或权限不符")
    current = decimal_value(point.get("value"))
    if not 0 <= (at - point_stamp(point)).total_seconds() <= 300:
        raise ControlError("出水设定上报过旧或晚于当前时间")
    outdoor = None
    point = points.get(OUTDOOR_ID)
    try:
        if (not isinstance(point, dict) or point.get("pointId") != OUTDOOR_ID
                or point.get("deviceId") != DEVICE_ID or point.get("projectId") != PROJECT_ID
                or point.get("identifierName") != "c21" or point.get("pointName") != "室外温度"
                or point.get("unit") != "℃" or not 0 <= (at-point_stamp(point)).total_seconds() <= 300):
            raise ControlError("室外辅助测点无效")
        candidate = decimal_value(point.get("value"))
        if not Decimal("-30") <= candidate <= Decimal("60"):
            raise ControlError("室外辅助温度超出合理范围")
        outdoor = float(candidate)
    except ControlError:
        pass
    return float(current), outdoor


def protected_schedules(points):
    """快照主机全部时间表字段，仅用于发现变化，不执行回写。"""
    return {pid: point.get("value") for pid, point in points.items()
            if pid.startswith("nfdh9q_mscrk01_t")}


def validate_running(points, at):
    """只认可五分钟内的主机运行反馈0/1，不以时间表或启停输出代替。"""
    at = local_time(at)
    point = points.get(RUNNING_ID) if isinstance(points, dict) else None
    if (not isinstance(point, dict) or point.get("pointId") != RUNNING_ID
            or point.get("deviceId") != DEVICE_ID or point.get("projectId") != PROJECT_ID
            or point.get("pointName") != "主机运行反馈" or point.get("identifierName") != "c00"
            or point.get("unit") != "" or point.get("isWrite") is not False):
        raise ControlError("主机运行反馈点身份不符或缺失")
    value = decimal_value(point.get("value"))
    if value not in (Decimal(0), Decimal(1)):
        raise ControlError("主机运行反馈必须为0或1")
    if not 0 <= (at - point_stamp(point)).total_seconds() <= 300:
        raise ControlError("主机运行反馈过旧或晚于当前时间")
    return value == 1


def gate_allowed(value):
    """只接受明确生效且未处于停止中的策略门控。"""
    return value is True or (isinstance(value, dict)
                             and value.get("effective_enabled") is True
                             and isinstance(value.get("status"), str)
                             and value["status"] != "stopping")


def local_gate():
    """绕过环境代理，只读取固定本地策略接口；异常时默认禁止写入。"""
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(GATE_URL, timeout=5) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(65537))
        return (isinstance(payload, dict) and payload.get("id") == "chiller_temperature"
                and gate_allowed(payload))
    except Exception:
        return False


class Platform:
    """固定同域认证及设备范围；构造对象时不登录、不请求网络。"""

    def __init__(self, config_path=None):
        """认证由后台环境注入，不自行读取或输出凭据。"""
        config_path = Path(config_path) if config_path else ROOT / "load_forecast_config.json"
        self.client = SmartEnergyClient(json.loads(config_path.read_text(encoding="utf-8")))
        if self.client.base_url != BASE_URL:
            raise ControlError("控制器只能连接已授权航信平台")

    def _headers(self):
        """到期前自动重登，令牌仅用于固定平台。"""
        if self.client._token is None or self.client._token_expires_at <= time.time() + 60:
            self.client._login()
        return {"Accept": "application/json", "Content-Type": "application/json",
                "Authorization": "Bearer " + self.client._token}

    def _read_request(self, path, body):
        """只读请求遇认证过期可重登一次，永不把响应正文写入记录。"""
        for attempt in range(2):
            response = self.client._post(self.client._endpoint(path), body=body, headers=self._headers())
            try:
                if response.status_code in (401, 403):
                    payload = None
                    expired = True
                else:
                    payload = self.client._json(response, "控制器只读查询")
                    expired = str(payload.get("code")) in {"401", "403"}
                if expired:
                    self.client._token = None
                    if attempt == 0:
                        continue
                    raise SmartEnergyError("控制器只读认证失败")
                if (response.status_code != 200 or payload.get("success") is not True
                        or str(payload.get("code")) != "200" or not isinstance(payload.get("data"), list)):
                    raise SmartEnergyError("控制器只读查询失败")
                return payload["data"]
            finally:
                response.close()
        raise SmartEnergyError("控制器只读查询失败")

    def read(self):
        """读取1#主机与四台1F AHU，2F AHU3不进入请求。"""
        rows = self._read_request("/api/GatewayMicro/Device/GetDevicePoint",
                                  {"deviceIdList": DEVICE_IDS, "isActivate": True})
        points = {}
        for point in rows:
            if (not isinstance(point, dict) or point.get("deviceId") not in DEVICE_IDS
                    or point.get("projectId") != PROJECT_ID):
                raise ControlError("当前点位含未授权设备或项目")
            pid = point.get("pointId")
            if not isinstance(pid, str) or pid in points:
                raise ControlError("当前点位标识缺失或重复")
            points[pid] = point
        return points

    def history_feedback(self, at):
        """按两分钟历史桶取数，宽取尾端以免截断；未来空桶由反馈层排除。"""
        at = local_time(at)
        start = (at - timedelta(minutes=35)).replace(second=0, microsecond=0)
        end = (at + timedelta(minutes=1)).replace(second=0, microsecond=0)
        rows = self._read_request("/api/GatewayMicro/Point/GetPointHistoryData", {
            "timeQuantum": {"startDate": start.strftime("%Y-%m-%d %H:%M"),
                            "endDate": end.strftime("%Y-%m-%d %H:%M")},
            "pointIdList": HISTORY_IDS, "interval": 2,
        })
        return parse_histories(rows, at)

    def prepare_write(self):
        """提前准备认证，之后仍需重新核验信号、门控和半小时窗口。"""
        self._headers()


    def write(self, pid, target):
        """只允许c17的一位小数绝对目标，单次请求不重试。"""
        value = decimal_value(target)
        if (pid != SETPOINT_ID or not LOWER <= value <= UPPER
                or value != value.quantize(Decimal("0.1"))):
            raise ControlError("拒绝授权范围外的设定下发")
        if self.client._token is None or self.client._token_expires_at <= time.time():
            raise ControlError("写入认证尚未准备或已经过期")
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Authorization": "Bearer " + self.client._token}
        response = self.client._post(self.client._endpoint(WRITE_PATH),
                                    body={SETPOINT_ID: float(value)}, headers=headers)
        try:
            payload = self.client._json(response, "控制器单点下发")
            return {"http_status": response.status_code,
                    "accepted": response.status_code == 200 and payload.get("success") is True
                    and str(payload.get("code")) == "200"}
        finally:
            response.close()


class Store:
    """SQLite事务保存决策基线与每个北京时间半小时的唯一执行记录。"""

    def __init__(self, state_dir):
        """状态目录与数据库只保存数值和安全状态，采用FULL同步。"""
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / "controller.sqlite3"
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    slot TEXT PRIMARY KEY, status TEXT NOT NULL, attempted INTEGER NOT NULL,
                    record TEXT NOT NULL
                );
            """)
        os.chmod(self.path, 0o600)

    def _connect(self):
        """每次连接明确启用完整同步，避免请求尝试标记留在进程内存。"""
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def read_state(self):
        """读取上次已确认的决策基线，没有状态时返回空。"""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT data FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def get_run(self, slot):
        """返回指定时隙记录，不能把既有尝试当成新的增量任务。"""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT record FROM runs WHERE slot=?", (slot,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending_runs(self):
        """列出尚未获得设备新上报确认的历史写入。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT record FROM runs WHERE attempted=1 AND status IN ('attempted','unconfirmed') ORDER BY slot"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, record, state=None):
        """一个事务提交时隙记录和可选候选状态，提交后才能发送写请求。"""
        rendered = json.dumps(record, ensure_ascii=False, allow_nan=False)
        state_text = json.dumps(state, ensure_ascii=False, allow_nan=False) if state is not None else None
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO runs(slot,status,attempted,record) VALUES(?,?,?,?) "
                    "ON CONFLICT(slot) DO UPDATE SET status=excluded.status,attempted=excluded.attempted,record=excluded.record",
                    (record["slot"], record["status"], int(record.get("attempted", False)), rendered),
                )
                if state_text is not None:
                    connection.execute(
                        "INSERT INTO state(id,data) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (state_text,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise


def candidate_state(at, setpoint, slot, feedback):
    """保存新模型状态；旧天气余量不进入新决策，旧小时审计保留。"""
    return {"date": local_time(at).date().isoformat(), "last_setpoint": setpoint,
            "last_slot": slot, "updated_at": local_time(at).isoformat(), "model": MODEL,
            "last_max_return_c": feedback.get("max_return_c"), "members": feedback.get("members", [])}


def read_confirmed(points, record, at):
    """只有固定目标一致且设备上报晚于请求尝试才算写后确认。"""
    value, _ = validate_points(points, at)
    return (decimal_value(value) == decimal_value(record["target"])
            and point_stamp(points[SETPOINT_ID]) > datetime.fromisoformat(record["attempted_at"]))


def finish_confirmation(store, record, points, at):
    """参数确认与候选状态同时提交，其他时间表变化单独报告。"""
    record = deepcopy(record)
    before = record.get("protected_schedules", {})
    after = protected_schedules(points)
    record.update(status="completed", verified_at=local_time(at).isoformat(),
                  other_schedules_unchanged=before == after,
                  other_schedule_changes=sorted(pid for pid in set(before) | set(after)
                                                if before.get(pid) != after.get(pid)),
                  last_readback={"value": points[SETPOINT_ID]["value"],
                                 "updateTime": points[SETPOINT_ID]["updateTime"]})
    store.save(record, record["candidate_state"])
    return record


def poll_confirmation(platform, store, record, clock, sleep):
    """最多四分钟只读确认已有绝对目标，不执行任何补发。"""
    until = local_time(clock()) + timedelta(minutes=4)
    for _ in range(25):
        at = local_time(clock())
        if at > until:
            break
        try:
            points = platform.read()
            if read_confirmed(points, record, local_time(clock())):
                return finish_confirmation(store, record, points, local_time(clock()))
            record["last_readback"] = {"value": points[SETPOINT_ID].get("value"),
                                       "updateTime": points[SETPOINT_ID].get("updateTime")}
            store.save(record)
        except Exception:
            pass
        if local_time(clock()) >= until:
            break
        sleep(10)
    record.update(status="unconfirmed", finished_at=local_time(clock()).isoformat(),
                  error="本次写入尚未获得新上报确认，仅保留待核状态，不重发")
    store.save(record)
    return record


def execute(platform, state_dir=STATE_DIR, clock=now, sleep=time.sleep, gate=local_gate):
    """每天11:00至21:00每半小时单轮，先持久记录，再唯一一次绝对值下发。"""
    at = local_time(clock())
    slot_at = at.replace(minute=(at.minute // 30) * 30, second=0, microsecond=0)
    slot = slot_at.isoformat()

    def in_window(moment):
        return slot_at.strftime("%H:%M") in EXECUTION_TIMES and 0 <= (local_time(moment)-slot_at).total_seconds() < 120

    if not in_window(at):
        return {"status": "skipped_window", "slot": slot, "attempted": False}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "execute.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "slot": slot, "attempted": False}
        store = Store(state_dir)
        existing = store.get_run(slot)

        def allowed():
            try:
                return gate_allowed(gate())
            except Exception:
                return False

        if not allowed():
            result = {"status": "skipped_gate", "slot": slot, "attempted": False,
                      "error": "本地策略未生效或门控不可用"}
            if existing is None:
                store.save(result)
            return result
        if existing is not None:
            if existing.get("attempted") and existing.get("status") in PENDING_STATUSES:
                return poll_confirmation(platform, store, existing, clock, sleep)
            return {**existing, "skipped_existing": True}
        record = {"slot": slot, "status": "preparing", "attempted": False, "attempted_at": None,
                  "target": None, "candidate_state": None, "started_at": at.isoformat(),
                  "device_id": DEVICE_ID, "point_id": SETPOINT_ID, "model": MODEL}
        store.save(record)

        def finish(status, reason=None, state=None):
            record.update(status=status, finished_at=local_time(clock()).isoformat())
            if reason:
                record["reason"] = reason
            store.save(record, state)
            return record

        try:
            points = platform.read()
            for pending in store.pending_runs():
                if read_confirmed(points, pending, local_time(clock())):
                    finish_confirmation(store, pending, points, local_time(clock()))
                else:
                    return finish("pending_hold", "遗留写入尚未完整确认，只读等待，不重新下发")
            running = validate_running(points, local_time(clock()))
            record["running_feedback"] = {"value": points[RUNNING_ID]["value"], "updateTime": points[RUNNING_ID]["updateTime"]}
            if not running:
                return finish("skipped_not_running", "主机运行反馈为0，本轮不调节出水设定")
            current, outdoor = validate_points(points, local_time(clock()))
            if not LOWER <= decimal_value(current) <= UPPER or decimal_value(current) != decimal_value(current).quantize(Decimal("0.1")):
                return finish("skipped_setpoint_out_of_bounds", "当前出水设定越界或精度异常，保留人工核对")
            before_schedule = protected_schedules(points)
            record.update(baseline_setpoint=current, outdoor=outdoor, protected_schedules=before_schedule)
            state = store.read_state()
            if (state and state.get("date") == at.date().isoformat() and "last_setpoint" in state
                    and decimal_value(current) != decimal_value(state["last_setpoint"])):
                rebuilt = candidate_state(local_time(clock()), current, slot, {})
                record.update(candidate_state=rebuilt, previous_setpoint=state["last_setpoint"])
                return finish("manual_rebase", "当前设定被外部改变，本轮不覆盖并重建基线", rebuilt)
            try:
                histories = platform.history_feedback(local_time(clock()))
                record["history_status"] = "queried"
            except Exception:
                histories = {}
                record["history_status"] = "unavailable_no_raise"
            latest = platform.read()
            checked_at = local_time(clock())
            if not validate_running(latest, checked_at):
                return finish("skipped_not_running", "历史查询期间主机已停止，本轮不写")
            fresh_current, outdoor = validate_points(latest, checked_at)
            if decimal_value(fresh_current) != decimal_value(current) or protected_schedules(latest) != before_schedule:
                return finish("external_change", "查询期间设定或时间表变化，本轮不覆盖")
            feedback = assess_feedback(latest, histories, checked_at)
            calculation = calculate(current, feedback)
            target = calculation["target"]
            proposed_state = candidate_state(checked_at, target, slot, feedback)
            record.update(feedback=feedback, calculation=calculation, target=target,
                          candidate_state=proposed_state, outdoor=outdoor)
            if decimal_value(target) == decimal_value(current):
                return finish("no_change", calculation["reason"], proposed_state)
            platform.prepare_write()
            if not allowed():
                return finish("skipped_gate", "发送前策略门控失效")
            latest = platform.read()
            checked_at = local_time(clock())
            if not validate_running(latest, checked_at):
                return finish("skipped_not_running", "发送前主机已停机，本轮取消写入")
            fresh_current, outdoor = validate_points(latest, checked_at)
            if decimal_value(fresh_current) != decimal_value(current) or protected_schedules(latest) != before_schedule:
                return finish("external_change", "发送前设定或时间表变化，本轮不覆盖")
            feedback = assess_feedback(latest, histories, checked_at)
            calculation = calculate(current, feedback)
            target = calculation["target"]
            proposed_state = candidate_state(checked_at, target, slot, feedback)
            record.update(feedback=feedback, calculation=calculation, target=target,
                          candidate_state=proposed_state, outdoor=outdoor)
            if decimal_value(target) == decimal_value(current):
                return finish("no_change", calculation["reason"], proposed_state)
            if not in_window(clock()):
                return finish("skipped_window", "计算或认证后超过半小时下发窗口")
            record.update(status="attempted", attempted=True, attempted_at=local_time(clock()).isoformat())
            store.save(record)
            # 门控和落盘都可能耗时；实际POST前再核验时间及同一组信号。
            final_status, final_reason = None, None
            if not allowed():
                final_status, final_reason = "skipped_gate", "持久化后门控失效，本字段未发送"
            elif not in_window(clock()):
                final_status, final_reason = "skipped_window", "持久化后超过半小时下发窗口，本字段未发送"
            else:
                try:
                    final_at = local_time(clock())
                    if not validate_running(latest, final_at):
                        final_status, final_reason = "skipped_not_running", "发送前运行反馈失效，本字段未发送"
                    else:
                        check_current, _ = validate_points(latest, final_at)
                        final_feedback = assess_feedback(latest, histories, final_at)
                        final_calculation = calculate(check_current, final_feedback)
                        if decimal_value(final_calculation["target"]) != decimal_value(target):
                            final_status, final_reason = "skipped_conditions", "发送前回风条件失效，本字段未发送"
                except ControlError:
                    final_status, final_reason = "skipped_conditions", "发送前测量时效或身份校验失败，本字段未发送"
            if final_status:
                record.update(attempted=False, attempted_at=None)
                return finish(final_status, final_reason)
            try:
                response = platform.write(SETPOINT_ID, target)
                record["write_response"] = ({key: response[key] for key in ("http_status", "accepted") if key in response}
                                            if isinstance(response, dict) else {"outcome": "unknown_readback_only"})
            except Exception:
                record["write_response"] = {"outcome": "unknown_readback_only"}
            store.save(record)
            return poll_confirmation(platform, store, record, clock, sleep)
        except Exception:
            if record.get("attempted"):
                return finish("unconfirmed", "写入尚未确认，禁止自动重发")
            return finish("failed", "当前测量、历史或本地状态未通过校验，本轮不写")


def main(argv=None):
    """默认预览；check只读当前点位和门控；execute才启动受保护控制。"""
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preview", action="store_true")
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--execute", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--config", type=Path, default=ROOT / "load_forecast_config.json")
    args = parser.parse_args(argv)
    try:
        if not args.check and not args.execute:
            result = {"status": "preview", "device_id": DEVICE_ID, "point_id": SETPOINT_ID,
                      "execution_times": EXECUTION_TIMES, "timezone": "Asia/Shanghai", "model": MODEL,
                      "ahu_ids": list(AHU_IDS), "excluded_ahu_ids": [3],
                      "setpoint_bounds": [7.5, 11.0], "setpoint_step": 0.5,
                      "return_limit_c": 26.0, "raise_buffer_c": 25.5, "trend_horizon_minutes": 30,
                      "requires_running_feedback": True, "running_point_id": RUNNING_ID,
                      "after_19_force_11": False, "write_sent": False}
        elif args.check:
            platform = Platform(args.config)
            points = platform.read()
            current, outdoor = validate_points(points, now())
            running = validate_running(points, now())
            result = {"status": "readonly_check", "setpoint": current, "outdoor": outdoor,
                      "running": running, "running_feedback": points[RUNNING_ID]["value"],
                      "running_update_time": points[RUNNING_ID]["updateTime"],
                      "setpoint_update_time": points[SETPOINT_ID]["updateTime"],
                      "outdoor_update_time": points[OUTDOOR_ID]["updateTime"],
                      "gate_enabled": local_gate(), "protected_schedules": protected_schedules(points),
                      "model": MODEL, "ahu_ids": list(AHU_IDS), "excluded_ahu_ids": [3],
                      "feedback": assess_feedback(points, platform.history_feedback(now()), now()),
                      "write_sent": False}
        else:
            result = execute(Platform(args.config), args.state_dir)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result["status"] in {"failed", "unconfirmed", "external_change"} else 0
    except Exception:
        print(json.dumps({"status": "failed", "error": "控制器配置、认证、存储或数据校验失败"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
