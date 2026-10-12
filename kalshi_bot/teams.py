"""NFL team codes (as Kalshi writes them) -> full names (as The Odds API writes them).

Kalshi tickers use short codes ("KC", "JAC"); The Odds API uses full names
("Kansas City Chiefs"). This table translates between them. Extra spellings
(aliases) are included where sites commonly differ, e.g. JAX vs JAC.
If a matchup fails to match, check this table first.
"""

TEAM_CODES = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAC": "Jacksonville Jaguars",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LV": "Las Vegas Raiders",
    "LAC": "Los Angeles Chargers",
    "LA": "Los Angeles Rams",
    "LAR": "Los Angeles Rams",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers",
    "SEA": "Seattle Seahawks",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
    "WSH": "Washington Commanders",
}


def split_matchup(codes: str) -> tuple[str, str] | None:
    """Split a run-together code like "KCBUF" into ("KC", "BUF").

    Codes are 2 or 3 letters, so "KCBUF" could be KC+BUF or KCB+UF. We try
    every split and keep the one where BOTH halves are real team codes.
    Returns None if no split works.
    """
    for i in (2, 3):
        away, home = codes[:i], codes[i:]
        if away in TEAM_CODES and home in TEAM_CODES:
            return away, home
    return None
