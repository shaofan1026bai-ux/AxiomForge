"""按北京时间从小麦同步一个完整自然日的能耗和温度。"""

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta
from fcntl import LOCK_EX, LOCK_NB, flock
from itertools import batched
from pathlib import Path
from statistics import fmean
from time import monotonic, sleep

import httpx
from axiomforge.ahu_temperature import Query, QueryConditions, Window, normalize_series
from axiomforge.energy_share import POINTS, PROJECT_ID, SHANGHAI, Conditions, normalize, resolve_query
from axiomforge.energy_share_source import query_chunks
from axiomforge.power_temperature import TEMPERATURE
from axiomforge.region_climate_source import ClimateSnapshot
from axiomforge.xiaomai_client import XiaomaiClient, XiaomaiSourceError, http_client, load_settings

from import_region_temperature import configured_regions, summarize_regions
from sync_outdoor_temperature import outdoor_buckets

ONE_DAY = timedelta(days=1)
SYNC_AT = time(3)
SOURCE_NAME = "小麦历史接口"


def fetch_day(day, now):
    day_key = day.isoformat()
    energy_query = resolve_query(Conditions("custom", day_key, day_key), now)
    start, end = energy_query.start, energy_query.end
    temperature_query = Query(QueryConditions(period="custom", start=day_key, end=day_key), (Window(day, start, end),))
    regions = configured_regions()
    probes = {probe.id: probe.temperature for region in regions for probe in region.probes if probe.temperature}
    temperature_points = (*probes.values(), TEMPERATURE)
    meter_series = defaultdict(list)
    temperature_series = {}
    with http_client() as client:
        source = XiaomaiClient(client, monotonic() + 180, 180)
        source.authenticate(load_settings(), PROJECT_ID)
        source.validate_points(POINTS, PROJECT_ID)
        for batch in batched(temperature_points, 8):
            source.validate_points(batch, PROJECT_ID)
        for first, last in query_chunks(energy_query, now):
            for point, values in history(source, POINTS, first, last).items():
                meter_series[point].extend(values)
        for batch in batched(temperature_points, 8):
            records = history(source, batch, start, end)
            for point in batch:
                series, _ = normalize_series(records.get(point.point_id, ()), temperature_query)
                temperature_series[point.point_id] = series

    energy_day, = normalize(energy_query, meter_series, now).daily
    energy = [
        (day_key, meter.meter.device_id, float(meter.kwh) if meter.kwh is not None else None,
         int(meter.estimated), meter.reason)
        for group in energy_day.items for meter in group.meters
    ]
    readings = {
        probe_id: {stamp: value for stamp, (value, quality) in temperature_series[point.point_id].items()
                   if value is not None and quality == "valid"}
        for probe_id, point in probes.items()
    }
    snapshot = ClimateSnapshot(day, start, end, now, readings, {}, {})
    summaries = summarize_regions(snapshot, regions)
    buckets = outdoor_buckets(temperature_series[TEMPERATURE.point_id], start)
    hourly = [[value for value in buckets[first:first + 12] if value is not None] for first in range(0, 288, 12)]
    average = round(fmean(fmean(hour) for hour in hourly), 1) if all(len(hour) >= 6 for hour in hourly) else None
    outdoor = [round(value, 2) if value is not None else None for value in buckets]
    return energy, summaries, outdoor, average


def history(source, points, start, end):
    return source.history(points, PROJECT_ID, start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"), 1)


def open_database(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=15)


def save_day(path, day, now, energy, regions, outdoor, average):
    key = day.isoformat()
    with closing(open_database(path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE")
        # 一次发布四张业务表与同步日期，连接失败时保留原有记录。
        db.execute("CREATE TABLE IF NOT EXISTS daily_sync_runs (day TEXT PRIMARY KEY, synced_at TEXT NOT NULL)")
        db.executemany("""
            INSERT INTO daily_energy (day, meter_id, kwh, estimated, reason) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day, meter_id) DO UPDATE SET kwh=excluded.kwh, estimated=excluded.estimated, reason=excluded.reason
        """, energy)
        db.execute("""
            INSERT INTO region_temperature_days (day, regions_json, source) VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET regions_json=excluded.regions_json, source=excluded.source
        """, (key, json.dumps(regions, ensure_ascii=False, allow_nan=False), SOURCE_NAME))
        db.execute("""
            INSERT INTO outdoor_temperature_days (day, trend_json, point_id) VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET trend_json=excluded.trend_json, point_id=excluded.point_id
        """, (key, json.dumps(outdoor, allow_nan=False), TEMPERATURE.point_id))
        db.execute("""
            INSERT INTO daily_weather (day, temperature) VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET temperature=excluded.temperature
        """, (key, average))
        db.execute("""
            INSERT INTO daily_sync_runs (day, synced_at) VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET synced_at=excluded.synced_at
        """, (key, now.isoformat(timespec="seconds")))


def due_day(path, now):
    cutoff = datetime.combine(now.date(), SYNC_AT, SHANGHAI)
    latest = (now.date() if now >= cutoff else now.date() - ONE_DAY) - ONE_DAY
    with closing(open_database(path)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_schema WHERE name='daily_sync_runs' AND type='table'").fetchone()
        last = db.execute("SELECT MAX(day) FROM daily_sync_runs").fetchone()[0] if exists else None
    candidate = date.fromisoformat(last) + ONE_DAY if last else latest
    next_time = cutoff + ONE_DAY if now >= cutoff else cutoff
    return (candidate if candidate <= latest else None), (next_time - now).total_seconds()


def sync_once(path, day):
    now = datetime.now(SHANGHAI)
    with path.with_name(".daily-sync.lock").open("a") as lock:
        flock(lock, LOCK_EX | LOCK_NB)
        energy, regions, outdoor, average = fetch_day(day, now)
        save_day(path, day, now, energy, regions, outdoor, average)
    print(f"{day} 同步完成：电表 {sum(row[2] is not None for row in energy)}/{len(energy)}，"
          f"区域 {sum(region['count'] > 0 for region in regions)}/{len(regions)}，"
          f"室外时段 {sum(value is not None for value in outdoor)}/288，"
          f"室外日均 {'有值' if average is not None else '缺测'}。", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("/data/energy.sqlite"))
    parser.add_argument("--date", type=date.fromisoformat, help="立即同步指定历史日期")
    args = parser.parse_args()
    os.umask(0o007)
    while True:
        try:
            if args.date:
                sync_once(args.database, args.date)
                return
            day, delay = due_day(args.database, datetime.now(SHANGHAI))
            if day:
                sync_once(args.database, day)
            else:
                sleep(delay)
        except (XiaomaiSourceError, httpx.HTTPError, ValueError, sqlite3.Error, OSError) as error:
            message = str(error) if isinstance(error, XiaomaiSourceError) else type(error).__name__
            print(f"同步失败（{message}），本次结果未写入。", flush=True)
            if args.date:
                raise SystemExit(1) from None
            sleep(900)


if __name__ == "__main__":
    main()
