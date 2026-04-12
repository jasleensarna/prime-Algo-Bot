"""
APEX Pro Bot — v2.0
═══════════════════════════════════════════════════════════════
WHAT'S NEW vs v1:

ENTRY — 3-layer system:
  Layer 1: Trend Gate (all 3 must pass or trade is BLOCKED)
    • UT Bot direction on 15m candles
    • ADX > 25 (trend has strength, not chop)
    • Price above/below VWAP

  Layer 2: Score (minimum 65 required)
    • OBI            — 35 pts  (unchanged)
    • CVD + CVD Div  — 35 pts  (upgraded from raw flow)
    • OI Delta       — 20 pts  (replaces funding rate)
    • All 3 agree    — +10 pts bonus

EXIT — 3-layer system:
  Exit 1: Normal TP/SL (bot handles)
    • TP1 hit → move SL to breakeven immediately
    • TP2 hit → trail SL
    • TP3 hit → full close

  Exit 2: Momentum Exhaustion (Jasleen's eye, now coded)
    • 3 consecutive shrinking candle bodies
    • Current body < 40% of 10-candle average
    • CVD slope flattening or turning negative
    • Only triggers if already > 2% in profit

  Exit 3: Structure Break
    • CVD divergence confirms reversal
    • Full close immediately

SCORE THRESHOLD: minimum 65 (MEDIUM signals blocked)
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn, os, time, asyncio, hmac, hashlib, json
import httpx

app = FastAPI()

# ── ENV ──────────────────────────────────────────────────────
API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BASE_URL   = "https://api.bybit.com"
MIN_SCORE  = 75          # only STRONG 75+ and VERY STRONG 80+
LEVERAGE   = 2
SCAN_INTERVAL = 30       # seconds between scans

# ── SUPABASE ─────────────────────────────────────────────────
SB_URL = os.getenv("SUPABASE_URL", "")
SB_KEY = os.getenv("SUPABASE_KEY", "")

# ── IN-MEMORY STATE ──────────────────────────────────────────
state = {
    "wins": 0, "losses": 0, "total_pnl": 0.0,
    "trades": [], "position": None, "balance": 0.0,
    "last_scan": 0, "status": "starting",
    "trend_blocks": 0,   # count how many signals trend gate blocked
    "score_blocks": 0,   # count how many signals score blocked
}

# ═══════════════════════════════════════════════════════════════
#  BYBIT HELPERS
# ═══════════════════════════════════════════════════════════════

def bybit_sign(params: dict) -> dict:
    params["api_key"]   = API_KEY
    params["timestamp"] = str(int(time.time() * 1000))
    params["recv_window"] = "5000"
    sorted_params = sorted(params.items())
    query = "&".join(f"{k}={v}" for k, v in sorted_params)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["sign"] = sig
    return params

async def bybit_get(path: str, params: dict = {}, signed: bool = False) -> dict:
    if signed:
        params = bybit_sign(params)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BASE_URL + path, params=params)
        return r.json()

async def bybit_post(path: str, body: dict) -> dict:
    body = bybit_sign(body)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(BASE_URL + path, json=body)
        return r.json()

async def get_balance() -> float:
    try:
        r = await bybit_get("/v5/account/wallet-balance",
                            {"accountType": "UNIFIED"}, signed=True)
        coins = r["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                return float(c["availableToWithdraw"])
    except Exception as e:
        print(f"Balance error: {e}")
    return 0.0

async def get_klines(symbol: str, interval: str = "5", limit: int = 50) -> list:
    """Fetch OHLCV candles. interval: '5'=5m, '15'=15m"""
    try:
        r = await bybit_get("/v5/market/kline", {
            "category": "linear", "symbol": symbol,
            "interval": interval, "limit": str(limit)
        })
        # Returns newest first — reverse to oldest first
        return list(reversed(r["result"]["list"]))
    except Exception as e:
        print(f"Klines error {symbol}: {e}")
        return []

async def get_orderbook(symbol: str, limit: int = 50) -> dict:
    try:
        r = await bybit_get("/v5/market/orderbook", {
            "category": "linear", "symbol": symbol, "limit": str(limit)
        })
        return r.get("result", {})
    except Exception as e:
        print(f"Orderbook error {symbol}: {e}")
        return {}

async def get_recent_trades(symbol: str, limit: int = 200) -> list:
    try:
        r = await bybit_get("/v5/market/recent-trade", {
            "category": "linear", "symbol": symbol, "limit": str(limit)
        })
        return r.get("result", {}).get("list", [])
    except Exception as e:
        print(f"Recent trades error {symbol}: {e}")
        return []

async def get_open_interest(symbol: str) -> dict:
    """Get OI for last 5 periods to calculate delta"""
    try:
        r = await bybit_get("/v5/market/open-interest", {
            "category": "linear", "symbol": symbol,
            "intervalTime": "5min", "limit": "5"
        })
        return r.get("result", {}).get("list", [])
    except Exception as e:
        print(f"OI error {symbol}: {e}")
        return []

async def get_tickers(symbol: str) -> dict:
    try:
        r = await bybit_get("/v5/market/tickers", {
            "category": "linear", "symbol": symbol
        })
        items = r.get("result", {}).get("list", [])
        return items[0] if items else {}
    except Exception as e:
        print(f"Ticker error {symbol}: {e}")
        return {}

async def place_order(symbol: str, side: str, qty: float,
                      sl: float, tp1: float) -> dict:
    body = {
        "category": "linear", "symbol": symbol,
        "side": "Buy" if side == "long" else "Sell",
        "orderType": "Market", "qty": str(qty),
        "stopLoss": str(sl), "takeProfit": str(tp1),
        "tpslMode": "Full", "leverage": str(LEVERAGE),
        "positionIdx": "0",
    }
    return await bybit_post("/v5/order/create", body)

async def close_position(symbol: str, qty: float, side: str) -> dict:
    close_side = "Sell" if side == "long" else "Buy"
    body = {
        "category": "linear", "symbol": symbol,
        "side": close_side, "orderType": "Market",
        "qty": str(qty), "reduceOnly": True,
        "positionIdx": "0",
    }
    return await bybit_post("/v5/order/create", body)

async def set_sl(symbol: str, sl_price: float, side: str) -> dict:
    body = {
        "category": "linear", "symbol": symbol,
        "stopLoss": str(sl_price),
        "positionIdx": "0",
    }
    return await bybit_post("/v5/position/trading-stop", body)

# ═══════════════════════════════════════════════════════════════
#  LAYER 1 — TREND GATE
#  All 3 must agree with trade direction or signal is BLOCKED
# ═══════════════════════════════════════════════════════════════

def calc_atr(candles: list, period: int = 14) -> float:
    """ATR from OHLCV candles"""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2])
        l = float(candles[i][3])
        pc = float(candles[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

def calc_adx(candles: list, period: int = 14) -> float:
    """ADX — measures trend strength regardless of direction"""
    if len(candles) < period * 2:
        return 0.0
    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, len(candles)):
        h  = float(candles[i][2]);   ph = float(candles[i-1][2])
        l  = float(candles[i][3]);   pl = float(candles[i-1][3])
        pc = float(candles[i-1][4])
        plus_dm  = max(h - ph, 0) if (h - ph) > (pl - l) else 0
        minus_dm = max(pl - l, 0) if (pl - l) > (h - ph) else 0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        plus_dms.append(plus_dm); minus_dms.append(minus_dm); trs.append(tr)

    def smooth(vals, p):
        s = sum(vals[:p])
        result = [s]
        for v in vals[p:]:
            s = s - s/p + v
            result.append(s)
        return result

    s_tr   = smooth(trs,       period)
    s_plus = smooth(plus_dms,  period)
    s_min  = smooth(minus_dms, period)

    dx_vals = []
    for i in range(len(s_tr)):
        if s_tr[i] == 0:
            continue
        di_plus  = 100 * s_plus[i] / s_tr[i]
        di_minus = 100 * s_min[i]  / s_tr[i]
        dsum = di_plus + di_minus
        if dsum == 0:
            continue
        dx_vals.append(100 * abs(di_plus - di_minus) / dsum)

    if not dx_vals:
        return 0.0
    return sum(dx_vals[-period:]) / min(len(dx_vals), period)

def calc_ut_bot(candles: list, atr_mult: float = 1.0, period: int = 10) -> str:
    """
    UT Bot — ATR trailing stop that flips direction.
    Returns 'long', 'short', or 'neutral'
    Uses 15m candles for trend direction.
    """
    if len(candles) < period + 5:
        return "neutral"

    atr = calc_atr(candles, period)
    if atr == 0:
        return "neutral"

    trail = float(candles[-2][4])  # init
    direction = "neutral"

    for i in range(1, len(candles)):
        close = float(candles[i][4])
        prev_close = float(candles[i-1][4])

        stop_dist = atr_mult * atr
        if close > trail:
            new_trail = close - stop_dist
            trail = max(trail, new_trail)
        else:
            new_trail = close + stop_dist
            trail = min(trail, new_trail)

        if close > trail and prev_close <= trail:
            direction = "long"
        elif close < trail and prev_close >= trail:
            direction = "short"

    return direction

def calc_vwap(candles: list) -> float:
    """Session VWAP from candles"""
    total_pv = 0.0
    total_v  = 0.0
    for c in candles:
        h = float(c[2]); l = float(c[3]); cl = float(c[4]); v = float(c[5])
        typical = (h + l + cl) / 3
        total_pv += typical * v
        total_v  += v
    return total_pv / total_v if total_v > 0 else 0.0

async def check_trend_gate(symbol: str, direction: str) -> tuple[bool, dict]:
    """
    Layer 1 — Trend Gate
    Returns (passes: bool, details: dict)
    All 3 must agree with direction for gate to pass.
    """
    candles_15m = await get_klines(symbol, interval="15", limit=60)
    candles_5m  = await get_klines(symbol, interval="5",  limit=30)

    if not candles_15m or not candles_5m:
        return False, {"reason": "no candle data"}

    # ① UT Bot on 15m
    ut_dir = calc_ut_bot(candles_15m)
    ut_pass = (ut_dir == direction) or (ut_dir == "neutral")

    # ② ADX > 25
    adx = calc_adx(candles_15m)
    adx_pass = adx >= 25

    # ③ Price vs VWAP
    vwap = calc_vwap(candles_5m)
    price = float(candles_5m[-1][4])
    if direction == "long":
        vwap_pass = price > vwap
    else:
        vwap_pass = price < vwap

    all_pass = ut_pass and adx_pass and vwap_pass

    details = {
        "ut_bot": ut_dir, "ut_pass": ut_pass,
        "adx": round(adx, 1), "adx_pass": adx_pass,
        "vwap": round(vwap, 4), "price": round(price, 4),
        "vwap_pass": vwap_pass,
        "passed": all_pass,
    }

    if not all_pass:
        fails = []
        if not ut_pass:  fails.append(f"UT Bot={ut_dir}")
        if not adx_pass: fails.append(f"ADX={adx:.1f}<25")
        if not vwap_pass: fails.append(f"Price {'below' if direction=='long' else 'above'} VWAP")
        details["reason"] = " | ".join(fails)

    return all_pass, details

# ═══════════════════════════════════════════════════════════════
#  LAYER 2 — ENTRY SCORE
#  OBI (35) + CVD/CVD-Div (35) + OI Delta (20) + bonus (10)
#  Minimum score: 65
# ═══════════════════════════════════════════════════════════════

async def get_obi(symbol: str) -> dict:
    """
    Order Book Imbalance
    OBI = bid_vol / (bid_vol + ask_vol) across top 50 levels
    Bid stacking: top 5 bid levels 1.5x larger than bottom 5
    """
    ob = await get_orderbook(symbol, limit=50)
    bids = ob.get("b", [])
    asks = ob.get("a", [])

    if not bids or not asks:
        return {"obi": 0.5, "bid_stack": False}

    bid_vol = sum(float(b[1]) for b in bids)
    ask_vol = sum(float(a[1]) for a in asks)
    total   = bid_vol + ask_vol
    obi     = bid_vol / total if total > 0 else 0.5

    # Bid stacking check
    bid_vols = [float(b[1]) for b in bids]
    top5_avg = sum(bid_vols[:5]) / 5 if len(bid_vols) >= 5 else 0
    bot5_avg = sum(bid_vols[-5:]) / 5 if len(bid_vols) >= 5 else 0
    bid_stack = top5_avg >= bot5_avg * 1.5

    return {"obi": round(obi, 4), "bid_stack": bid_stack}

async def get_cvd(symbol: str) -> dict:
    """
    Cumulative Volume Delta + CVD Divergence
    CVD = sum(buy_vol - sell_vol) across last 200 trades
    Divergence: price new high but CVD lower high = fake move
    Acceleration: last 50 trades more aggressive than first 50
    """
    trades = await get_recent_trades(symbol, limit=200)
    if not trades:
        return {"buy_ratio": 0.5, "cvd_delta": 0, "accel_buy": False, "cvd_diverge": False}

    buy_vol  = sum(float(t["size"]) for t in trades if t.get("side") == "Buy")
    sell_vol = sum(float(t["size"]) for t in trades if t.get("side") == "Sell")
    total_vol = buy_vol + sell_vol
    buy_ratio = buy_vol / total_vol if total_vol > 0 else 0.5
    cvd_delta = buy_vol - sell_vol

    # Acceleration: last 50 vs first 50
    first50 = trades[-50:] if len(trades) >= 100 else trades[:len(trades)//2]
    last50  = trades[:50]  if len(trades) >= 100 else trades[len(trades)//2:]
    first_buy = sum(float(t["size"]) for t in first50 if t.get("side") == "Buy")
    last_buy  = sum(float(t["size"]) for t in last50  if t.get("side") == "Buy")
    accel_buy = last_buy > first_buy * 1.2

    # CVD divergence: compare early vs late CVD slope vs price
    first_cvd = first_buy - sum(float(t["size"]) for t in first50 if t.get("side") == "Sell")
    last_cvd  = last_buy  - sum(float(t["size"]) for t in last50  if t.get("side") == "Sell")
    first_price = float(first50[-1]["price"]) if first50 else 0
    last_price  = float(last50[0]["price"])   if last50  else 0

    # Divergence: price up but CVD down (or vice versa) = fake move
    cvd_diverge = False
    if first_price > 0 and last_price > 0:
        price_up = last_price > first_price
        cvd_up   = last_cvd  > first_cvd
        cvd_diverge = (price_up != cvd_up)  # they disagree = divergence

    return {
        "buy_ratio":  round(buy_ratio, 4),
        "cvd_delta":  round(cvd_delta, 2),
        "accel_buy":  accel_buy,
        "cvd_diverge": cvd_diverge,
    }

async def get_oi_delta(symbol: str) -> dict:
    """
    Open Interest Delta — replaces Funding Rate
    Rising OI + price rising = real buyers entering (LONG signal)
    Falling OI + price rising = short covering only (weaker signal)
    Rising OI + price falling = real sellers entering (SHORT signal)
    """
    oi_list = await get_open_interest(symbol)
    ticker  = await get_tickers(symbol)

    if not oi_list or len(oi_list) < 2:
        return {"oi_signal": "neutral", "oi_delta_pct": 0.0}

    try:
        oi_now  = float(oi_list[0]["openInterest"])
        oi_prev = float(oi_list[-1]["openInterest"])
        oi_delta_pct = (oi_now - oi_prev) / oi_prev * 100 if oi_prev > 0 else 0

        price_now  = float(ticker.get("lastPrice", 0))
        price_mark = float(ticker.get("markPrice", price_now))

        # OI rising = new money entering
        oi_rising = oi_delta_pct > 0.3    # >0.3% increase
        oi_falling = oi_delta_pct < -0.3  # >0.3% decrease

        if oi_rising:
            oi_signal = "long"    # new longs entering
        elif oi_falling:
            oi_signal = "short"   # longs exiting /
        return {"oi_signal": oi_signal, "oi_delta_pct": round(oi_delta_pct, 3)}
    except Exception as e:
        print(f"OI delta error: {e}")
        return {"oi_signal": "neutral", "oi_delta_pct": 0.0}

def calc_score(direction: str, obi_data: dict, cvd_data: dict, oi_data: dict) -> tuple:
    """
    Score = OBI (35) + CVD (35) + OI Delta (20) + all-agree bonus (10)
    Minimum to trade: 75
    """
    score = 0

    obi        = obi_data.get("obi", 0.5)
    bid_stack  = obi_data.get("bid_stack", False)
    buy_ratio  = cvd_data.get("buy_ratio", 0.5)
    accel_buy  = cvd_data.get("accel_buy", False)
    cvd_diverge = cvd_data.get("cvd_diverge", False)
    oi_signal  = oi_data.get("oi_signal", "neutral")

    # ① OBI — 35 pts
    if direction == "long":
        obi_dist = max(0, obi - 0.5) * 2
    else:
        obi_dist = max(0, 0.5 - obi) * 2
    score += min(obi_dist * 35, 35)
    if bid_stack and direction == "long":  score += 5
    if bid_stack and direction == "short": score -= 5

    # ② CVD — 35 pts
    if direction == "long":
        flow_dist = max(0, buy_ratio - 0.5) * 2
    else:
        flow_dist = max(0, 0.5 - buy_ratio) * 2
    score += min(flow_dist * 35, 35)
    if accel_buy and direction == "long": score += 5

    # CVD divergence penalty — fake move detected
    if cvd_diverge: score -= 10

    # ③ OI Delta — 20 pts (replaces funding rate)
    if oi_signal == direction:   score += 20
    elif oi_signal == "neutral": score += 5

    # All 3 agree bonus — +10 pts
    obi_vote = "long" if obi >= 0.6 else ("short" if obi <= 0.4 else "neutral")
    cvd_vote = "long" if buy_ratio >= 0.6 else ("short" if buy_ratio <= 0.4 else "neutral")
    oi_vote  = oi_signal
    votes    = sum(1 for v in [obi_vote, cvd_vote, oi_vote] if v == direction)
    if votes == 3: score += 10

    score = max(0, min(100, int(score)))

    if   score >= 80: label = "VERY STRONG"
    elif score >= 75: label = "STRONG"
    elif score >= 40: label = "MEDIUM"
    else:             label = "WEAK"

    return score, label, votes

# ═══════════════════════════════════════════════════════════════
#  TP / SL / POSITION SIZING
# ═══════════════════════════════════════════════════════════════

def get_tp_sl(score: int, entry: float, direction: str) -> dict:
    """
    Score-driven TP/SL targets.
    STRONG  75-79: TP1 +7%  TP2 +18%  TP3 +30%  SL -3.5%
    V.STRONG 80+:  TP1 +10% TP2 +25%  TP3 +40%  SL -4%
    """
    if score >= 80:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 10.0, 25.0, 40.0, 4.0
    else:  # 75-79 STRONG
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 7.0, 18.0, 30.0, 3.5

    m = 1 if direction == "long" else -1
    return {
        "tp1": round(entry * (1 + m * tp1_pct / 100), 6),
        "tp2": round(entry * (1 + m * tp2_pct / 100), 6),
        "tp3": round(entry * (1 + m * tp3_pct / 100), 6),
        "sl":  round(entry * (1 - m * sl_pct  / 100), 6),
        "tp1_pct": tp1_pct, "tp2_pct": tp2_pct,
        "tp3_pct": tp3_pct, "sl_pct": sl_pct,
    }

def get_position_size(score: int, balance: float) -> float:
    """Score-driven sizing. Hard cap 30% of balance."""
    if   score >= 80: pct = 25.0
    else:             pct = 20.0   # 75-79
    size = balance * pct / 100
    return min(size, balance * 0.30)

# ═══════════════════════════════════════════════════════════════
#  LAYER 3 — MOMENTUM EXHAUSTION EXIT
#  Jasleen's eye coded: shrinking candles + CVD flattening
# ═══════════════════════════════════════════════════════════════

async def check_momentum_exhaustion(symbol: str, direction: str, entry: float, current_price: float) -> bool:
    """
    Returns True if momentum is dying and we should exit early.
    Only triggers if already >2% in profit.

    Conditions (all must be true):
    1. Already > 2% profit
    2. Last 3 candle bodies are shrinking
    3. Current body < 40% of 10-candle average body size
    4. CVD slope is flattening or reversing
    """
    # Gate: only if >2% in profit
    if direction == "long":
        profit_pct = (current_price - entry) / entry * 100
    else:
        profit_pct = (entry - current_price) / entry * 100

    if profit_pct < 2.0:
        return False

    # Fetch recent 5m candles
    candles = await get_klines(symbol, interval="5", limit=15)
    if len(candles) < 10:
        return False

    # Body sizes (absolute)
    def body(c): return abs(float(c[4]) - float(c[1]))

    bodies = [body(c) for c in candles]

    # Check last 3 bodies are shrinking
    last3  = bodies[-3:]
    shrinking = last3[0] > last3[1] > last3[2]

    # Current body vs 10-candle average
    avg_body = sum(bodies[-10:]) / 10
    tiny_body = bodies[-1] < avg_body * 0.40 if avg_body > 0 else False

    # CVD slope check — is buying pressure dying?
    cvd = await get_cvd(symbol)
    buy_ratio   = cvd.get("buy_ratio", 0.5)
    accel_buy   = cvd.get("accel_buy", False)

    if direction == "long":
        # For longs: CVD flattening = buy_ratio dropping toward neutral
        cvd_dying = buy_ratio < 0.55 and not accel_buy
    else:
        # For shorts: selling pressure dying
        cvd_dying = buy_ratio > 0.45 and not accel_buy

    result = shrinking and tiny_body and cvd_dying

    if result:
        print(f"⚡ Momentum exhaustion detected on {symbol} | "
              f"profit={profit_pct:.1f}% | bodies={[round(b,4) for b in last3]} | "
              f"avg_body={avg_body:.4f} | buy_ratio={buy_ratio:.3f}")

    return result

# ═══════════════════════════════════════════════════════════════
#  CVD STRUCTURE BREAK EXIT
# ═══════════════════════════════════════════════════════════════

async def check_structure_break(symbol: str, direction: str, entry: float, current_price: float) -> bool:
    """
    Hard exit if CVD divergence confirms reversal.
    Only triggers if already >1% in profit (don't exit too early).
    """
    if direction == "long":
        profit_pct = (current_price - entry) / entry * 100
    else:
        profit_pct = (entry - current_price) / entry * 100

    if profit_pct < 1.0:
        return False

    cvd = await get_cvd(symbol)
    diverge = cvd.get("cvd_diverge", False)
    buy_ratio = cvd.get("buy_ratio", 0.5)

    if direction == "long" and diverge and buy_ratio < 0.45:
        print(f"🔴 Structure break on {symbol} LONG | CVD diverged + buy_ratio={buy_ratio:.3f}")
        return True
    if direction == "short" and diverge and buy_ratio > 0.55:
        print(f"🔴 Structure break on {symbol} SHORT | CVD diverged + buy_ratio={buy_ratio:.3f}")
        return True

    return False

# ═══════════════════════════════════════════════════════════════
#  MAIN SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT",
    "ADAUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT",
]

async def scan_symbol(symbol: str) -> dict | None:
    """
    Full 3-layer analysis on one symbol.
    Returns signal dict if all layers pass, None otherwise.
    """
    ticker = await get_tickers(symbol)
    if not ticker:
        return None

    price = float(ticker.get("lastPrice", 0))
    if price <= 0:
        return None

    # Quick pre-filter: check OBI to determine candidate direction
    obi_data = await get_obi(symbol)
    obi = obi_data.get("obi", 0.5)

    if obi > 0.58:
        direction = "long"
    elif obi < 0.42:
        direction = "short"
    else:
        return None  # too neutral, skip

    # ── Layer 1: Trend Gate ──
    trend_ok, trend_details = await check_trend_gate(symbol, direction)
    if not trend_ok:
        state["trend_blocks"] += 1
        print(f"🚫 {symbol} {direction.upper()} BLOCKED by trend gate: {trend_details.get('reason','')}")
        return None

    # ── Layer 2: Score ──
    cvd_data = await get_cvd(symbol)
    oi_data  = await get_oi_delta(symbol)
    score, label, votes = calc_score(direction, obi_data, cvd_data, oi_data)

    if score < MIN_SCORE:
        state["score_blocks"] += 1
        print(f"📊 {symbol} score={score} < {MIN_SCORE} — skipped")
        return None

    # ── All layers passed ──
    tp_sl = get_tp_sl(score, price, direction)

    print(f"✅ SIGNAL: {symbol} {direction.upper()} | score={score} {label} | "
          f"OBI={obi:.3f} CVD={cvd_data['buy_ratio']:.3f} OI={oi_data['oi_signal']} | "
          f"ADX={trend_details['adx']} UTBot={trend_details['ut_bot']}")

    return {
        "symbol": symbol, "direction": direction,
        "price": price, "score": score, "label": label,
        "votes": votes, "tp_sl": tp_sl,
        "obi": obi_data, "cvd": cvd_data, "oi": oi_data,
        "trend": trend_details,
        "timestamp": int(time.time()),
    }

# ═══════════════════════════════════════════════════════════════
#  POSITION MONITOR — runs every 5 seconds when in trade
# ═══════════════════════════════════════════════════════════════

async def monitor_position():
    """
    Watches open position for:
    - TP1/TP2/TP3 hits
    - Momentum exhaustion exit
    - CVD structure break exit
    - Breakeven SL after TP1
    """
    pos = state.get("position")
    if not pos:
        return

    symbol    = pos["symbol"]
    direction = pos["direction"]
    entry     = pos["entry"]
    tp_sl     = pos["tp_sl"]
    qty       = pos["qty"]
    tp1_hit   = pos.get("tp1_hit", False)
    tp2_hit   = pos.get("tp2_hit", False)

    ticker = await get_tickers(symbol)
    if not ticker:
        return

    price = float(ticker.get("lastPrice", 0))
    if price <= 0:
        return

    pos["current_price"] = price

    # P&L calculation
    if direction == "long":
        unrealized_pct = (price - entry) / entry * 100
    else:
        unrealized_pct = (entry - price) / entry * 100
    pos["unrealized_pct"] = round(unrealized_pct, 2)

    tp1 = tp_sl["tp1"]; tp2 = tp_sl["tp2"]; tp3 = tp_sl["tp3"]

    # ── TP3 hit — full close ──
    if not tp2_hit and ((direction == "long" and price >= tp3) or
                        (direction == "short" and price <= tp3)):
        print(f"🎯 TP3 hit {symbol} @ {price}")
        await close_position(symbol, qty * 0.20, direction)
        await record_close(symbol, price, "win", "TP3")
        return

    # ── TP2 hit — close 40% more ──
    if not tp2_hit and ((direction == "long" and price >= tp2) or
                        (direction == "short" and price <= tp2)):
        print(f"🎯 TP2 hit {symbol} @ {price}")
        await close_position(symbol, qty * 0.40, direction)
        pos["tp2_hit"] = True
        pos["qty"] = qty * 0.20  # only 20% remaining

    # ── TP1 hit — close 40% + move SL to breakeven ──
    if not tp1_hit and ((direction == "long" and price >= tp1) or
                        (direction == "short" and price <= tp1)):
        print(f"🎯 TP1 hit {symbol} @ {price} — moving SL to breakeven")
        await close_position(symbol, qty * 0.40, direction)
        pos["tp1_hit"] = True
        pos["qty"] = qty * 0.60  # 60% remaining

        # Move SL to breakeven + 0.1% buffer
        be_sl = entry * 1.001 if direction == "long" else entry * 0.999
        pos["tp_sl"]["sl"] = round(be_sl, 6)
        await set_sl(symbol, be_sl, direction)
        print(f"🔒 SL moved to breakeven {be_sl:.4f}")
        return

    # ── Momentum exhaustion exit ──
    if await check_momentum_exhaustion(symbol, direction, entry, price):
        print(f"⚡ Momentum exit {symbol} @ {price}")
        await close_position(symbol, pos["qty"], direction)
        await record_close(symbol, price, "win", "momentum_exit")
        return

    # ── CVD structure break exit ──
    if await check_structure_break(symbol, direction, entry, price):
        print(f"🔴 Structure break exit {symbol} @ {price}")
        await close_position(symbol, pos["qty"], direction)
        await record_close(symbol, price, "win", "structure_break")
        return

async def record_close(symbol: str, exit_price: float, result: str, reason: str):
    """Record trade close to state + Supabase"""
    pos = state.get("position")
    if not pos:
        return

    entry = pos["entry"]
    direction = pos["direction"]
    size  = pos.get("size_usdt", 0)

    if direction == "long":
        pnl = (exit_price - entry) / entry * size * LEVERAGE
    else:
        pnl = (entry - exit_price) / entry * size * LEVERAGE

    pnl = round(pnl, 4)

    trade = {
        "symbol": symbol, "direction": direction,
        "result": result, "entry": entry,
        "exit_price": exit_price, "pnl": pnl,
        "score": pos.get("score", 0),
        "label": pos.get("label", ""),
        "reason": reason,
        "tp1_hit": pos.get("tp1_hit", False),
        "tp2_hit": pos.get("tp2_hit", False),
        "timestamp": int(time.time()),
    }

    state["trades"].insert(0, trade)
    state["trades"] = state["trades"][:100]
    state["total_pnl"] = round(state["total_pnl"] + pnl, 4)

    if result == "win":
        state["wins"] += 1
    else:
        state["losses"] += 1

    state["position"] = None
    print(f"{'✅ WIN' if result=='win' else '❌ LOSS'} {symbol} | PnL={pnl:+.4f} | reason={reason}")

    # Save to Supabase
    await save_to_supabase(trade)

async def save_to_supabase(trade: dict):
    if not SB_URL or not SB_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"{SB_URL}/rest/v1/trades",
                headers={
                    "apikey": SB_KEY,
                    "Authorization": f"Bearer {SB_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={
                    "symbol":     trade["symbol"],
                    "direction":  trade["direction"],
                    "result":     trade["result"],
                    "entry":      trade["entry"],
                    "exit_price": trade["exit_price"],
                    "pnl":        trade["pnl"],
                    "score":      trade["score"],
                    "label":      trade["label"],
                    "tp1_hit":    trade["tp1_hit"],
                    "tp2_hit":    trade["tp2_hit"],
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
    except Exception as e:
        print(f"Supabase save error: {e}")

# ═══════════════════════════════════════════════════════════════
#  MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════

async def bot_loop():
    print("🚀 APEX Pro v2.0 starting...")
    await asyncio.sleep(3)

    while True:
        try:
            state["balance"] = await get_balance()
            state["last_scan"] = int(time.time())

            # Monitor open position every loop
            if state.get("position"):
                await monitor_position()
                await asyncio.sleep(5)
                continue

            # Scan for new signals
            state["status"] = "scanning"
            print(f"🔍 Scanning {len(WATCHLIST)} symbols | Balance: ${state['balance']:.2f}")

            for symbol in WATCHLIST:
                if state.get("position"):
                    break  # stop scanning if we entered a trade

                signal = await scan_symbol(symbol)
                if not signal:
                    await asyncio.sleep(1)
                    continue

                # Enter trade
                balance = state["balance"]
                size    = get_position_size(signal["score"], balance)
                price   = signal["price"]
                qty     = round((size * LEVERAGE) / price, 3)

                if qty <= 0 or size < 5:
                    print(f"⚠️ Size too small: ${size:.2f} — skip")
                    continue

                tp_sl = signal["tp_sl"]
                resp  = await place_order(symbol, signal["direction"], qty,
                                          tp_sl["sl"], tp_sl["tp1"])

                if resp and resp.get("retCode") == 0:
                    state["position"] = {
                        "symbol":    symbol,
                        "direction": signal["direction"],
                        "entry":     price,
                        "qty":       qty,
                        "size_usdt": size,
                        "score":     signal["score"],
                        "label":     signal["label"],
                        "tp_sl":     tp_sl,
                        "tp1_hit":   False,
                        "tp2_hit":   False,
                        "opened_at": int(time.time()),
                    }
                    state["status"] = "in_trade"
                    print(f"📈 ENTERED {symbol} {signal['direction'].upper()} | "
                          f"qty={qty} size=${size:.2f} score={signal['score']}")
                else:
                    print(f"⚠️ Order failed: {resp}")

                break  # one trade at a time

            state["status"] = "idle"
            await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"Bot loop error: {e}")
            state["status"] = "error"
            await asyncio.sleep(10)

# ═══════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/state")
async def api_state():
    pos = state.get("position")
    wins   = state["wins"]
    losses = state["losses"]
    total  = wins + losses
    return JSONResponse({
        "balance":      round(state["balance"], 2),
        "total_pnl":    state["total_pnl"],
        "wins":         wins,
        "losses":       losses,
        "win_rate":     round(wins / total * 100, 1) if total > 0 else 0,
        "trades":       state["trades"][:20],
        "position":     pos,
        "status":       state["status"],
        "last_scan":    state["last_scan"],
        "trend_blocks": state["trend_blocks"],
        "score_blocks": state["score_blocks"],
        "min_score":    MIN_SCORE,
    })

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(UI_HTML)

@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_loop())

# ═══════════════════════════════════════════════════════════════
#  DASHBOARD UI
# ═══════════════════════════════════════════════════════════════

UI_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX Pro v2</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0a0f;--card:#14141e;--border:#1e1e2e;--accent:#f0c040;--green:#3dffa0;--red:#ff4d6d;--text:#e8e8f0;--muted:#6b6b80}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;padding:20px 16px 60px;max-width:480px;margin:0 auto}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border)}
.logo{font-family:'Space Mono',monospace;font-size:18px;font-weight:700}.logo span{color:var(--accent)}
.badge{display:flex;align-items:center;gap:6px;background:rgba(61,255,160,.08);border:1px solid rgba(61,255,160,.2);border-radius:20px;padding:4px 12px;font-size:11px;font-family:'Space Mono',monospace;color:var(--green)}
.dot{width:6px;height:6px;background:var(--green);border-radius:50%;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
.card-lbl{font-size:10px;color:var(--muted);font-family:'Space Mono',monospace;letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px}
.card-val{font-family:'Space Mono',monospace;font-size:22px;font-weight:700}
.card-sub{font-size:11px;color:var(--muted);margin-top:3px}
.green{color:var(--green)}.red{color:var(--red)}.amber{color:var(--accent)}
.sec{font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;margin:20px 0 10px;display:flex;align-items:center;gap:8px}
.sec::after{content:'';flex:1;height:1px;background:var(--border)}
.pos-card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:16px}
.pos-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.pos-sym{font-family:'Space Mono',monospace;font-size:16px;font-weight:700}
.pos-dir{font-size:11px;font-family:'Space Mono',monospace;padding:3px 10px;border-radius:4px}
.long-bg{background:rgba(61,255,160,.12);color:var(--green)}
.short-bg{background:rgba(255,77,109,.12);color:var(--red)}
.tp-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.tp-chip{font-size:10px;font-family:'Space Mono',monospace;padding:3px 9px;border-radius:12px;background:rgba(255,255,255,.06);color:var(--muted)}
.tp-chip.hit{background:rgba(61,255,160,.15);color:var(--green)}
.layers{background:rgba(255,255,255,.03);border-radius:10px;padding:12px;margin-top:10px;font-size:12px}
.layer-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.layer-row:last-child{border:none}
.trade-row{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.trade-sym{font-family:'Space Mono',monospace;font-size:12px;font-weight:700}
.trade-score{font-size:10px;color:var(--muted);font-family:'Space Mono',monospace}
.trade-pnl{font-family:'Space Mono',monospace;font-size:13px;font-weight:700}
.filter-bar{background:rgba(240,192,64,.06);border:1px solid rgba(240,192,64,.2);border-radius:10px;padding:12px 14px;margin-bottom:16px;font-size:12px;color:#bbb}
.filter-bar b{color:var(--accent)}
</style>
</head>
<body>
<div class="header">
  <div class="logo">APEX <span>Pro</span> <span style="font-size:11px;color:var(--muted)">v2.0</span></div>
  <div class="badge"><span class="dot"></span> LIVE</div>
</div>

<div class="filter-bar" id="filter-bar">
  Loading filters...
</div>

<div class="grid2" id="summary"></div>

<div id="position-section"></div>

<div class="sec">All Trades</div>
<div class="card" id="trade-log"><div style="color:var(--muted);font-size:13px;text-align:center;padding:20px">No trades yet</div></div>

<script>
async function refresh(){
  try{
    const d = await fetch('/api/state').then(r=>r.json())
    renderSummary(d)
    renderFilterBar(d)
    renderPosition(d.position)
    renderTrades(d.trades)
  }catch(e){console.error(e)}
}

function renderFilterBar(d){
  const el = document.getElementById('filter-bar')
  el.innerHTML = `Min score: <b>${d.min_score}/100</b> &nbsp;|&nbsp; `+
    `Trend blocks: <b>${d.trend_blocks}</b> &nbsp;|&nbsp; `+
    `Score blocks: <b>${d.score_blocks}</b>`
}

function renderSummary(d){
  const wr = d.win_rate
  const pnl = d.total_pnl
  document.getElementById('summary').innerHTML = `
    <div class="card">
      <div class="card-lbl">Total P&L</div>
      <div class="card-val ${pnl>=0?'green':'red'}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</div>
      <div class="card-sub">Balance: $${d.balance.toFixed(2)}</div>
    </div>
    <div class="card">
      <div class="card-lbl">Win Rate</div>
      <div class="card-val amber">${wr}%</div>
      <div class="card-sub">${d.wins}W / ${d.losses}L</div>
    </div>`
}

function renderPosition(pos){
  const sec = document.getElementById('position-section')
  if(!pos){sec.innerHTML='';return}
  const dir = pos.direction
  const tp = pos.tp_sl
  const unreal = pos.unrealized_pct||0
  sec.innerHTML = `
    <div class="sec">Open Position</div>
    <div class="pos-card">
      <div class="pos-head">
        <div>
          <div class="pos-sym">${pos.symbol}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:3px">
            Entry: $${pos.entry} &nbsp;|&nbsp; Score: ${pos.score} ${pos.label}
          </div>
        </div>
        <div>
          <div class="pos-dir ${dir==='long'?'long-bg':'short-bg'}">${dir.toUpperCase()}</div>
          <div style="font-family:'Space Mono',monospace;font-size:14px;font-weight:700;text-align:right;margin-top:6px;color:${unreal>=0?'var(--green)':'var(--red)'}">
            ${unreal>=0?'+':''}${unreal.toFixed(2)}%
          </div>
        </div>
      </div>
      <div class="tp-row">
        <span class="tp-chip ${pos.tp1_hit?'hit':''}">TP1 ${tp.tp1_pct}%</span>
        <span class="tp-chip ${pos.tp2_hit?'hit':''}">TP2 ${tp.tp2_pct}%</span>
        <span class="tp-chip">TP3 ${tp.tp3_pct}%</span>
        <span class="tp-chip" style="color:var(--red)">SL ${tp.sl_pct}%</span>
      </div>
      <div class="layers">
        <div class="layer-row"><span style="color:var(--muted)">UT Bot</span><span>${pos.trend?.ut_bot||'—'}</span></div>
        <div class="layer-row"><span style="color:var(--muted)">ADX</span><span>${pos.trend?.adx||'—'}</span></div>
        <div class="layer-row"><span style="color:var(--muted)">vs VWAP</span><span>${pos.trend?.vwap_pass?'✓ Pass':'✗ Fail'}</span></div>
      </div>
    </div>`
}

function renderTrades(trades){
  if(!trades||!trades.length) return
  const el = document.getElementById('trade-log')
  el.innerHTML = trades.map(t=>{
    const isWin = t.result==='win'
    return `<div class="trade-row">
      <div>
        <div class="trade-sym">${t.symbol}</div>
        <div class="trade-score">${t.score} ${t.label} &nbsp;·&nbsp; ${t.reason||''}</div>
      </div>
      <div class="trade-pnl ${isWin?'green':'red'}">${isWin?'+':''}$${(t.pnl||0).toFixed(2)}</div>
    </div>`
  }).join('')
}

refresh()
setInterval(refresh, 5000)
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
