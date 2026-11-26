"""从小麦历史接口同步室外测点的 5 分钟均温。"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import fmean
from time import monotonic


def outdoor_buckets(normalized, start):
    slots = defaultdict(list)
    for stamp, (value, quality) in normalized.items():
        if quality == "valid" and value is not None:
            slots[(stamp - start) // timedelta(minutes=5)].append(value)
    return [fmean(slots[key]) if key in slots else None for key in range(288)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiomforge", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.axiomforge / "src"))
    import httpx
    from axiomforge.ahu_temperature import Query, QueryConditions, Window, normalize_series
    from axiomforge.energy_share import Conditions, PROJECT_ID, SHANGHAI, resolve_query
    from axiomforge.power_temperature import TEMPERATURE
    from axiomforge.xiaomai_client import XiaomaiClient, XiaomaiSourceError, http_client, load_settings

    now = datetime.now(SHANGHAI)
    period = resolve_query(Conditions("custom", args.start, args.end), now)
    rows = []
    try:
        with http_client() as client:
            source = XiaomaiClient(client, monotonic() + 900, 900)
            source.authenticate(load_settings(), PROJECT_ID)
            source.validate_points((TEMPERATURE,), PROJECT_ID)
            for index, day in enumerate(period.days, 1):
                start = datetime.combine(day, time.min, SHANGHAI)
                end = start + timedelta(days=1)
                query = Query(QueryConditions(period="custom", start=day.isoformat(), end=day.isoformat()),
                    (Window(day, start, end),))
                records = source.history((TEMPERATURE,), PROJECT_ID,
                    start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"), 1)
                normalized, _ = normalize_series(records.get(TEMPERATURE.point_id, ()), query)
                buckets = outdoor_buckets(normalized, start)
                if any(value is not None for value in buckets):
                    trend = [round(value, 2) if value is not None else None for value in buckets]
                    rows.append((day.isoformat(), json.dumps(trend, allow_nan=False), TEMPERATURE.point_id))
                if index % 7 == 0 or index == len(period.days):
                    print(f"已读取 {index}/{len(period.days)} 天。", flush=True)
    except XiaomaiSourceError as error:
        parser.exit(1, str(error) + "\n")
    except httpx.HTTPError:
        parser.exit(1, "查询小麦室外温度时网络连接失败。\n")

    if not rows:
        parser.error("所选日期没有有效的室外温度记录。")

    path = Path(__file__).resolve().parents[1] / ".local" / "energy.sqlite"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS outdoor_temperature_days (
                day TEXT PRIMARY KEY, trend_json TEXT NOT NULL, point_id TEXT NOT NULL
            )
        """)
        db.executemany("""
            INSERT INTO outdoor_temperature_days VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET trend_json=excluded.trend_json, point_id=excluded.point_id
        """, rows)
    print(f"已同步 {len(rows)} 天室外温度，最新日期 {rows[-1][0]}。")


if __name__ == "__main__":
    main()
