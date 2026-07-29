"""
Combine Batter + Pitcher Results
====================================
Merges RatingCalc.csv (batters, from mlb_pipeline.py) and
ProbablePitchers_<date>.csv (pitchers, from probable_pitchers.py) into one
CSV, so everyone - batters and pitchers - is ranked by Rating in one place.

Column layout:
    A: Player
    B: Type        <- "Batter" or "Pitcher"
    C: Rating
    D: Confidence   <- % of underlying betting lines backed by real odds
                       vs season-average fallbacks
    E onward: every other column from either source. A batter row leaves
    the pitcher-only columns blank, and vice versa.

Run this AFTER mlb_pipeline.py and probable_pitchers.py - run_pipeline.bat
already does this in the right order.

Output: CombinedResults.csv
"""

import csv
import glob
import os

FOLDER = os.path.dirname(os.path.abspath(__file__))
RATING_CSV = os.path.join(FOLDER, "RatingCalc.csv")
OUT_CSV = os.path.join(FOLDER, "CombinedResults.csv")

BATTER_COLUMNS = [
    "RBI_EV", "TB_EV", "TB_EV_Estimated", "Boost", "Slot1", "Slot2", "Slot3", "Slot4", "Slot5",
    "Expected_PA", "Expected_Outs", "Expected_Runs", "Expected_Walks", "Expected_SB",
]
PITCHER_COLUMNS = [
    "Team", "Opponent", "Games_Started", "IP_per_Start", "Outs_per_Start",
    "SO_per_Start", "BB_per_Start", "H_per_Start", "ERA_season",
    "Strikeouts", "Walks", "ER", "Outs", "Hits",
    "SO_Line", "SO_Over_Odds", "SO_Under_Odds",
    "BB_Line", "BB_Over_Odds", "BB_Under_Odds",
    "ER_Line", "ER_Over_Odds", "ER_Under_Odds",
    "Outs_Line", "Outs_Over_Odds", "Outs_Under_Odds",
    "Hits_Line", "Hits_Over_Odds", "Hits_Under_Odds",
    "Odds_Used_For",
]


def find_latest_pitchers_csv():
    """ProbablePitchers_<date>.csv is date-tagged - grab the most recent one."""
    candidates = sorted(
        glob.glob(os.path.join(FOLDER, "ProbablePitchers_*.csv")),
        key=os.path.getmtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_rows(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def rating_key(row):
    try:
        return float(row["Rating"])
    except (TypeError, ValueError):
        return float("-inf")


def main():
    batters = load_rows(RATING_CSV)
    pitchers_path = find_latest_pitchers_csv()
    pitchers = load_rows(pitchers_path)

    if not batters and not pitchers:
        print("Nothing to combine - run mlb_pipeline.py and/or probable_pitchers.py first.")
        return

    combined = []
    for row in batters:
        out = {
            "Player": row.get("Player", ""), "Type": "Batter",
            "Rating": row.get("Rating", ""), "Confidence": row.get("Confidence", ""),
        }
        for col in BATTER_COLUMNS:
            out[col] = row.get(col, "")
        for col in PITCHER_COLUMNS:
            out[col] = ""
        combined.append(out)

    for row in pitchers:
        out = {
            "Player": row.get("Player", ""), "Type": "Pitcher",
            "Rating": row.get("Rating", ""), "Confidence": row.get("Confidence", ""),
        }
        for col in BATTER_COLUMNS:
            out[col] = ""
        for col in PITCHER_COLUMNS:
            out[col] = row.get(col, "")
        combined.append(out)

    combined.sort(key=rating_key, reverse=True)

    fieldnames = ["Player", "Type", "Rating", "Confidence"] + BATTER_COLUMNS + PITCHER_COLUMNS
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(combined)

    print(f"Wrote {os.path.basename(OUT_CSV)} ({len(batters)} batters + {len(pitchers)} pitchers = {len(combined)} rows)")


if __name__ == "__main__":
    main()
