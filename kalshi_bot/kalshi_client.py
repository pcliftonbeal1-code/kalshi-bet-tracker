"""Reads NFL market prices from Kalshi's public API. READ-ONLY.

This file only ever sends GET requests (the HTTP verb for "read").
There is no code here that can place, change, or cancel an order.

The HTTP plumbing (rate limiting, retries) lives in http_client.py.

Libraries used:
  dataclasses - built in; a quick way to define a "record" with named fields.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime

from kalshi_bot.config import load_config
from kalshi_bot.http_client import ApiError, HttpClient


class KalshiError(ApiError):
    """Raised when Kalshi can't be reached even after retrying."""


@dataclass
class KalshiMarket:
    """One Kalshi market, cleaned up into the fields we care about.

    Prices are probabilities (0.62 = 62 cents). None means nobody is
    currently offering at that side (an empty order book).
    """

    ticker: str            # unique id, e.g. "KXNFLGAME-26OCT18KCBUF-KC"
    event_ticker: str      # the game this market belongs to
    series_ticker: str     # which family of markets, e.g. "KXNFLGAME"
    title: str             # human-readable question
    yes_sub_title: str     # what YES means, e.g. "Kansas City" or "250+"
    yes_bid: float | None  # highest price someone will PAY for YES
    yes_ask: float | None  # lowest price someone will SELL YES for (what you'd pay)
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    floor_strike: float | None  # the threshold number for "X or more" markets
    strike_type: str | None     # how floor_strike is used, e.g. "greater"
    close_time: datetime | None
    volume: int


class KalshiClient(HttpClient):
    """Kalshi-specific reads. Rate limiting and retries come from HttpClient."""

    error_class = KalshiError

    @classmethod
    def from_config(cls) -> "KalshiClient":
        cfg = load_config()["kalshi"]
        return cls(cfg["base_url"], cfg["requests_per_second"], cfg["timeout_seconds"])

    def fetch_markets(self, series_ticker: str, status: str = "open") -> list[KalshiMarket]:
        """Every open market in one series, following pagination.

        Kalshi returns results in pages. Each page includes a "cursor"; sending
        it back gets the next page. An empty cursor means we've seen them all.
        """
        markets: list[KalshiMarket] = []
        cursor = None
        while True:
            params = {"series_ticker": series_ticker, "status": status,
                      "limit": 1000, "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            data = self.get("/markets", params)
            markets.extend(parse_market(m, series_ticker) for m in data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                return markets

    def fetch_all_nfl(self, series_tickers: dict[str, str],
                      strict: bool = False) -> dict[str, list[KalshiMarket]]:
        """Fetch every configured series. One failing series doesn't stop the rest,
        unless strict=True, which raises instead (for callers that need ALL of it).

        Returns {"moneyline": [...markets], "qb_pass_yds": [...], ...}.
        """
        results: dict[str, list[KalshiMarket]] = {}
        for kind, ticker in series_tickers.items():
            try:
                results[kind] = self.fetch_markets(ticker)
            except KalshiError as exc:
                if strict:
                    raise
                print(f"[kalshi] WARNING: could not fetch {kind} ({ticker}): {exc}")
                results[kind] = []
        return results

    def fetch_orderbook(self, ticker: str) -> dict:
        """Raw order book for one market. Called only for alert candidates,
        not for every market every cycle, to keep request counts low."""
        return self.get(f"/markets/{ticker}/orderbook")

    def list_series(self, category: str = "Sports") -> list[dict]:
        """List series in a category. Used once, by hand, to find NFL tickers."""
        return self.get("/series", {"category": category}).get("series", [])


# ---------- turning Kalshi's raw JSON into clean numbers ----------

def _price(raw: dict, field: str) -> float | None:
    """Read a price as a probability, or None if there's no real quote.

    Kalshi now sends prices as dollar strings ("yes_ask_dollars": "0.5600").
    Older responses used whole cents ("yes_ask": 56). We accept either.
    A bid of 0 or an ask of 1.00 means "nobody is offering", so -> None.
    """
    value = raw.get(f"{field}_dollars")
    if value not in (None, ""):
        p = float(value)
    elif raw.get(field) is not None:
        p = raw[field] / 100
    else:
        return None
    return p if 0 < p < 1 else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    # Kalshi uses ISO-8601 like "2026-10-18T17:00:00Z". "Z" means UTC.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_market(raw: dict, series_ticker: str = "") -> KalshiMarket:
    """Convert one market dict from the API into a KalshiMarket."""
    yes_bid = _price(raw, "yes_bid")
    yes_ask = _price(raw, "yes_ask")
    # Buying NO is the same as selling YES, so if Kalshi doesn't send NO
    # prices directly we can derive them: NO ask = 1 - YES bid.
    no_bid = _price(raw, "no_bid") or (1 - yes_ask if yes_ask else None)
    no_ask = _price(raw, "no_ask") or (1 - yes_bid if yes_bid else None)
    strike = raw.get("floor_strike")
    return KalshiMarket(
        ticker=raw["ticker"],
        event_ticker=raw.get("event_ticker", ""),
        series_ticker=series_ticker,
        title=raw.get("title", ""),
        yes_sub_title=raw.get("yes_sub_title", ""),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        last_price=_price(raw, "last_price"),
        floor_strike=float(strike) if strike is not None else None,
        strike_type=raw.get("strike_type"),
        close_time=_parse_time(raw.get("close_time")),
        volume=int(float(raw.get("volume_fp") or raw.get("volume") or 0)),
    )


# ---------- order book depth (liquidity) ----------

@dataclass
class Depth:
    """How much you could actually buy at or below a price limit."""

    contracts: float      # number of contracts available
    cost_dollars: float   # total cost to buy all of them
    worst_price: float | None  # the highest price you'd pay along the way


def _bid_levels(raw: dict, side: str) -> list[tuple[float, float]]:
    """Read one side's bids as [(price, quantity), ...].

    Current format:  {"orderbook_fp": {"yes_dollars": [["0.4200", "150.00"], ...]}}
    Older format:    {"orderbook":    {"yes": [[42, 150], ...]}}  (cents)
    """
    fp = (raw.get("orderbook_fp") or {}).get(f"{side}_dollars")
    if fp:
        return [(float(p), float(q)) for p, q in fp]
    old = (raw.get("orderbook") or {}).get(side)
    if old:
        return [(p / 100, float(q)) for p, q in old]
    return []


def buyable_depth(raw_orderbook: dict, side: str, max_price: float) -> Depth:
    """Contracts you could BUY on `side` ("yes" or "no") paying at most max_price.

    Kalshi's book only lists bids. Buying YES means matching someone's NO bid:
    a NO bid at 0.55 is a YES offer at 1 - 0.55 = 0.45. So we flip the
    opposite side's bids into offers, sort cheapest first, and add up every
    level at or under our limit.
    """
    opposite = "no" if side == "yes" else "yes"
    offers = sorted((round(1 - p, 4), q) for p, q in _bid_levels(raw_orderbook, opposite))
    contracts = cost = 0.0
    worst = None
    for price, qty in offers:
        if price > max_price + 1e-9:
            break
        contracts += qty
        cost += price * qty
        worst = price
    return Depth(contracts=contracts, cost_dollars=round(cost, 2), worst_price=worst)


# ---------- run this file directly to see live data ----------

def _fmt(p: float | None) -> str:
    return f"{p * 100:5.1f}c" if p is not None else "   -- "


def main() -> None:
    parser = argparse.ArgumentParser(description="Show live Kalshi NFL prices (read-only).")
    parser.add_argument("--discover", action="store_true",
                        help="list NFL series tickers so you can fill in config.toml")
    args = parser.parse_args()
    client = KalshiClient.from_config()

    if args.discover:
        for s in client.list_series():
            text = f"{s.get('ticker', '')} {s.get('title', '')}".upper()
            if "NFL" in text or "PRO FOOTBALL" in text:
                print(f"{s['ticker']:<28} {s.get('title', '')}")
        return

    series = load_config()["kalshi"]["series"]
    for kind, markets in client.fetch_all_nfl(series).items():
        print(f"\n=== {kind}: {len(markets)} open markets ===")
        print(f"{'ticker':<44} {'YES means':<22} {'bid':>6} {'ask':>6}")
        for m in markets[:15]:
            print(f"{m.ticker:<44} {m.yes_sub_title[:22]:<22} {_fmt(m.yes_bid)} {_fmt(m.yes_ask)}")
        if len(markets) > 15:
            print(f"... and {len(markets) - 15} more")


if __name__ == "__main__":
    main()
