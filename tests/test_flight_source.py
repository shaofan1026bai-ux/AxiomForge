"""核对固定只读查询、参数绑定、配置优先级与错误脱敏。"""

from datetime import date
from typing import Any

import pytest

from axiomforge import flight_source


def test_read_only_query_and_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("SURREALDB_") or key == "AXIOMFORGE_FLIGHT_ENV_FILE":
            monkeypatch.delenv(key)
    for key, value in {
        "URL": "https://example.invalid/rpc",
        "NAMESPACE": "airport",
        "DATABASE": "flights",
        "USERNAME": "reader",
        "PASSWORD": "hidden",
        "AUTH_LEVEL": "database",
    }.items():
        monkeypatch.setenv("SURREALDB_" + key, value)
    seen: dict[str, Any] = {}

    class Database:
        def __init__(self, url: str) -> None:
            pass

        def __enter__(self) -> "Database":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def signin(self, values: dict[str, Any]) -> None:
            seen["auth"] = values

        def use(self, *args: Any) -> None:
            pass

        def query(self, sql: str, values: dict[str, Any]) -> list[Any]:
            seen.update(sql=sql, values=values)
            return []

    monkeypatch.setattr(flight_source, "Surreal", Database)
    result = flight_source.load_flights(date(2026, 11, 4))
    assert result.flights == ()
    assert seen["sql"] == flight_source.FLIGHT_SQL
    assert seen["sql"].startswith("SELECT") and "LIMIT" not in seen["sql"]
    assert seen["values"] == {
        "airport": "LUM",
        "dates": ["2026-11-03 00:00:00", "2026-11-04 00:00:00", "2026-11-05 00:00:00"],
    }
    assert seen["auth"]["database"] == "flights"

    def fail(*args: Any) -> Any:
        raise RuntimeError("hidden password https://private.invalid")

    monkeypatch.setattr(flight_source, "Surreal", fail)
    with pytest.raises(flight_source.FlightSourceError) as exc:
        flight_source.load_flights(date(2026, 11, 4))
    assert "hidden" not in str(exc.value)
    assert "private.invalid" not in str(exc.value)
