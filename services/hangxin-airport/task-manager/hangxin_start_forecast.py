"""整点预测1#机组开机时间，并在固定冻结边界之前修改第一组时间表。"""

from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.request import ProxyHandler, build_opener

from algorithms.hangxin_start_forecast_mean.forecast import (
    rolling_tick,SCHEDULE_FREEZE_MINUTES,SCHEDULE_CONFIRMATION_MINUTES,
)
import hangxin_start_day_policy as day_policy
from nowapi_weather import WeatherService
from hangxin_start_platform import (
    HOUR_ID, MINUTE_ID, Platform, StartError, ZONE, assert_stopped,
    build_payload, eligible_ahus, off_time, point_stamp, protected_schedules, read_schedule,
)

ROOT = Path(__file__).resolve().parent
ALGORITHM_DIR = ROOT / "algorithms/hangxin_start_forecast_mean"
STATE_DIR = Path("/var/lib/task-manager/hangxin-start-forecast")
HOURS = list(range(day_policy.FIRST_HOUR, day_policy.FINAL_HOUR + 1))
RUN_HOURS = HOURS.copy()
GATE_URL = "http://127.0.0.1:8090/tasks/chiller_start_stop"
PENDING_STATUSES = {"attempted", "unconfirmed"}


def now():
    """运行边界统一使用北京时间。"""
    return datetime.now(ZONE)


def local_time(value):
    """拒绝无时区的计划，避免服务器系统时区影响写入。"""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StartError("计划时间必须带时区")
    return value.astimezone(ZONE)


def next_evaluation(at):
    """返回08—17点真实调度；最后一轮后直接到次日08点。"""
    at = local_time(at)
    for hour in RUN_HOURS:
        candidate=at.replace(hour=hour,minute=0,second=0,microsecond=0)
        if candidate>at:return candidate
    return (at+timedelta(days=1)).replace(hour=HOURS[0],minute=0,second=0,microsecond=0)


def gate_allowed(value):
    """本地门控未知或正在停止时，不能产生设备请求。"""
    return value is True or (
        isinstance(value, dict) and value.get("id") == "chiller_start_stop"
        and value.get("effective_enabled") is True
        and isinstance(value.get("status"), str) and value["status"] != "stopping"
    )


def local_gate():
    """只读取本机策略接口，明确绕过代理且失败关闭。"""
    try:
        with build_opener(ProxyHandler({})).open(GATE_URL, timeout=5) as response:
            return response.status == 200 and gate_allowed(json.loads(response.read(65537)))
    except Exception:
        return False


def allowed(gate):
    """门控查询异常不能使控制继续。"""
    try:
        return gate_allowed(gate())
    except Exception:
        return False


class Store:
    """事务保存原计划冻结边界、算法缓存及每小时唯一的下发审计。"""

    def __init__(self, state_dir):
        """FULL同步确保尝试标记先于请求落盘。"""
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = state_dir / "controller.sqlite3"
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    slot TEXT PRIMARY KEY, status TEXT NOT NULL, attempted INTEGER NOT NULL, record TEXT NOT NULL
                );
            """)
        os.chmod(self.path, 0o600)

    def _connect(self):
        """连接明确关闭，不让线程或子进程继承未结束的事务。"""
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def read_state(self):
        """读取每日原计划和上次原样保存的next_state。"""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT data FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def get_run(self, slot):
        """已发生的小时不会重新计算并重复下发。"""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT record FROM runs WHERE slot=?", (slot,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending_runs(self):
        """部分写入和超时请求必须先只读核对，不能继续新计划。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT record FROM runs WHERE attempted=1 AND status IN ('attempted','unconfirmed') ORDER BY slot"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, record, state=None):
        """每次请求前原子提交记录，可同时提交算法状态。"""
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


def readiness(points, at, members=None):
    """明确停机且没有启动输出或故障，才使用有效运行AHU做判断。"""
    assert_stopped(points, at)
    active = eligible_ahus(points, at)
    if len(active) < 2 or (members is not None and not set(members).issubset(active)):
        raise StartError("有效运行AHU不足两台或本轮参考AHU已失效")
    return active


def rebase_external_schedule(state,current,at):
    """人工新表成为新一轮基准；算法自己已确认的后移不触发重置。"""
    old={'original_start_at':state.get('original_start_at'),
         'expected_start_at':state.get('expected_start_at'),'legacy_external_hold':bool(state.get('external_hold'))}
    state.update(original_start_at=current.isoformat(),expected_start_at=current.isoformat(),
        fixed_freeze_deadline=(current-timedelta(minutes=SCHEDULE_FREEZE_MINUTES)).isoformat(),
        freeze_minutes=SCHEDULE_FREEZE_MINUTES,forecast_state=None,
        last_external_schedule_at=at.isoformat())
    state.pop('external_hold',None)
    return {**old,'observed_start_at':current.isoformat(),'observed_at':at.isoformat()}


def change_plan(current, candidate, original, stop_at, at):
    """检查原计划、当前、目标和中间组合，选择冻结截止最宽的单点写入顺序。"""
    current, candidate, original, stop_at, at = map(
        local_time, (current, candidate, original, stop_at, at)
    )
    if (candidate.date() != at.date() or candidate.minute % 5 or candidate.second
            or candidate.microsecond or candidate >= stop_at):
        raise StartError("候选必须是当天五分钟整点且早于第一组关机时间")
    desired = {HOUR_ID: candidate.hour, MINUTE_ID: candidate.minute}
    existing = {HOUR_ID: current.hour, MINUTE_ID: current.minute}
    changed = [pid for pid in desired if desired[pid] != existing[pid]]
    if not changed:
        return [], min(original, current, candidate) - timedelta(minutes=SCHEDULE_FREEZE_MINUTES)
    orders = [changed] if len(changed) == 1 else [changed, list(reversed(changed))]
    choices = []
    for order in orders:
        hour, minute = current.hour, current.minute
        times = [original, current, candidate]
        for pid in order:
            if pid == HOUR_ID:
                hour = desired[pid]
            else:
                minute = desired[pid]
            times.append(at.replace(hour=hour, minute=minute, second=0, microsecond=0))
        deadline = min(times) - timedelta(minutes=SCHEDULE_FREEZE_MINUTES)
        choices.append((deadline, order))
    deadline, order = max(choices, key=lambda item: item[0])
    return [{"point_id": pid, "value": desired[pid]} for pid in order], deadline


def full_confirmed(points, record, at):
    """不凭接口成功确认；所有计划字段都需发送后的设备新时间戳。"""
    if record.get('operation') in day_policy.OPERATIONS:
        return day_policy.full_confirmed(points,record,at)
    try:
        if read_schedule(points, at).isoformat() != record["target_at"]:
            return False
        if protected_schedules(points) != record["protected_schedules"]:
            return False
        writes = record.get("writes", [])
        if len(writes) != len(record["changes"]):
            return False
        return all(
            points[entry["point_id"]].get("value") == entry["value"]
            and point_stamp(points[entry["point_id"]]) > local_time(entry["attempted_at"])
            for entry in writes
        )
    except (StartError, KeyError, TypeError, ValueError):
        return False


def finish_confirmation(store, record, at):
    """只在完整目标确认后推进预期设备时间表。"""
    state = store.read_state()
    if record.get('operation') in day_policy.OPERATIONS:
        state=day_policy.confirmation_state(state,record,at)
    elif state and state.get("day") == local_time(record["slot"]).date().isoformat():
        state["expected_start_at"] = record["target_at"]
    record.update(status="confirmed", finished_at=at.isoformat())
    store.save(record, state)


def execute(platform, state_dir=STATE_DIR, clock=now, sleep=time.sleep,
            gate=local_gate, predictor=rolling_tick, weather=None, manual=False, final_replay=None):
    """每轮继续预测与判断；未知控制只核对不重发，人工改表不再锁住整天。"""
    at = local_time(clock())
    # 手动执行以真实调用时刻作为独立审计键，不伪造整点，也不占用服务器小时记录。
    slot_at = at if manual is True else at.replace(minute=0, second=0, microsecond=0)
    slot = slot_at.isoformat()
    replay=final_replay is not None
    if replay:
        try:
            source_at=local_time(final_replay['forecast']['evaluated_at'])
            expected=local_time(final_replay['expected_start_at'])
            version=json.loads((ALGORITHM_DIR/'forecast_config.json').read_text())['version']
            if (manual is not True or source_at.date()!=at.date() or source_at.hour!=17 or source_at.minute>=2
                or at<source_at or final_replay['source_slot']!=source_at.replace(minute=0,second=0,microsecond=0).isoformat()
                or final_replay['source_model']!=version or final_replay['forecast'].get('automatic_adjustment_allowed') is not True
                or not re.fullmatch('[0-9a-f]{64}',final_replay['payload_sha256'])
                or expected.date()!=at.date() or at+timedelta(minutes=20)>=expected):raise ValueError
        except Exception:
            return {'slot':slot,'status':'skipped_replay','attempted':False,'reason':'17点回放来源、模型版本或20分钟保护预算不符'}
    if (not replay and at.hour not in (HOURS if manual is True else RUN_HOURS)) or not 0 <= (at - slot_at).total_seconds() < 120:
        return {"slot": slot, "status": "skipped_window", "attempted": False}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "execute.lock").open("a+b") as lock:
        control_blockers=[]
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"slot": slot, "status": "already_running", "attempted": False}
        store = Store(state_dir)
        existing = store.get_run(slot)
        if existing is not None:
            # 既有尝试无论是否成功都不重复；后续整点统一核查未确认记录。
            return {**existing, "skipped_existing": True}
        record = {"slot": slot, "status": "preparing", "attempted": False, "writes": [],
                  "manual": manual is True,
                  "started_at": at.isoformat(), "next_evaluation_at": next_evaluation(at).isoformat(),
                  "device_id": 12788}
        if replay:record.update(replay_source_slot=final_replay['source_slot'],replay_payload_sha256=final_replay['payload_sha256'])
        store.save(record)

        def finish(status, message=None):
            """错误仅保存固定说明，不序列化上游请求上下文。"""
            record.update(status=status, finished_at=local_time(clock()).isoformat())
            if message:
                record["reason"] = message
            store.save(record)
            return record

        if not allowed(gate):
            return finish("skipped_gate", "AI模式或策略未生效")
        try:
            state=store.read_state()
            points = platform.read()
            at = local_time(clock())
            for pending in store.pending_runs():
                verification_points=platform.read_time_control() if pending.get('operation') in day_policy.OPERATIONS else points
                if full_confirmed(verification_points, pending, local_time(clock())):
                    finish_confirmation(store, pending, local_time(clock()))
                else:
                    control_blockers.append({'status':'pending_hold','reason':'遗留写入尚未完整确认，本轮继续预测但不新增控制',
                                             'pending_slot':pending['slot']})
            state=store.read_state()
            if (state or {}).get('cold_day'):
                control_blockers.append({'status':'legacy_hold','reason':'旧清零方案状态仍待人工核对，本轮仅预测'})
            if (state or {}).get('time_control_hold'):
                control_blockers.append({'status':'legacy_time_control_hold','reason':'旧时控停用记录待核对，本轮继续预测但不改表、不切换时控'})
            try:
                members = readiness(points, at)
            except StartError as exc:
                return finish("skipped_conditions", str(exc))
            current = read_schedule(points, at)
            stop_at = off_time(points, at)
            if replay and current!=local_time(final_replay['expected_start_at']):
                return finish('external_change','现场开机表已不同于原17点决策，不覆盖后续改动')
            state = store.read_state()
            if not state or state.get("day") != at.date().isoformat():
                state = {"day": at.date().isoformat(), "original_start_at": current.isoformat(),
                         "fixed_freeze_deadline": (current - timedelta(minutes=SCHEDULE_FREEZE_MINUTES)).isoformat(),
                         "expected_start_at": current.isoformat(), "forecast_state": None}
            if not control_blockers and (state.get('external_hold') or current.isoformat()!=state['expected_start_at']):
                record['external_schedule_rebased']=rebase_external_schedule(state,current,at)
            original = local_time(state["original_start_at"])
            # 保留当天首次读取的原计划，仅迁移冻结提前量，不能跟随后移候选延长截止。
            state.update(fixed_freeze_deadline=(original-timedelta(minutes=SCHEDULE_FREEZE_MINUTES)).isoformat(),
                         freeze_minutes=SCHEDULE_FREEZE_MINUTES)
            record.update(original_start_at=original.isoformat(), before_start_at=current.isoformat(),
                          protected_schedules=protected_schedules(points), eligible_ahus=members,
                          fixed_freeze_deadline=(original - timedelta(minutes=SCHEDULE_FREEZE_MINUTES)).isoformat())
            store.save(record, state)
            history = platform.history(at)
            # 历史查询期间设备可变，重新获取真实当前反馈再构建输入。
            points = platform.read()
            at = local_time(clock())
            readiness(points, at, members)
            if (read_schedule(points, at) != current
                    or protected_schedules(points) != record["protected_schedules"]):
                record['external_change_during_calculation']=True
                current=read_schedule(points,at);stop_at=off_time(points,at)
                if not control_blockers:record['external_schedule_rebased']=rebase_external_schedule(state,current,at)
                original=local_time(state['original_start_at'])
                record.update(before_start_at=current.isoformat(),protected_schedules=protected_schedules(points),
                              original_start_at=original.isoformat(),fixed_freeze_deadline=state['fixed_freeze_deadline'])
                store.save(record,state)
            payload = build_payload(points, history, at, original)
            if weather is not None:
                try:payload['weather_forecast']=weather()
                except Exception:record['weather_unavailable']=True
                # 天气请求期间反馈仍可能变化，必须重新读取，不能用取数前反馈授权改表。
                points=platform.read();at=local_time(clock());readiness(points,at,members)
                if (read_schedule(points,at)!=current or protected_schedules(points)!=record['protected_schedules']):
                    record['external_change_during_calculation']=True
                    current=read_schedule(points,at);stop_at=off_time(points,at)
                    if not control_blockers:record['external_schedule_rebased']=rebase_external_schedule(state,current,at)
                    original=local_time(state['original_start_at'])
                    record.update(before_start_at=current.isoformat(),protected_schedules=protected_schedules(points),
                                  original_start_at=original.isoformat(),fixed_freeze_deadline=state['fixed_freeze_deadline'])
                    store.save(record,state)
                snapshot=payload.get('weather_forecast')
                payload=build_payload(points,history,at,original)
                if snapshot is not None:payload['weather_forecast']=snapshot
            model = json.loads((ALGORITHM_DIR / "model.json").read_text(encoding="utf-8"))
            config = json.loads((ALGORITHM_DIR / "forecast_config.json").read_text(encoding="utf-8"))
            policy=day_policy.settings()
            if (at.hour==policy['final_hour'] or manual is True) and stop_at>at:
                remaining=math.ceil((stop_at-at).total_seconds()/300)*5
                if remaining<=policy['maximum_final_horizon_minutes']:
                    config=dict(config,forecast_horizon_minutes=remaining)
            prediction = predictor(payload, model, config, state.get("forecast_state"))
            state["forecast_state"] = deepcopy(prediction["next_state"])
            record["forecast"] = {k: v for k, v in prediction.items() if k != "next_state"}
            record['prediction_completed']=True
            record['control_blockers']=control_blockers
            store.save(record, state)
            if prediction.get('tentative_start_at'):record['target_at']=local_time(prediction['tentative_start_at']).isoformat()
            assessment=None
            if at.hour==policy['final_hour'] or manual is True:
                assessment=day_policy.cancellation_assessment(prediction,at,stop_at,manual=manual is True,authorized_final_replay=replay)
                record['cold_day_assessment']=assessment;store.save(record,state)
            if replay:
                source_assessment=day_policy.cancellation_assessment(final_replay['forecast'],source_at,stop_at,manual=True)
                record['replay_source_assessment']=source_assessment
                if not source_assessment['allowed'] or not assessment or not assessment['allowed']:
                    return finish('replay_not_cancelled','17点回放或实时复核未同时满足取消条件，未改表')
                if at+timedelta(minutes=20)>=current:
                    return finish('frozen','实时取数后已不足15分钟冻结加5分钟确认预算，未改表')
            if control_blockers:
                return finish(control_blockers[0]['status'],control_blockers[0]['reason'])
            if record.get('external_change_during_calculation'):
                return finish('external_change','取数期间时间表改变，本轮已完成预测但不下发；下个有效时刻重读并继续自动判断')
            defer_to_noon=assessment is not None and assessment['allowed']
            if defer_to_noon:
                # 现场已确认错过时刻不补开；此处是跳过今天的专用分支，不是普通未来开机改表。
                candidate=at.replace(hour=policy['defer_start_hour'],minute=policy['defer_start_minute'],second=0,microsecond=0)
                if (at.hour!=policy['final_hour'] and not replay) or candidate>=at:
                    return finish('invalid_candidate','延至次日只允许最后判断时写已过的12点')
                record.update(operation='defer_start_to_noon',schedule_trigger_semantics=policy['schedule_trigger_semantics'],
                              effective_next_start_at=(candidate+timedelta(days=1)).isoformat())
                # 先改小时：12:任意分钟在17点都已过去，不会产生未来的中间开机时刻。
                changes=[{'point_id':pid,'value':value} for pid,value,old in
                         ((HOUR_ID,candidate.hour,current.hour),(MINUTE_ID,candidate.minute,current.minute)) if old!=value]
                deadline=at+timedelta(seconds=policy['confirmation_seconds'])
                if replay:deadline=min(deadline,current-timedelta(minutes=SCHEDULE_FREEZE_MINUTES))
            else:
                if prediction.get('automatic_adjustment_allowed') is False:
                    return finish('insufficient_data','当前历史支持及实时趋势回退条件不足，本轮只展示预测，不自动改表')
                if prediction.get("status") != "tentative" or not prediction.get("tentative_start_at"):
                    return finish("insufficient_data", "算法未生成可用的开机时间候选")
                candidate = local_time(prediction["tentative_start_at"])
                try:
                    changes, deadline = change_plan(current, candidate, original, stop_at, at)
                except StartError as exc:
                    return finish("invalid_candidate", str(exc))
            record["target_at"] = candidate.isoformat()
            record.update(changes=changes, deadline=deadline.isoformat())
            if not changes:
                return finish("no_change", "当前时间表已等于建议时间")
            if not defer_to_noon and (at + timedelta(minutes=SCHEDULE_CONFIRMATION_MINUTES) >= deadline or prediction.get("schedule_change_window_open") is not True):
                return finish("frozen", "距原计划、当前计划或目标的15分钟冻结边界不足5分钟确认预算")
            forecast_members = prediction.get("group_prediction", {}).get("members", members)
            expected = current
            until = min(at + timedelta(minutes=SCHEDULE_CONFIRMATION_MINUTES), deadline)
            for change in changes:
                points = platform.read()
                sent_at = local_time(clock())
                readiness(points, sent_at, forecast_members)
                if (read_schedule(points, sent_at) != expected
                        or protected_schedules(points) != record["protected_schedules"]):
                    state['last_external_schedule_at']=sent_at.isoformat()
                    store.save(record, state)
                    return finish("unconfirmed" if record["attempted"] else "external_change",
                                  "写入前时间表变化，停止剩余字段，不回滚或补发")
                if not allowed(gate):
                    return finish("unconfirmed" if record["attempted"] else "skipped_gate", "发送前策略门控失效")
                # 门控请求本身可能耗时；用其返回后的时刻再检查新鲜度和边界。
                sent_at = local_time(clock())
                readiness(points, sent_at, forecast_members)
                if (sent_at >= until or (not record["attempted"]
                        and not 0 <= (sent_at - slot_at).total_seconds() < 120)):
                    return finish("unconfirmed" if record["attempted"] else "skipped_window", "已超过本轮下发时间窗口")
                entry = {**change, "attempted_at": sent_at.isoformat()}
                record["writes"].append(entry)
                record.update(status="attempted", attempted=True)
                store.save(record)
                posting_at = local_time(clock())
                if (posting_at >= until or (len(record["writes"]) == 1
                        and not 0 <= (posting_at - slot_at).total_seconds() < 120)):
                    # 持久化可能阻塞；本字段尚未发送，移除标记后终止，而不越界POST。
                    record["writes"].pop()
                    record["attempted"] = bool(record["writes"])
                    return finish("unconfirmed" if record["attempted"] else "skipped_window", "持久化后超过下发窗口，本字段未发送")
                try:
                    readiness(points, posting_at, forecast_members)
                except StartError:
                    record["writes"].pop()
                    record["attempted"] = bool(record["writes"])
                    return finish("unconfirmed" if record["attempted"] else "skipped_conditions", "持久化后反馈已过期，本字段未发送")
                try:
                    response = platform.write(change["point_id"], change["value"])
                    entry["response"] = {k: response[k] for k in ("http_status", "accepted") if k in response}
                except Exception:
                    entry["response"] = {"outcome": "unknown_readback_only"}
                store.save(record)
                expected = expected.replace(**{
                    "hour" if change["point_id"] == HOUR_ID else "minute": change["value"]
                })
                confirmed = False
                while local_time(clock()) < until:
                    try:
                        points = platform.read()
                        checked_at = local_time(clock())
                        if (checked_at < until and read_schedule(points, checked_at) == expected
                                and protected_schedules(points) == record["protected_schedules"]
                                and point_stamp(points[change["point_id"]]) > sent_at):
                            confirmed = True
                            entry["confirmed_at"] = checked_at.isoformat()
                            store.save(record)
                            break
                    except Exception:
                        pass
                    remaining = (until - local_time(clock())).total_seconds()
                    if remaining <= 0:
                        break
                    sleep(min(10, remaining))
                if not confirmed:
                    return finish("unconfirmed", "字段未获得设备新上报确认，只记录结果，不重试或继续剩余字段")
            if local_time(clock()) >= until or not full_confirmed(points, record, local_time(clock())):
                return finish("unconfirmed", "最终时间表未完整确认")
            finish_confirmation(store, record, local_time(clock()))
            return record
        except Exception:
            return finish("unconfirmed" if record["attempted"] else "failed",
                          "取数、校验或计算未完成；已尝试字段只回读不重发" if record["attempted"]
                          else "当前数据或本地状态未通过校验，本轮未下发")


def main(argv=None):
    """默认仅展示规则；check实时只读，execute才允许受管修改时间表。"""
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--execute", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--config", type=Path, default=ROOT / "load_forecast_config.json")
    args = parser.parse_args(argv)
    try:
        if args.execute:
            result = execute(Platform(args.config), args.state_dir, weather=WeatherService().get)
        elif args.check:
            points = Platform(args.config).read()
            at = now()
            checks = {}
            try:
                assert_stopped(points, at)
                checks["stopped_without_fault"] = True
            except StartError as exc:
                checks.update(stopped_without_fault=False, hold_reason=str(exc))
            result = {"status": "readonly_check", "write_sent": False,
                      "gate_allowed": local_gate(), "eligible_ahus": eligible_ahus(points, at),
                      "current_start_at": read_schedule(points, at).isoformat(),
                      "current_off_at": off_time(points, at).isoformat(), **checks}
        else:
            result = {"status": "preview", "write_sent": False, "device_id": 12788,
                      "hours": HOURS, "final_hour": day_policy.FINAL_HOUR, "cold_day_target": "12:00", "timezone": "Asia/Shanghai", "minimum_running_ahus": 2,
                      "maximum_signal_age_seconds": 180, "freeze_minutes": SCHEDULE_FREEZE_MINUTES,
                      "confirmation_budget_minutes": SCHEDULE_CONFIRMATION_MINUTES, "allowed_write_points": [HOUR_ID, MINUTE_ID]}
            result['day_policy_write_points']=[HOUR_ID,MINUTE_ID]
            result['model']=json.loads((ALGORITHM_DIR/'forecast_config.json').read_text())['version']
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        return 0 if result["status"] not in {"failed", "unconfirmed", "pending_hold"} else 1
    except Exception:
        print(json.dumps({"status": "failed", "reason": "执行未完成，请核对持久记录；未确认请求不会自动重发"}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
