"""
APEX Pro Bot — Original + Inverted Signal
══════════════════════════════════════════
ONLY CHANGE FROM ORIGINAL:
  OBI heavy bids  → SHORT (was LONG)
  OBI heavy asks  → LONG  (was SHORT)
  Trade flow heavy buys  → SHORT (was LONG)
  Trade flow heavy sells → LONG  (was SHORT)

Everything else identical to original:
  - OBI + Trade Flow + Funding Rate scoring
  - Dynamic TP1/TP2/TP3 based on score
  - Score-driven position sizing
  - Supabase persistence
  - Same watchlist, same scan interval
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
LEVERAGE   = 2
SCAN_INTERVAL = 30

# ── SUPABASE ─────────────────────────────────────────────────
SB_URL = os.getenv("SUPABASE_URL", "")
SB_KEY = os.getenv("SUPABASE_KEY", "")

# ── STATE ────────────────────────────────────────────────────
state = {
    "wins": 0, "losses": 0, "total_pnl": 0.0,
    "trades": [], "position": None, "balance": 0.0,
    "last_scan": 0, "status": "starting",
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

async def by_get(path: str, params: dict = {}, signed: bool = False) -> dict:
    if signed:
        params = bybit_sign(params)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BASE_URL + path, params=params)
        return r.json()

async def by_post(path: str, body: dict) -> dict:
    body = bybit_sign(body)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(BASE_URL + path, json=body)
        return r.json()

async def get_balance() -> float:
    try:
        r = await by_get("/v5/account/wallet-balance",
                         {"accountType": "UNIFIED"}, signed=True)
        coins = r["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                return float(c["availableToWithdraw"])
    except Exception as e:
        print(f"Balance error: {e}")
    return 0.0

async def get_tickers(symbol: str) -> dict:
    try:
        r = await by_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        items = r.get("result", {}).get("list", [])
        return items[0] if items else {}
    except Exception as e:
        print(f"Ticker error {symbol}: {e}")
        return {}

async def place_order(symbol: str, side: str, qty: float, sl: float, tp: float) -> dict:
    body = {
        "category": "linear", "symbol": symbol,
        "side": "Buy" if side == "long" else "Sell",
        "orderType": "Market", "qty": str(qty),
        "stopLoss": str(sl), "takeProfit": str(tp),
        "tpslMode": "Full", "leverage": str(LEVERAGE),
        "positionIdx": "0",
    }
    return await by_post("/v5/order/create", body)

async def close_position(symbol: str, qty: float, side: str) -> dict:
    close_side = "Sell" if side == "long" else "Buy"
    body = {
        "category": "linear", "symbol": symbol,
        "side": close_side, "orderType": "Market",
        "qty": str(qty), "reduceOnly": True,
        "positionIdx": "0",
    }
    return await by_post("/v5/order/create", body)

# ═══════════════════════════════════════════════════════════════
#  LEAD INDICATORS — INVERTED
#  Heavy bids  → SHORT (smart money selling into demand)
#  Heavy sells → LONG  (smart money buying into panic)
# ═══════════════════════════════════════════════════════════════

async def get_orderbook_imbalance(symbol: str) -> dict:
    try:
        r = await by_get("/v5/market/orderbook",
                         {"category": "linear", "symbol": symbol, "limit": "50"})
        ob   = r.get("result", {})
        bids = ob.get("b", [])
        asks = ob.get("a", [])
        if not bids or not asks:
            return {"obi": 0.5, "signal": "neutral", "bid_stack": False}

        bid_vol = sum(float(b[1]) for b in bids)
        ask_vol = sum(float(a[1]) for a in asks)
        total   = bid_vol + ask_vol
        obi     = bid_vol / total if total > 0 else 0.5

        # Bid stacking check
        bid_vols  = [float(b[1]) for b in bids]
        top5_avg  = sum(bid_vols[:5]) / 5 if len(bid_vols) >= 5 else 0
        bot5_avg  = sum(bid_vols[-5:]) / 5 if len(bid_vols) >= 5 else 0
        bid_stack = top5_avg >= bot5_avg * 1.5

        # ── INVERTED ──
        if obi > 0.65 or (obi > 0.58 and bid_stack):
            signal = "short"   # was "long"
        elif obi < 0.35:
            signal = "long"    # was "short"
        else:
            signal = "neutral"

        return {"obi": round(obi, 4), "signal": signal, "bid_stack": bid_stack}
    except Exception as e:
        print(f"OBI error {symbol}: {e}")
        return {"obi": 0.5, "signal": "neutral", "bid_stack": False}

async def get_trade_flow(symbol: str) -> dict:
    try:
        r = await by_get("/v5/market/recent-trade",
                         {"category": "linear", "symbol": symbol, "limit": "200"})
        trades = r.get("result", {}).get("list", [])
        if not trades:
            return {"buy_ratio": 0.5, "signal": "neutral", "accel_buy": False}

        buy_vol  = sum(float(t["size"]) for t in trades if t.get("side") == "Buy")
        total_vol = sum(float(t["size"]) for t in trades)
        buy_ratio = buy_vol / total_vol if total_vol > 0 else 0.5

        # Acceleration check
        mid       = len(trades) // 2
        older     = trades[mid:]
        recent    = trades[:mid]
        r_buy     = sum(float(t["size"]) for t in recent if t.get("side") == "Buy")
        r_tot     = sum(float(t["size"]) for t in recent) or 1
        o_buy     = sum(float(t["size"]) for t in older  if t.get("side") == "Buy")
        o_tot     = sum(float(t["size"]) for t in older)  or 1
        accel_buy = (r_buy / r_tot) > (o_buy / o_tot) * 1.2

        # ── INVERTED ──
        if buy_ratio > 0.60 or (buy_ratio > 0.53 and accel_buy):
            signal = "short"   # was "long"
        elif buy_ratio < 0.40:
            signal = "long"    # was "short"
        else:
            signal = "neutral"

        return {"buy_ratio": round(buy_ratio, 3), "signal": signal, "accel_buy": accel_buy}
    except Exception as e:
        print(f"Trade flow error {symbol}: {e}")
        return {"buy_ratio": 0.5, "signal": "neutral", "accel_buy": False}

async def get_funding_momentum(symbol: str) -> dict:
    try:
        td = await by_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        items   = td.get("result", {}).get("list", [])
        current = float(items[0].get("fundingRate", 0)) * 100 if items else 0

        hd   = await by_get("/v5/market/funding/history",
                            {"category": "linear", "symbol": symbol, "limit": "10"})
        hist  = hd.get("result", {}).get("list", [])
        rates = [float(h["fundingRate"]) * 100 for h in hist] if hist else [current]
        avg   = sum(rates) / len(rates) if rates else current
        momentum = current - avg

        # Funding not inverted — it's a squeeze signal, stays same direction
        if   current < -0.05 and momentum < -0.02: signal = "long"
        elif current >  0.05 and momentum >  0.02: signal = "short"
        else:                                       signal = "neutral"

        return {"rate": round(current, 4), "momentum": round(momentum, 4), "signal": signal}
    except Exception as e:
        print(f"Funding error {symbol}: {e}")
        return {"rate": 0, "momentum": 0, "signal": "neutral"}

async def get_signal(symbol: str):
    """
    Run all 3 indicators in parallel.
    Need 2/3 agreement. OBI + Flow alone is enough.
    Returns (direction, details) or (None, details)
    """
    try:
        obi, flow, fund = await asyncio.gather(
            get_orderbook_imbalance(symbol),
            get_trade_flow(symbol),
            get_funding_momentum(symbol),
        )
    except Exception as e:
        print(f"Signal error {symbol}: {e}")
        return None, {}

    os_ = obi.get("signal",  "neutral")
    fs_ = flow.get("signal", "neutral")
    fd_ = fund.get("signal", "neutral")

    long_votes  = [os_, fs_, fd_].count("long")
    short_votes = [os_, fs_, fd_].count("short")

    obi_flow_long  = os_ == "long"  and fs_ == "long"
    obi_flow_short = os_ == "short" and fs_ == "short"

    if   long_votes  >= 2 or obi_flow_long:  direction = "long"
    elif short_votes >= 2 or obi_flow_short: direction = "short"
    else:                                     direction = None

    details = {
        "obi":        obi.get("obi", 0.5),
        "obi_signal": os_,
        "bid_stack":  obi.get("bid_stack", False),
        "buy_ratio":  flow.get("buy_ratio", 0.5),
        "flow_signal": fs_,
        "accel_buy":  flow.get("accel_buy", False),
        "fund_rate":  fund.get("rate", 0),
        "fund_signal": fd_,
        "long_votes":  long_votes,
        "short_votes": short_votes,
    }
    return direction, details

# ═══════════════════════════════════════════════════════════════
#  SCORING — 0 to 100
# ═══════════════════════════════════════════════════════════════

def calc_score(direction: str, details: dict) -> tuple:
    score = 0
    obi       = details.get("obi", 0.5)
    buy_ratio = details.get("buy_ratio", 0.5)
    fund_sig  = details.get("fund_signal", "neutral")
    bid_stack = details.get("bid_stack", False)
    accel_buy = details.get("accel_buy", False)
    votes     = details.get("long_votes" if direction == "long" else "short_votes", 0)

    # OBI (0-35 pts)
    if direction == "long":
        # inverted: strong asks = good long signal
        obi_dist = max(0, 0.5 - obi) * 2
    else:
        # inverted: strong bids = good short signal
        obi_dist = max(0, obi - 0.5) * 2
    score += min(obi_dist * 35, 35)
    if bid_stack and direction == "short": score += 5  # inverted bonus

    # Flow (0-35 pts)
    if direction == "long":
        flow_dist = max(0, 0.5 - buy_ratio) * 2   # inverted
    else:
        flow_dist = max(0, buy_ratio - 0.5) * 2   # inverted
    score += min(flow_dist * 35, 35)
    if accel_buy and direction == "short": score += 5  # inverted bonus

    # Funding (0-20 pts) — not inverted
    if fund_sig == direction:   score += 20
    elif fund_sig == "neutral": score += 5

    # All 3 agree bonus
    if votes == 3: score += 10

    score = max(0, min(100, int(score)))

    if   score >= 80: label = "VERY STRONG"
    elif score >= 65: label = "STRONG"
    elif score >= 40: label = "MEDIUM"
    else:             label = "WEAK"

    return score, label

# ═══════════════════════════════════════════════════════════════
#  TP / SL — score driven (unchanged from original)
# ═══════════════════════════════════════════════════════════════

def get_tp_sl(score: int, entry: float, direction: str) -> dict:
    if   score >= 80: tp1_pct, tp2_pct, tp3_pct, sl_pct = 10.0, 25.0, 40.0, 4.0
    elif score >= 65: tp1_pct, tp2_pct, tp3_pct, sl_pct =  7.0, 18.0, 30.0, 3.5
    elif score >= 40: tp1_pct, tp2_pct, tp3_pct, sl_pct =  5.0, 12.0, 20.0, 3.0
    else:             tp1_pct, tp2_pct, tp3_pct, sl_pct =  3.0,  7.0, 12.0, 2.0

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
    if   score >= 80: pct = 25.0
    elif score >= 65: pct = 20.0
    elif score >= 40: pct = 15.0
    else:             pct = 10.0
    return min(balance * pct / 100, balance * 0.30)

# ═══════════════════════════════════════════════════════════════
#  POSITION MONITOR
# ═══════════════════════════════════════════════════════════════

async def monitor_position():
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
    if direction == "long":
        pos["unrealized_pct"] = round((price - entry) / entry * 100, 2)
    else:
        pos["unrealized_pct"] = round((entry - price) / entry * 100, 2)

    tp1 = tp_sl["tp1"]; tp2 = tp_sl["tp2"]; tp3 = tp_sl["tp3"]

    # TP3 — full close
    if not tp2_hit and ((direction == "long" and price >= tp3) or
                        (direction == "short" and price <= tp3)):
        print(f"🎯 TP3 {symbol} @ {price}")
        await close_position(symbol, round(qty * 0.20, 3), direction)
        await record_close(symbol, price, "win", "TP3")
        return

    # TP2 — close 40%
    if not tp2_hit and ((direction == "long" and price >= tp2) or
                        (direction == "short" and price <= tp2)):
        print(f"🎯 TP2 {symbol} @ {price}")
        await close_position(symbol, round(qty * 0.40, 3), direction)
        pos["tp2_hit"] = True
        pos["qty"]     = round(qty * 0.20, 3)

    # TP1 — close 40%
    if not tp1_hit and ((direction == "long" and price >= tp1) or
                        (direction == "short" and price <= tp1)):
        print(f"🎯 TP1 {symbol} @ {price}")
        await close_position(symbol, round(qty * 0.40, 3), direction)
        pos["tp1_hit"] = True
        pos["qty"]     = round(qty * 0.60, 3)

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
    state["trades"]    = state["trades"][:100]
    state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
    if result == "win": state["wins"]   += 1
    else:               state["losses"] += 1
    state["position"] = None

    print(f"{'✅ WIN' if result=='win' else '❌ LOSS'} {symbol} | "
          f"PnL={pnl:+.4f} | reason={reason}")

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
        print(f"Supabase error: {e}")

# ═══════════════════════════════════════════════════════════════
#  WATCHLIST + MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT",
    "ADAUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT",
]

async def bot_loop():
    print("🚀 APEX Pro (Original + Inverted) starting...")
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
            print(f"🔍 Scanning {len(WATCHLIST)} symbols | "
                  f"Balance: ${state['balance']:.2f}")

            for symbol in WATCHLIST:
                if state.get("position"):
                    break

                direction, details = await get_signal(symbol)
                if not direction:
                    await asyncio.sleep(1)
                    continue

                score, label = calc_score(direction, details)

                if score < 40:
                    print(f"📊 {symbol} score={score} too low — skip")
                    await asyncio.sleep(1)
                    continue

                ticker = await get_tickers(symbol)
                price  = float(ticker.get("lastPrice", 0))
                if price <= 0:
                    continue

                balance = state["balance"]
                size    = get_position_size(score, balance)
                qty     = round((size * LEVERAGE) / price, 3)

                if qty <= 0 or size < 5:
                    print(f"⚠️ Size too small ${size:.2f} — skip")
                    continue

                tp_sl = get_tp_sl(score, price, direction)
                resp  = await place_order(symbol, direction, qty,
                                          tp_sl["sl"], tp_sl["tp1"])

                if resp and resp.get("retCode") == 0:
                    state["position"] = {
                        "symbol":    symbol,
                        "direction": direction,
                        "entry":     price,
                        "qty":       qty,
                        "size_usdt": size,
                        "score":     score,
                        "label":     label,
                        "tp_sl":     tp_sl,
                        "tp1_hit":   False,
                        "tp2_hit":   False,
                        "opened_at": int(time.time()),
                        "details":   details,
                    }
                    state["status"] = "in_trade"
                    print(f"📈 ENTERED {symbol} {direction.upper()} | "
                          f"score={score} {label} | qty={qty} size=${size:.2f}")
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
#  API + DASHBOARD
# ═══════════════════════════════════════════════════════════════

@app.get("/api/state")
async def api_state():
    pos   = state.get("position")
    wins  = state["wins"]
    losses = state["losses"]
    total = wins + losses
    return JSONResponse({
        "balance":   round(state["balance"], 2),
        "total_pnl": state["total_pnl"],
        "wins":      wins,
        "losses":    losses,
        "win_rate":  round(wins / total * 100, 1) if total > 0 else 0,
        "trades":    state["trades"][:20],
        "position":  pos,
        "status":    state["status"],
        "last_scan": state["last_scan"],
        "inverted":  True,
    })

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(UI_HTML)

@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_loop())

UI_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX Pro</title>
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
.inv-bar{background:rgba(255,77,109,.06);border:1px solid rgba(255,77,109,.25);border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:#ff9ab0;font-family:'Space Mono',monospace}
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
.trade-row{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.trade-sym{font-family:'Space Mono',monospace;font-size:12px;font-weight:700}
.trade-score{font-size:10px;color:var(--muted);font-family:'Space Mono',monospace}
.trade-pnl{font-family:'Space Mono',monospace;font-size:13px;font-weight:700}
</style>
</head>
<body>
<div class="header">
  <div class="logo">APEX <span>Pro</span></div>
  <div class="badge"><span class="dot"></span> LIVE</div>
</div>
<div class="inv-bar">⚡ INVERTED MODE — Heavy bids = SHORT &nbsp;|&nbsp; Heavy sells = LONG</div>
<div class="grid2" id="summary"></div>
<div id="position-section"></div>
<div class="sec">All Trades</div>
<div class="card" id="trade-log">
  <div style="color:var(--muted);font-size:13px;text-align:center;padding:20px">No trades yet</div>
</div>
<script>
async function refresh(){
  try{
    const d = await fetch('/api/state').then(r=>r.json())
    const pnl = d.total_pnl
    const wr  = d.win_rate
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
    const sec = document.getElementById('position-section')
    if(d.position){
      const pos=d.position, tp=pos.tp_sl, dir=pos.direction
      const unreal=pos.unrealized_pct||0
      sec.innerHTML=`
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
        </div>`
    } else { sec.innerHTML='' }
    if(d.trades&&d.trades.length){
      document.getElementById('trade-log').innerHTML=d.trades.map(t=>{
        const isWin=t.result==='win'
        return `<div class="trade-row">
          <div>
            <div class="trade-sym">${t.symbol}</div>
            <div class="trade-score">${t.score} ${t.label} · ${t.reason||''}</div>
          </div>
          <div class="trade-pnl ${isWin?'green':'red'}">${isWin?'+':''}$${(t.pnl||0).toFixed(2)}</div>
        </div>`
      }).join('')
    }
  }catch(e){console.error(e)}
}
refresh()
setInterval(refresh,5000)
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
