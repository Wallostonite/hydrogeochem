"""Authentication, scopes, and rate limiting.

Custom PHREEQC input is a privileged capability, not a public one: it is an
expression language that runs on our CPUs. It sits behind the `runs:custom` scope.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, Request, status

from ..config import get_settings
from ..domain.errors import RateLimitedError

SCOPE_SITES_READ = "sites:read"
SCOPE_RUNS_WRITE = "runs:write"
SCOPE_RUNS_CUSTOM = "runs:custom"
SCOPE_ADMIN = "admin"


@dataclass(slots=True)
class Principal:
    subject: str
    scopes: frozenset[str] = field(default_factory=frozenset)

    def require(self, scope: str) -> None:
        if scope not in self.scopes and SCOPE_ADMIN not in self.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing scope {scope}",
            )


_ANONYMOUS = Principal(
    subject="anonymous",
    scopes=frozenset({SCOPE_SITES_READ, SCOPE_RUNS_WRITE, SCOPE_RUNS_CUSTOM}),
)


async def current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> Principal:
    settings = get_settings()

    # Local development and tests run open; every other environment requires a credential.
    if settings.env in ("local", "test") and not settings.api_keys:
        return _ANONYMOUS

    if x_api_key and x_api_key in settings.api_keys:
        return Principal(
            subject=f"key:{x_api_key[:6]}",
            scopes=frozenset({SCOPE_SITES_READ, SCOPE_RUNS_WRITE}),
        )

    if authorization and authorization.lower().startswith("bearer "):
        return _principal_from_token(authorization.split(" ", 1)[1])

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="provide a bearer token or X-API-Key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _principal_from_token(token: str) -> Principal:
    """Verify an OIDC access token.

    Replace with your provider's JWKS validation; the shape of the result is all the
    rest of the application depends on.
    """
    import jwt  # PyJWT, provided by the deployment image

    settings = get_settings()
    try:
        claims = jwt.decode(
            token, settings.jwt_secret.get_secret_value(), algorithms=["HS256"], audience="hgc"
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="invalid token") from exc
    return Principal(
        subject=str(claims.get("sub", "unknown")),
        scopes=frozenset(str(claims.get("scope", "")).split()),
    )


class SlidingWindowLimiter:
    """Per-principal limiter.

    In-process by default, which is correct for a single node and approximately correct
    behind a load balancer with N nodes (effective limit is N x the configured rate).
    Swap in the Redis implementation when the multiplier stops being acceptable.
    """

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        window = self._hits.setdefault(key, [])
        window[:] = [t for t in window if now - t < self._window]
        if len(window) >= self._limit:
            raise RateLimitedError(
                f"rate limit of {self._limit}/min exceeded", retry_after_s=int(self._window)
            )
        window.append(now)


_settings = get_settings()
general_limiter = SlidingWindowLimiter(_settings.rate_limit_per_minute)
run_limiter = SlidingWindowLimiter(_settings.rate_limit_runs_per_minute)


def rate_limit_general(principal: Principal = Depends(current_principal)) -> Principal:
    general_limiter.check(principal.subject)
    return principal


def rate_limit_runs(principal: Principal = Depends(current_principal)) -> Principal:
    run_limiter.check(principal.subject)
    return principal
