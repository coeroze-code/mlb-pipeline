"""
MLB Pipeline - phone-friendly dashboard.

Shows the latest CombinedResults.csv (batters + pitchers, ranked by Rating)
and lets you trigger a fresh run on demand from a button. Scheduled runs
happen separately via GitHub Actions (see .github/workflows/daily_run.yml) -
this app just displays whatever the most recent run produced, and can also
run the pipeline itself right now if you tap the button.
"""

import os
from datetime import datetime

import pandas as pd
import streamlit as st

import run_all

st.set_page_config(page_title="MLB Pipeline", page_icon="⚾", layout="wide")

# Make secrets available as environment variables, since the underlying
# pipeline scripts read keys via os.environ (works the same locally or here).
if "ODDS_API_KEYS" in st.secrets:
    os.environ["ODDS_API_KEYS"] = st.secrets["ODDS_API_KEYS"]

CSV_PATH = "CombinedResults.csv"

st.title("⚾ MLB Pipeline")

col1, col2 = st.columns([1, 3])
with col1:
    run_clicked = st.button("🔄 Run Pipeline Now", use_container_width=True, type="primary")

if run_clicked:
    with st.spinner("Fetching odds, computing projections, combining results..."):
        try:
            run_all.run_all()
            st.success("Done - results updated below.")
        except Exception as e:
            st.error(f"Pipeline run failed: {e}")

st.divider()

if os.path.exists(CSV_PATH):
    mtime = datetime.fromtimestamp(os.path.getmtime(CSV_PATH))
    st.caption(f"Last updated: {mtime.strftime('%Y-%m-%d %I:%M %p')}")

    df = pd.read_csv(CSV_PATH)

    type_filter = st.radio("Show", ["All", "Batter", "Pitcher"], horizontal=True, label_visibility="collapsed")
    if type_filter != "All":
        df = df[df["Type"] == type_filter]

    # Drop columns that are entirely empty for the current filter (e.g. hide
    # all the pitcher-only odds columns when viewing Batters only)
    df = df.dropna(axis=1, how="all")

    st.dataframe(df, use_container_width=True, hide_index=True, height=650)
    st.caption(f"{len(df)} players shown")
else:
    st.info("No results yet - tap 'Run Pipeline Now' above to generate the first set.")
