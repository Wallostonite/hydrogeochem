"""HydroGeoChem Explorer — presentation layer.

Everything here is display and form handling. The UI never imports the engine, the
database, or the chemistry: it talks to the API, so what a scientist sees on screen is
exactly what a script would get back.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from hgc.ui.api_client import ApiClient, ApiError

st.set_page_config(page_title="HydroGeoChem Explorer", page_icon="◎", layout="wide")


@st.cache_resource
def get_client() -> ApiClient:
    return ApiClient()


@st.cache_data(ttl=600)
def load_catalog() -> dict:
    return get_client().catalog()


def show_error(exc: ApiError) -> None:
    """Errors name what happened and what to do; they do not apologise."""
    guidance = {
        "unsafe_phreeqc_input": "Remove the flagged keyword. File access is disabled on the server.",
        "phreeqc_timeout": "The model exceeded the time limit. Simplify it or submit it as a batch.",
        "phreeqc_error": "PHREEQC rejected the model. The message below is from the solver.",
        "upstream_unavailable": "USGS is not responding. Cached results are still available.",
        "rate_limited": "You have hit the request limit. Wait a minute and retry.",
    }.get(exc.code, "")
    st.error(f"{exc}\n\n{guidance}" if guidance else str(exc))


def num(value: object, fmt: str = ".2f", fallback: str = "n/a") -> str:
    """Format a number, tolerating None. PHREEQC can succeed yet leave pH/pe/mu unset."""
    return format(value, fmt) if isinstance(value, (int, float)) else fallback


# ----------------------------------------------------------------------- sidebar

st.sidebar.title("HydroGeoChem")
# Another view can request a page switch by setting _goto; apply it before the radio is
# built (a widget's state cannot be changed once it exists in the same run).
if "_goto" in st.session_state:
    st.session_state["nav"] = st.session_state.pop("_goto")
page = st.sidebar.radio(
    "View", ["Find sites", "Model a sample", "Run history"], label_visibility="collapsed", key="nav"
)

try:
    catalog = load_catalog()
except ApiError:
    st.sidebar.error("API unavailable")
    catalog = {"databases": ["phreeqc.dat"], "default_phases": ["Calcite", "Dolomite", "Gypsum"]}

database = st.sidebar.selectbox("Thermodynamic database", catalog["databases"])
st.sidebar.caption(
    "Saturation indices are only comparable within one database. The database and its "
    "checksum are recorded with every run."
)

# ----------------------------------------------------------------- find sites

if page == "Find sites":
    st.header("Find monitoring sites")

    # "nwis" is the WDFN OGC site catalogue (every monitoring location); "wqp" filters to
    # sites that actually carry the required chemistry (USGS Samples API); "synthetic" lists
    # whatever is seeded in the local database. ("wqp"/"nwis" kept as backend source keys.)
    SOURCES = {
        "Site catalog (USGS Water Data)": "nwis",
        "Has chemistry (USGS Samples)": "wqp",
        "Synthetic demo (local database)": "synthetic",
    }
    source_label = st.selectbox(
        "Data source",
        list(SOURCES),
        help="The catalog lists every monitoring location. 'Has chemistry' filters to sites "
        "with the required analytes (live USGS Samples query). Synthetic lists the seeded demo sites.",
    )
    source = SOURCES[source_label]
    provider = None

    state = bbox = None
    no_limit = False
    limit = 200
    start = end = None
    if source == "synthetic":
        st.caption("Lists the sites seeded into the local database. No filters needed.")
    else:
        col_state, col_bbox = st.columns([1, 3])
        state = col_state.text_input("State code", "CO", max_chars=2)
        bbox = col_bbox.text_input("Bounding box (west,south,east,north)", "")

        # Limit controls on both catalog and chemistry sources.
        col_nl, col_lim = st.columns([1, 3])
        no_limit = col_nl.checkbox("No limit", help="Return every matching site (can be a lot)")
        limit = col_lim.number_input("Max sites", 10, 5000, 200, step=10, disabled=no_limit)

        if source == "wqp":
            st.caption(
                "Finds USGS sites that carry the required analytes (Samples locations query). No "
                "date range needed here, that only matters when you model a site. A bounding box "
                "is fastest. Results carry coordinates, so they appear on the map."
            )
        else:
            st.caption(
                "The site catalogue lists every monitoring location in the area. 'Max sites' is "
                "applied at the source, so a smaller number loads faster."
            )

    if st.button("Search", type="primary"):
        client = get_client()
        capped = None if no_limit else int(limit)
        try:
            with st.spinner("Searching..."):
                if source == "nwis":
                    found = client.search_sites(
                        state=state or None, bbox=bbox or None, limit=capped,
                    )
                elif source == "synthetic":
                    found = client.ready_sites(source="synthetic")
                else:
                    found = client.ready_sites(
                        source="wqp", provider=provider,
                        state=state or None, bbox=bbox or None, limit=capped,
                    )
        except ApiError as exc:
            show_error(exc)
        else:
            # Persist so a row click (which reruns the script) does not clear the results.
            st.session_state["found_sites"] = found

    sites = st.session_state.get("found_sites")
    if sites is not None:
        if not sites:
            st.info("No sites matched. Widen the area or clear the state filter.")
        else:
            frame = pd.DataFrame(sites)
            st.success(f"{len(frame)} sites — click a row to grab its ID")

            # Only sources with coordinates get a map. WQP results carry none, so they show
            # a table only rather than an empty map and empty lat/lon columns.
            coord_cols = {"latitude", "longitude"}
            has_coords = coord_cols.issubset(frame.columns) and frame[
                ["latitude", "longitude"]
            ].notna().any(axis=None)
            if has_coords:
                located = frame.dropna(subset=["latitude", "longitude"])
                # Pass only the coordinate columns; list columns (analytes/missing) can trip
                # the map's Arrow serialization.
                st.map(located[["latitude", "longitude"]].rename(
                    columns={"latitude": "lat", "longitude": "lon"}
                ))

            # Drop all-empty columns (e.g. coords on WQP) and render list fields readably.
            display = frame.dropna(axis=1, how="all").copy()
            for col in ("analytes", "missing"):
                if col in display.columns:
                    display[col] = display[col].apply(
                        lambda v: ", ".join(v) if isinstance(v, list) else v
                    )

            event = st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
            )
            WARN_SITES = 15   # selecting more than this is slow (one live query per site)
            MAX_BUILD_SITES = 40  # hard cap so the combined build stays responsive

            picked = event.selection.rows if event.selection else []
            if picked:
                ids = [str(s) for s in display.iloc[picked]["site_id"].tolist()]
                st.divider()
                st.markdown(f"**{len(ids)} site(s) selected**")
                st.code("\n".join(ids), language=None)  # one-click copy of every id
                if len(ids) > WARN_SITES:
                    st.warning(
                        f"{len(ids)} sites selected. Each site is a separate live query, so a "
                        f"combined build will be slow. For bulk, prefer the CLI "
                        f"`build_ml_dataset.py --discover`. The build here is capped at "
                        f"{MAX_BUILD_SITES} sites."
                    )

                if len(ids) == 1 and st.button("Model this site →", type="primary"):
                    st.session_state["model_site_id"] = ids[0]
                    st.session_state["_goto"] = "Model a sample"
                    st.rerun()

                # Build one combined ML dataset across every selected site.
                with st.expander(f"Build ML dataset for {len(ids)} selected site(s)"):
                    c1, c2, c3 = st.columns(3)
                    ds_start = c1.date_input("From", date.today() - timedelta(days=365 * 10), key="ms_from")
                    ds_end = c2.date_input("To", date.today(), key="ms_to")
                    ds_bucket = c3.selectbox(
                        "Rows",
                        ["year", "quarter", "month", "event", "window"],
                        key="ms_bucket",
                        format_func=lambda b: {
                            "year": "per year", "quarter": "per quarter", "month": "per month",
                            "event": "per sampling event", "window": "one per site",
                        }[b],
                    )
                    targets = ids[:MAX_BUILD_SITES]
                    if st.button(f"Build dataset ({len(targets)} sites)"):
                        if len(ids) > MAX_BUILD_SITES:
                            st.info(
                                f"Building the first {MAX_BUILD_SITES} of {len(ids)} selected "
                                f"sites to stay responsive."
                            )
                        client = get_client()
                        records: list[dict] = []
                        progress = st.progress(0.0, text="Modelling selected sites...")
                        for i, sid in enumerate(targets):
                            try:
                                records.extend(
                                    client.dataset(sid, ds_start, ds_end,
                                                   database=database, bucket=ds_bucket)
                                )
                            except ApiError:
                                pass  # a site with no WQP data just contributes no rows
                            progress.progress((i + 1) / len(targets))
                        progress.empty()
                        if not records:
                            st.info("No modellable rows for the selected sites and window.")
                            st.session_state.pop("multi_dataset_csv", None)
                        else:
                            frame = pd.DataFrame(records)
                            order = ("id_", "in_", "out_", "si_", "meta_")
                            frame = frame[sorted(
                                frame.columns,
                                key=lambda c: next(
                                    ((i, c) for i, p in enumerate(order) if c.startswith(p)), (5, c)
                                ),
                            )]
                            st.session_state["multi_dataset_csv"] = frame.to_csv(index=False)
                            st.session_state["multi_rows"] = len(frame)
                            st.session_state["multi_sites"] = frame["id_site_id"].nunique()
                    if st.session_state.get("multi_dataset_csv"):
                        st.download_button(
                            f"Download combined dataset "
                            f"({st.session_state.get('multi_rows', 0)} rows, "
                            f"{st.session_state.get('multi_sites', 0)} sites)",
                            st.session_state["multi_dataset_csv"],
                            file_name="dataset_selected_sites.csv",
                            mime="text/csv",
                        )

# ------------------------------------------------------------- model a sample

elif page == "Model a sample":
    st.header("Model a sample")

    tab_fetch, tab_custom = st.tabs(["From a USGS site", "Custom PHREEQC input"])

    with tab_fetch:
        # Keyed so "Model this site" on the Find sites page can prefill it.
        st.session_state.setdefault("model_site_id", "USGS-09071750")
        site_id = st.text_input("Site identifier", key="model_site_id")
        col_a, col_b, col_c = st.columns(3)
        # Wide default window: the finder surfaces sites with data across all time, and a
        # single-site fetch is cheap, so cast a wide net and let the user narrow it.
        start = col_a.date_input("From", date.today() - timedelta(days=365 * 20))
        end = col_b.date_input("To", date.today())
        aggregate = col_c.selectbox("Combine analyses by", ["median", "mean", "latest"])
        phases = st.multiselect(
            "Report saturation indices for", catalog["default_phases"], catalog["default_phases"][:4]
        )
        equilibrate = st.multiselect("Equilibrate with", catalog["default_phases"], [])
        plot_series = st.checkbox(
            "Also plot a saturation-index time series",
            help="Runs one model per sample over the window, instead of only the aggregated "
            "representative. Slower, but shows how each mineral's SI moves through time.",
        )

        if st.button("Fetch and model", type="primary"):
            client = get_client()
            try:
                payload = client.samples(site_id, start, end, aggregate)
            except ApiError as exc:
                show_error(exc)
                st.stop()

            sample = payload.get("representative")
            if not sample:
                st.warning(f"No usable analyses at {site_id} in this window. Try a longer period.")
                st.stop()

            readiness = payload.get("readiness") or {}
            cols = st.columns(3)
            cols[0].metric("Analyses found", payload["count"])
            cols[1].metric("Analytes", readiness.get("measurement_count", 0))
            cols[2].metric("Charge balance", f"{readiness.get('charge_balance_pct', 0):+.1f}%")
            if readiness.get("missing_parameters"):
                st.warning(
                    "Missing from this analysis: "
                    + ", ".join(readiness["missing_parameters"])
                    + ". The model will still run, but treat the output as indicative."
                )

            request = {
                "sample": sample,
                "spec": {
                    "database": database,
                    "title": f"{site_id} {aggregate} {start}..{end}",
                    "saturation_phases": phases or catalog["default_phases"][:4],
                    "equilibrium_phases": [{"name": p} for p in equilibrate],
                },
            }
            try:
                preview = client.preview(request)
                st.session_state["input_text"] = preview["input_text"]
                run = client.create_run(request)
            except ApiError as exc:
                show_error(exc)
                st.stop()
            st.session_state["run"] = run

            # Optional: trace each phase's SI over time. A single WQP sample rarely carries
            # a full ion suite, so we bucket the window by year and model each bucket's
            # aggregate — complete analyses, and few enough runs to respect the rate limit.
            st.session_state["si_timeseries"] = None
            if plot_series and aggregate != "none":
                from datetime import timedelta

                buckets = min(max(end.year - start.year, 1), 8)
                span = (end - start).days
                rows: list[dict] = []
                progress = st.progress(0.0, text=f"Modelling {buckets} time buckets...")
                for i in range(buckets):
                    b_start = start + timedelta(days=span * i // buckets)
                    b_end = start + timedelta(days=span * (i + 1) // buckets)
                    try:
                        bucket = client.samples(site_id, b_start, b_end, aggregate)
                        rep = bucket.get("representative")
                        if rep:
                            one = client.create_run(
                                {
                                    "sample": rep,
                                    "spec": {
                                        "database": database,
                                        "saturation_phases": phases or catalog["default_phases"][:4],
                                    },
                                }
                            )
                            result = one.get("result")
                            if result:
                                mid = (b_start + (b_end - b_start) / 2).isoformat()
                                for si in result.get("saturation_indices", []):
                                    # -1000 is the engine's sentinel for an undefined SI
                                    # (the aggregate lacked an element the phase needs);
                                    # dropping it keeps the y-axis readable.
                                    if si["si"] > -999:
                                        rows.append(
                                            {"period": mid, "phase": si["phase"], "si": si["si"]}
                                        )
                    except ApiError:
                        continue  # a thin or unmodelable bucket should not sink the series
                    finally:
                        progress.progress((i + 1) / buckets)
                progress.empty()
                st.session_state["si_timeseries"] = rows

        # ML-ready export: one row per sample, inputs joined with model outputs.
        st.divider()
        st.caption(
            "Export a flat, ML-ready table for this site over the window above: inputs "
            "(analytes in mg/L) joined with the model outputs (pH, pe, ionic strength, and "
            "a saturation index per mineral)."
        )
        col_ds1, col_ds2 = st.columns([1, 2])
        ds_bucket = col_ds2.selectbox(
            "Rows",
            ["event", "month", "quarter", "year", "window"],
            format_func=lambda b: {
                "event": "one row per sampling event",
                "month": "one row per month (median)",
                "quarter": "one row per quarter (median)",
                "year": "one row per year (median)",
                "window": "one row (whole window)",
            }[b],
        )
        if col_ds1.button("Build dataset"):
            try:
                with st.spinner("Modelling every sample..."):
                    records = get_client().dataset(
                        site_id, start, end, database=database,
                        phases=phases or None, bucket=ds_bucket, aggregate=aggregate,
                    )
            except ApiError as exc:
                show_error(exc)
                records = None
            if records is not None:
                if not records:
                    st.info("No modellable samples in this window.")
                    st.session_state.pop("dataset_csv", None)
                else:
                    frame = pd.DataFrame(records)
                    order = ("id_", "in_", "out_", "si_", "meta_")
                    frame = frame[sorted(
                        frame.columns,
                        key=lambda c: next(((i, c) for i, p in enumerate(order) if c.startswith(p)), (5, c)),
                    )]
                    st.session_state["dataset_csv"] = frame.to_csv(index=False)
                    st.session_state["dataset_rows"] = len(frame)
                    st.session_state["dataset_site"] = site_id
        if st.session_state.get("dataset_csv"):
            st.download_button(
                f"Download dataset CSV ({st.session_state.get('dataset_rows', 0)} rows)",
                st.session_state["dataset_csv"],
                file_name=f"dataset_{st.session_state.get('dataset_site', 'site')}.csv",
                mime="text/csv",
            )

    with tab_custom:
        st.caption("Expert mode. Filesystem keywords are rejected; models run under a time limit.")
        default_input = "SOLUTION 1\n    units mg/l\n    pH 7.2\n    Ca 88\n    Alkalinity 210 as HCO3\nEND\n"
        raw = st.text_area("PHREEQC input", value=default_input, height=320)
        if st.button("Run input"):
            try:
                st.session_state["run"] = get_client().create_run(
                    {"raw_input": raw, "spec": {"database": database}}
                )
                st.session_state["input_text"] = raw
            except ApiError as exc:
                show_error(exc)

    run = st.session_state.get("run")
    if run:
        st.divider()
        st.subheader("Result")
        if run["status"] == "failed":
            st.error(f"{run.get('error_code')}: {run.get('error')}")
        elif run["status"] in ("queued", "running"):
            st.info(f"Run {run['id']} is {run['status']}. Check Run history in a moment.")
        else:
            result = run["result"]
            cols = st.columns(4)
            cols[0].metric("pH", num(result.get("ph"), ".2f"))
            cols[1].metric("pe", num(result.get("pe"), ".2f"))
            cols[2].metric("Ionic strength", f"{num(result.get('ionic_strength'), '.4f')} mol/kgw")
            cols[3].metric("Run time", f"{run.get('duration_ms') or 0} ms")

            si = pd.DataFrame(result["saturation_indices"])
            if not si.empty:
                figure = px.bar(
                    si.sort_values("si"),
                    x="si",
                    y="phase",
                    orientation="h",
                    color=si.sort_values("si")["si"].gt(0).map(
                        {True: "supersaturated", False: "undersaturated"}
                    ),
                    color_discrete_map={"supersaturated": "#1f6f8b", "undersaturated": "#c96f4a"},
                    labels={"si": "Saturation index", "phase": "", "color": ""},
                )
                figure.add_vline(x=0, line_dash="dash", line_color="#555")
                st.plotly_chart(figure, use_container_width=True)
                st.dataframe(si, use_container_width=True, hide_index=True)

            for warning in result.get("warnings", []):
                st.caption(f"· {warning}")

            with st.expander("PHREEQC input used"):
                st.code(st.session_state.get("input_text", ""), language="text")
            st.download_button(
                "Download results (CSV)",
                si.to_csv(index=False),
                file_name=f"si_{run['id'][:8]}.csv",
                mime="text/csv",
            )

    series = st.session_state.get("si_timeseries")
    if series:
        st.divider()
        st.subheader("Saturation-index time series")
        st.caption(
            "One PHREEQC model per yearly aggregate. A line above the dashed zero is "
            "supersaturated (the mineral tends to precipitate); below it, undersaturated."
        )
        ts = pd.DataFrame(series)
        ts["period"] = pd.to_datetime(ts["period"])
        figure = px.line(
            ts.sort_values("period"),
            x="period",
            y="si",
            color="phase",
            markers=True,
            labels={"period": "", "si": "Saturation index", "phase": ""},
        )
        figure.add_hline(y=0, line_dash="dash", line_color="#555")
        st.plotly_chart(figure, use_container_width=True)
        st.download_button(
            "Download time series (CSV)",
            ts.to_csv(index=False),
            file_name="si_timeseries.csv",
            mime="text/csv",
        )
    elif series == []:
        st.info("No yearly bucket in this window had enough analytes to model over time.")

# --------------------------------------------------------------- run history

else:
    st.header("Run history")
    run_id = st.text_input("Run identifier")
    if run_id:
        try:
            run = get_client().get_run(run_id)
        except ApiError as exc:
            show_error(exc)
        else:
            st.json(run)
    else:
        st.info("Enter a run identifier to reload its exact input, database, and results.")
