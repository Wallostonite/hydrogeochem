from __future__ import annotations

import os

os.environ.setdefault("HGC_ENV", "test")

import pytest

from hgc.domain.models import Measurement, ModelSpec, WaterSample


@pytest.fixture()
def sample() -> WaterSample:
    """A charge-balanced calcium-bicarbonate water, roughly typical of a limestone aquifer."""
    return WaterSample(
        site_id="USGS-06730200",
        measurements=[
            Measurement(key="ph", value=7.4, unit="std units"),
            Measurement(key="temperature", value=12.5, unit="deg C"),
            Measurement(key="ca", value=88.0, unit="mg/l"),
            Measurement(key="mg", value=24.0, unit="mg/l"),
            Measurement(key="na", value=15.0, unit="mg/l"),
            Measurement(key="k", value=2.1, unit="mg/l"),
            Measurement(key="cl", value=12.0, unit="mg/l"),
            Measurement(key="so4", value=64.0, unit="mg/l"),
            Measurement(key="alk_caco3", value=250.0, unit="mg/l"),
            Measurement(key="sio2", value=11.0, unit="mg/l"),
            Measurement(key="fe", value=45.0, unit="ug/l"),
        ],
    )


@pytest.fixture()
def spec() -> ModelSpec:
    return ModelSpec(database="phreeqc.dat", saturation_phases=("Calcite", "Dolomite", "Gypsum"))
