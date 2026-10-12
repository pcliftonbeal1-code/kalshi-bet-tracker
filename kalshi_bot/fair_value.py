"""Turn raw sportsbook prices into one fair probability per line.

Rule (from the project spec):
  - If Pinnacle (the "sharp" book) offers the line, trust Pinnacle alone.
  - Otherwise, de-vig each US book separately and AVERAGE their fair
    probabilities. (Averaging after de-vigging is more accurate than
    averaging the raw odds, because each book's vig is removed on its own.)
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from kalshi_bot.odds_client import BookOutcome
from kalshi_bot.odds_math import american_to_implied, remove_vig


@dataclass
class FairLine:
    """The fair probabilities for one two-sided line, e.g. Mahomes O/U 277.5."""

    event_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    market: str                 # "h2h", "player_pass_yds", "team_totals", ...
    subject: str                # player/team the line is about ("" for h2h)
    point: float | None         # the line; None for h2h
    fair: dict[str, float]      # {"Over": 0.53, "Under": 0.47} or {team: prob}
    source: str                 # "pinnacle" or "avg(draftkings,fanduel)"
    book_odds: str              # the raw prices used, for the CSV log
    fetched_at: datetime        # oldest download time among the prices used
    book_updated_at: datetime | None = None  # oldest "book last changed it" time
    # When Pinnacle is the source AND enough US books also have this exact
    # line, their average goes here as a second opinion. A delayed Pinnacle
    # feed then can't trigger an alert on its own (see conservative_fair).
    check_fair: dict[str, float] | None = None
    check_source: str = ""


def _devig_one_book(pair: list[BookOutcome]) -> dict[str, float]:
    fair_a, fair_b = remove_vig(american_to_implied(pair[0].price),
                                american_to_implied(pair[1].price))
    return {pair[0].name: fair_a, pair[1].name: fair_b}


def _average_fair(pairs: list[list[BookOutcome]]) -> dict[str, float]:
    """De-vig each book separately, then average each side across books."""
    per_book = [_devig_one_book(p) for p in pairs]
    return {n: sum(fb[n] for fb in per_book) / len(per_book) for n in per_book[0]}


def _describe(pair: list[BookOutcome]) -> str:
    sides = " / ".join(f"{o.name} {o.price:+d}" for o in pair)
    return f"{pair[0].bookmaker}: {sides}"


def build_fair_lines(outcomes: list[BookOutcome], sharp_book: str,
                     fallback_books: list[str], min_fallback_books: int = 2
                     ) -> list[FairLine]:
    """Group prices into two-sided lines and compute a fair line for each."""
    # Step 1: put each book's two sides of the same line together.
    # Key = (game, market, subject, line, book) -> [side A, side B]
    pairs: dict[tuple, list[BookOutcome]] = defaultdict(list)
    for o in outcomes:
        pairs[(o.event_id, o.market, o.description, o.point, o.bookmaker)].append(o)

    # Step 2: regroup by line (dropping the book), keeping only proper 2-sided pairs.
    by_line: dict[tuple, dict[str, list[BookOutcome]]] = defaultdict(dict)
    for (event_id, market, subject, point, book), pair in pairs.items():
        if len(pair) == 2:
            by_line[(event_id, market, subject, point)][book] = pair

    # Step 3: pick the source for each line and compute fair probabilities.
    lines = []
    for (event_id, market, subject, point), books in by_line.items():
        us_books = [books[b] for b in fallback_books if b in books]
        us_source = f"avg({','.join(p[0].bookmaker for p in us_books)})"
        has_us_consensus = len(us_books) >= min_fallback_books

        if sharp_book in books:
            used = [books[sharp_book]]
            source = sharp_book
            check_fair = _average_fair(us_books) if has_us_consensus else None
        elif has_us_consensus:
            used, source, check_fair = us_books, us_source, None
        else:
            continue  # not enough books agree on this exact line; skip it

        fair = _average_fair(used)

        first = used[0][0]
        lines.append(FairLine(
            event_id=event_id,
            home_team=first.home_team,
            away_team=first.away_team,
            commence_time=first.commence_time,
            market=market,
            subject=subject,
            point=point,
            fair=fair,
            source=source,
            book_odds="; ".join(_describe(p) for p in (used + us_books if check_fair else used)),
            fetched_at=min(o.fetched_at for p in used for o in p),
            book_updated_at=min((o.book_updated_at for p in used for o in p
                                 if o.book_updated_at), default=None),
            check_fair=check_fair,
            check_source=us_source if check_fair else "",
        ))
    return lines


def conservative_fair(line: FairLine, side: str) -> float:
    """The LOWER of the main fair % and the second opinion, for one side.

    Using the lower number means an alert only fires if the edge exists by
    BOTH Pinnacle's and the US books' estimates. Fewer alerts, fewer false ones.
    """
    main = line.fair[side]
    if line.check_fair and side in line.check_fair:
        return min(main, line.check_fair[side])
    return main
