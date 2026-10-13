"""The alert bot: check Kalshi against the sportsbooks every few minutes. READ-ONLY.

    python -m kalshi_bot.bot          # run until you press Ctrl+C
    python -m kalshi_bot.bot --once   # one check, then exit

Each cycle:
  1. Refresh sportsbook odds only when the cached copy is old (they cost credits).
  2. Read Kalshi prices (free) and match every market to its book line.
  3. For anything that looks like an edge, re-pull THAT game's odds if ours are
     older than confirm_max_age_minutes, and re-check. Old odds find
     candidates cheaply; only fresh odds can trigger an alert.
  4. Check the order book, then print, log, and (optionally) push the alert.

If the sportsbook budget runs low, steps 1 and 3 stop spending. Without
fresh odds nothing can pass step 3, so the bot goes quiet instead of
alerting on stale prices.
"""

import argparse
import os
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from kalshi_bot.alerts import (
    DATA_DIR, Alert, AlertTracker, format_alert, log_alert, max_buy_price, send_push,
)
from kalshi_bot.distributions import Shapes
from kalshi_bot.fair_value import FairLine, build_fair_lines
from kalshi_bot.kalshi_client import KalshiError, buyable_depth
from kalshi_bot.matching import match_all
from kalshi_bot.odds_client import BookOutcome, OddsApiError


class OddsCache:
    """Sportsbook odds we've already paid for, stored per (game, market).
    New downloads replace only the games/markets they contain."""

    def __init__(self):
        self.rows: dict[tuple[str, str], list[BookOutcome]] = {}
        self.last_full_refresh: datetime | None = None
        self.last_props: dict[str, datetime] = {}     # event_id -> when
        self.last_confirm: dict[str, datetime] = {}   # event_id -> when

    def store(self, outcomes: list[BookOutcome]) -> None:
        fresh: dict[tuple[str, str], list[BookOutcome]] = defaultdict(list)
        for o in outcomes:
            fresh[(o.event_id, o.market)].append(o)
        self.rows.update(fresh)

    def outcomes(self) -> list[BookOutcome]:
        return [o for rows in self.rows.values() for o in rows]

    def games(self) -> dict[str, datetime]:
        """event_id -> kickoff, for every game we have odds on."""
        return {o.event_id: o.commence_time for o in self.outcomes()}


def _due(last: datetime | None, now: datetime, minutes: float) -> bool:
    return last is None or now - last >= timedelta(minutes=minutes)


def _lines(cache: OddsCache, odds_cfg: dict) -> list[FairLine]:
    return build_fair_lines(cache.outcomes(), odds_cfg["sharp_book"],
                            odds_cfg["fallback_books"], odds_cfg["min_fallback_books"])


def refresh_odds(odds, cache: OddsCache, cfg: dict, now: datetime) -> None:
    """Step 1: re-buy sportsbook odds only when the cached copy is due."""
    odds_cfg, loop_cfg = cfg["odds_api"], cfg["loop"]
    every = odds_cfg["full_refresh_minutes"]
    if _due(cache.last_full_refresh, now, every):
        cache.store(odds.get_game_odds(odds_cfg["game_markets"]))
        cache.last_full_refresh = now
    if not loop_cfg["props"]:
        return
    window = timedelta(hours=loop_cfg["props_hours_before_kickoff"])
    for event_id, kickoff in sorted(cache.games().items(), key=lambda kv: kv[1]):
        if now < kickoff <= now + window and _due(cache.last_props.get(event_id), now, every):
            cache.store(odds.get_event_odds(event_id, odds_cfg["event_markets"]))
            cache.last_props[event_id] = now


def same_bet(c) -> tuple:
    """Kalshi lists each team's moneyline as its own market, so "YES Dallas" and
    "NO Green Bay" are the same bet. Both win if Dallas wins, so both have the
    same fair probability. Everything else is identified by ticker + side."""
    if c.book_market == "h2h":
        return (c.event_id, "h2h", round(c.fair_prob, 6))
    return (c.kalshi_ticker, c.side)


def run_cycle(kalshi, odds, cache: OddsCache, tracker: AlertTracker, cfg: dict,
              now: datetime | None = None, notify=print,
              log_path=DATA_DIR / "alerts.csv", shapes=None) -> list[Alert]:
    """One full check. Returns the alerts it sent."""
    now = now or datetime.now(timezone.utc)
    odds_cfg, loop_cfg = cfg["odds_api"], cfg["loop"]
    std_devs, fee_rate = cfg["std_devs"], cfg["fees"]["taker_fee_rate"]
    max_age = timedelta(minutes=odds_cfg["confirm_max_age_minutes"])

    try:
        refresh_odds(odds, cache, cfg, now)
    except OddsApiError as exc:
        print(f"[odds] WARNING: {exc} (using cached odds; alerts need fresh ones)")

    markets = kalshi.fetch_all_nfl(cfg["kalshi"]["series"])

    def candidates():
        comps, _ = match_all(markets, _lines(cache, odds_cfg), std_devs, fee_rate, now=now,
                             max_gap_sd=std_devs.get("max_gap_sd", 0.5), shapes=shapes)
        return [c for c in comps if tracker.is_candidate(c)]

    # Step 3: confirm stale candidates with one fresh pull per game.
    found = candidates()
    stale: dict[str, set[str]] = defaultdict(set)
    for c in found:
        if now - c.book_fetched_at > max_age:
            stale[c.event_id].add(c.book_market)
    pulled = False
    for event_id, book_markets in stale.items():
        # A cooldown stops a stubborn fake edge from costing a credit every cycle.
        if not _due(cache.last_confirm.get(event_id), now, loop_cfg["confirm_cooldown_minutes"]):
            continue
        try:
            cache.store(odds.get_event_odds(event_id, sorted(book_markets)))
            cache.last_confirm[event_id] = now
            pulled = True
        except OddsApiError as exc:
            print(f"[odds] WARNING: couldn't confirm game {event_id}: {exc}")
    if pulled:
        found = candidates()   # recompute with the fresh odds

    min_edge = tracker.min_edge
    sent, alerted_bets = [], set()
    # Best edge first, so of two ways to make the same bet we alert the cheaper.
    for c in sorted(found, key=lambda c: c.edge_after_fees, reverse=True):
        if same_bet(c) in alerted_bets:
            tracker.record(c)   # covered by the alert just sent; stay quiet later too
            continue
        if now - c.book_fetched_at > max_age:
            continue  # couldn't confirm; never alert on stale odds
        limit = max_buy_price(c.fair_prob, min_edge, fee_rate)
        if limit is None:
            continue
        try:
            depth = buyable_depth(kalshi.fetch_orderbook(c.kalshi_ticker), c.side, limit)
        except KalshiError as exc:
            print(f"[kalshi] WARNING: no order book for {c.kalshi_ticker}: {exc}")
            depth = None
        if depth is not None and depth.contracts < 1:
            continue  # the cheap price is already gone
        alert = Alert(c, limit, depth, now)
        tracker.record(c)
        alerted_bets.add(same_bet(c))
        log_alert(alert, log_path)
        notify(format_alert(alert))
        sent.append(alert)
    return sent


def main() -> None:
    from kalshi_bot.config import load_config
    from kalshi_bot.kalshi_client import KalshiClient
    from kalshi_bot.odds_client import OddsClient

    parser = argparse.ArgumentParser(description="Kalshi vs sportsbook alert bot (read-only).")
    parser.add_argument("--once", action="store_true", help="run one check, then exit")
    args = parser.parse_args()

    cfg = load_config()
    load_dotenv()
    topic = os.getenv("NTFY_TOPIC", "").strip()

    def notify(text: str) -> None:
        print("\a" + text + "\n")   # \a beeps the terminal
        if topic:
            send_push(text, topic)

    kalshi = KalshiClient.from_config()
    try:
        odds = OddsClient.from_config()
    except OddsApiError as exc:
        raise SystemExit(f"Can't start: {exc}")
    tracker = AlertTracker(cfg["alerts"]["min_edge"], cfg["alerts"]["realert_edge_increase"],
                           DATA_DIR / "alert_state.json")
    cache = OddsCache()
    shapes = Shapes.load()   # real-data line adjustment (distributions.py)
    poll = cfg["loop"]["poll_minutes"] * 60
    print(f"Read-only bot started. Checking every {cfg['loop']['poll_minutes']} min. "
          f"Phone push: {'on' if topic else 'off'}. Ctrl+C to stop.")

    while True:
        started = time.monotonic()
        try:
            alerts = run_cycle(kalshi, odds, cache, tracker, cfg, notify=notify, shapes=shapes)
            print(f"[{datetime.now():%H:%M}] check done: {len(alerts)} new alert(s), "
                  f"{odds.credits_remaining} odds credits left")
        except Exception:   # one bad cycle (API hiccup, odd data) shouldn't kill the bot
            print(f"[{datetime.now():%H:%M}] ERROR in this check; trying again next time:")
            traceback.print_exc()
        if args.once:
            return
        try:
            time.sleep(max(0.0, poll - (time.monotonic() - started)))
        except KeyboardInterrupt:
            print("Stopped.")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
