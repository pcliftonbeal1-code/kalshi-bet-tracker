"""Reads NFL market prices from Kalshi's public API. READ-ONLY.

This file only ever sends GET requests (the HTTP verb for "read").
There is no code here that can place, change, or cancel an order.

Libraries used:
  requests - the standard Python library for talking to websites/APIs.
  time     - built in; used to pause between requests (rate limiting).
  dataclasses - built in; a quick way to define a "record" with named fields.
"""

import argparse
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from kalshi_bot.config import load_config

# Status codes that mean "try again later" rather than "you did something wrong".
# 429 = too many requests (rate limited); 5xx = Kalshi's server had a problem.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class KalshiError(Exception):
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


class KalshiClient:
    """Wraps the HTTP details: base URL, timeouts, rate limiting, retries."""

    def __init__(self, base_url: str, requests_per_second: float = 5,
                 timeout_seconds: float = 15, max_retries: int = 4,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval = 1.0 / requests_per_second
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        # A Session reuses the same connection for many requests (faster).
        self.session = session or requests.Session()
        self._last_request_at = 0.0

    @classmethod
    def from_config(cls) -> "KalshiClient":
        cfg = load_config()["kalshi"]
        return cls(cfg["base_url"], cfg["requests_per_second"], cfg["timeout_seconds"])

    def _wait_for_rate_limit(self) -> None:
        """Sleep just long enough to keep under requests_per_second."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET a Kalshi endpoint and return the JSON body as a dict.

        Retries with "exponential backoff" (wait 1s, 2s, 4s, 8s) on network
        errors and retryable status codes. Raises KalshiError if all tries fail.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_problem = ""
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(2 ** (attempt - 1))
            self._wait_for_rate_limit()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # no internet, DNS, timeout...
                last_problem = f"network error: {exc}"
                continue
            if resp.status_code in RETRYABLE_STATUS:
                last_problem = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != 200:
                # 400/401/404 won't fix themselves by retrying.
                raise KalshiError(f"GET {url} failed: HTTP {resp.status_code} {resp.text[:200]}")
            return resp.json()
        raise KalshiError(f"GET {url} gave up after {self.max_retries + 1} tries ({last_problem})")

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

    def fetch_all_nfl(self, series_tickers: dict[str, str]) -> dict[str, list[KalshiMarket]]:
        """Fetch every configured series. One failing series doesn't stop the rest.

        Returns {"moneyline": [...markets], "qb_pass_yds": [...], ...}.
        """
        results: dict[str, list[KalshiMarket]] = {}
        for kind, ticker in series_tickers.items():
            try:
                results[kind] = self.fetch_markets(ticker)
            except KalshiError as exc:
                print(f"[kalshi] WARNING: could not fetch {kind} ({ticker}): {exc}")
                results[kind] = []
        return results

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
