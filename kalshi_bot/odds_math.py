"""Pure math functions for comparing Kalshi prices to sportsbook odds.

"Pure" means: numbers in, numbers out. No internet, no files, no clock.
That makes them easy to test by hand, and every other part of the bot
depends on them being right.

All probabilities are decimals between 0 and 1 (0.5 means 50%).
"""

from statistics import NormalDist


def american_to_implied(odds: float) -> float:
    """Convert American odds to the probability the sportsbook is implying.

    -150 means "bet $150 to win $100"  -> 150 / (150 + 100) = 0.600
    +130 means "bet $100 to win $130"  -> 100 / (130 + 100) = 0.435
    """
    if odds == 0 or -100 < odds < 100:
        raise ValueError(f"Invalid American odds: {odds}")
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def remove_vig(implied_a: float, implied_b: float) -> tuple[float, float]:
    """Strip the sportsbook's built-in profit (the "vig") from a two-sided market.

    Both sides' implied probabilities add up to MORE than 100% (e.g. 104%);
    the extra 4% is the book's cut. Dividing each side by the total scales
    them back so they sum to exactly 100%. This is the "multiplicative"
    method - the simplest and most common one.
    """
    total = implied_a + implied_b
    if total <= 0:
        raise ValueError("Implied probabilities must be positive")
    return implied_a / total, implied_b / total


def kalshi_price_to_prob(cents: float) -> float:
    """Kalshi prices are in cents (1-99). A 62-cent YES pays $1 if it happens,
    so the market is saying 62%. Returns 0.62."""
    if not 0 < cents < 100:
        raise ValueError(f"Kalshi price must be between 0 and 100 cents: {cents}")
    return cents / 100


def edge(fair_prob: float, kalshi_prob: float) -> float:
    """How much the fair probability exceeds what Kalshi charges.

    Positive edge = Kalshi is cheap relative to the sportsbooks.
    Example: fair 0.55, Kalshi 0.50 -> edge 0.05 (5 percentage points).
    """
    return fair_prob - kalshi_prob


def kalshi_fee_per_contract(kalshi_prob: float, fee_rate: float) -> float:
    """Approximate Kalshi taker fee for ONE contract, in dollars.

    Kalshi's formula is fee_rate * price * (1 - price), then rounded UP to the
    next cent on the whole order. We skip the rounding here (it matters less
    for bigger orders), so treat this as an estimate.
    At 50 cents with rate 0.07: 0.07 * 0.5 * 0.5 = $0.0175 per contract.
    """
    return fee_rate * kalshi_prob * (1 - kalshi_prob)


def edge_after_fees(fair_prob: float, kalshi_prob: float, fee_rate: float) -> float:
    """Edge minus the fee you'd pay to buy at that price."""
    return edge(fair_prob, kalshi_prob) - kalshi_fee_per_contract(kalshi_prob, fee_rate)


def prob_at_least(
    threshold: float,
    book_line: float,
    book_over_prob: float,
    std_dev: float,
    integer_stat: bool = True,
) -> float:
    """Estimate P(stat >= threshold) when Kalshi's threshold differs from the book line.

    Example: Kalshi asks "250+ passing yards?" but the sportsbook line is 277.5.

    We model the stat as a bell curve (normal distribution):
      1. Spread (std_dev) comes from config.toml - an ESTIMATE.
      2. Center: if the book's fair over% is exactly 50%, the center is the
         book line. If the over is favored (say 57%), the center is shifted
         a bit above the line so that P(over line) still equals 57%.
         This way the curve agrees with the sportsbook at its own line.
      3. Read P(stat >= threshold) off the curve.

    integer_stat: yards and points are whole numbers, so "250 or more" is the
    same as "more than 249.5". Subtracting 0.5 (a "continuity correction")
    makes the smooth curve match whole-number reality.
    """
    if std_dev <= 0:
        raise ValueError("std_dev must be positive")
    if not 0 < book_over_prob < 1:
        raise ValueError("book_over_prob must be between 0 and 1")

    standard = NormalDist()  # mean 0, std dev 1
    # Step 2: find the center so the curve gives book_over_prob above the line.
    mean = book_line + std_dev * standard.inv_cdf(book_over_prob)
    curve = NormalDist(mu=mean, sigma=std_dev)

    # Step 3: probability of landing at or above the threshold.
    cutoff = threshold - 0.5 if integer_stat else threshold
    return 1 - curve.cdf(cutoff)
