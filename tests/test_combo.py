"""Tests for combo.py and correlation.py with SAMPLE data (no internet)."""

import math

import pytest

from kalshi_bot.combo import (
    Leg, describe_leg, evaluate, pick_legs, player_team, simulate,
)
from kalshi_bot.correlation import build_table, latent_correlation, team_games
from kalshi_bot.kalshi_client import parse_market

BUFLAR = {"Buffalo Bills", "Los Angeles Rams"}
# A tiny table: same-team pass vs RB rush fights (-0.3), RB rush helps margin (+0.4).
TABLE = {"same": {"pass|rb_rush": -0.3, "margin|rb_rush": 0.4}, "opp": {"margin|margin": -1.0}}
SIMS = 40_000


def leg(n, side="yes", var="pass", team="Buffalo Bills", who="Josh Allen", fair=0.5,
        price=0.45, event="26OCT12BUFLAR", net=0.03):
    return Leg(n, f"T{n}", side, f"leg {n}", event, var, team, who, fair, price, net, "exact")


# ---------- reading Kalshi tickers ----------

def test_player_team_from_ticker_segment():
    assert player_team("SEASDARNOLD14", {"Seattle Seahawks", "Denver Broncos"}) == "Seattle Seahawks"
    assert player_team("LARPNACUA12", BUFLAR) == "Los Angeles Rams"     # LAR, not LA + "RPNACUA"
    assert player_team("BUFJALLEN17", BUFLAR) == "Buffalo Bills"
    assert player_team("KCPMAHOMES15", BUFLAR) is None                 # not in this game


def test_describe_leg_each_kind():
    def km(ticker, title):
        return parse_market({"ticker": ticker, "event_ticker": "-".join(ticker.split("-")[:2]),
                             "title": title, "yes_sub_title": ""})
    qbs = {"Josh Allen"}
    assert describe_leg("moneyline", km("KXNFLGAME-26OCT12BUFLAR-BUF", "Buffalo wins"), qbs) == \
        ("margin", "Buffalo Bills", "Buffalo Bills")
    assert describe_leg("team_points", km("KXNFLTEAMTOTAL-26OCT12BUFLAR-LAR29", "LA Rams over"), qbs) == \
        ("points", "Los Angeles Rams", "Los Angeles Rams")
    assert describe_leg("qb_pass_yds", km("KXNFLPASSYDS-26OCT12BUFLAR-BUFJALLEN17-250",
                                          "Josh Allen: 250+ passing yards"), qbs)[0] == "pass"
    # Same series, but a QB's rushing behaves differently from an RB's
    assert describe_leg("rb_rush_yds", km("KXNFLRSHYDS-26OCT12BUFLAR-BUFJALLEN17-30",
                                          "Josh Allen: 30+ rushing yards"), qbs)[0] == "qb_rush"
    assert describe_leg("rb_rush_yds", km("KXNFLRSHYDS-26OCT12BUFLAR-BUFJCOOK4-70",
                                          "James Cook III: 70+ rushing yards"), qbs)[0] == "rb_rush"


# ---------- simulation ----------

def test_single_leg_keeps_its_fair_price():
    assert simulate([leg(1, fair=0.3)], TABLE, SIMS) == pytest.approx(0.3, abs=0.01)


def test_different_games_are_independent():
    legs = [leg(1, fair=0.6), leg(2, fair=0.5, event="26OCT19WASSF")]
    assert simulate(legs, TABLE, SIMS) == pytest.approx(0.30, abs=0.01)


def test_same_stat_two_thresholds_hit_together():
    # Allen 225+ (60%) and Allen 250+ (45%): if 250+ hits, 225+ always hits.
    legs = [leg(1, fair=0.60), leg(2, fair=0.45)]
    assert simulate(legs, TABLE, SIMS) == pytest.approx(0.45, abs=0.01)


def test_fighting_legs_hit_together_less_often():
    legs = [leg(1, var="pass", fair=0.5), leg(2, var="rb_rush", who="James Cook", fair=0.5)]
    # Bivariate normal at the medians: P = 1/4 + asin(rho) / (2 pi), rho = -0.3
    assert simulate(legs, TABLE, SIMS) == pytest.approx(0.25 + math.asin(-0.3) / (2 * math.pi), abs=0.01)


def test_no_side_flips_the_relationship():
    # NO on Allen yards + YES on Cook yards now HELP each other.
    legs = [leg(1, side="no", var="pass", fair=0.5), leg(2, var="rb_rush", who="James Cook", fair=0.5)]
    assert simulate(legs, TABLE, SIMS) == pytest.approx(0.25 + math.asin(0.3) / (2 * math.pi), abs=0.01)


def test_yes_one_team_and_no_other_team_are_the_same_bet():
    legs = [leg(1, var="margin", who="Buffalo Bills", fair=0.4),
            leg(2, side="no", var="margin", team="Los Angeles Rams", who="Los Angeles Rams", fair=0.4)]
    assert simulate(legs, TABLE, SIMS) == pytest.approx(0.4, abs=0.01)


# ---------- the verdict ----------

def test_gate_rejects_combo_with_an_overpriced_leg():
    r = evaluate([leg(1, net=0.02), leg(2, net=-0.01, event="X")], 0.20, TABLE, 0.07, sims=SIMS)
    assert [x.number for x in r.failed_gate] == [2]
    assert r.verdict.startswith("NO COMBO")


def test_value_math_and_stake():
    # Two independent 50% legs: fair 25%. Quote 20c, fee 0.07*0.2*0.8 = 1.12c.
    legs = [leg(1), leg(2, event="X")]
    r = evaluate(legs, 0.20, TABLE, 0.07, bankroll=1000, stake_pct=0.005, sims=SIMS)
    assert r.fee == pytest.approx(0.0112)
    assert r.roi_after_fees == pytest.approx((0.25 - 0.2112) / 0.2112, abs=0.05)
    assert r.verdict == "VALUE" and r.stake == 5.0
    assert r.conflicts == []


def test_conflict_is_flagged():
    legs = [leg(1, var="pass"), leg(2, var="rb_rush", who="James Cook")]
    (a, b, rho), = evaluate(legs, 0.2, TABLE, 0.07, sims=1000).conflicts
    assert rho == pytest.approx(-0.3)


def test_pick_legs():
    legs = [leg(3), leg(3, side="no"), leg(7)]
    assert [(x.number, x.side) for x in pick_legs(legs, ["3n", "7y"])] == [(3, "no"), (7, "yes")]
    with pytest.raises(ValueError):
        pick_legs(legs, ["7n"])
    with pytest.raises(ValueError):
        pick_legs(legs, ["seven"])


# ---------- learning correlations from game logs ----------

def test_latent_correlation_extremes():
    assert latent_correlation([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert latent_correlation([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_build_table_from_game_logs():
    games, stats = [], []
    # Four games where the winner always ran more and the loser always threw more.
    for i in range(4):
        gid = f"g{i}"
        games.append({"game_id": gid, "home_team": "BUF", "away_team": "LA",
                      "home_score": str(20 + i), "away_score": str(17 - i)})
        for team, pass_yds, rush_yds in (("BUF", 200 - i, 90 + i), ("LA", 300 + i, 40 - i)):
            stats.append({"game_id": gid, "team": team, "position": "QB", "attempts": "30",
                          "carries": "3", "passing_yards": str(pass_yds), "rushing_yards": "10"})
            stats.append({"game_id": gid, "team": team, "position": "RB", "attempts": "0",
                          "carries": "15", "passing_yards": "0", "rushing_yards": str(rush_yds)})
    tg = team_games(stats, games)
    assert tg[("g0", "BUF")]["margin"] == 3 and tg[("g0", "LA")]["rb_rush"] == 40
    table = build_table(tg)
    assert table["team_games"] == 8
    assert table["opp"]["margin|margin"] == pytest.approx(-1.0)
    assert table["same"]["margin|rb_rush"] > 0.5
