"""Render a normalised WaterSample into PHREEQC input.

Two rules govern this module:
  1. Every concentration is emitted with an explicit reporting basis (`as SO4`, `as N`,
     `as HCO3`). Relying on database defaults is how alkalinity ends up 22% wrong.
  2. Nothing is silently dropped. Values excluded by the censoring policy are returned
     as notes so the caller can show them to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...domain.models import ModelSpec, WaterSample
from ...domain.parameters import BY_KEY
from .sanitizer import validate_phase_name

_INDENT = " " * 4


@dataclass(slots=True)
class BuiltInput:
    text: str
    notes: list[str] = field(default_factory=list)
    charge_balance_pct: float | None = None
    included_keys: list[str] = field(default_factory=list)


def _fmt(value: float) -> str:
    return f"{value:.6g}"


def _solution_lines(sample: WaterSample, spec: ModelSpec) -> tuple[list[str], list[str], list[str]]:
    lines: list[str] = []
    notes: list[str] = []
    included: list[str] = []

    temp = spec.temperature_c if spec.temperature_c is not None else sample.temperature_c
    lines.append(f"{_INDENT}units     mg/l")
    lines.append(f"{_INDENT}temp      {_fmt(temp if temp is not None else 25.0)}")
    if temp is None:
        notes.append("No field temperature reported; assumed 25 degC.")

    ph = sample.ph
    if ph is None:
        ph, note = 7.0, "No pH reported; assumed 7.0. Saturation indices are indicative only."
        notes.append(note)
    charge_on_ph = spec.charge_balance_on == "pH"
    lines.append(f"{_INDENT}pH        {_fmt(ph)}{' charge' if charge_on_ph else ''}")

    if spec.pe is not None:
        lines.append(f"{_INDENT}pe        {_fmt(spec.pe)}")
    if spec.redox_couple:
        lines.append(f"{_INDENT}redox     {spec.redox_couple}")
    lines.append(f"{_INDENT}density   1.0")
    lines.append(f"{_INDENT}-water    1 # kg")

    # Alkalinity: prefer a directly reported HCO3, otherwise carry CaCO3 through
    # with its explicit basis rather than pre-converting.
    alkalinity_emitted = False
    for key in ("hco3", "alk_caco3"):
        m = sample.get(key)
        if m is None:
            continue
        basis = BY_KEY[key].basis or "HCO3"
        lines.append(f"{_INDENT}{'Alkalinity':<10}{_fmt(m.mg_per_l):>10} as {basis}")
        included.append(key)
        alkalinity_emitted = True
        break
    if not alkalinity_emitted:
        notes.append(
            "No alkalinity or bicarbonate reported; the carbonate system is unconstrained "
            "and carbonate-mineral SI values will be unreliable."
        )

    for m in sample.measurements:
        p = m.parameter
        if not p.is_solute or p.key in ("hco3", "alk_caco3"):
            continue
        value = m.mg_per_l
        if m.censored:
            if spec.censored_policy == "drop":
                notes.append(f"{p.label}: below detection limit, excluded.")
                continue
            if spec.censored_policy == "zero":
                notes.append(f"{p.label}: below detection limit, entered as 0.")
                value = 0.0
            else:
                value = value / 2.0
                notes.append(f"{p.label}: below detection limit, entered at half the limit.")
        if value <= 0:
            notes.append(f"{p.label}: non-positive value, excluded from the solution.")
            continue
        basis = f" as {p.basis}" if p.basis else ""
        lines.append(f"{_INDENT}{p.phreeqc:<10}{_fmt(value):>10}{basis}")
        included.append(p.key)

    if spec.charge_balance_on in ("Cl", "Na"):
        target = spec.charge_balance_on
        lines = [
            line + " charge"
            if line.strip().startswith(target) and " charge" not in line
            else line
            for line in lines
        ]

    return lines, notes, included


def _equilibrium_block(spec: ModelSpec) -> list[str]:
    if not spec.equilibrium_phases:
        return []
    out = ["EQUILIBRIUM_PHASES 1"]
    for phase in spec.equilibrium_phases:
        name = validate_phase_name(phase.name)
        out.append(f"{_INDENT}{name:<14}{_fmt(phase.saturation_index):>8}{_fmt(phase.moles):>12}")
    return out


def _selected_output_block(spec: ModelSpec, totals: list[str]) -> list[str]:
    phases = " ".join(validate_phase_name(p) for p in spec.saturation_phases)
    unique_totals = " ".join(dict.fromkeys(totals)) or "Ca Mg Na K Cl S(6) C(4)"
    return [
        "SELECTED_OUTPUT 1",
        f"{_INDENT}-reset            false",
        f"{_INDENT}-solution         true",
        f"{_INDENT}-pH               true",
        f"{_INDENT}-pe               true",
        f"{_INDENT}-temperature      true",
        f"{_INDENT}-ionic_strength   true",
        f"{_INDENT}-charge_balance   true",
        f"{_INDENT}-percent_error    true",
        f"{_INDENT}-water            true",
        f"{_INDENT}-saturation_indices {phases}",
        f"{_INDENT}-totals           {unique_totals}",
    ]


def build_solution_input(sample: WaterSample, spec: ModelSpec) -> BuiltInput:
    """Full, runnable PHREEQC input for one sample."""
    title = spec.title.replace("\n", " ")[:120]
    body, notes, included = _solution_lines(sample, spec)

    totals = [BY_KEY[k].phreeqc for k in included if BY_KEY[k].phreeqc]
    totals = [t for t in totals if t]
    if "Alkalinity" in totals:
        totals[totals.index("Alkalinity")] = "C(4)"

    lines = [
        f"TITLE {title}",
        f"SOLUTION 1 {sample.site_id}",
        *body,
        *_equilibrium_block(spec),
        *_selected_output_block(spec, totals),
        "END",
    ]

    cbe = sample.charge_balance_pct()
    if abs(cbe) > 10:
        notes.append(
            f"Charge-balance error is {cbe:+.1f}%. Analyses beyond +/-10% are incomplete; "
            "treat the model output as indicative."
        )
    elif abs(cbe) > 5:
        notes.append(f"Charge-balance error is {cbe:+.1f}% (acceptable but not ideal).")

    return BuiltInput(
        text="\n".join(lines) + "\n",
        notes=notes,
        charge_balance_pct=cbe,
        included_keys=included,
    )


def build_custom_input(raw: str, spec: ModelSpec) -> BuiltInput:
    """Expert-authored input. We append a SELECTED_OUTPUT block only if none is present,
    so that results are machine-readable without overriding the author's own reporting."""
    text = raw if raw.endswith("\n") else raw + "\n"
    if "SELECTED_OUTPUT" not in text.upper():
        text += "\n".join(_selected_output_block(spec, [])) + "\nEND\n"
    return BuiltInput(text=text, notes=["Custom input: server-side validation is limited to safety."])


def summarise_for_display(built: BuiltInput) -> str:
    return built.text if len(built.text) < 8000 else built.text[:8000] + "\n# ...truncated\n"
