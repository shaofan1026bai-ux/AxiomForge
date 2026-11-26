"""只读航班分析的边界验证，所有记录均为独立构造的虚构航班。"""

from copy import deepcopy
from datetime import date, datetime
import json
import unittest

from flight_gap_analysis import analyze
from algorithms.flight_gap_logic.flight_gaps import SHANGHAI


DAY = date(2026, 11, 5)
FETCHED = datetime(2026, 11, 5, 17, tzinfo=SHANGHAI)


def row(number, moment, *, direction="I", region="0", plan_day=DAY, **changes):
    """以本场进港或出港字段构造完整航班，不沿用线上数据。"""
    incoming = direction == "I"
    data = {
        "id": number, "flightNum": number,
        "planDate": plan_day.isoformat() + " 00:00:00",
        "arrDepFlag": direction,
        "origAirptCd": "AAA" if incoming else "LUM",
        "destAirptCd": "LUM" if incoming else "AAA",
        "fcategory": region,
        "regulatoryPlatformSysDataTime": "2026-11-05 16:00:00",
        "fltStat": "计划", "fltStatChnNm": "计划",
        "schdArrvDt" if incoming else "schdDptDt": moment,
    }
    data.update(changes)
    return data


class FlightGapAnalysisTests(unittest.TestCase):
    """候选仅用于分析，不发送或授权任何设备动作。"""

    def baseline(self, **kwargs):
        """19点至22点之间包含20点，后方仍有航班。"""
        return analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("AFTER", "2026-11-05 22:00:00"),
        ], DAY, FETCHED, **kwargs)

    def evening(self, result, mode="planned", area="all"):
        """定位两种模式和五区域下的同构晚间判断。"""
        return result["modes"][mode]["areas"][area]["evening_assessment"]

    def test_known_gap_marks_twenty_even_when_computed_before_twenty(self):
        result = self.baseline()
        for mode in ("planned", "dynamic"):
            evening = self.evening(result, mode)
            self.assertEqual(evening["status"], "inside_gap")
            self.assertEqual(evening["candidate_shutdown_at"], "2026-11-05T20:00:00+08:00")
            self.assertEqual(evening["remaining_gap_minutes"], 120)
            self.assertEqual(evening["later_today_flight_count"], 1)
            self.assertEqual(evening["next_flights"][0]["flight_number"], "AFTER")
        self.assertFalse(result["device_control_sent"])
        self.assertFalse(result["is_shutdown_permission"])

    def test_standard_json_contains_full_dates_and_endpoint_fields(self):
        result = json.loads(json.dumps(self.baseline(), allow_nan=False))
        self.assertEqual(result["date"], "2026-11-05")
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(set(result["modes"]), {"planned", "dynamic"})
        self.assertEqual(len(result["modes"]["planned"]["areas"]), 5)
        gap = result["modes"]["planned"]["areas"]["all"]["gaps"][0]
        self.assertEqual(gap["duration_minutes"], 180)
        self.assertEqual(gap["before"][0]["flight_number"], "BEFORE")
        self.assertIn("event_at", gap["after"][0])
        self.assertIn("planned_at", gap["after"][0])

    def test_later_flights_are_information_and_do_not_block_evening_cutoff(self):
        """20点是晚间关机档口，后续航班不新增推迟或重启要求。"""
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("AFTER", "2026-11-05 22:00:00"),
            row("LATER", "2026-11-05 23:00:00"),
        ], DAY, FETCHED)
        for mode in ("planned", "dynamic"):
            evening = self.evening(result, mode)
            self.assertEqual(evening["candidate_shutdown_at"], "2026-11-05T20:00:00+08:00")
            self.assertEqual(evening["later_today_flight_count"], 2)
            self.assertEqual(evening["shutdown_policy"], "evening_cutoff")
            self.assertFalse(evening["later_flights_block_shutdown"])
            self.assertEqual(evening["pump_afterrun"], "existing_site_delay")
            self.assertFalse(evening["thermal_capacity_evaluated"])
            self.assertTrue(any("不自动推迟关机或要求重启" in message
                                for message in evening["notices"]))
        self.assertFalse(result["device_control_sent"])
        self.assertFalse(result["is_shutdown_permission"])
        self.assertTrue(any("未计算余冷能力" in message for message in result["limitations"]))

    def test_event_exactly_at_target_does_not_allow_candidate(self):
        for middle in ("2026-11-05 20:00:00", "2026-11-05T12:00:00Z"):
            with self.subTest(middle=middle):
                result = analyze([
                    row("BEFORE", "2026-11-05 19:00:00"),
                    row("AT", middle), row("AFTER", "2026-11-05 22:00:00"),
                ], DAY, FETCHED)
                evening = self.evening(result)
                self.assertEqual(evening["status"], "event_at_target")
                self.assertIsNone(evening["candidate_shutdown_at"])
                self.assertEqual(evening["flights_at_target"][0]["flight_number"], "AT")

    def test_target_at_first_or_last_boundary_is_not_inside(self):
        for moments in (("20:00:00", "22:00:00"), ("19:00:00", "20:00:00")):
            with self.subTest(moments=moments):
                result = analyze([row(str(i), "2026-11-05 " + at)
                                  for i, at in enumerate(moments)], DAY, FETCHED)
                self.assertEqual(self.evening(result)["status"], "event_at_target")
                self.assertIsNone(self.evening(result)["candidate_shutdown_at"])

    def test_short_remaining_time_is_notice_not_new_candidate_threshold(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("AFTER", "2026-11-05 20:10:00"),
        ], DAY, FETCHED)
        evening = self.evening(result)
        self.assertEqual(evening["status"], "inside_gap")
        self.assertEqual(evening["candidate_shutdown_at"], "2026-11-05T20:00:00+08:00")
        self.assertEqual(evening["remaining_gap_minutes"], 10)
        self.assertTrue(evening["remaining_below_minimum"])
        self.assertTrue(evening["notices"])

    def test_total_gap_below_threshold_does_not_mark_candidate(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:50:00"),
            row("AFTER", "2026-11-05 20:10:00"),
        ], DAY, FETCHED)
        evening = self.evening(result)
        self.assertEqual(evening["status"], "no_qualifying_gap")
        self.assertIsNone(evening["candidate_shutdown_at"])
        self.assertEqual(evening["gap_duration_minutes"], 20)
        self.assertEqual(result["modes"]["planned"]["areas"]["all"]["gaps"], [])

    def test_exact_threshold_qualifies(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:45:00"),
            row("AFTER", "2026-11-05 20:15:00"),
        ], DAY, FETCHED)
        self.assertEqual(self.evening(result)["gap_duration_minutes"], 30)
        self.assertIsNotNone(self.evening(result)["candidate_shutdown_at"])

    def test_cross_day_next_flight_retains_date(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("TOMORROW", "2026-11-06 01:00:00", plan_day=date(2026, 11, 6)),
        ], DAY, FETCHED)
        evening = self.evening(result)
        self.assertEqual(evening["gap_end"], "2026-11-06T01:00:00+08:00")
        self.assertEqual(evening["remaining_gap_minutes"], 300)
        self.assertEqual(evening["later_today_flight_count"], 0)
        self.assertEqual(evening["next_flights"][0]["plan_date"], "2026-11-06")
        self.assertIsNotNone(evening["candidate_shutdown_at"])

    def test_same_time_groups_preserve_all_endpoint_flights(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("A", "2026-11-05 22:00:00"),
            row("B", "2026-11-05 22:00:00", direction="O"),
        ], DAY, FETCHED)
        self.assertEqual({f["flight_number"] for f in self.evening(result)["next_flights"]}, {"A", "B"})
        self.assertEqual(self.evening(result)["later_today_flight_count"], 2)

    def test_dynamic_mode_follows_estimated_then_actual(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00", arrvDt="2026-11-05 20:00:00"),
            row("AFTER", "2026-11-05 22:00:00", estmtArrvDt="2026-11-05 22:30:00"),
        ], DAY, FETCHED)
        self.assertEqual(self.evening(result, "planned")["status"], "inside_gap")
        self.assertEqual(self.evening(result, "dynamic")["status"], "event_at_target")
        self.assertIsNone(self.evening(result, "dynamic")["candidate_shutdown_at"])

    def test_missing_mode_time_keeps_raw_gap_but_holds_candidate(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("MISSING", None, estmtArrvDt="2026-11-05 20:00:00"),
            row("AFTER", "2026-11-05 22:00:00"),
        ], DAY, FETCHED)
        evening = self.evening(result, "planned")
        self.assertEqual(evening["status"], "inside_gap")
        self.assertTrue(evening["quality_hold"])
        self.assertIn("missing_event_time", evening["quality_flags"])
        self.assertIsNone(evening["candidate_shutdown_at"])
        self.assertEqual(result["modes"]["planned"]["areas"]["all"]["gap_count"], 1)
        self.assertEqual(self.evening(result, "dynamic")["status"], "event_at_target")

    def test_deleted_or_nonoperating_main_holds_active_shared_alias(self):
        for changes in ({"dataOperation": "DELETE"}, {"fltStat": "非营运"}):
            with self.subTest(changes=changes):
                result = analyze([
                    row("BEFORE", "2026-11-05 19:00:00"),
                    row("MAIN", "2026-11-05 20:00:00", **changes),
                    row("ALIAS", "2026-11-05 20:00:00", fltShrInd="MAIN"),
                    row("AFTER", "2026-11-05 22:00:00"),
                ], DAY, FETCHED)
                evening = self.evening(result)
                self.assertEqual(evening["status"], "inside_gap")
                self.assertIn("active_shared_flight_with_inactive_main", evening["quality_flags"])
                self.assertIsNone(evening["candidate_shutdown_at"])
                self.assertEqual(result["modes"]["planned"]["areas"]["all"]["gap_count"], 1)

    def test_old_deleted_main_does_not_override_latest_active_version(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("MAIN", "2026-11-05 19:00:00", id="old", dataOperation="DELETE",
                regulatoryPlatformSysDataTime="2026-11-05 15:00:00"),
            row("MAIN", "2026-11-05 19:00:00", id="new"),
            row("ALIAS", "2026-11-05 19:00:00", fltShrInd="MAIN"),
            row("AFTER", "2026-11-05 22:00:00"),
        ], DAY, FETCHED)
        self.assertFalse(self.evening(result)["quality_hold"])
        self.assertIsNotNone(self.evening(result)["candidate_shutdown_at"])

    def test_regions_and_directions_are_independent(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("AFTER", "2026-11-05 22:00:00"),
            row("OUT", "2026-11-05 20:00:00", direction="O"),
            row("INT", "2026-11-05 20:00:00", region="1"),
        ], DAY, FETCHED)
        self.assertEqual(self.evening(result)["status"], "event_at_target")
        self.assertEqual(self.evening(result, area="domestic:I")["status"], "inside_gap")
        self.assertIsNotNone(self.evening(result, area="domestic:I")["candidate_shutdown_at"])
        self.assertEqual(self.evening(result, area="international:I")["status"], "event_at_target")

    def test_unknown_region_blocks_both_same_direction_areas(self):
        result = analyze([
            row("BEFORE", "2026-11-05 19:00:00"),
            row("UNKNOWN", "2026-11-05 20:00:00", region="unknown"),
            row("AFTER", "2026-11-05 22:00:00"),
        ], DAY, FETCHED)
        for area in ("all", "domestic:I", "international:I"):
            self.assertEqual(self.evening(result, area=area)["status"], "event_at_target")
            self.assertIsNone(self.evening(result, area=area)["candidate_shutdown_at"])

    def test_empty_input_and_single_flight_do_not_extrapolate_whole_day(self):
        for rows in ([], [row("ONE", "2026-11-05 19:00:00")]):
            with self.subTest(rows=len(rows)):
                result = analyze(rows, DAY, FETCHED)
                for mode in result["modes"].values():
                    for area in mode["areas"].values():
                        self.assertEqual(area["gap_count"], 0)
                        self.assertIsNone(area["evening_assessment"]["candidate_shutdown_at"])
                        self.assertEqual(area["gaps"], [])
        self.assertEqual(self.evening(analyze([], DAY, FETCHED))["status"], "insufficient_data")

    def test_first_and_last_data_boundary_is_not_fabricated_gap(self):
        for moments in (("17:00:00", "19:00:00"), ("21:00:00", "23:00:00")):
            result = analyze([row(str(i), "2026-11-05 " + at)
                              for i, at in enumerate(moments)], DAY, FETCHED)
            evening = self.evening(result)
            self.assertEqual(evening["status"], "no_qualifying_gap")
            self.assertIsNone(evening["candidate_shutdown_at"])
            self.assertIsNone(evening["gap_start"])

    def test_input_validation_and_original_rows_unchanged(self):
        rows = [row("BEFORE", "2026-11-05 19:00:00"), row("AFTER", "2026-11-05 22:00:00")]
        before = deepcopy(rows)
        analyze(rows, DAY.isoformat(), FETCHED)
        self.assertEqual(rows, before)
        for minimum in (0, 1441, float("nan"), True, "30"):
            with self.subTest(minimum=minimum):
                with self.assertRaises(ValueError):
                    analyze(rows, DAY, FETCHED, minimum_minutes=minimum)
        for target in (-1, 24, True, 20.0):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    analyze(rows, DAY, FETCHED, target_hour=target)
        for invalid in ({}, "[]", [None]):
            with self.assertRaises(ValueError):
                analyze(invalid, DAY, FETCHED)
        with self.assertRaises(ValueError):
            analyze(rows, DAY, FETCHED.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
