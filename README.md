# Alpaca Long-Only Trading Bot

A paper-trading bot for Alpaca Markets that screens the S&P 500 for gap/momentum setups, manages positions with trailing stops, and runs autonomously via Windows Task Scheduler.

**Direction:** long only  
**Universe:** S&P 500 (~503 stocks)  
**Timeframe:** 5-minute bars  
**Broker:** Alpaca Markets (paper)  
**Market hours:** US equities, regular session (9:30–16:00 ET)

---

## Prerequisites

- **Python 3.10+** installed and on PATH
- **VS Code** with the [Python extension](https://marketplace.visualstudio.com/items?itemName=ms-python.python)
- A free **Alpaca Markets paper-trading account** at [alpaca.markets](https://alpaca.markets)

---

## Quick Setup

### 1. Create a virtual environment

Open the project folder in VS Code, then open a terminal (`` Ctrl+` ``):

```powershell
python -m venv .venv
.venv\Scripts\activate
```

When the terminal prompt shows `(.venv)` at the start, the environment is active.

### 2. Install dependencies

```powershell
pip install -r ..\requirements.txt
pip install yfinance    # bar data provider (installed separately)
```

### 3. Configure your Alpaca credentials

Create a file named `.env` in this directory with your paper-trading keys:

```
ALPACA_API_KEY=PK1234567890abcdef
ALPACA_API_SECRET=yourSecretKeyHere
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

Get these from the Alpaca Dashboard → Paper Trading → API Keys.

> The bot **will refuse to run** if `ALPACA_BASE_URL` does not contain `paper-api`. This is a hard safety guard.

### 4. Verify setup

```powershell
python strategy.py --symbol NVDA --check-only
```

Expected output (market hours):
```
strategy.evaluate(symbol=NVDA): {"pass": false, "reasons": [...], "price": ...}
```

If you see `connection error` or `position fetch error`, double-check your `.env` file and that your Alpaca account is active.

---

## Project Structure

```
alpaca-long-only-strategy/
├── .env                    # Alpaca credentials (not committed)
├── rules.json              # All strategy configuration
├── strategy.py             # Single-symbol evaluation engine
├── bot.py                  # CLI orchestrator (evaluate → size → trade)
├── trade.py                # Generalized buy/sell subprocess
├── cycle.py                # Autonomous 9-step trading cycle
├── morning_prefilter.py    # S&P 500 gap screener
├── src/
│   ├── __init__.py
│   └── sp500_tickers.py    # 503 tickers (alphabetical, IBKR format)
├── state/
│   └── positions.json      # Per-position stop levels & state machine
├── trades/
│   └── trades.csv          # Append-only trade log
├── logs/
│   ├── safety_log.jsonl        # From strategy.evaluate()
│   ├── safety-check-log.json   # From cycle.py
│   └── cycle_errors.log        # Unhandled exceptions from cycle.py
└── watchlist.txt           # Output of morning_prefilter.py
```

### File roles

| File | What it does |
|------|-------------|
| `strategy.py` | Checks time gate, Alpaca position dedup, and fetches latest trade price for one symbol. Called by `bot.py` and usable standalone. |
| `bot.py` | CLI wrapper: runs `strategy.evaluate()`, sizes the position (min of 1% risk / 10% portfolio / $2000 cap), spawns `trade.py` as a subprocess, and logs to `trades.csv`. Supports `--check-only` and `--force`. |
| `trade.py` | Places a market order on Alpaca, polls for 10 seconds, appends the result to `trades.csv`. Designed to be called as a subprocess with `--symbol`, `--side`, `--size`. |
| `cycle.py` | Full 9-step autonomous cycle (see below). Designed to run every 5 minutes via Task Scheduler. |
| `morning_prefilter.py` | Downloads 2-day daily bars for all 503 S&P 500 tickers via yfinance, filters by gap % from prior close and minimum price, writes the top 20 to `watchlist.txt`. Supports `--dry-run`. |
| `rules.json` | All tunable strategy parameters: time windows, daily/intraday filters, exit rules, risk limits. |
| `src/sp500_tickers.py` | Alphabetically sorted list of all 503 S&P 500 tickers. Class B shares use a space (e.g. `BRK B`, `BF B`). |

---

## Configuration (`rules.json`)

```json
{
  "strategy_name": "Trend Join Long",
  "direction": "long_only",

  "time_filter": {
    "earliest_entry_et": "10:05",
    "latest_entry_et": "15:30",
    "force_close_et": "15:51"
  },

  "daily_filters": {
    "D1_above_prior_day_high": true,
    "D2_prior_close_above_sma200": true,
    "D3_min_gap_pct_from_prior_close": 3.0
  },

  "intraday_filters": {
    "I1_above_premarket_high": true,
    "I2_above_today_hod": true,
    "I3_rvol_min": 2.0,
    "I3_rvol_lookback_days": 14
  },

  "exit": {
    "initial_stop_rule": "lod_minus_1pct",
    "partial_profit_trigger_R": 0.75,
    "partial_profit_fraction": 0.3333,
    "breakeven_trigger_R": 1.0,
    "post_breakeven_trail": "swing_low_5m_2_2"
  },

  "risk": {
    "max_risk_per_trade_pct": 1.0,
    "max_position_size_pct_of_portfolio": 10,
    "max_concurrent_positions": 5
  }
}
```

### Field reference

| Field | Default | Description |
|-------|---------|-------------|
| `earliest_entry_et` | `10:05` | No new entries before this time |
| `latest_entry_et` | `15:30` | No new entries after this time |
| `force_close_et` | `15:51` | All positions liquidated at/after this time |
| `D1_above_prior_day_high` | `true` | Today's high must exceed yesterday's high |
| `D2_prior_close_above_sma200` | `true` | Yesterday's close must be above the 200-day SMA |
| `D3_min_gap_pct` | `3.0` | Minimum gap % from prior close (used in prefilter) |
| `I1_above_premarket_high` | `true` | Current price above premarket high (first 5m bar) |
| `I2_above_today_hod` | `true` | Current price must break today's high-of-day |
| `I3_rvol_min` | `2.0` | Minimum relative volume vs 14-day avg |
| `max_risk_per_trade_pct` | `1.0` | Max % of portfolio risked per trade |
| `max_concurrent_positions` | `5` | Maximum positions held simultaneously |
| `initial_stop_rule` | `lod_minus_1pct` | Stop = low of day × 0.99 |
| `partial_profit_trigger_R` | `0.75` | Take partial profit at 0.75× risk |
| `breakeven_trigger_R` | `1.0` | Move stop to breakeven at 1× risk |

---

## CLI Usage

### Morning prefilter

Run before market open (~9:45 ET) to screen the S&P 500:

```powershell
# Dry run — prints survivors, does not write watchlist.txt
python morning_prefilter.py --dry-run

# Live run — writes watchlist.txt
python morning_prefilter.py

# Custom thresholds
python morning_prefilter.py --min-gap 5.0 --min-price 10.0
```

Output is JSON to stdout. Degradation alerts go to stderr if ≥30% or ≥95% of tickers fail to load.

### Single-symbol evaluation & trade

```powershell
# Check eligibility only
python bot.py --symbol NVDA --check-only

# Evaluate + buy (if eligible)
python bot.py --symbol NVDA

# Bypass time gate (e.g. after-hours testing)
python bot.py --symbol NVDA --force
```

The bot will skip if:
- Daily trade cap (3) has been reached
- Already in a position for that symbol
- Outside the entry window (10:05–15:30 ET)
- Position size rounds to 0 shares

### Standalone trade execution

```powershell
python trade.py --symbol AAPL --side BUY --size 5
python trade.py --symbol AAPL --side SELL --size 5
```

This places a DAY market order, polls up to 10 seconds, appends to `trades.csv`, and prints JSON.

### Autonomous cycle

```powershell
python cycle.py
```

This is the full 9-step cycle (see below). It reads state, manages positions, and may enter new trades.

---

## Autonomous Cycle (9-Step Flow)

`cycle.py` is designed to run every 5 minutes during market hours via Windows Task Scheduler. Each invocation is a single-shot pass through these steps:

```
┌─────────────────────────────────────────────────┐
│ 1. TIME GATE                                    │
│    weekday? → weekend exit                      │
│    <10:00 or ≥16:00 → closed exit               │
│    10:00-10:04 or 15:30-15:50 → manage_only     │
│    ≥15:51 → force_close                         │
│    else → ok                                     │
├─────────────────────────────────────────────────┤
│ 2. LOAD STATE                                   │
│    Read state/positions.json                     │
├─────────────────────────────────────────────────┤
│ 3. CHECK STOP-OUTS                              │
│    Query each stop order on Alpaca               │
│    Remove any that filled (stop-out)             │
├─────────────────────────────────────────────────┤
│ 4. MANAGE POSITIONS                             │
│    pre_breakeven:                                │
│      ≥0.75R → sell ⅓ as partial profit          │
│      ≥1.0R → cancel old stop, place breakeven   │
│    post_breakeven:                               │
│      Trail stop to highest swing low − 0.01     │
├─────────────────────────────────────────────────┤
│ 5. SAVE STATE                                   │
│    Atomic write via .tmp + os.replace           │
├─────────────────────────────────────────────────┤
│ 6. FORCE CLOSE (if gate=force_close)            │
│    Cancel all stops, sell all positions          │
│    Clear state → exit                            │
├─────────────────────────────────────────────────┤
│ 7. MANAGE ONLY (if gate=manage_only)            │
│    Print message → exit (no new entries)         │
├─────────────────────────────────────────────────┤
│ 8. ENTRY SCAN (if gate=ok)                      │
│    Read watchlist.txt                            │
│    For each ticker:                              │
│      Skip if already in position                 │
│      Check D1-D3 daily filters (yfinance)        │
│      Check I1-I3 intraday filters (yfinance)    │
│      Compute R = price − (low_of_day × 0.99)    │
│      Size = min(risk_budget / R, 10% portfolio) │
│      If size ≥ 1 → place BUY → place GTC stop   │
│      Append to state → break (one entry/cycle) │
├─────────────────────────────────────────────────┤
│ 9. FINAL SAVE & EXIT                            │
└─────────────────────────────────────────────────┘
```

---

## Scheduling with Windows Task Scheduler

1. Open **Task Scheduler** → Create Task
2. **General**: Name = `Alpaca Trading Cycle`, run whether user is logged on or not
3. **Triggers**: New → Daily, repeat every 5 minutes for 6.5 hours (9:30–16:00), start at 9:30
4. **Actions**: New → Start a program
   - Program: `C:\Users\...\.venv\Scripts\python.exe`
   - Arguments: `cycle.py`
   - Start in: `C:\Users\...\alpaca-long-only-strategy`
5. **Conditions**: Uncheck "Stop if running for X" — the cycle completes in under 30s

---

## Filter Reference

### Daily filters (D1–D3)

| Filter | Logic | Data source |
|--------|-------|-------------|
| D1 | `today_high > yesterday_high` | yfinance 5d, 1d bars |
| D2 | `yesterday_close > SMA(200)` | yfinance 5d, 1d bars (skipped if <200 bars) |
| D3 | gap % ≥ threshold in `morning_prefilter.py` | yfinance 2d, 1d bars |

### Intraday filters (I1–I3)

| Filter | Logic | Data source |
|--------|-------|-------------|
| I1 | `current_price > premarket_high` (first 5m bar high) | yfinance 1d, 5m bars |
| I2 | `current_price > today_high_of_day` | yfinance 1d, 5m bars |
| I3 | `today_volume / avg_14d_volume ≥ rvol_min` | yfinance 14d daily volume + 1d 5m volume |

### Position management

| Trigger | Action |
|---------|--------|
| Entry | BUY market order + GTC stop at `low_of_day × 0.99` |
| R ≥ 0.75 | Sell ⅓ of position (partial profit) |
| R ≥ 1.0 | Cancel stop, set new stop at `entry − $0.01` (breakeven) |
| Post-breakeven | Trail stop to `highest_swing_low − $0.01` |
| Force close (15:51) | Cancel stops, sell whole position |

---

## Log Files

| File | Format | Contents |
|------|--------|----------|
| `logs/safety_log.jsonl` | JSONL (append) | Every `strategy.evaluate()` call |
| `logs/safety-check-log.json` | JSONL (append) | Every `cycle.py` action (stop-out, entry, trail, etc.) |
| `logs/cycle_errors.log` | Plain text (append) | Unhandled exceptions from `cycle.py` |
| `trades/trades.csv` | CSV (append) | Every trade placed, with status |

---

## Example Prompts

These are designed for use with AI coding assistants (Claude Code, etc.):

```
"Check NVDA for entry eligibility"
"Run the morning prefilter with --dry-run"
"Place a BUY for 5 shares of AAPL on Alpaca paper"
"Show me today's trades from trades.csv"
"What's in the watchlist?"
"Run the full cycle.py"
"What's the current portfolio value?"
"Cancel the pending NVDA order that was placed today"
"Force sell all positions"
"Show the last 5 entries from safety-check-log.json"
"Add a new ticker QCOM to the watchlist"
"Run python morning_prefilter.py --min-gap 5.0"
```

---

## Troubleshooting

### "expected paper URL" error
Your `.env` `ALPACA_BASE_URL` does not contain `paper-api`. Only paper-trading URLs are allowed.

### Orders stuck in "accepted"
The market is closed. Orders submitted outside regular hours remain in `accepted` and will fill at the next market open (Mon–Fri, 9:30 ET).

### yfinance download fails
- Run `morning_prefilter.py --dry-run` first to test connectivity
- If ≥95% of tickers fail, Yahoo may have changed their API — check `yfinance` changelog
- Reduce concurrent threads: the script uses `threads=5` by default

### "position too small"
Your budget (10% of portfolio or $2000 cap) ÷ stock price rounds to 0 shares. Either use a larger portfolio or trade cheaper stocks.

### Task Scheduler runs but nothing happens
Check `logs/cycle_errors.log` for unhandled exceptions. Common issues:
- `.env` not in the expected directory (the scheduled task's "Start in" folder)
- Virtual environment path is stale after a Python upgrade

---

## Safety Features

- **Paper-only guard**: hard-coded check that `ALPACA_BASE_URL` contains `paper-api`
- **Position dedup**: refuses to buy a symbol already held
- **Daily trade cap**: max 3 BUY trades per calendar day
- **Max concurrent positions**: configurable in `rules.json` (default 5)
- **Atomic state writes**: `positions.json` is written via `.tmp` + `os.replace` to prevent corruption
- **Degradation tripwires**: stderr alerts when yfinance failure rate exceeds 30% or 95%
- **Force close window**: all positions liquidated after 15:51 ET
- **Time gate enforcement**: new entries only between earliest_entry_et and latest_entry_et

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `alpaca-py` | Alpaca trading & data API |
| `yfinance` | Free bar data (screening + indicators) |
| `python-dotenv` | `.env` file loading |
| `pandas` | DataFrame operations (yfinance output) |
| `requests` | HTTP (Alpaca SDK dependency) |
| `pydantic` / `pydantic-settings` | Alpaca SDK models |

---

## Notes

- All times are US Eastern (`America/New_York`)
- The S&P 500 ticker list includes class B shares as `BRK B`, `BF B` (space-separated, NOT hyphenated)
- `cycle.py` is single-shot per invocation — no infinite loop. The OS scheduler handles periodic re-execution.
- This is **paper trading only**. No real money is at risk.
