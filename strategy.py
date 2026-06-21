import os
import json
import zoneinfo
from datetime import datetime, time, date

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(BASE_DIR, "rules.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
ENV_PATH = os.path.join(BASE_DIR, ".env")


def _load_clients():
    load_dotenv(ENV_PATH)
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_API_SECRET")
    base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    trading = TradingClient(key, secret, paper=True, url_override=base)
    data = StockHistoricalDataClient(key, secret)
    return trading, data


def _load_rules():
    with open(RULES_PATH) as f:
        return json.load(f)


def _to_et_now():
    return datetime.now(zoneinfo.ZoneInfo("America/New_York"))


def _current_time_str(dt):
    return dt.strftime("%H:%M")


def _parse_time(s):
    parts = s.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


def evaluate(symbol: str, force: bool = False) -> dict:
    result = {}

    try:
        trading, data = _load_clients()
    except Exception as exc:
        return {
            "pass": bool(False),
            "reasons": [f"connection error: {exc}"],
            "price": 0.0,
        }

    try:
        positions = trading.get_all_positions()
    except Exception as exc:
        return {
            "pass": bool(False),
            "reasons": [f"position fetch error: {exc}"],
            "price": 0.0,
        }

    for p in positions:
        if p.symbol == symbol.upper() and float(p.qty) > 0:
            result = {
                "pass": bool(False),
                "reasons": ["already in position"],
                "price": 0.0,
            }
            _log_result(result)
            return result

    try:
        rules = _load_rules()
    except Exception as exc:
        return {
            "pass": bool(False),
            "reasons": [f"rules load error: {exc}"],
            "price": 0.0,
        }

    tf = rules["time_filter"]
    earliest = tf["earliest_entry_et"]
    latest = tf["latest_entry_et"]

    now_et = _to_et_now()
    now_time = now_et.time()
    earliest_t = _parse_time(earliest)
    latest_t = _parse_time(latest)

    if not force and not (earliest_t <= now_time <= latest_t):
        result = {
            "pass": bool(False),
            "reasons": [f"outside entry window {earliest}-{latest}"],
            "price": 0.0,
        }
        _log_result(result)
        return result

    try:
        trade_req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trade_data = data.get_stock_latest_trade(trade_req)
        trade = trade_data[symbol.upper()]
        price = float(trade.price)
    except Exception as exc:
        result = {
            "pass": bool(False),
            "reasons": [f"price fetch error: {exc}"],
            "price": 0.0,
        }
        _log_result(result)
        return result

    result = {
        "pass": bool(True),
        "reasons": ["time gate ok", "no existing position"],
        "price": price,
    }
    _log_result(result)
    return result


def _log_result(result: dict):
    os.makedirs(LOGS_DIR, exist_ok=True)
    line = json.dumps(result)
    log_path = os.path.join(LOGS_DIR, "safety_log.jsonl")
    with open(log_path, "a") as f:
        f.write(line + "\n")
