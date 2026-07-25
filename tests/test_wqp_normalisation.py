from __future__ import annotations

from hgc.services.usgs import _parse_rdb_sites, aggregate_samples, normalise_wqp_rows

ROWS = [
    {
        "MonitoringLocationIdentifier": "USGS-06730200",
        "ActivityStartDate": "2024-03-14",
        "ActivityStartTime/Time": "10:30:00",
        "CharacteristicName": "Calcium",
        "USGSPCode": "00915",
        "ResultMeasureValue": "88",
        "ResultMeasure/MeasureUnitCode": "mg/l",
    },
    {
        "MonitoringLocationIdentifier": "USGS-06730200",
        "ActivityStartDate": "2024-03-14",
        "ActivityStartTime/Time": "10:30:00",
        "CharacteristicName": "Iron",
        "USGSPCode": "01046",
        "ResultMeasureValue": "",
        "ResultDetectionConditionText": "Not Detected",
        "DetectionQuantitationLimitMeasure/MeasureValue": "10",
        "DetectionQuantitationLimitMeasure/MeasureUnitCode": "ug/l",
    },
    {
        "MonitoringLocationIdentifier": "USGS-06730200",
        "ActivityStartDate": "2024-03-14",
        "CharacteristicName": "Unicornium",
        "ResultMeasureValue": "3",
        "ResultMeasure/MeasureUnitCode": "mg/l",
    },
]


def test_rows_group_into_one_sample_and_drop_unmappable_analytes():
    samples = normalise_wqp_rows(ROWS, site_id="USGS-06730200")
    assert len(samples) == 1
    assert {m.key for m in samples[0].measurements} == {"ca", "fe"}


def test_non_detects_carry_the_detection_limit_and_the_flag():
    sample = normalise_wqp_rows(ROWS, site_id="USGS-06730200")[0]
    iron = sample.get("fe")
    assert iron.censored is True
    assert iron.value == 10.0 and iron.unit == "ug/l"


def test_median_aggregation_ignores_a_single_outlier():
    rows = []
    for day, value in (("2024-01-01", "80"), ("2024-02-01", "90"), ("2024-03-01", "8000")):
        rows.append(
            {
                "MonitoringLocationIdentifier": "S",
                "ActivityStartDate": day,
                "CharacteristicName": "Calcium",
                "USGSPCode": "00915",
                "ResultMeasureValue": value,
                "ResultMeasure/MeasureUnitCode": "mg/l",
            }
        )
    merged = aggregate_samples(normalise_wqp_rows(rows, "S"), "median")
    assert merged.value_mg_l("ca") == 90.0


def test_rdb_site_parsing_skips_comments_and_the_format_row():
    text = (
        "# comment\n"
        "agency_cd\tsite_no\tstation_nm\tdec_lat_va\tdec_long_va\n"
        "5s\t15s\t50s\t16s\t16s\n"
        "USGS\t06730200\tBOULDER CREEK\t40.05\t-105.18\n"
    )
    sites = _parse_rdb_sites(text)
    assert len(sites) == 1
    assert sites[0].site_id == "USGS-06730200"
    assert sites[0].latitude == 40.05
