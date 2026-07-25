"""Translate IPhreeqc SELECTED_OUTPUT arrays into domain results."""

from __future__ import annotations

from typing import Any

from ...domain.models import ModelResult, SaturationIndex

_SCALARS = {
    "pH": "ph",
    "pe": "pe",
    "temp(C)": "temperature_c",
    "mu": "ionic_strength",
    "pct_err": "charge_balance_pct",
}


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return text
    return value


def parse_selected_output(array: list[list[Any]], warnings: list[str] | None = None) -> ModelResult:
    """`array[0]` is the header row; subsequent rows are simulation steps.

    We report the final step, which is the state after any EQUILIBRIUM_PHASES reaction,
    and keep every row in `selected_output` for users who want the reaction path.
    """
    result = ModelResult(warnings=list(warnings or []))
    if not array or len(array) < 2:
        result.warnings.append("PHREEQC produced no selected output rows.")
        return result

    headers = [str(h).strip() for h in array[0]]
    rows: list[dict[str, Any]] = []
    for raw_row in array[1:]:
        rows.append({h: _coerce(v) for h, v in zip(headers, raw_row, strict=False)})
    result.selected_output = rows

    final = rows[-1]
    for header, attr in _SCALARS.items():
        value = final.get(header)
        if isinstance(value, (int, float)):
            setattr(result, attr, float(value))

    for header, value in final.items():
        if header.startswith("si_") and isinstance(value, (int, float)):
            result.saturation_indices.append(
                SaturationIndex(phase=header[3:], si=float(value))
            )
        elif header.endswith("(mol/kgw)") and isinstance(value, (int, float)):
            result.totals_mol_kgw[header.removesuffix("(mol/kgw)")] = float(value)

    result.saturation_indices.sort(key=lambda s: s.si, reverse=True)
    return result
