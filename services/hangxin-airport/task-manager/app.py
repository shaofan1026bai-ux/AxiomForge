"""定时脚本、AI 控制模式及运行统计服务。"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from datetime import date as CalendarDate
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from load_forecast import make_load_forecast
from load_response import format_load_response
from daily_load_forecast import ForecastCacheError, HistoryValidationError
from forecast_runtime import create_forecast_runtime
from smart_energy_client import SmartEnergyError
from runtime_stats import RuntimeStats
from nowapi_weather import WeatherService, WeatherError, format_forecast_response


TIMEZONE = ZoneInfo("Asia/Shanghai")
CHECKPOINT_INTERVAL_SECONDS = 10
LOGGER = logging.getLogger(__name__)


def display_hours(value: float) -> float:
    """仅序列化时调整显示精度，数据库与内部计算仍保留原值。"""
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


Hours = Annotated[float, PlainSerializer(display_hours, return_type=float)]


class WeatherHourResponse(BaseModel):
    time: datetime = Field(description="预报有效时刻，北京时间")
    date: CalendarDate = Field(description="该预报点对应的北京时间日期，YYYY-MM-DD")
    time_label: str = Field(description="完整展示时间，例如：今天（2026-11-09）15:00；相对日期按响应时的北京时间计算")
    temperature_c: float = Field(description="逐小时预测室外温度，℃")
    weather: str


class WeatherHourGroupResponse(BaseModel):
    date: date
    date_label: str = Field(description="分组标题，例如：明天（2026-11-10）")
    hourly: list[WeatherHourResponse]


class WeatherDayResponse(BaseModel):
    date: date
    date_label: str = Field(description="相对日期和完整日期，例如：明天（2026-11-10）")
    day_temperature_c: float
    night_temperature_c: float


class WeatherForecastResponse(BaseModel):
    source: str
    upstream_app: str
    city: str
    city_id: str
    timezone: str
    unit: str
    fetched_at: datetime = Field(description="本服务请求完成时间，不是预报发布时间")
    forecast_issued_at: datetime | None = Field(default=None, description="上游未提供时为null")
    freshness_basis: str
    reference_date: date = Field(description="本次响应的北京时间日期，今天／明天等标签以此为准，不是预报发布时间")
    hourly: list[WeatherHourResponse]
    hourly_by_date: list[WeatherHourGroupResponse] = Field(description="逐小时预报按北京时间日期分组，与hourly为同一批数据")
    daily: list[WeatherDayResponse]
    cached: bool


class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = ""
    category: str = ""
    control_points: list[str] = Field(default_factory=list)
    script: str = Field(min_length=1)
    interval_seconds: float = Field(gt=0, allow_inf_nan=False)
    execution_hours: list[Annotated[StrictInt, Field(ge=0, le=23)]] | None = Field(default=None, min_length=1)
    execution_times: list[Annotated[StrictStr, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]] | None = Field(default=None, min_length=1)
    enabled: bool = False

    @field_validator("execution_hours")
    @classmethod
    def validate_execution_hours(cls, value: list[int] | None) -> list[int] | None:
        """整点列表不得重复；未配置时沿用既有间隔调度。"""
        if value is not None and len(value) != len(set(value)):
            raise ValueError("execution_hours 中的小时不能重复")
        return value

    @field_validator("execution_times")
    @classmethod
    def validate_execution_times(cls, value: list[str] | None) -> list[str] | None:
        """固定时刻必须唯一，避免同一策略因重复配置触发多次。"""
        if value is not None and len(value) != len(set(value)):
            raise ValueError("execution_times 中的时刻不能重复")
        return value

    @model_validator(mode="after")
    def validate_schedule_choice(self):
        """整点列表和精确时刻不能并用，避免产生两个冲突的调度解释。"""
        if self.execution_hours is not None and self.execution_times is not None:
            raise ValueError("execution_hours 与 execution_times 不能同时配置")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_mode_enabled: bool = False
    tasks: list[TaskConfig]


class OverviewItem(BaseModel):
    id: str
    name: str
    enabled: bool = Field(description="实际调度是否开启，即 AI 模式和策略选择同时开启")
    monthly_runtime_hours: Hours | None = Field(description="北京时间本月累计有效开启小时数，统一保留1位小数")


class OverviewResponse(BaseModel):
    total: int
    month: str
    items: list[OverviewItem]


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class ModeResponse(BaseModel):
    enabled: bool
    status: str
    selected_task_count: int
    active_task_count: int
    updated_at: str


class RuntimeItem(BaseModel):
    id: str
    name: str
    enabled: bool = Field(description="保存的单策略开关选择")
    effective_enabled: bool = Field(description="AI 模式与策略同时开启")
    monthly_runtime_hours: Hours
    total_runtime_hours: Hours


class RuntimeResponse(BaseModel):
    enabled: bool
    month: str
    timezone: str
    monthly_mode_runtime_hours: Hours
    total_mode_runtime_hours: Hours
    monthly_strategy_runtime_hours: Hours = Field(description="各策略本月累计时长的最大值，不是总和；统一保留1位小数")
    tasks: list[RuntimeItem]


class LoadPoint(BaseModel):
    time: str = Field(description="两分钟区间结束时间，首点00:02、末点次日00:00，含北京时间时区")
    actual_load_kw: float | None = Field(description="使用区间左端采样值作平均负荷估计；缺测及未完成区间为null，单位kW")
    predicted_load_kw: float | None = Field(description="固定的当日预测负荷；当日预测未生成时为 null，单位 kW")


class LoadResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: str
    timezone: str
    scope: str
    source: str
    interval_minutes: int
    load_unit: str
    energy_unit: str
    actual_energy_kwh: float | None = Field(description="按区间左端负荷分段保持积分的累计冷量估计(kWh)；已完成区间有缺测时为null")
    predicted_energy_kwh: float | None = Field(description="相同已完成时间窗的累计预测冷量(kWh)，预测缺失时为null")
    points: list[LoadPoint] = Field(description="全天720个两分钟点，未来及缺测的实际值为null")


@dataclass
class Task:
    config: TaskConfig
    script: Path
    log_file: Path
    enabled: bool = False
    enabled_since: datetime | None = None
    scheduled: bool = False
    generation: int = 0
    stopping: bool = False
    stop_requested: bool = False
    process: subprocess.Popen | None = None
    started_at: str | None = None
    last_finished_at: str | None = None
    last_result: str | None = None
    last_exit_code: int | None = None
    last_error: str | None = None


def now() -> str:
    return datetime.now(TIMEZONE).isoformat()


class TaskManager:
    def __init__(self, config_path: Path):
        config_path = config_path.resolve()
        config = Config.model_validate_json(config_path.read_text(encoding="utf-8"))
        self.lock = threading.RLock()
        self.scheduler = BackgroundScheduler(timezone=TIMEZONE)
        self.tasks: dict[str, Task] = {}
        self.closing = False
        self.started = False
        self.transitioning = False
        self.mode_since: datetime | None = None
        self.updated_at = now()
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread: threading.Thread | None = None
        log_dir = config_path.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        for item in config.tasks:
            if item.id in self.tasks:
                raise ValueError(f"任务 ID 重复：{item.id}")
            script = (config_path.parent / item.script).resolve()
            if not script.is_file():
                raise ValueError(f"脚本不存在：{script}")
            task = Task(item, script, log_dir / f"{item.id}.log")
            self.tasks[item.id] = task
            self._add_job(task, paused=True)
        self.runtime_stats = RuntimeStats(config_path.parent / "runtime.sqlite3")
        self.ai_mode_enabled, selections = self.runtime_stats.load_state(
            config.ai_mode_enabled, {item.id: item.enabled for item in config.tasks}
        )
        for task_id, selected in selections.items():
            self.tasks[task_id].enabled = selected

    def _add_job(self, task: Task, *, paused: bool = False) -> None:
        """固定时刻按分钟分组为cron；整点和间隔策略沿用原行为。"""
        options = {"next_run_time": None} if paused else {}
        if task.config.execution_times is not None or task.config.execution_hours is not None:
            if task.config.execution_times is not None:
                # 分钟相同的小时合并，避免21:00截止配置意外多出21:30。
                groups: dict[int, list[int]] = {}
                for execution_time in task.config.execution_times:
                    hour, minute = map(int, execution_time.split(":"))
                    groups.setdefault(minute, []).append(hour)
                triggers = [CronTrigger(hour=",".join(map(str, sorted(hours))),
                                        minute=minute, second=0, timezone=TIMEZONE)
                            for minute, hours in sorted(groups.items())]
                trigger = triggers[0] if len(triggers) == 1 else OrTrigger(triggers)
            else:
                trigger = CronTrigger(
                    hour=",".join(str(hour) for hour in task.config.execution_hours),
                    minute=0, second=0, timezone=TIMEZONE,
                )
            self.scheduler.add_job(
                self._run, trigger,
                args=[task.config.id, task.generation], id=task.config.id,
                replace_existing=True, max_instances=1, coalesce=True,
                misfire_grace_time=60, **options,
            )
            return
        self.scheduler.add_job(
            self._run, "interval", seconds=task.config.interval_seconds,
            args=[task.config.id, task.generation], id=task.config.id,
            replace_existing=True, max_instances=1, coalesce=True, **options,
        )

    def start(self) -> None:
        with self.lock:
            self.scheduler.start(paused=True)
            self.started = True
            # 上次检查点只用于恢复累计数，不把停服时间当作活动时段。
            self._checkpoint_locked(datetime.now(TIMEZONE))
            self._sync_schedule_locked()
            self.scheduler.resume()
        self._checkpoint_thread = threading.Thread(
            target=self._checkpoint_loop, name="runtime-checkpoint", daemon=True
        )
        self._checkpoint_thread.start()

    def get(self, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise HTTPException(status_code=404, detail="任务不存在")
        return self.tasks[task_id]

    def _effective(self, task: Task) -> bool:
        return self.ai_mode_enabled and task.enabled and not self.closing

    def _checkpoint_locked(
        self, at: datetime, *, mode: bool | None = None,
        selections: dict[str, bool] | None = None,
    ) -> None:
        new_mode = self.ai_mode_enabled if mode is None else mode
        choices = {task_id: task.enabled for task_id, task in self.tasks.items()}
        if selections:
            choices.update(selections)
        # 累计旧活动段与保存新选择在同一事务提交，成功后才推进内存起点。
        self.runtime_stats.checkpoint(
            at, self.mode_since,
            {task_id: task.enabled_since for task_id, task in self.tasks.items()
             if task.enabled_since is not None},
            new_mode, choices,
        )
        self.ai_mode_enabled = new_mode
        self.mode_since = at if new_mode and not self.closing else None
        for task_id, task in self.tasks.items():
            task.enabled = choices[task_id]
            task.enabled_since = at if self._effective(task) else None

    def checkpoint(self) -> None:
        with self.lock:
            if not self.closing:
                self._checkpoint_locked(datetime.now(TIMEZONE))

    def _checkpoint_loop(self) -> None:
        # 独立线程避免长脚本占满调度线程池，导致十秒检查点被阻塞。
        while not self._checkpoint_stop.wait(CHECKPOINT_INTERVAL_SECONDS):
            try:
                self.checkpoint()
            except Exception:
                LOGGER.exception("保存运行时长检查点失败")

    def _sync_schedule_locked(self) -> None:
        for task in self.tasks.values():
            effective = self._effective(task)
            if task.scheduled == effective:
                continue
            task.generation += 1
            if effective:
                # 使用新的 Job 对象，旧队列项持有旧代号，不能在重新开启后偷跑。
                self._add_job(task)
            else:
                self.scheduler.pause_job(task.config.id)
            task.scheduled = effective

    def _finish(self, task: Task, process: subprocess.Popen) -> None:
        if task.process is not process:
            return
        task.last_finished_at = now()
        task.last_exit_code = process.returncode
        task.last_result = (
            "stopped" if task.stop_requested else "success" if process.returncode == 0 else "failed"
        )
        task.last_error = (
            f"脚本退出码为 {process.returncode}，请查看日志"
            if task.last_result == "failed" else None
        )
        task.process = None
        task.stopping = False
        task.stop_requested = False

    def _run(self, task_id: str, generation: int) -> None:
        task = self.get(task_id)
        with self.lock:
            if (not self._effective(task) or task.stopping
                    or task.process is not None or task.generation != generation):
                return
            task.started_at = now()
            try:
                with task.log_file.open("a", encoding="utf-8") as output:
                    output.write(f"\n--- {task.started_at} 开始执行 ---\n")
                    output.flush()
                    process = subprocess.Popen(
                        [sys.executable, "-u", str(task.script)], cwd=task.script.parent,
                        stdout=output, stderr=subprocess.STDOUT,
                        start_new_session=(os.name == "posix"),
                    )
                task.process = process
            except OSError as exc:
                task.last_finished_at = now()
                task.last_result = "failed"
                task.last_exit_code = None
                task.last_error = str(exc)
                LOGGER.exception("启动任务 %s 失败", task_id)
                return
        process.wait()
        with self.lock:
            if not task.stopping:
                self._finish(task, process)

    def snapshot(self, task_id: str) -> dict:
        task = self.get(task_id)
        with self.lock:
            process = task.process
            if process is not None and process.poll() is not None and not task.stopping:
                self._finish(task, process)
            running = task.process is not None and task.process.poll() is None
            effective = self._effective(task)
            job = self.scheduler.get_job(task_id)
            next_run = job.next_run_time if job is not None and effective else None
            result = {
                "id": task_id, "name": task.config.name or task_id,
                "category": task.config.category, "control_points": task.config.control_points,
                "script": str(task.script), "interval_seconds": task.config.interval_seconds,
                "enabled": task.enabled, "ai_mode_enabled": self.ai_mode_enabled,
                "effective_enabled": effective, "running": running,
                "status": (
                    "stopping" if task.stopping else "running" if running else
                    "idle" if effective else "paused" if task.enabled else "disabled"
                ),
                "pid": task.process.pid if running else None,
                "started_at": task.started_at,
                "next_run_time": next_run.isoformat() if next_run else None,
                "last_finished_at": task.last_finished_at,
                "last_result": task.last_result, "last_exit_code": task.last_exit_code,
                "last_error": task.last_error, "log_file": str(task.log_file),
            }
            if task.config.execution_hours is not None:
                result["execution_hours"] = list(task.config.execution_hours)
            if task.config.execution_times is not None:
                # 小时概要继续提供给旧页面；精确触发时刻以execution_times为准。
                result["execution_hours"] = sorted({int(value[:2]) for value in task.config.execution_times})
                result["execution_times"] = list(task.config.execution_times)
            return result

    def mode_snapshot(self) -> dict:
        with self.lock:
            return {
                "enabled": self.ai_mode_enabled,
                "status": "stopping" if self.transitioning else "enabled" if self.ai_mode_enabled else "disabled",
                "selected_task_count": sum(task.enabled for task in self.tasks.values()),
                "active_task_count": sum(self._effective(task) for task in self.tasks.values()),
                "updated_at": self.updated_at,
            }

    def _ensure_mutable(self) -> None:
        if self.closing:
            raise HTTPException(status_code=503, detail="服务正在退出")
        if self.transitioning:
            raise HTTPException(status_code=409, detail="正在停止脚本，请稍后重试")

    def _save_change(self, *, mode: bool | None = None, selections: dict | None = None) -> None:
        at = datetime.now(TIMEZONE)
        try:
            self._checkpoint_locked(at, mode=mode, selections=selections)
        except (sqlite3.Error, OSError) as exc:
            LOGGER.exception("保存控制状态失败")
            raise HTTPException(status_code=503, detail="控制状态保存失败，请重试") from exc
        self.updated_at = at.isoformat()
        self._sync_schedule_locked()

    def enable(self, task_id: str) -> dict:
        task = self.get(task_id)
        with self.lock:
            self._ensure_mutable()
            if task.stopping:
                raise HTTPException(status_code=409, detail="任务正在停止，请稍后开启")
            if not task.enabled:
                self._save_change(selections={task_id: True})
            return self.snapshot(task_id)

    def disable(self, task_id: str) -> dict:
        task = self.get(task_id)
        with self.lock:
            self._ensure_mutable()
            if task.enabled:
                self._save_change(selections={task_id: False})
            self.transitioning = True
        try:
            self._stop_task(task_id)
        finally:
            with self.lock:
                self.transitioning = False
        return self.snapshot(task_id)

    def set_mode(self, enabled: bool) -> dict:
        with self.lock:
            self._ensure_mutable()
            if enabled:
                if any(task.stop_requested and task.process is not None
                       and task.process.poll() is None for task in self.tasks.values()):
                    raise HTTPException(status_code=409, detail="仍有未停止的脚本，请先关闭重试")
            if self.ai_mode_enabled != enabled:
                self._save_change(mode=enabled)
            if enabled:
                return self.mode_snapshot()
            self.transitioning = True
        errors = []
        try:
            for task_id in self.tasks:
                try:
                    self._stop_task(task_id)
                except Exception:
                    LOGGER.exception("停止任务 %s 失败", task_id)
                    errors.append(task_id)
        finally:
            with self.lock:
                self.transitioning = False
        if errors:
            raise HTTPException(status_code=503, detail={"message": "部分脚本停止失败，请重试关闭", "tasks": errors})
        return self.mode_snapshot()

    def overview(self, limit: int) -> dict:
        with self.lock:
            at = datetime.now(TIMEZONE)
            items = []
            for task_id, task in list(self.tasks.items())[:limit]:
                try:
                    hours = self.runtime_stats.hours(task_id, at, task.enabled_since)
                except (sqlite3.Error, OSError):
                    LOGGER.exception("读取任务 %s 的运行时长失败", task_id)
                    hours = None
                items.append({
                    "id": task_id, "name": task.config.name or task_id,
                    "enabled": self._effective(task), "monthly_runtime_hours": hours,
                })
            return {"total": len(self.tasks), "month": at.strftime("%Y-%m"), "items": items}

    def runtime(self) -> dict:
        with self.lock:
            at = datetime.now(TIMEZONE)
            try:
                items = [{
                    "id": task_id, "name": task.config.name or task_id,
                    "enabled": task.enabled, "effective_enabled": self._effective(task),
                    "monthly_runtime_hours": self.runtime_stats.hours(task_id, at, task.enabled_since),
                    "total_runtime_hours": self.runtime_stats.total_hours(task_id, at, task.enabled_since),
                } for task_id, task in self.tasks.items()]
                monthly_mode = self.runtime_stats.mode_hours(at, self.mode_since)
                total_mode = self.runtime_stats.mode_total_hours(at, self.mode_since)
            except (sqlite3.Error, OSError) as exc:
                raise HTTPException(status_code=503, detail="运行时长暂时无法读取") from exc
            maximum = max((item["monthly_runtime_hours"] for item in items), default=0.0)
            return {
                "enabled": self.ai_mode_enabled, "month": at.strftime("%Y-%m"),
                "timezone": "Asia/Shanghai", "monthly_mode_runtime_hours": monthly_mode,
                "total_mode_runtime_hours": total_mode,
                "monthly_strategy_runtime_hours": maximum,
                "tasks": items,
            }

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        def send(force: bool) -> None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
                elif force:
                    process.kill()
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
        send(force=False)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            send(force=True)
            process.wait(timeout=3)
        if os.name == "posix":
            send(force=True)

    def _stop_task(self, task_id: str) -> None:
        with self.lock:
            task = self.get(task_id)
            process = task.process
            if process is None or task.stopping:
                return
            if process.poll() is not None:
                self._finish(task, process)
                return
            task.stopping = True
            task.stop_requested = True
        try:
            self._stop_process(process)
        except (OSError, subprocess.TimeoutExpired) as exc:
            with self.lock:
                task.stopping = False
                task.last_error = f"停止失败：{exc}"
            raise HTTPException(status_code=503, detail="停止失败，请查询任务状态后重试") from exc
        with self.lock:
            self._finish(task, process)

    def shutdown(self) -> None:
        with self.lock:
            if self.closing:
                return
            self.closing = True
            if self.started:
                self.scheduler.pause()
                try:
                    self._checkpoint_locked(datetime.now(TIMEZONE))
                except Exception:
                    LOGGER.exception("退出时保存运行时长失败")
            self._checkpoint_stop.set()
        if self._checkpoint_thread is not None:
            self._checkpoint_thread.join(timeout=6)
        wait = True
        for task_id in self.tasks:
            try:
                self._stop_task(task_id)
            except Exception:
                wait = False
                LOGGER.exception("退出时停止任务 %s 失败", task_id)
        if self.started:
            self.scheduler.shutdown(wait=wait)


def create_app(config_path: Path | str | None = None) -> FastAPI:
    config_path = Path(config_path) if config_path else Path(__file__).with_name("tasks.json")
    weather = WeatherService(config_path.with_name("nowapi_weather_config.json"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        manager = TaskManager(config_path)
        application.state.manager = manager
        forecast = create_forecast_runtime(config_path.with_name("load_forecast_config.json"))
        application.state.forecast_runtime = forecast
        try:
            if forecast is not None:
                forecast.install(manager.scheduler)
            manager.start()
            if forecast is not None:
                forecast.catch_up()
            yield
        finally:
            manager.shutdown()

    application = FastAPI(title="航信策略临时接口", lifespan=lifespan)

    @application.get("/weather-forecast", response_model=WeatherForecastResponse, tags=["天气预测"], summary="获取芒市逐小时天气预报",
                     description="读取NowAPI整合版逐小时及日预报，温度单位℃，时间均为北京时间。hourly保留原始时间并补充完整日期标签；hourly_by_date按日期分组，便于区分今天和明天。标签以reference_date为准，逐小时数据是滚动预报，不保证覆盖每个日期的完整24小时。默认缓存15分钟；未配置、城市不符或预报无效时返回503。fetched_at是取数时间，上游未提供预报发布时间。")
    def weather_forecast():
        """统一提供给前端和航班策略，接口不包含任何NowAPI密钥。"""
        try:
            return format_forecast_response(weather.get(), datetime.now(TIMEZONE))
        except WeatherError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @application.put("/ai-control-mode", response_model=ModeResponse, tags=["AI 控制"], summary="设置 AI 控制模式")
    def set_ai_mode(body: ModeRequest):
        """关闭时停止全部脚本并保留策略选择；重新开启只恢复已选策略。"""
        return application.state.manager.set_mode(body.enabled)

    @application.get("/ai-control-mode/runtime", response_model=RuntimeResponse, tags=["AI 控制"], summary="查询模式与策略运行时长")
    def ai_runtime():
        """读取当前模式状态、本月与历史累计；策略展示时长取本月最大值。"""
        return application.state.manager.runtime()

    @application.get("/load-forecast", response_model=LoadResponse, tags=["负荷预测"], summary="读取当日固定预测与实际负荷")
    def load_forecast(target_date: date | None = Query(default=None, alias="date", description="默认北京时间今日；允许过去日期，不允许未来日期")):
        try:
            forecast = application.state.forecast_runtime
            if forecast is not None:
                if not forecast.enabled:
                    raise HTTPException(status_code=503, detail="真实负荷功能已停用")
                at = datetime.now(TIMEZONE)
                return format_load_response(forecast.response(at, target_date), at)
            return make_load_forecast(datetime.now(TIMEZONE), target_date)
        except SmartEnergyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (ForecastCacheError, HistoryValidationError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/load-forecast/status", tags=["负荷预测"], summary="查询每日00:05生成任务与缓存状态")
    def load_forecast_status():
        forecast = application.state.forecast_runtime
        if forecast is None:
            return {"enabled": False, "source": "mock", "timezone": "Asia/Shanghai"}
        return forecast.status()

    @application.get("/load-forecast/details", tags=["负荷预测"], summary="查询负荷曲线及详细诊断信息")
    def load_forecast_details(target_date: date | None = Query(default=None, alias="date")):
        try:
            forecast = application.state.forecast_runtime
            if forecast is None:
                return make_load_forecast(datetime.now(TIMEZONE), target_date)
            if not forecast.enabled:
                raise HTTPException(status_code=503, detail="真实负荷功能已停用")
            at = datetime.now(TIMEZONE)
            return format_load_response(forecast.response(at, target_date), at, details=True)
        except (SmartEnergyError, ForecastCacheError, HistoryValidationError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/tasks", tags=["策略管理"], summary="查询策略列表", description="查询策略配置、开关选择及实际执行状态。running=true 仅返回当前正在执行脚本的策略；不传则返回全部策略。")
    def list_tasks(running: bool | None = None):
        manager = application.state.manager
        tasks = [manager.snapshot(task_id) for task_id in manager.tasks]
        return [task for task in tasks if running is None or task["running"] == running]

    @application.get("/tasks/overview", response_model=OverviewResponse, tags=["策略管理"], summary="查询策略执行概览", description="返回策略总数、月份和策略运行时长，默认展示前3条。时长单位为小时，统一保留1位小数。")
    def task_overview(limit: int = Query(default=3, ge=1, le=100)):
        return application.state.manager.overview(limit)

    @application.get("/tasks/{task_id}", tags=["策略管理"], summary="查询策略详情", description="根据策略ID查询配置、开关选择、生效状态、脚本进程及最近执行结果。")
    def get_task(task_id: str):
        return application.state.manager.snapshot(task_id)

    @application.post("/tasks/{task_id}/enable", tags=["策略管理"], summary="开启策略", description="保存策略的开启选择。AI模式开启时恢复定时执行；AI模式关闭时仅保存选择，不执行也不计时。")
    def enable_task(task_id: str):
        """保存策略开启选择；AI 模式关闭时仅保存，不执行也不计时。"""
        return application.state.manager.enable(task_id)

    @application.post("/tasks/{task_id}/disable", tags=["策略管理"], summary="关闭策略", description="停止该策略的后续定时执行，终止正在执行的脚本，并结算有效运行时长。")
    def disable_task(task_id: str):
        return application.state.manager.disable(task_id)

    return application


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
