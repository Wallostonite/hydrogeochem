from __future__ import annotations

from hgc.services.ingest import build_template_csv, parse_samples_csv


def test_template_round_trips() -> None:
    """The shipped template must parse cleanly into exactly one sample."""
    samples, report = parse_samples_csv(build_template_csv())
    assert report.rows == 1
    assert report.sites == 1
    assert not report.ignored
    assert not report.missing_required  # the template covers every required analyte
    sample = samples[0]
    assert sample.site_id == "WELL-1"
    assert sample.source == "upload"
    keys = {m.key for m in sample.measurements}
    assert {"ph", "ca", "mg", "na", "cl", "so4"} <= keys


def test_header_units_and_bare_keys_resolve() -> None:
    csv = "ca (mg/L),Magnesium,00930\n40,12,25\n"  # unit header, label, pcode
    samples, report = parse_samples_csv(csv)
    assert report.rows == 1
    by_key = {m.key: m for m in samples[0].measurements}
    assert set(by_key) == {"ca", "mg", "na"}
    assert by_key["ca"].unit == "mg/L"
    assert by_key["mg"].value == 12
    assert by_key["na"].value == 25


def test_censored_and_blank_values() -> None:
    csv = "ph,ca,mg\n7.4,<0.5,\n"
    samples, _ = parse_samples_csv(csv)
    by_key = {m.key: m for m in samples[0].measurements}
    assert by_key["ca"].value == 0.5
    assert by_key["ca"].censored is True
    assert "mg" not in by_key  # blank cell contributes no measurement


def test_unknown_columns_are_reported_not_fatal() -> None:
    csv = "site_id,ca,mystery_col\nA-1,40,foo\n"
    samples, report = parse_samples_csv(csv)
    assert report.ignored == ["mystery_col"]
    assert samples[0].site_id == "A-1"
    assert {m.key for m in samples[0].measurements} == {"ca"}


def test_metadata_and_missing_required() -> None:
    csv = "site_id,date,latitude,longitude,ca\nS1,2024-03-01,39.05,-107.35,40\n"
    samples, report = parse_samples_csv(csv)
    s = samples[0]
    assert s.latitude == 39.05 and s.longitude == -107.35
    assert s.sampled_at is not None and s.sampled_at.year == 2024
    # only ca present, so the rest of the required set is reported missing
    assert set(report.missing_required) == {"ph", "mg", "na", "cl", "so4"}


def test_row_with_no_recognised_analyte_is_skipped() -> None:
    csv = "site_id,note\nA-1,hello\n"
    samples, report = parse_samples_csv(csv)
    assert samples == []
    assert report.rows == 0
