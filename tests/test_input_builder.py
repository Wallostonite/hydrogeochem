from __future__ import annotations

from hgc.domain.models import Measurement, ModelSpec, PhaseTarget, WaterSample
from hgc.services.phreeqc.input_builder import build_solution_input


def test_solution_block_declares_units_and_basis(sample, spec):
    built = build_solution_input(sample, spec)
    text = built.text

    assert "units     mg/l" in text
    assert "SOLUTION 1 USGS-06730200" in text
    # Every basis-dependent species carries its basis explicitly.
    assert "as CaCO3" in text          # alkalinity
    assert "S(6)" in text and "as SO4" in text
    assert "Si" in text and "as SiO2" in text
    assert text.rstrip().endswith("END")


def test_micrograms_are_converted_before_entry(sample, spec):
    text = build_solution_input(sample, spec).text
    iron_line = next(line for line in text.splitlines() if line.strip().startswith("Fe"))
    assert "0.045" in iron_line  # 45 ug/L, not 45 mg/L


def test_censored_results_are_halved_and_reported(spec):
    sample = WaterSample(
        site_id="X",
        measurements=[
            Measurement(key="ph", value=7.0, unit="std units"),
            Measurement(key="fe", value=10.0, unit="ug/l", censored=True),
        ],
    )
    built = build_solution_input(sample, spec)
    assert "0.005" in built.text
    assert any("detection limit" in note for note in built.notes)


def test_missing_alkalinity_is_flagged_not_hidden(spec):
    sample = WaterSample(
        site_id="X", measurements=[Measurement(key="ph", value=7.0, unit="std units")]
    )
    built = build_solution_input(sample, spec)
    assert any("alkalinity" in note.lower() for note in built.notes)


def test_charge_balance_option_is_attached_to_ph(sample):
    spec = ModelSpec(charge_balance_on="pH")
    assert "pH" in build_solution_input(sample, spec).text
    assert "charge" in build_solution_input(sample, spec).text


def test_equilibrium_phases_are_rendered(sample):
    spec = ModelSpec(equilibrium_phases=[PhaseTarget(name="Calcite", saturation_index=0, moles=10)])
    text = build_solution_input(sample, spec).text
    assert "EQUILIBRIUM_PHASES 1" in text
    assert "Calcite" in text
