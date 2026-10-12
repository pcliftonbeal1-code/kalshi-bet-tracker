"""Tests for alerts.py and bot.py with FAKE Kalshi and sportsbook clients.

Scenario (same as test_matching.py): Chiefs at Bills, kickoff Oct 18 17:00 UTC.
Pinnacle +124 / -148 -> fair KC 42.8%. Kalshi sells KC YES at 38c:
gross edge 4.8%, fee 0.07 * 0.38 * 0.62 = 1.65c, net edge 3.15% >= 3% -> alert.
"""

import csv
import copy
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.alerts import AlertTracker, format_alert, max_buy_price
from kalshi_bot.bot import OddsCache, run_cycle
from kalshi_bot.kalshi_client import parse_market
from kalshi_bot.odds_client import OddsApiError, parse_event

T0 = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)

BOOK_EVENT = {
    "id": "evt1", "commence_time": "2026-10-18T17:00:00Z",
    "home_team": "Buffalo Bills", "away_team": "Kansas City Chiefs",
    "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Kansas City Chiefs", "price": 124},
        {"name": "Buffalo Bills", "price": -148},
    ]}]}],
}

CFG = {
    "alerts": {"min_edge": 0.03, "realert_edge_increase": 0.01},
    "fees": {"taker_fee_rate": 0.07},
    "std_devs": {"qb_pass_yds": 70, "rb_rush_yds": 30, "team_points": 10},
    "kalshi": {"series": {"moneyline": "KXNFLGAME"}},
    "odds_api": {"sharp_book": "pinnacle", "fallback_books": ["draftkings", "fanduel"],
                 "min_fallback_books": 2, "game_markets": ["h2h"],
                 "event_markets": ["player_pass_yds"], "full_refresh_minutes": 120,
                 "confirm_max_age_minutes": 5},
    "loop": {"props": False, "props_hours_before_kickoff": 6, "confirm_cooldown_minutes": 30},
}


def kc_market(bid, ask):
    return parse_market({"ticker": "KXNFLGAME-26OCT18KCBUF-KC",
                         "event_ticker": "KXNFLGAME-26OCT18KCBUF",
                         "title": "Kansas City at Buffalo Winner?", "yes_sub_title": "Kansas City",
                         "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"})


def book_with_yes_offer(price, qty=100):
    """A YES offer at `price` is someone's NO bid at 1 - price."""
    return {"orderbook_fp": {"yes_dollars": [], "no_dollars": [[f"{1 - price:.4f}", f"{qty}"]]}}


class FakeKalshi:
    def __init__(self):
        self.markets = {"moneyline": [kc_market(0.36, 0.38)]}
        self.book = book_with_yes_offer(0.38)

    def fetch_all_nfl(self, series):
        return self.markets

    def fetch_orderbook(self, ticker):
        return self.book


class FakeOdds:
    """Records every paid call. `now` stands in for the download time."""

    def __init__(self):
        self.now = T0
        self.game_calls = []
        self.event_calls = []
        self.broke = False
        self.credits_remaining = 400

    def get_game_odds(self, markets):
        if self.broke:
            raise OddsApiError("Only 10 credits left")
        self.game_calls.append(markets)
        return parse_event(BOOK_EVENT, self.now)

    def get_event_odds(self, event_id, markets):
        if self.broke:
            raise OddsApiError("Only 10 credits left")
        self.event_calls.append((event_id, markets))
        return [o for o in parse_event(BOOK_EVENT, self.now) if o.market in markets]


@pytest.fixture
def bot(tmp_path):
    kalshi, odds, cache = FakeKalshi(), FakeOdds(), OddsCache()
    tracker = AlertTracker(0.03, 0.01, tmp_path / "state.json")
    log = tmp_path / "alerts.csv"
    sent = []

    def cycle(minutes, cfg=CFG):
        now = T0 + timedelta(minutes=minutes)
        return run_cycle(kalshi, odds, cache, tracker, cfg, now=now,
                         notify=sent.append, log_path=log)

    return kalshi, odds, tracker, log, sent, cycle


# ---------- small pieces ----------

def test_max_buy_price():
    assert max_buy_price(0.50, 0.03, 0.07) == 0.45    # 45c nets 3.27%, 46c only 2.26%
    assert max_buy_price(0.428, 0.03, 0.07) == 0.38
    assert max_buy_price(0.02, 0.03, 0.07) is None


def test_tracker_realerts_only_when_edge_grows(tmp_path):
    t = AlertTracker(0.03, 0.01, tmp_path / "state.json")
    c = type("C", (), {"kalshi_ticker": "X", "side": "yes", "edge_after_fees": 0.035})()
    assert t.is_candidate(c)
    t.record(c)
    assert not t.is_candidate(c)                       # same edge: quiet
    c.edge_after_fees = 0.045
    assert t.is_candidate(c)                           # grew by 1 point: alert again
    c.edge_after_fees = 0.02
    assert not t.is_candidate(c)                       # below min edge
    # Remembered after a restart
    c.edge_after_fees = 0.035
    assert not AlertTracker(0.03, 0.01, tmp_path / "state.json").is_candidate(c)


# ---------- full cycles ----------

def test_alerts_once_then_stays_quiet(bot):
    _, odds, _, log, sent, cycle = bot
    (alert,) = cycle(0)
    assert alert.comparison.side == "yes"
    assert alert.comparison.edge_after_fees == pytest.approx(0.0315, abs=0.001)
    assert alert.max_price == 0.38
    assert alert.depth.contracts == 100
    assert "BUY YES" in sent[0] and "Don't pay over 38c" in sent[0]

    assert cycle(5) == []                       # same edge 5 minutes later: no repeat
    assert len(odds.game_calls) == 1            # cached odds reused: no credits spent
    assert odds.event_calls == []
    rows = list(csv.DictReader(open(log)))
    assert len(rows) == 1 and rows[0]["kalshi_ticker"] == "KXNFLGAME-26OCT18KCBUF-KC"


def bu_market(bid, ask):
    return parse_market({"ticker": "KXNFLGAME-26OCT18KCBUF-BUF",
                         "event_ticker": "KXNFLGAME-26OCT18KCBUF",
                         "title": "Kansas City at Buffalo Winner?", "yes_sub_title": "Buffalo",
                         "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"})


def test_same_bet_in_two_markets_alerts_once(bot):
    kalshi, _, _, _, sent, cycle = bot
    # "NO Buffalo" at 1 - 0.63 = 37c is the same bet as "YES KC" at 38c, and cheaper.
    kalshi.markets = {"moneyline": [kc_market(0.36, 0.38), bu_market(0.63, 0.64)]}
    # One fake book serves both markets: offers YES at 38c and NO at 37c.
    kalshi.book = {"orderbook_fp": {"yes_dollars": [["0.6300", "100"]],
                                    "no_dollars": [["0.6200", "100"]]}}
    (alert,) = cycle(0)
    assert (alert.comparison.kalshi_ticker, alert.comparison.side) == \
        ("KXNFLGAME-26OCT18KCBUF-BUF", "no")
    assert cycle(5) == []                       # the YES KC twin stays quiet too
    assert len(sent) == 1


def test_bigger_edge_on_stale_odds_is_confirmed_first(bot):
    kalshi, odds, _, _, sent, cycle = bot
    cycle(0)
    # Kalshi drops to 36c: net edge 5.2%, up 2 points. Our odds are 10 min old,
    # so the bot must re-pull that one game (1 credit) before alerting.
    kalshi.markets = {"moneyline": [kc_market(0.34, 0.36)]}
    kalshi.book = book_with_yes_offer(0.36)
    odds.now = T0 + timedelta(minutes=10)
    (alert,) = cycle(10)
    assert odds.event_calls == [("evt1", ["h2h"])]
    assert alert.comparison.kalshi_price == pytest.approx(0.36)
    assert len(sent) == 2


def test_no_alert_when_nothing_for_sale_and_confirm_has_cooldown(bot):
    kalshi, odds, _, log, sent, cycle = bot
    kalshi.book = book_with_yes_offer(0.45)     # only offers above our 38c limit
    assert cycle(0) == []
    odds.now = T0 + timedelta(minutes=10)
    assert cycle(10) == []                      # stale -> confirmed once
    assert cycle(15) == []                      # 5 min old: still fresh, no pull
    assert cycle(20) == []                      # stale, but within 30 min cooldown
    assert len(odds.event_calls) == 1
    assert sent == [] and not log.exists()


def test_out_of_credits_never_alerts_on_stale_odds(bot):
    kalshi, odds, _, _, sent, cycle = bot
    cycle(0)                                    # odds cached at T0
    odds.broke = True
    # A bigger edge appears, but we can't afford fresh odds to confirm it.
    kalshi.markets = {"moneyline": [kc_market(0.34, 0.36)]}
    kalshi.book = book_with_yes_offer(0.36)
    assert cycle(10) == []                      # confirm fails: no alert, no crash
    assert len(sent) == 1


def test_props_only_for_games_inside_window(bot):
    _, odds, _, _, _, cycle = bot
    cfg = copy.deepcopy(CFG)
    cfg["loop"]["props"] = True
    cfg["loop"]["props_hours_before_kickoff"] = 1
    cycle(0, cfg)                               # kickoff in 2h: outside 1h window
    assert odds.event_calls == []
    cycle(70, cfg)                              # kickoff in 50 min: fetch props
    assert ("evt1", ["player_pass_yds"]) in odds.event_calls


def test_started_game_never_alerts(bot):
    _, _, _, _, sent, cycle = bot
    assert cycle(125) == []                     # 17:05, game under way
    assert sent == []


def test_format_alert_mentions_missing_book(bot):
    kalshi, _, _, _, sent, cycle = bot
    (alert,) = cycle(0)
    alert.depth = None
    assert "order book unavailable" in format_alert(alert)
