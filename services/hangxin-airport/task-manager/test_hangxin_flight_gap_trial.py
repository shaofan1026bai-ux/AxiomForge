"""只读航班查询的范围、认证隔离和错误脱敏验证，不访问真实网络。"""

from datetime import date
from importlib.util import find_spec
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hangxin_flight_gap_trial import FLIGHT_SQL, FlightTrialError, fetch_raw_flights


@unittest.skipUnless(find_spec("surrealdb") is not None and find_spec("dotenv") is not None,
                     "真实航班查询测试需要requirements-flight-test.txt中的可选依赖")
class FlightQueryTests(unittest.TestCase):
    """模拟SDK，保证试算不能产生设备写入或截断航班集合。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env_file = Path(self.temporary.name) / "test.env"
        self.env_file.write_text(
            "SURREALDB_URL=https://flight-source.invalid/rpc\n"
            "SURREALDB_NAMESPACE=test\nSURREALDB_DATABASE=test\n"
            "SURREALDB_USERNAME=test\nSURREALDB_PASSWORD=test-secret\n"
            "OTHER_PASSWORD=other-secret\n",
            encoding="utf-8",
        )
        self.environment = patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.day = date(2026, 11, 5)

    def test_fixed_select_fetches_complete_neighbor_plan_days(self):
        """参数绑定机场和日期，不能加方向、状态过滤或LIMIT。"""
        with patch("surrealdb.Surreal") as factory:
            database = factory.return_value.__enter__.return_value
            database.query.return_value = [{"flightNum": "TEST"}]
            rows, report = fetch_raw_flights(self.day, self.env_file)
        database.query.assert_called_once_with(FLIGHT_SQL, {
            "airport": "LUM", "dates": ["2026-11-04 00:00:00", "2026-11-05 00:00:00", "2026-11-06 00:00:00"],
        })
        self.assertTrue(FLIGHT_SQL.startswith("SELECT"))
        self.assertNotIn("LIMIT", FLIGHT_SQL)
        self.assertEqual(rows, [{"flightNum": "TEST"}])
        self.assertNotIn("secret", str(report))
        self.assertNotIn("invalid", str(report))
        self.assertFalse(report["device_control_sent"])

    def test_empty_success_is_not_a_query_failure_or_whole_day_gap(self):
        """空结果保持为空，仅证明查询完成，不声明源库数据完整。"""
        with patch("surrealdb.Surreal") as factory:
            factory.return_value.__enter__.return_value.query.return_value = []
            rows, report = fetch_raw_flights(self.day, self.env_file)
        self.assertEqual(rows, [])
        self.assertFalse(report["source_completeness_guaranteed"])

    def test_authentication_network_and_sdk_errors_are_sanitized(self):
        """底层异常含地址或密码也不得向调用者传播。"""
        for operation in ("signin", "use", "query"):
            with self.subTest(operation=operation), patch("surrealdb.Surreal") as factory:
                database = factory.return_value.__enter__.return_value
                getattr(database, operation).side_effect = RuntimeError("test-secret https://private.invalid")
                with self.assertRaises(FlightTrialError) as caught:
                    fetch_raw_flights(self.day, self.env_file)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("invalid", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_sdk_response_shell_cannot_be_treated_as_raw_flights(self):
        """拒绝SDK外壳或非对象数组，不能误算成成功空档。"""
        for response in ({"status": "OK", "result": []}, [None], "unexpected"):
            with self.subTest(response=response), patch("surrealdb.Surreal") as factory:
                factory.return_value.__enter__.return_value.query.return_value = response
                with self.assertRaises(FlightTrialError):
                    fetch_raw_flights(self.day, self.env_file)

    def test_credentials_in_connection_url_are_rejected_before_connecting(self):
        """认证必须通过SDK显式字段传入，不能嵌入地址。"""
        self.env_file.write_text(self.env_file.read_text().replace(
            "https://flight-source.invalid/rpc", "https://user:secret@flight-source.invalid/rpc"
        ))
        with patch("surrealdb.Surreal") as factory, self.assertRaises(FlightTrialError):
            fetch_raw_flights(self.day, self.env_file)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
