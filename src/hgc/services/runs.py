"""Run orchestration: build, deduplicate, execute, persist.

The interesting decisions here are (a) idempotency by input hash, which makes retries
free and identical models across users instant, and (b) the sync/async split, which keeps
trivial speciation interactive without forcing every client to poll.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID

from ..domain.errors import HgcError, PhreeqcError, PhreeqcTimeoutError
from ..domain.models import (
    ModelResult,
    ModelRun,
    ModelSpec,
    RunRequest,
    RunStatus,
    WaterSample,
)
from ..logging import get_logger
from ..metrics import PHREEQC_RUN_SECONDS, RUNS_TOTAL
from .phreeqc import PhreeqcEngine, build_custom_input, build_solution_input, parse_selected_output
from .phreeqc.input_builder import BuiltInput
from .phreeqc.sanitizer import sanitize_input
from .usgs import UsgsClient, aggregate_samples

log = get_logger(__name__)


class RunRepository(Protocol):
    """Persistence port. The service never writes SQL."""

    def get_by_hash(self, input_hash: str) -> ModelRun | None: ...
    def get(self, run_id: UUID) -> ModelRun | None: ...
    def create(self, run: ModelRun) -> ModelRun: ...
    def update(self, run: ModelRun) -> ModelRun: ...


class TaskQueue(Protocol):
    def enqueue_run(self, run_id: UUID) -> None: ...


def compute_input_hash(input_text: str, database: str, engine_version: str, db_sha: str) -> str:
    """Identity of a result = input + thermodynamic database + engine build.

    Leaving any of the three out would let a database upgrade silently serve stale science.
    """
    digest = hashlib.sha256()
    for part in (input_text, database, engine_version, db_sha):
        digest.update(part.encode())
        digest.update(b"\x1f")
    return digest.hexdigest()


class RunService:
    def __init__(
        self,
        engine: PhreeqcEngine,
        repository: RunRepository,
        max_input_bytes: int = 64_000,
        sync_deadline_s: float = 5.0,
        queue: TaskQueue | None = None,
    ) -> None:
        self._engine = engine
        self._repo = repository
        self._max_input_bytes = max_input_bytes
        self._sync_deadline_s = sync_deadline_s
        self._queue = queue

    # -- building ------------------------------------------------------------------

    def build(self, request: RunRequest) -> BuiltInput:
        if request.raw_input is not None:
            sanitize_input(request.raw_input, max_bytes=self._max_input_bytes)
            built = build_custom_input(request.raw_input, request.spec)
        else:
            assert request.sample is not None
            built = build_solution_input(request.sample, request.spec)
        sanitize_input(built.text, max_bytes=self._max_input_bytes)
        return built

    # -- executing -----------------------------------------------------------------

    def submit(self, request: RunRequest) -> ModelRun:
        """Return a run that is either already complete or queued; never blocks past deadline."""
        built = self.build(request)
        database = request.spec.database
        db_sha = self._engine.database_checksum(database)
        input_hash = compute_input_hash(
            built.text, database, self._engine.engine_version, db_sha
        )

        if not request.force:
            existing = self._repo.get_by_hash(input_hash)
            live = (RunStatus.succeeded, RunStatus.queued, RunStatus.running)
            if existing and existing.status in live:
                log.info("run_deduplicated", extra={"run_id": str(existing.id)})
                return existing

        run = ModelRun(
            status=RunStatus.queued,
            input_hash=input_hash,
            input_text=built.text,
            database=database,
            database_sha256=db_sha,
            engine_version=self._engine.engine_version,
            site_id=request.sample.site_id if request.sample else None,
            project_id=request.project_id,
            created_at=datetime.now(UTC),
        )
        run = self._repo.create(run)

        if self._queue is not None and not self._is_fast(built):
            self._queue.enqueue_run(run.id)
            RUNS_TOTAL.labels(status="queued", mode="async").inc()
            return run

        return self.execute(run.id, notes=built.notes, charge_balance=built.charge_balance_pct)

    def execute(
        self,
        run_id: UUID,
        notes: list[str] | None = None,
        charge_balance: float | None = None,
    ) -> ModelRun:
        """Run to completion and persist the terminal state. Safe to call from a worker."""
        run = self._repo.get(run_id)
        if run is None:
            raise HgcError(f"run {run_id} disappeared before execution")

        run.status = RunStatus.running
        self._repo.update(run)
        mode = "async" if self._queue is not None else "sync"

        try:
            raw = self._engine.run(run.input_text, run.database)
        except (PhreeqcError, PhreeqcTimeoutError) as exc:
            run.status = RunStatus.failed
            run.error = exc.message
            run.error_code = exc.code
            run.completed_at = datetime.now(UTC)
            PHREEQC_RUN_SECONDS.labels(database=run.database, outcome="error").observe(0)
            RUNS_TOTAL.labels(status="failed", mode=mode).inc()
            log.warning("run_failed", extra={"run_id": str(run.id), "code": exc.code})
            return self._repo.update(run)

        result: ModelResult = parse_selected_output(raw.selected_output, raw.warnings)
        result.warnings.extend(notes or [])
        if result.charge_balance_pct is None:
            result.charge_balance_pct = charge_balance

        run.result = result
        run.status = RunStatus.succeeded
        run.duration_ms = raw.duration_ms
        run.engine_version = raw.engine_version
        run.database_sha256 = raw.database_sha256
        run.completed_at = datetime.now(UTC)
        PHREEQC_RUN_SECONDS.labels(database=run.database, outcome="ok").observe(
            raw.duration_ms / 1000
        )
        RUNS_TOTAL.labels(status="succeeded", mode=mode).inc()
        log.info("run_succeeded", extra={"run_id": str(run.id), "duration_ms": raw.duration_ms})
        return self._repo.update(run)

    def get(self, run_id: UUID) -> ModelRun | None:
        return self._repo.get(run_id)

    # -- heuristics ----------------------------------------------------------------

    def _is_fast(self, built: BuiltInput) -> bool:
        """Speciation and equilibrium phases finish in milliseconds; reaction paths do not.

        The heuristic is deliberately conservative: anything containing a keyword that can
        iterate (transport, kinetics, titration sweeps) goes to the queue regardless of size.
        """
        upper = built.text.upper()
        slow_keywords = ("TRANSPORT", "KINETICS", "ADVECTION", "INVERSE_MODELING", "RATES")
        if any(keyword in upper for keyword in slow_keywords):
            return False
        steps = upper.count("REACTION") + upper.count("USE SOLUTION")
        return steps <= 2 and len(built.text) < 8_000


class SampleService:
    """Fetch-and-normalise on top of the upstream client, with model-readiness checks."""

    def __init__(self, client: UsgsClient) -> None:
        self._client = client

    async def samples_for_site(
        self, site_id: str, start: date, end: date
    ) -> list[WaterSample]:
        return await self._client.fetch_samples(site_id=site_id, start=start, end=end)

    async def representative_sample(
        self, site_id: str, start: date, end: date, how: str = "median"
    ) -> WaterSample | None:
        samples = await self.samples_for_site(site_id, start, end)
        return aggregate_samples(samples, how)

    @staticmethod
    def readiness(sample: WaterSample, spec: ModelSpec | None = None) -> dict[str, object]:
        """What a user needs to know before trusting a model built from this analysis."""
        missing = sample.missing_for_speciation()
        cbe = sample.charge_balance_pct()
        return {
            "missing_parameters": missing,
            "charge_balance_pct": round(cbe, 2),
            "usable": not missing and abs(cbe) <= 10,
            "measurement_count": len(sample.measurements),
        }
