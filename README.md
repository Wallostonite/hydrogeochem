# HydroGeoChem Explorer

Retrieve water-quality analyses, turn them into correct PHREEQC input, and run
geochemical models as a service, with reproducible results.

Full design rationale: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Step-by-step setup for macOS, Windows and Linux: [`docs/TESTING.md`](docs/TESTING.md).

## The goal

Help everyone who depends on water understand and predict how its chemistry will behave.
The immediate, concrete question the tool answers today is the one every water system shares:
**will this water stay safe and non-damaging on its way to the tap**, or will it corrode pipes
and leach metals (the chemistry behind lead-in-water problems) or clog them with scale.

That question matters to a wide set of stakeholders, and the same reproducible result serves
all of them:

| Who depends on the answer | What they get from it |
|---|---|
| Communities and households | Whether the water is chemically inclined to corrode pipes and mobilise lead or copper |
| Utilities and treatment operators | Corrosion-control and scaling decisions, grounded in the actual source chemistry |
| Farmers and irrigators | The dissolved-ion picture behind salinity and sodium hazard |
| Regulators and watershed managers | Consistent, comparable readings across many sites, and trends over time |
| Researchers and educators | Reproducible model output and machine-learning-ready datasets |

**Where it is heading.** The anchor above is what the tool does reliably now. The growth
vision is to broaden from corrosion and scaling toward overall water-quality prediction:
trends over time, more parameters, and machine-learning surrogates that make an answer
available even where sampling is thin. The aim is to move from a specialist geochemistry tool
toward something the whole water community can use.

> **Where the data comes from (read this first).** As of 2024, USGS discrete water-quality
> data has largely **left the Water Quality Portal (WQP)**, so a `USGS-…` site typically
> returns no analyses. The chemistry that is still retrievable comes from **state and other
> providers** (STORET/WQX), whose site identifiers look like `21COL001_WQX-5605`. Use the
> **finder** ([`ops/find_ready_sites.py`](ops/find_ready_sites.py)) or the UI's *Data source*
> selector to discover sites that actually carry the required analytes. USGS's own discrete
> data now lives in the newer [Water Data / Samples API](https://api.waterdata.usgs.gov/),
> which the `dataretrieval` Python package wraps, a natural next adapter for this app.

---

## Quick start

`make` targets always run through the project's own `.venv`, created automatically on
first use, so these work the same regardless of what's active in your shell (conda,
system Python, nothing). Running tools directly (`pytest`, `uvicorn`, …) instead of
through `make` still needs `source .venv/bin/activate` first.

```bash
make install                 # creates .venv, then pip install -e ".[dev,ui]"
make databases               # fetch + checksum the PHREEQC thermodynamic databases
cp .env.example .env
make test                    # 35 tests, no database or DLL required

make api                     # http://localhost:8000/docs
make ui                      # http://localhost:8501
```

Or the whole stack, API, worker, UI, Postgres, Redis:

```bash
./ops/fetch_databases.sh && docker compose up --build
```

`phreeqpy` needs the IPhreeqc shared library. On Debian/Ubuntu the wheel bundles it; on
macOS install it with `brew install phreeqc` and point `HGC_PHREEQC_DATABASE_DIR` at the
database directory. The API starts without it and reports the condition on `/readyz`
rather than failing every request with a stack trace.

> **Database compatibility.** `make databases` fetches `phreeqc.dat`/`pitzer.dat` from the
> `phreeqpython` mirror and the other three from `usgs-coupled/phreeqc3`, because the
> IPhreeqc build bundled in `phreeqpy 0.6.0` **cannot parse** the newer Peng-Robinson gas
> sections in `usgs-coupled`'s `phreeqc.dat`/`pitzer.dat` (every run fails with a generic
> `phreeqc_error`). If you re-point the fetch script or upgrade the databases, verify a real
> run still succeeds, the fetch scripts document this in a comment.

---

## Shape of the system

```
Streamlit UI ──┐
Notebooks ─────┼──> FastAPI ──> PHREEQC process pool ──> IPhreeqc + pinned .dat files
QGIS / curl ───┘        │  └──> Redis ──> Celery workers (batches, long runs)
                        └────> Postgres (runs, samples)  ·  USGS NWIS + Water Quality Portal
```

Four deployables, one image. The UI is a pure HTTP client of the API, so what a scientist
sees on screen is exactly what a script gets back.

| Package | Responsibility | Imports |
|---|---|---|
| `hgc.domain` | units, parameter registry, samples, charge balance, results | nothing |
| `hgc.services` | USGS adapters, PHREEQC engine, sanitizer, orchestration | domain |
| `hgc.db` | tables and repositories | domain |
| `hgc.api` | HTTP transport, auth, error mapping | services, db |
| `hgc.worker` | Celery tasks | services, db |
| `hgc.ui` | Streamlit | the API, over HTTP |

---

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/sites` | Search the NWIS site catalogue by `state`, `bbox`, or `site_ids` (`limit=0` = no cap) |
| `GET` | `/v1/sites/ready` | Sites that carry the required analytes. `source=wqp` (optional `provider`) or `source=synthetic` |
| `GET` | `/v1/sites/{id}/samples` | Normalised analyses + the representative sample + readiness |
| `GET` | `/v1/sites/{id}/dataset` | Flat ML dataset: inputs joined with outputs, one row per `bucket` (event/month/quarter/year/window) |
| `POST` | `/v1/runs/preview` | Render the PHREEQC input without running it |
| `POST` | `/v1/runs` | 201 when run inline, 202 + `Location` when queued, 200 when deduplicated |
| `GET` | `/v1/runs/{id}` | Status and results; the exact input is always retained |
| `POST` | `/v1/batches` | Fan a spec across up to 250 sites |
| `GET` | `/v1/catalog` | Databases, phases, and the parameter registry, enough to build a form |
| `GET` | `/healthz`, `/readyz`, `/metrics` | Liveness, readiness, Prometheus |

Errors are RFC-7807 problem documents with a stable `code` (`phreeqc_timeout`,
`unsafe_phreeqc_input`, `upstream_unavailable`, …), so clients branch on the code and not
on prose.

```bash
# A site that actually has chemistry (state provider), not a USGS- id, see the note up top
curl -s localhost:8000/v1/sites/ready?source=wqp\&bbox=-105.3,39.9,-105.1,40.1\&start=2015-01-01 | jq '.[0]'

curl -s -X POST localhost:8000/v1/runs \
  -H 'content-type: application/json' \
  -d '{"sample":{"site_id":"21COL001_WQX-5605","measurements":[
        {"key":"ph","value":7.4,"unit":"std units"},
        {"key":"ca","value":88,"unit":"mg/l"},
        {"key":"alk_caco3","value":250,"unit":"mg/l"}]},
       "spec":{"database":"phreeqc.dat","saturation_phases":["Calcite","Dolomite"]}}' | jq .result
```

---

## Finding sites and building ML datasets

Three `ops/` scripts and the matching UI/API surfaces turn this from a one-model-at-a-time
tool into a way to assemble training data:

| Script | What it does |
|---|---|
| [`ops/find_ready_sites.py`](ops/find_ready_sites.py) | Lists WQP sites that carry all required analytes (`--state`/`--bbox`, `--provider`, `--min-required`) |
| [`ops/seed_demo_data.py`](ops/seed_demo_data.py) | Generates a charge-balanced synthetic time series and writes it to the database (`--from-db` source, Supabase-ready) |
| [`ops/build_ml_dataset.py`](ops/build_ml_dataset.py) | Builds a flat **input+output** CSV: `--from-db`, `--sites`, or `--discover --state CO`; `--bucket event/month/quarter/year/window` |

Each dataset row is one sample: `id_*` (site, date, coords), `in_*` (analytes in mg/L +
charge balance), `out_*` (pH, pe, ionic strength), `si_<phase>` (a saturation index per
mineral, the ML targets), and `meta_*` (database SHA-256, engine version) for
reproducibility. In the UI, **Model a sample → Build dataset** and **Find sites →
multi-select → Build dataset** produce the same table for download.

Because real per-sampling-event WQP records are sparse (a visit rarely measures the whole
ion suite), the `month`/`quarter`/`year` buckets aggregate a period's events into one
*complete* analysis. Use `--bucket year` (or month) for usable rows; `event` keeps real
dates but many rows will be incomplete.

```bash
# A ready-to-train dataset across every chemistry-bearing site in Colorado, one row per month
python ops/build_ml_dataset.py --discover --state CO --max-sites 25 --bucket month -o train.csv
```

---

## Three things this gets right that a notebook usually doesn't

**Units and reporting basis.** Iron arrives as µg/L, sulfate as SO₄, nitrate as N,
alkalinity as CaCO₃. Every value is converted through `hgc.domain.units` and emitted with
an explicit `as` qualifier. A 1000× error in dissolved iron still converges and still
prints a saturation index, it is just wrong, quietly.

**Charge balance is surfaced, not suppressed.** Every sample reports its charge-balance
error, and analyses outside ±10% carry a warning into the result. It is the single best
indicator that an analysis is incomplete.

**Results are reproducible.** A saturation index means nothing without the thermodynamic
database that produced it, so every run records the database name, its SHA-256, the engine
version, and the verbatim input. The same triple is what forms the idempotency key.

---

## Safety

Custom PHREEQC input is an expression language executing on our CPUs. Two layers guard it:

1. `services/phreeqc/sanitizer.py` rejects `DATABASE`, `DUMP`, `INCLUDE$`, and every
   `-file` option, with a byte cap.
2. Engine children run with `RLIMIT_FSIZE = 0`, capped address space and CPU time, so a
   keyword the sanitizer missed still cannot write a file.

Timeouts are enforced by killing the child process, a running IPhreeqc call cannot be
interrupted from Python, and the pool is rebuilt. `hgc_phreeqc_pool_recycles_total` is
alerted on: a rising rate means some class of input reliably hangs the solver.

---

## Operations

- **Config**: environment only, `HGC_`-prefixed, validated at startup (`hgc/config.py`).
- **Logs**: JSON, with `request_id` threaded from API through Celery into the engine.
- **Metrics**: `/metrics`; run latency by database and outcome, pool recycles, upstream
  latency, cache hits, queue depth.
- **Scaling**: API and UI on CPU; workers on queue depth. Set `HGC_PHREEQC_WORKERS ≈ vCPU`,
  because PHREEQC saturates a core and oversubscription turns a 200 ms speciation into a timeout.
- **Migrations**: Alembic against `hgc.db.base.Base.metadata`; run as a pre-deploy job,
  expand-then-contract so a rollback never loses data.

## Testing

```bash
make test          # domain, services, orchestration, no DB, no DLL
pytest -m phreeqc  # scientific regression against a real IPhreeqc install
```

The `phreeqc`-marked suite asserts known saturation indices to ±0.01. It is what stops a
refactor from quietly moving the chemistry.
