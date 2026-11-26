"""按 AxiomForge 的七区映射导入航信室内温度历史。"""

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import fmean

from import_energy import read_series


def configured_regions():
    from axiomforge.ahu_temperature import PointSpec
    from axiomforge.region_climate_source import REGIONS, Probe

    # 现场已确认迎客／出港厅的待确认点位为「值机大厅3」。
    check_in = Probe(
        12699,
        "值机大厅3",
        PointSpec("bfby5y_120171_temp", 12699, "℃", "temperature"),
        PointSpec("bfby5y_120171_rh", 12699, "%", "humidity"),
    )
    return tuple(
        replace(region, probes=tuple(check_in if probe.id == -1 else probe for probe in region.probes))
        if region.id == "departure" else region
        for region in REGIONS
    )


def summarize(trend):
    values = [point.temperature for point in trend if point.temperature is not None]
    return {
        "average": round(fmean(values), 1) if values else None,
        "count": len(values),
        "trend": [round(point.temperature, 2) if point.temperature is not None else None for point in trend],
    }


def summarize_regions(snapshot, regions):
    from axiomforge.region_climate import analyze_region

    result = []
    for region in regions:
        summary = summarize(analyze_region(snapshot, region).trend)
        probes = []
        for probe in region.probes:
            pending = probe.temperature is None
            data = {} if pending else summarize(analyze_region(snapshot, region, probe_id=probe.id).trend)
            probes.append({"id": probe.id, "name": probe.name, "pending": pending, **data})
        result.append({"id": region.id, "name": region.name, "floor": region.floor, **summary, "probes": probes})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiomforge", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--start", default="2026-10-09")
    parser.add_argument("--end", default="2026-10-17")
    args = parser.parse_args()
    sys.path.insert(0, str(args.axiomforge / "src"))
    from axiomforge.energy_share import Conditions, SHANGHAI, resolve_query
    from axiomforge.region_climate_source import ClimateSnapshot

    regions = configured_regions()
    now = datetime.now(SHANGHAI)
    query = resolve_query(Conditions("custom", args.start, args.end), now)
    folder = args.archive / "history" / "group_636_type_54"
    if not folder.is_dir():
        parser.error("导出目录中没有温湿度探头历史。")
    readings = {}
    for region in regions:
        for probe in region.probes:
            if probe.temperature is None:
                continue
            paths = sorted(folder.glob(f"{probe.id}_*.csv"))
            if not paths:
                continue
            path, = paths
            samples = {}
            for stamp, raw in read_series(path, probe.temperature.point_id, query, timedelta()):
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    continue
                # 同一时刻出现多个有效值时留空，避免任取其一。
                if math.isfinite(value):
                    samples.setdefault(stamp, set()).add(value)
            for stamp, values in samples.items():
                if len(values) == 1:
                    value, = values
                    readings.setdefault(stamp.date(), {}).setdefault(probe.id, {})[stamp] = value

    rows = []
    for day in query.days:
        start = datetime.combine(day, time.min, SHANGHAI)
        snapshot = ClimateSnapshot(day, start, start + timedelta(days=1), now, readings.get(day, {}), {}, {})
        summaries = summarize_regions(snapshot, regions)
        rows.append((day.isoformat(), json.dumps(summaries, ensure_ascii=False, allow_nan=False), args.archive.resolve().name))

    if not readings:
        parser.error("所选日期没有有效的探头温度记录。")
    path = Path(__file__).resolve().parents[1] / ".local" / "energy.sqlite"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS region_temperature_days (
                day TEXT PRIMARY KEY, regions_json TEXT NOT NULL, source TEXT NOT NULL
            )
        """)
        db.executemany("""
            INSERT INTO region_temperature_days VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET regions_json=excluded.regions_json, source=excluded.source
        """, rows)
    print(f"已导入 {len(rows)} 天、{len(regions)} 个区域，{sum(len(probes) for probes in readings.values())} 条探头日序列。")


if __name__ == "__main__":
    main()
