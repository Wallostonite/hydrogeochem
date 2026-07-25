from __future__ import annotations

import pytest

from hgc.domain import units
from hgc.domain.errors import ValidationError
from hgc.domain.parameters import BY_KEY, lookup


def test_micrograms_convert_to_milligrams():
    iron = BY_KEY["fe"]
    assert units.to_mg_per_l(45.0, "ug/l", iron) == pytest.approx(0.045)
    assert units.to_mg_per_l(45.0, "µg/L", iron) == pytest.approx(0.045)


def test_millimolar_uses_molar_mass():
    calcium = BY_KEY["ca"]
    assert units.to_mg_per_l(1.0, "mmol/l", calcium) == pytest.approx(40.078)


def test_milliequivalents_use_equivalent_weight():
    calcium = BY_KEY["ca"]  # divalent: eq weight is half the molar mass
    assert units.to_mg_per_l(1.0, "meq/l", calcium) == pytest.approx(20.039)


def test_unknown_unit_is_rejected_not_guessed():
    with pytest.raises(ValidationError):
        units.to_mg_per_l(1.0, "grains per gallon", BY_KEY["ca"])


def test_alkalinity_basis_conversion():
    # 100 mg/L as CaCO3 is 122 mg/L as HCO3; getting this wrong moves calcite SI by ~0.1
    assert units.alkalinity_caco3_to_hco3(100.0) == pytest.approx(121.93, abs=0.05)


def test_charge_balance_of_a_clean_analysis(sample):
    assert abs(sample.charge_balance_pct()) < 10


def test_charge_balance_sign_follows_cation_excess():
    assert units.charge_balance_error({"na": 5.0, "cl": -3.0}) > 0
    assert units.charge_balance_error({"na": 3.0, "cl": -5.0}) < 0
    assert units.charge_balance_error({}) == 0.0


def test_lookup_resolves_pcode_key_and_wqp_name():
    assert lookup("00915").key == "ca"
    assert lookup("ca").key == "ca"
    assert lookup("Calcium").key == "ca"
    assert lookup("Unobtanium") is None
