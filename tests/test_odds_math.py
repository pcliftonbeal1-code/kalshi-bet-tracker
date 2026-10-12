"""Unit tests for odds_math.py. Run with:  pytest -v

pytest.approx(x, abs=0.001) means "equal to x, give or take 0.001", because
computers store decimals slightly imprecisely (0.1 + 0.2 = 0.30000000000000004).
"""

import pytest

from kalshi_bot.config import load_config
from kalshi_bot.odds_math import (
    american_to_implied,
    edge,
    edge_after_fees,
    kalshi_fee_per_contract,
    kalshi_price_to_prob,
    prob_at_least,
    remove_vig,
)


# ---------- american_to_implied ----------

def test_negative_odds():
    # 114 / (114 + 100) = 114 / 214 = 0.5327
    assert american_to_implied(-114) == pytest.approx(0.5327, abs=0.0001)


def test_positive_odds():
    # 100 / (124 + 100) = 100 / 224 = 0.4464
    assert american_to_implied(124) == pytest.approx(0.4464, abs=0.0001)


def test_even_money_is_50_percent():
    # +100 and -100 both mean "bet $100 to win $100" -> 50%
    assert american_to_implied(100) == 0.5
    assert american_to_implied(-100) == 0.5


def test_invalid_odds_rejected():
    # American odds between -100 and +100 don't exist.
    with pytest.raises(ValueError):
        american_to_implied(50)


# ---------- remove_vig ----------

def test_remove_vig_symmetric():
    # -114 / -114: each side 53.27%, total 106.5%. Each / total -> exactly 50%.
    a = american_to_implied(-114)
    b = american_to_implied(-114)
    fair_a, fair_b = remove_vig(a, b)
    assert fair_a == pytest.approx(0.500)
    assert fair_b == pytest.approx(0.500)


def test_remove_vig_asymmetric():
    # +124 -> 0.4464, -148 -> 148/248 = 0.5968. Total = 1.0432.
    # 0.4464 / 1.0432 = 0.428 ; 0.5968 / 1.0432 = 0.572
    fair_dog, fair_fav = remove_vig(american_to_implied(124), american_to_implied(-148))
    assert fair_dog == pytest.approx(0.428, abs=0.001)
    assert fair_fav == pytest.approx(0.572, abs=0.001)


def test_remove_vig_sums_to_one():
    a, b = remove_vig(0.55, 0.50)
    assert a + b == pytest.approx(1.0)


# ---------- Kalshi price, edge, fees ----------

def test_kalshi_price_to_prob():
    assert kalshi_price_to_prob(62) == 0.62


def test_kalshi_price_out_of_range():
    with pytest.raises(ValueError):
        kalshi_price_to_prob(100)


def test_edge_positive_when_kalshi_cheap():
    # fair 55%, Kalshi 50 cents -> 5 points of edge
    assert edge(0.55, 0.50) == pytest.approx(0.05)


def test_edge_negative_when_kalshi_expensive():
    assert edge(0.45, 0.50) == pytest.approx(-0.05)


def test_fee_at_50_cents():
    # 0.07 * 0.5 * 0.5 = 0.0175
    assert kalshi_fee_per_contract(0.50, 0.07) == pytest.approx(0.0175)


def test_edge_after_fees():
    # raw edge 0.05, fee at 50c = 0.0175 -> 0.0325 left
    assert edge_after_fees(0.55, 0.50, 0.07) == pytest.approx(0.0325)


# ---------- prob_at_least (line adjustment) ----------

def test_threshold_below_line_even_odds():
    # Book: 277.5 yards, over/under both 50% fair. Kalshi: "250+" (QB sd 70).
    # Center = 277.5. Cutoff = 249.5 (continuity correction).
    # z = (249.5 - 277.5) / 70 = -0.40. P(above z=-0.40) = 0.6554 (z-table).
    p = prob_at_least(threshold=250, book_line=277.5, book_over_prob=0.5, std_dev=70)
    assert p == pytest.approx(0.6554, abs=0.0001)


def test_threshold_above_line_even_odds():
    # Book 277.5, Kalshi "300+". z = (299.5 - 277.5) / 70 = 0.314
    # P(above z=0.314) = 0.3766 (z-table)
    p = prob_at_least(threshold=300, book_line=277.5, book_over_prob=0.5, std_dev=70)
    assert p == pytest.approx(0.3766, abs=0.0005)


def test_threshold_equal_to_line_returns_book_prob():
    # Sanity check: asking about the book's own line must return the book's own
    # probability, whatever it is. (integer_stat=False so no 0.5 shift.)
    p = prob_at_least(threshold=277.5, book_line=277.5, book_over_prob=0.57,
                      std_dev=70, integer_stat=False)
    assert p == pytest.approx(0.57)


def test_favored_over_shifts_probability_up():
    # If the over is favored at the book, P(250+) should be higher than the
    # 0.6554 we got with a 50/50 line.
    p = prob_at_least(threshold=250, book_line=277.5, book_over_prob=0.60, std_dev=70)
    assert p > 0.6554


def test_bad_std_dev_rejected():
    with pytest.raises(ValueError):
        prob_at_least(250, 277.5, 0.5, std_dev=0)


# ---------- config ----------

def test_config_loads_std_devs():
    cfg = load_config()
    assert cfg["std_devs"]["qb_pass_yds"] == 70
    assert cfg["std_devs"]["rb_rush_yds"] == 30
    assert cfg["std_devs"]["team_points"] == 10
    assert cfg["alerts"]["min_edge"] == 0.03
