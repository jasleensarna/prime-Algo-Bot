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
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>APEX Pro v2 — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#080810;
  --s1:#0f0f1a;
  --s2:#171724;
  --border:#22223a;
  --border2:#2e2e4a;
  --gold:#e8b94a;
  --gold2:#f5d27a;
  --glow:rgba(232,185,74,.12);
  --green:#39e8a0;
  --green-bg:rgba(57,232,160,.08);
  --red:#f04060;
  --red-bg:rgba(240,64,96,.08);
  --blue:#6eb4ff;
  --muted:#5a5a7a;
  --muted2:#3a3a5a;
  --text:#ddddf0;
  --text2:#9898b8;
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;overflow-x:hidden}

/* ── HEADER ── */
.header{
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 20px;border-bottom:1px solid var(--border);
  background:var(--s1);position:sticky;top:0;z-index:100;
}
.logo{font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:600;letter-spacing:.02em}
.logo-apex{color:var(--text)}
.logo-pro{color:var(--gold)}
.logo-v{font-size:10px;color:var(--muted);margin-left:4px}
.status-pill{
  display:flex;align-items:center;gap:7px;
  background:var(--s2);border:1px solid var(--border);
  border-radius:20px;padding:5px 12px 5px 10px;
  font-family:'IBM Plex Mono',monospace;font-size:10px;
}
.status-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.dot-scanning{background:var(--gold);animation:dotpulse 1.2s ease infinite}
.dot-in_trade{background:var(--green);animation:dotpulse .8s ease infinite}
.dot-idle{background:var(--blue)}
.dot-error{background:var(--red)}
.dot-starting{background:var(--muted)}
@keyframes dotpulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}
.status-txt{color:var(--text2);text-transform:uppercase;letter-spacing:.08em}

/* ── MAIN ── */
.main{padding:16px 16px 80px;max-width:520px;margin:0 auto}

/* ── SECTION LABEL ── */
.section-label{
  font-family:'IBM Plex Mono',monospace;font-size:9px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--muted);margin:20px 0 10px;
  display:flex;align-items:center;gap:8px;
}
.section-label::after{content:'';flex:1;height:1px;background:var(--border)}

/* ── STAT GRID ── */
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
.stat-card{
  background:var(--s1);border:1px solid var(--border);
  border-radius:12px;padding:14px 16px;position:relative;overflow:hidden;
}
.stat-card::before{
  content:'';position:absolute;inset:0;
  background:var(--glow);opacity:0;transition:opacity .3s;border-radius:12px;
}
.stat-label{font-family:'IBM Plex Mono',monospace;font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-val{font-family:'IBM Plex Mono',monospace;font-size:24px;font-weight:600;line-height:1}
.stat-sub{font-size:11px;color:var(--muted2);margin-top:5px;font-family:'IBM Plex Mono',monospace}
.val-green{color:var(--green)}
.val-red{color:var(--red)}
.val-gold{color:var(--gold)}
.val-blue{color:var(--blue)}

/* Win rate arc */
.wr-wrap{display:flex;align-items:center;gap:12px}
.wr-arc-svg{flex-shrink:0}
.wr-arc-bg{stroke:var(--border);fill:none;stroke-width:4}
.wr-arc-fill{fill:none;stroke-width:4;stroke-linecap:round;transition:stroke-dasharray .6s ease}

/* ── FILTER BAR ── */
.filter-bar{
  background:var(--s1);border:1px solid var(--border);border-radius:10px;
  padding:11px 14px;font-family:'IBM Plex Mono',monospace;font-size:11px;
  display:flex;gap:16px;flex-wrap:wrap;align-items:center;
}
.fb-item{display:flex;flex-direction:column;gap:2px}
.fb-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
.fb-val{color:var(--text)}
.fb-val.accent{color:var(--gold)}

/* ── POSITION CARD ── */
.pos-card{
  background:var(--s1);border:1px solid var(--border2);
  border-radius:14px;overflow:hidden;margin-bottom:8px;
}
.pos-top{
  display:flex;align-items:flex-start;justify-content:space-between;
  padding:16px;border-bottom:1px solid var(--border);
}
.pos-sym{font-family:'IBM Plex Mono',monospace;font-size:18px;font-weight:600}
.pos-entry{font-size:11px;color:var(--muted2);margin-top:3px;font-family:'IBM Plex Mono',monospace}
.pos-right{text-align:right}
.dir-badge{
  display:inline-block;font-family:'IBM Plex Mono',monospace;
  font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  padding:3px 10px;border-radius:4px;margin-bottom:6px;
}
.dir-long{background:var(--green-bg);color:var(--green);border:1px solid rgba(57,232,160,.2)}
.dir-short{background:var(--red-bg);color:var(--red);border:1px solid rgba(240,64,96,.2)}
.pos-pnl{font-family:'IBM Plex Mono',monospace;font-size:18px;font-weight:600}

.pos-body{padding:14px 16px}

/* Score bar */
.score-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.score-bar-wrap{flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden}
.score-bar-fill{height:100%;border-radius:3px;transition:width .6s ease}
.score-bar-strong{background:linear-gradient(90deg,var(--gold),var(--gold2))}
.score-bar-vstrong{background:linear-gradient(90deg,var(--green),#60ffcc)}
.score-label{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600}

/* TP chips */
.tp-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tp-chip{
  font-family:'IBM Plex Mono',monospace;font-size:10px;
  padding:4px 10px;border-radius:6px;
  background:var(--s2);border:1px solid var(--border);color:var(--muted2);
  transition:all .3s;
}
.tp-chip.tp-hit{background:rgba(57,232,160,.1);border-color:rgba(57,232,160,.3);color:var(--green)}
.tp-chip.sl-chip{background:rgba(240,64,96,.06);border-color:rgba(240,64,96,.2);color:var(--red)}

/* Signal layers */
.layers{background:var(--bg);border-radius:8px;padding:10px 12px}
.layer-row{
  display:flex;justify-content:space-between;align-items:center;
  padding:5px 0;border-bottom:1px solid var(--border);
  font-family:'IBM Plex Mono',monospace;font-size:11px;
}
.layer-row:last-child{border:none}
.layer-key{color:var(--muted2)}
.layer-val{font-weight:600}
.pass{color:var(--green)}.fail{color:var(--red)}.neutral{color:var(--muted)}

/* ── NO POSITION ── */
.no-pos{
  background:var(--s1);border:1px solid var(--border);border-radius:12px;
  padding:24px;text-align:center;margin-bottom:8px;
}
.no-pos-icon{font-size:28px;margin-bottom:10px;opacity:.3}
.no-pos-txt{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--muted);letter-spacing:.05em}

/* ── TRADE LOG ── */
.trade-log{
  background:var(--s1);border:1px solid var(--border);border-radius:12px;overflow:hidden;
}
.trade-row{
  display:grid;grid-template-columns:auto 1fr auto;gap:10px 12px;
  align-items:center;padding:12px 14px;border-bottom:1px solid var(--border);
  transition:background .15s;
}
.trade-row:last-child{border:none}
.trade-row:hover{background:var(--s2)}
.trade-indicator{width:3px;height:36px;border-radius:2px;flex-shrink:0}
.trade-win-ind{background:var(--green)}
.trade-loss-ind{background:var(--red)}
.trade-sym{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600}
.trade-meta{font-size:10px;color:var(--muted2);font-family:'IBM Plex Mono',monospace;margin-top:2px}
.trade-pnl{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;text-align:right}
.trade-score-badge{
  font-family:'IBM Plex Mono',monospace;font-size:9px;padding:2px 7px;
  border-radius:4px;background:var(--s2);border:1px solid var(--border);
  color:var(--muted2);margin-left:6px;
}
.no-trades{padding:30px;text-align:center;font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--muted)}

/* ── SCAN FOOTER ── */
.scan-footer{
  position:fixed;bottom:0;left:0;right:0;
  background:var(--s1);border-top:1px solid var(--border);
  padding:10px 20px;display:flex;justify-content:space-between;align-items:center;
  font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);
  z-index:100;
}
.scan-dot{width:5px;height:5px;border-radius:50%;background:var(--green);margin-right:6px;display:inline-block;animation:dotpulse 2s ease infinite}

/* ── CONFIG MODAL ── */
.cfg-btn{
  background:none;border:1px solid var(--border);color:var(--muted);
  font-family:'IBM Plex Mono',monospace;font-size:10px;padding:4px 10px;
  border-radius:6px;cursor:pointer;transition:all .2s;
}
.cfg-btn:hover{border-color:var(--gold);color:var(--gold)}

.modal-overlay{
  display:none;position:fixed;inset:0;background:rgba(8,8,16,.85);
  z-index:200;align-items:center;justify-content:center;padding:20px;
}
.modal-overlay.open{display:flex}
.modal{
  background:var(--s1);border:1px solid var(--border2);border-radius:16px;
  padding:22px;width:100%;max-width:400px;
}
.modal-title{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;color:var(--gold);margin-bottom:16px}
.modal-label{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted2);text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px;margin-top:12px}
.modal-input{
  width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);
  font-family:'IBM Plex Mono',monospace;font-size:12px;padding:9px 12px;
  border-radius:8px;outline:none;
}
.modal-input:focus{border-color:var(--gold)}
.modal-row{display:flex;gap:8px;margin-top:16px}
.btn-save{
  flex:1;background:var(--gold);color:#080810;
  font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;
  padding:10px;border:none;border-radius:8px;cursor:pointer;
}
.btn-save:hover{background:var(--gold2)}
.btn-cancel{
  flex:1;background:none;color:var(--muted);
  font-family:'IBM Plex Mono',monospace;font-size:11px;
  padding:10px;border:1px solid var(--border);border-radius:8px;cursor:pointer;
}
.btn-cancel:hover{border-color:var(--muted);color:var(--text)}

/* ── ERROR STATE ── */
.error-banner{
  background:rgba(240,64,96,.08);border:1px solid rgba(240,64,96,.2);
  border-radius:10px;padding:12px 14px;margin-bottom:12px;
  font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--red);
  display:none;
}
.error-banner.show{display:block}
</style>
</head>
<body>

<!-- CONFIG MODAL -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-title">// Configure API Endpoint</div>
    <div class="modal-label">Railway / Server URL</div>
    <input class="modal-input" id="api-url-input" placeholder="https://your-app.up.railway.app" />
    <div style="font-size:10px;color:var(--muted);font-family:'IBM Plex Mono',monospace;margin-top:6px">
      Leave blank to use relative path (same server)
    </div>
    <div class="modal-row">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-save" onclick="saveUrl()">Connect</button>
    </div>
  </div>
</div>

<!-- HEADER -->
<div class="header">
  <div class="logo">
    <span class="logo-apex">APEX</span> <span class="logo-pro">Pro</span>
    <span class="logo-v">v2.0</span>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <div class="status-pill">
      <span class="status-dot" id="status-dot"></span>
      <span class="status-txt" id="status-txt">—</span>
    </div>
    <button class="cfg-btn" onclick="openModal()">⚙</button>
  </div>
</div>

<div class="main">

  <!-- ERROR BANNER -->
  <div class="error-banner" id="error-banner">
    Cannot reach API — check your server URL &nbsp;·&nbsp;
    <a href="#" onclick="openModal();return false" style="color:var(--gold)">Configure →</a>
  </div>

  <!-- STATS -->
  <div class="section-label">Performance</div>
  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">Total P&L</div>
      <div class="stat-val" id="stat-pnl">—</div>
      <div class="stat-sub" id="stat-bal">Balance: —</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win Rate</div>
      <div class="wr-wrap">
        <svg class="wr-arc-svg" width="44" height="44" viewBox="0 0 44 44">
          <circle class="wr-arc-bg" cx="22" cy="22" r="18"/>
          <circle id="wr-arc" class="wr-arc-fill" cx="22" cy="22" r="18"
            stroke-dasharray="0 113" stroke-dashoffset="28" stroke="var(--green)"/>
        </svg>
        <div>
          <div class="stat-val val-gold" id="stat-wr">0%</div>
          <div class="stat-sub" id="stat-wl">0W / 0L</div>
        </div>
      </div>
    </div>
  </div>

  <!-- FILTER BAR -->
  <div class="filter-bar">
    <div class="fb-item">
      <span class="fb-lbl">Min Score</span>
      <span class="fb-val accent" id="fb-minscore">—</span>
    </div>
    <div class="fb-item">
      <span class="fb-lbl">Trend Blocks</span>
      <span class="fb-val" id="fb-trend">—</span>
    </div>
    <div class="fb-item">
      <span class="fb-lbl">Score Blocks</span>
      <span class="fb-val" id="fb-score">—</span>
    </div>
    <div class="fb-item">
      <span class="fb-lbl">Watchlist</span>
      <span class="fb-val" id="fb-watch">15</span>
    </div>
  </div>

  <!-- OPEN POSITION -->
  <div class="section-label">Position</div>
  <div id="position-section"></div>

  <!-- TRADE LOG -->
  <div class="section-label">Trade History</div>
  <div class="trade-log" id="trade-log">
    <div class="no-trades">No trades recorded yet</div>
  </div>

</div>

<!-- FOOTER -->
<div class="scan-footer">
  <span><span class="scan-dot" id="scan-indicator"></span><span id="scan-time">—</span></span>
  <span id="scan-leverage" style="color:var(--muted)">2× leverage</span>
</div>

<script>
var API_BASE = localStorage.getItem('apex_url') || '';

function getApiUrl(path){
  return (API_BASE ? API_BASE.replace(/\/+$/,'') : '') + path;
}

function openModal(){
  document.getElementById('api-url-input').value = API_BASE;
  document.getElementById('modal').classList.add('open');
}
function closeModal(){
  document.getElementById('modal').classList.remove('open');
}
function saveUrl(){
  var v = document.getElementById('api-url-input').value.trim();
  API_BASE = v;
  localStorage.setItem('apex_url', v);
  closeModal();
  refresh();
}

function timeAgo(ts){
  if(!ts) return '—';
  var s = Math.floor(Date.now()/1000) - ts;
  if(s < 5) return 'just now';
  if(s < 60) return s+'s ago';
  if(s < 3600) return Math.floor(s/60)+'m ago';
  return Math.floor(s/3600)+'h ago';
}

function setStatus(st){
  var dot = document.getElementById('status-dot');
  var txt = document.getElementById('status-txt');
  var map = {scanning:'scanning',in_trade:'in trade',idle:'idle',error:'error',starting:'starting'};
  dot.className = 'status-dot dot-'+(st||'idle');
  txt.textContent = (map[st]||st||'—').toUpperCase();
}

function renderStats(d){
  var pnl = d.total_pnl||0;
  var el = document.getElementById('stat-pnl');
  el.textContent = (pnl>=0?'+$':'−$') + Math.abs(pnl).toFixed(2);
  el.className = 'stat-val '+(pnl>=0?'val-green':'val-red');
  document.getElementById('stat-bal').textContent = 'Balance: $'+(d.balance||0).toFixed(2);

  var wr = d.win_rate||0;
  document.getElementById('stat-wr').textContent = wr.toFixed(1)+'%';
  document.getElementById('stat-wl').textContent = (d.wins||0)+'W / '+(d.losses||0)+'L';

  // Arc: circumference = 2π×18 ≈ 113
  var circ = 113;
  var fill = (wr/100)*circ;
  var arc = document.getElementById('wr-arc');
  arc.style.strokeDasharray = fill+' '+circ;
  arc.style.stroke = wr>=60?'var(--green)':wr>=40?'var(--gold)':'var(--red)';
}

function renderFilterBar(d){
  document.getElementById('fb-minscore').textContent = d.min_score||'—';
  document.getElementById('fb-trend').textContent = d.trend_blocks||0;
  document.getElementById('fb-score').textContent = d.score_blocks||0;
}

function renderPosition(pos){
  var sec = document.getElementById('position-section');
  if(!pos){
    sec.innerHTML = '<div class="no-pos"><div class="no-pos-icon">○</div><div class="no-pos-txt">NO OPEN POSITION</div></div>';
    return;
  }

  var dir = pos.direction;
  var tp = pos.tp_sl||{};
  var unreal = pos.unrealized_pct||0;
  var score = pos.score||0;

  var scoreClass = score>=80?'score-bar-vstrong':'score-bar-strong';
  var scoreLabel = score>=80?'VERY STRONG':score>=75?'STRONG':'MEDIUM';

  // Layer data
  var trend = pos.trend||{};
  var obi_data = pos.obi||{};
  var cvd_data = pos.cvd||{};
  var oi_data = pos.oi||{};

  sec.innerHTML = '<div class="pos-card">'
    +'<div class="pos-top">'
      +'<div>'
        +'<div class="pos-sym">'+pos.symbol+'</div>'
        +'<div class="pos-entry">Entry $'+pos.entry+' &nbsp;·&nbsp; Qty '+pos.qty+'</div>'
      +'</div>'
      +'<div class="pos-right">'
        +'<div class="dir-badge dir-'+dir+'">'+dir.toUpperCase()+'</div>'
        +'<div class="pos-pnl '+(unreal>=0?'val-green':'val-red')+'">'+(unreal>=0?'+':'')+unreal.toFixed(2)+'%</div>'
        +'<div style="font-size:10px;color:var(--muted2);font-family:\'IBM Plex Mono\',monospace;text-align:right;margin-top:2px">Since: '+timeAgo(pos.opened_at)+'</div>'
      +'</div>'
    +'</div>'
    +'<div class="pos-body">'
      +'<div class="score-row">'
        +'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:var(--muted2);width:42px">SCORE</div>'
        +'<div class="score-bar-wrap"><div class="score-bar-fill '+scoreClass+'" style="width:'+(score)+'%"></div></div>'
        +'<div class="score-label '+(score>=80?'val-green':'val-gold')+'">'+score+'</div>'
        +'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:var(--muted2);margin-left:2px">'+scoreLabel+'</div>'
      +'</div>'
      +'<div class="tp-row">'
        +'<span class="tp-chip '+(pos.tp1_hit?'tp-hit':'')+'">TP1 +'+(tp.tp1_pct||'?')+'%</span>'
        +'<span class="tp-chip '+(pos.tp2_hit?'tp-hit':'')+'">TP2 +'+(tp.tp2_pct||'?')+'%</span>'
        +'<span class="tp-chip">TP3 +'+(tp.tp3_pct||'?')+'%</span>'
        +'<span class="tp-chip sl-chip">SL −'+(tp.sl_pct||'?')+'%</span>'
      +'</div>'
      +'<div class="layers">'
        +'<div class="layer-row"><span class="layer-key">UT Bot</span><span class="layer-val '+(trend.ut_pass?'pass':'fail')+'">'+(trend.ut_bot||'—')+'</span></div>'
        +'<div class="layer-row"><span class="layer-key">ADX</span><span class="layer-val '+(trend.adx_pass?'pass':'fail')+'">'+(trend.adx||'—')+'</span></div>'
        +'<div class="layer-row"><span class="layer-key">vs VWAP</span><span class="layer-val '+(trend.vwap_pass?'pass':'fail')+'">'+(trend.vwap_pass?'Above VWAP':'Below VWAP')+'</span></div>'
        +'<div class="layer-row"><span class="layer-key">OBI</span><span class="layer-val">'+((obi_data.obi||0)*100).toFixed(1)+'%</span></div>'
        +'<div class="layer-row"><span class="layer-key">CVD Buy Ratio</span><span class="layer-val">'+((cvd_data.buy_ratio||0)*100).toFixed(1)+'%</span></div>'
        +'<div class="layer-row"><span class="layer-key">OI Signal</span><span class="layer-val '+(oi_data.oi_signal===dir?'pass':oi_data.oi_signal==='neutral'?'neutral':'fail')+'">'+(oi_data.oi_signal||'—').toUpperCase()+'</span></div>'
      +'</div>'
    +'</div>'
  +'</div>';
}

function renderTrades(trades){
  var el = document.getElementById('trade-log');
  if(!trades||!trades.length){
    el.innerHTML = '<div class="no-trades">No trades recorded yet</div>';
    return;
  }
  el.innerHTML = trades.map(function(t){
    var isWin = t.result==='win';
    var pnl = t.pnl||0;
    var d = new Date(t.timestamp*1000);
    var dateStr = d.toLocaleDateString('en-IN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
    return '<div class="trade-row">'
      +'<div class="trade-indicator '+(isWin?'trade-win-ind':'trade-loss-ind')+'"></div>'
      +'<div>'
        +'<div style="display:flex;align-items:center">'
          +'<span class="trade-sym">'+t.symbol+'</span>'
          +'<span class="trade-score-badge">'+t.score+' pts</span>'
        +'</div>'
        +'<div class="trade-meta">'+(t.direction||'').toUpperCase()+' &nbsp;·&nbsp; '+(t.reason||'').replace(/_/g,' ')+' &nbsp;·&nbsp; '+dateStr+'</div>'
      +'</div>'
      +'<div>'
        +'<div class="trade-pnl '+(isWin?'val-green':'val-red')+'">'+(pnl>=0?'+':'')+pnl.toFixed(2)+'</div>'
        +'<div style="font-size:9px;color:var(--muted2);font-family:\'IBM Plex Mono\',monospace;text-align:right;margin-top:2px">'+(t.label||'')+'</div>'
      +'</div>'
    +'</div>';
  }).join('');
}

async function refresh(){
  try{
    var url = getApiUrl('/api/state');
    var resp = await fetch(url, {cache:'no-store'});
    if(!resp.ok) throw new Error('HTTP '+resp.status);
    var d = await resp.json();

    document.getElementById('error-banner').classList.remove('show');

    setStatus(d.status);
    renderStats(d);
    renderFilterBar(d);
    renderPosition(d.position);
    renderTrades(d.trades||[]);

    var scanEl = document.getElementById('scan-time');
    scanEl.textContent = 'Last scan: '+timeAgo(d.last_scan);

  }catch(e){
    console.error('Fetch failed:',e);
    document.getElementById('error-banner').classList.add('show');
    setStatus('error');
    document.getElementById('scan-time').textContent = 'Connection error';
    if(!API_BASE) openModal();
  }
}

// Check if first time (no URL configured and not same-origin server)
var firstLoad = !localStorage.getItem('apex_url_set');
if(firstLoad && !window.location.pathname.startsWith('/api')){
  // Try relative first; if it fails modal opens automatically
  localStorage.setItem('apex_url_set','1');
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
