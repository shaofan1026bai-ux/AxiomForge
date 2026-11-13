"""外部配置的航班只读连接；不持久化凭据或原始航班。"""

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values
from surrealdb import Surreal

from axiomforge.flights import AIRPORT, SHANGHAI, FlightSnapshot, normalize_flights

FLIGHT_SQL = "SELECT * FROM flight WHERE airptIataCd = $airport AND planDate IN $dates;"
MAX_FLIGHT_RANGE_DAYS = 31


class FlightSourceError(ValueError):
    """供页面显示的脱敏错误，不携带底层连接异常。"""


def load_flights(day: date) -> FlightSnapshot:
    """固定查询本场与相邻计划日，所有用户输入均绑定为参数。"""
    return load_flights_range(day, day)


def load_flights_range(start_day: date, end_day: date) -> FlightSnapshot:
    """一次查询本场日期范围与首尾相邻计划日，保留跨午夜航班。"""
    if start_day <= date.min or end_day >= date.max or start_day > end_day:
        raise FlightSourceError("请选择有效的航班日期范围。")
    days = (end_day - start_day).days + 1
    if days > MAX_FLIGHT_RANGE_DAYS:
        raise FlightSourceError(f"航班查询范围最多 {MAX_FLIGHT_RANGE_DAYS} 天。")
    dates = [(start_day + timedelta(days=n)).isoformat() + " 00:00:00" for n in range(-1, days + 1)]

    path = os.environ.get("AXIOMFORGE_FLIGHT_ENV_FILE", "")
    if path and not Path(path).expanduser().is_file():
        raise FlightSourceError("航班连接配置文件不可读，请检查服务启动配置。")
    values = dotenv_values(Path(path).expanduser(), interpolate=False) if path else {}
    settings = {**{k: v for k, v in values.items() if v is not None}, **os.environ}
    required = ("SURREALDB_URL", "SURREALDB_NAMESPACE", "SURREALDB_DATABASE")
    if any(not settings.get(k, "").strip() for k in required):
        raise FlightSourceError("尚未配置航班数据源，请在服务端指定外部连接配置。")
    parsed = urlsplit(settings["SURREALDB_URL"])
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        raise FlightSourceError("航班连接地址格式不正确。")
    if parsed.username or parsed.password:
        raise FlightSourceError("连接地址不能包含凭据，请使用独立认证配置。")
    username, password = (
        settings.get("SURREALDB_USERNAME", ""),
        settings.get("SURREALDB_PASSWORD", ""),
    )
    token = settings.get("SURREALDB_TOKEN", "")
    level = settings.get("SURREALDB_AUTH_LEVEL", "") or "root"
    if level not in {"root", "namespace", "database"}:
        raise FlightSourceError("航班认证级别配置不正确。")
    if (username or password) and not (username and password):
        raise FlightSourceError("航班账号与密码需同时配置。")
    if not username and not token:
        raise FlightSourceError("航班数据源缺少认证配置。")
    try:
        with Surreal(settings["SURREALDB_URL"].strip().rstrip("/")) as db:
            if username:
                credentials: dict[str, Any] = {"username": username, "password": password}
                if level in {"namespace", "database"}:
                    credentials["namespace"] = settings["SURREALDB_NAMESPACE"]
                if level == "database":
                    credentials["database"] = settings["SURREALDB_DATABASE"]
                db.signin(credentials)
            else:
                db.authenticate(token)
            db.use(settings["SURREALDB_NAMESPACE"], settings["SURREALDB_DATABASE"])
            variables: dict[str, Any] = {"airport": AIRPORT, "dates": dates}
            rows: Any = db.query(FLIGHT_SQL, variables)
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise TypeError("unexpected_result")
        return normalize_flights(rows, datetime.now(SHANGHAI))
    except Exception:
        # 不向UI、日志传播包含地址、SQL或认证数据的底层异常。
        raise FlightSourceError(
            "航班查询失败，请检查网络、认证与库表权限；原快照已保留。"
        ) from None
