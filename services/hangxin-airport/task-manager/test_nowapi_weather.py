"""NowAPI文档适配、温度区间与缓存的离线验证，禁止真实网络。"""
from copy import deepcopy
from datetime import datetime,timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock

from fastapi.testclient import TestClient
import requests
import nowapi_weather as w

AT=datetime(2026,11,8,18,tzinfo=w.ZONE)


def payload():
    return {'success':'1','result':{'cityid':'101291508','area_1':'云南','area_2':'德宏','area_3':'芒市',
        'futureHour':[{'dateYmdh':(AT+timedelta(hours=i)).strftime('%Y-%m-%d %H:%M:%S'),'wtTemp':'25','wtNm':'晴'} for i in range(24)],
        'futureDay':[{'dateYmd':'2026-11-08','wtTemp1':'28','wtTemp2':'17'}]}}


class Cases(unittest.TestCase):
    def setUp(self):
        self.cfg=w.settings();self.data=payload()
        blocker=patch('requests.sessions.Session.request',side_effect=AssertionError('禁止真实联网'));blocker.start();self.addCleanup(blocker.stop)
    def parse(self):return w.parse_forecast(self.data,self.cfg,AT)
    def test_hourly_and_daily_are_separate_and_no_issued_time_is_invented(self):
        r=self.parse();self.assertEqual(len(r['hourly']),24);self.assertEqual(r['hourly'][0]['temperature_c'],25)
        self.assertEqual(r['daily'][0]['night_temperature_c'],17);self.assertIsNone(r['forecast_issued_at'])
    def test_daily_only_endpoint_cannot_be_used_for_hourly_control(self):
        self.data={'success':'1','result':[{'days':'2026-11-08','temp_low':'18','temp_high':'28'}]}
        with self.assertRaises(w.WeatherError):self.parse()
    def test_wrong_city_or_id_is_rejected(self):
        for mutation in ({'area_2':'北京','area_3':''},{'cityid':'bad'}):
            with self.subTest(mutation=mutation):
                self.data=payload();self.data['result'].update(mutation)
                with self.assertRaises(w.WeatherError):self.parse()
    def test_bad_temperature_duplicate_and_stale_forecast_are_rejected(self):
        for value in (None,True,'NaN','inf','-','99'):
            self.data=payload();self.data['result']['futureHour'][0]['wtTemp']=value
            with self.subTest(value=value),self.assertRaises(w.WeatherError):self.parse()
        self.data=payload();self.data['result']['futureHour'].append(deepcopy(self.data['result']['futureHour'][0]))
        with self.assertRaises(w.WeatherError):self.parse()
        self.data=payload()
        with self.assertRaises(w.WeatherError):w.parse_forecast(self.data,self.cfg,AT+timedelta(days=2))
    def test_quoted_upstream_error_never_leaks_private_message(self):
        self.data={'success':'0','msg':'PRIVATE-CREDENTIALS'}
        with self.assertRaises(w.WeatherError) as e:self.parse()
        self.assertNotIn('PRIVATE',str(e.exception))
    def test_half_hour_is_interpolated_and_twenty_seven_is_excluded(self):
        r=self.parse();r['hourly'][0]['temperature_c']=28;r['hourly'][1]['temperature_c']=26
        a=AT+timedelta(minutes=30);b=AT+timedelta(hours=1)
        self.assertFalse(w.interval_temperature(r,a,b,AT,27)['allowed'])
        r['hourly'][0]['temperature_c']=27.8
        result=w.interval_temperature(r,a,b,AT,27)
        self.assertTrue(result['allowed']);self.assertEqual(result['candidate_temperature_c'],26.9)
    def test_later_rebound_in_same_gap_blocks_cool_candidate(self):
        r=self.parse();r['hourly'][2]['temperature_c']=28
        self.assertFalse(w.interval_temperature(r,AT+timedelta(minutes=30),AT+timedelta(hours=3),AT)['allowed'])
    def test_missing_internal_hour_and_uncovered_endpoint_are_not_extrapolated(self):
        r=self.parse();r['hourly'].pop(1)
        with self.assertRaises(w.WeatherError):w.interval_temperature(r,AT+timedelta(minutes=30),AT+timedelta(hours=2),AT)
        r=self.parse()
        with self.assertRaises(w.WeatherError):w.interval_temperature(r,AT+timedelta(hours=22),AT+timedelta(hours=25),AT)
    def test_stale_future_wrong_units_or_duplicate_snapshot_are_blocked(self):
        for mutation in ({'fetched_at':(AT-timedelta(minutes=31)).isoformat()},
                         {'fetched_at':(AT+timedelta(seconds=1)).isoformat()},{'unit':'F'}):
            r={**self.parse(),**mutation}
            with self.subTest(mutation=mutation),self.assertRaises(w.WeatherError):
                w.interval_temperature(r,AT+timedelta(minutes=30),AT+timedelta(hours=1),AT)
    def test_credentials_are_required_without_network_fallback(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(w.WeatherError,'尚未配置'):w.fetch_forecast(self.cfg)
    def test_post_uses_fixed_https_no_redirect_no_credentials_in_result(self):
        response=requests.Response();response.status_code=200;response._content=json.dumps(self.data).encode();response._content_consumed=True
        with patch.dict(os.environ,{'NOWAPI_APPKEY':'LOCAL-KEY','NOWAPI_SIGN':'LOCAL-SIGN'}),patch.object(requests.Session,'post',return_value=response) as post:
            r=w.fetch_forecast(self.cfg,lambda:AT)
        self.assertEqual(post.call_args.args[0],w.ENDPOINT);self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertEqual(post.call_args.kwargs['data']['ag'],'futureDay,futureHour')
        self.assertNotIn('LOCAL-',json.dumps(r))
    def test_cache_keeps_original_time_and_failure_does_not_return_stale_success(self):
        clock=[AT];source=Mock(side_effect=lambda cfg,now:w.parse_forecast(payload(),cfg,now()))
        service=w.WeatherService(clock=lambda:clock[0],source=source)
        a=service.get();clock[0]+=timedelta(minutes=5);b=service.get()
        self.assertEqual(a['fetched_at'],b['fetched_at']);self.assertTrue(b['cached']);self.assertEqual(source.call_count,1)
        clock[0]+=timedelta(minutes=11);source.side_effect=w.WeatherError('天气失败')
        with self.assertRaises(w.WeatherError):service.get()
    def test_api_returns_forecast_and_chinese_swagger_description(self):
        from app import create_app
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'nowapi_weather_config.json').write_text(json.dumps(self.cfg))
            application=create_app(root/'tasks.json');client=TestClient(application)
            with patch.object(w.WeatherService,'get',return_value=self.parse()):
                result=client.get('/weather-forecast');self.assertEqual(result.status_code,200)
            schema=client.get('/openapi.json').json()
            self.assertEqual(schema['paths']['/weather-forecast']['get']['tags'],['天气预测'])
            with patch.object(w.WeatherService,'get',side_effect=w.WeatherError('尚未配置天气密钥')):
                self.assertEqual(client.get('/weather-forecast').status_code,503)


if __name__=='__main__':unittest.main()
