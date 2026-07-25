from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ...domain.errors import ValidationError
from ...domain.models import ReadySite, SiteSummary
from ..deps import ContainerDep
from ..security import SCOPE_SITES_READ, Principal, rate_limit_general

router = APIRouter(tags=["sites"])


@router.get("/sites", response_model=list[SiteSummary], summary="Search monitoring locations")
async def search_sites(
    container: ContainerDep,
    principal: Annotated[Principal, Depends(rate_limit_general)],
    state: str | None = Query(default=None, min_length=2, max_length=2, description="e.g. 'CO'"),
    bbox: str | None = Query(default=None, description="west,south,east,north"),
    site_ids: str | None = Query(default=None, description="Comma-separated site identifiers"),
    limit: int = Query(default=200, ge=0, description="Max sites to return; 0 means no limit"),
) -> list[SiteSummary]:
    principal.require(SCOPE_SITES_READ)
    parsed_bbox = None
    if bbox:
        parts = [float(p) for p in bbox.split(",")]
        parsed_bbox = (parts[0], parts[1], parts[2], parts[3])
    return await container.usgs.search_sites(
        state=state,
        bbox=parsed_bbox,
        site_ids=site_ids.split(",") if site_ids else None,
        limit=limit,
    )


@router.get(
    "/sites/ready",
    response_model=list[ReadySite],
    summary="Sites that carry the analytes a speciation model needs",
)
async def ready_sites(
    container: ContainerDep,
    principal: Annotated[Principal, Depends(rate_limit_general)],
    source: str = Query(default="wqp", pattern="^(wqp|synthetic)$"),
    provider: str | None = Query(default=None, description="Deprecated; ignored (Samples API is USGS-only)"),
    state: str | None = Query(default=None, min_length=2, max_length=2),
    bbox: str | None = Query(default=None, description="west,south,east,north"),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    min_required: int | None = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=0),
) -> list[ReadySite]:
    """Search a data source for sites with the required chemistry.

    ``source=wqp`` queries the Water Quality Portal (optionally narrowed by ``provider``);
    ``source=synthetic`` lists the sites held in the local database (e.g. the seeded demo).
    """
    principal.require(SCOPE_SITES_READ)

    if source == "synthetic":
        if container.session_factory is None:
            return []  # no database configured (e.g. test mode)
        from ...db.repository import synthetic_sites

        return synthetic_sites(container.session_factory, limit=limit)

    parsed_bbox = None
    if bbox:
        parts = [float(p) for p in bbox.split(",")]
        parsed_bbox = (parts[0], parts[1], parts[2], parts[3])
    if not (state or parsed_bbox):
        raise ValidationError("provide a state or a bounding box for a WQP search")
    return await container.usgs.find_ready_sites(
        start=start or (end or date.today()) - timedelta(days=365 * 6),
        end=end or date.today(),
        state=state,
        bbox=parsed_bbox,
        provider=provider or None,
        min_required=min_required,
        limit=limit,
    )
