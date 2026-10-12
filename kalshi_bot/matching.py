"""Match each Kalshi market to its sportsbook line and compute the edge.

The three questions for every Kalshi market:
  1. WHICH GAME?    Read the teams + date from Kalshi's event ticker
                    ("KXNFLGAME-26OCT18KCBUF" -> KC at BUF on Oct 18), then find
                    the sportsbook game with those teams within a day of that date.
  2. WHICH LINE?    Moneyline -> the team in the ticker suffix ("...-KC").
                    Props / team totals -> find the player/team name, and the
                    threshold ("250+"), then the closest book line for them.
  3. WHAT'S FAIR?   Exact match if Kalshi's threshold equals the book line
                    (Kalshi "278+" == book "Over 277.5"); otherwise the bell-curve
                    estimate from odds_math.prob_at_least (std devs = ESTIMATES).

Anything we can't match is returned in an "unmatched" list WITH A REASON,
so you can see exactly what to fix instead of it silently disappearing.
"""

import argparse
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kalshi_bot.fair_value import FairLine
from kalshi_bot.kalshi_client import KalshiMarket
from kalshi_bot.odds_math import edge, edge_after_fees, prob_at_least
from kalshi_bot.teams import TEAM_CODES, split_matchup

# Kalshi series name (the keys in config.toml [kalshi.series]) -> Odds API market key.
# The same names are used for the std-dev estimates in config.toml [std_devs].
KIND_TO_BOOK_MARKET = {
    "moneyline": "h2h",
    "qb_pass_yds": "player_pass_yds",
    "rb_rush_yds": "player_rush_yds",
    "team_points": "team_totals",
}


@dataclass
class Comparison:
    """One side (YES or NO) of one Kalshi market, compared to the books."""

    kalshi_ticker: str
    kalshi_title: str
    side: str                   # "yes" or "no"
    kalshi_price: float         # the ASK: what you'd pay for this side
    fair_prob: float            # conservative fair probability for this side
    edge: float                 # fair_prob - kalshi_price
    edge_after_fees: float
    method: str                 # "exact" or "line-adjusted (sd=70, ESTIMATE)"
    book_line: str              # e.g. "Patrick Mahomes Over 277.5"
    book_source: str            # "pinnacle" or "avg(draftkings,fanduel)"
    book_odds: str              # the raw prices used
    book_fetched_at: datetime
    book_updated_at: datetime | None
    event_id: str
    commence_time: datetime
    book_market: str            # Odds API market key, e.g. "h2h" (to re-pull just that)


@dataclass
class Unmatched:
    kalshi_ticker: str
    reason: str


# ---------- step 1: which game? ----------

_EVENT_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})(?:\d{4})?([A-Z]+)$")


def parse_event_ticker(event_ticker: str) -> tuple[datetime, str, str] | None:
    """"KXNFLGAME-26OCT18KCBUF" -> (2026-10-18, "KC", "BUF"). None if unreadable."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    m = _EVENT_RE.match(parts[1])
    if not m:
        return None
    try:
        date = datetime.strptime(m.group(1).title(), "%y%b%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    teams = split_matchup(m.group(2))
    if not teams:
        return None
    return date, teams[0], teams[1]


def find_game_lines(market: KalshiMarket, lines: list[FairLine]) -> list[FairLine] | str:
    """All book lines for the same game, or a string explaining why not."""
    parsed = parse_event_ticker(market.event_ticker)
    if not parsed:
        return f"can't read teams/date from event ticker {market.event_ticker!r}"
    date, away_code, home_code = parsed
    away, home = TEAM_CODES[away_code], TEAM_CODES[home_code]
    # Kalshi's date is US time; the book's kickoff is UTC (a 8:20pm ET game is
    # "tomorrow" in UTC). Allowing +/- 1.5 days covers that safely - the same two
    # teams never play each other twice in one week.
    window = timedelta(days=1.5)
    game = [ln for ln in lines
            if {ln.home_team, ln.away_team} == {home, away}
            and abs(ln.commence_time - (date + timedelta(hours=12))) <= window]
    return game or f"no sportsbook game found for {away} at {home} near {date:%Y-%m-%d}"


# ---------- step 2: which line? ----------

def _normalize(name: str) -> str:
    """'Amon-Ra St. Brown Jr.' -> 'amon ra st brown'. Strips accents,
    punctuation, and suffixes so small spelling differences still match."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z ]", " ", name.lower().replace("-", " "))
    words = [w for w in name.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"}]
    return " ".join(words)


def name_appears(player: str, market: KalshiMarket) -> bool:
    """Does this player's name appear in the Kalshi market?

    Tries: full name in the title/subtitle, then the ticker style many
    exchanges use: first initial + last name ("PMAHOMES").
    """
    full = _normalize(player)
    text = _normalize(f"{market.title} {market.yes_sub_title}")
    if full and f" {full} " in f" {text} ":
        return True
    words = full.split()
    if len(words) >= 2:
        initial_last = (words[0][0] + "".join(words[1:])).upper()
        if initial_last in market.ticker.upper().split("-"):
            return True
    return False


def kalshi_threshold(market: KalshiMarket) -> int | None:
    """The whole number Kalshi's YES needs to reach. "250+" -> 250.

    Prefers Kalshi's structured fields (floor_strike + strike_type); falls back
    to reading "250+" out of the subtitle/title.
    """
    if market.floor_strike is not None:
        if market.strike_type == "greater":           # YES if stat > floor_strike
            return math.floor(market.floor_strike) + 1
        if market.strike_type == "greater_or_equal":  # YES if stat >= floor_strike
            return math.ceil(market.floor_strike)
    m = re.search(r"(\d+)\s*\+", f"{market.yes_sub_title} {market.title}")
    if m:
        return int(m.group(1))
    m = re.search(r"(?:over|above|more than)\s+(\d+(?:\.\d+)?)", market.title, re.I)
    if m:
        return math.floor(float(m.group(1))) + 1
    return None


def _team_for_total(market: KalshiMarket, game: list[FairLine]) -> str | None:
    """For a team-total market, which team is it about?"""
    teams = {ln.subject for ln in game if ln.market == "team_totals"}
    # Live tickers glue the code to the number ("...-SEA8", "...-LAR43"), so strip
    # trailing digits. The ticker is the reliable source: Kalshi's titles mix
    # styles ("Seattle", "LA Rams", "BUF Bills").
    for part in market.ticker.upper().split("-")[2:]:
        code = part.rstrip("0123456789")
        if TEAM_CODES.get(code) in teams:
            return TEAM_CODES[code]
    text = _normalize(f"{market.title} {market.yes_sub_title}")
    for team in teams:
        nickname = _normalize(team).split()[-1]          # "chiefs"
        city = _normalize(team).rsplit(" ", 1)[0]        # "kansas city"
        if nickname in text.split() or f" {city} " in f" {text} ":
            return team
    return None


# ---------- step 3: what's fair? ----------

def compare_market(kind: str, market: KalshiMarket, lines: list[FairLine],
                   std_devs: dict, fee_rate: float,
                   max_gap_sd: float = 0.5) -> list[Comparison] | Unmatched:
    """Compare one Kalshi market to the books. Returns YES/NO comparisons."""
    book_market = KIND_TO_BOOK_MARKET.get(kind)
    if not book_market:
        return Unmatched(market.ticker, f"unknown market kind {kind!r}")

    game = find_game_lines(market, lines)
    if isinstance(game, str):
        return Unmatched(market.ticker, game)
    game = [ln for ln in game if ln.market == book_market]
    if not game:
        return Unmatched(market.ticker, f"sportsbooks have no {book_market} for this game")

    # ---- Moneyline: no threshold, just "does this team win?" ----
    if book_market == "h2h":
        code = market.ticker.split("-")[-1].upper()
        team = TEAM_CODES.get(code)
        line = game[0]
        if team not in line.fair:
            return Unmatched(market.ticker, f"ticker suffix {code!r} isn't a team in this game")
        main_yes = line.fair[team]
        check_yes = line.check_fair[team] if line.check_fair else None
        return _both_sides(market, line, main_yes, check_yes, "exact",
                           f"{team} moneyline", fee_rate)

    # ---- Props and team totals: subject + threshold ----
    threshold = kalshi_threshold(market)
    if threshold is None:
        return Unmatched(market.ticker, "can't find a threshold like '250+' in this market")

    if book_market == "team_totals":
        subject = _team_for_total(market, game)
    else:
        subject = next((ln.subject for ln in game if name_appears(ln.subject, market)), None)
    if not subject:
        return Unmatched(market.ticker, f"no {book_market} line at the books for "
                                        f"the player/team in {market.title!r}")

    # Kalshi "250+" lines up exactly with book "Over 249.5". Pick the book line
    # closest to that, so the bell-curve estimate has the least distance to cover.
    candidates = [ln for ln in game if ln.subject == subject and ln.point is not None]
    line = min(candidates, key=lambda ln: abs(ln.point - (threshold - 0.5)))
    sd = std_devs[kind]
    exact = line.point == threshold - 0.5
    # The bell curve is only a rough guess, and it gets worse the further we
    # stretch it. Real NFL stats are lopsided: a receiver with a 1.5-yard rushing
    # line almost never gets 15+, but a 30-yard bell curve says 33%. Live data
    # showed these produce big FAKE edges, so we refuse to estimate them.
    if not exact:
        gap = abs((threshold - 0.5) - line.point)
        if line.point < sd:
            return Unmatched(market.ticker, f"book line {line.point} is smaller than the "
                                            f"std dev ({sd}); bell-curve estimate unreliable")
        if gap > max_gap_sd * sd:
            return Unmatched(market.ticker, f"threshold {threshold}+ is {gap:g} from the book "
                                            f"line {line.point}; too far to estimate safely")

    def yes_prob(fair: dict[str, float]) -> float:
        return prob_at_least(threshold, line.point, fair["Over"], sd)

    main_yes = yes_prob(line.fair)
    check_yes = yes_prob(line.check_fair) if line.check_fair else None
    method = "exact" if exact else f"line-adjusted (sd={sd}, ESTIMATE)"
    label = f"{subject} Over {line.point}"
    return _both_sides(market, line, main_yes, check_yes, method, label, fee_rate)


def _both_sides(market: KalshiMarket, line: FairLine, main_yes: float,
                check_yes: float | None, method: str, label: str,
                fee_rate: float) -> list[Comparison]:
    """Build the YES comparison and the NO comparison.

    Conservative rule: when we have a second opinion (US books alongside
    Pinnacle), each side uses whichever estimate is LOWER for that side.
    """
    yes_options = [main_yes] + ([check_yes] if check_yes is not None else [])
    fair_by_side = {
        "yes": min(yes_options),
        "no": min(1 - p for p in yes_options),
    }
    asks = {"yes": market.yes_ask, "no": market.no_ask}
    out = []
    for side in ("yes", "no"):
        price = asks[side]
        if price is None:
            continue  # nobody is selling this side right now
        fair = fair_by_side[side]
        out.append(Comparison(
            kalshi_ticker=market.ticker,
            kalshi_title=f"{market.title} [{market.yes_sub_title}]".strip(),
            side=side,
            kalshi_price=price,
            fair_prob=fair,
            edge=edge(fair, price),
            edge_after_fees=edge_after_fees(fair, price, fee_rate),
            method=method,
            book_line=label,
            book_source=line.source + (f" + check {line.check_source}" if line.check_source else ""),
            book_odds=line.book_odds,
            book_fetched_at=line.fetched_at,
            book_updated_at=line.book_updated_at,
            event_id=line.event_id,
            commence_time=line.commence_time,
            book_market=line.market,
        ))
    return out


def match_all(kalshi: dict[str, list[KalshiMarket]], lines: list[FairLine],
              std_devs: dict, fee_rate: float, now: datetime | None = None,
              max_gap_sd: float = 0.5) -> tuple[list[Comparison], list[Unmatched]]:
    """Run compare_market over every Kalshi market.

    Games that have already kicked off are skipped: Kalshi prices move live
    during the game while our sportsbook odds are pre-game, so comparing them
    would produce huge fake edges.
    """
    now = now or datetime.now(timezone.utc)
    upcoming = [ln for ln in lines if ln.commence_time > now]
    comparisons, unmatched = [], []
    for kind, markets in kalshi.items():
        for m in markets:
            result = compare_market(kind, m, upcoming, std_devs, fee_rate, max_gap_sd)
            if isinstance(result, Unmatched):
                unmatched.append(result)
            else:
                comparisons.extend(result)
    return comparisons, unmatched


# ---------- run this file directly: the full read-only pipeline, once ----------

def main() -> None:
    from kalshi_bot.config import load_config
    from kalshi_bot.fair_value import build_fair_lines
    from kalshi_bot.kalshi_client import KalshiClient
    from kalshi_bot.odds_client import OddsClient

    parser = argparse.ArgumentParser(description="Match Kalshi to sportsbooks once.")
    parser.add_argument("--props", action="store_true",
                        help="also fetch props/team totals for the NEXT game (costs credits)")
    parser.add_argument("--show-unmatched", action="store_true",
                        help="list every Kalshi market we couldn't match, with the reason")
    args = parser.parse_args()

    cfg = load_config()
    odds_cfg = cfg["odds_api"]
    kalshi = KalshiClient.from_config().fetch_all_nfl(cfg["kalshi"]["series"])
    odds = OddsClient.from_config()
    outcomes = odds.get_game_odds(odds_cfg["game_markets"])
    if args.props and outcomes:
        future = [o for o in outcomes if o.commence_time > datetime.now(timezone.utc)]
        if future:
            next_game = min(future, key=lambda o: o.commence_time).event_id
            outcomes += odds.get_event_odds(next_game, odds_cfg["event_markets"])
    lines = build_fair_lines(outcomes, odds_cfg["sharp_book"], odds_cfg["fallback_books"],
                             odds_cfg["min_fallback_books"])

    comps, unmatched = match_all(kalshi, lines, cfg["std_devs"], cfg["fees"]["taker_fee_rate"],
                                 max_gap_sd=cfg["std_devs"].get("max_gap_sd", 0.5))
    comps.sort(key=lambda c: c.edge_after_fees, reverse=True)
    print(f"{'market':<50} {'side':<4} {'kalshi':>6} {'fair':>6} {'edge':>6} {'net':>6}  method")
    for c in comps[:40]:
        print(f"{c.kalshi_title[:50]:<50} {c.side:<4} {c.kalshi_price * 100:5.1f}c "
              f"{c.fair_prob * 100:5.1f}% {c.edge * 100:+5.1f} {c.edge_after_fees * 100:+5.1f}  "
              f"{c.method}")
    print(f"\n{len(comps)} comparisons, {len(unmatched)} unmatched markets, "
          f"{odds.credits_remaining} odds credits left")
    if args.show_unmatched:
        for u in unmatched:
            print(f"  UNMATCHED {u.kalshi_ticker}: {u.reason}")


if __name__ == "__main__":
    main()
