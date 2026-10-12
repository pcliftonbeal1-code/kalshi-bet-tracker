"""Reads NFL sportsbook odds from The Odds API (the-odds-api.com). READ-ONLY.

How The Odds API bills you ("credits"):
  - Listing upcoming games (/events) is free.
  - Game odds for ALL games (/odds) cost about 1 credit per market requested.
  - Props and team totals must be fetched one game at a time
    (/events/{id}/odds) and cost about 1 credit per market, per game.
  Your remaining credits come back in a response header, which we watch
  so the bot stops before burning through your monthly quota.

Libraries used:
  os, dotenv - read ODDS_API_KEY from the .env file (never hard-coded).
"""

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv

from kalshi_bot.config import load_config
from kalshi_bot.http_client import ApiError, HttpClient


class OddsApiError(ApiError):
    """Raised when sportsbook odds can't be fetched (or the budget is spent)."""


@dataclass
class BookOutcome:
    """One price at one sportsbook, e.g. "DraftKings: Mahomes Over 277.5 at -114"."""

    event_id: str
    home_team: str
    away_team: str
    commence_time: datetime   # kickoff, in UTC
    bookmaker: str            # e.g. "pinnacle", "draftkings"
    market: str               # e.g. "h2h", "player_pass_yds", "team_totals"
    name: str                 # team name for h2h; "Over" / "Under" otherwise
    description: str          # player or team the line is about ("" for h2h)
    point: float | None       # the line (277.5); None for h2h
    price: int                # American odds
    fetched_at: datetime      # when WE downloaded it, to judge staleness
    book_updated_at: datetime | None = None  # when the BOOK last changed this price


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_event(event: dict, fetched_at: datetime) -> list[BookOutcome]:
    """Flatten one game's nested JSON (game -> books -> markets -> outcomes)
    into a simple list of BookOutcome rows."""
    rows = []
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            # Our download can be fresh while the book's price is old (e.g. a
            # delayed feed). The API reports when the book last updated it.
            updated = market.get("last_update") or book.get("last_update")
            for o in market.get("outcomes", []):
                rows.append(BookOutcome(
                    event_id=event["id"],
                    home_team=event["home_team"],
                    away_team=event["away_team"],
                    commence_time=_parse_time(event["commence_time"]),
                    bookmaker=book["key"],
                    market=market["key"],
                    name=o["name"],
                    description=o.get("description", ""),
                    point=o.get("point"),
                    price=int(o["price"]),
                    fetched_at=fetched_at,
                    book_updated_at=_parse_time(updated) if updated else None,
                ))
    return rows


class OddsClient(HttpClient):
    error_class = OddsApiError

    def __init__(self, api_key: str, sport: str, bookmakers: list[str],
                 min_credits_reserve: int = 25, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.sport = sport
        self.bookmakers = bookmakers
        self.min_credits_reserve = min_credits_reserve

    @classmethod
    def from_config(cls) -> "OddsClient":
        load_dotenv()  # copies values from .env into the environment
        api_key = os.getenv("ODDS_API_KEY")
        if not api_key:
            raise OddsApiError("ODDS_API_KEY is missing. Put it in your .env file.")
        cfg = load_config()["odds_api"]
        return cls(
            api_key=api_key,
            sport=cfg["sport"],
            bookmakers=[cfg["sharp_book"], *cfg["fallback_books"]],
            min_credits_reserve=cfg["min_credits_reserve"],
            base_url=cfg["base_url"],
            requests_per_second=cfg["requests_per_second"],
            timeout_seconds=cfg["timeout_seconds"],
        )

    def _redact(self, text: str) -> str:
        # The key travels in the URL, so scrub it from any error message.
        return text.replace(self.api_key, "***") if self.api_key else text

    @property
    def credits_remaining(self) -> int | None:
        """Credits left this month, per the last response (None until we've asked)."""
        value = self.last_headers.get("x-requests-remaining")
        return int(float(value)) if value is not None else None

    def _check_budget(self) -> None:
        left = self.credits_remaining
        if left is not None and left < self.min_credits_reserve:
            raise OddsApiError(
                f"Only {left} credits left (reserve is {self.min_credits_reserve}); "
                "skipping paid requests until the quota resets.")

    def _odds_params(self, markets: list[str]) -> dict:
        return {
            "apiKey": self.api_key,
            "bookmakers": ",".join(self.bookmakers),
            "markets": ",".join(markets),
            "oddsFormat": "american",
            "dateFormat": "iso",
        }

    def get_events(self) -> list[dict]:
        """Upcoming games (id, teams, kickoff). Costs no credits."""
        return self.get(f"/sports/{self.sport}/events", {"apiKey": self.api_key})

    def get_game_odds(self, markets: list[str]) -> list[BookOutcome]:
        """Game-level markets (e.g. moneyline) for every upcoming game in one call."""
        self._check_budget()
        fetched_at = datetime.now(timezone.utc)
        events = self.get(f"/sports/{self.sport}/odds", self._odds_params(markets))
        return [row for ev in events for row in parse_event(ev, fetched_at)]

    def get_event_odds(self, event_id: str, markets: list[str]) -> list[BookOutcome]:
        """Props / team totals for ONE game. Also used to re-confirm a single game
        right before alerting, so the alert isn't based on stale odds."""
        self._check_budget()
        fetched_at = datetime.now(timezone.utc)
        event = self.get(f"/sports/{self.sport}/events/{event_id}/odds",
                         self._odds_params(markets))
        return parse_event(event, fetched_at)


# ---------- run this file directly to see live data ----------

def main() -> None:
    from kalshi_bot.fair_value import build_fair_lines

    parser = argparse.ArgumentParser(description="Show sportsbook fair odds (read-only).")
    parser.add_argument("--props", action="store_true",
                        help="also fetch props for the NEXT game only (costs a few credits)")
    args = parser.parse_args()

    cfg = load_config()["odds_api"]
    client = OddsClient.from_config()
    outcomes = client.get_game_odds(cfg["game_markets"])
    if args.props and outcomes:
        next_game = min(outcomes, key=lambda o: o.commence_time).event_id
        outcomes += client.get_event_odds(next_game, cfg["event_markets"])

    lines = build_fair_lines(outcomes, cfg["sharp_book"], cfg["fallback_books"],
                             cfg["min_fallback_books"])
    for line in lines:
        probs = "  ".join(f"{name} {p * 100:.1f}%" for name, p in line.fair.items())
        label = f"{line.away_team} @ {line.home_team} | {line.market} {line.subject} {line.point or ''}"
        print(f"{label:<70} {probs:<45} [{line.source}]")
    print(f"\nCredits remaining this month: {client.credits_remaining}")


if __name__ == "__main__":
    main()
