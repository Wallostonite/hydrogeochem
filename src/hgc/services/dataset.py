"""Flatten samples + model outputs into ML-ready records.

One record per sample: identity, input analytes (solutes in mg/L), the PHREEQC outputs,
and a saturation index per requested phase. Shared by the API dataset endpoint and the
`ops/build_ml_dataset.py` CLI so the two never diverge.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.models import ModelSpec, WaterSample
from .phreeqc import PhreeqcEngine, build_solution_input, parse_selected_output
from .usgs import aggregate_samples

BUCKETS = ("event", "month", "quarter", "year", "window")

#: Minerals reported by default; all present in phreeqc.dat.
DEFAULT_DATASET_PHASES: tuple[str, ...] = (
    "Calcite", "Dolomite", "Gypsum", "Anhydrite", "Aragonite", "Siderite",
    "Fluorite", "Halite", "Quartz", "Chalcedony", "CO2(g)", "O2(g)",
)


def flatten_sample(sample: WaterSample, spec: ModelSpec, engine: PhreeqcEngine) -> dict[str, Any]:
    """One flat record: inputs joined with the outputs of modelling this exact sample."""
    record: dict[str, Any] = {
        "id_site_id": sample.site_id,
        "id_sampled_at": sample.sampled_at.isoformat() if sample.sampled_at else None,
        "id_latitude": sample.latitude,
        "id_longitude": sample.longitude,
        "id_source": sample.source,
    }
    for m in sample.measurements:
        record[f"in_{m.key}"] = round(m.mg_per_l, 6) if m.parameter.is_solute else m.value
    record["in_charge_balance_pct"] = round(sample.charge_balance_pct(), 3)

    built = build_solution_input(sample, spec)
    try:
        raw = engine.run(built.text, spec.database)
        result = parse_selected_output(raw.selected_output, raw.warnings)
        record["out_status"] = "succeeded"
        record["out_ph"] = result.ph
        record["out_pe"] = result.pe
        record["out_ionic_strength"] = result.ionic_strength
        record["out_charge_balance_pct"] = result.charge_balance_pct
        sis = {s.phase: s.si for s in result.saturation_indices}
        for phase in spec.saturation_phases:
            si = sis.get(phase)
            # -1000 is the engine's sentinel for an undefined phase; leave it blank.
            record[f"si_{phase}"] = si if (si is not None and si > -999) else None
        record["meta_database"] = raw.database
        record["meta_database_sha256"] = raw.database_sha256
        record["meta_engine_version"] = raw.engine_version
        record["meta_duration_ms"] = raw.duration_ms
    except Exception as exc:
        record["out_status"] = "failed"
        record["out_error"] = str(exc)[:200]
    return record


def build_dataset(
    samples: list[WaterSample], spec: ModelSpec, engine: PhreeqcEngine
) -> list[dict[str, Any]]:
    return [flatten_sample(s, spec, engine) for s in samples]


def _period_key(when: datetime | None, bucket: str) -> str:
    if when is None:
        return "~unknown"  # sorts last
    if bucket == "month":
        return f"{when.year}-{when.month:02d}"
    if bucket == "quarter":
        return f"{when.year}-Q{(when.month - 1) // 3 + 1}"
    return f"{when.year}"


def bucket_samples(
    samples: list[WaterSample], bucket: str = "event", aggregate: str = "median"
) -> list[WaterSample]:
    """Reduce one site's samples to the rows a dataset should contain.

    ``event`` keeps every sampling event; ``month``/``quarter``/``year`` aggregate each
    period into one complete analysis; ``window`` collapses the whole range to one row.
    """
    if bucket == "event":
        return sorted(samples, key=lambda s: s.sampled_at.isoformat() if s.sampled_at else "~")
    if bucket == "window":
        rep = aggregate_samples(samples, aggregate)
        return [rep] if rep else []
    groups: dict[str, list[WaterSample]] = {}
    for s in samples:
        groups.setdefault(_period_key(s.sampled_at, bucket), []).append(s)
    reps: list[WaterSample] = []
    for key in sorted(groups):
        rep = aggregate_samples(groups[key], aggregate)
        if rep:
            reps.append(rep)
    return reps
