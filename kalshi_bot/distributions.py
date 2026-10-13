"""Real-data shapes for line adjustment, replacing the bell-curve guesses.

When Kalshi asks "70+ rushing yards" and the book's line is 59.5, we need the
chance of landing between 60 and 69. The old way used a symmetric bell curve
with a guessed width. Real stats are lopsided (a back can't run for -60, but
can run for +100) and their spread grows with the player's role. So instead
we measure it from four seasons of games:

  pass, qb_rush, rb_rush  - each game's yards divided by that player's typical
                            game that season (median of his OTHER games), pooled
                            from players whose typical game is near today's line.
  points                  - each team's points minus what the sportsbook
                            expected (from the closing spread and total).

Then we line the shape up with the sportsbook (so it agrees with the book at
the book's own line) and read off the chance at Kalshi's threshold.

    python -m kalshi_bot.distributions             # build the shapes file
    python -m kalshi_bot.distributions --backtest  # prove it beats the bell curve
"""

import argparse
import bisect
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist, median, pstdev

from kalshi_bot.correlation import _num, load_nflverse

SHAPES_PATH = Path(__file__).resolve().parent / "nfl_distributions.json"
RATIO_VARS = ("pass", "qb_rush", "rb_rush")
MIN_GAMES = 6          # a player-season needs this many games to define "typical"
MIN_SAMPLE = 300       # pooled games needed before we trust a shape


# ---------- building the shapes from game logs ----------

def _player_games(stats_rows: list[dict]) -> dict[str, dict[tuple, list[float]]]:
    """var -> {(player, season): [yards per game, ...]} for regular roles only.

    Every game the player appeared in counts (even a game he left hurt),
    because a Kalshi prop doesn't void once he's played a snap.
    """
    seasons: dict[tuple, list[dict]] = defaultdict(list)
    for r in stats_rows:
        if r["position"] in ("QB", "RB"):
            seasons[(r["player_id"], r["season"], r["position"])].append(r)
    out: dict[str, dict[tuple, list[float]]] = {v: {} for v in RATIO_VARS}
    for (pid, season, pos), games in seasons.items():
        if pos == "QB":
            games = [g for g in games if _num(g["attempts"]) >= 10]   # skip mop-up cameos
            if len(games) < MIN_GAMES or median(_num(g["attempts"]) for g in games) < 20:
                continue
            out["pass"][(pid, season)] = [_num(g["passing_yards"]) for g in games]
            out["qb_rush"][(pid, season)] = [_num(g["rushing_yards"]) for g in games]
        else:
            games = [g for g in games if _num(g["carries"]) >= 1]
            if len(games) < MIN_GAMES or median(_num(g["carries"]) for g in games) < 8:
                continue
            out["rb_rush"][(pid, season)] = [_num(g["rushing_yards"]) for g in games]
    return out


def ratio_samples(games_by_player: dict[tuple, list[float]]) -> list[tuple[float, float]]:
    """(typical, ratio) for every game: typical = median of his OTHER games."""
    out = []
    for values in games_by_player.values():
        for i, v in enumerate(values):
            typical = median(values[:i] + values[i + 1:])
            if typical > 0:
                out.append((typical, v / typical))
    return out


def implied_totals(game: dict) -> tuple[float, float] | None:
    """(home, away) points the sportsbook expected, from closing spread/total.
    nflverse spread_line is from the home side: +4 means home favored by 4."""
    try:
        total, spread = float(game["total_line"]), float(game["spread_line"])
    except (KeyError, ValueError):
        return None
    return total / 2 + spread / 2, total / 2 - spread / 2


def points_residuals(games_rows: list[dict]) -> list[float]:
    out = []
    for g in games_rows:
        if g.get("home_score") in ("", "NA", None):
            continue
        implied = implied_totals(g)
        if implied:
            out.append(_num(g["home_score"]) - implied[0])
            out.append(_num(g["away_score"]) - implied[1])
    return out


def measured_sds(per_var: dict, residuals: list[float]) -> dict[str, float]:
    """Typical game-to-game swing for each stat: std dev of (actual - typical)."""
    sds = {}
    for var in RATIO_VARS:
        diffs = [v - median(vals[:i] + vals[i + 1:])
                 for vals in per_var[var].values() for i, v in enumerate(vals)]
        sds[var] = round(pstdev(diffs), 1)
    sds["points"] = round(pstdev(residuals), 1)
    return sds


def build_shapes(stats_rows: list[dict], games_rows: list[dict]) -> dict:
    per_var = _player_games(stats_rows)
    residuals = points_residuals(games_rows)
    shapes = {v: sorted([round(t, 1), round(r, 4)] for t, r in ratio_samples(per_var[v]))
              for v in RATIO_VARS}
    shapes["points"] = sorted(round(x, 2) for x in residuals)
    shapes["sd"] = measured_sds(per_var, residuals)
    return shapes


# ---------- using the shapes ----------

def _quantile(sorted_vals: list[float], p: float) -> float:
    pos = p * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _share_at_least(sorted_vals: list[float], x: float) -> float:
    return (len(sorted_vals) - bisect.bisect_left(sorted_vals, x)) / len(sorted_vals)


class Shapes:
    """Loaded shapes plus the probability lookup."""

    def __init__(self, data: dict):
        self.ratio = {v: data[v] for v in RATIO_VARS if v in data}   # [[typical, ratio], ...]
        self.typicals = {v: [t for t, _ in rows] for v, rows in self.ratio.items()}
        self.points = data.get("points", [])
        self.sd: dict[str, float] = data.get("sd", {})
        self._pool_cache: dict[tuple, list[float]] = {}

    @classmethod
    def load(cls, path: Path = SHAPES_PATH) -> "Shapes | None":
        return cls(json.loads(path.read_text())) if path.exists() else None

    def pool(self, var: str, line: float) -> list[float]:
        """Sorted ratios from players whose typical game is near `line`.
        Starts at +/-30% and widens until there are MIN_SAMPLE games."""
        key = (var, round(line, 1))
        if key in self._pool_cache:
            return self._pool_cache[key]
        rows, typ = self.ratio.get(var, []), self.typicals.get(var, [])
        pool: list[float] = []
        width = 1.3
        while width < 4 and len(pool) < MIN_SAMPLE:
            lo, hi = bisect.bisect_left(typ, line / width), bisect.bisect_right(typ, line * width)
            pool = sorted(r for _, r in rows[lo:hi])
            width *= 1.15
        self._pool_cache[key] = pool if len(pool) >= MIN_SAMPLE else []
        return self._pool_cache[key]

    def prob_at_least(self, var: str, threshold: int, line: float,
                      over_prob: float) -> tuple[float, int] | None:
        """P(stat >= threshold), agreeing with the book's over_prob at its line.
        Returns (probability, games behind it), or None if there's no shape."""
        cutoff = threshold - 0.5          # whole yards/points: 70+ means above 69.5
        if var == "points":
            res = self.points
            if len(res) < MIN_SAMPLE:
                return None
            # Shift so P(points > line) = over_prob, then read the threshold.
            shift = _quantile(res, 1 - over_prob)
            p = _share_at_least(res, cutoff - line + shift)
            n = len(res)
        else:
            if line <= 0:
                return None
            pool = self.pool(var, line)
            if not pool:
                return None
            # Scale so P(yards > line) = over_prob, then read the threshold.
            scale = _quantile(pool, 1 - over_prob)
            p = _share_at_least(pool, cutoff * scale / line)
            n = len(pool)
        return min(max(p, 0.001), 0.999), n


def blended_prob(shapes: "Shapes | None", var: str, threshold: int, line: float,
                 over_prob: float, sd: float) -> tuple[float, str]:
    """The line-adjusted fair chance the bot actually uses, plus a label.

    Average of (a) the real-data shape and (b) a bell curve with the MEASURED
    width. The backtest (python -m kalshi_bot.distributions --backtest) shows
    this average beats either one alone, and beats the old guessed widths.
    Falls back to the bell curve alone when there's no real-data shape.
    """
    from kalshi_bot.odds_math import prob_at_least

    bell = prob_at_least(threshold, line, over_prob, sd)
    real = shapes.prob_at_least(var, threshold, line, over_prob) if shapes else None
    if real is None:
        return bell, f"line-adjusted (sd={sd:g}, ESTIMATE)"
    return (bell + real[0]) / 2, f"line-adjusted (real data, n={real[1]}, ESTIMATE)"


# ---------- backtest: does it beat the bell curve? ----------

def _brier(pred: list[float], hit: list[int]) -> float:
    return sum((p - h) ** 2 for p, h in zip(pred, hit)) / len(pred)


GUESSED_SD = {"pass": 70, "qb_rush": 30, "rb_rush": 30, "points": 10}  # the original config
STEPS = {"pass": (25, 50, 75), "qb_rush": (10, 20), "rb_rush": (10, 20, 30), "points": (3, 7, 10)}


def backtest(train_stats, train_games, test_stats, test_games) -> dict:
    """Fit on earlier seasons, then predict a later season, four ways:
    guessed bell curve, measured bell curve, real-data shape, and the blend.

    "Line" for a test game = that player's typical game that season (median of
    his OTHER games), a stand-in for a sportsbook line, which nflverse doesn't
    have for props. Team points use the real closing lines. Every method gets
    the same line, so the comparison is fair.
    Returns {(var, step): {"n", "actual", method: (avg prediction, brier)}}.
    """
    from kalshi_bot.odds_math import prob_at_least

    data = build_shapes(train_stats, train_games)
    shapes = Shapes(data)
    cases: dict[str, list[tuple[float, float]]] = defaultdict(list)
    per_var = _player_games(test_stats)
    for var in RATIO_VARS:
        for vals in per_var[var].values():
            for i, v in enumerate(vals):
                typical = median(vals[:i] + vals[i + 1:])
                if typical > 0:
                    cases[var].append((typical, v))
    for g in test_games:
        implied = implied_totals(g)
        if implied and g.get("home_score") not in ("", "NA", None):
            cases["points"] += [(implied[0], _num(g["home_score"])),
                                (implied[1], _num(g["away_score"]))]

    report = {}
    for var, rows in cases.items():
        for step in [st * d for st in STEPS[var] for d in (-1, 1)]:
            preds: dict[str, list[float]] = defaultdict(list)
            hits = []
            for line, actual in rows:
                threshold = math.floor(line) + 1 + step
                if threshold <= 0 or not shapes.prob_at_least(var, threshold, line, 0.5):
                    continue
                preds["guessed"].append(prob_at_least(threshold, line, 0.5, GUESSED_SD[var]))
                preds["measured"].append(prob_at_least(threshold, line, 0.5, data["sd"][var]))
                preds["real"].append(shapes.prob_at_least(var, threshold, line, 0.5)[0])
                preds["blend"].append(blended_prob(shapes, var, threshold, line, 0.5,
                                                   data["sd"][var])[0])
                hits.append(int(actual >= threshold))
            if hits:
                report[(var, step)] = {"n": len(hits), "actual": sum(hits) / len(hits),
                                       **{k: (sum(p) / len(p), _brier(p, hits))
                                          for k, p in preds.items()}}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build / test real-data line adjustment.")
    parser.add_argument("--seasons", nargs="+", type=int, default=[2022, 2023, 2024, 2025])
    parser.add_argument("--backtest", action="store_true",
                        help="fit on all but the last season, test on the last")
    args = parser.parse_args()
    stats, games = load_nflverse(args.seasons)

    if args.backtest:
        test = str(args.seasons[-1])
        report = backtest([r for r in stats if r["season"] != test],
                          [g for g in games if g["season"] != test],
                          [r for r in stats if r["season"] == test],
                          [g for g in games if g["season"] == test])
        methods = ("guessed", "measured", "real", "blend")
        print(f"Test season {test}: predicted chance vs what actually happened.")
        print(f"{'stat':<8} {'vs line':>7} {'cases':>6} {'actual':>7}  " +
              "  ".join(f"{m:>8}" for m in methods) + "   most accurate")
        totals = defaultdict(float)
        count = 0
        for (var, step), r in sorted(report.items()):
            best = min(methods, key=lambda m: r[m][1])
            print(f"{var:<8} {step:>+7} {r['n']:>6} {r['actual'] * 100:6.1f}%  " +
                  "  ".join(f"{r[m][0] * 100:7.1f}%" for m in methods) + f"   {best}")
            for m in methods:
                totals[m] += r[m][1] * r["n"]
            count += r["n"]
        print("\nOverall Brier score (lower = more accurate): " +
              ", ".join(f"{m} {totals[m] / count:.5f}" for m in methods))
        return

    shapes = build_shapes(stats, games)
    SHAPES_PATH.write_text(json.dumps(shapes, separators=(",", ":")))
    print(f"wrote {SHAPES_PATH.name}: " +
          ", ".join(f"{k} {len(v)} games" for k, v in shapes.items() if k != "sd"))
    print("measured game-to-game swing (std dev):", shapes["sd"])


if __name__ == "__main__":
    main()
