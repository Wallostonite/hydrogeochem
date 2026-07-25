"""FastAPI application: middleware, error mapping, lifespan, routes."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..config import get_settings
from ..domain.errors import HgcError
from ..logging import configure_logging, get_logger, set_request_id
from ..metrics import HTTP_REQUEST_SECONDS
from .deps import build_container
from .routers import health, runs, samples, sites

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format, settings.service_name)

    container = build_container(settings)
    app.state.container = container

    # Fail fast: a node without its thermodynamic databases must never take traffic.
    try:
        checksums = container.engine.verify_databases()
        log.info("phreeqc_databases_verified", extra={"databases": sorted(checksums)})
        container.engine.start()
    except Exception as exc:  # noqa: BLE001
        log.error("phreeqc_unavailable", extra={"error": str(exc)})
        app.state.engine_ready = False
    else:
        app.state.engine_ready = True

    try:
        yield
    finally:
        await container.aclose()


app = FastAPI(
    title="HydroGeoChem Explorer API",
    version="1.0.0",
    summary="USGS water-quality retrieval and PHREEQC geochemical modelling",
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_settings.cors_origins),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def observability(request: Request, call_next):
    request_id = set_request_id(request.headers.get("x-request-id"))
    started = time.perf_counter()
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        status_code = 500
        raise
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        elapsed = time.perf_counter() - started
        HTTP_REQUEST_SECONDS.labels(request.method, path, str(status_code)).observe(elapsed)
        log.info(
            "http_request",
            extra={
                "method": request.method,
                "path": path,
                "status": status_code,
                "duration_ms": round(elapsed * 1000, 1),
            },
        )
    response.headers["x-request-id"] = request_id
    return response


@app.exception_handler(HgcError)
async def hgc_error_handler(request: Request, exc: HgcError) -> JSONResponse:
    problem = exc.to_problem()
    problem["instance"] = str(request.url.path)
    problem["code"] = exc.code
    return JSONResponse(
        status_code=exc.status, content=problem, media_type="application/problem+json"
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        media_type="application/problem+json",
        content={
            "type": "https://errors.hydrogeochem.dev/request_validation",
            "title": "request validation failed",
            "status": 422,
            "code": "request_validation",
            "errors": exc.errors(),
            "instance": str(request.url.path),
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", extra={"path": request.url.path})
    return JSONResponse(
        status_code=500,
        media_type="application/problem+json",
        content={
            "type": "https://errors.hydrogeochem.dev/internal_error",
            "title": "internal error",
            "status": 500,
            "code": "internal_error",
            "detail": "The request could not be completed. Reference the request id when reporting.",
        },
    )


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(health.router)
app.include_router(sites.router, prefix="/v1")
app.include_router(samples.router, prefix="/v1")
app.include_router(runs.router, prefix="/v1")
