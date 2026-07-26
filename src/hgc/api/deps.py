"""Composition root. Every dependency is built once and hung off app.state.

Constructing the engine, the HTTP client and the session factory per request is the
classic way to melt a service under load; do it once at startup and share.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import Depends, Request

from ..config import Settings, get_settings
from ..db.memory import InMemoryRunRepository
from ..services.cache import build_cache
from ..services.phreeqc import PhreeqcEngine
from ..services.runs import RunRepository, RunService, SampleService
from ..services.usgs import HttpConfig, UsgsClient

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from ..db.repository import SqlRunRepository


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: PhreeqcEngine
    usgs: UsgsClient
    runs: RunService
    samples: SampleService
    session_factory: sessionmaker[Session] | None = None  # None in test mode (no database)

    async def aclose(self) -> None:
        await self.usgs.aclose()
        self.engine.shutdown()


def build_container(settings: Settings | None = None, queue: Any = None) -> Container:
    settings = settings or get_settings()
    cache = build_cache(None if settings.testing else settings.redis_url)

    engine = PhreeqcEngine(
        database_dir=settings.phreeqc_database_dir,
        allowed_databases=settings.phreeqc_allowed_databases,
        workers=settings.phreeqc_workers,
        timeout_s=settings.phreeqc_timeout_s,
        max_tasks_per_child=settings.phreeqc_max_tasks_per_child,
        child_memory_mb=settings.phreeqc_child_memory_mb,
    )

    usgs = UsgsClient(
        HttpConfig(
            ogc_base=settings.wdfn_ogc_base_url,
            samples_base=settings.samples_base_url,
            api_key=settings.usgs_api_key,
            timeout_s=settings.http_timeout_s,
            max_retries=settings.http_max_retries,
            user_agent=settings.http_user_agent,
            ttl_sites_s=settings.cache_ttl_sites_s,
            ttl_results_s=settings.cache_ttl_results_s,
        ),
        cache=cache,
    )

    session_factory = None if settings.testing else _build_session_factory(settings)
    repository: RunRepository = (
        InMemoryRunRepository()
        if settings.testing or session_factory is None
        else _sql_repository(session_factory)
    )

    runs = RunService(
        engine=engine,
        repository=repository,
        max_input_bytes=settings.phreeqc_max_input_bytes,
        sync_deadline_s=settings.sync_run_deadline_s,
        queue=queue,
    )
    return Container(
        settings=settings,
        engine=engine,
        usgs=usgs,
        runs=runs,
        samples=SampleService(usgs),
        session_factory=session_factory,
    )


def _build_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Imported lazily so that a test or a UI process never needs SQLAlchemy installed."""
    from ..db.base import build_session_factory

    return build_session_factory(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


def _sql_repository(session_factory: sessionmaker[Session]) -> SqlRunRepository:
    from ..db.repository import SqlRunRepository

    return SqlRunRepository(session_factory)


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


ContainerDep = Annotated[Container, Depends(get_container)]
