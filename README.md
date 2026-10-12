# Kalshi vs Sportsbook Alert Bot (read-only)

Compares Kalshi NFL prediction-market prices to sportsbook odds and alerts you
when Kalshi looks mispriced. **It never places trades.** It only reads data.

> Status: **Stages 1-5 are done** (math, Kalshi fetcher, sportsbook odds,
> matching, alert loop). Next: paper-trade for a few weekends and check the
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
| `kalshi_bot/config.py` | Reads `config.toml` |
| `config.toml` | Settings: alert threshold, fee rate, standard-deviation **estimates** |
| `tests/test_odds_math.py` | Unit tests with hand-checkable examples |
| `tests/test_kalshi_client.py` | Tests the Kalshi fetcher using fake responses (no internet needed) |
| `tests/test_odds_client.py` | Tests odds parsing, fair-value averaging, budget guard, key redaction |
| `tests/test_bot.py` | Tests alert rules and full bot cycles with fake APIs (credit use, cooldowns, no stale alerts) |
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
different number than the book (for example "250+ yards" against a 277.5 line),
we estimate using a bell curve whose width comes from `config.toml`. **Those
widths are guesses** that you should refine with real data.
