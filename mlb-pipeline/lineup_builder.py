"""
Lineup Builder - correlation/stacking-aware 5-man batting lineup optimizer.

Not part of the scheduled pipeline - this is purely interactive, called from
streamlit_app.py when a visitor clicks "Build Lineup". Everything here is
session-scoped: it operates on whatever Boost-adjusted Rating values the
caller passes in, and touches no shared files.

Model (deliberately simple and explainable, same "doesn't need to be sharp"
philosophy as the Rating formula elsewhere in this project):

  - Each batter's contribution in a slot = Rating * (Boost + slot_multiplier),
    the exact same formula as Slot1-5 everywhere else in this project.
  - For any FIXED set of 5 batters, the value-maximizing slot assignment is
    always to put the highest-Rating batter in Slot 1 (highest multiplier)
    down to the lowest-Rating batter in Slot 5 - true by the rearrangement
    inequality, so slot assignment never needs its own search once the 5
    players are chosen.
  - Stacking bonus: batters on the same team get a per-pair correlation
    credit that compounds with more teammates (2 batters = 1 pair, 3 = 3
    pairs, 4 = 6 pairs, 5 = 10 pairs) - reflects that when a team scores a
    lot of runs, several of its batters tend to benefit at once.
  - Risk: modeled as the lineup's combined standard deviation. Same-team
    batters are treated as correlated (using the same correlation value
    that drives the stack bonus, so both come from one consistent
    assumption); different-team batters are treated as independent. Each
    batter's own volatility is approximated from Confidence - less
    market-verified projections are assumed a bit less certain.
  - The two sliders control: how much correlation to assume between
    teammates (and therefore how much stacking is worth), and how heavily
    the resulting lineup's standard deviation gets penalized.

Search strategy: trying every 5-player combination out of a full slate is
computationally infeasible once the score depends on team grouping (and
unnecessary) - the bonus/risk terms only care about WHICH team is stacked
and HOW MANY of them, so the search only needs to try each team as a
candidate stack at each possible size, filling remaining slots with the
best players available elsewhere. A flat top-5-by-rating lineup (no team
constraint at all) is always included too, so stacking is only chosen when
it actually scores better.
"""

from collections import Counter
from itertools import combinations

# Tunable constants - approximations, not fitted to real data. Adjust freely.
MAX_CORRELATION = 0.6      # assumed same-team batter correlation at slider = 100
STACK_BONUS_SCALE = 0.15   # how much each correlated pair is worth, relative to the lineup's average value
RISK_PENALTY_SCALE = 0.35  # how large the stddev penalty can get, relative to the lineup's average value

# Must match mlb_pipeline.py / streamlit_app.py's SLOT_MULTIPLIERS (kept as a
# separate constant here rather than imported, so this module stays
# self-contained and independently testable).
SLOT_MULTIPLIERS = [2.0, 1.8, 1.6, 1.4, 1.2]  # index 0 = Slot1, ... index 4 = Slot5


def _sigma(confidence):
    """Approximate individual volatility from Confidence (0-100): 1.0 for a
    fully odds-verified player, up to 2.0 for a fully average-based one.
    Defensive against None/NaN/out-of-range values from a CSV round-trip."""
    try:
        conf = float(confidence)
        if conf != conf:  # NaN != NaN is True - catches NaN without needing math/pandas
            conf = 0.0
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(100.0, conf))
    return 1.0 + (1 - conf / 100.0)


def score_lineup(players, correlation_pct, risk_pct):
    """players: list of dicts with Player, Team, Rating, Boost, Confidence
    (exactly 5 of them). Returns a dict with the total score and its
    components, and the players in optimal slot order."""
    ordered = sorted(players, key=lambda p: p["Rating"], reverse=True)
    base_value = sum(
        p["Rating"] * (p["Boost"] + SLOT_MULTIPLIERS[i]) for i, p in enumerate(ordered)
    )
    avg_value = base_value / len(ordered) if ordered else 0.0

    rho = MAX_CORRELATION * (correlation_pct / 100.0)

    team_counts = Counter(p["Team"] for p in ordered)
    stack_bonus = 0.0
    for team, count in team_counts.items():
        if count >= 2:
            pairs = count * (count - 1) / 2
            stack_bonus += rho * pairs * avg_value * STACK_BONUS_SCALE

    sigmas = {p["Player"]: _sigma(p.get("Confidence")) for p in ordered}
    variance = sum(sigmas[p["Player"]] ** 2 for p in ordered)
    by_team = {}
    for p in ordered:
        by_team.setdefault(p["Team"], []).append(p["Player"])
    for team, members in by_team.items():
        if len(members) >= 2:
            for a, b in combinations(members, 2):
                variance += 2 * rho * sigmas[a] * sigmas[b]
    stddev = variance ** 0.5

    risk_weight = RISK_PENALTY_SCALE * (1 - risk_pct / 100.0) * avg_value
    risk_penalty = risk_weight * stddev

    total = base_value + stack_bonus - risk_penalty
    return {
        "players": ordered,  # already in optimal Slot1->Slot5 order
        "total_score": round(total, 3),
        "base_value": round(base_value, 3),
        "stack_bonus": round(stack_bonus, 3),
        "stddev": round(stddev, 3),
        "risk_penalty": round(risk_penalty, 3),
    }


def build_lineup(batters, correlation_pct, risk_pct):
    """batters: list of dicts (Player, Team, Rating, Boost, Confidence).
    Returns the best-scoring 5-man lineup found (a score_lineup() result),
    or None if fewer than 5 batters are available."""
    if len(batters) < 5:
        return None

    by_team = {}
    for b in batters:
        by_team.setdefault(b["Team"], []).append(b)
    for team in by_team:
        by_team[team].sort(key=lambda p: p["Rating"], reverse=True)

    candidates = []

    # Always include the flat top-5-by-rating lineup, with no team
    # constraint at all - the floor every stacked candidate has to beat.
    candidates.append(sorted(batters, key=lambda p: p["Rating"], reverse=True)[:5])

    # Try every (stack team, stack size) combination, filling remaining
    # slots with the best available players from OTHER teams.
    for team, team_players in by_team.items():
        max_stack = min(5, len(team_players))
        for stack_size in range(1, max_stack + 1):
            stack = team_players[:stack_size]
            remaining_needed = 5 - stack_size
            pool = [p for p in batters if p["Team"] != team]
            pool.sort(key=lambda p: p["Rating"], reverse=True)
            fill = pool[:remaining_needed]
            if len(stack) + len(fill) == 5:
                candidates.append(stack + fill)

    scored = [score_lineup(c, correlation_pct, risk_pct) for c in candidates]
    return max(scored, key=lambda s: s["total_score"])
