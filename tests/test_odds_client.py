"""Tests for odds_client.py and fair_value.py using SAMPLE data (no internet).

The JSON below is shaped like The Odds API v4 response. The prices are made
up for testing, chosen so the answers can be checked by hand.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from kalshi_bot.fair_value import build_fair_lines
from kalshi_bot.odds_client import OddsApiError, OddsClient, parse_event

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)

SAMPLE_EVENT = {
    "id": "evt1",
    "commence_time": "2026-10-18T17:00:00Z",
    "home_team": "Buffalo Bills",
    "away_team": "Kansas City Chiefs",
    "bookmakers": [
        {"key": "pinnacle", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Kansas City Chiefs", "price": 124},
                {"name": "Buffalo Bills", "price": -148},
            ]},
        ]},
        {"key": "draftkings", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Kansas City Chiefs", "price": 120},
                {"name": "Buffalo Bills", "price": -142},
            ]},
            {"key": "player_pass_yds", "outcomes": [
                {"name": "Over", "description": "Patrick Mahomes", "price": -114, "point": 277.5},
                {"name": "Under", "description": "Patrick Mahomes", "price": -114, "point": 277.5},
            ]},
        ]},
        {"key": "fanduel", "markets": [
            {"key": "player_pass_yds", "outcomes": [
                {"name": "Over", "description": "Patrick Mahomes", "price": -120, "point": 277.5},
                {"name": "Under", "description": "Patrick Mahomes", "price": 100, "point": 277.5},
                # Josh Allen only at ONE US book -> should be skipped (min 2 books)
                {"name": "Over", "description": "Josh Allen", "price": -110, "point": 245.5},
                {"name": "Under", "description": "Josh Allen", "price": -110, "point": 245.5},
            ]},
        ]},
    ],
}

SHARP = "pinnacle"
FALLBACK = ["draftkings", "fanduel", "betmgm"]


# ---------- parsing ----------

def test_parse_event_flattens_everything():
    rows = parse_event(SAMPLE_EVENT, NOW)
    assert len(rows) == 10
    prop = [r for r in rows if r.market == "player_pass_yds"][0]
    assert prop.description == "Patrick Mahomes"
    assert prop.point == 277.5
    assert prop.commence_time.hour == 17


# ---------- fair value ----------

def lines_by(lines, market, subject=""):
    return [ln for ln in lines if ln.market == market and ln.subject == subject]


def test_pinnacle_used_alone_when_present():
    lines = build_fair_lines(parse_event(SAMPLE_EVENT, NOW), SHARP, FALLBACK)
    (ml,) = lines_by(lines, "h2h")
    # Pinnacle +124 / -148 -> 42.8% / 57.2% (same as the Stage 1 test).
    # DraftKings' h2h prices are ignored because Pinnacle is available.
    assert ml.source == "pinnacle"
    assert ml.fair["Kansas City Chiefs"] == pytest.approx(0.428, abs=0.001)
    assert ml.fair["Buffalo Bills"] == pytest.approx(0.572, abs=0.001)


def test_average_of_us_books_when_no_pinnacle():
    lines = build_fair_lines(parse_event(SAMPLE_EVENT, NOW), SHARP, FALLBACK)
    (mahomes,) = lines_by(lines, "player_pass_yds", "Patrick Mahomes")
    # DraftKings -114/-114 -> 50.0% Over.
    # FanDuel -120/+100 -> 0.5455 and 0.5000, total 1.0455 -> Over 52.17%.
    # Average: (50.00 + 52.17) / 2 = 51.09% Over.
    assert mahomes.source == "avg(draftkings,fanduel)"
    assert mahomes.fair["Over"] == pytest.approx(0.5109, abs=0.0005)
    assert mahomes.fair["Over"] + mahomes.fair["Under"] == pytest.approx(1.0)
    assert "fanduel: Over -120 / Under +100" in mahomes.book_odds


def test_line_with_too_few_books_is_skipped():
    lines = build_fair_lines(parse_event(SAMPLE_EVENT, NOW), SHARP, FALLBACK)
    assert lines_by(lines, "player_pass_yds", "Josh Allen") == []


def test_single_book_allowed_if_configured():
    lines = build_fair_lines(parse_event(SAMPLE_EVENT, NOW), SHARP, FALLBACK,
                             min_fallback_books=1)
    assert len(lines_by(lines, "player_pass_yds", "Josh Allen")) == 1


# ---------- client behavior ----------

class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.text = str(body)
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def make_client(responses):
    session = FakeSession(responses)
    client = OddsClient(api_key="SECRET123", sport="americanfootball_nfl",
                        bookmakers=["pinnacle", "draftkings"], min_credits_reserve=25,
                        base_url="https://example.test/v4", requests_per_second=1000,
                        session=session)
    return client, session


@patch("kalshi_bot.http_client.time.sleep")
def test_game_odds_request_and_quota(_sleep):
    client, session = make_client([
        FakeResponse(200, [SAMPLE_EVENT], {"X-Requests-Remaining": "480"}),
    ])
    rows = client.get_game_odds(["h2h"])
    url, params = session.calls[0]
    assert url.endswith("/sports/americanfootball_nfl/odds")
    assert params["bookmakers"] == "pinnacle,draftkings"
    assert params["oddsFormat"] == "american"
    assert len(rows) == 10
    assert client.credits_remaining == 480


@patch("kalshi_bot.http_client.time.sleep")
def test_stops_spending_when_budget_low(_sleep):
    client, session = make_client([
        FakeResponse(200, [], {"x-requests-remaining": "10"}),
    ])
    client.get_events()                 # free call; tells us 10 credits are left
    with pytest.raises(OddsApiError, match="credits left"):
        client.get_game_odds(["h2h"])   # 10 < reserve of 25 -> refuse
    assert len(session.calls) == 1      # the paid request was never sent


@patch("kalshi_bot.http_client.time.sleep")
def test_api_key_never_appears_in_errors(_sleep):
    client, _ = make_client([FakeResponse(401, {"message": "bad key SECRET123"})])
    with pytest.raises(OddsApiError) as exc_info:
        client.get_events()
    assert "SECRET123" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_missing_api_key_gives_clear_error(monkeypatch):
    monkeypatch.setattr("kalshi_bot.odds_client.load_dotenv", lambda: None)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    with pytest.raises(OddsApiError, match="ODDS_API_KEY"):
        OddsClient.from_config()


# ---------- Pinnacle cross-check ----------

def _h2h_book(key, kc_price, buf_price, updated="2026-10-18T14:50:00Z"):
    return {"key": key, "last_update": updated, "markets": [{"key": "h2h", "outcomes": [
        {"name": "Kansas City Chiefs", "price": kc_price},
        {"name": "Buffalo Bills", "price": buf_price},
    ]}]}


CROSS_CHECK_EVENT = {
    "id": "evt2", "commence_time": "2026-10-18T17:00:00Z",
    "home_team": "Buffalo Bills", "away_team": "Kansas City Chiefs",
    "bookmakers": [
        _h2h_book("pinnacle", 124, -148, updated="2026-10-18T13:00:00Z"),  # 42.8% KC
        _h2h_book("draftkings", 100, -100),   # 50% KC
        _h2h_book("fanduel", 100, -100),      # 50% KC
    ],
}


def test_us_books_become_second_opinion_when_pinnacle_present():
    from kalshi_bot.fair_value import conservative_fair
    (ml,) = build_fair_lines(parse_event(CROSS_CHECK_EVENT, NOW), SHARP, FALLBACK)
    assert ml.source == "pinnacle"
    assert ml.check_source == "avg(draftkings,fanduel)"
    assert ml.check_fair["Kansas City Chiefs"] == pytest.approx(0.50)
    # KC: Pinnacle says 42.8%, US books say 50% -> conservative takes 42.8%.
    assert conservative_fair(ml, "Kansas City Chiefs") == pytest.approx(0.428, abs=0.001)
    # Buffalo: Pinnacle 57.2%, US 50% -> conservative takes 50%.
    assert conservative_fair(ml, "Buffalo Bills") == pytest.approx(0.50)
    assert "draftkings" in ml.book_odds


def test_book_update_time_recorded():
    (ml,) = build_fair_lines(parse_event(CROSS_CHECK_EVENT, NOW), SHARP, FALLBACK)
    # Pinnacle last changed its price at 13:00 even though we downloaded at 15:00.
    assert ml.book_updated_at.hour == 13
    assert ml.fetched_at.hour == 15
