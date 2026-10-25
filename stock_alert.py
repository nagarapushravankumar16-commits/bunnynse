"""
NSE stock breakout + volume spike alerts -> Telegram (separate stock bot)
Scans the Nifty 50 on 5-minute candles using free Yahoo Finance data (via yfinance).
NOTE: Yahoo intraday data is delayed ~15 minutes, so treat this as a TEST tool.

Trigger (both must be true, on the last CLOSED 5m candle):
  1. Breakout:     close > highest high of the previous LOOKBACK candles
  2. Volume spike: volume >= VOLUME_MULT x average volume of those candles
Only runs during NSE market hours (Mon-Fri, 09:15-15:30 IST).
"""
import os
import time
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

# Secrets come from GitHub (STOCK_BOT_TOKEN and TELEGRAM_CHAT_ID)
TELEGRAM_TOKEN = os.getenv("STOCK_BOT_TOKEN", "PASTE_YOUR_STOCK_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

# ---- Settings ----
INTERVAL = "5m"
LOOKBACK = 10            # candles for the breakout high + average volume
VOLUME_MULT = 1.5        # volume must be this many times the average
MIN_MOVE_PCT = 0.3       # breakout candle must be up at least this %
REWARD_RISK = 1.5        # target = entry + this x risk
STOP_BUFFER = 0.001      # stop sits 0.1% below the stop level
MIN_TARGET_PCT = 0.4     # skip if target is smaller than this %
MAX_MOVE_PCT = 8.0       # ignore bad ticks: a single 5m candle moving more than this is not real
COOLDOWN_CANDLES = 4     # skip a stock that already broke out in the last few candles
CAPITAL_INR = 10000      # example trade size for the cost estimate
SEND_TEST_MESSAGE = False
IGNORE_MARKET_HOURS = False   # set True only to test outside market hours

# ---- Zerodha intraday (MIS) equity cost estimates, edit if your charges differ ----
BROKERAGE_PER_ORDER = 20.0    # flat max per executed order (or 0.03% if lower)
BROKERAGE_PCT = 0.03          # percent of turnover
STT_SELL_PCT = 0.025          # STT on the sell side, intraday
EXCHANGE_TXN_PCT = 0.00297    # NSE transaction charge, both sides
SEBI_PCT = 0.0001             # SEBI turnover fee, both sides
STAMP_BUY_PCT = 0.003         # stamp duty, buy side only
GST_PCT = 0.18                # GST on brokerage + exchange + SEBI charges

NIFTY50 = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO",
    "BAJFINANCE","BAJAJFINSV","BEL","BHARTIARTL","BPCL","BRITANNIA","CIPLA",
    "COALINDIA","DRREDDY","EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE",
    "HEROMOTOCO","HINDALCO","HINDUNILVR","ICICIBANK","INDUSINDBK","INFY","ITC",
    "JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","NESTLEIND","NTPC","ONGC",
    "POWERGRID","RELIANCE","SBILIFE","SBIN","SHRIRAMFIN","SUNPHARMA","TATACONSUM",
    "TATAMOTORS","TATASTEEL","TCS","TECHM","TITAN","TRENT","ULTRACEMCO","WIPRO",
]

IST = timezone(timedelta(hours=5, minutes=30))


def market_open(now=None):
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= t <= 15 * 60 + 35


def trade_costs(entry, exit_price, qty):
    """Round-trip Zerodha intraday costs in rupees for a long trade."""
    buy_val, sell_val = entry * qty, exit_price * qty
    def brokerage(v):
        return min(BROKERAGE_PER_ORDER, v * BROKERAGE_PCT / 100)
    brk = brokerage(buy_val) + brokerage(sell_val)
    exch = (buy_val + sell_val) * EXCHANGE_TXN_PCT / 100
    sebi = (buy_val + sell_val) * SEBI_PCT / 100
    gst = (brk + exch + sebi) * GST_PCT
    stt = sell_val * STT_SELL_PCT / 100
    stamp = buy_val * STAMP_BUY_PCT / 100
    return brk + exch + sebi + gst + stt + stamp


def outcome(entry, target, stop):
    qty = int(CAPITAL_INR // entry)
    if qty < 1:
        return None
    win_gross = (target - entry) * qty
    loss_gross = (stop - entry) * qty
    win_net = win_gross - trade_costs(entry, target, qty)
    loss_net = loss_gross - trade_costs(entry, stop, qty)
    return qty, win_net, loss_net, trade_costs(entry, target, qty)


def fetch_candles(symbol):
    df = yf.download(f"{symbol}.NS", period="1d", interval=INTERVAL,
                     progress=False, auto_adjust=False, threads=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "levels"):          # newer yfinance returns multi-level columns
        df.columns = [c[0] for c in df.columns]
    return df.dropna()


def check_stock(symbol):
    try:
        df = fetch_candles(symbol)
        if df is None or len(df) < LOOKBACK + COOLDOWN_CANDLES + 3:
            return None
        df = df.iloc[:-1]                      # drop the still-forming candle
        last = df.iloc[-1]
        prev = df.iloc[-1 - LOOKBACK:-1]
        close, open_, low = float(last["Close"]), float(last["Open"]), float(last["Low"])
        volume = float(last["Volume"])
        prev_high = float(prev["High"].max())
        avg_vol = float(prev["Volume"].mean())
        if avg_vol <= 0 or open_ <= 0:
            return None
        vol_ratio = volume / avg_vol
        move = (close - open_) / open_ * 100
        if move > MAX_MOVE_PCT or close > prev_high * 1.15:
            return None                        # almost certainly a bad data tick

        # cooldown: skip if one of the last few candles already broke out
        recent = df.iloc[-1 - COOLDOWN_CANDLES:-1]
        base = df.iloc[-1 - COOLDOWN_CANDLES - LOOKBACK:-1 - COOLDOWN_CANDLES]
        if len(base) == LOOKBACK and (recent["Close"] > float(base["High"].max())).any():
            return None

        if close > prev_high and vol_ratio >= VOLUME_MULT and move >= MIN_MOVE_PCT:
            entry = close
            stop = min(low, prev_high) * (1 - STOP_BUFFER)
            risk = entry - stop
            if risk <= 0:
                return None
            target = entry + REWARD_RISK * risk
            if (target - entry) / entry * 100 < MIN_TARGET_PCT:
                return None
            res = outcome(entry, target, stop)
            if res is None:
                return None
            qty, win_net, loss_net, costs = res
            return {"symbol": symbol, "entry": entry, "stop": stop, "target": target,
                    "prev_high": prev_high, "vol_ratio": vol_ratio, "move": move,
                    "risk_pct": risk / entry * 100,
                    "reward_pct": (target - entry) / entry * 100,
                    "qty": qty, "win_net": win_net, "loss_net": loss_net, "costs": costs}
    except Exception as e:
        print(f"{symbol}: error {e}")
    return None


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=15,
    )
    if r.status_code != 200:
        print("Telegram error:", r.text)


def build_message(s):
    sym = s["symbol"]
    return (
        f"\U0001F4C8 <b>{sym}</b> breakout ({INTERVAL}, NSE)\n"
        f"Price: \u20b9{s['entry']:,.2f} ({s['move']:+.2f}% this candle)\n"
        f"Broke {LOOKBACK}-candle high: \u20b9{s['prev_high']:,.2f}\n"
        f"Volume: {s['vol_ratio']:.1f}x average\n\n"
        f"Entry: \u20b9{s['entry']:,.2f}\n"
        f"Stop loss: \u20b9{s['stop']:,.2f} (-{s['risk_pct']:.2f}%)\n"
        f"Target: \u20b9{s['target']:,.2f} (+{s['reward_pct']:.2f}%)\n"
        f"Reward:Risk = {REWARD_RISK:.1f}:1\n\n"
        f"<b>Zerodha intraday (MIS, LONG), \u20b9{CAPITAL_INR:,} capital</b>\n"
        f"Quantity: {s['qty']} shares\n"
        f"If target hits: net \u20b9{s['win_net']:+,.0f} (charges \u20b9{s['costs']:,.0f})\n"
        f"If stop hits: net \u20b9{s['loss_net']:+,.0f}\n"
        f"Charges are estimates. Data is delayed ~15 min, so check the live price in Kite first.\n"
        f"News: https://www.google.com/search?q={sym}+share+news&tbm=nws\n"
        f"Not financial advice. Rule-based levels only."
    )


def main():
    if SEND_TEST_MESSAGE:
        send_telegram("\u2705 Stock alert bot is connected.")
    if not IGNORE_MARKET_HOURS and not market_open():
        print("NSE is closed, nothing to scan.")
        return
    print(f"Scanning {len(NIFTY50)} stocks on {INTERVAL} candles...")
    results = []
    for sym in NIFTY50:
        r = check_stock(sym)
        if r:
            results.append(r)
        time.sleep(0.3)                        # be gentle with Yahoo
    print(f"{len(results)} signal(s) found")
    for s in sorted(results, key=lambda x: -x["vol_ratio"]):
        send_telegram(build_message(s))
        time.sleep(0.5)


if __name__ == "__main__":
    main()
