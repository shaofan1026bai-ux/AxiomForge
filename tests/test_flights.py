"""用独立构造的航班验证空档口径，不依赖真实库或运营数据。"""

from dataclasses import replace
from datetime import date, datetime
from typing import Any

import pytest

from axiomforge.flights import (
    SHANGHAI,
    Flight,
    flight_gaps,
    format_moment,
    local_time,
    normalize_flights,
)

DAY = date(2026, 11, 4)
NOW = datetime(2026, 11, 4, 18, tzinfo=SHANGHAI)


def row(number: str = "TEST1", **changes: Any) -> dict[str, Any]:
    value = {
        "id": number,
        "flightNum": number,
        "planDate": "2026-11-04 00:00:00",
        "arrDepFlag": "I",
        "destAirptCd": "LUM",
        "origAirptCd": "AAA",
        "schdArrvDt": "2026-11-04 12:00:00",
        "depCtyChnNm": "示例城市",
        "arrvCtyChnNm": "芒市",
        "fltStat": "计划",
        "fltStatChnNm": "计划",
        "regulatoryPlatformSysDataTime": "2026-11-04 08:00:00",
    }
    value.update(changes)
    return value


def flight(number: str, hour: int, minute: int = 0, **changes: Any) -> Flight:
    value = normalize_flights([row(number, schdArrvDt=f"2026-11-04 {hour:02}:{minute:02}:00")], NOW)
    return replace(value.flights[0], **changes)


def test_latest_deletion_and_shared_scope() -> None:
    records = [
        row(),
        row(id="new", dataOperation="DELETE", regulatoryPlatformSysDataTime="2026-11-04 09:00:00"),
        row("MAIN"),
        row("ALIAS", fltShrInd="MAIN"),
        row("OFF", fltStat="非营运", fltStatChnNm="非营运"),
        row("OTHER", destAirptCd="BBB"),
    ]
    result = normalize_flights(records, NOW)
    assert [f.number for f in result.flights] == ["MAIN"]
    assert result.flights[0].aliases == ("ALIAS",)
    assert result.counts["old_versions"] == 1
    assert result.counts["deleted"] == 1
    assert result.counts["non_operating"] == 1
    assert result.counts["scope_rejected"] == 1


def test_unmatched_shared_alias_is_retained_with_warning() -> None:
    result = normalize_flights([row("ALIAS", fltShrInd="MISSING")], NOW)
    assert len(result.flights) == 1
    assert result.warnings


def test_inactive_alias_does_not_warn_about_missing_main() -> None:
    result = normalize_flights(
        [row("OFF_ALIAS", fltShrInd="MISSING", fltStat="非营运", fltStatChnNm="非营运")], NOW
    )
    assert result.flights == ()
    assert result.warnings == ()


def test_departure_destination_arrival_is_not_local_arrival() -> None:
    result = normalize_flights(
        [
            row(
                arrDepFlag="O",
                origAirptCd="LUM",
                destAirptCd="AAA",
                schdDptDt="2026-11-04 09:00:00",
                dptDt="2026-11-04 09:02:00",
                arrvDt="2026-11-04 12:01:00",
                fltStat="起飞",
                fltStatChnNm="到达",
                gate="04",
            )
        ],
        NOW,
    )
    value = result.flights[0]
    assert value.status == "已从芒市起飞"
    assert value.actual and value.actual.hour == 9
    assert value.gate == "04"
    assert result.counts["status_conflicts"] == 1
    assert normalize_flights([row(gate="外站")], NOW).flights[0].gate == ""


def test_local_time_and_missing_values() -> None:
    assert local_time("2026-11-04T01:00:00Z") == local_time("2026-11-04 09:00:00")
    assert local_time("invalid") is None
    assert format_moment(None, DAY) == "—"
    assert format_moment(local_time("2026-11-05 01:35:00"), DAY) == "次日 01:35"


def test_gaps_merge_directions_and_same_time_groups() -> None:
    flights = [
        flight("A", 10),
        flight("B", 10, 20, direction="O"),
        flight("C", 11),
        flight("D", 11, direction="O"),
    ]
    gaps = flight_gaps(flights, DAY, "planned", 30)
    assert len(gaps) == 1
    assert gaps[0].minutes == 40
    assert [f.number for f in gaps[0].before] == ["B"]
    assert [f.number for f in gaps[0].after] == ["C", "D"]
    # 仅看进港会制造60分钟空档；服务必须接收完整集合。
    assert (
        flight_gaps([f for f in flights if f.direction == "I"], DAY, "planned", 30)[0].minutes == 60
    )


def test_neighbor_plan_day_flight_splits_cross_midnight_gap() -> None:
    a = flight("A", 23)
    b = replace(a, key="B", number="B", planned=local_time("2026-11-05 01:35:00"))
    neighbor = replace(
        a, key="N", number="N", day=date(2026, 11, 5), planned=local_time("2026-11-05 00:20:00")
    )
    gaps = flight_gaps([a, b, neighbor], DAY, "planned", 1)
    assert sorted(g.minutes for g in gaps) == [75, 80]


def test_dynamic_time_priority_and_no_extrapolation() -> None:
    a = flight(
        "A",
        10,
        estimated=local_time("2026-11-04 10:20:00"),
        actual=local_time("2026-11-04 10:05:00"),
    )
    b = flight("B", 11, estimated=local_time("2026-11-04 10:30:00"))
    assert flight_gaps([a, b], DAY, "planned", 1)[0].minutes == 60
    assert flight_gaps([a, b], DAY, "dynamic", 1)[0].minutes == 25
    assert flight_gaps([a], DAY, "planned", 1) == []


@pytest.mark.parametrize("value", [None, "invalid"])
def test_missing_schedule_warns(value: Any) -> None:
    result = normalize_flights([row(schdArrvDt=value)], NOW)
    assert len(result.flights) == 1
    assert result.warnings


def test_region_mapping_preserves_unknown_values() -> None:
    records = [
        row("DOM", fcategory="0"),
        row("INT", fcategory=1),
        row("UNKNOWN", fcategory="2"),
        row("MISSING"),
    ]
    result = normalize_flights(records, NOW)
    assert {f.number: f.region for f in result.flights} == {
        "DOM": "domestic",
        "INT": "international",
        "UNKNOWN": "unknown",
        "MISSING": "unknown",
    }


def test_area_gaps_do_not_mix_domestic_international_or_directions() -> None:
    from axiomforge.flights import flights_for_area

    flights = [
        flight("A", 10, region="domestic"),
        flight("B", 12, region="domestic"),
        flight("DEP", 11, region="domestic", direction="O"),
        flight("INT", 10, 30, region="international"),
    ]
    gaps = flight_gaps(flights, DAY, "planned", 1, area="domestic:I")
    assert len(gaps) == 1 and gaps[0].minutes == 120
    assert all(g.minutes < 120 for g in flight_gaps(flights, DAY, "planned", 1))
    assert [f.number for f in flights_for_area(flights, "international:I")] == ["INT"]
    assert flight_gaps(flights, DAY, "planned", 1, area="international:O") == []


def test_unknown_region_conservatively_blocks_same_direction_area_gap() -> None:
    from axiomforge.flights import flights_for_area

    flights = [
        flight("A", 10, region="domestic"),
        flight("B", 12, region="domestic"),
        flight("UNKNOWN", 11, region="unknown"),
        flight("UNKNOWN_OUT", 10, 30, region="unknown", direction="O"),
    ]
    assert [f.number for f in flights_for_area(flights, "domestic:I")] == ["A", "B"]
    gaps = flight_gaps(flights, DAY, "planned", 1, area="domestic:I")
    assert len(gaps) == 2 and all(g.minutes == 60 for g in gaps)
    assert any(f.region == "unknown" for g in gaps for f in g.before + g.after)
