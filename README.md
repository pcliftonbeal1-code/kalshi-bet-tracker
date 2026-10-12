# Kalshi vs Sportsbook Alert Bot (read-only)

Compares Kalshi NFL prediction-market prices to sportsbook odds and alerts you
when Kalshi looks mispriced. **It never places trades.** It only reads data.

> Status: built in stages. **Stage 1 (math) and Stage 2 (Kalshi fetcher) are
> done.** Odds fetcher, matching, and alerts come next.

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

## What each file does

| File | Purpose |
|---|---|
| `kalshi_bot/odds_math.py` | Pure math: odds to probability, vig removal, edge, fees, line adjustment |
| `kalshi_bot/kalshi_client.py` | Reads Kalshi market prices (GET requests only), with rate limiting and retries |
| `kalshi_bot/config.py` | Reads `config.toml` |
| `config.toml` | Settings: alert threshold, fee rate, standard-deviation **estimates** |
| `tests/test_odds_math.py` | Unit tests with hand-checkable examples |
| `tests/test_kalshi_client.py` | Tests the Kalshi fetcher using fake responses (no internet needed) |
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
