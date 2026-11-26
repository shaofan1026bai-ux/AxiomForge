"""从航信历史 CSV 导入两段等长的同期电表与室外温度记录。"""

import argparse
import csv
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean


def read_series(path, point_id, query, margin):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = [name for name in reader.fieldnames or () if name.endswith(f"[{point_id}]")]
        if len(columns) != 1:
            raise ValueError(f"{path.name} 中找不到唯一测点 {point_id}")
        for row in reader:
            stamp = datetime.fromisoformat(row["ts"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=query.start.tzinfo)
            else:
                stamp = stamp.astimezone(query.start.tzinfo)
            if query.start - margin <= stamp < query.end + margin:
                yield stamp, row[columns[0]].strip() or None


def temperature_rows(path, query, zero_margin):
    hourly = defaultdict(dict)
    for stamp, value in read_series(path, "nfdh9q_mscrk01_c21", query, zero_margin):
        if value is not None and math.isfinite(float(value)):
            hourly[stamp.date(), stamp.hour][stamp] = float(value)
    for day in query.days:
        hours = [hourly[day, hour] for hour in range(24)]
        # 5 分钟采样，每小时至少需要一半有效记录，再按小时等权求日均。
        mean = round(fmean(fmean(hour.values()) for hour in hours), 1) if all(len(hour) >= 6 for hour in hours) else None
        yield (day.isoformat(), mean)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiomforge", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path, help="包含 history/ 的航信全设备导出目录")
    parser.add_argument("--start", default="2026-10-09")
    parser.add_argument("--end", default="2026-10-17")
    parser.add_argument("--history-only", action="store_true", help="只导入所选日期，保留看板默认区间")
    args = parser.parse_args()
    sys.path.insert(0, str(args.axiomforge / "src"))
    from axiomforge.energy_share import Conditions, GROUPS, POINTS, SHANGHAI, normalize, resolve_query

    now = datetime.now(SHANGHAI)
    current = resolve_query(Conditions("custom", args.start, args.end), now)
    start, end = current.days[0], current.days[-1]
    windows = [current]
    if not args.history_only:
        if start.month != end.month:
            parser.error("本期须在同一个自然月内。")
        previous_month = start.replace(day=1) - timedelta(days=1)
        if end.day > previous_month.day:
            parser.error("上月没有完全对应的日期，请缩短本期区间。")
        comparison = resolve_query(Conditions("custom",
            previous_month.replace(day=start.day).isoformat(), previous_month.replace(day=end.day).isoformat()), now)
        windows.append(comparison)

    archive = args.archive / "history"
    paths = {}
    meters = []
    for group in GROUPS:
        group_key, group_name = ("midea", "美的空调电表") if group.key.startswith("midea") else (group.key, group.name)
        for meter in POINTS:
            if meter.point_id in group.point_ids:
                path, = (archive / "group_635_type_8").glob(f"{meter.device_id}_*.csv")
                paths[meter.point_id] = path
                meters.append((meter.device_id, meter.name, group_key, group_name))
    weather_path, = (archive / "group_642_type_14").glob("12788_*.csv")

    energy = []
    weather = []
    for window in windows:
        margin = timedelta(minutes=6)
        series = {
            point: [{"ts": stamp.isoformat(), "val": value} for stamp, value in read_series(path, point, window, margin)]
            for point, path in paths.items()
        }
        snapshot = normalize(window, series, now)
        for day in snapshot.daily:
            for item in day.items:
                for meter in item.meters:
                    energy.append((day.day.isoformat(), meter.meter.device_id,
                        float(meter.kwh) if meter.kwh is not None else None, int(meter.estimated), meter.reason))
        weather.extend(temperature_rows(weather_path, window, timedelta()))
        print(f"{window.days[0]} 至 {window.days[-1]}：用电合计可计算 {len(snapshot.included_days)}/{len(window.days)} 天")

    if not any(row[2] is not None for row in energy):
        parser.error("所选日期没有可导入的电量。")

    path = Path(__file__).resolve().parents[1] / ".local" / "energy.sqlite"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS meters (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, group_key TEXT NOT NULL, group_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_energy (
                day TEXT NOT NULL, meter_id INTEGER NOT NULL REFERENCES meters(id),
                kwh REAL CHECK (kwh >= 0), estimated INTEGER NOT NULL CHECK (estimated IN (0, 1)),
                reason TEXT NOT NULL, PRIMARY KEY (day, meter_id)
            );
            CREATE TABLE IF NOT EXISTS daily_weather (day TEXT PRIMARY KEY, temperature REAL);
            CREATE TABLE IF NOT EXISTS dashboard_period (
                id INTEGER PRIMARY KEY CHECK (id = 1), start_day TEXT NOT NULL, end_day TEXT NOT NULL,
                comparison_start TEXT NOT NULL, comparison_end TEXT NOT NULL, source TEXT NOT NULL
            );
        """)
        db.execute("PRAGMA foreign_keys = ON")
        db.executemany("""
            INSERT INTO meters VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, group_key=excluded.group_key, group_name=excluded.group_name
        """, meters)
        db.executemany("""
            INSERT INTO daily_energy VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day, meter_id) DO UPDATE SET kwh=excluded.kwh, estimated=excluded.estimated, reason=excluded.reason
        """, energy)
        db.executemany("""
            INSERT INTO daily_weather VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET temperature=excluded.temperature
        """, weather)
        if not args.history_only:
            db.execute("INSERT OR REPLACE INTO dashboard_period VALUES (1, ?, ?, ?, ?, ?)",
                (start.isoformat(), end.isoformat(), comparison.days[0].isoformat(), comparison.days[-1].isoformat(),
                 args.archive.resolve().name))
    print(f"已导入 {sum(row[2] is not None for row in energy)}/{len(energy)} 条电表日用电量，"
          f"{sum(row[1] is not None for row in weather)}/{len(weather)} 条日均温度。")


if __name__ == "__main__":
    main()
