"""
MLB Player Prop Pipeline
========================
Single-script replacement for: curltest.py + jsontocsv.py + the "Format Tools"
Google Sheets Apps Script. No Google Sheets involved - everything is fetched,
converted, and computed locally, and written straight to CSV.

Run via run_pipeline.bat (recommended), or directly:
    python mlb_pipeline.py

Requires:
    pip install curl_cffi

Folder layout (everything lives in the same folder as this script, e.g.
C:\\daily-scraper\\MlbJson):

    MLB_-_Stats.csv   <- season stats export you already have. Keep this
                         updated yourself; the pipeline only reads it.

Outputs written to the same folder each run:

    RBI_<date>.json / TB_<date>.json  <- raw API responses (kept for reference)
    RBI.csv           <- every RBI prop line, with P1-P4/EV columns added
    TB.csv            <- every Total Bases prop line, with P1-P7/EV columns added
    RatingCalc.csv    <- final ranked player list (replaces the "Rating Calc" tab).
                         Boost is left as 0 for you to fill in manually - edit
                         the CSV and re-run if you want Slot3/4/5 to reflect it.

Formula notes
-------------
All the probability math below is a direct, line-for-line port of the
formulas in your Apps Script (addRbiProbabilityColumns, addTbProbabilityColumns,
copyUniquePlayerNames). Nothing about the math was changed - only where it
runs (Python instead of Sheets formulas).
"""

import csv
import json
import os
import unicodedata
from datetime import datetime

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
FOLDER = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(FOLDER, "MLB_-_Stats.csv")

RBI_CSV = os.path.join(FOLDER, "RBI.csv")
TB_CSV = os.path.join(FOLDER, "TB.csv")
RATING_CSV = os.path.join(FOLDER, "RatingCalc.csv")

BASE_URL = "https://sportsbook-nash.draftkings.com/sites/CA-ON-SB/api/sportscontent/controldata/league/leagueSubcategory/v1/markets"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://sportsbook.draftkings.com",
    "referer": "https://sportsbook.draftkings.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
}
COMMON_PARAMS = {"isBatchable": "false", "include": "Events", "entity": "events"}

RAW_FIELDS = [
    "matchup", "start_time_utc", "market", "market_type",
    "player_name", "team", "label", "milestone_value",
    "american_odds", "decimal_odds", "fractional_odds",
    "stat_prefix", "stat_value",
]


# --------------------------------------------------------------------------
# STEP 1: fetch odds from DraftKings  (replaces curltest.py)
# --------------------------------------------------------------------------
def fetch_market(name, template_vars, sub_id):
    try:
        from curl_cffi import requests
    except ImportError as e:
        raise RuntimeError(
            f"curl_cffi isn't available in this environment ({e}). "
            "The batter odds fetch needs it for TLS/browser impersonation; "
            "everything else in the pipeline doesn't depend on it."
        ) from e

    params = COMMON_PARAMS.copy()
    params.update({
        "templateVars": template_vars,
        "eventsQuery": f"$filter=leagueId eq '84240' AND clientMetadata/Subcategories/any(s: s/Id eq '{sub_id}')",
        "marketsQuery": f"$filter=clientMetadata/subCategoryId eq '{sub_id}' AND tags/all(t: t ne 'SportcastBetBuilder')",
    })
    r = requests.get(BASE_URL, params=params, headers=HEADERS, impersonate="chrome120")
    data = r.json()

    file_tag = datetime.now().strftime("%B%d")
    raw_path = os.path.join(FOLDER, f"{name}_{file_tag}.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"  Fetched {name}: saved raw response to {os.path.basename(raw_path)}")
    return data


# --------------------------------------------------------------------------
# STEP 2: JSON -> row dicts  (replaces jsontocsv.py)
# --------------------------------------------------------------------------
def json_to_rows(data):
    events_by_id = {e["id"]: e for e in data.get("events", [])}
    markets_by_id = {m["id"]: m for m in data.get("markets", [])}
    rows = []

    for sel in data.get("selections", []):
        market = markets_by_id.get(sel.get("marketId"))
        event = events_by_id.get(market.get("eventId")) if market else None
        market_name = market.get("name", "") if market else ""
        market_type = market.get("marketType", {}).get("name", "") if market else ""
        matchup = event.get("name", "") if event else ""
        start_time = event.get("startEventDate", "") if event else ""
        odds = sel.get("displayOdds", {})
        participants = sel.get("participants", [])

        if not participants:
            continue  # team-level bets have no player to rate against

        for p in participants:
            stat = p.get("statistic", {}) or {}
            rows.append({
                "matchup": matchup,
                "start_time_utc": start_time,
                "market": market_name,
                "market_type": market_type,
                "player_name": (p.get("name", "") or "").strip(),
                "team": p.get("venueRole", ""),
                "label": sel.get("label", ""),
                "milestone_value": sel.get("milestoneValue", ""),
                "american_odds": odds.get("american", ""),
                "decimal_odds": odds.get("decimal", ""),
                "fractional_odds": odds.get("fractional", ""),
                "stat_prefix": stat.get("prefix", ""),
                "stat_value": stat.get("stringValue", ""),
            })
    return rows


# --------------------------------------------------------------------------
# STEP 3: per-player probability math
# (replaces addRbiProbabilityColumns / addTbProbabilityColumns)
# --------------------------------------------------------------------------
def implied_prob(rows_by_label, label):
    """1 / decimal odds for this label, or None if that market wasn't offered.
    Mirrors: IFERROR(1/INDEX($K:$K, MATCH(1, ($F:$F=player)*($H:$H=label), 0)))
    """
    row = rows_by_label.get(label)
    if not row:
        return None
    try:
        d = float(row["decimal_odds"])
        return 1.0 / d if d else None
    except (TypeError, ValueError):
        return None


def compute_rbi_probs(rows):
    """Port of addRbiProbabilityColumns. Columns O:S -> P1,P2,P3,P4,EV."""
    by_player = {}
    for r in rows:
        by_player.setdefault(r["player_name"], {})[r["label"]] = r

    result = {}
    for player, by_label in by_player.items():
        p1 = implied_prob(by_label, "1+") or 0.0
        p2 = implied_prob(by_label, "2+") or 0.0
        p3_direct = implied_prob(by_label, "3+")
        p3 = p3_direct if p3_direct is not None else 0.52 * p2
        p4_direct = implied_prob(by_label, "4+")
        p4 = 1.35 * p4_direct if p4_direct is not None else 0.462 * p3
        ev = p1 + p2 + p3 + p4
        result[player] = {"P1": p1, "P2": p2, "P3": p3, "P4": p4, "EV": ev}
    return result


def compute_tb_probs(rows):
    """Port of addTbProbabilityColumns. Columns O:V -> P1..P7,EV."""
    by_player = {}
    for r in rows:
        by_player.setdefault(r["player_name"], {})[r["label"]] = r

    result = {}
    for player, by_label in by_player.items():
        p2 = implied_prob(by_label, "2+") or 0.0
        p1 = p2 / 0.65
        p3 = implied_prob(by_label, "3+") or 0.0
        p4 = implied_prob(by_label, "4+") or 0.0
        p5_direct = implied_prob(by_label, "5+")
        p5 = p5_direct if p5_direct is not None else 0.54 * p4
        p6_direct = implied_prob(by_label, "6+")
        p6 = p6_direct if p6_direct is not None else 0.57 * p5
        p7_direct = implied_prob(by_label, "7+")
        p7 = p7_direct if p7_direct is not None else 0.67 * p6
        ev = p1 + p2 + p3 + p4 + p5 + p6 + p7
        result[player] = {"P1": p1, "P2": p2, "P3": p3, "P4": p4, "P5": p5, "P6": p6, "P7": p7, "EV": ev}
    return result


def write_prop_csv(path, rows, probs, prob_cols):
    fieldnames = RAW_FIELDS + prob_cols
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            row_out = dict(r)
            player_probs = probs.get(r["player_name"], {})
            for col in prob_cols:
                row_out[col] = round(player_probs.get(col, 0), 4)
            w.writerow(row_out)
    print(f"  Wrote {os.path.basename(path)} ({len(rows)} rows)")


# --------------------------------------------------------------------------
# STEP 4: season stats lookup
# --------------------------------------------------------------------------
def normalize_name(name):
    """Strip accents/punctuation/case so odds-feed names ('Jose Ramirez') match
    stats-file names ('José Ramírez') reliably."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.replace(".", "").replace(",", "")
    return " ".join(name.split()).lower()


def load_stats():
    """Reads MLB_-_Stats.csv. Needs columns: Player, Team, G, PA, R, BB, SB, OBP.

    Handles two real-world wrinkles in Baseball-Reference-style exports:
      1. Player names have accents (Jose Ramirez vs Jose Ramirez) -> matched
         via normalize_name() instead of an exact string match.
      2. Players traded mid-season get MULTIPLE rows: one aggregate row
         (Team = "2TM", "3TM", etc.) plus one row per individual team. We
         keep the aggregate row so games/PA reflect the full season, not
         just one team's stint.
    """
    stats = {}
    if not os.path.exists(STATS_FILE):
        print(f"  WARNING: {os.path.basename(STATS_FILE)} not found - expected stats will default to 0.")
        return stats
    with open(STATS_FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Player") or "").strip()
            if not name:
                continue
            try:
                g = float(row.get("G") or 0)
                pa = float(row.get("PA") or 0)
                runs = float(row.get("R") or 0)
                bb = float(row.get("BB") or 0)
                sb = float(row.get("SB") or 0)
                obp = float(row.get("OBP") or 0)
            except ValueError:
                continue

            key = normalize_name(name)
            team = (row.get("Team") or "").strip()
            entry = {"G": g, "PA": pa, "R": runs, "BB": bb, "SB": sb, "OBP": obp, "_team": team}

            existing = stats.get(key)
            if existing is None:
                stats[key] = entry
            else:
                existing_is_agg = existing["_team"][:1].isdigit()
                new_is_agg = team[:1].isdigit()
                if new_is_agg and not existing_is_agg:
                    stats[key] = entry
                # else: keep whichever entry is already stored (prefers the
                # aggregate row since it's the first one Baseball-Reference lists)
    return stats


# --------------------------------------------------------------------------
# STEP 5: build RatingCalc.csv  (replaces copyUniquePlayerNames)
# --------------------------------------------------------------------------
def build_rating_calc(rbi_probs, tb_probs, stats):
    rows = []
    for player in sorted(rbi_probs.keys()):
        rbi_ev = rbi_probs[player]["EV"]

        tb_entry = tb_probs.get(player)
        tb_estimated = tb_entry is None
        tb_ev = tb_entry["EV"] if tb_entry else 2.53 * rbi_ev  # fallback, matches original

        s = stats.get(normalize_name(player))
        if s and s["G"] and s["PA"]:
            exp_pa = s["PA"] / s["G"]
            exp_outs = exp_pa * (1 - s["OBP"])
            exp_runs = exp_pa * (s["R"] / s["PA"])
            exp_walks = exp_pa * (s["BB"] / s["PA"])
            exp_sb = exp_pa * (s["SB"] / s["PA"])
        else:
            exp_pa = exp_outs = exp_runs = exp_walks = exp_sb = 0.0

        rating = (
            (0.47 * tb_ev)
            + (0.74 * rbi_ev)
            + (-0.12 * exp_outs)
            + (0.25 * exp_runs)
            + (0.12 * exp_walks)
            + (0.45 * exp_sb)
        )
        boost = 0.0  # fill this in manually per player, same as the old blank "Boost" column
        slot3 = rating * (boost + 1.6)
        slot4 = rating * (boost + 1.4)
        slot5 = rating * (boost + 1.2)

        rows.append({
            "Player": player,
            "RBI_EV": round(rbi_ev, 4),
            "TB_EV": round(tb_ev, 4),
            "TB_EV_Estimated": tb_estimated,  # True = no TB market found, EV is a fallback estimate
            "Rating": round(rating, 4),
            "Boost": boost,
            "Slot3": round(slot3, 4),
            "Slot4": round(slot4, 4),
            "Slot5": round(slot5, 4),
            "Expected_PA": round(exp_pa, 3),
            "Expected_Outs": round(exp_outs, 3),
            "Expected_Runs": round(exp_runs, 3),
            "Expected_Walks": round(exp_walks, 3),
            "Expected_SB": round(exp_sb, 3),
        })

    rows.sort(key=lambda r: r["Rating"], reverse=True)

    fieldnames = list(rows[0].keys()) if rows else [
        "Player", "RBI_EV", "TB_EV", "TB_EV_Estimated", "Rating", "Boost",
        "Slot3", "Slot4", "Slot5", "Expected_PA", "Expected_Outs",
        "Expected_Runs", "Expected_Walks", "Expected_SB",
    ]
    with open(RATING_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"  Wrote {os.path.basename(RATING_CSV)} ({len(rows)} players)")


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def main():
    print("Fetching odds from DraftKings...")
    try:
        rbi_json = fetch_market("RBI", "84240,17322", "17322")
        tb_json = fetch_market("TB", "84240,17321", "17321")
    except Exception as e:
        print(f"  Couldn't fetch DraftKings odds ({e}). Skipping batters this run.")
        return

    print("Converting + computing probabilities...")
    rbi_rows = json_to_rows(rbi_json)
    tb_rows = json_to_rows(tb_json)

    rbi_probs = compute_rbi_probs(rbi_rows)
    tb_probs = compute_tb_probs(tb_rows)

    write_prop_csv(RBI_CSV, rbi_rows, rbi_probs, ["P1", "P2", "P3", "P4", "EV"])
    write_prop_csv(TB_CSV, tb_rows, tb_probs, ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "EV"])

    print("Loading season stats...")
    stats = load_stats()

    print("Building RatingCalc.csv...")
    build_rating_calc(rbi_probs, tb_probs, stats)

    print("\nDone. Open RatingCalc.csv to see the ranked player list.")


if __name__ == "__main__":
    main()
