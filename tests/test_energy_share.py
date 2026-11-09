"""自然日累计差的关键业务边界：固定分母、共同日期、异常与边界复用。"""

import csv
import io
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from axiomforge.energy_share import (
    GROUPS,
    POINTS,
    SHANGHAI,
    Conditions,
    decode_conditions,
    default_conditions,
    encode_conditions,
    export_daily,
    export_intervals,
    export_notes,
    export_quality,
    export_summary,
    normalize,
    preset_dates,
    resolve_query,
)

NOW = datetime(2026, 11, 9, 12, tzinfo=SHANGHAI)


def query(start="2026-10-01", end="2026-10-02"):
    return resolve_query(Conditions("custom", start, end), NOW)


def sample(stamp, value):
    return {"ts": stamp.isoformat() if isinstance(stamp, datetime) else stamp, "val": value}


def readings(q, increments=None):
    increments = increments or [Decimal("1.2"), Decimal("2.3")]
    return {
        p.point_id: [
            sample(q.start + timedelta(days=i), 100 + sum(increments[:i]))
            for i in range(len(q.days) + 1)
        ]
        for p in POINTS
    }


def test_fixed_registry_has_thirteen_disjoint_meters():
    ids = [pid for group in GROUPS for pid in group.point_ids]
    assert len(ids) == len(set(ids)) == 13
    assert set(ids) == {point.point_id for point in POINTS}
    assert [g.platform_id for g in GROUPS] == [8103, 8104, 8105, 8106, 8107, 8108, 8123, 8122]


def test_adjacent_boundaries_reused_and_complete_day_sum_closes_interval():
    q = query()
    result = normalize(q, readings(q), NOW)
    assert result.daily[0].total == Decimal("15.6")
    assert result.interval.total == Decimal("45.5") == result.complete_days.total
    assert abs(sum(item.share for item in result.daily[0].items) - Decimal(100)) < Decimal("1e-20")
    for first, second in zip(result.daily[0].items, result.daily[1].items, strict=True):
        for a, b in zip(first.meters, second.meters, strict=True):
            assert a.end is b.start
    assert result.included_days == q.days


def test_missing_group_keeps_known_energy_but_every_share_is_unknown():
    q = query()
    data = readings(q)
    data[POINTS[-1].point_id] = []
    result = normalize(q, data, NOW)
    day = result.daily[0]
    assert day.total is None
    assert day.known_count == 7
    assert day.known_subtotal == Decimal("9.6")
    assert day.items[-1].kwh is None
    assert day.items[-1].meters[0].kwh == Decimal("1.2")
    assert all(item.share is None for item in day.items)
    assert result.complete_days is None
    assert result.excluded_days == q.days


def test_interior_missing_boundary_cannot_split_a_valid_interval():
    q = query()
    data = readings(q)
    del data[POINTS[-1].point_id][1]
    result = normalize(q, data, NOW)
    assert all(d.total is None for d in result.daily)
    assert result.interval.total == Decimal("45.5")
    assert result.complete_days is None


def test_common_date_set_excludes_all_items_on_incomplete_day():
    q = query("2026-10-01", "2026-10-03")
    data = readings(q, [Decimal(1), Decimal(10), Decimal(100)])
    del data[POINTS[-1].point_id][-1]
    result = normalize(q, data, NOW)
    assert result.included_days == q.days[:2]
    assert result.complete_days.total == Decimal(143)
    assert result.complete_days.items[0].kwh == Decimal(11)
    assert result.daily[-1].items[0].kwh == 100


def test_real_zero_and_all_zero_differ_from_missing():
    q = query()
    data = readings(q, [Decimal(0), Decimal(1)])
    result = normalize(q, data, NOW)
    assert result.daily[0].total == 0
    assert all(i.kwh == 0 and i.share is None for i in result.daily[0].items)
    data[POINTS[0].point_id] = [sample(q.start + timedelta(days=i), 100) for i in range(3)]
    result = normalize(q, data, NOW)
    assert result.daily[1].items[0].kwh == 0
    assert result.daily[1].items[0].share == 0


@pytest.mark.parametrize("seconds,expected", [(300, Decimal("1.2")), (301, None)])
def test_alignment_requires_bracketing_total_gap_within_five_minutes(seconds, expected):
    q = query()
    data = readings(q)
    # 中间午夜前后取等斜率，300秒临界值允许，301秒拒绝。
    target = q.start + timedelta(days=1)
    left = 150
    right = seconds - left
    point = POINTS[0].point_id
    data[point] = [
        sample(q.start, 100),
        sample(target - timedelta(seconds=left), "101.0"),
        sample(target + timedelta(seconds=right), str(Decimal("101.0") + Decimal(seconds) / 750)),
        sample(q.end, "103.5"),
    ]
    result = normalize(q, data, NOW)
    meter = result.daily[0].items[0].meters[0]
    assert meter.kwh == expected
    assert meter.end is result.daily[1].items[0].meters[0].start
    assert meter.end.status == ("aligned" if expected is not None else "missing")
    assert meter.end.before.timestamp == target - timedelta(seconds=left)


def test_single_side_does_not_replace_midnight():
    q = query()
    data = readings(q)
    data[POINTS[0].point_id] = [sample(q.start + timedelta(seconds=30), 100), sample(q.end, 110)]
    assert normalize(q, data, NOW).interval.items[0].kwh is None


@pytest.mark.parametrize("bad", [True, -1, float("inf"), float("nan"), "garbage"])
def test_exact_bad_midnight_cannot_be_hidden_by_nearby_support(bad):
    q = query()
    data = readings(q)
    target = q.start + timedelta(days=1)
    data[POINTS[0].point_id].extend(
        [
            sample(target, bad),
            sample(target - timedelta(minutes=1), 101),
            sample(target + timedelta(minutes=1), 102),
        ]
    )
    result = normalize(q, data, NOW)
    assert result.daily[0].items[0].kwh is None
    assert result.daily[1].items[0].kwh is None
    assert result.interval.items[0].kwh is None


def test_conflict_at_chunk_seam_is_not_silently_deduped():
    q = query()
    data = readings(q)
    data[POINTS[0].point_id].append(sample(q.start + timedelta(days=1), "999"))
    result = normalize(q, data, NOW)
    assert result.daily[0].items[0].meters[0].end.status == "conflict"
    assert result.interval.items[0].kwh is None
    assert len(result.issues) == 1


def test_identical_duplicate_and_null_grid_do_not_destroy_real_reading():
    q = query()
    data = readings(q)
    data[POINTS[0].point_id].extend([sample(q.start, 100), sample(q.start, None)])
    assert normalize(q, data, NOW).interval.total == Decimal("45.5")


def test_observed_rollback_invalidates_affected_day_but_not_clean_later_day():
    q = query("2026-10-01", "2026-10-03")
    data = readings(q, [Decimal(1), Decimal(1), Decimal(1)])
    data[POINTS[0].point_id] = [
        sample(q.start, 100),
        sample(q.start + timedelta(hours=12), 50),
        sample(q.start + timedelta(days=1), 101),
        sample(q.start + timedelta(days=2), 102),
        sample(q.end, 103),
    ]
    result = normalize(q, data, NOW)
    assert result.daily[0].total is None
    assert result.daily[1].total == result.daily[2].total == 13
    assert result.interval.total is None
    assert result.complete_days.total == 26
    assert result.issues[0].kind == "rollback"


def test_aligned_support_with_rollback_is_not_trusted():
    q = query()
    data = readings(q)
    target = q.start + timedelta(days=1)
    data[POINTS[0].point_id] = [
        sample(q.start, 100),
        sample(target - timedelta(minutes=1), 110),
        sample(target + timedelta(minutes=1), 105),
        sample(q.end, 120),
    ]
    result = normalize(q, data, NOW)
    assert result.daily[0].items[0].meters[0].end.status == "invalid"
    assert result.interval.total is None


def test_preferences_are_strict_relative_rolls_and_custom_is_literal():
    original = default_conditions(NOW)
    later = NOW + timedelta(days=1)
    assert decode_conditions(encode_conditions(original), later).end_date == "2026-11-09"
    custom = Conditions("custom", "2026-10-01", "2026-10-02")
    assert decode_conditions(encode_conditions(custom), later) == custom
    assert decode_conditions({**encode_conditions(custom), "version": True}, NOW) == original
    assert decode_conditions({**encode_conditions(custom), "period": "invalid"}, NOW) == original
    assert preset_dates("last_month", datetime(2027, 1, 2, tzinfo=SHANGHAI)) == (
        "2026-12-01",
        "2026-12-31",
    )
    with pytest.raises(ValueError, match="尚无完整"):
        preset_dates("month", datetime(2026, 11, 1, tzinfo=SHANGHAI))


@pytest.mark.parametrize(
    "start,end",
    [
        ("20261001", "2026-10-02"),
        ("2026-11-09", "2026-11-09"),
        ("2026-10-02", "2026-10-01"),
        ("2026-08-01", "2026-10-02"),
    ],
)
def test_invalid_future_or_excessive_dates_rejected(start, end):
    with pytest.raises(ValueError):
        query(start, end)


def test_direct_query_cannot_bypass_frozen_calendar_contract():
    q = query()
    with pytest.raises(ValueError, match="统计窗口"):
        normalize(replace(q, end=q.end - timedelta(minutes=1)), readings(q), NOW)


def rows(content):
    assert content.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


def test_exports_keep_complete_scope_missing_blanks_zero_and_provenance():
    q = query()
    data = readings(q, [Decimal(0), Decimal(2)])
    del data[POINTS[-1].point_id][-1]
    result = normalize(q, data, NOW)
    daily = rows(export_daily(result))
    assert len(daily) == 16
    assert daily[0]["用电量_kWh"] == "0"
    assert daily[0]["占比_%"] == ""
    assert daily[-1]["用电量_kWh"] == ""
    assert all(row["占比_%"] == "" for row in daily[8:])
    assert all(row["时区"] == "Asia/Shanghai" and row["映射版本"] for row in daily)
    assert len(rows(export_summary(result))) == 2
    assert len(rows(export_intervals(result))) == 16
    quality = rows(export_quality(result))
    assert len([r for r in quality if r["依据类型"] == "日界"]) == 39
    assert len([r for r in quality if r["依据类型"] == "自然日差"]) == 26
    assert "清零" in export_notes(result).decode("utf-8-sig")


def test_extreme_future_date_is_controlled_validation_error():
    with pytest.raises(ValueError, match="完整自然日"):
        query("9999-12-31", "9999-12-31")
