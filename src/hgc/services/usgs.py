"""Upstream adapters for USGS Water Data for the Nation (WDFN).

Two modern services, one port:
  * OGC monitoring-locations API  -> site search (GeoJSON)
  * USGS Samples Data API         -> discrete water-quality results (CSV)

These replace the retired NWISWeb / WaterServices site service and the Water Quality
Portal. Everything upstream-specific lives here: retry policy, timeouts, the WDFN column
names, and the mapping from characteristic names to our parameter registry. The rest of
the system sees only `SiteSummary`, `WaterSample`, and `ReadySite`.
"""

from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Protocol

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..domain.errors import UpstreamError, ValidationError
from ..domain.models import Measurement, ReadySite, SiteSummary, WaterSample
from ..domain.parameters import BY_KEY, REQUIRED_FOR_SPECIATION, lookup
from ..logging import get_logger
from .cache import Cache, cache_key

log = get_logger(__name__)

_RETRYABLE = (httpx.TimeoutException, httpx.TransportError, UpstreamError)

#: Samples API profile. `basicphyschem` carries every field we parse and returns ~10x
#: faster than `fullphyschem`, which drags in 80 extra columns we never use.
_SAMPLES_PROFILE = "basicphyschem"


class WaterDataSource(Protocol):
    """The port. Tests and offline deployments substitute a fixture-backed implementation."""

    async def search_sites(
        self,
        *,
        state: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        site_ids: list[str] | None = None,
        limit: int = 200,
    ) -> list[SiteSummary]: ...

    async def fetch_samples(
        self, *, site_id: str, start: date, end: date
    ) -> list[WaterSample]: ...


@dataclass(slots=True)
class HttpConfig:
    ogc_base: str      # WDFN OGC API, e.g. https://api.waterdata.usgs.gov/ogcapi/v0
    samples_base: str  # USGS Samples Data API, e.g. https://api.waterdata.usgs.gov/samples-data
    timeout_s: float = 30.0
    max_retries: int = 3
    user_agent: str = "hydrogeochem/1.0"
    ttl_sites_s: int = 86_400
    ttl_results_s: int = 21_600


class UsgsClient:
    """Adapter over the WDFN OGC + Samples APIs. Owns one pool; construct once per process."""

    def __init__(self, config: HttpConfig, cache: Cache, client: httpx.AsyncClient | None = None):
        self._cfg = config
        self._cache = cache
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_s, connect=5.0),
            headers={"User-Agent": config.user_agent, "Accept-Encoding": "gzip"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- transport ----------------------------------------------------------------

    async def _request(
        self, url: str, params: dict[str, Any], timeout: float | None
    ) -> httpx.Response:
        @retry(
            retry=retry_if_exception_type(_RETRYABLE),
            stop=stop_after_attempt(self._cfg.max_retries),
            wait=wait_exponential_jitter(initial=0.5, max=8.0),
            reraise=True,
        )
        async def _attempt() -> httpx.Response:
            response = await self._client.get(
                url, params=params, timeout=timeout or self._cfg.timeout_s
            )
            if response.status_code in (429, 500, 502, 503, 504):
                raise UpstreamError(
                    f"{url} returned {response.status_code}", status_code=response.status_code
                )
            if response.status_code >= 400 and response.status_code != 404:
                raise UpstreamError(
                    f"{url} rejected the request ({response.status_code})",
                    status_code=response.status_code,
                    body=response.text[:500],
                )
            return response

        try:
            return await _attempt()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"could not reach {url}: {exc}") from exc

    async def _get_text(self, url: str, params: dict[str, Any], timeout: float | None = None) -> str:
        response = await self._request(url, params, timeout)
        return "" if response.status_code == 404 else response.text

    async def _get_json(
        self, url: str, params: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        response = await self._request(url, params, timeout)
        return {} if response.status_code == 404 else response.json()

    # -- sites (OGC monitoring-locations) -----------------------------------------

    async def search_sites(
        self,
        *,
        state: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        site_ids: list[str] | None = None,
        limit: int = 200,
    ) -> list[SiteSummary]:
        if not any((state, bbox, site_ids)):
            raise ValidationError("provide one of: state, bbox, or site_ids")

        params: dict[str, Any] = {"f": "json", "limit": limit if limit else 20_000}
        if state:
            params["state_code"] = _state_fips(state)
        if bbox:
            params["bbox"] = _bbox_str(bbox)
        if site_ids:
            params["id"] = ",".join(s.strip() for s in site_ids[:200])

        key = cache_key("sites", params)
        cached = self._cache.get(key)
        if cached is not None:
            rows = cached[:limit] if limit else cached
            return [SiteSummary(**row) for row in rows]

        payload = await self._get_json(
            f"{self._cfg.ogc_base}/collections/monitoring-locations/items", params, timeout=45.0
        )
        sites = _parse_ogc_features(payload)
        self._cache.set(key, [s.model_dump() for s in sites], self._cfg.ttl_sites_s)
        log.info("sites_fetched", extra={"count": len(sites), "state": state})
        return sites[:limit] if limit else sites

    # -- ready sites (USGS Samples API) -------------------------------------------

    async def find_ready_sites(
        self,
        *,
        state: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        start: date | None = None,  # retained for API compatibility; not needed to find sites
        end: date | None = None,
        provider: str | None = None,  # retained for API compatibility; Samples API is USGS-only
        min_required: int | None = None,
        limit: int = 200,
    ) -> list[ReadySite]:
        """USGS sites that carry the analytes a speciation model needs.

        Uses the Samples ``locations`` endpoint (which returns the *site list* for a
        characteristic, not every measurement), one concurrent query per required analyte.
        A site is ready if it carries at least ``min_required`` of them. No date range is
        needed here — that only matters when you later pull a site's samples to model.
        """
        if not (state or bbox):
            raise ValidationError("provide a state or a bounding box")

        required = list(REQUIRED_FOR_SPECIATION)
        threshold = min_required or len(required)
        base: dict[str, Any] = {"mimeType": "text/csv"}
        if state:
            base["stateFips"] = f"US:{_state_fips(state)}"
        if bbox:
            base["boundingBox"] = _bbox_str(bbox)

        key = cache_key("ready", {**base, "threshold": threshold})
        cached = self._cache.get(key)
        if cached is None:
            per_key = await asyncio.gather(*[self._locations_for_key(base, k) for k in required])
            sites: dict[str, dict[str, Any]] = {}
            for k, locations in zip(required, per_key, strict=True):
                for loc in locations:
                    entry = sites.setdefault(loc["site_id"], {**loc, "keys": []})
                    if k not in entry["keys"]:
                        entry["keys"].append(k)
            cached = list(sites.values())
            self._cache.set(key, cached, self._cfg.ttl_sites_s)

        ready = [
            ReadySite(
                site_id=r["site_id"],
                name=r["name"] or r["site_id"],
                latitude=r.get("lat"),
                longitude=r.get("lon"),
                source="usgs",
                analytes=sorted(r["keys"]),
                missing=sorted(set(required) - set(r["keys"])),
            )
            for r in cached
            if len(set(r["keys"])) >= threshold
        ]
        ready.sort(key=lambda s: (-len(s.analytes), s.site_id))
        return ready[:limit] if limit else ready

    async def _locations_for_key(self, base: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """Sites (from the Samples locations endpoint) that report the analyte ``key``."""
        params = {**base, "characteristic": _characteristic_names_for(key)}
        text = await self._get_text(
            f"{self._cfg.samples_base}/locations/site", params, timeout=60.0
        )
        out: list[dict[str, Any]] = []
        for row in _iter_csv(text):
            site_id = (row.get("Location_Identifier") or "").strip()
            if not site_id:
                continue
            out.append(
                {
                    "site_id": site_id,
                    "name": (row.get("Location_Name") or "").strip(),
                    "lat": _float(
                        row.get("Location_LatitudeStandardized") or row.get("Location_Latitude")
                    ),
                    "lon": _float(
                        row.get("Location_LongitudeStandardized") or row.get("Location_Longitude")
                    ),
                }
            )
        return out

    # -- results (USGS Samples API) -----------------------------------------------

    async def fetch_samples(self, *, site_id: str, start: date, end: date) -> list[WaterSample]:
        if start > end:
            raise ValidationError("start date must not be after end date")
        # A bare station number ("06730200") is a USGS site; qualify it. A full identifier
        # ("USGS-06730200") is passed through. The Samples API serves USGS monitoring locations.
        qualified = f"USGS-{site_id}" if site_id.isdigit() else site_id
        params = {
            "monitoringLocationIdentifier": qualified,
            "activityStartDateLower": start.isoformat(),
            "activityStartDateUpper": end.isoformat(),
            "mimeType": "text/csv",
        }
        key = cache_key("results", params)
        cached = self._cache.get(key)
        if cached is not None:
            return [WaterSample(**row) for row in cached]

        text = await self._get_text(
            f"{self._cfg.samples_base}/results/{_SAMPLES_PROFILE}", params, timeout=90.0
        )
        samples = normalise_samples_rows(_iter_csv(text), site_id=qualified)
        self._cache.set(key, [s.model_dump(mode="json") for s in samples], self._cfg.ttl_results_s)
        log.info("samples_fetched", extra={"site_id": qualified, "samples": len(samples)})
        return samples


# ------------------------------------------------------------------ parsing helpers


def _characteristic_names_for(key: str) -> list[str]:
    """Every characteristic-name spelling (label + aliases) that resolves to one registry key."""
    param = BY_KEY[key]
    return list(dict.fromkeys([param.label, *param.aliases]))


def _parse_ogc_features(payload: dict[str, Any]) -> list[SiteSummary]:
    """WDFN OGC monitoring-locations GeoJSON -> SiteSummary."""
    sites: list[SiteSummary] = []
    for feature in payload.get("features", []):
        p = feature.get("properties") or {}
        site_id = (p.get("id") or "").strip()
        if not site_id:
            continue
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (None, None)
        sites.append(
            SiteSummary(
                site_id=site_id,
                name=p.get("monitoring_location_name") or site_id,
                latitude=_float(lat),
                longitude=_float(lon),
                agency=p.get("agency_code"),
                site_type=p.get("site_type_code"),
                huc=p.get("hydrologic_unit_code") or None,
                state=p.get("state_code") or None,
            )
        )
    return sites


def normalise_samples_rows(rows: Iterable[dict[str, str]], site_id: str) -> list[WaterSample]:
    """Group Samples-API result rows into samples and resolve each to a known parameter.

    Rows we cannot map are dropped deliberately: a characteristic with no registry entry has
    no PHREEQC master species and no molar mass, so carrying it forward would only create the
    illusion of completeness.
    """
    grouped: dict[str, dict[str, Any]] = {}
    unmapped: set[str] = set()

    for row in rows:
        characteristic = (row.get("Result_Characteristic") or "").strip()
        pcode = (row.get("USGSpcode") or "").strip()
        parameter = (lookup(pcode) if pcode else None) or lookup(characteristic)
        if parameter is None:
            if characteristic:
                unmapped.add(characteristic)
            continue

        detection = (row.get("Result_ResultDetectionCondition") or "").strip().casefold()
        censored = any(t in detection for t in ("not detected", "non-detect", "below"))
        raw_value = (row.get("Result_Measure") or "").strip()
        unit = (row.get("Result_MeasureUnit") or parameter.default_unit).strip()

        if censored and not raw_value:
            raw_value = (row.get("DetectionLimit_MeasureA") or "").strip()
            unit = (row.get("DetectionLimit_MeasureUnitA") or unit).strip()
        value = _float(raw_value)
        if value is None:
            continue

        sampled_at = _parse_datetime(row.get("Activity_StartDate"), row.get("Activity_StartTime"))
        loc = (row.get("Location_Identifier") or site_id).strip()
        bucket_key = f"{loc}|{sampled_at}"
        bucket = grouped.setdefault(
            bucket_key,
            {
                "site_id": loc,
                "sampled_at": sampled_at,
                "latitude": _float(
                    row.get("Location_LatitudeStandardized") or row.get("Location_Latitude")
                ),
                "longitude": _float(
                    row.get("Location_LongitudeStandardized") or row.get("Location_Longitude")
                ),
                "measurements": {},
            },
        )
        # Last value wins for duplicate analytes within one activity.
        bucket["measurements"][parameter.key] = Measurement(
            key=parameter.key,
            value=value,
            unit=unit or parameter.default_unit,
            censored=censored,
            method=(row.get("ResultAnalyticalMethod_Name") or None),
        )

    if unmapped:
        log.info("samples_characteristics_unmapped", extra={"characteristics": sorted(unmapped)[:20]})

    samples = [
        WaterSample(
            site_id=bucket["site_id"],
            sampled_at=bucket["sampled_at"],
            latitude=bucket["latitude"],
            longitude=bucket["longitude"],
            measurements=list(bucket["measurements"].values()),
        )
        for bucket in grouped.values()
    ]
    samples.sort(key=lambda s: (s.sampled_at is None, s.sampled_at), reverse=True)
    return samples


def aggregate_samples(samples: list[WaterSample], how: str = "median") -> WaterSample | None:
    """Collapse a time series into one representative analysis.

    Medians are the default: water-quality series are short, skewed, and occasionally contain
    order-of-magnitude outliers that a mean would happily propagate into an SI.
    """
    if not samples:
        return None
    if how == "latest":
        return samples[0]

    by_key: dict[str, list[Measurement]] = {}
    for sample in samples:
        for m in sample.measurements:
            by_key.setdefault(m.key, []).append(m)

    merged: list[Measurement] = []
    for key, group in by_key.items():
        values = sorted(m.mg_per_l if m.parameter.is_solute else m.value for m in group)
        if how == "mean":
            value = sum(values) / len(values)
        else:
            mid = len(values) // 2
            value = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        unit = "mg/l" if group[0].parameter.is_solute else group[0].unit
        merged.append(
            Measurement(key=key, value=value, unit=unit, censored=all(m.censored for m in group))
        )

    return WaterSample(
        site_id=samples[0].site_id,
        sampled_at=samples[0].sampled_at,
        latitude=samples[0].latitude,
        longitude=samples[0].longitude,
        measurements=merged,
    )


# ------------------------------------------------------------------ small helpers


#: USGS APIs filter by state FIPS. OGC uses the bare code (08); Samples uses US:08.
_STATE_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09",
    "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17",
    "IN": "18", "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
    "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30", "NE": "31",
    "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
    "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54",
    "WI": "55", "WY": "56",
}


def _state_fips(state: str) -> str:
    fips = _STATE_FIPS.get(state.strip().upper())
    if not fips:
        raise ValidationError(f"unknown state code {state!r}")
    return fips


def _bbox_str(bbox: tuple[float, float, float, float]) -> str:
    west, south, east, north = bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValidationError("bbox must be west,south,east,north within valid ranges")
    return ",".join(f"{v:.6f}" for v in bbox)


def _iter_csv(text: str) -> Iterable[dict[str, str]]:
    if not text.strip():
        return []
    return csv.DictReader(io.StringIO(text))


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_datetime(day: str | None, clock: str | None) -> datetime | None:
    if not day:
        return None
    try:
        parsed = datetime.strptime(day.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    if clock:
        try:
            hour, minute = clock.strip().split(":")[:2]
            parsed = parsed.replace(hour=int(hour), minute=int(minute))
        except (ValueError, IndexError):
            pass
    return parsed
