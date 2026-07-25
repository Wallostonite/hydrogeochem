#!/usr/bin/env python3
"""Find USGS site IDs that actually carry the data a speciation model needs.

The API's `/v1/sites` search lists every monitoring location in an area — it does not
tell you which ones have the chemistry. This script asks the Water Quality Portal for
the required analytes directly, groups the results by station, and prints only the
sites whose analyses cover every parameter in ``REQUIRED_FOR_SPECIATION``.

The "required" set and the characteristic-name matching come straight from the app's
own parameter registry (`hgc.domain.parameters`), so this stays in lockstep with what
the API considers a runnable sample.

Examples
--------
    # Colorado, wells and springs, analytes reported since 2018
    python ops/find_ready_sites.py --state CO --site-type Well --site-type Spring

    # A bounding box instead of a whole state (faster, smaller download)
    python ops/find_ready_sites.py --bbox -105.3,39.5,-104.7,40.1

    # Loosen the bar: sites with at least 5 of the required analytes
    python ops/find_ready_sites.py --state CO --min-required 5

Feed any printed ID straight into the UI's "Model a sample" box or the API:
    curl "localhost:8000/v1/sites/21COL001_WQX-5605/samples?start=2018-01-01&end=2024-12-31"
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date

import httpx

# Reuse the running app's definitions so this never drifts from the API.
from hgc.config import get_settings
from hgc.domain.parameters import BY_KEY, REQUIRED_FOR_SPECIATION, lookup

# WQP wants state FIPS codes (US:08), not the two-letter code NWIS uses. Map the common
# ones; anything else can be passed through as a raw "US:NN" via --state.
STATE_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}


def characteristic_names(keys: tuple[str, ...]) -> list[str]:
    """WQP CharacteristicName strings to request for a set of registry keys.

    Each parameter's label and aliases are exactly the WQP characteristic spellings,
    so this asks the portal for every name that will resolve back to a required key.
    """
    names: list[str] = []
    for key in keys:
        param = BY_KEY[key]
        for name in (param.label, *param.aliases):
            if name not in names:
                names.append(name)
    return names


def statecode(value: str) -> str:
    v = value.strip().upper()
    if v.startswith("US:"):
        return v
    if v in STATE_FIPS:
        return f"US:{STATE_FIPS[v]}"
    raise argparse.ArgumentTypeError(
        f"unknown state {value!r}; use a two-letter code (CO) or a FIPS form (US:08)"
    )


def bbox(value: str) -> str:
    parts = value.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be west,south,east,north")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from exc
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise argparse.ArgumentTypeError("bbox out of range or not ordered west,south,east,north")
    return ",".join(str(p) for p in (west, south, east, north))


def build_params(args: argparse.Namespace) -> dict[str, object]:
    params: dict[str, object] = {
        "mimeType": "csv",
        "zip": "no",
        "dataProfile": "resultPhysChem",
        "startDateLo": args.start.strftime("%m-%d-%Y"),
        "startDateHi": args.end.strftime("%m-%d-%Y"),
        # NWIS = USGS. The app fetches samples by USGS- identifier, so by default we only
        # surface sites it can actually pull. Pass --provider '' to include every source.
        "providers": args.provider,
        # Ask only for the analytes we score on — keeps the download proportional to
        # the question instead of pulling every characteristic ever measured.
        "characteristicName": characteristic_names(args.required_keys),
    }
    if not args.provider:
        del params["providers"]
    if args.state:
        params["statecode"] = args.state
    if args.bbox:
        params["bBox"] = args.bbox
    if args.site_type:
        params["siteType"] = args.site_type
    return params


def scan(text_lines: object, required: set[str]) -> dict[str, dict[str, object]]:
    """Group WQP rows into {site_id: {name, keys}} using the registry to resolve rows."""
    reader = csv.DictReader(text_lines)  # type: ignore[arg-type]
    stations: dict[str, dict[str, object]] = defaultdict(
        lambda: {"name": "", "keys": set()}
    )
    for row in reader:
        pcode = (row.get("USGSPCode") or "").strip()
        characteristic = (row.get("CharacteristicName") or "").strip()
        param = (lookup(pcode) if pcode else None) or lookup(characteristic)
        if param is None or param.key not in required:
            continue
        site_id = (row.get("MonitoringLocationIdentifier") or "").strip()
        if not site_id:
            continue
        entry = stations[site_id]
        entry["keys"].add(param.key)  # type: ignore[union-attr]
        if not entry["name"]:
            entry["name"] = (row.get("MonitoringLocationName") or "").strip()
    return stations


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    today = date.today()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    area = parser.add_mutually_exclusive_group(required=True)
    area.add_argument("--state", type=statecode, help="Two-letter code (CO) or FIPS (US:08)")
    area.add_argument("--bbox", type=bbox, help="west,south,east,north in decimal degrees")
    parser.add_argument("--start", type=date.fromisoformat, default=today.replace(year=today.year - 6),
                        help="ISO date; default 6 years ago")
    parser.add_argument("--end", type=date.fromisoformat, default=today, help="ISO date; default today")
    parser.add_argument("--site-type", action="append", default=[],
                        help="WQP siteType filter, repeatable (e.g. Well, Spring, Stream)")
    parser.add_argument("--provider", default="NWIS",
                        help="WQP data provider; default NWIS (USGS). Pass '' for all sources.")
    parser.add_argument("--min-required", type=int, default=len(REQUIRED_FOR_SPECIATION),
                        help=f"Minimum required analytes present (default all {len(REQUIRED_FOR_SPECIATION)})")
    parser.add_argument("--limit", type=int, default=50, help="Max sites to print")
    args = parser.parse_args(argv)
    args.required_keys = REQUIRED_FOR_SPECIATION

    required = set(REQUIRED_FOR_SPECIATION)
    url = f"{settings.wqp_base_url}/Result/search"
    params = build_params(args)

    print(f"Required analytes: {', '.join(REQUIRED_FOR_SPECIATION)}", file=sys.stderr)
    print(f"Querying {url} ...  (live USGS; a whole state can take a minute)", file=sys.stderr)

    try:
        with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0),
                          headers={"User-Agent": settings.http_user_agent}) as client:
            with client.stream("GET", url, params=params) as response:
                if response.status_code >= 400:
                    print(f"WQP returned {response.status_code}", file=sys.stderr)
                    return 1
                stations = scan(response.iter_lines(), required)
    except httpx.HTTPError as exc:
        print(f"Could not reach the Water Quality Portal: {exc}", file=sys.stderr)
        return 1

    ranked = sorted(
        (
            (site_id, len(entry["keys"]), sorted(required - entry["keys"]), entry["name"])  # type: ignore[operator]
            for site_id, entry in stations.items()
            if len(entry["keys"]) >= args.min_required  # type: ignore[arg-type]
        ),
        key=lambda r: (-r[1], r[0]),
    )

    if not ranked:
        if args.provider.upper() == "NWIS":
            print(
                "No NWIS (USGS) records came back at all. As of 2024 the Water Quality "
                "Portal no longer serves USGS discrete water-quality data, so a USGS-only "
                "search returns nothing. Re-run with --provider '' to include state and "
                "other sources, or point the app at USGS's new Samples API.",
                file=sys.stderr,
            )
        else:
            print("No sites met the bar. Widen the date window, the area, or lower --min-required.",
                  file=sys.stderr)
        return 0

    print(f"\n{len(ranked)} site(s) with >= {args.min_required}/{len(required)} required analytes"
          f" (showing up to {args.limit}):\n")
    print(f"{'site_id':<22} {'have':>4}  {'missing':<22} name")
    print("-" * 90)
    for site_id, have, missing, name in ranked[: args.limit]:
        miss = ",".join(missing) if missing else "-"
        print(f"{site_id:<22} {have:>2}/{len(required)}  {miss:<22} {name[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
