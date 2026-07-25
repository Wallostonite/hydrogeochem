"""Unit conversion and charge balance.

Every value entering a PHREEQC SOLUTION block passes through here. Unit errors in
water chemistry are silent: ug/L iron entered as mg/L is wrong by 1000x and the model
still converges, still reports a saturation index, and is still garbage.
"""

from __future__ import annotations

from .errors import ValidationError
from .parameters import Parameter

#: Multipliers to mg/L for mass-per-volume units.
_MASS_PER_VOLUME: dict[str, float] = {
    "mg/l": 1.0,
    "mg/L": 1.0,
    "ppm": 1.0,
    "mg/l as caco3": 1.0,
    "ug/l": 1e-3,
    "ug/L": 1e-3,
    "ppb": 1e-3,
    "g/l": 1e3,
    "mg/ml": 1e3,
}

_MOLAR = {"mol/l", "mol/L", "m"}
_MILLIMOLAR = {"mmol/l", "mmol/L", "mm"}
_MICROMOLAR = {"umol/l", "umol/L", "um"}
_MILLIEQUIV = {"meq/l", "meq/L"}
_MICROEQUIV = {"ueq/l", "ueq/L"}


def normalize_unit(unit: str) -> str:
    return unit.strip().replace("µ", "u").casefold()


def to_mg_per_l(value: float, unit: str, parameter: Parameter) -> float:
    """Convert a reported value to mg/L on the parameter's own reporting basis."""
    u = normalize_unit(unit)

    if u in _MASS_PER_VOLUME:
        return value * _MASS_PER_VOLUME[u]

    if u in _MOLAR | _MILLIMOLAR | _MICROMOLAR:
        if parameter.molar_mass is None:
            raise ValidationError(
                f"cannot convert {unit} for {parameter.label}: no molar mass on record",
                parameter=parameter.key,
            )
        factor = {**dict.fromkeys(_MOLAR, 1e3),
                  **dict.fromkeys(_MILLIMOLAR, 1.0),
                  **dict.fromkeys(_MICROMOLAR, 1e-3)}[u]
        return value * factor * parameter.molar_mass

    if u in _MILLIEQUIV | _MICROEQUIV:
        eq = parameter.equivalent_weight
        if eq is None:
            raise ValidationError(
                f"cannot convert {unit} for {parameter.label}: uncharged or unknown charge",
                parameter=parameter.key,
            )
        factor = 1.0 if u in _MILLIEQUIV else 1e-3
        return value * factor * eq

    raise ValidationError(f"unsupported unit {unit!r} for {parameter.label}",
                          parameter=parameter.key, unit=unit)


def mmol_per_l(value_mg_l: float, parameter: Parameter) -> float | None:
    if parameter.molar_mass is None:
        return None
    return value_mg_l / parameter.molar_mass


def meq_per_l(value_mg_l: float, parameter: Parameter) -> float | None:
    """Signed milliequivalents per litre; None for species that carry no charge."""
    eq = parameter.equivalent_weight
    if eq is None:
        return None
    magnitude = value_mg_l / eq
    return magnitude if parameter.charge > 0 else -magnitude


def alkalinity_caco3_to_hco3(value_mg_l_caco3: float) -> float:
    """Alkalinity reported as CaCO3 -> equivalent mg/L HCO3 (x 61.016 / 50.043)."""
    return value_mg_l_caco3 * (61.016 / 50.043)


def charge_balance_error(meq_values: dict[str, float]) -> float:
    """Percent charge-balance error: 100 * (sum cations - sum anions) / (sum |cations| + |anions|).

    Convention follows USGS/PHREEQC. |CBE| under ~5% is a usable analysis; above 10%
    the analysis is incomplete or wrong and any derived SI should be treated as indicative.
    """
    cations = sum(v for v in meq_values.values() if v > 0)
    anions = -sum(v for v in meq_values.values() if v < 0)
    total = cations + anions
    if total == 0:
        return 0.0
    return 100.0 * (cations - anions) / total


def ionic_strength_estimate(meq_values: dict[str, float], molal: dict[str, float]) -> float:
    """Rough I = 0.5 * sum(m_i * z_i^2), used only for pre-flight sanity checks.

    The authoritative ionic strength comes from PHREEQC's speciation, not from here.
    """
    total = 0.0
    for key, m in molal.items():
        meq = meq_values.get(key)
        if meq is None or m == 0:
            continue
        z = abs(meq) / m if m else 0.0
        total += m * z * z
    return 0.5 * total
