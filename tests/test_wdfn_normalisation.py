from __future__ import annotations

from hgc.services.usgs import (
    _parse_ogc_features,
    aggregate_samples,
    normalise_samples_rows,
)

# USGS Samples API (fullphyschem) result rows.
ROWS = [
    {
        "Location_Identifier": "USGS-06730200",
        "Location_Name": "BOULDER CREEK",
        "Location_LatitudeStandardized": "40.05",
        "Location_LongitudeStandardized": "-105.18",
        "Activity_StartDate": "2024-03-14",
        "Activity_StartTime": "10:30:00",
        "Result_Characteristic": "Calcium",
        "USGSpcode": "00915",
        "Result_Measure": "88",
        "Result_MeasureUnit": "mg/L",
    },
    {
        "Location_Identifier": "USGS-06730200",
        "Activity_StartDate": "2024-03-14",
        "Activity_StartTime": "10:30:00",
        "Result_Characteristic": "Iron",
        "USGSpcode": "01046",
        "Result_Measure": "",
        "Result_ResultDetectionCondition": "Not Detected",
        "DetectionLimit_MeasureA": "10",
        "DetectionLimit_MeasureUnitA": "ug/L",
    },
    {
        "Location_Identifier": "USGS-06730200",
        "Activity_StartDate": "2024-03-14",
        "Result_Characteristic": "Unicornium",
        "Result_Measure": "3",
        "Result_MeasureUnit": "mg/L",
    },
]


def test_rows_group_into_one_sample_and_drop_unmappable_analytes():
    samples = normalise_samples_rows(ROWS, site_id="USGS-06730200")
    assert len(samples) == 1
    assert {m.key for m in samples[0].measurements} == {"ca", "fe"}


def test_sample_carries_coordinates_from_the_samples_api():
    sample = normalise_samples_rows(ROWS, site_id="USGS-06730200")[0]
    assert sample.latitude == 40.05 and sample.longitude == -105.18


def test_non_detects_carry_the_detection_limit_and_the_flag():
    sample = normalise_samples_rows(ROWS, site_id="USGS-06730200")[0]
    iron = sample.get("fe")
    assert iron.censored is True
    assert iron.value == 10.0 and iron.unit == "ug/L"


def test_median_aggregation_ignores_a_single_outlier():
    rows = []
    for day, value in (("2024-01-01", "80"), ("2024-02-01", "90"), ("2024-03-01", "8000")):
        rows.append(
            {
                "Location_Identifier": "S",
                "Activity_StartDate": day,
                "Result_Characteristic": "Calcium",
                "USGSpcode": "00915",
                "Result_Measure": value,
                "Result_MeasureUnit": "mg/L",
            }
        )
    merged = aggregate_samples(normalise_samples_rows(rows, "S"), "median")
    assert merged.value_mg_l("ca") == 90.0


def test_ogc_feature_parsing_yields_site_summaries_with_coordinates():
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-105.18, 40.05]},
                "properties": {
                    "id": "USGS-06730200",
                    "monitoring_location_name": "BOULDER CREEK",
                    "agency_code": "USGS",
                    "site_type_code": "ST",
                    "hydrologic_unit_code": "10190005",
                    "state_code": "08",
                },
            },
            {"type": "Feature", "geometry": None, "properties": {"id": ""}},  # skipped: no id
        ],
    }
    sites = _parse_ogc_features(payload)
    assert len(sites) == 1
    assert sites[0].site_id == "USGS-06730200"
    assert sites[0].latitude == 40.05 and sites[0].longitude == -105.18
    assert sites[0].huc == "10190005"
