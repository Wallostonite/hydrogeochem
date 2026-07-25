#!/usr/bin/env python3
"""Generate realistic demo data and write it to the configured database (e.g. Supabase).

Creates the three application tables if they are absent, then inserts:
  * a couple of Projects,
  * water Samples for real Colorado monitoring sites, with chemistry that passes the
    same charge-balance math the API uses, and
  * ModelRuns with plausible PHREEQC-shaped results linked back to those sites.

Connection target comes from HGC_DATABASE_URL (the app's own setting). Point it at your
Supabase Postgres and run:

    # in .env (never paste a DB password on the command line or in chat):
    #   HGC_DATABASE_URL=postgresql+psycopg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
    python ops/seed_demo_data.py --samples 24 --runs 12

    python ops/seed_demo_data.py --dry-run     # build + print, touch no database
    python ops/seed_demo_data.py --reset        # delete prior demo rows first

The Supabase URL is normalised automatically (psycopg driver + sslmode=require).
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from hgc.config import get_settings
from hgc.domain import units
from hgc.domain.models import Measurement, WaterSample
from hgc.domain.parameters import BY_KEY

# Real Colorado sites that returned all six required analytes from the WQP finder.
# (id, human name, approximate lat/lon within CO for the UI map.)
DEMO_SITES: list[tuple[str, str]] = [
    ("11NPSWRD_WQX-BLCA_09128000", "Gunnison River below Gunnison Tunnel"),
    ("11NPSWRD_WQX-CURE_09125000", "Curecanti Creek near Sapinero"),
    ("11NPSWRD_WQX-CURE_09127000", "Cimarron River below Squaw Creek"),
    ("21COL001_WQX-5602", "Clear Creek at Tennyson"),
    ("21COL001_WQX-5605", "Clear Creek at Youngfield St."),
    ("21COL001_WQX-5570", "Boulder Creek at mouth"),
    ("21COL001_WQX-5588", "S. Boulder Creek at S. Boulder Rd"),
    ("21COL001_WQX-5225", "Big Dry Creek d/s Broomfield WWTP"),
    ("21COL001_WQX-5560B", "Left Hand Creek at 95th St."),
    ("21COL001_WQX-5594", "Coal Creek at S. 120th St."),
    ("21COL001_WQX-CPW-051", "Gross Reservoir"),
    ("21COL001_WQX-CPW-128A", "Standley Lake"),
    ("CORIVWCH_WQX-18", "Clear Creek above gravel pit"),
    ("CORIVWCH_WQX-124", "S. Boulder Creek at Hwy 72"),
    ("CORIVWCH_WQX-222", "Clear Creek at Clear Creek Canyon Park"),
    ("CORIVWCH_WQX-1039", "Clear Creek at Doghead Rail Bridge"),
]

# A limestone-country Ca-HCO3 water (mg/L unless noted); each sample perturbs these.
BASE_WATER: dict[str, tuple[float, str]] = {
    "ph": (7.9, "std units"),
    "temperature": (11.0, "deg C"),
    "ca": (72.0, "mg/l"),
    "mg": (24.0, "mg/l"),
    "na": (38.0, "mg/l"),
    "k": (3.2, "mg/l"),
    "cl": (28.0, "mg/l"),
    "so4": (95.0, "mg/l"),
    "alk_caco3": (185.0, "mg/l"),
    "sio2": (11.0, "mg/l"),
    "fe": (120.0, "ug/l"),
    "mn": (30.0, "ug/l"),
}

PHASES = ("Calcite", "Dolomite", "Gypsum", "Quartz", "Siderite")


def jitter(value: float, spread: float, rng: random.Random) -> float:
    """Multiplicative noise, so trace and major ions both stay physical and positive."""
    return round(value * rng.uniform(1 - spread, 1 + spread), 3)


def build_sample(
    site_id: str,
    when: datetime,
    rng: random.Random,
    *,
    site_offset: float = 1.0,
    trend: float = 1.0,
) -> WaterSample:
    """One analysis at a point in time.

    ``site_offset`` gives each site its own baseline concentration; ``trend`` and a
    seasonal term make the value move slowly over the years and swing within each year,
    so a per-site series reads like real monitoring data rather than random scatter.
    """
    # Warmer, more evaporative late-summer water concentrates solutes; winter dilutes.
    season = 1.0 + 0.12 * math.sin(2 * math.pi * (when.timetuple().tm_yday / 365.0 - 0.25))
    temp_season = 6.0 * math.sin(2 * math.pi * (when.timetuple().tm_yday / 365.0 - 0.30))

    measurements = []
    for key, (val, unit) in BASE_WATER.items():
        if key == "alk_caco3":
            continue
        if key == "temperature":
            v = round(val + temp_season + jitter(1.0, 0.3, rng) - 1.0, 2)
            measurements.append(Measurement(key=key, value=v, unit=unit))
            continue
        if key == "ph":
            v = round(min(max(jitter(val, 0.03, rng), 6.5), 9.0), 2)
            measurements.append(Measurement(key=key, value=v, unit=unit))
            continue
        # Chloride and sulfate carry the multi-year trend (e.g. road salt, mining); the
        # rest just get the site baseline, the seasonal swing, and measurement noise.
        drift = trend if key in ("cl", "so4") else 1.0
        v = jitter(val * site_offset * season * drift, 0.12, rng)
        measurements.append(Measurement(key=key, value=v, unit=unit))

    # meq_per_l is signed (cations +, anions -). The net is the cation excess that
    # alkalinity must offset; a few percent of residual keeps it realistically imperfect.
    net_meq = sum(
        units.meq_per_l(m.mg_per_l, m.parameter) or 0.0
        for m in measurements
        if m.parameter.is_solute
    )
    residual = rng.uniform(-0.03, 0.03)
    alk_meq = max(net_meq * (1 + residual), 0.5)
    alk_mg = round(alk_meq * (BY_KEY["alk_caco3"].equivalent_weight or 50.043), 1)
    measurements.append(Measurement(key="alk_caco3", value=alk_mg, unit="mg/l"))

    return WaterSample(
        site_id=site_id,
        sampled_at=when,
        latitude=round(rng.uniform(37.0, 41.0), 5),
        longitude=round(rng.uniform(-109.0, -102.0), 5),
        source="wqp",
        measurements=measurements,
    )


def synth_result(sample: WaterSample, rng: random.Random) -> dict:
    """A believable PHREEQC-shaped result. Not a real solve — demo data, clearly labelled."""
    ph = sample.ph or 7.8
    # SIs loosely correlated with pH/alkalinity so the demo looks chemically sensible.
    calcite = round((ph - 8.2) * 0.9 + rng.uniform(-0.15, 0.15), 2)
    sis = {
        "Calcite": calcite,
        "Dolomite": round(calcite * 1.9 + rng.uniform(-0.3, 0.3), 2),
        "Gypsum": round(rng.uniform(-2.4, -1.4), 2),
        "Quartz": round(rng.uniform(0.2, 0.8), 2),
        "Siderite": round(rng.uniform(-1.6, -0.6), 2),
    }
    return {
        "ph": ph,
        "pe": round(rng.uniform(3.5, 6.0), 2),
        "temperature_c": sample.temperature_c,
        "ionic_strength": round(rng.uniform(0.006, 0.014), 4),
        "charge_balance_pct": round(sample.charge_balance_pct(), 2),
        "saturation_indices": [{"phase": p, "si": s} for p, s in sis.items()],
        "totals_mol_kgw": {},
        "selected_output": [],
        "warnings": ["synthetic demo result — not a PHREEQC solve"],
    }


def normalise_url(url: str) -> str:
    """Force the psycopg driver and require SSL, as Supabase needs."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if "supabase" in url and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=int, default=10, help="Length of the time series in years")
    parser.add_argument("--per-year", type=int, default=4, help="Samples per site per year (4 = quarterly)")
    parser.add_argument("--runs", type=int, default=None,
                        help="Number of model runs (default: one per site, on its latest sample)")
    parser.add_argument("--owner", default=None, help="Project owner (default: HGC service account)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible data")
    parser.add_argument("--reset", action="store_true", help="Delete existing demo rows first")
    parser.add_argument("--dry-run", action="store_true", help="Build and summarise; write nothing")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    owner = args.owner or "demo@hydrogeochem.dev"
    now = datetime.now(timezone.utc)

    # --- build rows in memory (no DB needed for this part) -----------------------
    projects = [
        {"id": uuid4(), "name": "Colorado Front Range Survey", "owner": owner},
        {"id": uuid4(), "name": "Gunnison Basin Monitoring", "owner": owner},
    ]

    # Each site gets a stable baseline and trend direction so its 10-year series is
    # internally consistent from one visit to the next.
    site_traits = {
        sid: (rng.uniform(0.7, 1.4), rng.uniform(-0.010, 0.035))  # (baseline, annual trend)
        for sid, _ in DEMO_SITES
    }

    points = args.years * args.per_year
    interval = timedelta(days=round(365.25 / args.per_year))
    start = now - interval * (points - 1)

    samples: list[dict] = []
    for site_id, _name in DEMO_SITES:
        baseline, annual_trend = site_traits[site_id]
        lat, lon = round(rng.uniform(37.0, 41.0), 5), round(rng.uniform(-109.0, -102.0), 5)
        for k in range(points):
            when = start + interval * k
            years_elapsed = k / args.per_year
            trend = 1.0 + annual_trend * years_elapsed
            s = build_sample(site_id, when, rng, site_offset=baseline, trend=trend)
            # Keep each site's coordinates fixed across its series.
            s = s.model_copy(update={"latitude": lat, "longitude": lon})
            samples.append(
                {
                    "id": s.id,
                    "site_id": s.site_id,
                    "sampled_at": s.sampled_at,
                    "source": s.source,
                    "latitude": s.latitude,
                    "longitude": s.longitude,
                    "charge_balance_pct": round(s.charge_balance_pct(), 2),
                    "measurements": [m.model_dump() for m in s.measurements],
                    "_sample": s,
                }
            )

    # One run per site by default, on that site's most recent sample.
    latest_per_site: dict[str, dict] = {}
    for row in samples:
        cur = latest_per_site.get(row["site_id"])
        if cur is None or row["sampled_at"] > cur["sampled_at"]:
            latest_per_site[row["site_id"]] = row
    run_sources = list(latest_per_site.values())
    if args.runs is not None:
        run_sources = (run_sources * (args.runs // len(run_sources) + 1))[: args.runs]

    runs: list[dict] = []
    for i, row in enumerate(run_sources):
        s = row["_sample"]
        input_text = (
            f"TITLE demo run {i}\nSOLUTION 1 {s.site_id}\n    units mg/l\n"
            + f"    pH {s.ph}\n    temp {s.temperature_c}\n"
            + "".join(f"    {m.key:<10} {m.value}\n" for m in s.measurements if m.key not in ("ph", "temperature"))
            + "END\n"
        )
        result = synth_result(s, rng)
        runs.append(
            {
                "id": uuid4(),
                "input_hash": hashlib.sha256(input_text.encode()).hexdigest(),
                "status": "succeeded",
                "database": "phreeqc.dat",
                "database_sha256": hashlib.sha256(b"demo").hexdigest(),
                "engine_version": "phreeqpy/0.6.0",
                "input_text": input_text,
                "result": result,
                "duration_ms": rng.randint(6, 40),
                "site_id": s.site_id,
                "project_id": projects[i % len(projects)]["id"],
                "completed_at": now,
            }
        )

    span_lo = min(s["sampled_at"] for s in samples)
    span_hi = max(s["sampled_at"] for s in samples)
    print(f"Built {len(projects)} projects, {len(samples)} samples, {len(runs)} runs.", file=sys.stderr)
    print(f"Time series: {len(DEMO_SITES)} sites x {points} points "
          f"({args.per_year}/yr over {args.years} yr), {span_lo:%Y-%m-%d} .. {span_hi:%Y-%m-%d}",
          file=sys.stderr)
    cbe = [s["charge_balance_pct"] for s in samples]
    print(f"Charge-balance range: {min(cbe):+.1f}% .. {max(cbe):+.1f}%  (within +/-10% is analytically sound)",
          file=sys.stderr)

    if args.dry_run:
        print("\n--- dry run: sample[0] ---")
        s0 = samples[0]
        print(f"site {s0['site_id']}  cbe {s0['charge_balance_pct']:+.2f}%  "
              f"lat/lon {s0['latitude']},{s0['longitude']}")
        for m in s0["measurements"]:
            print(f"  {m['key']:<12} {m['value']} {m['unit']}")
        print("\n--- dry run: run[0] input ---")
        print(runs[0]["input_text"])
        print("No database was touched (--dry-run).", file=sys.stderr)
        return 0

    # --- write to the database ---------------------------------------------------
    url = normalise_url(settings.database_url)
    safe = url.split("@")[-1] if "@" in url else url
    print(f"Connecting to ...@{safe}", file=sys.stderr)

    from sqlalchemy import delete

    from hgc.db.base import Base, build_engine
    from hgc.db.models import ModelRunRow, Project, SampleRow

    engine = build_engine(url, settings.db_pool_size, settings.db_max_overflow)
    Base.metadata.create_all(engine)  # idempotent; creates the three tables if missing

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        if args.reset:
            session.execute(delete(ModelRunRow).where(ModelRunRow.database_sha256 == hashlib.sha256(b"demo").hexdigest()))
            owners = {p["owner"] for p in projects}
            for o in owners:
                session.execute(delete(Project).where(Project.owner == o))
            session.execute(delete(SampleRow).where(SampleRow.source == "wqp"))
            session.commit()
            print("Cleared prior demo rows.", file=sys.stderr)

        session.add_all(Project(id=p["id"], name=p["name"], owner=p["owner"]) for p in projects)
        session.add_all(
            SampleRow(
                id=s["id"], site_id=s["site_id"], sampled_at=s["sampled_at"], source=s["source"],
                latitude=s["latitude"], longitude=s["longitude"],
                charge_balance_pct=s["charge_balance_pct"], measurements=s["measurements"],
            )
            for s in samples
        )
        session.add_all(
            ModelRunRow(**{k: v for k, v in r.items()}) for r in runs
        )
        session.commit()

    print(f"\nWrote {len(projects)} projects, {len(samples)} samples, {len(runs)} runs to the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
