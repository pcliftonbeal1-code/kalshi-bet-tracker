"""Tests for distributions.py with SAMPLE data (no internet)."""

import math
from statistics import NormalDist

import pytest

from kalshi_bot.distributions import (
    Shapes, blended_prob, build_shapes, implied_totals, ratio_samples,
)

# Rushing ratios: an RB pool whose typical game is ~60 yards. Lopsided on purpose
# (log-normal): a back can have a 2x game more easily than a 0x game.
RATIOS = [math.exp(0.6 * NormalDist().inv_cdf((i + 0.5) / 1000)) for i in range(1000)]
SHAPES = Shapes({"rb_rush": [[60.0, r] for r in RATIOS],
                 "points": sorted(x - 10 for x in range(21) for _ in range(20)),   # -10..10
                 "sd": {"rb_rush": 36.0, "points": 9.0}})


def test_agrees_with_the_book_at_its_own_line():
    # Kalshi 60+ == book Over 59.5: must return the book's own over %.
    for over in (0.40, 0.50, 0.62):
        p, n = SHAPES.prob_at_least("rb_rush", 60, 59.5, over)
        assert p == pytest.approx(over, abs=0.01)
        assert n == 1000


def test_higher_thresholds_are_less_likely():
    probs = [SHAPES.prob_at_least("rb_rush", t, 59.5, 0.5)[0] for t in (40, 50, 60, 70, 80)]
    assert probs == sorted(probs, reverse=True)


def test_lopsided_shape_differs_from_bell_curve():
    # Long right tail: 20 yards ABOVE the line keeps more chance than the
    # symmetric 20 yards below it gives up.
    above = SHAPES.prob_at_least("rb_rush", 80, 59.5, 0.5)[0]
    below = SHAPES.prob_at_least("rb_rush", 40, 59.5, 0.5)[0]
    assert above > 1 - below


def test_points_shift_matches_book():
    p, _ = SHAPES.prob_at_least("points", 25, 24.5, 0.55)
    assert p == pytest.approx(0.55, abs=0.03)
    assert SHAPES.prob_at_least("points", 31, 24.5, 0.5)[0] < 0.3


def test_no_shape_when_pool_too_small():
    tiny = Shapes({"rb_rush": [[60.0, 1.0]] * 10})
    assert tiny.prob_at_least("rb_rush", 70, 59.5, 0.5) is None
    assert tiny.prob_at_least("pass", 250, 244.5, 0.5) is None


def test_blend_falls_back_to_bell_curve():
    p, label = blended_prob(None, "rb_rush", 70, 59.5, 0.5, 36.0)
    assert label == "line-adjusted (sd=36, ESTIMATE)" and 0.3 < p < 0.5
    p2, label2 = blended_prob(SHAPES, "rb_rush", 70, 59.5, 0.5, 36.0)
    assert label2.startswith("line-adjusted (real data, n=1000")
    real = SHAPES.prob_at_least("rb_rush", 70, 59.5, 0.5)[0]
    assert p2 == pytest.approx((p + real) / 2)


def test_ratio_samples_leave_the_game_out():
    # Typical for each game = median of the OTHER games, so a big game
    # doesn't inflate its own baseline.
    rows = ratio_samples({("p", "2024"): [50, 100, 50, 50]})
    assert (50.0, 2.0) in rows


def test_implied_totals_from_spread():
    # Home favored by 4, total 44: home 24, away 20.
    assert implied_totals({"total_line": "44", "spread_line": "4"}) == (24.0, 20.0)
    assert implied_totals({"total_line": "NA", "spread_line": "4"}) is None


def test_build_shapes_end_to_end():
    stats = []
    for wk in range(8):
        stats.append({"player_id": "rb1", "season": "2024", "position": "RB", "game_id": f"g{wk}",
                      "carries": "15", "attempts": "0", "rushing_yards": str(50 + 10 * wk),
                      "passing_yards": "0"})
        stats.append({"player_id": "qb1", "season": "2024", "position": "QB", "game_id": f"g{wk}",
                      "carries": "3", "attempts": "30", "rushing_yards": "12",
                      "passing_yards": str(220 + wk)})
    games = [{"home_score": "27", "away_score": "20", "total_line": "44", "spread_line": "4"}]
    shapes = build_shapes(stats, games)
    assert len(shapes["rb_rush"]) == 8 and len(shapes["pass"]) == 8
    assert shapes["points"] == [0.0, 3.0]          # 20-20, 27-24
    assert shapes["sd"]["qb_rush"] == 0.0           # always 12 yards
