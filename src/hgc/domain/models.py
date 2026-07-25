"""Canonical domain models shared by the API, the workers, and the UI client."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import units
from .parameters import BY_KEY, DEFAULT_PHASES, Parameter, lookup


class Measurement(BaseModel):
    """One analytical result, already resolved to a known parameter."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Canonical parameter key, e.g. 'ca'")
    value: float
    unit: str
    censored: bool = Field(default=False, description="Reported below the detection limit")
    method: str | None = None

    @property
    def parameter(self) -> Parameter:
        return BY_KEY[self.key]

    @property
    def mg_per_l(self) -> float:
        return units.to_mg_per_l(self.value, self.unit, self.parameter)

    @model_validator(mode="after")
    def _known_parameter(self) -> Measurement:
        if self.key not in BY_KEY:
            raise ValueError(f"unknown parameter key {self.key!r}")
        return self


class SiteSummary(BaseModel):
    site_id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    agency: str | None = None
    site_type: str | None = None
    huc: str | None = None
    state: str | None = None


class ReadySite(BaseModel):
    """A site that carries (most of) the analytes a speciation model needs.

    Returned by data-source searches that filter on chemistry, unlike the raw NWIS site
    catalogue. ``analytes`` is the set of required keys actually present; ``missing`` is the
    remainder, so a client can show how complete each site is.
    """

    site_id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    source: str = "wqp"
    analytes: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class WaterSample(BaseModel):
    """A single analysis at a point in time, normalised and unit-resolved."""

    id: UUID = Field(default_factory=uuid4)
    site_id: str
    sampled_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    source: str = "wqp"
    measurements: list[Measurement] = Field(default_factory=list)

    def get(self, key: str) -> Measurement | None:
        return next((m for m in self.measurements if m.key == key), None)

    def value_mg_l(self, key: str) -> float | None:
        m = self.get(key)
        return m.mg_per_l if m else None

    @property
    def ph(self) -> float | None:
        m = self.get("ph")
        return m.value if m else None

    @property
    def temperature_c(self) -> float | None:
        m = self.get("temperature")
        return m.value if m else None

    def meq_table(self) -> dict[str, float]:
        table: dict[str, float] = {}
        for m in self.measurements:
            if not m.parameter.is_solute:
                continue
            meq = units.meq_per_l(m.mg_per_l, m.parameter)
            if meq is not None:
                table[m.key] = meq
        return table

    def charge_balance_pct(self) -> float:
        return units.charge_balance_error(self.meq_table())

    def missing_for_speciation(self) -> list[str]:
        from .parameters import REQUIRED_FOR_SPECIATION

        present = {m.key for m in self.measurements}
        # Alkalinity may arrive on either basis.
        if "alk_caco3" in present or "hco3" in present:
            present.add("alkalinity")
        return [k for k in REQUIRED_FOR_SPECIATION if k not in present]


class PhaseTarget(BaseModel):
    """An EQUILIBRIUM_PHASES entry: phase name, target SI, available moles."""

    name: str
    saturation_index: float = 0.0
    moles: float = Field(default=10.0, ge=0)


class ModelSpec(BaseModel):
    """Everything that determines the numeric result, besides the sample itself."""

    model_config = ConfigDict(frozen=True)

    database: str = "phreeqc.dat"
    title: str = "HydroGeoChem run"
    temperature_c: float | None = None
    pressure_atm: float = 1.0
    charge_balance_on: Literal["pH", "Cl", "Na", "none"] = "none"
    pe: float | None = None
    redox_couple: str | None = None
    equilibrium_phases: list[PhaseTarget] = Field(default_factory=list)
    saturation_phases: tuple[str, ...] = DEFAULT_PHASES
    censored_policy: Literal["half_dl", "zero", "drop"] = "half_dl"


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class RunRequest(BaseModel):
    """Either a sample to be rendered into PHREEQC input, or expert-authored raw input."""

    sample: WaterSample | None = None
    raw_input: str | None = None
    spec: ModelSpec = ModelSpec()
    project_id: UUID | None = None
    force: bool = Field(default=False, description="Bypass the idempotency cache")

    @model_validator(mode="after")
    def _exactly_one_source(self) -> RunRequest:
        if bool(self.sample) == bool(self.raw_input):
            raise ValueError("provide exactly one of 'sample' or 'raw_input'")
        return self


class SaturationIndex(BaseModel):
    phase: str
    si: float
    log_iap: float | None = None
    log_kt: float | None = None

    @property
    def state(self) -> str:
        if self.si > 0.1:
            return "supersaturated"
        if self.si < -0.1:
            return "undersaturated"
        return "at equilibrium"


class ModelResult(BaseModel):
    """The scientific payload. Reproducible only together with database + engine version."""

    ph: float | None = None
    pe: float | None = None
    temperature_c: float | None = None
    ionic_strength: float | None = None
    charge_balance_pct: float | None = None
    saturation_indices: list[SaturationIndex] = Field(default_factory=list)
    totals_mol_kgw: dict[str, float] = Field(default_factory=dict)
    selected_output: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def si(self, phase: str) -> float | None:
        return next((s.si for s in self.saturation_indices if s.phase == phase), None)


class ModelRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    status: RunStatus = RunStatus.queued
    input_hash: str
    input_text: str
    database: str
    engine_version: str | None = None
    database_sha256: str | None = None
    result: ModelResult | None = None
    error: str | None = None
    error_code: str | None = None
    duration_ms: int | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    site_id: str | None = None
    project_id: UUID | None = None


class BatchRunRequest(BaseModel):
    site_ids: Annotated[list[str], Field(min_length=1, max_length=250)]
    start_date: datetime
    end_date: datetime
    spec: ModelSpec = ModelSpec()
    aggregate: Literal["mean", "median", "latest", "none"] = "median"


def parameter_or_raise(token: str) -> Parameter:
    p = lookup(token)
    if p is None:
        raise ValueError(f"unrecognised parameter {token!r}")
    return p
