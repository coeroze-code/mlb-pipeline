"""
Lineup Builder - correlation/stacking-aware 5-player lineup optimizer.

Not part of the scheduled pipeline - this is purely interactive, called from
streamlit_app.py when a visitor clicks "Build Lineup". Everything here is
session-scoped: it operates on whatever Boost-adjusted Rating values the
caller passes in, and touches no shared files.

Picks the best 5 players TOTAL from the combined batter + pitcher pool - no
fixed split, and the 5 slot multipliers [2.0, 1.8, 1.6, 1.4, 1.2] are shared
across both types rather than reserved for batters.

Model (deliberately simple and explainable, same "doesn't need to be sharp"
philosophy as the Rating formula elsewhere in this project):

  - Every chosen player's contribution = Rating * (Boost + slot_multiplier).
    For a FIXED set of 5 players (any mix of batters/pitchers), the value-
    maximizing slot assignment is always to rank them ALL by Rating and
    assign the largest multiplier to the highest-Rating player, down to the
    smallest multiplier for the lowest - true by the rearrangement
    inequality regardless of type, so slot assignment never needs its own
    search once the 5 players are chosen.
  - In practice this usually means pitchers land in the top slots (2.0,
    1.8) since they tend to carry higher raw Ratings, while batters occupy
    the lower slots (1.6 down to 1.2) and make up ground through Boost -
    matching how this is typically played in practice, and something the
    optimizer arrives at naturally rather than needing to be told.
  - Stacking bonus: BATTERS on the same team get a per-pair correlation
    credit that compounds with more teammates (2 batters = 1 pair, 3 = 3
    pairs, 4 = 6 pairs, 5 = 10 pairs) - reflects that when a team scores a
    lot of runs, several of its batters tend to benefit at once. Pitchers
    aren't part of this - a pitcher doesn't share his own team's batters'
    same-game correlation in the same way.
  - Risk: modeled as the lineup's combined standard deviation. Same-team
    batters are treated as correlated (using the same correlation value
    that drives the stack bonus); everyone else (different-team batters,
    all pitchers) is treated as independent. Each player's own volatility
    is approximated from Confidence - less market-verified projections are
    assumed a bit less certain.
  - The two sliders control: how much correlation to assume between
    teammates (and therefore how much stacking is worth), and how heavily
    the resulting lineup's standard deviation gets penalized.
  - One pitcher-specific consideration: when picking pitchers, prefer one
    who ISN'T facing a team you've stacked batters from - if your batters do
    well, that pitcher probably didn't, so rostering him works against you.
  - A two-way player (e.g. Ohtani, on a day he's both hitting and pitching)
    can appear in both the batter and pitcher pools - he's never allowed to
    fill two lineup spots at once, only one or the other.

Search strategy: since batters and pitchers contribute independently to the
stacking/risk terms (no batter-pitcher correlation is modeled, only slot
VALUE is shared), the search tries every possible split of the 5 roster
spots between how many batters vs pitchers to include (0-5 either way),
uses the same team-stacking search as before to pick which batters for
whatever count that split calls for, takes the best available pitchers for
whatever count it calls for (as a ranking heuristic - see
_best_pitcher_group), and then scores the resulting 5-player group with the
shared, rank-based slot assignment described above. The best-scoring split
wins.
"""

from collections import Counter
from itertools import combinations

# Tunable constants - approximations, not fitted to real data. Adjust freely.
MAX_CORRELATION = 0.6      # assumed same-team batter correlation at slider = 100
STACK_BONUS_SCALE = 0.15   # how much each correlated pair is worth, relative to the lineup's average value
RISK_PENALTY_SCALE = 0.8   # how large the stddev penalty can get, relative to the lineup's average value

SLOT_MULTIPLIERS = [2.0, 1.8, 1.6, 1.4, 1.2]  # shared across both types, assigned purely by Rating rank
AVERAGE_MULTIPLIER = sum(SLOT_MULTIPLIERS) / len(SLOT_MULTIPLIERS)  # 1.6 - used only as a candidate-
                                                                       # selection heuristic (see
                                                                       # _best_pitcher_group), never for
                                                                       # final scoring, which always uses
                                                                       # the real rank-based assignment
LINEUP_SIZE = 5


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


def _value(p, multiplier):
    return p.get("Rating", 0.0) * (p.get("Boost", 0.0) + multiplier)


def score_combo(batters_chosen, pitchers_chosen, correlation_pct, risk_pct):
    """batters_chosen + pitchers_chosen together must total LINEUP_SIZE.
    Returns a dict with the total score, its components, and a single
    "players" list already in slot order (index 0 = Slot with multiplier
    2.0, etc.) - each entry tagged with "_type" ("Batter"/"Pitcher"),
    "_slot_multiplier", and "_slot_value" for easy display."""
    tagged = [dict(p, _type="Batter") for p in batters_chosen] + [dict(p, _type="Pitcher") for p in pitchers_chosen]
    ordered = sorted(tagged, key=lambda p: p["Rating"], reverse=True)
    for i, p in enumerate(ordered):
        p["_slot_multiplier"] = SLOT_MULTIPLIERS[i]
        p["_slot_value"] = round(_value(p, SLOT_MULTIPLIERS[i]), 3)
    base_value = sum(p["_slot_value"] for p in ordered)

    n = len(ordered)
    avg_value = base_value / n if n else 0.0

    rho = MAX_CORRELATION * (correlation_pct / 100.0)

    # Stacking bonus - batters on the same team only
    team_counts = Counter(p["Team"] for p in batters_chosen)
    stack_bonus = 0.0
    for team, count in team_counts.items():
        if count >= 2:
            pairs = count * (count - 1) / 2
            stack_bonus += rho * pairs * avg_value * STACK_BONUS_SCALE

    sigmas = {p["Player"]: _sigma(p.get("Confidence")) for p in ordered}
    variance = sum(sigmas[p["Player"]] ** 2 for p in ordered)
    by_team = {}
    for p in batters_chosen:
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
        "players": ordered,
        "total_score": round(total, 3),
        "base_value": round(base_value, 3),
        "stack_bonus": round(stack_bonus, 3),
        "stddev": round(stddev, 3),
        "risk_penalty": round(risk_penalty, 3),
    }


def _best_batter_group(batters, k, correlation_pct, risk_pct):
    """Best k-sized batter group (team-stacking-aware). Returns None if
    there aren't enough batters; [] if k == 0. The comparison here uses
    score_combo with an empty pitcher list, which - since slot assignment
    is rank-based - correctly reduces to ranking these k batters among
    themselves, same as if pitchers didn't exist."""
    if k == 0:
        return []
    if len(batters) < k:
        return None

    by_team = {}
    for b in batters:
        by_team.setdefault(b["Team"], []).append(b)
    for team in by_team:
        by_team[team].sort(key=lambda p: p["Rating"], reverse=True)

    candidates = [sorted(batters, key=lambda p: p["Rating"], reverse=True)[:k]]  # flat top-k baseline

    for team, team_players in by_team.items():
        max_stack = min(k, len(team_players))
        for stack_size in range(1, max_stack + 1):
            stack = team_players[:stack_size]
            remaining_needed = k - stack_size
            pool = [p for p in batters if p["Team"] != team]
            pool.sort(key=lambda p: p["Rating"], reverse=True)
            fill = pool[:remaining_needed]
            if len(stack) + len(fill) == k:
                candidates.append(stack + fill)

    def batter_only_score(group):
        return score_combo(group, [], correlation_pct, risk_pct)["total_score"]

    return max(candidates, key=batter_only_score)


def _best_pitcher_group(pitchers, n, excluded_teams, excluded_players=frozenset()):
    """Top n pitchers by value, preferring ones not facing a stacked team
    where possible, and never a player already used as a batter in this
    candidate (a two-way player like Ohtani can appear in both pools on a
    day he's both hitting and pitching - he can only occupy one lineup
    spot). Ranked using AVERAGE_MULTIPLIER as a stand-in since we don't yet
    know which slot each will actually land in (that's decided later,
    globally, in score_combo) - a higher-Rating pitcher is a better pick in
    any slot, so this ranking is a safe proxy for candidate selection even
    though it isn't the final scoring formula. Returns [] if n == 0, None if
    not enough pitchers exist at all."""
    if n == 0:
        return []

    available = [p for p in pitchers if p.get("Player") not in excluded_players]
    if len(available) < n:
        return None

    eligible = [p for p in available if p.get("Opponent") not in excluded_teams]
    pool = eligible if len(eligible) >= n else available
    return sorted(pool, key=lambda p: _value(p, AVERAGE_MULTIPLIER), reverse=True)[:n]


def build_best_lineup(batters, pitchers, correlation_pct, risk_pct):
    """batters/pitchers: lists of player dicts (Player, Team, Rating, Boost,
    Confidence; pitchers also need Opponent). Returns the best-scoring
    5-player lineup found across every possible batter/pitcher split, or
    None if fewer than 5 players are available combined."""
    if len(batters) + len(pitchers) < LINEUP_SIZE:
        return None

    best = None
    for num_pitchers in range(0, LINEUP_SIZE + 1):
        num_batters = LINEUP_SIZE - num_pitchers
        if num_batters > len(batters) or num_pitchers > len(pitchers):
            continue

        batter_group = _best_batter_group(batters, num_batters, correlation_pct, risk_pct)
        if batter_group is None:
            continue

        batter_teams = {p["Team"] for p in batter_group}
        batter_names = {p["Player"] for p in batter_group}
        pitcher_group = _best_pitcher_group(pitchers, num_pitchers, batter_teams, batter_names)
        if pitcher_group is None:
            continue

        scored = score_combo(batter_group, pitcher_group, correlation_pct, risk_pct)
        if best is None or scored["total_score"] > best["total_score"]:
            best = scored

    return best
