"""航班空档边界、半点候选排序和温度门槛的离线业务测试。"""
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

from test_nowapi_weather import AT,payload
from nowapi_weather import parse_forecast,settings
from flight_weather_shutdown_policy import select_shutdown
from hangxin_flight_shutdown_platform import ShutdownError


class Cases(unittest.TestCase):
    def setUp(self):
        self.weather=parse_forecast(payload(),settings(),AT)
        self.area={'quality_flags':[],'gaps':[{'start':(AT+timedelta(minutes=5)).isoformat(),
                    'end':(AT+timedelta(hours=3)).isoformat(),'duration_minutes':175}]}
        self.result={'date':AT.date().isoformat(),'airport':'LUM','timezone':'Asia/Shanghai','minimum_minutes':30,
            'selected_day_flights':2,'raw_count':2,'warnings':[],'normalization_counts':{},
            'modes':{'dynamic':{'areas':{'all':self.area}}}}
    def select(self,at=AT):return select_shutdown(self.result,at.date(),self.weather,at,at.replace(hour=22))
    def test_earliest_cool_candidate_is_eighteen_thirty(self):
        goal,r=self.select();self.assertEqual(goal.strftime('%H:%M'),'18:30');self.assertEqual(r['status'],'eligible')
    def test_hot_first_candidate_falls_through_to_later_cool_candidate(self):
        self.weather['hourly'][0]['temperature_c']=30
        goal,r=self.select();self.assertEqual(goal.strftime('%H:%M'),'19:00')
    def test_flight_exactly_at_candidate_is_not_an_idle_instant(self):
        self.area['gaps'][0].update(start=AT.replace(minute=30).isoformat(),duration_minutes=150)
        self.assertEqual(self.select()[0].strftime('%H:%M'),'19:00')
    def test_exact_thirty_minute_gap_does_not_meet_exceeds_thirty(self):
        self.area['gaps'][0].update(start=AT.replace(minute=15).isoformat(),end=AT.replace(minute=45).isoformat(),duration_minutes=30)
        self.assertIsNone(self.select()[0])
    def test_empty_bad_quality_and_no_future_coverage_do_not_create_permission(self):
        self.area['quality_flags']=['missing_event_time']
        with self.assertRaises(ShutdownError):self.select()
        self.area['quality_flags']=[];self.area['gaps']=[];self.assertIsNone(self.select()[0])
    def test_hot_or_missing_forecast_never_falls_back_to_daily_low(self):
        self.weather['hourly']=[];self.weather['daily']=[{'night_temperature_c':10}]
        self.assertIsNone(self.select()[0])
    def test_earlier_candidates_are_not_replayed_at_nineteen(self):
        self.weather=parse_forecast(payload(),settings(),AT.replace(hour=19))
        self.assertEqual(self.select(AT.replace(hour=19))[0].strftime('%H:%M'),'19:30')
    def test_existing_earlier_schedule_is_not_delayed(self):
        self.assertIsNone(select_shutdown(self.result,AT.date(),self.weather,AT,AT.replace(minute=20))[0])


if __name__=='__main__':unittest.main()
