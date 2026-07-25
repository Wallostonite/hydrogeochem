from __future__ import annotations

from typing import Annotated
from uuid import UUID

from anyio import to_thread
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from ...domain.errors import NotFoundError
from ...domain.models import BatchRunRequest, ModelRun, RunRequest, RunStatus
from ...domain.parameters import DEFAULT_PHASES, PARAMETERS
from ..deps import ContainerDep
from ..security import (
    SCOPE_RUNS_CUSTOM,
    SCOPE_RUNS_WRITE,
    Principal,
    rate_limit_general,
    rate_limit_runs,
)

router = APIRouter(tags=["runs"])


class PreviewResponse(BaseModel):
    input_text: str
    notes: list[str]
    charge_balance_pct: float | None = None
    included_parameters: list[str]


@router.post("/runs/preview", response_model=PreviewResponse)
async def preview(
    request: RunRequest,
    container: ContainerDep,
    principal: Annotated[Principal, Depends(rate_limit_general)],
) -> PreviewResponse:
    """Render the PHREEQC input without executing it.

    Scientists check the input before trusting the output; hiding it would make the
    service a black box and the results uncitable.
    """
    principal.require(SCOPE_RUNS_WRITE)
    if request.raw_input:
        principal.require(SCOPE_RUNS_CUSTOM)
    built = container.runs.build(request)
    return PreviewResponse(
        input_text=built.text,
        notes=built.notes,
        charge_balance_pct=built.charge_balance_pct,
        included_parameters=built.included_keys,
    )


@router.post("/runs", response_model=ModelRun, status_code=status.HTTP_201_CREATED)
async def create_run(
    request: RunRequest,
    container: ContainerDep,
    response: Response,
    principal: Annotated[Principal, Depends(rate_limit_runs)],
) -> ModelRun:
    """Submit a model.

    Returns 200 when an identical run already exists, 201 when it ran inline, and 202
    with a Location header when it was queued. Clients should branch on `status`, not
    on the code, and poll `/v1/runs/{id}` while it is queued or running.
    """
    principal.require(SCOPE_RUNS_WRITE)
    if request.raw_input:
        principal.require(SCOPE_RUNS_CUSTOM)

    run = await to_thread.run_sync(container.runs.submit, request)

    if run.status is RunStatus.queued:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = f"/v1/runs/{run.id}"
    elif run.completed_at is not None and run.duration_ms is None:
        response.status_code = status.HTTP_200_OK
    return run


@router.get("/runs/{run_id}", response_model=ModelRun)
async def get_run(run_id: UUID, container: ContainerDep) -> ModelRun:
    run = container.runs.get(run_id)
    if run is None:
        raise NotFoundError(f"run {run_id} not found")
    return run


class BatchAccepted(BaseModel):
    batch_id: str
    site_count: int
    status_url: str


@router.post("/batches", response_model=BatchAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_batch(
    request: BatchRunRequest,
    container: ContainerDep,
    principal: Annotated[Principal, Depends(rate_limit_runs)],
) -> BatchAccepted:
    """Fan a model spec across many sites. Always asynchronous; per-site failures are isolated."""
    principal.require(SCOPE_RUNS_WRITE)
    from ...worker.tasks import run_batch

    task = run_batch.delay(
        site_ids=request.site_ids,
        start=request.start_date.date().isoformat(),
        end=request.end_date.date().isoformat(),
        spec=request.spec.model_dump(mode="json"),
        aggregate=request.aggregate,
    )
    return BatchAccepted(
        batch_id=task.id,
        site_count=len(request.site_ids),
        status_url=f"/v1/batches/{task.id}",
    )


@router.get("/batches/{batch_id}")
async def batch_status(batch_id: str) -> dict[str, object]:
    from ...worker.celery_app import celery_app

    result = celery_app.AsyncResult(batch_id)
    return {
        "batch_id": batch_id,
        "state": result.state,
        "result": result.result if result.successful() else None,
    }


@router.get("/catalog")
async def catalog(container: ContainerDep) -> dict[str, object]:
    """Everything a client needs to build a form: parameters, phases, databases."""
    return {
        "databases": sorted(container.settings.phreeqc_allowed_databases),
        "default_database": container.settings.phreeqc_default_database,
        "default_phases": list(DEFAULT_PHASES),
        "parameters": [
            {
                "key": p.key,
                "pcode": p.pcode,
                "label": p.label,
                "unit": p.default_unit,
                "phreeqc": p.phreeqc,
                "basis": p.basis,
                "role": p.role,
            }
            for p in PARAMETERS
        ],
    }
