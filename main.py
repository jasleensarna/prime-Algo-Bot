"""
APEX Pro Bot — v3.0
═══════════════════════════════════════════════════════════════
CHANGES FROM v2:

SCORING — 3 indicators:
  ① OBI              — 35 pts  (bid/ask imbalance top 50 levels)
                                +5 bonus if bids stacking near best price
  ② Trade Flow       — 35 pts  (last 200 trades buy vs sell aggression)
                                +5 bonus if last 50 more aggressive than first 50
  ③ Funding Rate     — 20 pts  (replaces OI delta)
                                Very negative funding → LONG (squeeze incoming)
                                Neutral funding → +5 pts
  All 3 agree bonus  — +10 pts

VOTING GATE (runs before score):
  • Need 2 of 3 indicators to agree on direction
  • OBI + Flow agreeing = fires immediately (skip funding)
  • Score below 35 = blocked regardless

INVERSION — applied at final order placement only:
  • Scoring and voting run on true signal direction
  • When placing the order: LONG → SHORT, SHORT → LONG
  • Thesis: smart money fades retail-visible signals
  • All internal logic, TP/SL, exit checks use TRUE direction
  • Only the Bybit order side is flipped
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
    "score_blocks": 0,
    "vote_blocks": 0,
}

# ═══════════════════════════════════════════════════════════════
#  BYBIT HELPERS
# ═══════════════════════════════════════════════════════════════

def bybit_sign(params: dict) -> dict:
    params["api_key"]    = API_KEY
    params["timestamp"]  = str(int(time.time() * 1000))
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

async def get_funding_history(symbol: str, limit: int = 10) -> list:
    """Last N funding rate periods"""
    try:
        r = await bybit_get("/v5/market/funding/history", {
            "category": "linear", "symbol": symbol, "limit": str(limit)
        })
        return r.get("result", {}).get("list", [])
    except Exception as e:
        print(f"Funding history error {symbol}: {e}")
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
    """
    INVERSION applied here — the only place it touches.
    side = true signal direction (long/short)
    We flip it before sending to Bybit.
    """
    inverted_side = "short" if side == "long" else "long"
    body = {
        "category": "linear", "symbol": symbol,
        "side": "Buy" if inverted_side == "long" else "Sell",
        "orderType": "Market", "qty": str(qty),
        "stopLoss": str(sl), "takeProfit": str(tp1),
        "tpslMode": "Full", "leverage": str(LEVERAGE),
        "positionIdx": "0",
    }
    return await bybit_post("/v5/order/create", body)

async def close_position(symbol: str, qty: float, side: str) -> dict:
    """
    side = true signal direction.
    Since we entered with an inverted order (signal=long → placed short),
    closing the short means buying → Buy.
    So close_side matches the true direction Buy for long signal.
    """
    close_side = "Buy" if side == "long" else "Sell"
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
#  LAYER 2 — INDICATORS
# ═══════════════════════════════════════════════════════════════

async def get_obi(symbol: str) -> dict:
    """
    ① Order Book Imbalance — max 35 pts
    bid_vol / (bid_vol + ask_vol) across top 50 levels.
    +5 bonus if top 5 bid levels 1.5x bigger than bottom 5 (stacking near best).
    """
    ob = await get_orderbook(symbol, limit=50)
    bids = ob.get("b", [])
    asks = ob.get("a", [])

    if not bids or not asks:
        return {"obi": 0.5, "bid_stack": False, "vote": "neutral"}

    bid_vol = sum(float(b[1]) for b in bids)
    ask_vol = sum(float(a[1]) for a in asks)
    total   = bid_vol + ask_vol
    obi     = bid_vol / total if total > 0 else 0.5

    bid_vols = [float(b[1]) for b in bids]
    top5_avg = sum(bid_vols[:5]) / 5 if len(bid_vols) >= 5 else 0
    bot5_avg = sum(bid_vols[-5:]) / 5 if len(bid_vols) >= 5 else 0
    bid_stack = top5_avg >= bot5_avg * 1.5

    vote = "long" if obi > 0.55 else ("short" if obi < 0.45 else "neutral")

    return {
        "obi": round(obi, 4),
        "bid_stack": bid_stack,
        "vote": vote,
    }

async def get_flow(symbol: str) -> dict:
    """
    ② Trade Flow Aggression — max 35 pts
    buy_vol / total_vol across last 200 trades.
    +5 bonus if last 50 trades more aggressive than first 50.
    CVD divergence penalty if price and flow disagree.
    """
    trades = await get_recent_trades(symbol, limit=200)
    if not trades:
        return {"buy_ratio": 0.5, "accel_buy": False, "cvd_diverge": False, "vote": "neutral"}

    buy_vol   = sum(float(t["size"]) for t in trades if t.get("side") == "Buy")
    sell_vol  = sum(float(t["size"]) for t in trades if t.get("side") == "Sell")
    total_vol = buy_vol + sell_vol
    buy_ratio = buy_vol / total_vol if total_vol > 0 else 0.5

    first50 = trades[-50:] if len(trades) >= 100 else trades[:len(trades)//2]
    last50  = trades[:50]  if len(trades) >= 100 else trades[len(trades)//2:]
    first_buy  = sum(float(t["size"]) for t in first50 if t.get("side") == "Buy")
    last_buy   = sum(float(t["size"]) for t in last50  if t.get("side") == "Buy")
    first_sell = sum(float(t["size"]) for t in first50 if t.get("side") == "Sell")
    last_sell  = sum(float(t["size"]) for t in last50  if t.get("side") == "Sell")
    accel_buy  = last_buy > first_buy * 1.2

    first_cvd   = first_buy - first_sell
    last_cvd    = last_buy  - last_sell
    first_price = float(first50[-1]["price"]) if first50 else 0
    last_price  = float(last50[0]["price"])   if last50  else 0

    cvd_diverge = False
    if first_price > 0 and last_price > 0:
        price_up = last_price > first_price
        cvd_up   = last_cvd > first_cvd
        cvd_diverge = (price_up != cvd_up)

    vote = "long" if buy_ratio > 0.55 else ("short" if buy_ratio < 0.45 else "neutral")

    return {
        "buy_ratio":   round(buy_ratio, 4),
        "accel_buy":   accel_buy,
        "cvd_diverge": cvd_diverge,
        "vote":        vote,
    }

async def get_funding(symbol: str) -> dict:
    """
    ③ Funding Rate Momentum — max 20 pts
    Compares current funding vs average of last 10 periods.
    Very negative → LONG (shorts squeezed).
    Very positive → SHORT (longs squeezed).
    Neutral → +5 pts (no crowd bias).
    """
    ticker  = await get_tickers(symbol)
    history = await get_funding_history(symbol, limit=10)

    current_rate = float(ticker.get("fundingRate", 0))
    avg_rate     = sum(float(h["fundingRate"]) for h in history) / len(history) if history else 0.0
    funding_delta = current_rate - avg_rate

    VERY_NEGATIVE = -0.0003
    VERY_POSITIVE =  0.0003
    NEUTRAL_BAND  =  0.0001

    if current_rate <= VERY_NEGATIVE:
        vote   = "long"
        signal = "very_negative"
    elif current_rate >= VERY_POSITIVE:
        vote   = "short"
        signal = "very_positive"
    elif abs(current_rate) <= NEUTRAL_BAND:
        vote   = "neutral"
        signal = "neutral"
    else:
        vote   = "long" if current_rate < 0 else "short"
        signal = "mild_negative" if current_rate < 0 else "mild_positive"

    return {
        "current_rate":  round(current_rate, 6),
        "avg_rate":      round(avg_rate, 6),
        "funding_delta": round(funding_delta, 6),
        "signal":        signal,
        "vote":          vote,
    }

# ═══════════════════════════════════════════════════════════════
#  VOTING GATE + SCORE
# ═══════════════════════════════════════════════════════════════

def check_voting_gate(obi_data: dict, flow_data: dict, funding_data: dict) -> tuple:
    """
    Voting gate — runs before scoring.
    Rules:
      • OBI + Flow agree → fires immediately (fast path)
      • Otherwise need 2 of 3 to agree
      • No 2-of-3 → blocked
    Returns (direction | None, vote_details)
    """
    obi_vote     = obi_data.get("vote", "neutral")
    flow_vote    = flow_data.get("vote", "neutral")
    funding_vote = funding_data.get("vote", "neutral")

    vote_details = {
        "obi_vote":     obi_vote,
        "flow_vote":    flow_vote,
        "funding_vote": funding_vote,
    }

    # Fast path: OBI + Flow agree
    if obi_vote != "neutral" and obi_vote == flow_vote:
        vote_details["gate"] = f"OBI+Flow agree → {obi_vote} (fast path)"
        return obi_vote, vote_details

    # Standard path: 2-of-3
    for direction in ["long", "short"]:
        votes = sum(1 for v in [obi_vote, flow_vote, funding_vote] if v == direction)
        if votes >= 2:
            vote_details["gate"] = f"2-of-3 agree → {direction}"
            return direction, vote_details

    vote_details["gate"] = "blocked — no 2-of-3 agreement"
    return None, vote_details

def calc_score(direction: str, obi_data: dict, flow_data: dict, funding_data: dict) -> tuple:
    """
    Score = OBI (35) + Flow (35) + Funding (20) + all-agree bonus (10)
    Hard floor: score < 35 → blocked
    Minimum trade: 75
    """
    score = 0

    obi          = obi_data.get("obi", 0.5)
    bid_stack    = obi_data.get("bid_stack", False)
    buy_ratio    = flow_data.get("buy_ratio", 0.5)
    accel_buy    = flow_data.get("accel_buy", False)
    cvd_diverge  = flow_data.get("cvd_diverge", False)
    funding_sig  = funding_data.get("signal", "neutral")
    funding_vote = funding_data.get("vote", "neutral")
    obi_vote     = obi_data.get("vote", "neutral")
    flow_vote    = flow_data.get("vote", "neutral")

    # ① OBI — 35 pts
    if direction == "long":
        obi_dist = max(0, obi - 0.5) * 2
    else:
        obi_dist = max(0, 0.5 - obi) * 2
    score += min(obi_dist * 35, 35)
    if bid_stack and direction == "long":  score += 5
    if bid_stack and direction == "short": score -= 5

    # ② Trade Flow — 35 pts
    if direction == "long":
        flow_dist = max(0, buy_ratio - 0.5) * 2
    else:
        flow_dist = max(0, 0.5 - buy_ratio) * 2
    score += min(flow_dist * 35, 35)
    if accel_buy and direction == "long": score += 5
    if cvd_diverge: score -= 10

    # ③ Funding Rate — 20 pts
    if funding_sig == "very_negative" and direction == "long":   score += 20
    elif funding_sig == "very_positive" and direction == "short": score += 20
    elif funding_sig == "mild_negative" and direction == "long":  score += 12
    elif funding_sig == "mild_positive" and direction == "short": score += 12
    elif funding_sig == "neutral":                                score += 5

    # All 3 agree bonus
    if obi_vote == direction and flow_vote == direction and funding_vote == direction:
        score += 10

    score = max(0, min(100, int(score)))

    if   score >= 80: label = "VERY STRONG"
    elif score >= 75: label = "STRONG"
    elif score >= 40: label = "MEDIUM"
    else:             label = "WEAK"

    return score, label

# ═══════════════════════════════════════════════════════════════
#  TP / SL / POSITION SIZING
# ═══════════════════════════════════════════════════════════════

def get_tp_sl(score: int, entry: float, direction: str) -> dict:
    """
    TP/SL on TRUE signal direction.
    Inversion handled at order placement — prices stay the same.
    STRONG  75-79: TP1 +7%  TP2 +18%  TP3 +30%  SL -3.5%
    V.STRONG 80+:  TP1 +10% TP2 +25%  TP3 +40%  SL -4%
    """
    if score >= 80:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 10.0, 25.0, 40.0, 4.0
    else:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 7.0, 18.0, 30.0, 3.5

    m = 1 if direction == "long" else -1
    return {
        "tp1": round(entry * (1 + m * tp1_pct / 100), 6),
        "tp2": round(entry * (1 + m * tp2_pct / 100), 6),
        "tp3": round(entry * (1 + m * tp3_pct / 100), 6),
        "sl":  round(entry * (1 - m * sl_pct  / 100), 6),
        "tp1_pct": tp1_pct, "tp2_pct": tp2_pct,
        "tp3_pct": tp3_pct, "sl_pct":  sl_pct,
    }

def get_position_size(score: int, balance: float) -> float:
    pct = 25.0 if score >= 80 else 20.0
    return min(balance * pct / 100, balance * 0.30)

# ═══════════════════════════════════════════════════════════════
#  EXIT — MOMENTUM EXHAUSTION
# ═══════════════════════════════════════════════════════════════

async def check_momentum_exhaustion(symbol: str, direction: str, entry: float, current_price: float) -> bool:
    if direction == "long":
        profit_pct = (current_price - entry) / entry * 100
    else:
        profit_pct = (entry - current_price) / entry * 100

    if profit_pct < 2.0:
        return False

    flow      = await get_flow(symbol)
    buy_ratio = flow.get("buy_ratio", 0.5)
    accel_buy = flow.get("accel_buy", False)
    cvd_dying = (buy_ratio < 0.55 and not accel_buy) if direction == "long" \
                else (buy_ratio > 0.45 and not accel_buy)

    if cvd_dying:
        print(f"⚡ Momentum exhaustion {symbol} | profit={profit_pct:.1f}% | buy_ratio={buy_ratio:.3f}")
    return cvd_dying

# ═══════════════════════════════════════════════════════════════
#  EXIT — CVD STRUCTURE BREAK
# ═══════════════════════════════════════════════════════════════

async def check_structure_break(symbol: str, direction: str, entry: float, current_price: float) -> bool:
    if direction == "long":
        profit_pct = (current_price - entry) / entry * 100
    else:
        profit_pct = (entry - current_price) / entry * 100

    if profit_pct < 1.0:
        return False

    flow      = await get_flow(symbol)
    diverge   = flow.get("cvd_diverge", False)
    buy_ratio = flow.get("buy_ratio", 0.5)

    if direction == "long" and diverge and buy_ratio < 0.45:
        print(f"🔴 Structure break {symbol} LONG | buy_ratio={buy_ratio:.3f}")
        return True
    if direction == "short" and diverge and buy_ratio > 0.55:
        print(f"🔴 Structure break {symbol} SHORT | buy_ratio={buy_ratio:.3f}")
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
    Full pipeline:
    1. Fetch all 3 indicators in parallel
    2. Voting gate → direction or block
    3. Score — hard floor 35, min trade 75
    4. Return signal with TRUE direction
       (inversion happens only inside place_order)
    """
    ticker = await get_tickers(symbol)
    if not ticker:
        return None

    price = float(ticker.get("lastPrice", 0))
    if price <= 0:
        return None

    # Fetch all 3 indicators in parallel
    obi_data, flow_data, funding_data = await asyncio.gather(
        get_obi(symbol),
        get_flow(symbol),
        get_funding(symbol),
    )

    # Voting gate
    direction, vote_details = check_voting_gate(obi_data, flow_data, funding_data)
    if direction is None:
        state["vote_blocks"] += 1
        print(f"🗳 {symbol} BLOCKED voting gate: {vote_details.get('gate','')}")
        return None

    # Score
    score, label = calc_score(direction, obi_data, flow_data, funding_data)

    if score < 35:
        state["score_blocks"] += 1
        print(f"📊 {symbol} score={score} < 35 floor — blocked")
        return None

    if score < MIN_SCORE:
        state["score_blocks"] += 1
        print(f"📊 {symbol} score={score} < {MIN_SCORE} — skipped")
        return None

    tp_sl    = get_tp_sl(score, price, direction)
    inverted = "short" if direction == "long" else "long"

    print(f"✅ SIGNAL: {symbol} true={direction.upper()} → placing {inverted.upper()} | "
          f"score={score} {label} | OBI={obi_data['obi']:.3f} "
          f"Flow={flow_data['buy_ratio']:.3f} Funding={funding_data['signal']} | "
          f"{vote_details['gate']}")

    return {
        "symbol":    symbol,
        "direction": direction,
        "inverted":  inverted,
        "price":     price,
        "score":     score,
        "label":     label,
        "tp_sl":     tp_sl,
        "obi":       obi_data,
        "flow":      flow_data,
        "funding":   funding_data,
        "votes":     vote_details,
        "timestamp": int(time.time()),
    }

# ═══════════════════════════════════════════════════════════════
#  POSITION MONITOR
# ═══════════════════════════════════════════════════════════════

async def monitor_position():
    pos = state.get("position")
    if not pos:
        return

    symbol    = pos["symbol"]
    direction = pos["direction"]    # TRUE direction for P&L + exit logic
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

    if direction == "long":
        unrealized_pct = (price - entry) / entry * 100
    else:
        unrealized_pct = (entry - price) / entry * 100
    pos["unrealized_pct"] = round(unrealized_pct, 2)

    tp1 = tp_sl["tp1"]; tp2 = tp_sl["tp2"]; tp3 = tp_sl["tp3"]

    # TP3 — full close
    if not tp2_hit and ((direction == "long" and price >= tp3) or
                        (direction == "short" and price <= tp3)):
        print(f"🎯 TP3 hit {symbol} @ {price}")
        await close_position(symbol, qty * 0.20, direction)
        await record_close(symbol, price, "win", "TP3")
        return

    # TP2 — close 40%
    if not tp2_hit and ((direction == "long" and price >= tp2) or
                        (direction == "short" and price <= tp2)):
        print(f"🎯 TP2 hit {symbol} @ {price}")
        await close_position(symbol, qty * 0.40, direction)
        pos["tp2_hit"] = True
        pos["qty"] = qty * 0.20

    # TP1 — close 40% + breakeven SL
    if not tp1_hit and ((direction == "long" and price >= tp1) or
                        (direction == "short" and price <= tp1)):
        print(f"🎯 TP1 hit {symbol} @ {price} — moving SL to breakeven")
        await close_position(symbol, qty * 0.40, direction)
        pos["tp1_hit"] = True
        pos["qty"] = qty * 0.60
        be_sl = entry * 1.001 if direction == "long" else entry * 0.999
        pos["tp_sl"]["sl"] = round(be_sl, 6)
        await set_sl(symbol, be_sl, direction)
        print(f"🔒 SL → breakeven {be_sl:.4f}")
        return

    # Momentum exhaustion
    if await check_momentum_exhaustion(symbol, direction, entry, price):
        print(f"⚡ Momentum exit {symbol} @ {price}")
        await close_position(symbol, pos["qty"], direction)
        await record_close(symbol, price, "win", "momentum_exit")
        return

    # Structure break
    if await check_structure_break(symbol, direction, entry, price):
        print(f"🔴 Structure break exit {symbol} @ {price}")
        await close_position(symbol, pos["qty"], direction)
        await record_close(symbol, price, "win", "structure_break")
        return

async def record_close(symbol: str, exit_price: float, result: str, reason: str):
    pos = state.get("position")
    if not pos:
        return

    entry     = pos["entry"]
    direction = pos["direction"]
    size      = pos.get("size_usdt", 0)

    if direction == "long":
        pnl = (exit_price - entry) / entry * size * LEVERAGE
    else:
        pnl = (entry - exit_price) / entry * size * LEVERAGE
    pnl = round(pnl, 4)

    trade = {
        "symbol":     symbol,
        "direction":  direction,
        "inverted":   pos.get("inverted", ""),
        "result":     result,
        "entry":      entry,
        "exit_price": exit_price,
        "pnl":        pnl,
        "score":      pos.get("score", 0),
        "label":      pos.get("label", ""),
        "reason":     reason,
        "tp1_hit":    pos.get("tp1_hit", False),
        "tp2_hit":    pos.get("tp2_hit", False),
        "timestamp":  int(time.time()),
    }

    state["trades"].insert(0, trade)
    state["trades"] = state["trades"][:100]
    state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
    if result == "win": state["wins"] += 1
    else:               state["losses"] += 1
    state["position"] = None

    print(f"{'✅ WIN' if result=='win' else '❌ LOSS'} {symbol} | PnL={pnl:+.4f} | reason={reason}")
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
    print("🚀 APEX Pro v3.0 starting...")
    await asyncio.sleep(3)

    while True:
        try:
            state["balance"]   = await get_balance()
            state["last_scan"] = int(time.time())

            if state.get("position"):
                await monitor_position()
                await asyncio.sleep(5)
                continue

            state["status"] = "scanning"
            print(f"🔍 Scanning {len(WATCHLIST)} symbols | Balance: ${state['balance']:.2f}")

            for symbol in WATCHLIST:
                if state.get("position"):
                    break

                signal = await scan_symbol(symbol)
                if not signal:
                    await asyncio.sleep(1)
                    continue

                balance = state["balance"]
                size    = get_position_size(signal["score"], balance)
                price   = signal["price"]
                qty     = round((size * LEVERAGE) / price, 3)

                if qty <= 0 or size < 5:
                    print(f"⚠️ Size too small: ${size:.2f} — skip")
                    continue

                tp_sl = signal["tp_sl"]
                # place_order flips direction internally
                resp = await place_order(symbol, signal["direction"], qty,
                                         tp_sl["sl"], tp_sl["tp1"])

                if resp and resp.get("retCode") == 0:
                    state["position"] = {
                        "symbol":    symbol,
                        "direction": signal["direction"],   # TRUE direction
                        "inverted":  signal["inverted"],    # what Bybit received
                        "entry":     price,
                        "qty":       qty,
                        "size_usdt": size,
                        "score":     signal["score"],
                        "label":     signal["label"],
                        "tp_sl":     tp_sl,
                        "votes":     signal["votes"],
                        "funding":   signal["funding"],
                        "tp1_hit":   False,
                        "tp2_hit":   False,
                        "opened_at": int(time.time()),
                    }
                    state["status"] = "in_trade"
                    print(f"📈 ENTERED {symbol} {signal['inverted'].upper()} "
                          f"(signal={signal['direction'].upper()}) | "
                          f"qty={qty} size=${size:.2f} score={signal['score']}")
                else:
                    print(f"⚠️ Order failed: {resp}")

                break

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
    pos    = state.get("position")
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
        "vote_blocks":  state["vote_blocks"],
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
<title>APEX Pro v3</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#060608;
  --surface:#0d0d12;
  --card:#111118;
  --border:#1c1c28;
  --border2:#242436;
  --accent:#e8ff47;
  --accent2:#ff6b35;
  --green:#00f5a0;
  --red:#ff3d6b;
  --blue:#4d9fff;
  --purple:#a855f7;
  --text:#f0f0f8;
  --muted:#52526a;
  --muted2:#3a3a52;
}
body{
  background:var(--bg);
  color:var(--text);
  font-family:'IBM Plex Mono',monospace;
  max-width:430px;
  margin:0 auto;
  padding:0 0 80px;
  min-height:100vh;
  position:relative;
}
body::before{
  content:'';
  position:fixed;
  top:-200px;left:-100px;
  width:500px;height:500px;
  background:radial-gradient(circle,rgba(232,255,71,.03) 0%,transparent 70%);
  pointer-events:none;
  z-index:0;
}

/* ── HEADER ── */
.hdr{
  display:flex;justify-content:space-between;align-items:center;
  padding:18px 16px 14px;
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:100;
  background:rgba(6,6,8,.92);
  backdrop-filter:blur(12px);
}
.logo{font-family:'Syne',sans-serif;font-size:20px;font-weight:800;letter-spacing:-.02em}
.logo em{color:var(--accent);font-style:normal}
.logo small{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);font-weight:400;margin-left:6px;letter-spacing:.05em}
.status-pill{
  display:flex;align-items:center;gap:7px;
  font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  padding:5px 12px;border-radius:20px;
  border:1px solid;
  transition:all .3s;
}
.status-pill.scanning{border-color:rgba(232,255,71,.3);color:var(--accent);background:rgba(232,255,71,.06)}
.status-pill.in_trade{border-color:rgba(0,245,160,.3);color:var(--green);background:rgba(0,245,160,.06)}
.status-pill.idle{border-color:var(--border2);color:var(--muted);background:transparent}
.status-pill.error{border-color:rgba(255,61,107,.3);color:var(--red);background:rgba(255,61,107,.06)}
.pulse{width:6px;height:6px;border-radius:50%;animation:blink 1.4s infinite}
.scanning .pulse{background:var(--accent)}
.in_trade .pulse{background:var(--green)}
.idle .pulse{background:var(--muted)}
.error .pulse{background:var(--red)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

/* ── SCAN ENGINE PANEL ── */
.engine{
  margin:12px 12px 0;
  background:var(--card);
  border:1px solid var(--border);
  border-radius:16px;
  overflow:hidden;
}
.engine-hdr{
  display:flex;justify-content:space-between;align-items:center;
  padding:12px 14px;
  border-bottom:1px solid var(--border);
  background:rgba(232,255,71,.03);
}
.engine-title{font-size:9px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted)}
.engine-timer{font-size:11px;color:var(--accent)}
.block-grid{
  display:grid;grid-template-columns:1fr 1fr;
  border-bottom:1px solid var(--border);
}
.block-cell{
  padding:10px 12px;
  border-right:1px solid var(--border);
}
.block-cell:last-child{border-right:none}
.block-lbl{font-size:8px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.block-val{font-size:18px;font-weight:600;color:var(--text)}
.block-sub{font-size:9px;color:var(--muted);margin-top:2px}
.scan-bar{
  padding:10px 14px;
  display:flex;align-items:center;gap:10px;
  font-size:10px;color:var(--muted);
}
.scan-dot{
  width:5px;height:5px;border-radius:50%;
  background:var(--accent);flex-shrink:0;
}
.scan-scanning .scan-dot{animation:blink .7s infinite}
#scan-ticker{color:var(--text);font-size:11px}

/* ── INVERSION BANNER ── */
.inv-banner{
  margin:10px 12px 0;
  background:rgba(255,107,53,.06);
  border:1px solid rgba(255,107,53,.2);
  border-radius:12px;
  padding:10px 14px;
  display:flex;align-items:center;gap:10px;
  font-size:10px;color:rgba(255,107,53,.9);
  letter-spacing:.03em;
}
.inv-icon{font-size:14px;flex-shrink:0}

/* ── STATS ROW ── */
.stats{
  display:grid;grid-template-columns:1fr 1fr 1fr;
  gap:8px;
  margin:10px 12px 0;
}
.stat{
  background:var(--card);border:1px solid var(--border);
  border-radius:12px;padding:12px 10px;
}
.stat-lbl{font-size:8px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:5px}
.stat-val{font-size:20px;font-weight:600;font-family:'Syne',sans-serif;line-height:1}
.stat-sub{font-size:9px;color:var(--muted);margin-top:4px}
.g{color:var(--green)}.r{color:var(--red)}.a{color:var(--accent)}.b{color:var(--blue)}

/* ── SECTION LABEL ── */
.sec{
  font-size:8px;letter-spacing:.18em;text-transform:uppercase;
  color:var(--muted);
  display:flex;align-items:center;gap:8px;
  padding:16px 12px 8px;
}
.sec::after{content:'';flex:1;height:1px;background:var(--border)}

/* ── POSITION CARD ── */
.pos{
  margin:0 12px;
  background:var(--card);
  border:1px solid var(--border2);
  border-radius:16px;
  overflow:hidden;
}
.pos-top{
  display:flex;justify-content:space-between;align-items:flex-start;
  padding:14px;
  border-bottom:1px solid var(--border);
}
.pos-sym{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;line-height:1}
.pos-arrow{
  font-size:9px;letter-spacing:.08em;
  display:flex;align-items:center;gap:5px;
  margin-top:5px;color:var(--muted);
}
.pos-arrow .sig{padding:2px 7px;border-radius:4px;font-weight:600;letter-spacing:.06em}
.long-chip{background:rgba(0,245,160,.1);color:var(--green)}
.short-chip{background:rgba(255,61,107,.1);color:var(--red)}
.pos-pnl{text-align:right}
.pos-pct{font-family:'Syne',sans-serif;font-size:26px;font-weight:800;line-height:1}
.pos-score{font-size:9px;color:var(--muted);margin-top:4px}

.pos-tps{
  display:flex;gap:6px;padding:10px 14px;
  border-bottom:1px solid var(--border);
  flex-wrap:wrap;
}
.tp{
  font-size:9px;padding:4px 10px;border-radius:8px;
  border:1px solid var(--border2);color:var(--muted);
  letter-spacing:.04em;
}
.tp.hit{border-color:rgba(0,245,160,.3);color:var(--green);background:rgba(0,245,160,.07)}
.tp.sl{border-color:rgba(255,61,107,.2);color:var(--red)}

.pos-layers{padding:10px 14px}
.lr{
  display:flex;justify-content:space-between;
  padding:5px 0;
  border-bottom:1px solid rgba(255,255,255,.03);
  font-size:10px;
}
.lr:last-child{border:none}
.lr-k{color:var(--muted)}
.lr-v{color:var(--text)}
.lr-v.pass{color:var(--green)}
.lr-v.fail{color:var(--red)}
.lr-v.neutral{color:var(--muted)}

/* ── TRADES ── */
.trades{margin:0 12px}
.trade{
  display:flex;justify-content:space-between;align-items:center;
  padding:11px 0;
  border-bottom:1px solid var(--border);
}
.trade:last-child{border:none}
.t-sym{font-family:'Syne',sans-serif;font-size:14px;font-weight:700}
.t-meta{font-size:9px;color:var(--muted);margin-top:3px;line-height:1.5}
.t-meta .arrow{color:var(--muted2)}
.t-pnl{font-family:'Syne',sans-serif;font-size:16px;font-weight:700}
.empty{
  text-align:center;padding:28px 0;
  font-size:11px;color:var(--muted);
  letter-spacing:.05em;
}

/* ── BOTTOM NAV ── */
.bnav{
  position:fixed;bottom:0;left:50%;transform:translateX(-50%);
  width:100%;max-width:430px;
  background:rgba(6,6,8,.95);
  backdrop-filter:blur(16px);
  border-top:1px solid var(--border);
  padding:10px 20px 20px;
  display:flex;justify-content:space-between;align-items:center;
  font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  z-index:200;
}
.bnav-bal{font-family:'Syne',sans-serif;font-size:16px;font-weight:700;color:var(--text);letter-spacing:-.01em}
.bnav-right{text-align:right}
.refresh-ring{
  width:28px;height:28px;border-radius:50%;
  border:1.5px solid var(--border2);
  display:flex;align-items:center;justify-content:center;
  font-size:12px;cursor:pointer;
  transition:.2s;
}
.refresh-ring:active{border-color:var(--accent);color:var(--accent)}
.spinning{animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* empty state scan animation */
.radar{
  width:60px;height:60px;border-radius:50%;
  border:1px solid rgba(232,255,71,.15);
  display:flex;align-items:center;justify-content:center;
  margin:0 auto 12px;
  position:relative;
}
.radar::before{
  content:'';position:absolute;
  width:100%;height:100%;border-radius:50%;
  border:1px solid rgba(232,255,71,.3);
  animation:ripple 2s infinite;
}
@keyframes ripple{
  0%{transform:scale(1);opacity:.6}
  100%{transform:scale(1.6);opacity:0}
}
.radar-dot{width:8px;height:8px;border-radius:50%;background:var(--accent)}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">APEX <em>Pro</em> <small>v3.0</small></div>
  <div class="status-pill idle" id="status-pill">
    <span class="pulse"></span>
    <span id="status-txt">STARTING</span>
  </div>
</div>

<!-- SCAN ENGINE -->
<div class="engine">
  <div class="engine-hdr">
    <span class="engine-title">⚡ Scan Engine</span>
    <span class="engine-timer" id="scan-age">—</span>
  </div>
  <div class="block-grid">
    <div class="block-cell">
      <div class="block-lbl">Vote blocks</div>
      <div class="block-val" id="b-vote">0</div>
      <div class="block-sub">no 2-of-3</div>
    </div>
    <div class="block-cell">
      <div class="block-lbl">Score blocks</div>
      <div class="block-val" id="b-score">0</div>
      <div class="block-sub">below 75</div>
    </div>
  </div>
  <div class="scan-bar" id="scan-bar">
    <span class="scan-dot"></span>
    <span id="scan-ticker">Initialising...</span>
  </div>
</div>

<!-- INVERSION BANNER -->
<div class="inv-banner">
  <span class="inv-icon">↔</span>
  <span>INVERSION ON — signal LONG places SHORT &amp; vice versa</span>
</div>

<!-- STATS -->
<div class="stats">
  <div class="stat">
    <div class="stat-lbl">P&amp;L</div>
    <div class="stat-val" id="s-pnl">$0</div>
    <div class="stat-sub" id="s-pnl-sub">—</div>
  </div>
  <div class="stat">
    <div class="stat-lbl">Win rate</div>
    <div class="stat-val a" id="s-wr">0%</div>
    <div class="stat-sub" id="s-wl">0W / 0L</div>
  </div>
  <div class="stat">
    <div class="stat-lbl">Balance</div>
    <div class="stat-val b" id="s-bal">$0</div>
    <div class="stat-sub">USDT</div>
  </div>
</div>

<!-- POSITION -->
<div id="pos-section"></div>

<!-- TRADES -->
<div class="sec">Trade Log</div>
<div class="trades" id="trade-log">
  <div class="empty">
    <div class="radar"><div class="radar-dot"></div></div>
    Scanning for signals...
  </div>
</div>

<!-- BOTTOM NAV -->
<div class="bnav">
  <div>
    <div style="margin-bottom:2px">balance</div>
    <div class="bnav-bal" id="bnav-bal">$0.00</div>
  </div>
  <div class="bnav-right">
    <div style="margin-bottom:4px" id="bnav-status">idle</div>
    <div style="display:flex;align-items:center;gap:8px;justify-content:flex-end">
      <span id="bnav-time" style="font-size:9px">—</span>
      <div class="refresh-ring" id="refresh-btn" onclick="manualRefresh()">↻</div>
    </div>
  </div>
</div>

<script>
let lastData = null
let scannerTick = 0
const WATCHLIST = [
  'BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT',
  'DOGEUSDT','AVAXUSDT','LINKUSDT','DOTUSDT','MATICUSDT',
  'ADAUSDT','LTCUSDT','ATOMUSDT','NEARUSDT','APTUSDT'
]

function ago(ts){
  if(!ts) return '—'
  const s = Math.floor(Date.now()/1000) - ts
  if(s < 60)  return s+'s ago'
  if(s < 3600) return Math.floor(s/60)+'m ago'
  return Math.floor(s/3600)+'h ago'
}

function updateStatusPill(status){
  const pill = document.getElementById('status-pill')
  const txt  = document.getElementById('status-txt')
  pill.className = 'status-pill ' + (status||'idle')
  const map = {scanning:'SCANNING',in_trade:'IN TRADE',idle:'IDLE',error:'ERROR',starting:'STARTING'}
  txt.textContent = map[status]||status.toUpperCase()
}

function animateScanBar(status, lastScan){
  const bar    = document.getElementById('scan-bar')
  const ticker = document.getElementById('scan-ticker')
  const dot    = bar.querySelector('.scan-dot')

  if(status === 'scanning'){
    bar.className = 'scan-bar scan-scanning'
    scannerTick = (scannerTick+1) % WATCHLIST.length
    ticker.innerHTML = `Scanning <span style="color:var(--accent)">${WATCHLIST[scannerTick]}</span> ...`
  } else if(status === 'in_trade'){
    bar.className = 'scan-bar'
    dot.style.background = 'var(--green)'
    dot.style.animation  = 'none'
    ticker.innerHTML = `<span style="color:var(--green)">Position open — monitoring every 5s</span>`
  } else {
    bar.className = 'scan-bar'
    dot.style.background = 'var(--muted)'
    dot.style.animation  = 'none'
    ticker.innerHTML = `Next scan in ~30s &nbsp;·&nbsp; Last: <span style="color:var(--text)">${ago(lastScan)}</span>`
  }
}

function render(d){
  lastData = d
  updateStatusPill(d.status)
  animateScanBar(d.status, d.last_scan)

  // engine
  document.getElementById('scan-age').textContent  = ago(d.last_scan)
  document.getElementById('b-vote').textContent    = d.vote_blocks||0
  document.getElementById('b-score').textContent   = d.score_blocks||0

  // stats
  const pnl = d.total_pnl||0
  document.getElementById('s-pnl').className = 'stat-val '+(pnl>=0?'g':'r')
  document.getElementById('s-pnl').textContent = (pnl>=0?'+':'')+pnl.toFixed(2)
  document.getElementById('s-pnl-sub').textContent = 'min score '+d.min_score
  document.getElementById('s-wr').textContent  = d.win_rate+'%'
  document.getElementById('s-wl').textContent  = d.wins+'W / '+d.losses+'L'
  document.getElementById('s-bal').textContent = '$'+d.balance.toFixed(2)
  document.getElementById('bnav-bal').textContent = '$'+d.balance.toFixed(2)
  document.getElementById('bnav-status').textContent = d.status||'idle'
  document.getElementById('bnav-time').textContent = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})

  // position
  renderPosition(d.position)

  // trades
  renderTrades(d.trades)
}

function renderPosition(pos){
  const sec = document.getElementById('pos-section')
  if(!pos){sec.innerHTML='';return}

  const dir    = pos.direction
  const inv    = pos.inverted
  const tp     = pos.tp_sl||{}
  const unreal = pos.unrealized_pct||0
  const votes  = pos.votes||{}
  const fund   = pos.funding||{}
  const pColor = unreal>=0 ? 'var(--green)':'var(--red)'

  sec.innerHTML = `
  <div class="sec">Open Position</div>
  <div class="pos">
    <div class="pos-top">
      <div>
        <div class="pos-sym">${pos.symbol}</div>
        <div class="pos-arrow">
          Signal <span class="sig ${dir==='long'?'long-chip':'short-chip'}">${dir.toUpperCase()}</span>
          <span style="color:var(--muted2)">→ placed</span>
          <span class="sig ${inv==='long'?'long-chip':'short-chip'}">${inv.toUpperCase()}</span>
        </div>
        <div style="font-size:9px;color:var(--muted);margin-top:6px">
          Entry $${pos.entry} &nbsp;·&nbsp; ${pos.score} pts ${pos.label}
        </div>
      </div>
      <div class="pos-pnl">
        <div class="pos-pct" style="color:${pColor}">${unreal>=0?'+':''}${unreal.toFixed(2)}%</div>
        <div class="pos-score">unrealised</div>
      </div>
    </div>
    <div class="pos-tps">
      <span class="tp ${pos.tp1_hit?'hit':''}">TP1 ${tp.tp1_pct||0}%</span>
      <span class="tp ${pos.tp2_hit?'hit':''}">TP2 ${tp.tp2_pct||0}%</span>
      <span class="tp">TP3 ${tp.tp3_pct||0}%</span>
      <span class="tp sl">SL ${tp.sl_pct||0}%</span>
    </div>
    <div class="pos-layers">
      <div class="lr"><span class="lr-k">Vote gate</span><span class="lr-v">${votes.gate||'—'}</span></div>
      <div class="lr"><span class="lr-k">OBI vote</span><span class="lr-v ${votes.obi_vote===dir?'pass':votes.obi_vote==='neutral'?'neutral':'fail'}">${votes.obi_vote||'—'}</span></div>
      <div class="lr"><span class="lr-k">Flow vote</span><span class="lr-v ${votes.flow_vote===dir?'pass':votes.flow_vote==='neutral'?'neutral':'fail'}">${votes.flow_vote||'—'}</span></div>
      <div class="lr"><span class="lr-k">Funding</span><span class="lr-v">${fund.signal||'—'} &nbsp;(${fund.current_rate||0})</span></div>
    </div>
  </div>`
}

function renderTrades(trades){
  const el = document.getElementById('trade-log')
  if(!trades||!trades.length){
    el.innerHTML = `<div class="empty">
      <div class="radar"><div class="radar-dot"></div></div>
      Scanning for signals...
    </div>`
    return
  }
  el.innerHTML = trades.map(t=>{
    const win = t.result==='win'
    const pnl = (t.pnl||0).toFixed(2)
    return `<div class="trade">
      <div>
        <div class="t-sym">${t.symbol}</div>
        <div class="t-meta">
          <span class="${t.direction==='long'?'g':'r'}">${t.direction?.toUpperCase()}</span>
          <span class="arrow"> → </span>
          <span class="${t.inverted==='long'?'g':'r'}">${t.inverted?.toUpperCase()||'?'}</span>
          &nbsp;·&nbsp;${t.score}pts&nbsp;·&nbsp;${t.reason||''}
        </div>
      </div>
      <div class="t-pnl ${win?'g':'r'}">${win?'+':''}$${pnl}</div>
    </div>`
  }).join('')
}

async function manualRefresh(){
  const btn = document.getElementById('refresh-btn')
  btn.classList.add('spinning')
  await fetchData()
  setTimeout(()=>btn.classList.remove('spinning'),500)
}

async function fetchData(){
  try{
    const d = await fetch('/api/state').then(r=>r.json())
    render(d)
  }catch(e){
    document.getElementById('status-txt').textContent='ERROR'
    console.error(e)
  }
}

// Fast refresh when scanning/in trade, slow when idle
function scheduleNext(status){
  const delay = status==='scanning'||status==='in_trade' ? 4000 : 8000
  setTimeout(loop, delay)
}

async function loop(){
  await fetchData()
  scheduleNext(lastData?.status||'idle')
}

// Animate scan ticker independently
setInterval(()=>{
  if(lastData?.status==='scanning'){
    scannerTick = (scannerTick+1) % WATCHLIST.length
    document.getElementById('scan-ticker').innerHTML =
      `Scanning <span style="color:var(--accent)">${WATCHLIST[scannerTick]}</span> ...`
  }
  if(lastData?.last_scan){
    document.getElementById('scan-age').textContent = ago(lastData.last_scan)
    document.getElementById('bnav-time').textContent = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})
  }
},1500)

loop()
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
