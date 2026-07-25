from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ...domain.models import ModelSpec, WaterSample
from ...services.dataset import DEFAULT_DATASET_PHASES, bucket_samples, build_dataset
from ...services.runs import SampleService
from ..deps import ContainerDep
from ..security import SCOPE_SITES_READ, Principal, rate_limit_general

router = APIRouter(tags=["samples"])


class SampleCollection(BaseModel):
    site_id: str
    count: int
    samples: list[WaterSample]
    representative: WaterSample | None = None
    readiness: dict[str, Any] | None = None


@router.get("/sites/{site_id}/samples", response_model=SampleCollection)
async def site_samples(
    site_id: str,
    container: ContainerDep,
    principal: Annotated[Principal, Depends(rate_limit_general)],
    start: date = Query(...),
    end: date = Query(...),
    aggregate: str = Query(default="median", pattern="^(mean|median|latest|none)$"),
) -> SampleCollection:
    """Normalised analyses for a site, plus the representative sample a model would use.

    `readiness` is returned alongside so a client can warn before a model is run, rather
    than presenting a confident saturation index derived from an incomplete analysis.
    """
    principal.require(SCOPE_SITES_READ)
    samples = await container.samples.samples_for_site(site_id, start, end)

    representative = None
    readiness = None
    if samples and aggregate != "none":
        from ...services.usgs import aggregate_samples

        representative = aggregate_samples(samples, aggregate)
        if representative:
            readiness = SampleService.readiness(representative)

    return SampleCollection(
        site_id=site_id,
        count=len(samples),
        samples=samples,
        representative=representative,
        readiness=readiness,
    )


@router.get("/sites/{site_id}/dataset", summary="Flat input+output dataset for ML")
async def site_dataset(
    site_id: str,
    container: ContainerDep,
    principal: Annotated[Principal, Depends(rate_limit_general)],
    start: date = Query(...),
    end: date = Query(...),
    database: str = Query(default="phreeqc.dat"),
    phases: Annotated[list[str] | None, Query()] = None,
    bucket: str = Query(default="event", pattern="^(event|month|quarter|year|window)$"),
    aggregate: str = Query(default="median", pattern="^(mean|median|latest)$"),
) -> list[dict[str, Any]]:
    """One record per sample: inputs joined with the outputs of modelling it.

    `bucket` controls the time resolution: `event` keeps every sampling event (real dates,
    but individual WQP samples are often sparse); `month`/`quarter`/`year` aggregate each
    period into one complete analysis; `window` collapses the whole range to a single row.
    """
    principal.require(SCOPE_SITES_READ)
    samples = await container.samples.samples_for_site(site_id, start, end)
    if not samples:
        return []

    reps: list[WaterSample] = bucket_samples(samples, bucket, aggregate)

    spec = ModelSpec(
        database=database,
        saturation_phases=tuple(phases) if phases else DEFAULT_DATASET_PHASES,
    )
    # Engine calls are blocking; run them off the event loop.
    return await run_in_threadpool(build_dataset, reps, spec, container.engine)
