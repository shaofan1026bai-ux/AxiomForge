"""将每天00:05的固定预测快照接到现有后台调度器。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_load_forecast import DailyLoadForecast
from smart_energy_client import SmartEnergyClient, SmartEnergyError


TIMEZONE = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)
DAILY_JOB_ID = "__daily_load_forecast"
CATCHUP_JOB_ID = "__daily_load_forecast_catchup"
RETRY_JOB_ID = "__daily_load_forecast_retry"


class ForecastRuntime:
    def __init__(self, config_path: Path, *, client=None, clock=None):
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        if type(self.config.get("enabled")) is not bool:
            raise ValueError("负荷预测配置 enabled 必须为布尔值")
        self.enabled = self.config["enabled"]
        self.clock = clock or (lambda: datetime.now(TIMEZONE))
        self.cache_dir = self.config_path.parent / self.config.get("cache_directory", "forecast_cache")
        self.client = client if client is not None else SmartEnergyClient(self.config)
        self.service = DailyLoadForecast(self.config, self.cache_dir, self.client.fetch_history)
        self.scheduler = None
        self.retry_delays = self.config.get("generation_retry_delays_minutes", [5, 10])
        if (not isinstance(self.retry_delays, list)
                or any(type(delay) is not int or delay <= 0 for delay in self.retry_delays)):
            raise ValueError("预测重试间隔必须为正整数分钟数组")
        self.lock = threading.Lock()
        self.last_attempt = None
        self.status_path = self.cache_dir / "last_generation_status.json"
        if self.status_path.exists():
            try:
                self.last_attempt = json.loads(self.status_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.last_attempt = {"status": "unreadable", "error": "上次生成状态文件无法读取"}

    def install(self, scheduler):
        """独立于AI控制总开关；仅取数和计算，不操作设备。"""
        self.scheduler = scheduler
        if not self.enabled:
            return
        if any(scheduler.get_job(job_id) is not None
               for job_id in (DAILY_JOB_ID, CATCHUP_JOB_ID, RETRY_JOB_ID)):
            raise ValueError("负荷预测内部任务ID发生冲突")
        scheduler.add_job(
            self.generate_today,
            "cron",
            hour=0,
            minute=5,
            second=0,
            timezone=TIMEZONE,
            id=DAILY_JOB_ID,
            name="每日00:05生成当日负荷预测",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    def catch_up(self):
        """重启已过00:05且当天没有快照时，后台补生成一次。"""
        if not self.enabled or self.scheduler is None:
            return
        at = self.clock().astimezone(TIMEZONE)
        if at.timetz().replace(tzinfo=None) < time(0, 5):
            return
        try:
            existing = self.service.read_forecast(at.date())
        except (ValueError, OSError):
            # 缓存损坏不能通过重生成静默覆盖。
            LOGGER.warning("当天负荷预测缓存无法读取，需检查缓存文件")
            return
        if existing is None:
            self.scheduler.add_job(
                self.generate_today, "date", run_date=at,
                id=CATCHUP_JOB_ID, name="补生成今日负荷预测",
                max_instances=1, coalesce=True, misfire_grace_time=3600,
                replace_existing=True,
            )

    def _save_status(self, value):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".generation-", suffix=".json", dir=self.cache_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.status_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def generate_today(self):
        at = self.clock().astimezone(TIMEZONE)
        return self._scheduled_attempt(at.date(), 0)

    def _scheduled_attempt(self, day: date, attempt: int):
        at = self.clock().astimezone(TIMEZONE)
        if at.date() != day:
            return {"status": "skipped", "reason": "目标日期已过，不补写为当前日期"}
        result = self.generate(day, at)
        if (self.last_attempt and self.last_attempt["status"] == "failed"
                and not (result and result.get("status") == "already_running")
                and self.scheduler is not None and attempt < len(self.retry_delays)):
            retry_at = at + timedelta(minutes=self.retry_delays[attempt])
            if retry_at.date() == day:
                self.scheduler.add_job(
                    self._scheduled_attempt, "date", run_date=retry_at,
                    args=[day, attempt + 1], id=RETRY_JOB_ID,
                    name="重试生成当日负荷预测", replace_existing=True,
                    max_instances=1, coalesce=True, misfire_grace_time=3600,
                )
        return result

    def generate(self, day: date, at: datetime):
        """计划任务与手动补跑使用同一入口，成功快照不会被覆盖。"""
        if not self.lock.acquire(blocking=False):
            return {"status": "already_running"}
        try:
            try:
                result = self.service.generate(day, at)
                status = {
                    "status": "success", "date": day.isoformat(),
                    "attempted_at": at.astimezone(TIMEZONE).isoformat(),
                    "error": None,
                    "reference_date":result['reference_date'],
                    "fallback_used":result.get('fallback_used',False),
                    "interpolated_sample_count":result.get('reference_quality',{}).get('interpolated_sample_count',0),
                }
            except (SmartEnergyError, ValueError) as exc:
                # 两个业务模块的异常均应为脱敏文本，不附带请求/响应内容。
                status = {
                    "status": "failed", "date": day.isoformat(),
                    "attempted_at": at.astimezone(TIMEZONE).isoformat(),
                    "error": str(exc),
                    "reference_attempts":getattr(exc,'reference_attempts',[]),
                }
                result = None
                LOGGER.warning("当日负荷预测生成失败：%s", status["error"])
            except Exception:
                status = {
                    "status": "failed", "date": day.isoformat(),
                    "attempted_at": at.astimezone(TIMEZONE).isoformat(),
                    "error": "预测生成发生内部错误，请检查后台运行环境",
                }
                result = None
                LOGGER.warning("当日负荷预测生成发生内部错误")
            self.last_attempt = status
            try:
                self._save_status(status)
            except OSError:
                LOGGER.warning("预测生成状态无法写入磁盘")
            return result if result is not None else status
        finally:
            self.lock.release()

    def response(self, at: datetime, target_date: date | None = None):
        return self.service.response(at, target_date)

    def status(self):
        at = self.clock().astimezone(TIMEZONE)
        state = "missing"
        metadata = None
        try:
            metadata = self.service.read_forecast(at.date())
            if metadata is not None:
                state = "ready"
        except (ValueError, OSError):
            state = "invalid_cache"
        job = self.scheduler.get_job(DAILY_JOB_ID) if self.scheduler is not None else None
        next_run = getattr(job, "next_run_time", None)
        retry = self.scheduler.get_job(RETRY_JOB_ID) if self.scheduler is not None else None
        retry_run = getattr(retry, "next_run_time", None)
        return {
            "enabled": self.enabled,
            "timezone": "Asia/Shanghai",
            "schedule": {"hour": 0, "minute": 5},
            "scheduled": job is not None,
            "next_run_time": next_run.isoformat() if next_run is not None else None,
            "next_retry_time": retry_run.isoformat() if retry_run is not None else None,
            "date": at.date().isoformat(),
            "forecast_status": state,
            "forecast_generated_at": metadata.get("generated_at") if metadata is not None else None,
            "reference_date":metadata.get('reference_date') if metadata else None,
            "fallback_used":metadata.get('fallback_used',False) if metadata else False,
            "interpolated_sample_count":metadata.get('reference_quality',{}).get('interpolated_sample_count',0) if metadata else 0,
            "last_generation": self.last_attempt,
            "authentication": {
                "mode": "automatic_login",
                "credentials_configured": bool(os.environ.get("XIAOMAI_ACCOUNT")) and bool(os.environ.get("XIAOMAI_PASSWORD")),
            },
        }


def create_forecast_runtime(config_path: Path, *, client=None, clock=None):
    path = Path(config_path)
    if not path.exists():
        return None
    return ForecastRuntime(path, client=client, clock=clock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("load_forecast_config.json"))
    parser.add_argument("--date", type=date.fromisoformat, help="补生成的目标日期，默认北京时间今日")
    parser.add_argument("--status", action="store_true", help="只读取状态，不登录平台、不取数")
    args = parser.parse_args()
    runtime = ForecastRuntime(args.config)
    if args.status:
        print(json.dumps(runtime.status(), ensure_ascii=False, indent=2))
        return 0
    at = datetime.now(TIMEZONE)
    result = runtime.generate(args.date or at.date(), at)
    state = runtime.last_attempt or {"status": "failed"}
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if result and state["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
