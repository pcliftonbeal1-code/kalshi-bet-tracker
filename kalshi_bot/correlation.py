"""Learn how NFL stats move together, from real game logs (nflverse, free).

Why: a combo's legs aren't independent. If the Bills run well, Josh Allen
throws less. We measure that from seasons of games instead of guessing.

For every team in every game we record five numbers:
    margin   - points scored minus points allowed (a moneyline leg wins if > 0)
    points   - points scored (team totals)
    pass     - starting QB's passing yards (most pass attempts)
    qb_rush  - that QB's rushing yards
    rb_rush  - lead RB's rushing yards (most carries)

Then we measure how each pair moves together, for the SAME team ("same")
and for a team vs its OPPONENT ("opp"). We use rank correlation (robust to
blowout outliers) converted to the scale the simulator in combo.py needs.

    python -m kalshi_bot.correlation          # downloads ~35 MB, writes the table

The result is saved to kalshi_bot/nfl_correlations.json (small, committed),
so the bot never needs to download anything at game time.
"""

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import requests

VARS = ["margin", "points", "pass", "qb_rush", "rb_rush"]
TABLE_PATH = Path(__file__).resolve().parent / "nfl_correlations.json"
STATS_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
             "stats_player/stats_player_week_{season}.csv")
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"


def _num(value: str) -> float:
    return float(value) if value not in ("", "NA", None) else 0.0


def team_games(stats_rows: list[dict], games_rows: list[dict]) -> dict[tuple[str, str], dict]:
    """(game_id, team) -> {"opp": team, "margin": .., "points": .., "pass": .., ...}.

    Only games with a final score and both a starting QB and a lead RB are kept.
    """
    out: dict[tuple[str, str], dict] = {}
    for g in games_rows:
        if g.get("home_score") in ("", "NA", None):
            continue  # not played yet
        home, away = _num(g["home_score"]), _num(g["away_score"])
        out[(g["game_id"], g["home_team"])] = {
            "opp": g["away_team"], "margin": home - away, "points": home}
        out[(g["game_id"], g["away_team"])] = {
            "opp": g["home_team"], "margin": away - home, "points": away}

    qb: dict[tuple[str, str], dict] = {}
    rb: dict[tuple[str, str], dict] = {}
    for r in stats_rows:
        key = (r["game_id"], r["team"])
        if key not in out:
            continue
        if _num(r["attempts"]) > _num((qb.get(key) or {}).get("attempts", "0")):
            qb[key] = r
        if r["position"] == "RB" and _num(r["carries"]) > _num((rb.get(key) or {}).get("carries", "0")):
            rb[key] = r

    complete = {}
    for key, row in out.items():
        if key in qb and key in rb:
            complete[key] = {**row,
                             "pass": _num(qb[key]["passing_yards"]),
                             "qb_rush": _num(qb[key]["rushing_yards"]),
                             "rb_rush": _num(rb[key]["rushing_yards"])}
    return complete


def _ranks(values: list[float]) -> list[float]:
    """Rank values 1..n, giving ties their average rank."""
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0


def latent_correlation(x: list[float], y: list[float]) -> float:
    """Rank (Spearman) correlation, converted to the normal-curve correlation
    the simulator uses: r = 2 * sin(pi * rho / 6)."""
    rho = _pearson(_ranks(x), _ranks(y))
    return 2 * math.sin(math.pi * rho / 6)


def pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def build_table(games: dict[tuple[str, str], dict]) -> dict:
    """Correlation for every pair of variables, same team and vs opponent."""
    same_x, same_y = defaultdict(list), defaultdict(list)
    opp_x, opp_y = defaultdict(list), defaultdict(list)
    for (game_id, team), row in games.items():
        other = games.get((game_id, row["opp"]))
        for i, a in enumerate(VARS):
            for b in VARS[i + 1:]:
                same_x[pair_key(a, b)].append(row[a] if a < b else row[b])
                same_y[pair_key(a, b)].append(row[b] if a < b else row[a])
            if other:
                for b in VARS:
                    # Each game is seen from both teams, so both orientations land here.
                    k = pair_key(a, b)
                    first, second = (row[a], other[b]) if a <= b else (other[b], row[a])
                    opp_x[k].append(first)
                    opp_y[k].append(second)
    return {
        "same": {k: round(latent_correlation(same_x[k], same_y[k]), 3) for k in same_x},
        "opp": {k: round(latent_correlation(opp_x[k], opp_y[k]), 3) for k in opp_x},
        "team_games": len(games),
    }


def load_table(path: Path = TABLE_PATH) -> dict:
    return json.loads(path.read_text())


def load_nflverse(seasons: list[int]) -> tuple[list[dict], list[dict]]:
    """(weekly player stats, game scores + closing lines) for these seasons.
    Downloads once into data/nflverse/ (git-ignored), then reads from disk."""
    from kalshi_bot.alerts import DATA_DIR

    cache = DATA_DIR / "nflverse"
    cache.mkdir(parents=True, exist_ok=True)

    def get(name: str, url: str) -> list[dict]:
        path = cache / name
        if not path.exists():
            print(f"downloading {name}...")
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    stats = [r for s in seasons
             for r in get(f"stats_{s}.csv", STATS_URL.format(season=s))]
    wanted = {str(s) for s in seasons}
    games = [g for g in get("games.csv", GAMES_URL) if g["season"] in wanted]
    return stats, games


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the NFL correlation table.")
    parser.add_argument("--seasons", nargs="+", type=int, default=[2022, 2023, 2024, 2025])
    args = parser.parse_args()
    stats, games = load_nflverse(args.seasons)
    table = build_table(team_games(stats, games))
    table["seasons"] = args.seasons
    TABLE_PATH.write_text(json.dumps(table, indent=1, sort_keys=True))
    print(f"{table['team_games']} team-games -> {TABLE_PATH.name}")
    for side in ("same", "opp"):
        print(f"\n{side} team:")
        for k, v in sorted(table[side].items(), key=lambda kv: kv[1]):
            print(f"  {k:<18} {v:+.2f}")


if __name__ == "__main__":
    main()
