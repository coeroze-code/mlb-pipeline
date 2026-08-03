"""
MLB Pipeline - dashboard.

Shows the latest CombinedResults.csv (batters + pitchers, ranked by Rating).
Running the pipeline is restricted to whoever knows the admin password (set
via the ADMIN_PASSWORD secret) - everyone else can view results but not
trigger a run, since each run consumes your odds-api quota.

Boost is directly editable in the results table for both batters and
pitchers (Slot1-5 for batters, PitcherSlot for pitchers recalculate
automatically). The Lineup Builder below it uses those same live Boost
values to pick the best 5-player lineup from batters and pitchers combined
- there's no fixed split, and it factors in same-team stacking correlation
and lineup risk. All of this is entirely per-browser-session: Streamlit
gives every visitor their own isolated session_state on the server, so one
person's edits, sliders, and built lineup are never visible to, or
overwrite, anyone else's - and nothing here is written back to
CombinedResults.csv, so it never touches the shared data either.
"""

import hmac
import io
import os
from contextlib import redirect_stdout
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import lineup_builder
import run_all

FOLDER = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(FOLDER, "CombinedResults.csv")

# Same slot-multiplier pattern as mlb_pipeline.py's SLOT_MULTIPLIERS - kept
# in sync here so the live recompute below matches the pipeline's own math.
SLOT_MULTIPLIERS = {1: 2.0, 2: 1.8, 3: 1.6, 4: 1.4, 5: 1.2}
PITCHER_SLOT_MULTIPLIER = 1.0  # matches probable_pitchers.py's PITCHER_SLOT_MULTIPLIER

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
df = None
if os.path.exists(CSV_PATH):
    try:
        df = pd.read_csv(CSV_PATH)
        if df.empty:
            st.info("CombinedResults.csv is empty - an admin needs to run the pipeline.")
            df = None
    except Exception as e:
        st.error(f"CombinedResults.csv exists but couldn't be read ({e}). An admin should run the pipeline again.")
        df = None

if df is not None:
    batter_mask = df["Type"] == "Batter"
    if batter_mask.any() and "Expected_PA" in df.columns and (df.loc[batter_mask, "Expected_PA"].fillna(0) == 0).all():
        st.warning(
            "Every batter shows 0 for Expected PA/Outs/Runs/Walks/SB, which usually means "
            "MLB_-_Stats.csv wasn't found by the pipeline (wrong folder, or missing from the repo). "
            "Check the Admin section's Run log after your next run for the exact path it looked for."
        )

    mtime = datetime.fromtimestamp(os.path.getmtime(CSV_PATH), tz=ZoneInfo("America/New_York"))

    batters_n = int((df["Type"] == "Batter").sum())
    pitchers_n = int((df["Type"] == "Pitcher").sum())

    m1, m2, m3 = st.columns(3)
    m1.metric("Batters", batters_n)
    m2.metric("Pitchers", pitchers_n)
    m3.metric("Last Updated", mtime.strftime("%b %d, %I:%M %p") + f" {mtime.tzname()}")

    st.write("")
    type_filter = st.radio("Filter", ["All", "Batter", "Pitcher"], horizontal=True, label_visibility="collapsed")
    shown = df if type_filter == "All" else df[df["Type"] == type_filter]
    shown = shown.dropna(axis=1, how="all").sort_values("Rating", ascending=False)

    column_config = {}
    if "Confidence" in shown.columns:
        column_config["Confidence"] = st.column_config.NumberColumn(
            "Confidence", format="%d%%", help="% of the underlying betting lines backed by real odds vs season averages"
        )

    editing_enabled = "Boost" in shown.columns and "Player" in shown.columns
    if editing_enabled:
        # Editable Boost, with Slot1-5 (batters) / PitcherSlot (pitchers)
        # recalculated using it - private to this browser session only (see
        # module docstring for why that's safe).
        if "boosts" not in st.session_state:
            st.session_state["boosts"] = {}  # {player_name: boost_value}, survives filter switches

        # Apply whatever the user's last edit was (captured by Streamlit into
        # session_state before this script re-ran) into our stable, player-
        # keyed store - row positions shift when the filter changes, but
        # player names don't, so keying by name is what makes edits survive
        # switching between All/Batter/Pitcher.
        prior_state = st.session_state.get("results_editor", {})
        prior_edited_rows = prior_state.get("edited_rows", {}) if isinstance(prior_state, dict) else {}
        prev_player_order = st.session_state.get("_results_player_order", [])
        for row_idx_str, changes in prior_edited_rows.items():
            row_idx = int(row_idx_str)
            if "Boost" in changes and row_idx < len(prev_player_order):
                st.session_state["boosts"][prev_player_order[row_idx]] = changes["Boost"]

        working = shown.reset_index(drop=True).copy()
        working["Boost"] = working.apply(lambda r: st.session_state["boosts"].get(r["Player"], r["Boost"]), axis=1)
        boost_numeric = pd.to_numeric(working["Boost"], errors="coerce").fillna(0)
        rating_numeric = pd.to_numeric(working["Rating"], errors="coerce").fillna(0)

        if "Type" in working.columns:
            is_batter = working["Type"] == "Batter"
            is_pitcher = working["Type"] == "Pitcher"
        else:
            is_batter = pd.Series(True, index=working.index)
            is_pitcher = pd.Series(False, index=working.index)

        for n, mult in SLOT_MULTIPLIERS.items():
            col = f"Slot{n}"
            if col in working.columns:
                working.loc[is_batter, col] = (rating_numeric[is_batter] * (boost_numeric[is_batter] + mult)).round(4)

        if "PitcherSlot" in working.columns:
            working.loc[is_pitcher, "PitcherSlot"] = (
                rating_numeric[is_pitcher] * (boost_numeric[is_pitcher] + PITCHER_SLOT_MULTIPLIER)
            ).round(4)

        # Remember this run's row order so the NEXT run can map row positions back to players
        st.session_state["_results_player_order"] = working["Player"].tolist()

        column_config["Boost"] = st.column_config.NumberColumn(
            "Boost", step=0.1, format="%.2f",
            help="Edit this - Slot1-5 (batters) or PitcherSlot (pitchers) recalculate automatically. Private to your session only.",
        )
        st.caption("Boost is editable below - your edits are private to this session and aren't saved or shared with other visitors.")
        st.data_editor(
            working,
            use_container_width=True,
            hide_index=True,
            height=650,
            key="results_editor",
            disabled=[c for c in working.columns if c != "Boost"],
            num_rows="fixed",
            column_config=column_config,
        )
    else:
        st.dataframe(shown, use_container_width=True, hide_index=True, height=650, column_config=column_config)

    st.caption(f"{len(shown)} players shown")

    # ----------------------------------------------------------------------
    # Lineup Builder - correlation/stacking-aware, private to this session
    # ----------------------------------------------------------------------
    def _boosted_pool(type_name, cols):
        pool = df[df["Type"] == type_name][cols].copy()
        if "boosts" in st.session_state:
            pool["Boost"] = pool.apply(lambda r: st.session_state["boosts"].get(r["Player"], r["Boost"]), axis=1)
        for c in cols:
            if c not in ("Player", "Team", "Opponent"):
                pool[c] = pd.to_numeric(pool[c], errors="coerce")
        return pool

    batter_pool = _boosted_pool("Batter", ["Player", "Team", "Rating", "Boost", "Confidence"])
    batter_pool = batter_pool.dropna(subset=["Player", "Team", "Rating"])

    pitcher_pool = pd.DataFrame()
    if "Opponent" in df.columns:
        pitcher_pool = _boosted_pool("Pitcher", ["Player", "Team", "Opponent", "Rating", "Boost", "Confidence"])
        pitcher_pool = pitcher_pool.dropna(subset=["Player", "Rating"])

    st.divider()
    st.subheader("Lineup Builder")
    if len(batter_pool) + len(pitcher_pool) < 5:
        st.caption("Not enough players with data yet to build a lineup.")
    else:
        c1, c2 = st.columns(2)
        correlation_pct = c1.slider("Correlation (stacking)", 1, 100, 50, key="lineup_corr")
        risk_pct = c2.slider("Risk tolerance", 1, 100, 50, key="lineup_risk",
                              help="Higher = more willing to accept a volatile/stacked lineup for upside.")

        if st.button("Build Lineup", type="primary"):
            st.session_state["lineup_result"] = lineup_builder.build_best_lineup(
                batter_pool.to_dict("records"), pitcher_pool.to_dict("records"), correlation_pct, risk_pct
            )

        result = st.session_state.get("lineup_result")
        if result:
            lineup_rows = [
                {
                    "Slot": i + 1,
                    "Multiplier": p["_slot_multiplier"],
                    "Type": p["_type"],
                    "Player": p["Player"],
                    "Team": p["Team"],
                    "Rating": round(p["Rating"], 3),
                    "Boost": round(p.get("Boost", 0.0), 2),
                    "Slot Value": p["_slot_value"],
                }
                for i, p in enumerate(result["players"])
            ]
            st.dataframe(pd.DataFrame(lineup_rows), use_container_width=True, hide_index=True)
            n_batters = sum(1 for p in result["players"] if p["_type"] == "Batter")
            n_pitchers = sum(1 for p in result["players"] if p["_type"] == "Pitcher")
            st.caption(f"{n_batters} batter(s), {n_pitchers} pitcher(s) - chosen by the optimizer, not fixed.")

            lm1, lm2, lm3 = st.columns(3)
            lm1.metric("Total Score", result["total_score"])
            lm2.metric("Stack Bonus", f"+{result['stack_bonus']}")
            lm3.metric("Est. Std Dev", result["stddev"])
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
