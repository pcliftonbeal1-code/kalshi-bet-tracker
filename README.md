# Kalshi vs Sportsbook Alert Bot (read-only)

Compares Kalshi NFL prediction-market prices to sportsbook odds and alerts you
when Kalshi looks mispriced. **It never places trades.** It only reads data.

> Status: **Stages 1-6 are done** (math, Kalshi fetcher, sportsbook odds,
> matching, alert loop, combo checker). Next: paper-trade for a few weekends and check the
> logged alerts against closing lines before betting real money.

## Setup

Requires Python 3.11 or newer (`python3 --version` to check).

```bash
# 1. Make a virtual environment: a private folder of libraries for this project
python3 -m venv .venv

# 2. Turn it on (do this every time you open a new terminal)
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install the libraries listed in requirements.txt
pip install -r requirements.txt

# 4. Create your secrets file (needed from Stage 2 onward)
cp .env.example .env             # then edit .env with your real keys
```

## Run the tests

```bash
pytest -v
```

## See live Kalshi prices (Stage 2)

No Kalshi account is needed: reading market prices is public.

```bash
# One time: list Kalshi's NFL series so you can confirm the tickers in config.toml
python -m kalshi_bot.kalshi_client --discover

# Print open NFL markets with their YES bid / ask
python -m kalshi_bot.kalshi_client
```

## See sportsbook fair odds (Stage 3)

Get a key at [the-odds-api.com](https://the-odds-api.com) and put it in `.env`
as `ODDS_API_KEY=...`.

```bash
# Moneylines for all upcoming games (about 1 credit)
python -m kalshi_bot.odds_client

# Also pull props + team totals for the NEXT game only (a few more credits)
python -m kalshi_bot.odds_client --props
```

**Credit budget:** the free plan (500 credits/month) is enough to build and
test, not to run all season. Props cost credits per game, per market. The bot
reads your remaining credits after every call and stops paid requests when you
fall below `min_credits_reserve` in `config.toml`.

## Match Kalshi to the books (Stage 4)

```bash
# One full read-only pass: prints every comparison, best edge first
python -m kalshi_bot.matching

# Include props/team totals for the next game, and explain anything unmatched
python -m kalshi_bot.matching --props --show-unmatched
```

`--show-unmatched` is your debugging tool. Every Kalshi market that couldn't
be paired with a sportsbook line is listed with the reason (unknown team code,
no book line for that player, can't find a threshold...).

## Run the alert bot (Stage 5)

```bash
python -m kalshi_bot.bot          # checks every 5 minutes until Ctrl+C
python -m kalshi_bot.bot --once   # one check, then exit
```

An alert looks like this (it beeps the terminal too):

```
EDGE +6.2% net: BUY NO Green Bay wins [Green Bay]
  Kalshi 50c vs fair 58.0% (gross +8.0%) | exact
  Don't pay over 53c. 155674 contracts for sale at <= 53c ($79,643.90 total)
  Book: Green Bay Packers moneyline [pinnacle] pinnacle: Dallas Cowboys -150 / ...
  Kickoff in 5h 12m | KXNFLGAME-26OCT18DALGB-GB
```

- **Net** means after Kalshi's fee. Alerts need at least `min_edge` (3%).
- **Don't pay over** is the highest price that still leaves 3% after fees.
  The bot checks the live order book and skips alerts with nothing for sale.
- Before any alert, the bot re-pulls that game's sportsbook odds if its copy is
  more than 5 minutes old. Out of credits means no fresh odds, so no alerts:
  it goes quiet instead of alerting on stale prices.
- The same market only alerts again if its edge grows by another 1%.
  "YES Dallas" and "NO Green Bay" are the same bet, so you get one alert (the cheaper).
- Every alert is saved to `data/alerts.csv` (opens in Excel/Sheets). That log
  is how we'll judge whether the edges are real.

**Phone alerts (optional):** install the free **ntfy** app, subscribe to a topic
name nobody could guess (it works like a password), and add `NTFY_TOPIC=that-name`
to `.env`.

**Credits:** with default settings (moneylines only, refreshed every 2 hours) the
bot uses about 12 credits a day plus 1 per confirmed alert. Running it on game
days only, the free 500/month plan is enough. Props (`props = true` in
`config.toml`) cost credits per game; turn them on once you have a paid plan.

## Check a combo (Stage 6)

Kalshi quotes each combo on the spot when you build it, so there's no list of
combo prices to scan. Build the combo in the Kalshi app, note its price, then:

```bash
# 1. List the game's legs: Kalshi price, fair price, and whether each qualifies
python -m kalshi_bot.combo SEADEN

# 2. Check your combo: leg numbers + y/n, and Kalshi's quote (0.09 = 9c)
python -m kalshi_bot.combo SEADEN --legs 7y 13n --quote 0.09
```

Example verdict (real data, Seahawks @ Broncos):

```
  [ok  ] #7y YES Denver over 24.5 points scored    Kalshi 28c fair 32.2% net +2.8
  [ok  ] #13n NO Bo Nix: 175+ passing yards        Kalshi 33c fair 34.6% net +0.1
Fair combo:   5.87% (legs independent would be 11.17%)
  FIGHT: #7y and #13n strongly work against each other (-0.42).
VERDICT: no bet
```

How it decides:
1. **Gate:** every leg must be cheaper on Kalshi than its fair price after fees.
   Most nights few or no legs qualify. No qualifying legs means no combo.
2. **Correlation:** it simulates 50,000 games where legs move together the way
   they did in 2,278 real NFL team-games (2022-2025). Rebuild that table with
   `python -m kalshi_bot.correlation`.
3. **Verdict:** VALUE only if the expected return after fees beats `min_roi`
   (10%). Max stake is `stake_pct` (0.5%) of your `bankroll` in `config.toml`.

Each check costs about 4 Odds API credits, then is free for 30 minutes (cached).
Every checked combo is logged to `data/combos.csv` for measuring CLV later.

## What each file does

| File | Purpose |
|---|---|
| `kalshi_bot/odds_math.py` | Pure math: odds to probability, vig removal, edge, fees, line adjustment |
| `kalshi_bot/kalshi_client.py` | Reads Kalshi market prices (GET requests only), with rate limiting and retries |
| `kalshi_bot/http_client.py` | Shared GET-only plumbing: rate limiting, retries, hiding secrets in errors |
| `kalshi_bot/odds_client.py` | Reads sportsbook odds from The Odds API and tracks your credit budget |
| `kalshi_bot/fair_value.py` | Turns book prices into fair probabilities (Pinnacle, else average of US books) |
| `kalshi_bot/teams.py` | Kalshi team codes (KC, JAC) to sportsbook names (Kansas City Chiefs) |
| `kalshi_bot/matching.py` | Pairs each Kalshi market with its book line; computes fair %, edge, net edge |
| `kalshi_bot/alerts.py` | Which edges deserve an alert; the CSV log; phone push |
| `kalshi_bot/bot.py` | The 5-minute loop: cached odds, confirm-before-alert, order book check |
| `kalshi_bot/distributions.py` | Line adjustment from real game logs, plus its backtest |
| `kalshi_bot/nfl_distributions.json` | The measured stat shapes and swings (2022-2025) |
| `kalshi_bot/combo.py` | Combo checker: leg gate, correlated simulation, verdict, stake |
| `kalshi_bot/correlation.py` | Learns how stats move together from nflverse game logs |
| `kalshi_bot/nfl_correlations.json` | The learned correlation table (2022-2025 seasons) |
| `kalshi_bot/config.py` | Reads `config.toml` |
| `config.toml` | Settings: alert threshold, fee rate, bankroll, measured stat swings |
| `tests/test_odds_math.py` | Unit tests with hand-checkable examples |
| `tests/test_kalshi_client.py` | Tests the Kalshi fetcher using fake responses (no internet needed) |
| `tests/test_odds_client.py` | Tests odds parsing, fair-value averaging, budget guard, key redaction |
| `tests/test_bot.py` | Tests alert rules and full bot cycles with fake APIs (credit use, cooldowns, no stale alerts) |
| `tests/test_distributions.py` | Tests the real-data shapes agree with the book and handle lopsided stats |
| `tests/test_combo.py` | Tests ticker reading, simulation math, the gate, and correlation learning |
| `tests/test_matching.py` | Tests game/player/threshold matching and exact vs line-adjusted fair values |
| `.env.example` | Template for your API keys. Copy it to `.env`, which git ignores |
| `.gitignore` | Tells git never to commit `.env`, logs, or clutter |
| `requirements.txt` | Libraries to install |
| `pyproject.toml` | Tells pytest where to find the code |

## The math, in one paragraph

A sportsbook's odds imply a probability (`-150` means 60%). Both sides add up
to more than 100% because the book takes a cut (the "vig"). Dividing each side
by the total gives a **fair** probability. If Kalshi sells YES for less than
that fair probability, the difference is the **edge**. When Kalshi asks about a
different number than the book (for example "70+ rushing yards" against a 59.5
line), we estimate the gap from **real game logs** (see below), and refuse to
estimate when the gap is too big or the line too small to trust.

## How accurate is the line adjustment?

`python -m kalshi_bot.distributions --backtest` fits on 2022-2024 and predicts
the 2025 season, which the model never saw. It compares four methods: the old
guessed bell curve, a bell curve with measured widths, the real-data shape, and
the blend of those last two, which the bot uses. Highlights:

| Case | What happened | Old guess | Bot now |
|---|---|---|---|
| QB rushes 20+ yds over his typical game | 14.1% | 24.8% | 14.4% |
| QB rushes 20+ yds under his typical game (still clears it) | 83.2% | 74.4% | 85.9% |
| Team scores 10+ under its implied total (still clears it) | 87.0% | 83.6% | 87.4% |
| RB rushes 30 under his typical game (still clears it) | 84.6% | 83.8% | 82.3% |

Overall accuracy (Brier score, lower is better): old guess 0.1809, bot now
0.1795. The biggest fix is QB rushing: the old ±30-yard guess was nearly double
the real swing (±17). **Known weakness:** for RB rushing and passing far ABOVE the
line, the bot runs about 2 points high (e.g. 20.0% vs 17.7% at +30 yards), which
is one reason estimates further than half a typical swing from the line are skipped.

Rebuild after each season with `python -m kalshi_bot.distributions` and
`python -m kalshi_bot.correlation`.
