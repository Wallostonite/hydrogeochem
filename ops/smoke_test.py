"""End-to-end check: build a sample, run PHREEQC for real, print the chemistry.

Run it after installing phreeqpy and fetching the databases:

    HGC_ENV=test PYTHONPATH=src python ops/smoke_test.py

The `if __name__ == "__main__"` guard is required, not decorative: the engine spawns
child processes, and a spawned child re-imports the entry point. A script without the
guard would recursively launch itself.
"""

from __future__ import annotations

from hgc.api.deps import build_container
from hgc.domain.models import Measurement, ModelSpec, RunRequest, WaterSample

SAMPLE = WaterSample(
    site_id="USGS-06730200",
    measurements=[
        Measurement(key="ph", value=7.4, unit="std units"),
        Measurement(key="temperature", value=12.5, unit="deg C"),
        Measurement(key="ca", value=88, unit="mg/l"),
        Measurement(key="mg", value=24, unit="mg/l"),
        Measurement(key="na", value=15, unit="mg/l"),
        Measurement(key="cl", value=12, unit="mg/l"),
        Measurement(key="so4", value=64, unit="mg/l"),
        Measurement(key="alk_caco3", value=250, unit="mg/l"),
        Measurement(key="fe", value=45, unit="ug/l"),
    ],
)


def main() -> None:
    container = build_container()
    print("databases:", sorted(container.engine.verify_databases()))

    spec = ModelSpec(saturation_phases=("Calcite", "Dolomite", "Gypsum", "Siderite"))
    run = container.runs.submit(RunRequest(sample=SAMPLE, spec=spec))

    print(f"\nstatus {run.status} in {run.duration_ms} ms using {run.engine_version}")
    if run.result is None:
        print("error:", run.error_code, run.error)
        return

    result = run.result
    print(
        f"pH {result.ph:.2f}   pe {result.pe:.2f}   "
        f"ionic strength {result.ionic_strength:.4f} mol/kgw   "
        f"charge balance {result.charge_balance_pct:+.1f}%"
    )
    print("\nsaturation indices")
    for si in result.saturation_indices:
        print(f"  {si.phase:<12}{si.si:+.2f}  {si.state}")
    for note in result.warnings:
        print(f"  note: {note}")

    again = container.runs.submit(RunRequest(sample=SAMPLE, spec=spec))
    print(f"\nresubmitted identical model -> same run id: {again.id == run.id}")

    container.engine.shutdown()


if __name__ == "__main__":
    main()
