"""
Regression test: archive CSV values must sit under their own column names.

This exists because they did not. `append_batch` built each record by iterating
`schema.field_names`, which is a **frozenset** — so the values were written in set-iteration
order while the header came from the ordered `schema.csv_header`. On top of that, Python
randomises string hashing per process, so the scrambling was different on every restart.

The failure is close to the worst kind: the file is valid CSV, it has the right number of
columns, and every value is plausible. Nothing raises. It only shows up when someone plots a
column and the numbers are subtly from the wrong sensor — which, for an experiment export, means
a wrong result rather than an error.

The test uses a distinct sentinel per field so that any permutation is unmissable.
"""

from __future__ import annotations

import csv
from datetime import date

import pytest

from aqua_gateway.archive.csv_writer import CsvArchiveWriter
from aqua_gateway.schema.registry import load_registry

ROLES = ("od_sensor", "aerator", "mixer", "pump")


def _distinct_sample(schema):
    """One unique value per field, so a misalignment cannot coincide with a correct value."""
    sample = {}
    for index, field in enumerate(schema.fields):
        if field.type.value == "bool":
            sample[field.name] = index % 2 == 0
        elif field.type.value == "enum":
            sample[field.name] = field.values[index % len(field.values)]
        else:
            sample[field.name] = index + 1
    return sample


def _expected_cell(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_values_align_with_header(tmp_path, role):
    registry = load_registry()
    schema = registry.for_role(role)
    sample = _distinct_sample(schema)

    writer = CsvArchiveWriter(tmp_path)
    await writer.append_batch(
        schema,
        [{**sample, "t_dev_ms": 1791547200000}],
        day=date(2026, 10, 9),
        seq=7,
        t_src="espnow",
    )
    await writer.close_all()

    path = tmp_path / role / "2026-10-09.csv"
    assert path.exists(), "the batch was not flushed to disk"

    lines = path.read_text().splitlines()
    header = next(csv.reader([lines[0]]))
    row = next(csv.reader([lines[1]]))

    # The header must be the registry's declared order, which the export also relies on.
    assert header == schema.csv_header
    assert len(row) == len(header), "row length differs from the header"

    # The three envelope columns come first and are supplied by the writer, not the sample.
    assert row[0] == "1791547200000"
    assert row[1] == "espnow"
    assert row[2] == "7"

    for index, name in enumerate(header[3:], start=3):
        assert row[index] == _expected_cell(sample[name]), (
            f"{role}: column {index} is {name!r} but holds {row[index]!r}; expected {_expected_cell(sample[name])!r}"
        )


@pytest.mark.asyncio
async def test_field_names_is_not_an_ordering_source(tmp_path):
    """
    Guard the specific mistake rather than the symptom.

    If `field_names` is ever reintroduced as an iteration source, the order it yields for a
    given schema will not equal the declared order — that inequality is what the previous bug
    depended on, so asserting it here fails loudly if someone reaches for the set again.
    """
    schema = load_registry().for_role("aerator")
    declared = [field.name for field in schema.fields]

    assert schema.field_names == frozenset(declared), "membership must still work"
    assert isinstance(schema.field_names, frozenset), "field_names should remain a set"
    assert schema.csv_header[3:] == declared, "csv_header must be the declared order"
