import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
TRADES_CSV = os.path.join(BASE_DIR, "trades", "trades.csv")

load_dotenv(ENV_PATH)

parser = argparse.ArgumentParser()
parser.add_argument("--symbol", required=True)
parser.add_argument("--side", required=True, choices=["BUY", "SELL"])
parser.add_argument("--size", required=True, type=int)
args = parser.parse_args()

key = os.getenv("ALPACA_API_KEY")
secret = os.getenv("ALPACA_API_SECRET")
base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

try:
    client = TradingClient(key, secret, paper=True, url_override=base)
except Exception as e:
    print(json.dumps({"error": f"connection failed: {e}"}))
    sys.exit(1)

try:
    order_request = MarketOrderRequest(
        symbol=args.symbol,
        qty=args.size,
        side=OrderSide.BUY if args.side == "BUY" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = client.submit_order(order_request)
    order_id = order.id
except Exception as e:
    print(json.dumps({"error": f"submit failed: {e}"}))
    sys.exit(1)

SETTLED = {
    OrderStatus.FILLED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.REJECTED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
}

for _ in range(10):
    time.sleep(1)
    order = client.get_order_by_id(order_id)
    if order.status in SETTLED:
        break

fill_price = str(order.filled_avg_price) if order.filled_avg_price else "pending"
status_val = order.status.value if hasattr(order.status, "value") else str(order.status)

result = {
    "order_id": str(order.id),
    "symbol": args.symbol,
    "side": args.side,
    "qty": args.size,
    "fill_price": fill_price,
    "status": status_val,
}
print(json.dumps(result))

row = (
    f"{datetime.now(timezone.utc).isoformat()},"
    f"{args.symbol},{args.side},{args.size},{fill_price},"
    f"{order.id},{status_val}\n"
)
with open(TRADES_CSV, "a") as f:
    f.write(row)

os.makedirs(os.path.join(BASE_DIR, "trades"), exist_ok=True)
