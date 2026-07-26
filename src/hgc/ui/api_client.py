"""HTTP client for the UI.

The UI holds no chemistry and no database credentials. Everything it shows comes from
the same public API that notebooks and QGIS use, which keeps the two from drifting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests


class ApiError(RuntimeError):
    def __init__(self, message: str, code: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(slots=True)
class ApiClient:
    base_url: str = os.getenv("HGC_API_URL", "http://localhost:8000")
    api_key: str | None = os.getenv("HGC_API_KEY")
    timeout: float = 120.0  # live USGS Samples queries and multi-model dataset builds can run long

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = requests.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                headers=self._headers(),
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ApiError(f"Could not reach the API at {self.base_url}.") from exc

        if response.status_code >= 400:
            problem = _safe_json(response)
            raise ApiError(
                problem.get("detail") or f"Request failed ({response.status_code}).",
                code=str(problem.get("code", "error")),
                status=response.status_code,
            )
        return response.json()

    # -- endpoints ----------------------------------------------------------------

    def catalog(self) -> dict[str, Any]:
        return self._request("GET", "/v1/catalog")

    def search_sites(
        self,
        state: str | None = None,
        bbox: str | None = None,
        site_ids: str | None = None,
        limit: int | None = 200,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {k: v for k, v in
                  {"state": state, "bbox": bbox, "site_ids": site_ids}.items()
                  if v}
        # 0 = no limit; set explicitly so it survives serialisation
        params["limit"] = 0 if limit is None else limit
        return self._request("GET", "/v1/sites", params=params)

    def ready_sites(
        self,
        source: str = "wqp",
        provider: str | None = None,
        state: str | None = None,
        bbox: str | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = 200,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"source": source}
        if provider:
            params["provider"] = provider
        if state:
            params["state"] = state
        if bbox:
            params["bbox"] = bbox
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()
        params["limit"] = 0 if limit is None else limit
        return self._request("GET", "/v1/sites/ready", params=params)

    def samples(
        self, site_id: str, start: date, end: date, aggregate: str = "median"
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/sites/{site_id}/samples",
            params={"start": start.isoformat(), "end": end.isoformat(), "aggregate": aggregate},
        )

    def dataset(
        self,
        site_id: str,
        start: date,
        end: date,
        database: str = "phreeqc.dat",
        phases: list[str] | None = None,
        bucket: str = "year",
        aggregate: str = "median",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "start": start.isoformat(), "end": end.isoformat(),
            "database": database, "bucket": bucket, "aggregate": aggregate,
        }
        if phases:
            params["phases"] = phases
        return self._request("GET", f"/v1/sites/{site_id}/dataset", params=params)

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/runs/preview", json=payload)

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/runs", json=payload)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}")

    def create_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/batches", json=payload)

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/batches/{batch_id}")


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
