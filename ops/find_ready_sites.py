#!/usr/bin/env python3
"""Find USGS site IDs that actually carry the data a speciation model needs.

The `/v1/sites` search lists every monitoring location in an area; it does not tell you
which ones have the chemistry. This asks the USGS Samples API for the required analytes
directly, groups the results by station, and prints only the sites whose analyses cover
(most of) ``REQUIRED_FOR_SPECIATION``.

It reuses the app's own `UsgsClient.find_ready_sites`, so it never drifts from the API.

Examples
--------
    python ops/find_ready_sites.py --state CO --start 2018-01-01
    python ops/find_ready_sites.py --bbox -105.3,39.5,-104.7,40.1
    python ops/find_ready_sites.py --state CO --min-required 5

Feed any printed ID straight into the UI's "Model a sample" box or the API:
    curl "localhost:8000/v1/sites/USGS-09071750/samples?start=2018-01-01&end=2024-12-31"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from hgc.config import get_settings
from hgc.domain.parameters import REQUIRED_FOR_SPECIATION


def _bbox(value: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be west,south,east,north")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from exc
    return (west, south, east, north)


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    today = date.today()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    area = parser.add_mutually_exclusive_group(required=True)
    area.add_argument("--state", help="Two-letter state code, e.g. CO")
    area.add_argument("--bbox", type=_bbox, help="west,south,east,north in decimal degrees")
    parser.add_argument("--start", type=date.fromisoformat, default=today.replace(year=today.year - 6),
                        help="ISO date; default 6 years ago")
    parser.add_argument("--end", type=date.fromisoformat, default=today, help="ISO date; default today")
    parser.add_argument("--min-required", type=int, default=len(REQUIRED_FOR_SPECIATION),
                        help=f"Minimum required analytes present (default all {len(REQUIRED_FOR_SPECIATION)})")
    parser.add_argument("--limit", type=int, default=50, help="Max sites to print")
    args = parser.parse_args(argv)

    from hgc.services.cache import build_cache
    from hgc.services.usgs import HttpConfig, UsgsClient

    print(f"Required analytes: {', '.join(REQUIRED_FOR_SPECIATION)}", file=sys.stderr)
    print("Querying the USGS Samples API ...  (live; a whole state can take a minute)", file=sys.stderr)

    async def run():
        client = UsgsClient(
            HttpConfig(ogc_base=settings.wdfn_ogc_base_url, samples_base=settings.samples_base_url,
                       api_key=settings.usgs_api_key, timeout_s=120.0, user_agent=settings.http_user_agent),
            cache=build_cache(None),
        )
        try:
            return await client.find_ready_sites(
                start=args.start, end=args.end, state=args.state, bbox=args.bbox,
                min_required=args.min_required, limit=args.limit,
            )
        finally:
            await client.aclose()

    try:
        ready = asyncio.run(run())
    except Exception as exc:
        print(f"Could not reach the Samples API: {exc}", file=sys.stderr)
        return 1

    if not ready:
        print("No sites met the bar. Widen the date window, the area, or lower --min-required.",
              file=sys.stderr)
        return 0

    n = len(REQUIRED_FOR_SPECIATION)
    print(f"\n{len(ready)} site(s) with >= {args.min_required}/{n} required analytes:\n")
    print(f"{'site_id':<24} {'have':>5}  {'missing':<22} name")
    print("-" * 92)
    for s in ready:
        miss = ",".join(s.missing) if s.missing else "-"
        print(f"{s.site_id:<24} {len(s.analytes):>2}/{n}  {miss:<22} {s.name[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
