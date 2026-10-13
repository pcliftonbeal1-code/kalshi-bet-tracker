"""Tests for matching.py with SAMPLE data (no internet).

Scenario: Kansas City Chiefs at Buffalo Bills, Oct 18 2026, 1pm ET (17:00 UTC).
Kalshi ticker formats below follow Kalshi's public URLs (e.g. KXNFLPASSYDS-25DEC28JACIND);
the player/threshold parts are our best guess until checked against live data.
"""

from datetime import datetime, timezone

import pytest

from kalshi_bot.fair_value import build_fair_lines
from kalshi_bot.kalshi_client import parse_market
from kalshi_bot.matching import (
    kalshi_threshold,
    match_all,
    name_appears,
    parse_event_ticker,
)
from kalshi_bot.odds_client import parse_event
from kalshi_bot.teams import split_matchup

BEFORE_KICKOFF = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
AFTER_KICKOFF = datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc)
STD_DEVS = {"qb_pass_yds": 70, "rb_rush_yds": 30, "team_points": 10}
FEE = 0.07


def _two_sided(key, market, description, point, over, under):
    return {"key": key, "markets": [{"key": market, "outcomes": [
        {"name": "Over", "description": description, "price": over, "point": point},
        {"name": "Under", "description": description, "price": under, "point": point},
    ]}]}


BOOK_EVENT = {
    "id": "evt1", "commence_time": "2026-10-18T17:00:00Z",
    "home_team": "Buffalo Bills", "away_team": "Kansas City Chiefs",
    "bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Kansas City Chiefs", "price": 124},   # fair 42.8%
            {"name": "Buffalo Bills", "price": -148},       # fair 57.2%
        ]}]},
        _two_sided("draftkings", "player_pass_yds", "Patrick Mahomes", 277.5, -114, -114),
        _two_sided("fanduel", "player_pass_yds", "Patrick Mahomes", 277.5, -114, -114),
        _two_sided("draftkings", "team_totals", "Kansas City Chiefs", 24.5, -110, -110),
        _two_sided("fanduel", "team_totals", "Kansas City Chiefs", 24.5, -110, -110),
    ],
}

LINES = build_fair_lines(parse_event(BOOK_EVENT, BEFORE_KICKOFF),
                         "pinnacle", ["draftkings", "fanduel", "betmgm"])


def km(**fields):
    base = {"title": "", "yes_sub_title": ""}
    return parse_market({**base, **fields})


MONEYLINE = km(ticker="KXNFLGAME-26OCT18KCBUF-KC", event_ticker="KXNFLGAME-26OCT18KCBUF",
               title="Kansas City at Buffalo Winner?", yes_sub_title="Kansas City",
               yes_bid_dollars="0.3600", yes_ask_dollars="0.3800")
PROP_EXACT = km(ticker="KXNFLPASSYDS-26OCT18KCBUF-PMAHOMES-278",
                event_ticker="KXNFLPASSYDS-26OCT18KCBUF",
                title="Patrick Mahomes: 278+ passing yards", yes_sub_title="278+",
                floor_strike=277.5, strike_type="greater",
                yes_bid_dollars="0.4400", yes_ask_dollars="0.4600")
PROP_ADJUSTED = km(ticker="KXNFLPASSYDS-26OCT18KCBUF-PMAHOMES-250",
                   event_ticker="KXNFLPASSYDS-26OCT18KCBUF",
                   title="Passing yards", yes_sub_title="250+",   # name only in ticker
                   yes_bid_dollars="0.6000", yes_ask_dollars="0.6100")
TEAM_TOTAL = km(ticker="KXNFLTEAMTOTAL-26OCT18KCBUF-KC-28",
                event_ticker="KXNFLTEAMTOTAL-26OCT18KCBUF",
                title="Kansas City points", floor_strike=27.5, strike_type="greater",
                yes_bid_dollars="0.3500", yes_ask_dollars="0.3700")
UNKNOWN_GAME = km(ticker="KXNFLGAME-26OCT18NYGLV-NYG", event_ticker="KXNFLGAME-26OCT18NYGLV",
                  yes_bid_dollars="0.4000", yes_ask_dollars="0.4200")

KALSHI = {"moneyline": [MONEYLINE, UNKNOWN_GAME], "qb_pass_yds": [PROP_EXACT, PROP_ADJUSTED],
          "team_points": [TEAM_TOTAL]}


def run(now=BEFORE_KICKOFF):
    comps, unmatched = match_all(KALSHI, LINES, STD_DEVS, FEE, now=now)
    by_key = {(c.kalshi_ticker, c.side): c for c in comps}
    return by_key, unmatched


# ---------- small pieces ----------

def test_split_matchup():
    assert split_matchup("KCBUF") == ("KC", "BUF")
    assert split_matchup("NYGLV") == ("NYG", "LV")
    assert split_matchup("JACIND") == ("JAC", "IND")
    assert split_matchup("LACAR") == ("LA", "CAR")   # Rams at Panthers
    assert split_matchup("XXYY") is None


def test_parse_event_ticker():
    date, away, home = parse_event_ticker("KXNFLPASSYDS-25DEC28JACIND")
    assert (date.year, date.month, date.day) == (2025, 12, 28)
    assert (away, home) == ("JAC", "IND")
    assert parse_event_ticker("garbage") is None


def test_threshold_from_strike_fields_and_text():
    assert kalshi_threshold(PROP_EXACT) == 278       # floor 277.5, "greater" -> 278
    assert kalshi_threshold(PROP_ADJUSTED) == 250    # from "250+"
    assert kalshi_threshold(km(ticker="X", floor_strike=250, strike_type="greater_or_equal")) == 250


def test_name_matching():
    assert name_appears("Patrick Mahomes", PROP_EXACT)         # in the title
    assert name_appears("Patrick Mahomes", PROP_ADJUSTED)      # only in ticker
    assert not name_appears("Josh Allen", PROP_EXACT)
    weird = km(ticker="X", title="Amon-Ra St. Brown: 80+ receiving yards")
    assert name_appears("Amon-Ra St. Brown Jr.", weird)


# ---------- full matching ----------

def test_moneyline_both_sides():
    by_key, _ = run()
    yes = by_key[("KXNFLGAME-26OCT18KCBUF-KC", "yes")]
    # Fair KC 42.8% vs Kalshi YES ask 38c -> edge +4.8
    assert yes.fair_prob == pytest.approx(0.428, abs=0.001)
    assert yes.edge == pytest.approx(0.048, abs=0.001)
    # Fee at 38c: 0.07 * 0.38 * 0.62 = 0.0165 -> net edge 3.15
    assert yes.edge_after_fees == pytest.approx(0.0315, abs=0.001)
    no = by_key[("KXNFLGAME-26OCT18KCBUF-KC", "no")]
    # NO ask = 1 - YES bid = 64c; fair NO = 57.2% -> edge -6.8
    assert no.kalshi_price == pytest.approx(0.64)
    assert no.edge == pytest.approx(-0.068, abs=0.001)


def test_prop_exact_line():
    by_key, _ = run()
    c = by_key[("KXNFLPASSYDS-26OCT18KCBUF-PMAHOMES-278", "yes")]
    # Kalshi 278+ == book Over 277.5 (50% fair) -> exact, edge 50 - 46 = +4
    assert c.method == "exact"
    assert c.fair_prob == pytest.approx(0.50)
    assert c.edge == pytest.approx(0.04)


def test_prop_line_adjusted():
    by_key, _ = run()
    c = by_key[("KXNFLPASSYDS-26OCT18KCBUF-PMAHOMES-250", "yes")]
    # Same as Stage 1: 250+ vs 277.5 line, sd 70 -> 65.54%. Kalshi 61c -> +4.54
    assert c.method == "line-adjusted (sd=70, ESTIMATE)"
    assert c.fair_prob == pytest.approx(0.6554, abs=0.0001)
    assert c.edge == pytest.approx(0.0454, abs=0.0001)


def test_team_total_line_adjusted():
    by_key, _ = run()
    c = by_key[("KXNFLTEAMTOTAL-26OCT18KCBUF-KC-28", "yes")]
    # 28+ vs book 24.5 (50/50), sd 10: z = (27.5 - 24.5) / 10 = 0.30
    # P(above z=0.30) = 0.3821 (z-table). Kalshi 37c -> +1.21
    assert c.fair_prob == pytest.approx(0.3821, abs=0.0001)
    assert c.book_line == "Kansas City Chiefs Over 24.5"


def test_team_total_live_ticker_format():
    # Real Kalshi format, checked live: code glued to the number ("-BUF28"), and
    # a title that names neither the city nor the full team name.
    live = km(ticker="KXNFLTEAMTOTAL-26OCT18KCBUF-BUF28",
              event_ticker="KXNFLTEAMTOTAL-26OCT18KCBUF",
              title="Over 27.5 points scored", floor_strike=27.5, strike_type="greater",
              yes_bid_dollars="0.3500", yes_ask_dollars="0.3700")
    event = dict(BOOK_EVENT, bookmakers=BOOK_EVENT["bookmakers"] + [
        _two_sided("draftkings", "team_totals", "Buffalo Bills", 26.5, -110, -110),
        _two_sided("fanduel", "team_totals", "Buffalo Bills", 26.5, -110, -110),
    ])
    lines = build_fair_lines(parse_event(event, BEFORE_KICKOFF),
                             "pinnacle", ["draftkings", "fanduel", "betmgm"])
    comps, unmatched = match_all({"team_points": [live]}, lines, STD_DEVS, FEE,
                                 now=BEFORE_KICKOFF)
    assert unmatched == []
    assert comps[0].book_line == "Buffalo Bills Over 26.5"


def test_unmatched_has_reason():
    _, unmatched = run()
    (u,) = unmatched
    assert u.kalshi_ticker == "KXNFLGAME-26OCT18NYGLV-NYG"
    assert "New York Giants at Las Vegas Raiders" in u.reason


def test_started_games_are_skipped():
    by_key, unmatched = run(now=AFTER_KICKOFF)
    assert by_key == {}
    assert len(unmatched) == 5


def test_refuses_unreliable_bell_curve_estimates():
    # Real case, Oct 12 2026: Puka Nacua (a receiver) had a 1.5-yard rushing line.
    # A 30-yard bell curve called "15+" 33% while Kalshi said 13c: a fake +19% edge.
    event = dict(BOOK_EVENT, bookmakers=[
        _two_sided("pinnacle", "player_rush_yds", "Puka Nacua", 1.5, -105, -115),
        _two_sided("pinnacle", "player_rush_yds", "James Cook", 75.5, -110, -110),
    ])
    lines = build_fair_lines(parse_event(event, BEFORE_KICKOFF), "pinnacle", ["draftkings"])

    def rush(ticker, title, threshold):
        return km(ticker=ticker, event_ticker="KXNFLRSHYDS-26OCT18KCBUF", title=title,
                  floor_strike=threshold - 0.5, strike_type="greater",
                  yes_bid_dollars="0.1200", yes_ask_dollars="0.1300")

    markets = {"rb_rush_yds": [
        rush("KXNFLRSHYDS-26OCT18KCBUF-NACUA-15", "Puka Nacua: 15+ rushing yards", 15),
        rush("KXNFLRSHYDS-26OCT18KCBUF-COOK-120", "James Cook: 120+ rushing yards", 120),
        rush("KXNFLRSHYDS-26OCT18KCBUF-COOK-80", "James Cook: 80+ rushing yards", 80),
    ]}
    comps, unmatched = match_all(markets, lines, STD_DEVS, FEE, now=BEFORE_KICKOFF)
    reasons = {u.kalshi_ticker.split("-")[-1]: u.reason for u in unmatched}
    assert "under half a typical" in reasons["15"]       # tiny line: refuse
    assert "too far to estimate" in reasons["120"]            # 44.5 yds > 0.5 * 30: refuse
    assert {c.kalshi_ticker for c in comps} == {"KXNFLRSHYDS-26OCT18KCBUF-COOK-80"}  # 4.5 yds: ok
