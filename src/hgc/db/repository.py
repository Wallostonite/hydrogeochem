"""Repositories translating between domain models and rows.

An in-memory implementation ships alongside the SQL one so that the API, the run service,
and the whole test suite can run without a database.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..domain.models import ModelResult, ModelRun, ReadySite, RunStatus
from ..domain.parameters import REQUIRED_FOR_SPECIATION
from .models import ModelRunRow, SampleRow


def _to_domain(row: ModelRunRow) -> ModelRun:
    return ModelRun(
        id=row.id,
        status=RunStatus(row.status),
        input_hash=row.input_hash,
        input_text=row.input_text,
        database=row.database,
        database_sha256=row.database_sha256,
        engine_version=row.engine_version,
        result=ModelResult(**row.result) if row.result else None,
        error=row.error,
        error_code=row.error_code,
        duration_ms=row.duration_ms,
        site_id=row.site_id,
        project_id=row.project_id,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


def _apply(row: ModelRunRow, run: ModelRun) -> ModelRunRow:
    row.status = run.status.value
    row.result = run.result.model_dump(mode="json") if run.result else None
    row.error = run.error
    row.error_code = run.error_code
    row.duration_ms = run.duration_ms
    row.engine_version = run.engine_version
    row.database_sha256 = run.database_sha256
    row.completed_at = run.completed_at
    return row


class SqlRunRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def get(self, run_id: UUID) -> ModelRun | None:
        with self._factory() as session:
            row = session.get(ModelRunRow, run_id)
            return _to_domain(row) if row else None

    def get_by_hash(self, input_hash: str) -> ModelRun | None:
        with self._factory() as session:
            stmt = (
                select(ModelRunRow)
                .where(ModelRunRow.input_hash == input_hash)
                .order_by(ModelRunRow.created_at.desc())
                .limit(1)
            )
            row = session.execute(stmt).scalar_one_or_none()
            return _to_domain(row) if row else None

    def create(self, run: ModelRun) -> ModelRun:
        with self._factory() as session:
            row = ModelRunRow(
                id=run.id,
                input_hash=run.input_hash,
                status=run.status.value,
                database=run.database,
                database_sha256=run.database_sha256,
                engine_version=run.engine_version,
                input_text=run.input_text,
                site_id=run.site_id,
                project_id=run.project_id,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _to_domain(row)

    def update(self, run: ModelRun) -> ModelRun:
        with self._factory() as session:
            row = session.get(ModelRunRow, run.id)
            if row is None:
                raise LookupError(f"run {run.id} not found")
            _apply(row, run)
            session.commit()
            session.refresh(row)
            return _to_domain(row)

    def recent(self, limit: int = 50) -> list[ModelRun]:
        with self._factory() as session:
            stmt = select(ModelRunRow).order_by(ModelRunRow.created_at.desc()).limit(limit)
            return [_to_domain(row) for row in session.execute(stmt).scalars()]


def synthetic_sites(session_factory: sessionmaker[Session], limit: int = 500) -> list[ReadySite]:
    """Distinct sites held in the local `sample` table (e.g. the seeded demo data).

    One row per site: its coordinates, the required analytes present across its samples,
    and how many are still missing — the same shape a WQP search returns.
    """
    required = set(REQUIRED_FOR_SPECIATION)
    with session_factory() as session:
        rows = session.execute(select(SampleRow)).scalars()
        by_site: dict[str, ReadySite] = {}
        present: dict[str, set[str]] = {}
        for row in rows:
            keys = {m.get("key") for m in (row.measurements or [])}
            present.setdefault(row.site_id, set()).update(k for k in keys if k in required)
            if row.site_id not in by_site:
                by_site[row.site_id] = ReadySite(
                    site_id=row.site_id,
                    name=row.site_id,
                    latitude=row.latitude,
                    longitude=row.longitude,
                    source="synthetic",
                )
    for site_id, site in by_site.items():
        site.analytes = sorted(present.get(site_id, set()))
        site.missing = sorted(required - present.get(site_id, set()))
    ordered = sorted(by_site.values(), key=lambda s: (-len(s.analytes), s.site_id))
    return ordered[:limit] if limit else ordered
