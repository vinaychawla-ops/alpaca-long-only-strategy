import os
import sys
import json
import csv
import argparse
import subprocess
import zoneinfo
from datetime import datetime, date

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
TRADES_DIR = os.path.join(BASE_DIR, "trades")
TRADES_CSV = os.path.join(TRADES_DIR, "trades.csv")

MAX_TRADES_PER_DAY = 3
MAX_TRADE_SIZE_USD = 2000

ET = zoneinfo.ZoneInfo("America/New_York")


def _et_tag():
    return datetime.now(ET).strftime("[%H:%M:%S ET]")


def _ensure_trades_csv():
    os.makedirs(TRADES_DIR, exist_ok=True)
    if not os.path.isfile(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_iso", "symbol", "side", "size", "fill_price", "order_id", "status"])


def _count_today_buys():
    today_str = date.today().isoformat()
    count = 0
    if not os.path.isfile(TRADES_CSV):
        return 0
    with open(TRADES_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("timestamp_iso", "")
            side = row.get("side", "")
            if ts.startswith(today_str) and side.upper() == "BUY":
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)

    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_API_SECRET")
    base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    if "paper-api" not in base:
        print(f"{_et_tag()} ABORT: expected paper URL but got {base}")
        sys.exit(1)

    _ensure_trades_csv()

    today_buys = _count_today_buys()
    if today_buys >= MAX_TRADES_PER_DAY:
        print(f"{_et_tag()} hit daily trade limit ({MAX_TRADES_PER_DAY})")
        sys.exit(0)

    sys.path.insert(0, BASE_DIR)
    import strategy
    result = strategy.evaluate(args.symbol, force=args.force)
    print(f"{_et_tag()} evaluate: {json.dumps(result)}")

    if args.check_only:
        print(f"{_et_tag()} check only, exiting")
        sys.exit(0)

    if not result["pass"]:
        print(f"{_et_tag()} skipped: {'; '.join(result['reasons'])}")
        sys.exit(0)

    try:
        tc = TradingClient(key, secret, paper=True, url_override=base)
        equity = float(tc.get_account().equity)
    except Exception as e:
        print(f"{_et_tag()} account fetch error: {e}")
        sys.exit(1)

    price = result["price"]
    budget = min(MAX_TRADE_SIZE_USD, equity * 0.10)
    qty = int(budget / price)

    if qty < 1:
        print(f"{_et_tag()} position too small — ${budget:.2f} / ${price:.2f} = 0 shares")
        sys.exit(0)

    print(f"{_et_tag()} buying {qty} share(s) of {args.symbol} @ ${price:.2f} (budget=${budget:.2f})")

    try:
        cp = subprocess.run(
            [sys.executable, "trade.py", "--symbol", args.symbol, "--side", "BUY", "--size", str(qty)],
            cwd=BASE_DIR,
            timeout=30,
            capture_output=True,
            text=True,
        )
        if cp.stdout:
            print(cp.stdout.strip())
        if cp.stderr:
            print(cp.stderr.strip())
    except subprocess.TimeoutExpired:
        print(f"{_et_tag()} trade.py timed out after 30s")
        sys.exit(1)

    if not os.path.isfile(TRADES_CSV):
        print(f"{_et_tag()} trades.csv missing after trade")
        sys.exit(1)

    with open(TRADES_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    if rows:
        last = rows[-1]
        order_id = last.get("order_id", "?")
        status = last.get("status", "?")
        fill = last.get("fill_price", "?")
        if status in ("filled", "partially_filled"):
            print(f"{_et_tag()} trade succeeded: order={order_id} status={status} fill={fill}")
        else:
            print(f"{_et_tag()} trade issue: order={order_id} status={status} fill={fill}")
    else:
        print(f"{_et_tag()} trades.csv is empty after trade")

    print(f"{_et_tag()} done")


if __name__ == "__main__":
    main()
