# Testing this in VS Code, step by step

Written for **macOS**, **Windows** and **Linux**. Where a command differs, both versions
are shown, macOS and Linux share one, Windows PowerShell gets its own.

Every stage ends with something you can see. Stages 1–5 need nothing but Python: no
database, no Docker, no PHREEQC. Add the heavier pieces only once the light ones pass.

> **Using Git Bash or WSL on Windows?** Follow the macOS/Linux commands throughout. The
> PowerShell column is only for the default VS Code terminal on Windows.

---

## 0 · Before you start

**All platforms:** VS Code with the **Python** extension (`ms-python.python`), and Python
3.11 or newer.

| | How to check | If you need it |
|---|---|---|
| **macOS** | `python3 --version` | macOS ships an old Python. `brew install python@3.12`, or the installer from python.org. |
| **Windows** | `py --version` | Install from python.org or `winget install Python.Python.3.12`. Tick **Add Python to PATH**. |
| **Linux** | `python3 --version` | `sudo apt install python3 python3-venv` |

Docker Desktop is optional, stage 11 only.

**One platform difference worth knowing now:** the engine sandbox that caps memory and
blocks file writes in the calculation processes is a Unix feature. On macOS and Linux it's
active. On Windows it silently isn't, and the input sanitizer carries the load alone. Fine
for local testing; for anything user-facing on Windows, run it in Docker.

---

## 1 · Open the project

Unzip `hydrogeochem.zip`, then **File → Open Folder…** and pick the `hydrogeochem` folder.
Say yes to "trust the authors", and yes to installing the recommended extensions. The `.vscode/extensions.json` file asks for Python, Pylance, Ruff, REST Client and Docker.

The Explorer should show `src/`, `tests/`, `docs/`, `ops/`, `Dockerfile`.

**Windows only:** if you unzipped from a downloaded file, right-click the zip → Properties
→ **Unblock** before extracting, or Windows marks the scripts as untrusted.

---

## 2 · Create the virtual environment

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell refuses with a script-execution error, allow it for this window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

Then point VS Code at it: **Ctrl+Shift+P** (**Cmd+Shift+P** on macOS) → *Python: Select
Interpreter* → choose the one whose path contains `.venv`. The status bar should read
`Python 3.1x.x ('.venv')`.

Close and reopen the terminal afterwards so it picks up the interpreter and the
`PYTHONPATH` that `.vscode/settings.json` sets per platform.

---

## 3 · Install the light dependencies

```
pip install -r requirements-test.txt
```

Same on every platform. About fifteen seconds, pure Python, no compiler, no database
drivers, no PHREEQC. That's deliberate: the chemistry and the orchestration are testable
without any of the heavy machinery, and if that stops being true, the layering has broken.

---

## 4 · Run the tests

Setting an environment variable for a single command is the one place the shells really
diverge.

**macOS / Linux**

```bash
HGC_ENV=test pytest
```

**Windows (PowerShell)**

```powershell
$env:HGC_ENV = "test"
pytest
```

`HGC_ENV=test pytest` on one line does **not** work in PowerShell, it's the most common
first stumble. The variable stays set for the rest of that terminal session, so you only
do it once.

Either way:

```
35 passed in 0.4s
```

Or skip the terminal entirely: the flask-shaped **Testing** icon in the sidebar discovers
all 35, runs them individually, and offers **Debug Test** on right-click. It reads
`HGC_ENV` from your `.env` file, so it works before you've set anything.

What just got proved:

| Test file | The claim it defends |
|---|---|
| `test_units.py` | Micrograms, millimoles and milliequivalents convert correctly; unknown units are refused rather than guessed |
| `test_input_builder.py` | Iron entered as 45 µg/L comes out as 0.045 mg/L, alkalinity keeps its "as CaCO₃" basis, non-detects are halved and reported |
| `test_sanitizer.py` | `DATABASE`, `DUMP`, `INCLUDE$` and every `-file` option are rejected |
| `test_wqp_normalisation.py` | Government CSV rows group into samples; an 8000 mg/L outlier doesn't move the median |
| `test_parser.py` | The final reaction step is reported, the whole path is kept |
| `test_runs_service.py` | Identical models run once; a timeout is recorded against the run, not lost |

---

## 5 · Break something on purpose

A test suite only means something once you've watched it fail. Open
`src/hgc/domain/units.py` and change the microgram multiplier:

```python
    "ug/l": 1.0,     # was 1e-3
```

Run the tests again. Exactly two fail, the direct unit conversion, and the input-builder
check that iron reaches PHREEQC as 0.045 rather than 45. That one character is the
thousand-fold error described in the README, and it's why those tests exist.

Undo (**Ctrl+Z** / **Cmd+Z**) and confirm you're back to 35 passed.

---

## 6 · Start the API

Press **F5** and choose **API (uvicorn, reload)**, the launch config sets the environment
for you on every platform, which is the easiest route on Windows.

By hand:

**macOS / Linux**

```bash
HGC_ENV=test PYTHONPATH=src uvicorn hgc.api.main:app --reload --port 8000
```

**Windows (PowerShell)**

```powershell
$env:HGC_ENV = "test"; $env:PYTHONPATH = "src"
uvicorn hgc.api.main:app --reload --port 8000
```

Then check two things:

- <http://localhost:8000/docs>, interactive API documentation, generated from the code
- <http://localhost:8000/readyz>, **503**, saying no PHREEQC databases were found

That 503 is correct, not a fault. The process is alive but can't do the one thing it
exists for, so it declines traffic. Stage 9 fixes it.

`HGC_ENV=test` keeps runs in memory, so no Postgres or Redis is needed yet.

---

## 7 · Poke it with the REST Client

Open `ops/requests.http`. With the REST Client extension installed, a **Send Request** link
appears above each block, identical on all three platforms, which is why it's there rather
than a page of `curl` commands. Two are worth doing carefully.

**The preview request** returns the PHREEQC input the system would run. Read it:

```
SOLUTION 1 USGS-06730200
    units     mg/l
    pH        7.4
    Alkalinity       250 as CaCO3
    Ca                88
    Fe             0.045
```

Iron went in at 45 µg/L and appears as `0.045`. Alkalinity kept its basis instead of being
silently treated as bicarbonate. That block is the entire point of the system; everything
else exists to produce it reliably.

**The hostile request** submits `DUMP -file /tmp/leak.txt`:

```
422  {"code": "unsafe_phreeqc_input",
      "detail": "DUMP is not permitted: it reads or writes files on the server"}
```

Named error, actionable message, no stack trace.

---

## 8 · Fetch real government data

Still in `ops/requests.http`:

```
GET localhost:8000/v1/sites?bbox=-105.3,39.9,-105.1,40.1&limit=5
GET localhost:8000/v1/sites/ready?source=wqp&bbox=-105.5,39.5,-104.5,40.5&start=2020-01-01
GET localhost:8000/v1/sites/USGS-09071750/samples?start=2015-01-01&end=2022-12-31
```

The first hits the WDFN OGC monitoring-locations API (site catalogue). The second finds sites
that actually carry the required chemistry via the USGS Samples API. The third pulls one such
site's analyses. All are live and occasionally slow; the client retries with backoff and caches
for six hours, so a repeat returns instantly.

> **USGS sites carry data again.** The app now reads USGS discrete chemistry from the Samples
> API, so `USGS-…` ids return real, coordinate-tagged analyses. (The old Water Quality Portal
> path, and its `…_WQX-…` state ids, has been retired from this app.)

Look at `readiness` in the samples response, the analyte count, the charge-balance
percentage, and any parameters missing for speciation. That's the honesty check running
before any model does.

---

## 9 · Turn on real PHREEQC

`phreeqpy` bundles the IPhreeqc binary for all three platforms, `IPhreeqc.dll` on Windows,
`.dylib` on macOS including Apple Silicon, `.so` on Linux, so there's nothing to compile.

```
pip install phreeqpy
```

Then fetch the thermodynamic databases:

**macOS / Linux**

```bash
./ops/fetch_databases.sh
```

**Windows (PowerShell)**

```powershell
.\ops\fetch_databases.ps1
```

Both write the databases into `ops/phreeqc-databases/` along with a `SHA256SUMS` file, and
fall back to a GitHub mirror if `water.usgs.gov` is blocked on your network.

Now create your `.env`:

**macOS / Linux**

```bash
cp .env.example .env
```

**Windows (PowerShell)**

```powershell
Copy-Item .env.example .env
```

and set the path inside it, forward slashes work on Windows too:

```
HGC_PHREEQC_DATABASE_DIR=./ops/phreeqc-databases
```

Press **F5** → **Smoke test (real PHREEQC run)**. Expected on any platform:

```
databases: ['llnl.dat', 'minteq.v4.dat', 'phreeqc.dat', 'pitzer.dat', 'wateq4f.dat']

status succeeded in 8 ms using phreeqpy/0.6.0
pH 7.40   pe 4.00   ionic strength 0.0100 mol/kgw   charge balance +2.7%

saturation indices
  Calcite     +0.15  supersaturated
  Dolomite    -0.11  undersaturated
  Siderite    -1.12  undersaturated
  Gypsum      -1.71  undersaturated

resubmitted identical model -> same run id: True
```

Read that last line: the second, identical submission returned the first run's identifier
without touching PHREEQC. That's the fingerprint cache.

The chemistry is sensible too, a limestone-country water sitting just above calcite
saturation and far below gypsum. If your numbers differ by more than about 0.01 you're on a
different database version, which is exactly why the checksum is recorded with every run.

Restart the API and `/readyz` returns 200 with the database list. The run requests in
`requests.http` now produce real saturation indices.

**macOS, first run only:** if you get *"cannot be opened because the developer cannot be
verified"*, Gatekeeper has quarantined the bundled library. Clear it and retry:

```bash
xattr -dr com.apple.quarantine "$(python -c 'import phreeqpy,os;print(os.path.dirname(phreeqpy.__file__))')"
```

---

## 10 · The user interface

```
pip install streamlit plotly pandas
```

Press **F5** → **Streamlit UI**, with the API still running in your other terminal. It opens
at <http://localhost:8501>.

Try **Find sites**, pick the *Data source* **Has chemistry (USGS Samples)**, and search a
Colorado bounding box. Click a result row to grab a `USGS-…` id, then **Model a sample** with
that id over a **ten-year** window (discrete sampling is sparse, so widen the range). You should
see the analysis count, the charge-balance metric, a warning if anything needed is missing, and a
saturation-index chart. Expand **PHREEQC input used**, the UI shows the exact input, because a
result you can't inspect is a result you can't put in a report.

`USGS-09071750` (Colorado River above Glenwood Springs) is a reliable one to start with.

---

## 11 · The full stack (optional)

```
docker compose up --build
```

Fetch the databases first (stage 9), the image copies them in. This adds Postgres, Redis
and a Celery worker, which is what the batch endpoint needs. UI on 8501, API on 8000.

Send the `/v1/batches` request with a handful of site identifiers and poll the returned
status URL: sites are processed independently, so one bad site reports as failed while the
rest complete.

**Windows:** Docker Desktop must be on the WSL2 backend, and the repo should live on the
Linux side or on a drive shared with Docker, or the bind mounts in `docker-compose.yml`
won't resolve. **macOS on Apple Silicon:** builds run natively; nothing special needed.

---

## 12 · Debugging tips

- **Set a breakpoint** on the first line of `build_solution_input`
  (`src/hgc/services/phreeqc/input_builder.py`) and send the preview request. Stepping
  through the conversions is the fastest way to understand the codebase.
- `"justMyCode": false` is already set in the launch configs, so you can step into FastAPI
  and pydantic when something surprises you.
- **Logs are JSON** by default. For readable local output, add `HGC_LOG_FORMAT=console` to
  `.env`.
- Breakpoints inside the PHREEQC child processes won't hit, they're separate processes on
  every platform. Test that layer through `RunService` with the fake engine in
  `tests/test_runs_service.py`.

---

## When something goes wrong

### Any platform

| Symptom | Cause and fix |
|---|---|
| `ModuleNotFoundError: No module named 'hgc'` | The terminal predates the interpreter selection. Close it, open a new one, confirm `.venv` is active. |
| `/readyz` returns 503 | Expected until stage 9. After it: check `HGC_PHREEQC_DATABASE_DIR` in `.env` points at a folder containing `phreeqc.dat`. |
| A script spawns itself repeatedly | Anything touching the engine needs an `if __name__ == "__main__":` guard, child processes re-import the entry point. See `ops/smoke_test.py`. This bites on Windows and macOS in particular, where spawning is the default. |
| USGS requests time out | The upstream services do go down. You'll get `503 upstream_unavailable`, never a silent empty result. Retry, or work from the fixtures in the tests. |
| Tests pass but the API won't start | Almost always `.env`, a malformed value fails validation at startup by design, and the error names the setting. |

### Windows

| Symptom | Fix |
|---|---|
| `HGC_ENV=test : The term ... is not recognized` | PowerShell needs `$env:HGC_ENV = "test"` on its own line first. |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`, then activate again. |
| `OSError` loading `IPhreeqc.dll` | 32-bit Python with the 64-bit DLL. Reinstall 64-bit Python, or install the Microsoft Visual C++ Redistributable. Easiest escape is `docker compose up`. |
| `Address already in use` on 8000 | `netstat -ano \| findstr :8000` then `taskkill /PID <pid> /F` |
| `make` not found | There's no `make` on Windows by default, every command in this guide is spelled out, so ignore the Makefile. |

### macOS

| Symptom | Fix |
|---|---|
| "developer cannot be verified" on the IPhreeqc library | Clear the quarantine attribute, command in stage 9. |
| `python3` is 3.9 | That's the system Python. `brew install python@3.12` and re-select the interpreter. |
| `xcrun: error: invalid active developer path` | `xcode-select --install` |
| `Address already in use` on 8000 | `lsof -ti:8000 \| xargs kill` |
