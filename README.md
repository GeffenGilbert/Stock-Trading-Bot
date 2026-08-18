# finalGraphBot

An automated equity trading bot that scans the S&P 500 for buy signals using a custom-built technical indicator, executes trades through the Alpaca brokerage API, and runs unattended in the cloud on a daily schedule.

I built this to go beyond backtesting a strategy and to test a strategy in real-time: come up with a signal, decide when to buy and sell, manage risk with stop-losses and take-profits, and deploy it so it runs on its own every day.

## What it does

Every trading day, the bot runs in three scheduled phases:

1. **Night — scan & buy.** Pulls hourly price/volume bars for ~500 symbols from Yahoo Finance, derives a custom momentum signal from each symbol's volume trend, and buys the symbols whose signal falls inside a hand-calibrated decision region. Position sizing is capped at ~3 concurrent holdings.
2. **Morning — manage risk.** Runs every minute from market open through 12:30pm ET. Attaches a trailing stop-loss to every new position and closes any position that has hit its take-profit target.
3. **Liquidate — exit.** An on-demand kill switch that closes every open position immediately.

The bot checks the market calendar and clock before acting, so it's a no-op on weekends, holidays, and outside trading hours.

## Tech stack

| Layer | Tools |
|---|---|
| Language | Python 3.11 |
| Brokerage / execution | [Alpaca Trading API](https://alpaca.markets/) (`alpaca-py`) — market orders, trailing stops, account & calendar data |
| Market data | `yfinance` (signal generation), Alpaca market data API (execution-time pricing) |
| Signal processing | `numpy` — weighted moving averages, normalized velocity/acceleration, polygon-based classification |
| Config / secrets | `python-dotenv` locally, Google Secret Manager in production |
| Packaging | Docker (`python:3.11-slim`) |
| Deployment | Google Cloud Run Jobs + Cloud Scheduler (3 independently scheduled jobs from one image) |

## How the signal works

Instead of using a standard indicator like RSI or MACD, I built my own signal from scratch based on how a symbol's hourly volume is moving:

1. Take the last 7 hourly bars for a symbol.
2. Compute first and second differences (velocity and acceleration) of both closing price and volume.
3. Smooth each series with a 5-period weighted moving average, weighted toward the most recent values.
4. Normalize by the latest close/volume so the metric is comparable across symbols regardless of price or liquidity.
5. Classify the resulting (volume velocity, volume acceleration) point using point-in-polygon geometry against a region calibrated from historical data (`polygons.py`).

This decouples signal generation (cheap, delayed Yahoo Finance data, run once a day) from execution pricing (Alpaca's live trade data, used only when money actually moves), which keeps the system fast without sacrificing execution accuracy.

## Architecture notes

- **Idempotent, stateless jobs** — each of the three modes is a fresh process invocation with no persistent state beyond what's held in the brokerage account itself, making it a natural fit for scheduled serverless jobs rather than a long-running service.
- **Dry-run mode** (`FAKE_BUY_SELL`) — every order-placing function can be flipped to print-only, so the full signal → decision → order pipeline can be validated against a live paper account without financial risk.
- **Separation of concerns** — market data, signal math, order execution, and scheduling gates are each isolated enough to unit-test independently (see `python main.py test` / `data` for ad-hoc verification tooling).

## Running it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Set up a `.env` with Alpaca paper-trading credentials:

```
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_PAPER=true
STOP_LOSS_PCT=1
TAKE_PROFIT_PCT=2
FAKE_BUY_SELL=true   # dry-run: print orders instead of placing them
```

```bash
python main.py night       # scan for buy signals
python main.py morning     # manage stops / take-profit
python main.py liquidate   # close all positions
python main.py orders      # inspect open orders
```

## Deployment

Ships as a single Docker image, deployed as three independently-scheduled [Google Cloud Run Jobs](https://cloud.google.com/run/docs/create-jobs) (morning, night, liquidate) triggered by Cloud Scheduler, with credentials pulled from Secret Manager at runtime — no long-running server or infrastructure to maintain.

```bash
docker build -t finalgraphbot .
docker run --env-file .env finalgraphbot night
```

## Disclaimer

This is a personal project for learning and experimentation with quantitative trading systems, not financial advice. It currently trades against a paper (simulated) Alpaca account.
