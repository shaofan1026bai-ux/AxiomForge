"""晚间每半小时依据航班空档与低于27℃的预测室外温度提前关机。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import json
from pathlib import Path
import time
from urllib.request import ProxyHandler, build_opener

from flight_gap_analysis import analyze
from hangxin_flight_gap_trial import fetch_raw_flights
from hangxin_start_forecast import Store
from nowapi_weather import WeatherError, interval_temperature
from flight_weather_shutdown_policy import EXECUTION_TIMES, CANDIDATE_TIMES, policy_config, select_shutdown
from hangxin_flight_shutdown_platform import (
    HOUR_ID, MINUTE_ID, Platform, ShutdownError, ZONE, point_stamp,
    protected_schedules, read_off_schedule, read_start_schedule, validate_running,
)


ROOT = Path(__file__).resolve().parent
STATE_DIR = Path("/var/lib/task-manager/hangxin-flight-shutdown")
FLIGHT_ENV_FILE = Path("/etc/task-manager-flight.env")
HOURS = [18,19,20]
GATE_URL = "http://127.0.0.1:8090/tasks/flight_linked_control"


def now():
    """调度和业务时刻固定为北京时间。"""
    return datetime.now(ZONE)


def local_time(value):
    """拒绝无时区的判断、候选及持久记录。"""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ShutdownError("时间必须带时区")
    return value.astimezone(ZONE)


def gate_allowed(value):
    """只有已生效的航班策略才允许修改设备时间表。"""
    return value is True or (
        isinstance(value, dict) and value.get("id") == "flight_linked_control"
        and value.get("effective_enabled") is True and isinstance(value.get("status"), str)
        and value["status"] != "stopping"
    )


def local_gate():
    """仅查询本地策略状态，禁用环境代理且失败关闭。"""
    try:
        with build_opener(ProxyHandler({})).open(GATE_URL, timeout=5) as response:
            return response.status == 200 and gate_allowed(json.loads(response.read(65537)))
    except Exception:
        return False


def allowed(gate):
    """门控异常不能放行下发。"""
    try:
        return gate_allowed(gate())
    except Exception:
        return False


def fetch_flights(day):
    """独立受限环境文件只供航班查询使用，不读取或输出其他项目凭据。"""
    return fetch_raw_flights(day, FLIGHT_ENV_FILE)


def fetch_weather():
    """复用主服务的15分钟缓存，不在子进程保存或传播天气密钥。"""
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:8090/weather-forecast',timeout=20) as response:
            data=response.read(500_001)
            if response.status!=200 or len(data)>500_000:raise ValueError
            return json.loads(data)
    except Exception:raise WeatherError('逐小时天气接口不可用，保留当前关机时间表') from None


def validate_query(metadata, at, slot_at):
    """只接受本轮完整三日查询；计算时刻不冒充源库更新水位。"""
    expected = [(at.date() + timedelta(days=n)).isoformat() + " 00:00:00" for n in (-1, 0, 1)]
    if (not isinstance(metadata, dict) or metadata.get("airport") != "LUM"
            or metadata.get("complete_query_returned") is not True
            or metadata.get("limited", False) is not False or metadata.get("plan_dates") != expected):
        raise ShutdownError("航班查询范围或完整返回标记不符")
    started = local_time(metadata["query_started_at"])
    completed = local_time(metadata["query_completed_at"])
    if not slot_at <= started <= completed <= at or (at - completed).total_seconds() > 120:
        raise ShutdownError("航班查询不是本轮新数据或时间异常")
    return completed


def candidate_from_analysis(result, day, weather, at, current=None, config=None):
    """旧分析中的20点解释字段不再参与设备控制，直接检查全场动态空档。"""
    return select_shutdown(result,day,weather,at,current,config)


def change_plan(current, candidate, original, start_at, at):
    """只提前到授权候选，五分钟前停止改表，避免30分钟冻结挡住下一半点。"""
    current, candidate, original, start_at, at = map(local_time, (current, candidate, original, start_at, at))
    if (candidate.date()!=at.date() or candidate.strftime('%H:%M')not in CANDIDATE_TIMES
            or candidate.second or candidate.microsecond or candidate<=start_at or candidate>current):
        raise ShutdownError("只允许当天候选半点提前关机，且必须晚于首组开机时刻")
    values = {HOUR_ID: candidate.hour, MINUTE_ID: candidate.minute}
    before = {HOUR_ID: current.hour, MINUTE_ID: current.minute}
    changes = [pid for pid in values if values[pid] != before[pid]]
    if not changes:
        return [], min(current, candidate, original) - timedelta(minutes=5)
    orders = [changes] if len(changes) == 1 else [changes, list(reversed(changes))]
    choices = []
    for order in orders:
        hour, minute = current.hour, current.minute
        moments = [current, candidate, original]
        for pid in order:
            if pid == HOUR_ID:
                hour = candidate.hour
            else:
                minute = candidate.minute
            moments.append(at.replace(hour=hour, minute=minute, second=0, microsecond=0))
        choices.append((min(moments) - timedelta(minutes=5), order))
    deadline, order = max(choices, key=lambda item: item[0])
    return [{"point_id": pid, "value": values[pid]} for pid in order], deadline


def full_confirmed(points, record, at):
    """完整目标与每个实际发送字段的新时间戳一起构成设备回读确认。"""
    try:
        return (
            read_off_schedule(points, at).isoformat() == record["target_at"]
            and protected_schedules(points) == record["protected_schedules"]
            and len(record["writes"]) == len(record["changes"])
            and all(points[entry["point_id"]].get("value") == entry["value"]
                    and point_stamp(points[entry["point_id"]]) > local_time(entry["attempted_at"])
                    for entry in record["writes"])
        )
    except (ShutdownError, KeyError, TypeError, ValueError):
        return False


def finish_confirmation(store, record, at):
    """确认改表只代表设备时间字段一致，不把它当作机组已经停机。"""
    state = store.read_state()
    if state and state.get("day") == local_time(record["slot"]).date().isoformat():
        state["expected_off_at"] = record["target_at"]
    record.update(status="confirmed", finished_at=at.isoformat())
    store.save(record, state)


def execute(platform, state_dir=STATE_DIR, clock=now, sleep=time.sleep,
            gate=local_gate, flight_source=fetch_flights, analyzer=analyze, weather_source=fetch_weather):
    """从18点每半小时判断，候选至20:30；本日人工接管与未知请求保护保留。"""
    at = local_time(clock())
    slot_at = at.replace(minute=at.minute//30*30, second=0, microsecond=0)
    slot = slot_at.isoformat()
    if slot_at.strftime('%H:%M')not in EXECUTION_TIMES or not 0 <= (at - slot_at).total_seconds() < 120:
        return {"slot": slot, "status": "skipped_window", "attempted": False}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "execute.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"slot": slot, "status": "already_running", "attempted": False}
        store = Store(state_dir)
        existing = store.get_run(slot)
        if existing is not None:
            return {**existing, "skipped_existing": True}
        record = {"slot": slot, "started_at": at.isoformat(), "status": "preparing",
                  "device_id": 12788, "attempted": False, "writes": [],
                  "shutdown_policy": "flight-weather-half-hour-v2", "pump_afterrun": "existing_site_delay"}
        store.save(record)

        def finish(status, reason=None):
            """仅记录固定本地说明，不保存底层响应、连接地址或认证信息。"""
            record.update(status=status, finished_at=local_time(clock()).isoformat())
            if reason:
                record["reason"] = reason
            store.save(record)
            return record

        if not allowed(gate):
            return finish("skipped_gate", "AI或航班策略未生效")
        try:
            config=policy_config()
            points = platform.read()
            at = local_time(clock())
            for pending in store.pending_runs():
                if full_confirmed(points, pending, at):
                    finish_confirmation(store, pending, at)
                else:
                    return finish("pending_hold", "遗留写入未完整确认，只读核对，不补发或开启新计划")
            try:
                if not validate_running(points, at):
                    return finish("skipped_not_running", "机组已停机，不修改本轮关机时间表")
            except ShutdownError as exc:
                return finish("skipped_conditions", str(exc))
            current = read_off_schedule(points, at)
            start_at = read_start_schedule(points, at)
            state = store.read_state()
            if not state or state.get("day") != at.date().isoformat():
                state = {"day": at.date().isoformat(), "original_off_at": current.isoformat(),
                         "expected_off_at": current.isoformat(),
                         "fixed_freeze_deadline": (current - timedelta(minutes=5)).isoformat()}
            if state.get("external_hold"):
                return finish("external_change", "本日人工接管标记仍有效，保留设备当前时间表")
            if current.isoformat() != state["expected_off_at"]:
                state["external_hold"] = True
                store.save(record, state)
                return finish("external_change", "关机时间被外部改变，本日不自动覆盖")
            original = local_time(state["original_off_at"])
            state['policy_version']=config['version']
            state['fixed_freeze_deadline']=(original-timedelta(minutes=5)).isoformat()
            record.update(before_off_at=current.isoformat(), original_off_at=original.isoformat(),
                          fixed_freeze_deadline=state["fixed_freeze_deadline"],
                          protected_schedules=protected_schedules(points))
            store.save(record, state)
            rows, metadata = flight_source(at.date())
            at = local_time(clock())
            completed = validate_query(metadata, at, slot_at)
            result = analyzer(rows, at.date(), completed)
            weather=weather_source()
            at=local_time(clock())
            candidate, evening = candidate_from_analysis(result, at.date(),weather,at,current,config)
            record.update(query={k: metadata[k] for k in (
                "airport", "plan_dates", "query_started_at", "query_completed_at", "complete_query_returned"
            )}, flight_count=result["selected_day_flights"], assessment=deepcopy(evening))
            if candidate is None:
                return finish("no_candidate", "候选时刻没有同时满足全场空档与预测温度条件，保留当前关机时间表")
            record["target_at"] = candidate.isoformat()
            changes, deadline = change_plan(current, candidate, original, start_at, at)
            record.update(changes=changes, deadline=deadline.isoformat())
            if not changes:
                return finish("no_change", "设备关机时间表已等于合格候选")
            if at + timedelta(minutes=5) >= deadline:
                return finish("frozen", "已进入原表、现表或目标的5分钟保护区间，或不足5分钟回读预算")
            until = min(at + timedelta(minutes=5), deadline)
            expected = current
            for change in changes:
                points = platform.read()
                at = local_time(clock())
                if not validate_running(points, at):
                    return finish("unconfirmed" if record["attempted"] else "skipped_not_running", "发送前机组已停止")
                if (read_off_schedule(points, at) != expected
                        or protected_schedules(points) != record["protected_schedules"]):
                    state["external_hold"] = True
                    store.save(record, state)
                    return finish("unconfirmed" if record["attempted"] else "external_change", "发送前时间表变化，停止后续字段")
                # 认证和门控在最后的时钟校验前完成，禁止请求内部重登扩展下发窗口。
                platform.prepare_write()
                if not allowed(gate):
                    return finish("unconfirmed" if record["attempted"] else "skipped_gate", "发送前策略门控失效")
                sent_at = local_time(clock())
                if not interval_temperature(weather,candidate,local_time(evening['selected']['gap_end']),sent_at,config['outdoor_temperature_limit_c'])['allowed']:
                    raise WeatherError('发送前天气条件失效')
                if (sent_at >= until or (sent_at - completed).total_seconds() > 300
                        or (not record["attempted"] and not 0 <= (sent_at - slot_at).total_seconds() < 120)):
                    return finish("unconfirmed" if record["attempted"] else "skipped_window", "发送前已超过窗口或航班快照时效")
                if not validate_running(points, sent_at):
                    return finish("unconfirmed" if record["attempted"] else "skipped_not_running", "发送前运行反馈失效")
                if (read_off_schedule(points, sent_at) != expected
                        or read_start_schedule(points, sent_at) != start_at
                        or protected_schedules(points) != record["protected_schedules"]):
                    return finish("unconfirmed" if record["attempted"] else "external_change", "认证或门控后时间表基准失效")
                entry = {**change, "attempted_at": sent_at.isoformat()}
                record["writes"].append(entry)
                record.update(status="attempted", attempted=True)
                store.save(record)
                posting_at = local_time(clock())
                cancel = (posting_at >= until or (posting_at - completed).total_seconds() > 300
                          or (len(record["writes"]) == 1
                              and not 0 <= (posting_at - slot_at).total_seconds() < 120))
                try:
                    cancel = cancel or not interval_temperature(weather,candidate,local_time(evening['selected']['gap_end']),posting_at,config['outdoor_temperature_limit_c'])['allowed']
                    cancel = (cancel or not validate_running(points, posting_at)
                              or read_off_schedule(points, posting_at) != expected
                              or read_start_schedule(points, posting_at) != start_at
                              or protected_schedules(points) != record["protected_schedules"])
                except (ShutdownError,WeatherError):
                    cancel = True
                if cancel:
                    record["writes"].pop()
                    record["attempted"] = bool(record["writes"])
                    return finish("unconfirmed" if record["attempted"] else "skipped_window", "落盘后窗口或反馈失效，本字段未发送")
                try:
                    response = platform.write(change["point_id"], change["value"])
                    entry["response"] = {k: response[k] for k in ("http_status", "accepted") if k in response}
                except Exception:
                    entry["response"] = {"outcome": "unknown_readback_only"}
                store.save(record)
                expected = expected.replace(**{"hour" if change["point_id"] == HOUR_ID else "minute": change["value"]})
                confirmed = False
                while local_time(clock()) < until:
                    try:
                        points = platform.read()
                        checked = local_time(clock())
                        if (checked < until and read_off_schedule(points, checked) == expected
                                and protected_schedules(points) == record["protected_schedules"]
                                and point_stamp(points[change["point_id"]]) > sent_at):
                            entry["confirmed_at"] = checked.isoformat()
                            store.save(record)
                            confirmed = True
                            break
                    except Exception:
                        pass
                    remaining = (until - local_time(clock())).total_seconds()
                    if remaining <= 0:
                        break
                    sleep(min(10, remaining))
                if not confirmed:
                    return finish("unconfirmed", "设备新上报未确认，不重发或继续剩余字段")
            if local_time(clock()) >= until or not full_confirmed(points, record, local_time(clock())):
                return finish("unconfirmed", "完整关机目标尚未确认")
            finish_confirmation(store, record, local_time(clock()))
            return record
        except WeatherError as error:
            return finish('unconfirmed' if record['attempted'] else 'weather_hold',str(error))
        except Exception:
            return finish("unconfirmed" if record["attempted"] else "failed",
                          "本轮取数、校验或持久状态未通过；未确认请求只回读不重发")


def readonly_check(platform, flight_source=fetch_flights, weather_source=fetch_weather):
    """部署核验使用同一数据链，但不写时间表或控制器状态。"""
    points = platform.read()
    at = now()
    running = validate_running(points, at)
    rows, metadata = flight_source(at.date())
    points = platform.read()
    at = now()
    running = validate_running(points, at)
    completed = validate_query(metadata, at, at.replace(minute=at.minute//30*30, second=0, microsecond=0))
    result = analyze(rows, at.date(), completed)
    candidate, evening = candidate_from_analysis(result, at.date(),weather_source(),at,read_off_schedule(points,at))
    return {"status": "readonly_check", "write_sent": False, "running": running,
            "current_off_at": read_off_schedule(points, at).isoformat(),
            "current_start_at": read_start_schedule(points, at).isoformat(),
            "flight_count": result["selected_day_flights"], "candidate_at": candidate.isoformat() if candidate else None,
            "assessment": evening, "gate_allowed": local_gate()}


def main(argv=None):
    """默认预览，check只读验证，execute才由半小时任务实际改表。"""
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--execute", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / "load_forecast_config.json")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--flight-env-file", type=Path, default=FLIGHT_ENV_FILE)
    args = parser.parse_args(argv)
    try:
        source = lambda day: fetch_raw_flights(day, args.flight_env_file)
        if args.execute:
            result = execute(Platform(args.config), args.state_dir, flight_source=source)
        elif args.check:
            result = readonly_check(Platform(args.config), source)
        else:
            result = {"status": "preview", "write_sent": False, "hours": HOURS, "timezone": "Asia/Shanghai",
                      "strategy_id": "flight_linked_control", "candidate_times": CANDIDATE_TIMES,
                      "execution_times": EXECUTION_TIMES, "outdoor_temperature_limit_c":27,
                      "allowed_write_points": [HOUR_ID, MINUTE_ID], "freeze_minutes": 5,
                      "confirmation_budget_minutes": 5, "pump_afterrun": "existing_site_delay"}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        return 1 if result["status"] in {"failed", "unconfirmed", "pending_hold"} else 0
    except Exception:
        print(json.dumps({"status": "failed", "reason": "执行未完成，请核对持久审计；未确认请求不会自动重发"}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
