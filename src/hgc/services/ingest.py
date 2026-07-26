"""Parse a user-uploaded CSV of water analyses into WaterSamples.

An uploaded CSV is a fourth source of samples, alongside USGS, the local database, and
custom PHREEQC input. Everything downstream (bucketing, PHREEQC, the flat ML dataset) is
shared; only the header->parameter mapping and unit handling live here.

The intended flow is template-driven: a user downloads `build_template_csv()`, fills in
their analyses, and uploads it. Headers carry their units in parentheses so the file is
self-documenting (`ca (mg/L)`), but a bare canonical key, a pcode, or a WQP label also
resolves, because `lookup()` already understands all three.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime

from ..domain.models import Measurement, WaterSample
from ..domain.parameters import REQUIRED_FOR_SPECIATION, lookup

#: Columns offered in the downloadable template: identity + the speciation essentials plus
#: a few common extras. Units live in the header so the file explains itself.
TEMPLATE_HEADER: list[str] = [
    "site_id", "date", "latitude", "longitude",
    "ph", "temperature (degC)",
    "ca (mg/L)", "mg (mg/L)", "na (mg/L)", "k (mg/L)",
    "cl (mg/L)", "so4 (mg/L)", "alkalinity (mg/L as CaCO3)",
    "sio2 (mg/L)", "fe (ug/L)", "mn (ug/L)",
]
_TEMPLATE_EXAMPLE: list[str] = [
    "WELL-1", "2024-03-01", "39.05", "-107.35",
    "7.4", "12.5",
    "88", "12", "25", "2.1",
    "30", "45", "210",
    "9.0", "40", "5",
]


def build_template_csv() -> str:
    """A header row plus one worked example row, ready to download and fill in."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADER)
    writer.writerow(_TEMPLATE_EXAMPLE)
    return buf.getvalue()


@dataclass(slots=True)
class ColumnMap:
    column: str
    key: str
    label: str
    unit: str


@dataclass(slots=True)
class IngestReport:
    """What the parser made of the file, so a client can show it before modelling."""

    recognized: list[ColumnMap] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    rows: int = 0
    sites: int = 0
    missing_required: list[str] = field(default_factory=list)


def _split_header(header: str) -> tuple[str, str | None]:
    """`'ca (mg/L)'` -> `('ca', 'mg/L')`; `'ph'` -> `('ph', None)`."""
    name = header.strip()
    unit: str | None = None
    if "(" in name and name.endswith(")"):
        name, _, rest = name.partition("(")
        unit = rest[:-1].strip() or None
        name = name.strip()
    return name, unit


def _meta_role(header: str) -> str | None:
    """Map an identity/metadata column to its role, or None if it is an analyte."""
    h = header.strip().casefold()
    if h in {"site_id", "site", "station", "station_id"}:
        return "site_id"
    if h in {"date", "sampled_at", "datetime", "sample_date"}:
        return "date"
    if h in {"latitude", "lat"}:
        return "latitude"
    if h in {"longitude", "lon", "long", "lng"}:
        return "longitude"
    return None


def _parse_value(raw: str) -> tuple[float, bool] | None:
    """`'0.5'` -> `(0.5, False)`; `'<0.01'` -> `(0.01, True)`; blank/text -> None."""
    s = raw.strip()
    if not s:
        return None
    censored = False
    if s[0] in "<":
        censored = True
        s = s[1:].strip()
    try:
        return float(s), censored
    except ValueError:
        return None


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _parse_date(raw: str) -> datetime | None:
    s = raw.strip()
    for fmt in ("iso", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.fromisoformat(s) if fmt == "iso" else datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_samples_csv(text: str) -> tuple[list[WaterSample], IngestReport]:
    """Turn CSV text into WaterSamples plus a report of what was recognised.

    One row becomes one sample. A row with no recognised numeric analyte is skipped. Units
    come from the header (`ca (mg/L)`) when present, otherwise the parameter's default.
    """
    reader = csv.DictReader(io.StringIO(text))
    report = IngestReport()
    if not reader.fieldnames:
        return [], report

    # Classify every column once, up front.
    analyte_cols: dict[str, tuple[str, str]] = {}  # column -> (parameter key, unit)
    meta_cols: dict[str, str] = {}                 # column -> metadata role
    for col in reader.fieldnames:
        role = _meta_role(col)
        if role:
            meta_cols[col] = role
            continue
        name, unit = _split_header(col)
        param = lookup(name)
        if param is None:
            report.ignored.append(col)
            continue
        resolved_unit = unit or param.default_unit
        analyte_cols[col] = (param.key, resolved_unit)
        report.recognized.append(
            ColumnMap(column=col, key=param.key, label=param.label, unit=resolved_unit)
        )

    samples: list[WaterSample] = []
    seen_sites: set[str] = set()
    for row in reader:
        measurements: list[Measurement] = []
        for col, (key, unit) in analyte_cols.items():
            parsed = _parse_value(row.get(col) or "")
            if parsed is None:
                continue
            value, censored = parsed
            measurements.append(Measurement(key=key, value=value, unit=unit, censored=censored))
        if not measurements:
            continue

        site_id = "upload"
        sampled_at: datetime | None = None
        latitude: float | None = None
        longitude: float | None = None
        for col, role in meta_cols.items():
            raw = (row.get(col) or "").strip()
            if not raw:
                continue
            if role == "site_id":
                site_id = raw
            elif role == "date":
                sampled_at = _parse_date(raw)
            elif role == "latitude":
                latitude = _to_float(raw)
            elif role == "longitude":
                longitude = _to_float(raw)

        samples.append(
            WaterSample(
                site_id=site_id,
                sampled_at=sampled_at,
                latitude=latitude,
                longitude=longitude,
                source="upload",
                measurements=measurements,
            )
        )
        seen_sites.add(site_id)

    report.rows = len(samples)
    report.sites = len(seen_sites)
    present = {m.key for s in samples for m in s.measurements}
    report.missing_required = [k for k in REQUIRED_FOR_SPECIATION if k not in present]
    return samples, report
