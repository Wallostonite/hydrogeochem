"""Dataset endpoints for user-uploaded CSVs.

A companion to the site-based `/sites/{id}/dataset`: instead of fetching a known site's
analyses, these accept a CSV the user filled in from the template, parse it into samples,
and run the same PHREEQC dataset build. The UI stays a pure HTTP client — all parsing and
modelling happen here.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ...domain.models import ModelSpec, WaterSample
from ...services.dataset import DEFAULT_DATASET_PHASES, bucket_samples, build_dataset
from ...services.ingest import build_template_csv, parse_samples_csv
from ..deps import ContainerDep
from ..security import SCOPE_SITES_READ, Principal, rate_limit_general

router = APIRouter(tags=["datasets"])


class ColumnMapOut(BaseModel):
    column: str
    key: str
    label: str
    unit: str


class IngestReportOut(BaseModel):
    recognized: list[ColumnMapOut]
    ignored: list[str]
    rows: int
    sites: int
    missing_required: list[str]


class CsvDatasetResponse(BaseModel):
    report: IngestReportOut
    rows: list[dict[str, Any]]


@router.get(
    "/datasets/template",
    response_class=PlainTextResponse,
    summary="Download a CSV template to fill in and upload",
)
async def dataset_template(
    principal: Annotated[Principal, Depends(rate_limit_general)],
) -> str:
    principal.require(SCOPE_SITES_READ)
    return build_template_csv()


@router.post(
    "/datasets/csv",
    response_model=CsvDatasetResponse,
    summary="Model an uploaded CSV of analyses into a flat ML dataset",
)
async def dataset_from_csv(
    container: ContainerDep,
    principal: Annotated[Principal, Depends(rate_limit_general)],
    file: UploadFile = File(...),
    database: str = Query(default="phreeqc.dat"),
    phases: Annotated[list[str] | None, Query()] = None,
    bucket: str = Query(default="event", pattern="^(event|month|quarter|year|window)$"),
    aggregate: str = Query(default="median", pattern="^(mean|median|latest)$"),
) -> CsvDatasetResponse:
    """One record per uploaded row (or per bucket): inputs joined with model outputs.

    `bucket=event` keeps every uploaded analysis as its own row; the period buckets
    aggregate a per-site time series the same way the site dataset does.
    """
    principal.require(SCOPE_SITES_READ)
    raw = await file.read()
    text = raw.decode("utf-8-sig", errors="replace")
    samples, report = parse_samples_csv(text)

    reps: list[WaterSample] = []
    by_site: dict[str, list[WaterSample]] = {}
    for sample in samples:
        by_site.setdefault(sample.site_id, []).append(sample)
    for group in by_site.values():
        reps.extend(bucket_samples(group, bucket, aggregate))

    spec = ModelSpec(
        database=database,
        saturation_phases=tuple(phases) if phases else DEFAULT_DATASET_PHASES,
    )
    rows = (
        await run_in_threadpool(build_dataset, reps, spec, container.engine) if reps else []
    )
    return CsvDatasetResponse(
        report=IngestReportOut(
            recognized=[
                ColumnMapOut(column=c.column, key=c.key, label=c.label, unit=c.unit)
                for c in report.recognized
            ],
            ignored=report.ignored,
            rows=report.rows,
            sites=report.sites,
            missing_required=report.missing_required,
        ),
        rows=rows,
    )
