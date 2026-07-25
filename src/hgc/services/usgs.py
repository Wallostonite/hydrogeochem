"""Upstream adapters for USGS data.

Two services, one port:
  * NWIS site service  -> monitoring locations (RDB)
  * Water Quality Portal -> analytical results (CSV)

Everything upstream-specific lives here: retry policy, timeouts, pagination, the
column names, and the mapping from WQP characteristic names to our parameter registry.
The rest of the system sees only `SiteSummary` and `WaterSample`.
"""

from __future__ import annotations

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
    nwis_base: str
    wqp_base: str
    timeout_s: float = 30.0
    max_retries: int = 3
    user_agent: str = "hydrogeochem/1.0"
    ttl_sites_s: int = 86_400
    ttl_results_s: int = 21_600


class UsgsClient:
    """Adapter over NWIS + WQP. Owns one connection pool; construct once per process."""

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

    async def _get_text(
        self, url: str, params: dict[str, Any], timeout: float | None = None
    ) -> str:
        @retry(
            retry=retry_if_exception_type(_RETRYABLE),
            stop=stop_after_attempt(self._cfg.max_retries),
            wait=wait_exponential_jitter(initial=0.5, max=8.0),
            reraise=True,
        )
        async def _attempt() -> str:
            response = await self._client.get(
                url, params=params, timeout=timeout or self._cfg.timeout_s
            )
            if response.status_code in (429, 500, 502, 503, 504):
                raise UpstreamError(
                    f"{url} returned {response.status_code}",
                    status_code=response.status_code,
                )
            if response.status_code == 404:
                return ""
            if response.status_code >= 400:
                raise UpstreamError(
                    f"{url} rejected the request ({response.status_code})",
                    status_code=response.status_code,
                    body=response.text[:500],
                )
            return response.text

        try:
            return await _attempt()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"could not reach {url}: {exc}") from exc

    # -- sites --------------------------------------------------------------------

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

        params: dict[str, Any] = {"format": "rdb", "siteOutput": "expanded", "siteStatus": "all"}
        if state:
            params["stateCd"] = state.lower()
        if bbox:
            west, south, east, north = bbox
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValidationError("bbox must be west,south,east,north within valid ranges")
            params["bBox"] = ",".join(f"{v:.6f}" for v in (west, south, east, north))
        if site_ids:
            params["sites"] = ",".join(s.removeprefix("USGS-") for s in site_ids[:100])

        key = cache_key("sites", params)
        cached = self._cache.get(key)
        if cached is not None:
            # Slice the raw rows before rebuilding models: a cached statewide search can hold
            # tens of thousands of sites, and building all of them just to keep `limit` is the
            # bulk of a cache-hit's latency.
            rows = cached[:limit] if limit else cached  # limit == 0 means no cap
            return [SiteSummary(**row) for row in rows]

        text = await self._get_text(f"{self._cfg.nwis_base}/site/", params)
        sites = _parse_rdb_sites(text)
        self._cache.set(key, [s.model_dump() for s in sites], self._cfg.ttl_sites_s)
        log.info("sites_fetched", extra={"count": len(sites), "state": state})
        return sites[:limit] if limit else sites  # limit == 0 means no cap

    # -- ready sites (Water Quality Portal) ---------------------------------------

    async def find_ready_sites(
        self,
        *,
        start: date,
        end: date,
        state: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        provider: str | None = None,
        min_required: int | None = None,
        limit: int = 200,
    ) -> list[ReadySite]:
        """Sites in the WQP that actually carry the analytes a speciation model needs.

        Unlike ``search_sites`` (the NWIS site catalogue, which lists every location), this
        asks the Water Quality Portal for the required characteristics, groups the results by
        station, and keeps those meeting ``min_required``. ``provider`` narrows to one WQP
        source (e.g. NWIS, STORET, NGWMN); omit it for all sources.
        """
        if start > end:
            raise ValidationError("start date must not be after end date")
        if not (state or bbox):
            raise ValidationError("provide a state or a bounding box")

        required = set(REQUIRED_FOR_SPECIATION)
        threshold = min_required or len(required)
        params: dict[str, Any] = {
            "mimeType": "csv",
            "zip": "no",
            "dataProfile": "resultPhysChem",
            "startDateLo": start.strftime("%m-%d-%Y"),
            "startDateHi": end.strftime("%m-%d-%Y"),
            "characteristicName": _required_characteristic_names(),
        }
        if provider:
            params["providers"] = provider
        if state:
            fips = _STATE_FIPS.get(state.strip().upper())
            if not fips:
                raise ValidationError(f"unknown state code {state!r}")
            params["statecode"] = f"US:{fips}"
        if bbox:
            west, south, east, north = bbox
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValidationError("bbox must be west,south,east,north within valid ranges")
            params["bBox"] = ",".join(f"{v:.6f}" for v in bbox)

        key = cache_key("ready", params)
        cached = self._cache.get(key)
        if cached is None:
            # A whole-state pull is a large CSV; give it far more room than a normal request.
            text = await self._get_text(
                f"{self._cfg.wqp_base}/Result/search", params, timeout=55.0
            )
            cached = _group_ready_sites(_iter_csv(text))
            self._cache.set(key, cached, self._cfg.ttl_results_s)

        ready = [
            ReadySite(
                site_id=row["site_id"],
                name=row["name"] or row["site_id"],
                analytes=row["analytes"],
                missing=sorted(required - set(row["keys"])),
            )
            for row in cached
            if len(set(row["keys"]) & required) >= threshold
        ]
        ready.sort(key=lambda s: (-len(s.analytes), s.site_id))
        return ready[:limit] if limit else ready

    # -- results ------------------------------------------------------------------

    async def fetch_samples(self, *, site_id: str, start: date, end: date) -> list[WaterSample]:
        if start > end:
            raise ValidationError("start date must not be after end date")
        # A bare station number (e.g. "06730200") is a USGS site; qualify it. Anything
        # already carrying a provider prefix ("USGS-...", "21COL001_WQX-...") is a full
        # WQP MonitoringLocationIdentifier and must be passed through verbatim — USGS
        # discrete data has largely left the WQP, so state/other-provider ids are what
        # actually return chemistry now.
        qualified = f"USGS-{site_id}" if site_id.isdigit() else site_id
        params = {
            "siteid": qualified,
            "startDateLo": start.strftime("%m-%d-%Y"),
            "startDateHi": end.strftime("%m-%d-%Y"),
            "mimeType": "csv",
            "zip": "no",
            "dataProfile": "resultPhysChem",
        }
        key = cache_key("results", params)
        cached = self._cache.get(key)
        if cached is not None:
            return [WaterSample(**row) for row in cached]

        text = await self._get_text(f"{self._cfg.wqp_base}/Result/search", params)
        samples = normalise_wqp_rows(_iter_csv(text), site_id=qualified)
        self._cache.set(key, [s.model_dump(mode="json") for s in samples], self._cfg.ttl_results_s)
        log.info(
            "samples_fetched",
            extra={"site_id": qualified, "samples": len(samples)},
        )
        return samples


# ------------------------------------------------------------------ parsing helpers


def _required_characteristic_names() -> list[str]:
    """WQP CharacteristicName spellings for the required keys (label + every alias)."""
    names: list[str] = []
    for key in REQUIRED_FOR_SPECIATION:
        param = BY_KEY[key]
        for name in (param.label, *param.aliases):
            if name not in names:
                names.append(name)
    return names


def _group_ready_sites(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Group WQP result rows into per-station required-analyte coverage."""
    stations: dict[str, dict[str, Any]] = {}
    required = set(REQUIRED_FOR_SPECIATION)
    for row in rows:
        pcode = (row.get("USGSPCode") or "").strip()
        characteristic = (row.get("CharacteristicName") or "").strip()
        param = (lookup(pcode) if pcode else None) or lookup(characteristic)
        if param is None or param.key not in required:
            continue
        site_id = (row.get("MonitoringLocationIdentifier") or "").strip()
        if not site_id:
            continue
        entry = stations.setdefault(site_id, {"site_id": site_id, "name": "", "keys": []})
        if param.key not in entry["keys"]:
            entry["keys"].append(param.key)
        if not entry["name"]:
            entry["name"] = (row.get("MonitoringLocationName") or "").strip()
    for entry in stations.values():
        entry["analytes"] = sorted(entry["keys"])
    return list(stations.values())


#: WQP filters by state FIPS (US:NN), not the two-letter code NWIS uses.
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


def _iter_csv(text: str) -> Iterable[dict[str, str]]:
    if not text.strip():
        return []
    return csv.DictReader(io.StringIO(text))


def _parse_rdb_sites(text: str) -> list[SiteSummary]:
    """NWIS RDB: '#' comments, a header row, a format row, then tab-separated data."""
    sites: list[SiteSummary] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if header is None:
            header = fields
            continue
        if fields and fields[0].endswith("s") and len(fields[0]) <= 3:
            continue  # the format row, e.g. '5s'
        row = dict(zip(header, fields, strict=False))
        site_no = row.get("site_no")
        if not site_no:
            continue
        sites.append(
            SiteSummary(
                site_id=f"USGS-{site_no}",
                name=row.get("station_nm", site_no),
                latitude=_float(row.get("dec_lat_va")),
                longitude=_float(row.get("dec_long_va")),
                agency=row.get("agency_cd"),
                site_type=row.get("site_tp_cd"),
                huc=row.get("huc_cd") or None,
                state=row.get("state_cd") or None,
            )
        )
    return sites


def normalise_wqp_rows(rows: Iterable[dict[str, str]], site_id: str) -> list[WaterSample]:
    """Group WQP result rows into samples and resolve each row to a known parameter.

    Rows we cannot map are dropped deliberately: a characteristic with no registry entry
    has no PHREEQC master species and no molar mass, so carrying it forward would only
    create the illusion of completeness.
    """
    grouped: dict[str, dict[str, Any]] = {}
    unmapped: set[str] = set()

    for row in rows:
        characteristic = (row.get("CharacteristicName") or "").strip()
        pcode = (row.get("USGSPCode") or "").strip()
        parameter = (lookup(pcode) if pcode else None) or lookup(characteristic)
        if parameter is None:
            if characteristic:
                unmapped.add(characteristic)
            continue

        detection = (row.get("ResultDetectionConditionText") or "").strip()
        censored = "not detected" in detection.casefold() or "non-detect" in detection.casefold()
        raw_value = (row.get("ResultMeasureValue") or "").strip()
        unit = (row.get("ResultMeasure/MeasureUnitCode") or parameter.default_unit).strip()

        if censored and not raw_value:
            raw_value = (row.get("DetectionQuantitationLimitMeasure/MeasureValue") or "").strip()
            unit = (
                row.get("DetectionQuantitationLimitMeasure/MeasureUnitCode") or unit
            ).strip()
        value = _float(raw_value)
        if value is None:
            continue

        sampled_at = _parse_datetime(
            row.get("ActivityStartDate"), row.get("ActivityStartTime/Time")
        )
        bucket_key = f"{row.get('MonitoringLocationIdentifier', site_id)}|{sampled_at}"
        bucket = grouped.setdefault(
            bucket_key,
            {
                "site_id": row.get("MonitoringLocationIdentifier") or site_id,
                "sampled_at": sampled_at,
                "measurements": {},
            },
        )
        # Last value wins for duplicate analytes within one activity; WQP occasionally
        # reports the same characteristic from several fractions.
        bucket["measurements"][parameter.key] = Measurement(
            key=parameter.key,
            value=value,
            unit=unit or parameter.default_unit,
            censored=censored,
            method=(row.get("ResultAnalyticalMethod/MethodName") or None),
        )

    if unmapped:
        log.info("wqp_characteristics_unmapped", extra={"characteristics": sorted(unmapped)[:20]})

    samples = [
        WaterSample(
            site_id=bucket["site_id"],
            sampled_at=bucket["sampled_at"],
            measurements=list(bucket["measurements"].values()),
        )
        for bucket in grouped.values()
    ]
    samples.sort(key=lambda s: (s.sampled_at is None, s.sampled_at), reverse=True)
    return samples


def aggregate_samples(samples: list[WaterSample], how: str = "median") -> WaterSample | None:
    """Collapse a time series into one representative analysis.

    Medians are the default: water-quality series are short, skewed, and occasionally
    contain order-of-magnitude outliers that a mean would happily propagate into an SI.
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
            Measurement(
                key=key,
                value=value,
                unit=unit,
                censored=all(m.censored for m in group),
            )
        )

    return WaterSample(
        site_id=samples[0].site_id,
        sampled_at=samples[0].sampled_at,
        latitude=samples[0].latitude,
        longitude=samples[0].longitude,
        measurements=merged,
    )


def _float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
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
