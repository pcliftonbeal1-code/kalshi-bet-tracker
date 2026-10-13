"""Combo (parlay) checker. READ-ONLY: it never places a bet.

Kalshi builds each combo on request and market makers quote it on the spot,
so there's no list of combo prices to scan. Instead: build the combo in the
Kalshi app, note the price it quotes, and ask this checker.

    python -m kalshi_bot.combo SEADEN                            # list this game's legs
    python -m kalshi_bot.combo SEADEN --legs 3y 7n --quote 0.21  # check a combo

The checker:
  1. GATE: every leg must be cheaper on Kalshi than its fair price after fees.
     Legs that fail mean "no combo" - expect that most nights.
  2. Prices the combo by simulating games where legs move together the way
     they did in real NFL games (correlation.py), then counts how often ALL hit.
  3. Compares that to Kalshi's quote after fees, flags legs that fight each
     other, and suggests a stake from your bankroll rules.
"""

import argparse
import csv
import math
import pickle
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import NormalDist

from kalshi_bot.alerts import DATA_DIR
from kalshi_bot.correlation import load_table, pair_key
from kalshi_bot.kalshi_client import KalshiMarket
from kalshi_bot.matching import Comparison, parse_event_ticker
from kalshi_bot.odds_math import kalshi_fee_per_contract
from kalshi_bot.teams import TEAM_CODES

# Kalshi series kind (config.toml [kalshi.series]) -> correlation variable.
# Rushing legs become "qb_rush" or "rb_rush" depending on the player.
KIND_TO_VAR = {"moneyline": "margin", "qb_pass_yds": "pass", "team_points": "points"}
SAME_THING = 0.999   # two legs on the same stat (e.g. 250+ and 275+): nearly locked together
CONFLICT = -0.05     # flag leg pairs at or below this correlation


@dataclass
class Leg:
    number: int
    ticker: str
    side: str                 # "yes" or "no"
    label: str                # "Josh Allen: 250+ passing yards"
    event: str                # Kalshi game code, e.g. "26OCT12BUFLAR"
    var: str                  # margin / points / pass / qb_rush / rb_rush
    team: str                 # full team name the stat belongs to
    who: str                  # player name, or team name for team stats
    fair_prob: float          # fair chance THIS SIDE wins
    kalshi_price: float       # Kalshi ask for this side
    edge_after_fees: float    # as a single bet
    method: str

    @property
    def sign(self) -> int:
        return 1 if self.side == "yes" else -1


# ---------- turning Kalshi markets into legs ----------

def _game(event_ticker: str) -> tuple[str, set[str]]:
    """("26OCT12BUFLAR", {"Buffalo Bills", "Los Angeles Rams"})."""
    parsed = parse_event_ticker(event_ticker)
    code = event_ticker.split("-")[1] if "-" in event_ticker else event_ticker
    if not parsed:
        return code, set()
    return code, {TEAM_CODES[parsed[1]], TEAM_CODES[parsed[2]]}


def player_team(segment: str, teams: set[str]) -> str | None:
    """Team from a player ticker segment: "SEASDARNOLD14" -> Seattle Seahawks.
    Longest code first, and it must be one of the two teams in this game."""
    for code in sorted(TEAM_CODES, key=len, reverse=True):
        if segment.upper().startswith(code) and TEAM_CODES[code] in teams:
            return TEAM_CODES[code]
    return None


def _player(market: KalshiMarket) -> str:
    return market.title.split(":")[0].strip()


def describe_leg(kind: str, market: KalshiMarket, qbs: set[str]) -> tuple[str, str, str] | None:
    """(variable, team, who) for one Kalshi market, or None if we can't tell."""
    _, teams = _game(market.event_ticker)
    parts = market.ticker.upper().split("-")
    if kind == "moneyline":
        team = TEAM_CODES.get(parts[-1])
        return ("margin", team, team) if team in teams else None
    if kind == "team_points":
        code = parts[-1].rstrip("0123456789")
        team = TEAM_CODES.get(code)
        return ("points", team, team) if team in teams else None
    if len(parts) < 3:
        return None
    team = player_team(parts[2], teams)
    if not team:
        return None
    who = _player(market)
    if kind == "qb_pass_yds":
        return "pass", team, who
    if kind == "rb_rush_yds":
        return ("qb_rush" if who in qbs else "rb_rush"), team, who
    return None


def build_legs(kalshi: dict[str, list[KalshiMarket]],
               comparisons: list[Comparison]) -> list[Leg]:
    """Every market we have a fair price for, both sides, numbered by market."""
    by_side = {(c.kalshi_ticker, c.side): c for c in comparisons}
    qbs = {_player(m) for m in kalshi.get("qb_pass_yds", [])}
    legs, number = [], 0
    for kind in ("moneyline", "team_points", "qb_pass_yds", "rb_rush_yds"):
        for m in sorted(kalshi.get(kind, []), key=lambda m: (m.title, m.floor_strike or 0)):
            info = describe_leg(kind, m, qbs)
            sides = [s for s in ("yes", "no") if (m.ticker, s) in by_side]
            if not info or not sides:
                continue
            number += 1
            var, team, who = info
            event, _ = _game(m.event_ticker)
            for s in sides:
                c = by_side[(m.ticker, s)]
                legs.append(Leg(number, m.ticker, s, f"{m.title} [{m.yes_sub_title}]".strip(),
                                event, var, team, who, c.fair_prob, c.kalshi_price,
                                c.edge_after_fees, c.method))
    return legs


# ---------- correlation and simulation ----------

def leg_correlation(a: Leg, b: Leg, table: dict) -> float:
    """How strongly two legs' underlying stats move together (before YES/NO)."""
    if a.event != b.event:
        return 0.0                                  # different games: independent
    if a.var == b.var and a.who == b.who:
        return SAME_THING                           # same stat, different threshold
    if a.team == b.team:
        if a.var == b.var:
            return 0.0                              # e.g. two RBs: no data, assume none
        return table["same"].get(pair_key(a.var, b.var), 0.0)
    return table["opp"].get(pair_key(a.var, b.var), 0.0)


def _cholesky(m: list[list[float]]) -> list[list[float]] | None:
    n = len(m)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = m[i][j] - sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                if s <= 1e-9:
                    return None
                low[i][j] = math.sqrt(s)
            else:
                low[i][j] = s / low[j][j]
    return low


def correlation_matrix(legs: list[Leg], table: dict) -> list[list[float]]:
    """Leg-by-leg correlation, with YES/NO flips applied (a NO leg wins when
    its stat is LOW, so its correlations change sign)."""
    n = len(legs)

    def rho(i: int, j: int) -> float:
        r = legs[i].sign * legs[j].sign * leg_correlation(legs[i], legs[j], table)
        # Exactly +/-1 (YES Bills + NO Rams) breaks the math; just under works.
        return max(-SAME_THING, min(SAME_THING, r))

    return [[1.0 if i == j else rho(i, j) for j in range(n)] for i in range(n)]


def simulate(legs: list[Leg], table: dict, sims: int = 50_000, seed: int = 7) -> float:
    """Chance ALL legs win, from `sims` simulated games.

    Each leg gets a bell-curve "stat" whose correlation with the others comes
    from the table; a leg wins when its stat clears the cutoff that makes its
    own chance equal its fair price. So each leg alone still wins exactly
    fair_prob of the time; only how they line up together changes.
    """
    m = correlation_matrix(legs, table)
    low = _cholesky(m)
    while low is None:   # estimated correlations can be slightly inconsistent: soften them
        m = [[v if i == j else v * 0.95 for j, v in enumerate(row)] for i, row in enumerate(m)]
        low = _cholesky(m)
    cutoffs = [NormalDist().inv_cdf(1 - leg.fair_prob) for leg in legs]
    rng = random.Random(seed)
    n, hits = len(legs), 0
    for _ in range(sims):
        z = [rng.gauss(0, 1) for _ in range(n)]
        if all(sum(low[i][k] * z[k] for k in range(i + 1)) > cutoffs[i] for i in range(n)):
            hits += 1
    return hits / sims


# ---------- the verdict ----------

@dataclass
class ComboResult:
    legs: list[Leg]
    quote: float
    fair_prob: float
    independent_prob: float     # what you'd get pretending legs don't affect each other
    sim_error: float            # +/- from simulation randomness (1 std error)
    fee: float
    roi_after_fees: float       # expected profit per $1 spent
    failed_gate: list[Leg]
    conflicts: list[tuple[Leg, Leg, float]] = field(default_factory=list)
    stake: float | None = None

    @property
    def verdict(self) -> str:
        if self.failed_gate:
            return "NO COMBO - at least one leg isn't cheaper than fair on Kalshi"
        return "VALUE" if self.roi_after_fees > 0 else "NO VALUE"


def evaluate(legs: list[Leg], quote: float, table: dict, fee_rate: float,
             min_leg_edge: float = 0.0, bankroll: float = 0.0, stake_pct: float = 0.005,
             sims: int = 50_000) -> ComboResult:
    fair = simulate(legs, table, sims)
    independent = math.prod(leg.fair_prob for leg in legs)
    fee = kalshi_fee_per_contract(quote, fee_rate)
    roi = (fair - quote - fee) / (quote + fee)
    conflicts = []
    m = correlation_matrix(legs, table)
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            if m[i][j] <= CONFLICT:
                conflicts.append((legs[i], legs[j], m[i][j]))
    return ComboResult(
        legs=legs, quote=quote, fair_prob=fair, independent_prob=independent,
        sim_error=math.sqrt(fair * (1 - fair) / sims), fee=fee, roi_after_fees=roi,
        failed_gate=[leg for leg in legs if leg.edge_after_fees < min_leg_edge],
        conflicts=conflicts,
        stake=round(bankroll * stake_pct, 2) if bankroll > 0 else None,
    )


def format_result(r: ComboResult, min_roi: float) -> str:
    lines = ["", "LEGS"]
    for leg in r.legs:
        gate = "ok  " if leg not in r.failed_gate else "FAIL"
        lines.append(f"  [{gate}] #{leg.number}{leg.side[0]} {leg.side.upper():<3} {leg.label[:48]:<48} "
                     f"Kalshi {leg.kalshi_price * 100:3.0f}c fair {leg.fair_prob * 100:5.1f}% "
                     f"net {leg.edge_after_fees * 100:+5.1f}")
    lines += ["",
              f"Kalshi quote: {r.quote * 100:.1f}c (+{r.fee * 100:.2f}c fee)",
              f"Fair combo:   {r.fair_prob * 100:.2f}% +/- {r.sim_error * 100:.2f} "
              f"(legs independent would be {r.independent_prob * 100:.2f}%)",
              f"Expected return after fees: {r.roi_after_fees * 100:+.1f}% per $1"]
    for a, b, rho in r.conflicts:
        strength = "strongly" if rho <= -0.25 else "mildly"
        lines.append(f"  FIGHT: #{a.number}{a.side[0]} and #{b.number}{b.side[0]} {strength} work against "
                     f"each other ({rho:+.2f}). One hitting makes the other less likely.")
    if r.failed_gate:
        lines.append(f"\nVERDICT: {r.verdict}. Skip it.")
    elif r.roi_after_fees >= min_roi:
        stake = f"${r.stake:.2f}" if r.stake is not None else "(set [bankroll] in config.toml)"
        lines.append(f"\nVERDICT: VALUE (clears the {min_roi * 100:.0f}% minimum). Max stake {stake}.")
        lines.append("Paper-trade only until CLV is proven.")
    else:
        lines.append(f"\nVERDICT: no bet - return {r.roi_after_fees * 100:+.1f}% is under the "
                     f"{min_roi * 100:.0f}% minimum.")
    return "\n".join(lines)


def log_combo(r: ComboResult, path=DATA_DIR / "combos.csv") -> None:
    """Every checked combo, so we can measure CLV later against closing prices."""
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["checked_at", "legs", "quote", "fee", "fair_prob", "independent_prob",
                        "roi_after_fees", "verdict", "leg_fair", "leg_prices"])
        w.writerow([datetime.now(timezone.utc).isoformat(),
                    " ".join(f"{leg.ticker}:{leg.side}" for leg in r.legs),
                    f"{r.quote:.4f}", f"{r.fee:.4f}", f"{r.fair_prob:.4f}",
                    f"{r.independent_prob:.4f}", f"{r.roi_after_fees:.4f}", r.verdict,
                    " ".join(f"{leg.fair_prob:.4f}" for leg in r.legs),
                    " ".join(f"{leg.kalshi_price:.4f}" for leg in r.legs)])


def pick_legs(legs: list[Leg], picks: list[str]) -> list[Leg]:
    """["3y", "7n"] -> those legs. Raises ValueError with a clear message."""
    chosen = []
    for p in picks:
        m = re.fullmatch(r"(\d+)([yn])", p.strip().lower())
        if not m:
            raise ValueError(f"{p!r}: use a number plus y or n, like 3y or 7n")
        side = "yes" if m.group(2) == "y" else "no"
        found = [leg for leg in legs if leg.number == int(m.group(1)) and leg.side == side]
        if not found:
            raise ValueError(f"{p!r}: no such leg (or no fair price for that side)")
        chosen.append(found[0])
    return chosen


# ---------- run it ----------

def _odds_for_game(code: str, cfg: dict, cache_minutes: float):
    """Sportsbook odds for ONE game, cached so re-checking combos is free.
    Uses the free /events list to find the game, then one paid call (~4 credits)."""
    from kalshi_bot.odds_client import OddsClient

    path = DATA_DIR / f"odds_{code}.pkl"
    if path.exists():
        saved = pickle.loads(path.read_bytes())
        if datetime.now(timezone.utc) - saved["at"] < timedelta(minutes=cache_minutes):
            return saved["outcomes"], None
    odds = OddsClient.from_config()
    parsed = parse_event_ticker(f"X-{code}")
    if not parsed:
        raise SystemExit(f"Can't read teams from {code!r}.")
    teams = {TEAM_CODES[parsed[1]], TEAM_CODES[parsed[2]]}
    event = next((e for e in odds.get_events() if {e["home_team"], e["away_team"]} == teams), None)
    if not event:
        raise SystemExit(f"The Odds API has no upcoming game for {' vs '.join(sorted(teams))}.")
    oc = cfg["odds_api"]
    outcomes = odds.get_event_odds(event["id"], oc["game_markets"] + oc["event_markets"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps({"at": datetime.now(timezone.utc), "outcomes": outcomes}))
    return outcomes, odds.credits_remaining


def main() -> None:
    from kalshi_bot.config import load_config
    from kalshi_bot.distributions import Shapes
    from kalshi_bot.fair_value import build_fair_lines
    from kalshi_bot.kalshi_client import KalshiClient
    from kalshi_bot.matching import match_all

    parser = argparse.ArgumentParser(description="Check a Kalshi combo (read-only).")
    parser.add_argument("game", help="matchup code from Kalshi tickers, e.g. SEADEN or 26OCT15SEADEN")
    parser.add_argument("--legs", nargs="+", help="legs to combine, e.g. 3y 7n 12y")
    parser.add_argument("--quote", type=float, help="Kalshi's combo price, e.g. 0.21 for 21c")
    args = parser.parse_args()
    if bool(args.legs) != (args.quote is not None):
        raise SystemExit("Give both --legs and --quote, or neither (to list legs).")

    cfg = load_config()
    combo_cfg, oc = cfg["combo"], cfg["odds_api"]
    code = args.game.upper()
    kalshi = KalshiClient.from_config().fetch_all_nfl(cfg["kalshi"]["series"])
    kalshi = {k: [m for m in ms if code in m.event_ticker.upper()] for k, ms in kalshi.items()}
    if not any(kalshi.values()):
        raise SystemExit(f"No open Kalshi markets match {code!r}.")
    full_code = next(m for ms in kalshi.values() for m in ms).event_ticker.split("-")[1]

    outcomes, credits = _odds_for_game(full_code, cfg, combo_cfg["odds_cache_minutes"])
    lines = build_fair_lines(outcomes, oc["sharp_book"], oc["fallback_books"], oc["min_fallback_books"])
    comps, _ = match_all(kalshi, lines, cfg["std_devs"], cfg["fees"]["taker_fee_rate"],
                         max_gap_sd=cfg["std_devs"].get("max_gap_sd", 0.5), shapes=Shapes.load())
    legs = build_legs(kalshi, comps)
    if credits is not None:
        print(f"(fresh sportsbook odds; {credits} credits left)")

    if not args.legs:
        good = [leg for leg in legs if leg.edge_after_fees >= combo_cfg["min_leg_edge"]]
        print(f"{'#':>4}  {'side':<4} {'market':<52} {'kalshi':>6} {'fair':>6} {'net':>6}")
        for leg in legs:
            mark = "  <- qualifies" if leg in good else ""
            print(f"{leg.number:>3}{leg.side[0]}  {leg.side:<4} {leg.label[:52]:<52} "
                  f"{leg.kalshi_price * 100:5.0f}c {leg.fair_prob * 100:5.1f}% "
                  f"{leg.edge_after_fees * 100:+5.1f}{mark}")
        print(f"\n{len(good)} of {len(legs)} legs qualify (cheaper than fair after fees).")
        if not good:
            print("No qualifying legs = no combo tonight. That's the bot doing its job.")
        return

    try:
        chosen = pick_legs(legs, args.legs)
    except ValueError as exc:
        raise SystemExit(str(exc))
    result = evaluate(chosen, args.quote, load_table(), cfg["fees"]["taker_fee_rate"],
                      combo_cfg["min_leg_edge"], cfg["bankroll"]["bankroll"],
                      cfg["bankroll"]["stake_pct"], combo_cfg["simulations"])
    print(format_result(result, combo_cfg["min_roi"]))
    log_combo(result)


if __name__ == "__main__":
    main()
