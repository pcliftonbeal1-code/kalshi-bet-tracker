"""Decide which comparisons deserve an alert, then log and send them.

An alert means "worth a human look". The bot never trades.

A comparison becomes an alert only if ALL of these hold:
  1. Its NET edge (after Kalshi's taker fee) is at least min_edge.
  2. We haven't already alerted on it, or its edge grew by realert_edge_increase.
  3. The sportsbook odds behind it are fresh (bot.py re-pulls them if not).
  4. The order book has contracts for sale at a price that still clears min_edge.

Libraries used:
  csv, json - built in; the alert log (CSV opens in Excel/Sheets) and the
              "already alerted" memory that survives restarts.
"""

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from kalshi_bot.kalshi_client import Depth
from kalshi_bot.matching import Comparison
from kalshi_bot.odds_math import edge_after_fees

# data/ is in .gitignore, so logs and state never get committed.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def max_buy_price(fair_prob: float, min_edge: float, fee_rate: float) -> float | None:
    """The highest whole-cent price that still leaves min_edge after fees.

    Fair 50%, min edge 3%, fee rate 0.07: at 45c the net edge is 3.27%,
    at 46c it's 2.26%, so the answer is 0.45. None if no price works.
    """
    for cents in range(99, 0, -1):
        price = cents / 100
        if edge_after_fees(fair_prob, price, fee_rate) >= min_edge - 1e-9:
            return price
    return None


class AlertTracker:
    """Remembers the edge we last alerted on for each market side, so the same
    edge doesn't ping you every 5 minutes. Saved to disk to survive restarts."""

    def __init__(self, min_edge: float, realert_increase: float, path: Path | None = None):
        self.min_edge = min_edge
        self.realert_increase = realert_increase
        self.path = path
        self.last_edge: dict[str, float] = {}
        if path and path.exists():
            self.last_edge = json.loads(path.read_text())

    @staticmethod
    def _key(c: Comparison) -> str:
        return f"{c.kalshi_ticker}|{c.side}"

    def is_candidate(self, c: Comparison) -> bool:
        if c.edge_after_fees < self.min_edge - 1e-9:
            return False
        last = self.last_edge.get(self._key(c))
        return last is None or c.edge_after_fees >= last + self.realert_increase - 1e-9

    def record(self, c: Comparison) -> None:
        self.last_edge[self._key(c)] = c.edge_after_fees
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.last_edge, indent=1))


@dataclass
class Alert:
    comparison: Comparison
    max_price: float          # don't pay more than this (keeps min_edge after fees)
    depth: Depth | None       # contracts for sale up to max_price; None = book unreadable
    alerted_at: datetime


def format_alert(a: Alert) -> str:
    c = a.comparison
    mins = int((c.commence_time - a.alerted_at).total_seconds() // 60)
    kickoff = f"{mins // 60}h {mins % 60}m" if mins >= 0 else "started"
    if a.depth is None:
        depth = "order book unavailable - check Kalshi before buying"
    else:
        depth = (f"{a.depth.contracts:.0f} contracts for sale at <= {a.max_price * 100:.0f}c "
                 f"(${a.depth.cost_dollars:,.2f} total)")
    return "\n".join([
        f"EDGE {c.edge_after_fees * 100:+.1f}% net: BUY {c.side.upper()} {c.kalshi_title}",
        f"  Kalshi {c.kalshi_price * 100:.0f}c vs fair {c.fair_prob * 100:.1f}% "
        f"(gross {c.edge * 100:+.1f}%) | {c.method}",
        f"  Don't pay over {a.max_price * 100:.0f}c. {depth}",
        f"  Book: {c.book_line} [{c.book_source}] {c.book_odds}",
        f"  Kickoff in {kickoff} | {c.kalshi_ticker}",
        "  The bot doesn't trade. Check the price on Kalshi yourself first.",
    ])


ALERT_FIELDS = [
    "alerted_at", "kalshi_ticker", "kalshi_title", "side", "kalshi_price", "fair_prob",
    "edge", "edge_after_fees", "max_price", "depth_contracts", "depth_cost_dollars",
    "method", "book_market", "book_line", "book_source", "book_odds", "book_fetched_at",
    "book_updated_at", "event_id", "commence_time",
]


def log_alert(a: Alert, path: Path) -> None:
    """Append one row to the alert CSV. This log is how we'll judge, after a few
    weekends of paper trading, whether the edges are real."""
    c = a.comparison
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ALERT_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({
            "alerted_at": a.alerted_at.isoformat(),
            "kalshi_ticker": c.kalshi_ticker,
            "kalshi_title": c.kalshi_title,
            "side": c.side,
            "kalshi_price": f"{c.kalshi_price:.4f}",
            "fair_prob": f"{c.fair_prob:.4f}",
            "edge": f"{c.edge:.4f}",
            "edge_after_fees": f"{c.edge_after_fees:.4f}",
            "max_price": f"{a.max_price:.2f}",
            "depth_contracts": "" if a.depth is None else f"{a.depth.contracts:.0f}",
            "depth_cost_dollars": "" if a.depth is None else f"{a.depth.cost_dollars:.2f}",
            "method": c.method,
            "book_market": c.book_market,
            "book_line": c.book_line,
            "book_source": c.book_source,
            "book_odds": c.book_odds,
            "book_fetched_at": c.book_fetched_at.isoformat(),
            "book_updated_at": c.book_updated_at.isoformat() if c.book_updated_at else "",
            "event_id": c.event_id,
            "commence_time": c.commence_time.isoformat(),
        })


def send_push(text: str, topic: str) -> None:
    """Phone notification through ntfy.sh (free app, no account needed).

    Anyone who knows the topic name can read your alerts, so make it
    hard to guess. A failed push only prints a warning; the bot keeps going.
    """
    title, _, body = text.partition("\n")
    try:
        resp = requests.post(f"https://ntfy.sh/{topic}", data=body.encode(),
                             headers={"Title": title[:200]}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[notify] WARNING: phone push failed: {exc}")
