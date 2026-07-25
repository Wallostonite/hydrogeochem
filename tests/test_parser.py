from __future__ import annotations

from hgc.services.phreeqc.parser import parse_selected_output

ARRAY = [
    ["sim", "pH", "pe", "temp(C)", "mu", "pct_err", "si_Calcite", "si_Gypsum", "Ca(mol/kgw)"],
    [1, 7.4, 4.0, 12.5, 0.0081, -1.2, 0.31, -1.85, 0.0022],
    [1, 7.1, 4.0, 12.5, 0.0079, -1.1, 0.00, -1.80, 0.0019],
]


def test_final_step_is_reported_and_all_steps_retained():
    result = parse_selected_output(ARRAY)
    assert result.ph == 7.1                    # post-reaction state
    assert len(result.selected_output) == 2    # the path is kept
    assert result.si("Calcite") == 0.0
    assert result.totals_mol_kgw["Ca"] == 0.0019


def test_saturation_indices_sort_high_to_low():
    result = parse_selected_output(ARRAY)
    assert [s.phase for s in result.saturation_indices] == ["Calcite", "Gypsum"]
    assert result.saturation_indices[0].state == "at equilibrium"


def test_empty_output_is_a_warning_not_a_crash():
    result = parse_selected_output([])
    assert result.ph is None
    assert result.warnings
