"""
MLB Pipeline - dashboard.

Shows the latest CombinedResults.csv (batters + pitchers, ranked by Rating).
Running the pipeline is restricted to whoever knows the admin password (set
via the ADMIN_PASSWORD secret) - everyone else can view results but not
trigger a run, since each run consumes your odds-api quota.

The "Adjust your boosts" section lets any visitor try their own Boost values
and see recomputed Slot1-5 numbers. This is entirely per-browser-session:
Streamlit gives every visitor their own isolated session_state on the
server, so one person's edits are never visible to, or overwrite, anyone
else's - and nothing here is written back to CombinedResults.csv, so it
never touches the shared data either.
"""

import hmac
import io
import os
from contextlib import redirect_stdout
from datetime import datetime

import pandas as pd
import streamlit as st

import run_all

FOLDER = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(FOLDER, "CombinedResults.csv")

# Same slot-multiplier pattern as mlb_pipeline.py's SLOT_MULTIPLIERS - kept
# in sync here so the live recompute below matches the pipeline's own math.
SLOT_MULTIPLIERS = {1: 2.0, 2: 1.8, 3: 1.6, 4: 1.4, 5: 1.2}

st.set_page_config(page_title="MLB Pipeline", layout="wide")

# Make secrets available as environment variables, since the underlying
# pipeline scripts read keys via os.environ (works the same locally or here).
if "ODDS_API_KEYS" in st.secrets:
    os.environ["ODDS_API_KEYS"] = st.secrets["ODDS_API_KEYS"]

st.markdown(
    """
    <style>
        .block-container {padding-top: 2.5rem; padding-bottom: 2rem; max-width: 1200px;}
        h1 {font-weight: 600; letter-spacing: -0.02em;}
        [data-testid="stMetricValue"] {font-size: 1.4rem;}
        .stDataFrame {border: 1px solid #e6e6e6; border-radius: 6px;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("MLB Pipeline")
st.caption("Batter and pitcher projections, ranked by Rating.")

# --------------------------------------------------------------------------
# Results table
# --------------------------------------------------------------------------
if os.path.exists(CSV_PATH):
    df = pd.read_csv(CSV_PATH)
    mtime = datetime.fromtimestamp(os.path.getmtime(CSV_PATH))

    batters_n = int((df["Type"] == "Batter").sum())
    pitchers_n = int((df["Type"] == "Pitcher").sum())

    m1, m2, m3 = st.columns(3)
    m1.metric("Batters", batters_n)
    m2.metric("Pitchers", pitchers_n)
    m3.metric("Last Updated", mtime.strftime("%b %d, %I:%M %p"))

    st.write("")
    type_filter = st.radio("Filter", ["All", "Batter", "Pitcher"], horizontal=True, label_visibility="collapsed")
    shown = df if type_filter == "All" else df[df["Type"] == type_filter]
    shown = shown.dropna(axis=1, how="all").sort_values("Rating", ascending=False)

    column_config = {}
    if "Confidence" in shown.columns:
        column_config["Confidence"] = st.column_config.NumberColumn(
            "Confidence", format="%d%%", help="% of the underlying betting lines backed by real odds vs season averages"
        )

    st.dataframe(shown, use_container_width=True, hide_index=True, height=650, column_config=column_config)
    st.caption(f"{len(shown)} players shown")

    # ----------------------------------------------------------------------
    # Adjust your boosts - per-session only, not saved anywhere shared
    # ----------------------------------------------------------------------
    batters_df = df[df["Type"] == "Batter"][["Player", "Rating", "Boost"]].copy()
    if not batters_df.empty:
        st.divider()
        st.subheader("Adjust your boosts")
        st.caption(
            "Edit Boost for any batter below - Slot 1 through 5 recalculate immediately. "
            "This is private to your session: nobody else visiting this site sees your edits, "
            "and they aren't saved anywhere once you close the page."
        )

        edited = st.data_editor(
            batters_df[["Player", "Boost"]],
            use_container_width=True,
            hide_index=True,
            disabled=["Player"],
            key="boost_editor",
        )

        result = edited.merge(batters_df[["Player", "Rating"]], on="Player", how="left")
        for n, mult in SLOT_MULTIPLIERS.items():
            result[f"Slot{n}"] = (result["Rating"] * (result["Boost"].fillna(0) + mult)).round(4)

        st.dataframe(
            result[["Player", "Boost", "Slot1", "Slot2", "Slot3", "Slot4", "Slot5"]],
            use_container_width=True,
            hide_index=True,
        )
else:
    st.info("No results yet. An admin needs to run the pipeline at least once.")

st.divider()

# --------------------------------------------------------------------------
# Admin-only: run the pipeline
# --------------------------------------------------------------------------
with st.expander("Admin"):
    admin_password = st.secrets.get("ADMIN_PASSWORD", "")
    if not admin_password:
        st.warning("No ADMIN_PASSWORD secret is set, so this section can't be unlocked. Add one in app settings.")
    else:
        entered = st.text_input("Password", type="password")
        unlocked = bool(entered) and hmac.compare_digest(entered, admin_password)

        if entered and not unlocked:
            st.error("Incorrect password.")

        if unlocked:
            run_clicked = st.button("Run Pipeline Now", type="primary")
            if run_clicked:
                log = io.StringIO()
                with st.spinner("Fetching odds, computing projections, combining results..."):
                    try:
                        with redirect_stdout(log):
                            run_all.run_all()
                        st.session_state["last_run_status"] = "success"
                    except Exception as e:
                        st.session_state["last_run_status"] = f"error: {e}"
                st.session_state["last_run_log"] = log.getvalue()
                st.rerun()

            if "last_run_status" in st.session_state:
                status = st.session_state["last_run_status"]
                if status == "success":
                    st.success("Pipeline run complete.")
                else:
                    st.error(f"Pipeline run failed: {status.replace('error: ', '')}")
                with st.expander("Run log", expanded=(status != "success")):
                    st.code(st.session_state.get("last_run_log", "") or "(no output)")
