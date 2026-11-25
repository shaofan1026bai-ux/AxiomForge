from datetime import UTC, datetime, timedelta

from axiomforge.ahu_temperature import BINS
from axiomforge.region_climate import TemperatureTimeCell
from axiomforge.ui.ahu_temperature_charts import temperature_bin_colors
from axiomforge.ui.region_climate_charts import temperature_time_options


def test_temperature_time_chart_aligns_rows_and_uses_distribution_bins() -> None:
    last_hour = datetime(2026, 11, 12, 15, tzinfo=UTC)
    half_hour = timedelta(minutes=30)
    final_start = last_hour + half_hour
    options = temperature_time_options(
        [
            ("区域", [TemperatureTimeCell(final_start, final_start + half_hour, 26, 6, 6)]),
            ("探头", [TemperatureTimeCell(last_hour, final_start, None, 0, 6)]),
        ],
        dark=True,
        minutes=30,
    )

    assert options["xAxis"]["data"] == ["23:00", "23:30"]
    assert options["yAxis"]["data"] == ["区域", "探头"]
    assert [item["color"] for item in options["visualMap"]["pieces"][: len(BINS)]] == list(
        temperature_bin_colors(True)
    )
    cells = options["series"][0]["data"]
    assert cells[0] == {
        "name": "区域",
        "value": [1, 0, 4],
        "time": "23:30–24:00",
        "temperature": 26,
        "band": "26～28℃",
        "count": 6,
        "expected": 6,
    }
    assert cells[1]["value"] == [0, 1, len(BINS)]
    assert cells[1]["band"] == "缺测"
    assert cells[1]["temperature"] is None
