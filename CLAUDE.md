# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file automated trading bot (`main.py`) that trades a list of stock symbols (`symbols.csv`) through Alpaca, using Yahoo Finance (`yfinance`) hourly bars to generate buy signals. It's meant to run as three separate scheduled invocations per trading day (morning / night, plus an optional liquidate), likely via cron or a container scheduler — there is no long-running process or web server.

## Commands

Run from the project root with the venv active (`.venv` already exists):

```bash
python main.py morning     # 9:30am-12:30pm ET: place trailing stops on new positions, check take-profit sells
python main.py night       # ~3:57pm ET: scan all symbols in symbols.csv for buy signals, place buys
python main.py liquidate   # sell all currently held positions
python main.py orders      # print all open Alpaca orders (debugging)
python main.py test        # ad-hoc: fetch batch bars for all symbols, print AAPL's
python main.py data        # ad-hoc: fetch single-symbol bars for AAPL, print missing expected hourly timestamps
```

Install deps: `pip install -r requirements.txt`

Docker: `docker build -t finalgraphbot .` then `docker run --env-file .env finalgraphbot <morning|night|liquidate>` (image only bakes in `main.py` and `symbols.csv`, so rebuild after editing either).

There is no test suite, linter, or build step configured — `test`/`data` CLI modes are the only ad-hoc verification available. When changing signal logic, verify by hand with `python main.py test` or `python main.py data` since there's nothing else to check correctness.

## Configuration

Config lives in a `.env` file (not committed) loaded via `python-dotenv`:
- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` — required, raises if missing
- `ALPACA_PAPER` — defaults to `true` (paper trading); set `false` for live trading
- `STOP_LOSS_PCT` — trailing stop percent, default `1`
- `TAKE_PROFIT_PCT` — take-profit percent above entry price, default `2`
- `FAKE_BUY_SELL` — when `true`, `buy()`/`sell()`/`place_trailing_stop_order()` only print instead of submitting real orders. Useful for dry-run testing of the night/morning flows without touching the Alpaca account.

## Architecture

Everything is in `main.py`, organized around two daily entry points plus shared position/pricing helpers:

**`run_at_night()`** — the buy scan. Loads all symbols from `symbols.csv`, batch-fetches the last 7 hourly bars per symbol via `get_recent_hour_bars_batch` (yfinance, chunked with `batch_size`/`pause_seconds` to avoid throttling), computes normalized velocity/acceleration of price and volume (`compute_metrics_from_bars`), and buys a symbol if its (volume_velocity, volume_acceleration) point falls inside a hand-tuned polygon region (`check_buy` → `point_in_polygon`). Buys spend `equity / 3` per symbol (`buy()`), so at most ~3 concurrent positions are intended.

**`run_in_morning()`** — the position-management pass, meant to run every minute from open until 12:30pm ET. For each held Alpaca position it: ensures a trailing-stop sell order exists (`ensure_trailing_stops`, skips symbols that already have one), then sells if the current price has crossed `take_profit` (`check_all_sell` → `check_sell`). Take-profit is fixed at buy time from `avg_entry_price * (1 + take_profit_pct/100)` and doesn't get updated afterward.

**Signal math (`compute_metrics_from_bars`)**: takes 7 hourly bars, computes first/second differences of Close and Volume, applies a 5-period weighted moving average (`_wma`, most-recent-weighted) to smooth them, then normalizes by the latest close/volume so results are comparable percentages across symbols/prices. The buy decision only looks at the volume velocity/acceleration pair, tested against a polygon of points that was presumably fit/tuned externally (not derived in this file) — treat the polygon coordinates in `check_buy` as a black-box calibrated model, not something to "simplify."

**Trading-day/market-open gating**: both entry points call `check_trading_day()` and `market_is_open_now()` (both hit the Alpaca calendar/clock API) before doing anything, and no-op if it's not a live trading session.

**Pricing has two sources on purpose**: `yfinance` is used for the signal-generating hourly bars (`get_recent_hour_bars*`, delayed data, fine for the once-a-day night scan) and for the buy-time execution price (`get_latest_price`); Alpaca's own data API (`get_current_prices` / `StockLatestTradeRequest`) is used for the frequent morning take-profit checks where price freshness matters more. Don't conflate the two or swap one for the other without considering staleness.

Known gaps noted inline by the author (see bottom of `main.py`): market-closed handling in `run_in_morning` doesn't force an immediate liquidation, and there's no handling for exchange days with a non-standard (e.g. 12pm) open.

## Deployment (Google Cloud)

Runs as a Cloud Run Job (built from the `Dockerfile` in this repo) invoked by Cloud Scheduler, with one scheduled trigger per mode (`morning`, `night`, `liquidate` — likely `run_in_morning` on a minutely schedule 9:30–12:30am ET, `night` around 3:57pm ET). There's no `cloudbuild.yaml` or deploy script checked into the repo, so builds/deploys happen via manual `gcloud` commands (or the console) — check with the user before assuming a specific image name, region, or job name.

`ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (and the other `.env` vars) are pulled from Google Secret Manager at runtime rather than from a committed `.env` in production; the local `.env` is only for local runs.

Runtime logs land in Cloud Logging. The user may paste log output from failed scheduled runs directly into the conversation for debugging — treat pasted logs as authoritative evidence of what actually happened in the last scheduled run, not the local repo state.
