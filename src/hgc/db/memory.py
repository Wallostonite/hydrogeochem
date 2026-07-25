"""In-memory repository.

Deliberately free of SQLAlchemy imports so the API, the run service and the whole test
suite can run with no database at all. Used by `HGC_ENV=test` and by local runs.
"""

from __future__ import annotations

from uuid import UUID

from ..domain.models import ModelRun


class InMemoryRunRepository:
    """Used by tests and by `HGC_ENV=local` runs without Postgres."""

    def __init__(self) -> None:
        self._runs: dict[UUID, ModelRun] = {}

    def get(self, run_id: UUID) -> ModelRun | None:
        return self._runs.get(run_id)

    def get_by_hash(self, input_hash: str) -> ModelRun | None:
        matches = [r for r in self._runs.values() if r.input_hash == input_hash]
        return matches[-1] if matches else None

    def create(self, run: ModelRun) -> ModelRun:
        self._runs[run.id] = run
        return run

    def update(self, run: ModelRun) -> ModelRun:
        self._runs[run.id] = run
        return run

    def recent(self, limit: int = 50) -> list[ModelRun]:
        return list(self._runs.values())[-limit:][::-1]
