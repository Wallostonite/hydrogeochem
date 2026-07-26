"""Typed errors. Transport layers map these to problem documents; nothing else raises bare."""

from __future__ import annotations


class HgcError(Exception):
    """Base class. `code` is a stable, machine-readable identifier."""

    code = "internal_error"
    status = 500

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_problem(self) -> dict[str, object]:
        return {
            "type": f"https://errors.hydrogeochem.dev/{self.code}",
            "title": self.code.replace("_", " "),
            "status": self.status,
            "detail": self.message,
            **({"errors": self.details} if self.details else {}),
        }


class ValidationError(HgcError):
    code = "validation_error"
    status = 422


class UnsafeInputError(ValidationError):
    """PHREEQC input contained a keyword that touches the filesystem or the database path."""

    code = "unsafe_phreeqc_input"


class NotFoundError(HgcError):
    code = "not_found"
    status = 404


class UpstreamError(HgcError):
    """USGS / WQP returned an error or failed to respond within budget."""

    code = "upstream_unavailable"
    status = 503


class PhreeqcError(HgcError):
    """PHREEQC ran but reported an error (convergence, unknown phase, missing element)."""

    code = "phreeqc_error"
    status = 422


class PhreeqcTimeoutError(HgcError):
    code = "phreeqc_timeout"
    status = 504


class EngineUnavailableError(HgcError):
    code = "engine_unavailable"
    status = 503


class RateLimitedError(HgcError):
    code = "rate_limited"
    status = 429
