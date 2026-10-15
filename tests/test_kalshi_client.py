"""Tests for kalshi_client.py using fake responses (no internet needed).

The sample JSON below is shaped like Kalshi's /markets response. It is
SAMPLE data written for testing, not real prices.
"""

from unittest.mock import patch

import pytest
import requests

from kalshi_bot.kalshi_client import KalshiClient, KalshiError, buyable_depth, parse_market

SAMPLE_MONEYLINE = {
    "ticker": "KXNFLGAME-26OCT18KCBUF-KC",
    "event_ticker": "KXNFLGAME-26OCT18KCBUF",
    "title": "Kansas City at Buffalo Winner?",
    "yes_sub_title": "Kansas City",
    "yes_bid_dollars": "0.4400",
    "yes_ask_dollars": "0.4600",
    "no_bid_dollars": "0.5400",
    "no_ask_dollars": "0.5600",
    "last_price_dollars": "0.4500",
    "close_time": "2026-11-01T17:00:00Z",
    "volume": 12500,
}

SAMPLE_PROP_OLD_FORMAT = {
    # Older-style response: prices in whole cents, no NO prices.
    "ticker": "KXNFLPASSYDS-26OCT18KCBUF-PMAHOMES-250",
    "event_ticker": "KXNFLPASSYDS-26OCT18KCBUF",
    "title": "Patrick Mahomes: 250+ passing yards?",
    "yes_sub_title": "250+",
    "yes_bid": 61,
    "yes_ask": 64,
    "floor_strike": 249.5,
    "strike_type": "greater",
}


# ---------- parsing ----------

def test_parse_dollar_prices():
    m = parse_market(SAMPLE_MONEYLINE, "KXNFLGAME")
    assert m.yes_bid == 0.44
    assert m.yes_ask == 0.46
    assert m.no_ask == 0.56
    assert m.close_time.year == 2026
    assert m.volume == 12500


def test_parse_cent_prices_and_derive_no_side():
    m = parse_market(SAMPLE_PROP_OLD_FORMAT)
    assert m.yes_ask == 0.64
    # NO ask = 1 - YES bid = 1 - 0.61 = 0.39
    assert m.no_ask == pytest.approx(0.39)
    assert m.floor_strike == 249.5


def test_empty_order_book_gives_none():
    raw = {"ticker": "X", "yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000"}
    m = parse_market(raw)
    assert m.yes_bid is None
    assert m.yes_ask is None
    assert m.no_ask is None


# ---------- HTTP behavior with a fake session ----------

class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    """Returns the queued responses in order and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses):
    session = FakeSession(responses)
    client = KalshiClient("https://example.test/v2", requests_per_second=1000, session=session)
    return client, session


@patch("kalshi_bot.http_client.time.sleep")  # skip real waiting during tests
def test_pagination_follows_cursor(_sleep):
    client, session = make_client([
        FakeResponse(200, {"markets": [SAMPLE_MONEYLINE], "cursor": "page2"}),
        FakeResponse(200, {"markets": [SAMPLE_MONEYLINE], "cursor": ""}),
    ])
    markets = client.fetch_markets("KXNFLGAME")
    assert len(markets) == 2
    assert session.calls[1]["cursor"] == "page2"


@patch("kalshi_bot.http_client.time.sleep")
def test_retries_on_rate_limit_then_succeeds(sleep):
    client, _ = make_client([
        FakeResponse(429),
        requests.ConnectionError("wifi dropped"),
        FakeResponse(200, {"markets": [], "cursor": ""}),
    ])
    assert client.fetch_markets("KXNFLGAME") == []
    # backoff waits of 1s then 2s
    assert [c.args[0] for c in sleep.call_args_list if c.args[0] >= 1] == [1, 2]


@patch("kalshi_bot.http_client.time.sleep")
def test_gives_up_after_max_retries(_sleep):
    client, _ = make_client([FakeResponse(503)] * 5)
    with pytest.raises(KalshiError):
        client.get("/markets")


@patch("kalshi_bot.http_client.time.sleep")
def test_bad_request_not_retried(_sleep):
    client, session = make_client([FakeResponse(400, {"error": "bad"})])
    with pytest.raises(KalshiError):
        client.get("/markets")
    assert len(session.calls) == 1


@patch("kalshi_bot.http_client.time.sleep")
def test_one_failing_series_does_not_stop_others(_sleep):
    client, _ = make_client(
        [FakeResponse(404, {})] +
        [FakeResponse(200, {"markets": [SAMPLE_MONEYLINE], "cursor": ""})]
    )
    results = client.fetch_all_nfl({"rb_rush_yds": "BADTICKER", "moneyline": "KXNFLGAME"})
    assert results["rb_rush_yds"] == []
    assert len(results["moneyline"]) == 1


@patch("kalshi_bot.http_client.time.sleep")
def test_strict_mode_refuses_partial_data(_sleep):
    # The combo checker numbers legs from the full list; a missing series
    # would shift the numbers, so strict mode raises instead of skipping.
    client, _ = make_client([FakeResponse(404, {})])
    with pytest.raises(KalshiError):
        client.fetch_all_nfl({"rb_rush_yds": "BADTICKER"}, strict=True)


def test_client_only_uses_get():
    # Safety check: the client must not have any method that writes to Kalshi.
    for forbidden in ("post", "put", "delete", "patch", "create_order", "place_order"):
        assert not hasattr(KalshiClient, forbidden)


# ---------- order book depth ----------

# Current format. Kalshi lists BIDS only, sorted low to high.
# NO bids at 0.52 (300), 0.54 (200), 0.55 (100) are YES offers at
# 0.48 (300), 0.46 (200), 0.45 (100).
BOOK_FP = {"orderbook_fp": {
    "yes_dollars": [["0.4000", "500.00"], ["0.4300", "250.00"]],
    "no_dollars": [["0.5200", "300.00"], ["0.5400", "200.00"], ["0.5500", "100.00"]],
}}


def test_depth_to_buy_yes_under_limit():
    d = buyable_depth(BOOK_FP, "yes", max_price=0.46)
    # Takes 100 @ 0.45 and 200 @ 0.46 = 300 contracts, cost 45 + 92 = $137
    assert d.contracts == 300
    assert d.cost_dollars == 137.00
    assert d.worst_price == 0.46


def test_depth_to_buy_no_from_yes_bids():
    # YES bids 0.43 (250), 0.40 (500) -> NO offers at 0.57 (250), 0.60 (500)
    d = buyable_depth(BOOK_FP, "no", max_price=0.58)
    assert d.contracts == 250
    assert d.cost_dollars == 142.50


def test_depth_old_cents_format():
    old = {"orderbook": {"yes": [[40, 10]], "no": [[55, 20]]}}
    d = buyable_depth(old, "yes", max_price=0.45)
    assert d.contracts == 20
    assert d.cost_dollars == 9.00


def test_depth_empty_book():
    d = buyable_depth({"orderbook_fp": {"yes_dollars": None, "no_dollars": None}}, "yes", 0.9)
    assert d.contracts == 0
    assert d.worst_price is None
