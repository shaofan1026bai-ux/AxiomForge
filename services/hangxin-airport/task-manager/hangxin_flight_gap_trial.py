"""读取真实LUM航班做空档试算；只有固定SELECT，没有设备控制或调度安装。"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
FLIGHT_SQL = "SELECT * FROM flight WHERE airptIataCd = $airport AND planDate IN $dates;"


class FlightTrialError(ValueError):
    """只允许固定错误信息，底层连接上下文和认证数据不能进入报告。"""


def fetch_raw_flights(day, env_file):
    """认证只在内存使用，原始航班不落盘，返回完整三个计划日的记录。"""
    from dotenv import dotenv_values
    from surrealdb import Surreal

    if not Path(env_file).is_file():
        raise FlightTrialError("航班连接配置文件不可读")
    keys = ("URL", "NAMESPACE", "DATABASE", "USERNAME", "PASSWORD", "TOKEN", "AUTH_LEVEL")
    keys = ["SURREALDB_" + key for key in keys]
    try:
        values = dotenv_values(env_file, interpolate=False)
    except (OSError, ValueError, TypeError):
        raise FlightTrialError("航班连接配置文件读取失败") from None
    settings = {key: os.environ.get(key, values.get(key) or "") for key in keys}
    if any(not settings[key].strip() for key in keys[:3]):
        raise FlightTrialError("缺少航班数据源连接配置")
    address = settings["SURREALDB_URL"].strip().rstrip("/")
    parsed = urlsplit(address)
    if (parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment or parsed.query):
        raise FlightTrialError("航班连接地址格式无效")
    username, password = settings["SURREALDB_USERNAME"], settings["SURREALDB_PASSWORD"]
    token = settings["SURREALDB_TOKEN"]
    level = settings["SURREALDB_AUTH_LEVEL"] or "root"
    if level not in {"root", "namespace", "database"}:
        raise FlightTrialError("航班认证级别配置无效")
    if (username or password) and not (username and password):
        raise FlightTrialError("航班账号与密码必须同时配置")
    if not username and not token:
        raise FlightTrialError("缺少航班数据源认证配置")
    dates = [(day + timedelta(days=n)).isoformat() + " 00:00:00" for n in (-1, 0, 1)]
    started_at = datetime.now(ZONE)
    try:
        with Surreal(address) as database:
            if username:
                credentials = {"username": username, "password": password}
                if level in {"namespace", "database"}:
                    credentials["namespace"] = settings["SURREALDB_NAMESPACE"]
                if level == "database":
                    credentials["database"] = settings["SURREALDB_DATABASE"]
                database.signin(credentials)
            else:
                database.authenticate(token)
            database.use(settings["SURREALDB_NAMESPACE"], settings["SURREALDB_DATABASE"])
            rows = database.query(FLIGHT_SQL, {"airport": "LUM", "dates": dates})
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise TypeError("unexpected_result")
    except Exception:
        raise FlightTrialError("航班只读查询失败，请检查网络、认证及库表权限") from None
    return rows, {"airport": "LUM", "plan_dates": dates, "query_started_at": started_at.isoformat(),
                  "query_completed_at": datetime.now(ZONE).isoformat(), "complete_query_returned": True,
                  "source_completeness_guaranteed": False, "limited": False, "device_control_sent": False}


def atomic_write(path, value):
    """先完整落盘再替换，测试结果不混入认证配置。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".flight-trial-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_results(result, output_dir):
    """JSON保存诊断，CSV便于核对全部空档，不保存原始数据库响应。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    atomic_write(output_dir / "真实数据测试结果.json", json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["时刻口径", "区域", "空档开始", "空档结束", "空档分钟", "前序航班", "后续航班", "严格包含20点"])
    for mode, details in result["modes"].items():
        for area, data in details["areas"].items():
            target = datetime.fromisoformat(data["evening_assessment"]["target_at"])
            for gap in data["gaps"]:
                numbers = lambda flights: "、".join(f["flight_number"] for f in flights)
                covers = datetime.fromisoformat(gap["start"]) < target < datetime.fromisoformat(gap["end"])
                writer.writerow([mode, data["label"], gap["start"], gap["end"], gap["duration_minutes"],
                                 numbers(gap["before"]), numbers(gap["after"]), "是" if covers else "否"])
    atomic_write(output_dir / "全部航班空档.csv", "\ufeff" + stream.getvalue())
    checks = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in output_dir.iterdir() if path.is_file()}
    atomic_write(output_dir / "文件校验.json", json.dumps(checks, ensure_ascii=False, indent=2) + "\n")


def main(argv=None):
    """用户明确要求的只读真实数据测试入口，运行不会开启任何策略。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(ZONE).date())
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output_dir.exists():
            raise FlightTrialError("测试输出目录已存在，请使用新的目录避免覆盖结果")
        from flight_gap_analysis import analyze
        rows, query = fetch_raw_flights(args.date, args.env_file)
        result = analyze(rows, args.date, datetime.fromisoformat(query["query_completed_at"]))
        result["query"] = query
        save_results(result, args.output_dir)
        compact = {
            "date": result["date"], "raw_count": result["raw_count"],
            "selected_day_flights": result["selected_day_flights"],
            "warnings": result.get("warnings", []), "quality_flags": result.get("quality_flags", []),
            "device_control_sent": False,
            "modes": {mode: {area: {"label": data["label"], "gap_count": len(data["gaps"]),
                                      "evening": data["evening_assessment"]}
                              for area, data in details["areas"].items()}
                      for mode, details in result["modes"].items()},
            "output_dir": str(args.output_dir.resolve()),
        }
        print(json.dumps(compact, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception:
        print(json.dumps({"status": "failed", "message": "航班只读测试未完成，请检查配置、网络或字段结构", "device_control_sent": False}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
