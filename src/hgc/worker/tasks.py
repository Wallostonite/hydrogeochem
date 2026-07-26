"""Background tasks. Thin wrappers over the same services the API uses."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from uuid import UUID

from ..api.deps import Container, build_container
from ..domain.errors import HgcError
from ..domain.models import ModelSpec, RunRequest
from ..logging import get_logger, set_request_id
from .celery_app import celery_app

log = get_logger(__name__)

_container: Container | None = None


def container() -> Container:
    """One container per worker process, created lazily so forking stays cheap."""
    global _container
    if _container is None:
        _container = build_container()
        _container.engine.start()
    return _container


@celery_app.task(  # type: ignore[untyped-decorator]
    name="hgc.run.execute", bind=True, max_retries=2, default_retry_delay=10
)
def execute_run(self: Any, run_id: str, request_id: str | None = None) -> dict[str, Any]:
    set_request_id(request_id)
    ctx = container()
    try:
        run = ctx.runs.execute(UUID(run_id))
    except HgcError as exc:
        log.warning("task_run_failed", extra={"run_id": run_id, "code": exc.code})
        raise
    return {"run_id": str(run.id), "status": run.status.value}


@celery_app.task(name="hgc.batch.run", bind=True)  # type: ignore[untyped-decorator]
def run_batch(
    self: Any,
    site_ids: list[str],
    start: str,
    end: str,
    spec: dict[str, Any],
    aggregate: str = "median",
) -> dict[str, Any]:
    """One site's failure never fails the batch; every outcome is reported per site."""
    ctx = container()
    model_spec = ModelSpec(**spec)
    outcomes: list[dict[str, Any]] = []

    for index, site_id in enumerate(site_ids, start=1):
        self.update_state(
            state="PROGRESS", meta={"completed": index - 1, "total": len(site_ids)}
        )
        try:
            sample = asyncio.run(
                ctx.samples.representative_sample(
                    site_id, date.fromisoformat(start), date.fromisoformat(end), aggregate
                )
            )
            if sample is None:
                outcomes.append({"site_id": site_id, "status": "no_data"})
                continue
            run = ctx.runs.submit(RunRequest(sample=sample, spec=model_spec))
            outcomes.append(
                {
                    "site_id": site_id,
                    "status": run.status.value,
                    "run_id": str(run.id),
                    "calcite_si": run.result.si("Calcite") if run.result else None,
                }
            )
        except HgcError as exc:
            outcomes.append({"site_id": site_id, "status": "failed", "error": exc.code})
        except Exception as exc:
            log.exception("batch_item_error", extra={"site_id": site_id})
            outcomes.append({"site_id": site_id, "status": "error", "error": str(exc)[:200]})

    succeeded = sum(1 for o in outcomes if o["status"] == "succeeded")
    return {"total": len(site_ids), "succeeded": succeeded, "results": outcomes}


class CeleryQueue:
    """The TaskQueue port, backed by Celery. Injected into RunService in worker/API processes."""

    def enqueue_run(self, run_id: UUID) -> None:
        from ..logging import get_request_id

        execute_run.delay(str(run_id), get_request_id())
