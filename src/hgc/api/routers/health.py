"""Liveness and readiness.

/healthz answers "is the process alive". /readyz answers "should this pod receive
traffic" and therefore checks the things a request actually needs.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from ... import __version__
from ..deps import ContainerDep

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/readyz", include_in_schema=False)
async def readyz(
    request: Request, container: ContainerDep, response: Response
) -> dict[str, object]:
    checks: dict[str, object] = {"version": __version__}

    try:
        checks["phreeqc_databases"] = sorted(container.engine.verify_databases())
        checks["phreeqc"] = "ok"
    except Exception as exc:
        checks["phreeqc"] = f"unavailable: {exc}"

    if checks.get("phreeqc") != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        checks["status"] = "degraded"
    else:
        checks["status"] = "ok"
    return checks
