import pytest

from axiomforge.ahu_temperature import BINS
from axiomforge.ui.ahu_temperature_charts import temperature_distribution_options


def test_temperature_distribution_uses_each_rows_own_denominator_and_leaves_empty_rows_blank():
    options = temperature_distribution_options(
        [
            ("迎客厅", (0, 0, 2, 6, 0, 0, 0)),
            ("探头（无有效采样）", (0,) * len(BINS)),
            ("候机厅", (0, 1, 0, 2, 0, 0, 0)),
        ],
        dark=True,
        show_missing=True,
    )

    assert options["yAxis"]["data"] == ["迎客厅", "探头（无有效采样）", "候机厅"]
    assert options["xAxis"]["min"] == 0
    assert options["xAxis"]["max"] == 100
    assert options["legend"]["selectedMode"] is False
    assert [series["name"] for series in options["series"]] == [bucket.name for bucket in BINS]
    assert options["series"][2]["data"][0] == {"value": 25, "count": 2, "total": 8}
    assert options["series"][1]["data"][2] == {
        "value": pytest.approx(100 / 3),
        "count": 1,
        "total": 3,
    }
    assert all(
        series["data"][1] == {"value": None, "count": 0, "total": 0} for series in options["series"]
    )
