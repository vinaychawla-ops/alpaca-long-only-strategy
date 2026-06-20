import os
import sys
import json
import math
import subprocess
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime, time as dtime

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopOrderRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce, OrderStatus
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
import yfinance as yf
import pandas as pd

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
RULES_PATH = BASE_DIR / "rules.json"
STATE_DIR = BASE_DIR / "state"
POSITIONS_PATH = STATE_DIR / "positions.json"
WATCHLIST_PATH = BASE_DIR / "watchlist.txt"
TRADES_CSV = BASE_DIR / "trades" / "trades.csv"
LOGS_DIR = BASE_DIR / "logs"
CYCLE_LOG = LOGS_DIR / "safety-check-log.json"
ERROR_LOG = LOGS_DIR / "cycle_errors.log"

MAX_TRADES_PER_DAY = 3
MAX_TRADE_SIZE_USD = 2000
MAX_RISK_PER_TRADE_PCT = 1.0
PORTFOLIO_VALUE_USD = 100000

ET = ZoneInfo("America/New_York")


def _log_cycle(entry: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    clean = {}
    for k, v in entry.items():
        if hasattr(v, "item"):
            v = v.item()
        elif isinstance(v, bool):
            v = bool(v)
        clean[k] = v
    with open(CYCLE_LOG, "a") as f:
        f.write(json.dumps(clean) + "\n")


def _now_et() -> datetime:
    return datetime.now(ET)


def _today_et_str() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _et_tag() -> str:
    return _now_et().strftime("[%H:%M:%S ET]")


def _to_yahoo(t: str) -> str:
    return t.replace(" ", "-")


def _load_env():
    load_dotenv(ENV_PATH)
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_API_SECRET")
    base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if "paper-api" not in base:
        print(f"{_et_tag()} ABORT: expected paper URL but got {base}", file=sys.stderr)
        sys.exit(1)
    return key, secret, base


def _load_rules():
    with open(RULES_PATH) as f:
        return json.load(f)


def _parse_time(s: str) -> dtime:
    parts = s.strip().split(":")
    return dtime(int(parts[0]), int(parts[1]))


def _read_watchlist() -> list:
    if not WATCHLIST_PATH.is_file():
        return []
    tickers = []
    with open(WATCHLIST_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ticker = line.split()[0]
            if ticker:
                tickers.append(ticker)
    return tickers


def _count_today_buys() -> int:
    if not TRADES_CSV.is_file():
        return 0
    today_str = _today_et_str()
    count = 0
    with open(TRADES_CSV, newline="") as f:
        import csv
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("timestamp_iso", "")
            side = row.get("side", "")
            if ts.startswith(today_str) and side.upper() == "BUY":
                count += 1
    return count


def _load_state() -> list:
    if not POSITIONS_PATH.is_file():
        return []
    with open(POSITIONS_PATH) as f:
        return json.load(f)


def _save_state(positions: list):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = POSITIONS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(positions, f, indent=2)
    os.replace(tmp, POSITIONS_PATH)


def _place_stop(trading: TradingClient, symbol: str, qty: int, stop_price: float) -> str:
    req = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        type=OrderType.STOP,
        stop_price=round(stop_price, 2),
        time_in_force=TimeInForce.GTC,
    )
    order = trading.submit_order(req)
    return str(order.id)


def _cancel_stop(trading: TradingClient, order_id: str):
    try:
        trading.cancel_order_by_id(order_id)
    except Exception:
        pass


def _current_price(data: StockHistoricalDataClient, symbol: str) -> float:
    req = StockLatestTradeRequest(symbol_or_symbols=symbol)
    trade = data.get_stock_latest_trade(req)
    return float(trade[symbol.upper()].price)


def _low_of_day(ticker: str) -> float:
    yahoo = _to_yahoo(ticker)
    bars = yf.download(yahoo, period="1d", interval="5m", progress=False, auto_adjust=True)
    if bars is None or bars.empty:
        return None
    if isinstance(bars.columns, pd.MultiIndex):
        return float(bars["Low"][yahoo].min())
    return float(bars["Low"].min())


def _swing_lows(ticker: str) -> list:
    yahoo = _to_yahoo(ticker)
    bars = yf.download(yahoo, period="1d", interval="5m", progress=False, auto_adjust=True)
    if bars is None or bars.empty:
        return []
    if isinstance(bars.columns, pd.MultiIndex):
        lows = bars["Low"][yahoo]
    else:
        lows = bars["Low"]
    swings = []
    n = len(lows)
    for i in range(2, n - 2):
        if lows.iloc[i] < lows.iloc[i - 2] and lows.iloc[i] < lows.iloc[i + 2]:
            swings.append(float(lows.iloc[i]))
    return swings


# ---------------------------------------------------------------------------
# Filters from rules.json (D1-D3 daily, I1-I3 intraday)
# ---------------------------------------------------------------------------

def _check_daily_filters(ticker: str, rules: dict) -> tuple:
    yahoo = _to_yahoo(ticker)
    bars = yf.download(yahoo, period="5d", interval="1d", progress=False, auto_adjust=True)
    if bars is None or len(bars) < 2:
        return False, "no_data"
    if isinstance(bars.columns, pd.MultiIndex):
        closes = bars["Close"][yahoo]
        highs = bars["High"][yahoo]
    else:
        closes = bars["Close"]
        highs = bars["High"]

    df = rules.get("daily_filters", {})
    yesterday_close = float(closes.iloc[-2])
    today_high = float(highs.iloc[-1])
    prior_day_high = float(highs.iloc[-2])

    if df.get("D1_above_prior_day_high", False):
        if today_high <= prior_day_high:
            data = {"pass": False, "reason": "D1: today high not above prior day high", "price": 0.0}
            _log_cycle(data)
            return False, "D1"

    if df.get("D2_prior_close_above_sma200", False):
        if len(bars) < 200:
            pass
        else:
            sma200 = closes.rolling(200).mean().iloc[-2]
            if float(sma200) is not None and yesterday_close <= float(sma200):
                data = {"pass": False, "reason": "D2: prior close not above SMA200", "price": 0.0}
                _log_cycle(data)
                return False, "D2"

    return True, "ok"


def _check_intraday_filters(ticker: str, rules: dict) -> tuple:
    yahoo = _to_yahoo(ticker)
    bars = yf.download(yahoo, period="1d", interval="5m", progress=False, auto_adjust=True)
    if bars is None or len(bars) < 3:
        return False, "no_intraday_data"

    if isinstance(bars.columns, pd.MultiIndex):
        opens = bars["Open"][yahoo]
        highs = bars["High"][yahoo]
        lows = bars["Low"][yahoo]
        closes = bars["Close"][yahoo]
        volumes = bars["Volume"][yahoo]
    else:
        opens = bars["Open"]
        highs = bars["High"]
        lows = bars["Low"]
        closes = bars["Close"]
        volumes = bars["Volume"]

    ifilter = rules.get("intraday_filters", {})
    current_price = float(closes.iloc[-1])

    if ifilter.get("I1_above_premarket_high", False):
        premarket_high = float(highs.iloc[0])
        if current_price <= premarket_high:
            data = {"pass": False, "reason": "I1: not above premarket high", "price": current_price}
            _log_cycle(data)
            return False, "I1"

    if ifilter.get("I2_above_today_hod", False):
        hod = float(highs.max())
        if current_price < hod:
            data = {"pass": False, "reason": "I2: not above today HOD", "price": current_price}
            _log_cycle(data)
            return False, "I2"

    rvol_min = ifilter.get("I3_rvol_min", None)
    rvol_lookback = ifilter.get("I3_rvol_lookback_days", 14)
    if rvol_min is not None:
        daily = yf.download(yahoo, period=f"{rvol_lookback + 2}d", interval="1d", progress=False, auto_adjust=True)
        if daily is not None and len(daily) >= rvol_lookback + 1:
            if isinstance(daily.columns, pd.MultiIndex):
                daily_vol = daily["Volume"][yahoo]
            else:
                daily_vol = daily["Volume"]
            avg_vol = daily_vol.iloc[-(rvol_lookback + 1):-1].mean()
            today_vol = float(volumes.sum())
            if avg_vol > 0 and (today_vol / avg_vol) < rvol_min:
                return False, "I3_rvol"

    return True, "ok"


# ---------------------------------------------------------------------------
# Time gate
# ---------------------------------------------------------------------------

def time_gate(rules: dict) -> str:
    now = _now_et()
    if now.weekday() >= 5:
        return "weekend"
    t = now.time()
    tf = rules.get("time_filter", {})
    earliest = _parse_time(tf.get("earliest_entry_et", "10:05"))
    latest = _parse_time(tf.get("latest_entry_et", "15:30"))
    force_close = _parse_time(tf.get("force_close_et", "15:51"))
    day_start = dtime(10, 0)
    day_end = dtime(16, 0)

    if t < day_start or t >= day_end:
        return "closed"

    if day_start <= t < earliest:
        return "manage_only"
    if latest <= t < force_close:
        return "manage_only"
    if force_close <= t < day_end:
        return "force_close"

    return "ok"


# ---------------------------------------------------------------------------
# Step 3: check stop-outs
# ---------------------------------------------------------------------------

def check_stop_outs(positions: list, trading: TradingClient) -> list:
    survivors = []
    for pos in positions:
        stop_id = pos.get("stop_order_id")
        if not stop_id:
            survivors.append(pos)
            continue
        try:
            order = trading.get_order_by_id(stop_id)
            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                data = {"action": "stop_out", "symbol": pos["symbol"], "stop_order_id": stop_id,
                        "qty": pos["qty"], "entry_price": pos["entry_price"]}
                _log_cycle(data)
                continue
        except Exception:
            pass
        survivors.append(pos)
    return survivors


# ---------------------------------------------------------------------------
# Step 4: manage each position
# ---------------------------------------------------------------------------

def manage_positions(positions: list, trading: TradingClient, data_client: StockHistoricalDataClient):
    for pos in positions:
        symbol = pos["symbol"]
        entry = pos["entry_price"]
        qty = pos["qty"]
        state = pos.get("state", "pre_breakeven")
        stop_order_id = pos.get("stop_order_id")

        try:
            price = _current_price(data_client, symbol)
        except Exception:
            continue

        R = price - entry
        initial_stop = pos.get("initial_stop", entry * 0.99)

        if state == "pre_breakeven":
            r75 = (entry - initial_stop) * 0.75
            r100 = entry - initial_stop

            if R >= r100:
                if stop_order_id:
                    _cancel_stop(trading, stop_order_id)
                new_stop = round(entry - 0.01, 2)
                sid = _place_stop(trading, symbol, qty, new_stop)
                pos["stop_order_id"] = sid
                pos["state"] = "post_breakeven_no_partial"
                pos["current_stop"] = new_stop
                data = {"action": "breakeven_stop", "symbol": symbol, "stop_price": new_stop,
                        "order_id": sid}
                _log_cycle(data)

            elif R >= r75:
                partial_qty = math.ceil(qty / 3)
                remaining = qty - partial_qty
                try:
                    cp = subprocess.run(
                        [sys.executable, "trade.py", "--symbol", symbol, "--side", "SELL",
                         "--size", str(partial_qty)],
                        cwd=str(BASE_DIR), timeout=30, capture_output=True, text=True,
                    )
                    data = {"action": "partial_profit", "symbol": symbol, "sell_qty": partial_qty,
                            "remaining": remaining}
                    _log_cycle(data)
                except Exception:
                    continue
                if stop_order_id:
                    _cancel_stop(trading, stop_order_id)
                if remaining > 0:
                    new_stop = round(entry * 0.99, 2)
                    sid = _place_stop(trading, symbol, remaining, new_stop)
                    pos["qty"] = remaining
                    pos["stop_order_id"] = sid
                    pos["current_stop"] = new_stop
                    pos["state"] = "post_breakeven_partial_done"
                else:
                    pos["qty"] = 0

        elif state.startswith("post_breakeven"):
            swings = _swing_lows(symbol)
            if swings:
                highest_swing = max(swings)
                current_stop = pos.get("current_stop", entry)
                new_stop = round(highest_swing - 0.01, 2)
                if new_stop > current_stop:
                    if stop_order_id:
                        _cancel_stop(trading, stop_order_id)
                    sid = _place_stop(trading, symbol, qty, new_stop)
                    pos["stop_order_id"] = sid
                    pos["current_stop"] = new_stop
                    pos["state"] = "post_breakeven_trailing"
                    data = {"action": "trail_stop", "symbol": symbol, "stop_price": new_stop,
                            "swing_low": highest_swing, "order_id": sid}
                    _log_cycle(data)

    return positions


# ---------------------------------------------------------------------------
# Step 8: entry scan
# ---------------------------------------------------------------------------

def entry_scan(trading: TradingClient, data_client: StockHistoricalDataClient, rules: dict):
    existing_positions = _load_state()
    existing_symbols = {p["symbol"] for p in existing_positions if p.get("qty", 0) > 0}

    today_buys = _count_today_buys()
    if today_buys >= MAX_TRADES_PER_DAY:
        print(f"{_et_tag()} hit daily trade limit ({MAX_TRADES_PER_DAY})")
        return

    max_conc = rules.get("risk", {}).get("max_concurrent_positions", 5)
    if len(existing_symbols) >= max_conc:
        print(f"{_et_tag()} already at max concurrent positions ({max_conc})")
        return

    try:
        alpaca_positions = trading.get_all_positions()
        for ap in alpaca_positions:
            existing_symbols.add(ap.symbol)
    except Exception:
        pass

    watchlist = _read_watchlist()
    if not watchlist:
        print(f"{_et_tag()} watchlist empty, skipping entry scan")
        return

    for ticker in watchlist:
        sym = ticker.upper()
        if sym in existing_symbols:
            data = {"action": "skip_duplicate", "symbol": sym}
            _log_cycle(data)
            continue

        ok_daily, reason_d = _check_daily_filters(sym, rules)
        if not ok_daily:
            data = {"action": "skip_daily_filter", "symbol": sym, "reason": reason_d}
            _log_cycle(data)
            continue

        ok_intra, reason_i = _check_intraday_filters(sym, rules)
        if not ok_intra:
            data = {"action": "skip_intraday_filter", "symbol": sym, "reason": reason_i}
            _log_cycle(data)
            continue

        try:
            price = _current_price(data_client, sym)
        except Exception:
            data = {"action": "skip_price_fetch", "symbol": sym}
            _log_cycle(data)
            continue

        lod = _low_of_day(sym)
        if lod is None:
            data = {"action": "skip_no_lod", "symbol": sym}
            _log_cycle(data)
            continue

        initial_stop = round(lod * 0.99, 2)
        R = price - initial_stop
        if R <= 0:
            data = {"action": "skip_negative_R", "symbol": sym, "R": R}
            _log_cycle(data)
            continue

        try:
            equity = float(trading.get_account().equity)
        except Exception:
            equity = PORTFOLIO_VALUE_USD

        risk_dollars = equity * (MAX_RISK_PER_TRADE_PCT / 100)
        size_from_risk = int(risk_dollars / R)
        size_from_budget = int(equity * 0.10 / price)
        size = min(size_from_risk, size_from_budget)

        if size < 1:
            data = {"action": "skip_too_small", "symbol": sym, "size_from_risk": size_from_risk}
            _log_cycle(data)
            continue

        try:
            cp = subprocess.run(
                [sys.executable, "trade.py", "--symbol", sym, "--side", "BUY", "--size", str(size)],
                cwd=str(BASE_DIR), timeout=30, capture_output=True, text=True,
            )
            if cp.returncode != 0:
                data = {"action": "trade_failed", "symbol": sym, "stderr": cp.stderr}
                _log_cycle(data)
                continue
            data = {"action": "entry", "symbol": sym, "size": size, "price": price, "stop": initial_stop}
            _log_cycle(data)
        except subprocess.TimeoutExpired:
            data = {"action": "trade_timeout", "symbol": sym}
            _log_cycle(data)
            continue
        except Exception as e:
            data = {"action": "trade_exception", "symbol": sym, "error": str(e)}
            _log_cycle(data)
            continue

        try:
            sid = _place_stop(trading, sym, size, initial_stop)
        except Exception:
            sid = None

        new_pos = {
            "symbol": sym,
            "entry_price": price,
            "entry_time_iso": _now_et().isoformat(),
            "qty": size,
            "initial_stop": initial_stop,
            "stop_order_id": sid,
            "state": "pre_breakeven",
            "R": 0.0,
        }
        existing_positions.append(new_pos)
        _save_state(existing_positions)
        print(f"{_et_tag()} ENTRY {sym} qty={size} @ ${price:.2f} stop=${initial_stop:.2f}")
        break


# ---------------------------------------------------------------------------
# Step 6: force close
# ---------------------------------------------------------------------------

def force_close(trading: TradingClient, positions: list):
    for pos in positions:
        sid = pos.get("stop_order_id")
        if sid:
            _cancel_stop(trading, sid)
        sym = pos["symbol"]
        qty = pos["qty"]
        if qty > 0:
            try:
                cp = subprocess.run(
                    [sys.executable, "trade.py", "--symbol", sym, "--side", "SELL",
                     "--size", str(qty)],
                    cwd=str(BASE_DIR), timeout=30, capture_output=True, text=True,
                )
                data = {"action": "force_close", "symbol": sym, "qty": qty, "result": cp.stdout.strip()}
                _log_cycle(data)
                print(f"{_et_tag()} FORCE CLOSE {sym} qty={qty}")
            except Exception as e:
                data = {"action": "force_close_failed", "symbol": sym, "error": str(e)}
                _log_cycle(data)
    _save_state([])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    key, secret, base = _load_env()
    trading = TradingClient(key, secret, paper=True, url_override=base)
    data_client = StockHistoricalDataClient(key, secret)

    rules = _load_rules()
    gate = time_gate(rules)
    print(f"{_et_tag()} gate={gate}")

    if gate in ("weekend", "too_early", "closed"):
        print(f"{_et_tag()} market closed, exiting")
        _log_cycle({"cycle": _now_et().isoformat(), "gate": gate, "action": "exit"})
        return

    positions = _load_state()
    positions = check_stop_outs(positions, trading)

    if positions:
        positions = manage_positions(positions, trading, data_client)
        _save_state(positions)

    if gate == "force_close":
        force_close(trading, positions)
        return

    if gate == "manage_only":
        print(f"{_et_tag()} manage only, no new entries")
        return

    entry_scan(trading, data_client, rules)

    _save_state(positions)
    print(f"{_et_tag()} cycle complete")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a") as f:
            f.write(f"[{_now_et().isoformat()}] UNHANDLED ERROR\n")
            f.write(traceback.format_exc())
            f.write("\n")
        print(f"{_et_tag()} CRASH: {e}", file=sys.stderr)
        sys.exit(1)
