"""Checks for the interactive demo's actual data and isolation guarantees."""

import csv
import io
from datetime import date, timedelta

import pytest

from axiomforge.demo import (
    DEMO_END,
    DEMO_START,
    PROJECTS,
    Parameters,
    export_csv,
    generate_samples,
)


def test_range_sampling_and_reproducibility() -> None:
    project = PROJECTS[0]
    args = (project.id, project.devices[0], DEMO_START, DEMO_END, Parameters())
    samples = generate_samples(*args)
    assert len(samples) == 7 * 24
    assert samples == generate_samples(*args)
    assert samples[0].time.date() == DEMO_START
    assert samples[-1].time.date() == DEMO_END
    assert len(generate_samples(*args[:-1], Parameters(interval=30))) == 2 * len(samples)


def test_projects_cannot_use_each_others_devices() -> None:
    with pytest.raises(ValueError, match="不属于"):
        generate_samples(PROJECTS[0].id, PROJECTS[1].devices[0], DEMO_START, DEMO_END, Parameters())


@pytest.mark.parametrize("end", [DEMO_START - timedelta(days=1), DEMO_START + timedelta(days=31)])
def test_invalid_ranges_rejected(end: date) -> None:
    with pytest.raises(ValueError):
        generate_samples(PROJECTS[0].id, PROJECTS[0].devices[0], DEMO_START, end, Parameters())


def test_csv_keeps_scope_units_and_demo_provenance() -> None:
    project = PROJECTS[2]
    samples = generate_samples(project.id, project.devices[0], DEMO_END, DEMO_END, Parameters())
    data = export_csv(project.id, project.devices[0], samples).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(data)))
    assert len(rows) == 24
    assert {row["project_id"] for row in rows} == {project.id}
    assert {row["device"] for row in rows} == {project.devices[0]}
    assert {row["source"] for row in rows} == {"synthetic_demo"}
    assert float(rows[0]["power_kW"]) == samples[0].power
