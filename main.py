import yfinance as yf
import datetime
import csv
import os
import math
import time
from zoneinfo import ZoneInfo

import numpy as np
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetCalendarRequest, GetOrdersRequest, TrailingStopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from polygons import polygons

load_dotenv()

def get_strategy_config():
    return {
        "stop_loss_pct": float(os.getenv("STOP_LOSS_PCT", "1")),
        "take_profit_pct": float(os.getenv("TAKE_PROFIT_PCT", "2")),
        "fake_buy_sell": os.getenv("FAKE_BUY_SELL", "false").lower() == "true",
    }

def get_alpaca_client():
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    if not api_key or not secret_key:
        raise ValueError(
            "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
        )

    return TradingClient(api_key, secret_key, paper=paper)

def check_trading_day(target_date=None):
    """
    Returns True if the provided date is a US market trading day according to Alpaca.
    target_date can be None (today in ET), datetime.date, datetime.datetime, or YYYY-MM-DD string.
    """
    market_tz = ZoneInfo("America/New_York")

    if target_date is None:
        target_date = datetime.datetime.now(market_tz).date()
    elif isinstance(target_date, str):
        target_date = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
    elif isinstance(target_date, datetime.datetime):
        target_date = target_date.date()
    elif not isinstance(target_date, datetime.date):
        raise TypeError("target_date must be None, date, datetime, or YYYY-MM-DD string")

    client = get_alpaca_client()
    calendar_request = GetCalendarRequest(start=target_date, end=target_date)
    trading_days = client.get_calendar(filters=calendar_request)

    return len(trading_days) > 0

def market_is_open_now():
    """
    Returns True if the US market is open right now according to Alpaca clock.
    """
    client = get_alpaca_client()
    clock = client.get_clock()
    return bool(clock.is_open)

def get_latest_price(symbol: str) -> float: # vetted
    ticker = yf.Ticker(symbol)
    fast_info = getattr(ticker, "fast_info", {}) or {}

    # yfinance key names may vary by version
    price = fast_info.get("lastPrice") or fast_info.get("last_price")
    if price is None or price <= 0:
        recent = ticker.history(period="1d", interval="1m")
        if recent.empty:
            raise ValueError(f"Unable to fetch latest price for {symbol}")
        price = float(recent["Close"].iloc[-1])

    return float(price)

def get_symbols(csv_path="symbols.csv"): # vetted
    symbols = []
    with open(csv_path, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            symbol = row.get("Symbol", "").strip()
            if symbol:
                symbols.append(symbol)
    return symbols

def get_positions():
    """
    Returns currently held Alpaca positions as:
    [{"symbol": "AAPL", "original_price": 192.34}, ...]

    original_price is taken from Alpaca's avg_entry_price.
    """
    client = get_alpaca_client()
    positions = client.get_all_positions()

    config = get_strategy_config()
    take_profit_pct = config["take_profit_pct"]

    held_positions = []
    for position in positions:
        original_price = float(position.avg_entry_price)
        held_positions.append(
            {
                "symbol": position.symbol,
                "qty": float(position.qty),
                "original_price": original_price,
                "take_profit": original_price * (1 + take_profit_pct / 100),
                "current_price": original_price,
            }
        )

    return held_positions

def get_open_sell_order_symbols():
    client = get_alpaca_client()
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    open_orders = client.get_orders(filter=request)

    symbols = set()
    for order in open_orders:
        side = str(getattr(order, "side", "")).lower()
        order_type = str(getattr(order, "type", "")).lower()
        if "sell" not in side:
            continue
        if order_type == "trailing_stop" or "trailing" in order_type:
            continue
        symbol = getattr(order, "symbol", None)
        if symbol:
            symbols.add(symbol)
    return symbols

def get_open_trailing_stop_symbols():
    client = get_alpaca_client()
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    open_orders = client.get_orders(filter=request)

    symbols = set()
    for order in open_orders:
        side = str(getattr(order, "side", "")).lower()
        order_type = str(getattr(order, "type", "")).lower()
        if "sell" not in side:
            continue
        if order_type != "trailing_stop" and "trailing" not in order_type:
            continue
        symbol = getattr(order, "symbol", None)
        if symbol:
            symbols.add(symbol)
    return symbols

def place_trailing_stop_order(symbol, qty):
    config = get_strategy_config()
    fake_buy_sell = config["fake_buy_sell"]
    stop_loss_pct = config["stop_loss_pct"]

    if fake_buy_sell:
        print(f"Place trailing stop: {symbol}, qty={qty}")
        return

    client = get_alpaca_client()
    trailing_stop = TrailingStopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        trail_percent=stop_loss_pct,
    )
    client.submit_order(order_data=trailing_stop)
    print(f"Placed trailing stop for {symbol}: qty={qty}, trail={stop_loss_pct}%")

def ensure_trailing_stops(positions):
    open_trailing_stop_symbols = get_open_trailing_stop_symbols()

    for position in positions:
        symbol = position.get("symbol")
        qty = position.get("qty")

        if not symbol or qty is None or qty <= 0:
            continue

        if symbol in open_trailing_stop_symbols:
            print(f"Skipping {symbol}: open sell order already exists")
            continue

        place_trailing_stop_order(symbol, qty)

def get_current_prices(positions=None): # vetted
    """
    Returns a dict of latest prices keyed by symbol.

    If positions is provided, symbols are read from positions[i]["symbol"].
    If positions is None, currently held Alpaca positions are fetched first.
    """
    if positions is None:
        positions = get_positions()

    symbols = [p["symbol"] for p in positions if p.get("symbol")]
    if not symbols:
        return {}

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise ValueError(
            "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
        )

    data_client = StockHistoricalDataClient(api_key, secret_key)
    request = StockLatestTradeRequest(symbol_or_symbols=symbols)
    latest_trades = data_client.get_stock_latest_trade(request)

    prices = {}
    for symbol in symbols:
        trade = latest_trades.get(symbol)
        if trade is not None and getattr(trade, "price", None) is not None:
            prices[symbol] = float(trade.price)

    return prices

def get_recent_hour_bars(symbol: str): # vetted
    """
    Fetch the 5 most recent hourly bars for a given symbol using Yahoo Finance.
    Note: Yahoo Finance data is delayed and not suitable for real-time trading.
    """
    numberOfBars = 7 # including the current one
    market_tz = ZoneInfo("America/New_York")
    now = datetime.datetime.now(market_tz)
    start = now - datetime.timedelta(hours=numberOfBars)
    end = now + datetime.timedelta(hours=1)
	# Pass datetime objects directly to avoid string parsing issues.
    df = yf.download(
		tickers=symbol,
		interval="60m",
		start=start,
		end=end,
		progress=False
	)
	# Get the last 5 bars (including the current hour, even if incomplete)
    return df.tail(numberOfBars)

def get_recent_hour_bars_batch(symbols, number_of_bars=7, batch_size=100, pause_seconds=0.35): # vetted
    """
    Fetch recent hourly bars for many symbols in batches to reduce request count.
    Returns: dict[symbol] -> DataFrame with that symbol's bars.
    """
    market_tz = ZoneInfo("America/New_York")
    now = datetime.datetime.now(market_tz)
    # now = datetime.datetime(2026, 3, 24, 16, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    start = now - datetime.timedelta(hours=number_of_bars)
    end = now + datetime.timedelta(hours=1)
    bars_by_symbol = {}

    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i:i + batch_size]
        tickers_arg = " ".join(chunk)

        df = yf.download(
            tickers=tickers_arg,
            interval="60m",
            start=start,
            end=end,
            progress=False,
            group_by="ticker",
            threads=False,
        )

        if df is None or df.empty:
            time.sleep(pause_seconds)
            continue

        # Multi-symbol response: top-level columns are symbols.
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            for symbol in chunk:
                if symbol in df.columns.get_level_values(0):
                    symbol_bars = df[symbol].dropna(how="all")
                    if not symbol_bars.empty:
                        bars_by_symbol[symbol] = symbol_bars.tail(number_of_bars)
        else:
            # Single symbol response shape fallback.
            symbol = chunk[0]
            symbol_bars = df.dropna(how="all")
            if not symbol_bars.empty:
                bars_by_symbol[symbol] = symbol_bars.tail(number_of_bars)

        # Short pause between batches lowers the chance of temporary throttling.
        time.sleep(pause_seconds)

    return bars_by_symbol

def _wma(values, window=5): # vetted
    if len(values) < window:
        return np.nan
    tail = values[-window:]
    weights = np.arange(1, window + 1, dtype=float)
    return np.dot(tail, weights) / weights.sum()

def compute_metrics_from_bars(bars): # vetted
    """
    bars: DataFrame with columns ["Close", "Volume"]
          ordered oldest → newest (most recent last row)

    returns: velocity, acceleration, volume_velocity, volume_acceleration
    """

    closes = bars["Close"].values
    volumes = bars["Volume"].astype(float).values

    # raw differences
    bar_vel_close = np.diff(closes)
    bar_acc_close = np.diff(bar_vel_close)

    bar_vel_vol = np.diff(volumes)
    bar_acc_vol = np.diff(bar_vel_vol)

    # remove NaNs not needed since np.diff handles it, but keep consistent logic
    vel = _wma(bar_vel_close, 5)
    acc = _wma(bar_acc_close, 5)
    vol_vel = _wma(bar_vel_vol, 5)
    vol_acc = _wma(bar_acc_vol, 5)

    # normalization
    today_close = closes[-1]
    today_volume = volumes[-1]

    if today_close != 0: # should I add some sort of error?
        vel = 100 * vel / today_close
        acc = 100 * acc / today_close

    if today_volume != 0:
        vol_vel = 100 * vol_vel / today_volume
        vol_acc = 100 * vol_acc / today_volume

    return vel, acc, vol_vel, vol_acc

def point_in_single_polygon(px, py, polygon): # vetted-ish
    inside = False
    n = len(polygon)
    for i in range(n):
        x1 = polygon[i]['x']
        y1 = polygon[i]['y']
        x2 = polygon[(i + 1) % n]['x']
        y2 = polygon[(i + 1) % n]['y']
        intersects = ((y1 > py) != (y2 > py)) and (
            px < (x2 - x1) * (py - y1) / (y2 - y1) + x1
        )
        if intersects:
            inside = not inside
    return inside

def point_in_polygons(px, py, polygons):
    """
    True if (px, py) falls inside any polygon in polygons — mirrors
    pointInPolygon/pointInSinglePolygon in frontend/sketch.js.
    """
    return any(point_in_single_polygon(px, py, polygon) for polygon in polygons)

def buy(symbol):
    config = get_strategy_config()
    fake_buy_sell = config["fake_buy_sell"]

    if fake_buy_sell:
        print("Buy:", symbol)
        return

    client = get_alpaca_client()
    account = client.get_account()
    available_cash = float(account.cash)
    total_equity = float(account.equity)
    desired_spend = total_equity / 3

    if desired_spend > available_cash:
        print(f"Wanted to buy {symbol} but out of cash")
        return

    if desired_spend <= 0:
        print(f"No equity available to buy {symbol}.")
        return

    last_price = get_latest_price(symbol)
    qty = math.floor(desired_spend / last_price)

    if qty < 1:
        print(
            f"Not enough cash to buy 1 share of {symbol}. "
            f"Desired spend=${desired_spend:.2f}, price=${last_price:.2f}"
        )
        return

    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )
    client.submit_order(order_data=order)
    print(f"Placed buy for {symbol}: qty={qty}, cash=${desired_spend:.2f}, price=${last_price:.2f}")

def check_buy(symbol, bars): # vetted-ish
    vel, acc, vol_vel, vol_acc = compute_metrics_from_bars(bars)

    good_values = point_in_polygons(vol_vel, vol_acc, polygons)

    print(
        f"{symbol}: vol_vel={vol_vel:.4f}, "
        f"vol_acc={vol_acc:.4f}, buy_signal={good_values}"
    )

    if good_values:
        print("Volume Velocity:", vol_vel)
        print("Volume Acceleration:", vol_acc)
        buy(symbol)

def check_all_buy(symbols): # vetted-ish
    print(f"Starting check_all_buy with {len(symbols)} symbols")

    bars_by_symbol = get_recent_hour_bars_batch(symbols)

    print(f"Bars returned for {len(bars_by_symbol)} symbols")

    total = len(symbols)
    too_few_bars = 0
    checked = 0

    for symbol in symbols:
        bars = bars_by_symbol.get(symbol)
        if bars is None or len(bars) < 7:
            times = [
                (dt.replace(tzinfo=ZoneInfo("UTC")) if dt.tzinfo is None else dt)
                .astimezone(ZoneInfo("America/New_York"))
                .strftime("%I:%M").lstrip("0")
                for dt in bars.index
            ]
            expected_times = ['9:30', '10:30', '11:30', '12:30', '1:30', '2:30', '3:30']
            missing_times = [t for t in expected_times if t not in times]
            print(f"Skipping {symbol}: fewer than 7 recent bars returned: {missing_times}")
            too_few_bars += 1
            continue

        check_buy(symbol, bars)
        checked += 1
    
    print(
        f"Night summary: total={total}, "
        f"too_few_bars={too_few_bars}, checked={checked}, "
    )

# stock market closes at 4pm EST, I would like this to finish running as close to that time as possible, so run 3:57pm EST
def run_at_night():
    market_tz = ZoneInfo("America/New_York")
    print(f"Running at night, time: {datetime.datetime.now(market_tz)}")

    if not check_trading_day(): 
        print(f"{datetime.datetime.now(market_tz)} is not a trading day.")
        return
    if not market_is_open_now(): 
        print(f"{datetime.datetime.now(market_tz)} market is currently closed.")
        return

    symbols = get_symbols()
    print(f"Loaded {len(symbols)} symbols")
    print(f"First 10 symbols: {symbols[:10]}")
    check_all_buy(symbols)

def cancel_open_orders_for_symbol(client, symbol):
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
    open_orders = client.get_orders(filter=request)
    for order in open_orders:
        client.cancel_order_by_id(order.id)
        print(f"Cancelled open order {order.id} for {symbol}")

def sell(symbol):
    config = get_strategy_config()
    fake_buy_sell = config["fake_buy_sell"]

    if fake_buy_sell:
        print("Sell:", symbol)
        return

    client = get_alpaca_client()
    # close_position 403s if an open order (e.g. our trailing stop) already exists for the symbol.
    cancel_open_orders_for_symbol(client, symbol)
    client.close_position(symbol)
    print(f"Placed sell for {symbol} (close position)")

def check_sell(position): # vetted
    current_price = position.get("current_price")
    take_profit = position.get("take_profit")

    if current_price is None or take_profit is None:
        return

    symbol = position.get("symbol")

    if current_price > take_profit:
        sell(symbol)
        return

    return

def check_all_sell(positions): # vetted
    current_prices = get_current_prices(positions)
    open_sell_symbols = get_open_sell_order_symbols()

    for position in positions:
        symbol = position.get("symbol")
        if not symbol:
            continue

        if symbol in open_sell_symbols:
            print(f"Skipping {symbol}: open sell order already exists")
            continue

        latest_price = current_prices.get(symbol)
        if latest_price is None:
            print(f"Skipping {symbol}: no current price returned")
            continue

        position["current_price"] = latest_price
        check_sell(position)

def sell_all_positions():
    remaining_positions = get_positions()
    if not remaining_positions:
        print("No remaining positions at 12:30 PM ET.")
        return

    sold_symbols = set()
    for position in remaining_positions:
        symbol = position.get("symbol")
        if not symbol or symbol in sold_symbols:
            continue
        sell(symbol)
        sold_symbols.add(symbol)

def run_in_morning(): # should run every minute from 9:30am-12:30pm EST
    market_tz = ZoneInfo("America/New_York")
    print(f"Running in the morning, time: {datetime.datetime.now(market_tz)}")

    if not check_trading_day(): 
        print(f"{datetime.datetime.now(market_tz)} is not a trading day.")
        return
    if not market_is_open_now(): 
        print(f"{datetime.datetime.now(market_tz)} market is currently closed.")
        return

    positions = get_positions()
    if not positions:
        print("No open positions to monitor.")
        return

    ensure_trailing_stops(positions)
    check_all_sell(positions)

def print_open_orders():
    client = get_alpaca_client()
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    open_orders = client.get_orders(filter=request)

    for order in open_orders:
        print(
            f"symbol={order.symbol}, "
            f"side={order.side}, "
            f"type={order.type}, "
            f"qty={order.qty}, "
            f"trail_percent={getattr(order, 'trail_percent', None)}, "
            f"trail_price={getattr(order, 'trail_price', None)}, "
            f"hwm={getattr(order, 'hwm', None)}, "
            f"stop_price={getattr(order, 'stop_price', None)}, "
            f"status={order.status}"
        )

import sys
def main():
    if len(sys.argv) < 2:
        raise ValueError("Usage: python main.py [morning|liquidate|night]")

    mode = sys.argv[1].lower()

    run_start_time = time.time()

    if mode == "morning":
        run_in_morning()
    elif mode == "liquidate":
        sell_all_positions()
    elif mode == "night":
        run_at_night()
    elif mode == "test":
        symbols = get_symbols()
        bars_by_symbol = get_recent_hour_bars_batch(symbols)
        bars = bars_by_symbol.get("AAPL")
        print(bars)
        # for symbol in symbols:
        #     if (symbol == "AAPL"):
        #         bars = bars_by_symbol.get(symbol)
        #         print(bars)
        #     bars = bars_by_symbol.get(symbol)
        #     if bars is None or len(bars) < 7:
        #         print(f"Skipping {symbol}: fewer than 7 recent bars returned")
        #         continue # talk to newt about if this is ok (error handling in general)
    elif mode == "data":
        # symbols = get_symbols()
        bars = get_recent_hour_bars("AAPL")
        times = [
            (dt.replace(tzinfo=ZoneInfo("UTC")) if dt.tzinfo is None else dt)
            .astimezone(ZoneInfo("America/New_York"))
            .strftime("%I:%M").lstrip("0")
            for dt in bars.index
        ]
        expected_times = ['9:30', '10:30', '11:30', '12:30', '1:30', '2:30', '3:30']
        missing_times = [t for t in expected_times if t not in times]
        print(missing_times)
    elif mode == "orders":
        print_open_orders()
    else:
        raise ValueError(f"Unknown mode: {mode}")
    

    elapsed_seconds = time.time() - run_start_time
    print(f"Total run time: {elapsed_seconds:.2f} seconds. Version 8.")

if __name__ == "__main__":
    main()

# /Users/geffengilbert/codingProjects/stockStuff/finalGraphBot/.venv/bin/python main.py

# To do: 
# In the beginning of run_in_morning if the market is not open then sell immediately
# If the next day is one of the few days that open at 12pm then just dont buy today and run it tmrw

# Need to update