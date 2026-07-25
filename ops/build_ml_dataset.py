#!/usr/bin/env python3
"""Build a flat, ML-ready dataset: one row per sample, inputs joined with model outputs.

Each record combines the water-chemistry inputs (the analytes that went into PHREEQC) with
the outputs of running that exact sample (pH, pe, ionic strength, charge balance, and a
saturation index per mineral). That is the shape a downstream researcher wants for
regression or classification: features + targets in a single table, one point per row.

Sources
-------
    # From the local database (the seeded synthetic data, or anything else stored there):
    python ops/build_ml_dataset.py --from-db -o dataset.csv

    # From a live site via the Water Quality Portal (bucket by year for complete analyses):
    python ops/build_ml_dataset.py --site 21COL001_WQX-5605 --start 2007-01-01 --bucket year

Columns
-------
    id_*        site_id, sampled_at, latitude, longitude, source
    in_*        input analytes (solutes in mg/L; in_ph in std units; in_temperature in degC)
    in_charge_balance_pct   the analysis's own charge-balance error
    out_*       out_status, out_ph, out_pe, out_ionic_strength, out_charge_balance_pct
    si_<phase>  saturation index per mineral (blank where the phase is undefined)
    meta_*      database, database_sha256, engine_version, duration_ms  (reproducibility)

PHREEQC runs here in-process (no API, no rate limit). Guarded by __main__ because the
engine spawns worker processes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

import pandas as pd

from hgc.config import get_settings
from hgc.domain.models import Measurement, ModelSpec, WaterSample
from hgc.services.dataset import BUCKETS
from hgc.services.dataset import DEFAULT_DATASET_PHASES as DEFAULT_PHASES
from hgc.services.dataset import bucket_samples, flatten_sample
from hgc.services.phreeqc import PhreeqcEngine


def normalise_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if "supabase" in url and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def samples_from_db(url: str) -> list[WaterSample]:
    from sqlalchemy import select

    from hgc.db.base import build_session_factory
    from hgc.db.models import SampleRow

    factory = build_session_factory(normalise_url(url))
    out: list[WaterSample] = []
    with factory() as session:
        for row in session.execute(select(SampleRow)).scalars():
            out.append(
                WaterSample(
                    site_id=row.site_id,
                    sampled_at=row.sampled_at,
                    latitude=row.latitude,
                    longitude=row.longitude,
                    source=row.source,
                    measurements=[Measurement(**m) for m in (row.measurements or [])],
                )
            )
    return out


def _new_client(settings):
    from hgc.services.cache import build_cache
    from hgc.services.usgs import HttpConfig, UsgsClient

    return UsgsClient(
        HttpConfig(ogc_base=settings.wdfn_ogc_base_url, samples_base=settings.samples_base_url,
                   timeout_s=120.0, user_agent=settings.http_user_agent),
        cache=build_cache(None),
    )


def samples_from_sites(
    settings, site_ids: list[str], start: date, end: date
) -> dict[str, list[WaterSample]]:
    """Fetch raw WQP samples for each site (bucketing happens later, uniformly)."""
    async def fetch() -> dict[str, list[WaterSample]]:
        client = _new_client(settings)
        try:
            out: dict[str, list[WaterSample]] = {}
            for i, sid in enumerate(site_ids, 1):
                out[sid] = await client.fetch_samples(site_id=sid, start=start, end=end)
                print(f"  fetched {i}/{len(site_ids)}  {sid} ({len(out[sid])} samples)",
                      file=sys.stderr)
            return out
        finally:
            await client.aclose()

    return asyncio.run(fetch())


def discover_sites(
    settings, state: str | None, bbox: str | None, start: date, end: date,
    provider: str | None, max_sites: int,
) -> list[str]:
    """Find sites with the required chemistry via the WQP finder."""
    parsed = tuple(float(p) for p in bbox.split(",")) if bbox else None

    async def find() -> list[str]:
        client = _new_client(settings)
        try:
            ready = await client.find_ready_sites(
                start=start, end=end, state=state, bbox=parsed,
                provider=provider, limit=max_sites,
            )
            return [r.site_id for r in ready]
        finally:
            await client.aclose()

    return asyncio.run(find())


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    today = date.today()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-db", action="store_true", help="Read samples from the database")
    src.add_argument("--sites", nargs="+", metavar="SITE_ID", help="One or more WQP site ids")
    src.add_argument("--discover", action="store_true", help="Find ready sites via --state/--bbox")
    parser.add_argument("--state", help="For --discover: two-letter state code, e.g. CO")
    parser.add_argument("--bbox", help="For --discover: west,south,east,north")
    parser.add_argument("--provider", default=None, help="WQP provider filter (NWIS/STORET/NGWMN)")
    parser.add_argument("--max-sites", type=int, default=25, help="Cap on discovered sites")
    parser.add_argument("--start", type=date.fromisoformat, default=today.replace(year=today.year - 10))
    parser.add_argument("--end", type=date.fromisoformat, default=today)
    parser.add_argument("--bucket", choices=list(BUCKETS), default="year",
                        help="Row resolution: event/month/quarter/year/window (default year)")
    parser.add_argument("--aggregate", choices=["mean", "median", "latest"], default="median")
    parser.add_argument("--database", default="phreeqc.dat")
    parser.add_argument("--phases", nargs="*", default=list(DEFAULT_PHASES))
    parser.add_argument("-o", "--output", default="ml_dataset.csv")
    args = parser.parse_args(argv)

    # Gather raw samples grouped by site, from whichever source.
    if args.from_db:
        print("Reading samples from the database ...", file=sys.stderr)
        by_site: dict[str, list[WaterSample]] = {}
        for s in samples_from_db(settings.database_url):
            by_site.setdefault(s.site_id, []).append(s)
    else:
        if args.discover:
            if not (args.state or args.bbox):
                parser.error("--discover needs --state or --bbox")
            print("Discovering ready sites via the WQP finder ...", file=sys.stderr)
            site_ids = discover_sites(settings, args.state, args.bbox, args.start, args.end,
                                      args.provider, args.max_sites)
            print(f"  found {len(site_ids)} sites.", file=sys.stderr)
        else:
            site_ids = args.sites
        if not site_ids:
            print("No sites to fetch.", file=sys.stderr)
            return 1
        by_site = samples_from_sites(settings, site_ids, args.start, args.end)

    # One uniform bucketing pass per site, then flatten every representative.
    reps: list[WaterSample] = []
    for site_samples in by_site.values():
        reps.extend(bucket_samples(site_samples, args.bucket, args.aggregate))
    print(f"{len(by_site)} sites -> {len(reps)} rows to model ({args.bucket} buckets).",
          file=sys.stderr)
    if not reps:
        print("No samples found.", file=sys.stderr)
        return 1

    spec = ModelSpec(database=args.database, saturation_phases=tuple(args.phases))
    engine = PhreeqcEngine(
        database_dir=settings.phreeqc_database_dir,
        allowed_databases=settings.phreeqc_allowed_databases,
        workers=settings.phreeqc_workers,
        timeout_s=settings.phreeqc_timeout_s,
        max_tasks_per_child=settings.phreeqc_max_tasks_per_child,
        child_memory_mb=settings.phreeqc_child_memory_mb,
    )
    engine.verify_databases()
    engine.start()
    try:
        records = []
        for i, sample in enumerate(reps, 1):
            records.append(flatten_sample(sample, spec, engine))
            if i % 50 == 0:
                print(f"  modelled {i}/{len(reps)}", file=sys.stderr)
    finally:
        engine.shutdown()

    frame = pd.DataFrame(records)
    # Stable, grouped column order: identity, inputs, outputs, saturation indices, meta.
    def rank(col: str) -> tuple[int, str]:
        for i, pre in enumerate(("id_", "in_", "out_", "si_", "meta_")):
            if col.startswith(pre):
                return (i, col)
        return (5, col)

    frame = frame[sorted(frame.columns, key=rank)]
    frame.to_csv(args.output, index=False)

    ok = (frame["out_status"] == "succeeded").sum()
    print(f"\nWrote {len(frame)} rows x {len(frame.columns)} columns to {args.output}", file=sys.stderr)
    print(f"{ok} modelled successfully, {len(frame) - ok} failed/incomplete.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
