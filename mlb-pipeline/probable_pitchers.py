"""
MLB Probable Starting Pitchers - Odds-Based Projections (with average fallback)
=================================================================================

Replaces the FanDuel Research scrape AND the old Google Sheets odds pipeline
(fetchAllMLBDraftKingsPitcherProps + insertPitcherProjectionFormulas) entirely.
Everything happens locally, no Google Sheets involved.

What it does:
  1. Gets today's MLB schedule with probable pitchers for every game, from
     MLB's own public Stats API (statsapi.mlb.com).
  2. For each probable pitcher, pulls their full-season GAME LOG and averages
     ONLY over the individual games where they were the starting pitcher,
     discarding every relief appearance (see build_row for why this matters -
     it's the fix for spot-starters/bullpen games skewing the numbers).
     This becomes the FALLBACK projection for anyone without odds.
  3. Fetches DraftKings pitcher props (Strikeouts, Walks, Earned Runs, Outs,
     Hits Allowed) from the-odds-api.com, with the same API-key rotation
     trick as the original Apps Script (cycles through multiple free-tier
     keys since each has a limited monthly quota). The raw line and Over/Under
     prices for each market are written out as their own columns.
  4. For each stat, de-vigs the Over/Under odds at every line DraftKings
     offers, converts the resulting "true probability of going Over" into a
     projected stat value (assuming a normal distribution centered on the
     betting line), and averages across all lines for that player - this is
     a direct port of the LET/MAP/LAMBDA formula from insertPitcherProjectionFormulas.
     If a player has no odds (or an incomplete Over/Under pair) for a stat,
     it falls back to the season-average number from step 2.
  5. Computes the same Rating formula from the original Sheets script:
         Rating = 0.37*(Outs - Strikeouts) + 0.47*Strikeouts
                  - 0.3*Walks - 0.3*ER - 0.48*Hits
     using whichever of odds-based or average-based numbers ended up being
     used for each stat.

One bug fixed while porting: the original Sheets formula checked
"IF(odds < 10, treat as decimal, else convert from American)" to auto-detect
odds format - which was actually a reasonable heuristic, since the-odds-api
defaults to DECIMAL odds (almost always under 10) unless you explicitly
request oddsFormat=american. This script requests oddsFormat=american
explicitly on every odds call, so there's no ambiguity or format-guessing
needed at all - prices are always American, full stop.

Requires:
    pip install requests

Folder layout: put this next to mlb_pipeline.py, e.g. C:\\daily-scraper\\MlbJson
(MLB_-_Stats.csv is not needed by this script anymore)

Output (written to the same folder):
    ProbablePitchers_<date>.csv
"""

import csv
import os
import unicodedata
from datetime import datetime
from statistics import NormalDist

import requests

FOLDER = os.path.dirname(os.path.abspath(__file__))

TODAY = datetime.now().strftime("%Y-%m-%d")
SEASON = datetime.now().year
OUT_CSV = os.path.join(FOLDER, f"ProbablePitchers_{datetime.now().strftime('%B%d')}.csv")

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
PLAYER_STATS_URL = "https://statsapi.mlb.com/api/v1/people/{id}/stats"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}

# the-odds-api.com free-tier keys, rotated on failure/quota exhaustion - same
# trick as the original Apps Script. When running locally these fall back to
# the hardcoded list below; when running via GitHub Actions or Streamlit
# Cloud, set an ODDS_API_KEYS environment variable / secret as a comma-
# separated list and it'll be used instead (keeps keys out of the repo).
_env_keys = os.environ.get("ODDS_API_KEYS", "")
if _env_keys.strip():
    ODDS_API_KEYS = [k.strip() for k in _env_keys.split(",") if k.strip()]
else:
    ODDS_API_KEYS = [
        "6b91ab0419581fc5eec4bf6582527b94",
        "Da2dfc8ac5d454fe8bc1ded71c24208a",
        "A2b9facd382041a098c453542668d250",
        "faa04849a180501d6927fc6eae78efdc",
        "8457f5d08ee1ad037334e345c637ae35",
        "ce28f911f78fd94d209b20e60fcdb171",
        "391f62c863b720e8ff604b41f9ebb802",
    ]
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb"
PITCHER_MARKETS = [
    "pitcher_strikeouts",
    "pitcher_walks",
    "pitcher_earned_runs",
    "pitcher_outs",
    "pitcher_hits_allowed",
]
# Rating formula weights - Outs minus Strikeouts isolates "in-play" outs from
# strikeout outs so they can be weighted differently. Ported as-is from
# insertPitcherProjectionFormulas' column X formula.
RATING_WEIGHTS = {"non_k_out": 0.37, "strikeout": 0.47, "walk": -0.3, "er": -0.3, "hit": -0.48}

def normalize_name(name):
    """Strip accents/punctuation/case so names line up across MLB's Stats API
    and the-odds-api (same fix as the batter side in mlb_pipeline.py)."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.replace(".", "").replace(",", "")
    return " ".join(name.split()).lower()


def fetch_with_key_rotation(url, params):
    """Tries each odds-api key in turn until one works. Mirrors
    fetchWithKeyRotation from the original Apps Script."""
    last_error = None
    for i, key in enumerate(ODDS_API_KEYS):
        try:
            p = dict(params)
            p["apiKey"] = key
            r = requests.get(url, params=p, timeout=15)
            if r.status_code == 200:
                return r.json()
            last_error = f"Key {i + 1} failed ({r.status_code})"
        except Exception as e:
            last_error = str(e)
    raise RuntimeError(f"All odds-api keys exhausted. Last error: {last_error}")


def ip_to_outs(ip_str):
    """MLB innings-pitched notation is NOT decimal: '142.1' means 142 innings
    plus 1 out (1/3 of an inning), '142.2' means 142 innings plus 2 outs."""
    if not ip_str:
        return 0
    s = str(ip_str)
    if "." in s:
        whole, frac = s.split(".", 1)
    else:
        whole, frac = s, "0"
    whole = int(whole) if whole else 0
    frac = int(frac) if frac else 0
    return whole * 3 + frac


# --------------------------------------------------------------------------
# STEP 1: today's probable starters
# --------------------------------------------------------------------------
def get_probable_pitchers():
    """Returns a list of dicts: player_id, name, team, opponent."""
    params = {"sportId": 1, "date": TODAY, "hydrate": "probablePitcher,team"}
    r = requests.get(SCHEDULE_URL, params=params, headers=REQUEST_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()

    pitchers = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            teams = game.get("teams", {})
            away, home = teams.get("away", {}), teams.get("home", {})
            away_sp, home_sp = away.get("probablePitcher"), home.get("probablePitcher")
            away_team = away.get("team", {}).get("abbreviation", "")
            home_team = home.get("team", {}).get("abbreviation", "")

            if away_sp:
                pitchers.append({
                    "player_id": away_sp["id"],
                    "name": away_sp.get("fullName", ""),
                    "team": away_team,
                    "opponent": home_team,
                })
            if home_sp:
                pitchers.append({
                    "player_id": home_sp["id"],
                    "name": home_sp.get("fullName", ""),
                    "team": home_team,
                    "opponent": away_team,
                })

    if not pitchers:
        print("  No probable pitchers returned - either no games today, or MLB hasn't announced starters yet.")
    return pitchers


# --------------------------------------------------------------------------
# STEP 2: pitching line per pitcher - STARTS ONLY
# --------------------------------------------------------------------------
def get_start_only_stats(player_id):
    """Returns the average pitching line for this pitcher's STARTS ONLY this
    season - not his season totals.

    Why: season totals (IP, SO, BB, H) include EVERY appearance, starts and
    relief both. That's fine for a full-time starter, but plenty of today's
    probable starters are actually bullpen/swingman arms making a rare spot
    start (openers, injury fill-ins, "bullpen games") - for them, gamesStarted
    might be 1 while gamesPitched is 30+, so season IP / gamesStarted grossly
    overstates what they'll actually do in a start (e.g. a reliever with 1
    start and 38 innings across 35 relief outings showed up as "38 IP/start").

    Fix: pull the full game log and only average over the individual games
    where stat.gamesStarted == 1 for that game, throwing out every relief
    appearance entirely. This gives a true "per start" number even for
    part-time starters.
    """
    params = {
        "stats": "gameLog",
        "group": "pitching",
        "season": SEASON,
        "sportId": 1,
        "gameType": "R",
    }
    r = requests.get(PLAYER_STATS_URL.format(id=player_id), params=params, headers=REQUEST_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()

    totals = {"outs": 0.0, "strikeOuts": 0.0, "baseOnBalls": 0.0, "hits": 0.0, "earnedRuns": 0.0}
    starts = 0
    for stat_group in data.get("stats", []):
        for split in stat_group.get("splits", []):
            stat = split.get("stat", {})
            if not stat or int(stat.get("gamesStarted") or 0) != 1:
                continue  # relief appearance in this game - skip it
            starts += 1
            totals["outs"] += ip_to_outs(stat.get("inningsPitched"))
            totals["strikeOuts"] += float(stat.get("strikeOuts") or 0)
            totals["baseOnBalls"] += float(stat.get("baseOnBalls") or 0)
            totals["hits"] += float(stat.get("hits") or 0)
            totals["earnedRuns"] += float(stat.get("earnedRuns") or 0)

    if starts == 0:
        return {}

    innings = totals["outs"] / 3.0
    era = round((totals["earnedRuns"] * 9.0 / innings), 2) if innings > 0 else 0.0

    return {
        "gamesStarted": starts,
        "outs": totals["outs"],
        "strikeOuts": totals["strikeOuts"],
        "baseOnBalls": totals["baseOnBalls"],
        "hits": totals["hits"],
        "era": era,
    }


# --------------------------------------------------------------------------
# STEP 3: DraftKings pitcher props, from the-odds-api.com
# (replaces fetchAllMLBDraftKingsPitcherProps)
# --------------------------------------------------------------------------
def fetch_pitcher_props():
    """Pulls every DraftKings pitcher-prop line currently offered for
    upcoming MLB events. Returns a flat list of dicts."""
    events = fetch_with_key_rotation(f"{ODDS_API_BASE}/events", {})

    props = []
    for event in events:
        try:
            odds_json = fetch_with_key_rotation(
                f"{ODDS_API_BASE}/events/{event['id']}/odds",
                {"regions": "us", "markets": ",".join(PITCHER_MARKETS), "oddsFormat": "american"},
            )
        except Exception as e:
            print(f"    Skipped event {event.get('id')}: {e}")
            continue

        dk = next((b for b in odds_json.get("bookmakers", []) if b.get("title") == "DraftKings"), None)
        if not dk:
            continue

        for market in dk.get("markets", []):
            if not market.get("key", "").startswith("pitcher_"):
                continue
            for outcome in market.get("outcomes", []):
                if outcome.get("point") is None or outcome.get("price") is None:
                    continue
                props.append({
                    "player": outcome.get("description", ""),
                    "market": market["key"],
                    "bet_type": outcome.get("name", ""),
                    "point": outcome.get("point"),
                    "odds": outcome.get("price"),
                })

    print(f"  Pulled {len(props)} DraftKings pitcher prop lines across {len(events)} events.")
    return props


def build_odds_lookup(props):
    """{(normalized_player_name, market): {point: {"Over": odds, "Under": odds}}}"""
    lookup = {}
    for p in props:
        key = (normalize_name(p["player"]), p["market"])
        lookup.setdefault(key, {}).setdefault(p["point"], {})[p["bet_type"]] = p["odds"]
    return lookup


# --------------------------------------------------------------------------
# STEP 5: de-vig odds into a projected stat value per market
# (replaces the LET/MAP/LAMBDA formulas in insertPitcherProjectionFormulas)
# --------------------------------------------------------------------------
_NORMAL = NormalDist()


def american_to_decimal(odds):
    odds = float(odds)
    return 1 + odds / 100.0 if odds > 0 else 1 + 100.0 / abs(odds)


def project_stat_from_odds(player_name, market, odds_lookup):
    """For every line DraftKings offers, de-vigs Over/Under into a 'true'
    probability of going Over, converts that into a projected stat value
    assuming a normal distribution centered on the line (same approach as
    the original formula: line + NORMSINV(trueProb) * sqrt(line)), then
    averages across all lines. Returns None if there's no odds at all, or if
    any line is missing one side of Over/Under - same fail-to-fallback
    behavior as the original Sheets formula."""
    lines = odds_lookup.get((normalize_name(player_name), market))
    if not lines:
        return None

    projected_values = []
    for point, sides in lines.items():
        if "Over" not in sides or "Under" not in sides:
            return None
        over_dec = american_to_decimal(sides["Over"])
        under_dec = american_to_decimal(sides["Under"])
        over_imp = 1.0 / over_dec
        under_imp = 1.0 / under_dec
        true_prob = over_imp / (over_imp + under_imp)
        true_prob = min(max(true_prob, 0.001), 0.999)  # keep inv_cdf well-defined at the extremes
        z = _NORMAL.inv_cdf(true_prob)
        projected_values.append(point + z * (max(point, 0.1) ** 0.5))

    return sum(projected_values) / len(projected_values) if projected_values else None


def format_market_odds(player_name, market, odds_lookup):
    """Returns (line, over_odds, under_odds) as display strings for the raw
    odds columns. If DraftKings offers more than one line for a market,
    they're joined with '/' (rare for these 5 markets, but handled)."""
    lines = odds_lookup.get((normalize_name(player_name), market))
    if not lines:
        return "", "", ""
    points = sorted(lines.keys())
    line_str = "/".join(str(pt) for pt in points)
    over_str = "/".join(str(lines[pt].get("Over", "")) for pt in points)
    under_str = "/".join(str(lines[pt].get("Under", "")) for pt in points)
    return line_str, over_str, under_str


def compute_rating(strikeouts, outs, walks, er, hits):
    """Direct port of the Rating formula from column X of insertPitcherProjectionFormulas."""
    w = RATING_WEIGHTS
    return (
        w["non_k_out"] * (outs - strikeouts)
        + w["strikeout"] * strikeouts
        + w["walk"] * walks
        + w["er"] * er
        + w["hit"] * hits
    )


def apply_odds_and_rating(row, player_name, odds_lookup):
    """Fills in the raw odds columns, final Strikeouts/Walks/ER/Outs/Hits
    (odds-based if available, else the average-based fallback already
    sitting in `row`), and Rating."""
    era_val = row["ERA_season"]
    try:
        era_val = float(era_val)
    except (TypeError, ValueError):
        era_val = 0.0
    er_fallback = era_val * row["IP_per_Start"] / 9.0 if row["IP_per_Start"] else 0.0

    # market key -> (fallback value, short column prefix)
    markets = {
        "pitcher_strikeouts": (row["SO_per_Start"], "SO"),
        "pitcher_walks": (row["BB_per_Start"], "BB"),
        "pitcher_earned_runs": (er_fallback, "ER"),
        "pitcher_outs": (row["Outs_per_Start"], "Outs"),
        "pitcher_hits_allowed": (row["H_per_Start"], "Hits"),
    }

    final = {}
    used_odds_for = []
    for market, (fallback_value, prefix) in markets.items():
        line, over_odds, under_odds = format_market_odds(player_name, market, odds_lookup)
        row[f"{prefix}_Line"] = line
        row[f"{prefix}_Over_Odds"] = over_odds
        row[f"{prefix}_Under_Odds"] = under_odds

        odds_value = project_stat_from_odds(player_name, market, odds_lookup)
        if odds_value is not None:
            final[market] = odds_value
            used_odds_for.append(prefix)
        else:
            final[market] = fallback_value

    row["Strikeouts"] = round(final["pitcher_strikeouts"], 2)
    row["Walks"] = round(final["pitcher_walks"], 2)
    row["ER"] = round(final["pitcher_earned_runs"], 2)
    row["Outs"] = round(final["pitcher_outs"], 2)
    row["Hits"] = round(final["pitcher_hits_allowed"], 2)
    row["Rating"] = round(
        compute_rating(row["Strikeouts"], row["Outs"], row["Walks"], row["ER"], row["Hits"]), 2
    )
    row["Confidence"] = round(len(used_odds_for) / len(markets) * 100)
    row["Odds_Used_For"] = ",".join(used_odds_for) if used_odds_for else "none - averages only"
    return row


# --------------------------------------------------------------------------
# STEP 6: build the average-based fallback row
# --------------------------------------------------------------------------
def build_row(pitcher, stat):
    games_started = stat.get("gamesStarted", 0.0)
    outs_total = stat.get("outs", 0.0)
    so_total = stat.get("strikeOuts", 0.0)
    bb_total = stat.get("baseOnBalls", 0.0)
    h_total = stat.get("hits", 0.0)
    era = stat.get("era", "")

    if games_started > 0:
        outs_per_start = outs_total / games_started
        so_per_start = so_total / games_started
        bb_per_start = bb_total / games_started
        h_per_start = h_total / games_started
    else:
        outs_per_start = so_per_start = bb_per_start = h_per_start = 0.0

    if games_started == 1:
        print(f"  NOTE: {pitcher['name']} has only 1 tracked MLB start this season - "
              f"today's projection is based on that single outing, so treat it as a rough guess.")
    elif outs_per_start / 3 > 9:
        print(f"  WARNING: {pitcher['name']} still shows {outs_per_start / 3:.2f} IP/start across "
              f"{int(games_started)} start(s) - unusual, worth a manual look.")

    return {
        "Player": pitcher["name"],
        "Team": pitcher["team"],
        "Opponent": pitcher["opponent"],
        "Games_Started": int(games_started),
        "IP_per_Start": round(outs_per_start / 3, 2),
        "Outs_per_Start": round(outs_per_start, 1),
        "SO_per_Start": round(so_per_start, 2),
        "BB_per_Start": round(bb_per_start, 2),
        "H_per_Start": round(h_per_start, 2),
        "ERA_season": era,
    }


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def main():
    print(f"Fetching probable pitchers for {TODAY}...")
    pitchers = get_probable_pitchers()
    if not pitchers:
        print("\nNothing to project today.")
        return

    print(f"Fetching starts-only pitching stats for {len(pitchers)} probable starters...")
    rows = []
    for p in pitchers:
        try:
            stat = get_start_only_stats(p["player_id"])
        except Exception as e:
            print(f"  Skipping {p['name']} - stats lookup failed: {e}")
            continue
        if not stat:
            print(f"  {p['name']} has no tracked MLB starts this season (likely a debut/rare call-up) - skipping projection.")
            continue
        rows.append((p["name"], build_row(p, stat)))

    print("Fetching DraftKings pitcher props (the-odds-api.com)...")
    try:
        props = fetch_pitcher_props()
        odds_lookup = build_odds_lookup(props)
    except Exception as e:
        print(f"  Couldn't fetch odds ({e}) - every pitcher will use the average-based fallback instead.")
        odds_lookup = {}

    print("Applying odds (with average fallback) and computing Rating...")
    final_rows = [apply_odds_and_rating(row, name, odds_lookup) for name, row in rows]

    fieldnames = [
        "Player", "Team", "Opponent", "Games_Started",
        "IP_per_Start", "Outs_per_Start", "SO_per_Start", "BB_per_Start", "H_per_Start", "ERA_season",
        "SO_Line", "SO_Over_Odds", "SO_Under_Odds",
        "BB_Line", "BB_Over_Odds", "BB_Under_Odds",
        "ER_Line", "ER_Over_Odds", "ER_Under_Odds",
        "Outs_Line", "Outs_Over_Odds", "Outs_Under_Odds",
        "Hits_Line", "Hits_Over_Odds", "Hits_Under_Odds",
        "Strikeouts", "Walks", "ER", "Outs", "Hits", "Rating", "Confidence", "Odds_Used_For",
    ]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        final_rows.sort(key=lambda r: r["Rating"], reverse=True)
        w.writerows(final_rows)

    print(f"\nWrote {os.path.basename(OUT_CSV)} ({len(final_rows)} pitchers).")


if __name__ == "__main__":
    main()
