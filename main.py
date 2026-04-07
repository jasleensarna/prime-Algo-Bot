# APEX Pro - Bybit Futures Bot
# Signal Engine: OBI + Flow + CVD (gate) + Whale + Funding (score boosters)
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn, os, time, asyncio, hmac, hashlib, urllib.parse, json, random
import httpx
try:
    import asyncpg
    HAS_DB = True
except (ImportError, Exception):
    HAS_DB = False
    asyncpg = None

app = FastAPI()

# ============================================================
#  PERSISTENT DATABASE
# ============================================================
DB_URL   = os.getenv("DATABASE_URL") or ""
_db_pool = None

async def db_connect():
    global _db_pool
    if not HAS_DB or not DB_URL:
        print("[!] No DATABASE_URL - history will not persist")
        return
    try:
        url = DB_URL.replace("postgres://", "postgresql://", 1)
        _db_pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
        await db_init()
        print("[OK] PostgreSQL connected - trade history persists forever")
    except Exception as e:
        print(f"[!] DB connect failed ({e}) - in-memory only")

async def db_init():
    if not _db_pool: return
    async with _db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY, symbol TEXT, direction TEXT, result TEXT,
                entry DOUBLE PRECISION, exit_price DOUBLE PRECISION,
                pnl DOUBLE PRECISION, pnl_pct DOUBLE PRECISION,
                usdt_size DOUBLE PRECISION, leverage INTEGER,
                tp1_pct DOUBLE PRECISION, tp2_pct DOUBLE PRECISION,
                tp3_pct DOUBLE PRECISION, sl_pct DOUBLE PRECISION,
                score INTEGER, label TEXT, mode TEXT,
                tp1_hit BOOLEAN DEFAULT FALSE, tp2_hit BOOLEAN DEFAULT FALSE,
                close_ts BIGINT, created_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                id INTEGER PRIMARY KEY DEFAULT 1,
                wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
                total_pnl DOUBLE PRECISION DEFAULT 0,
                long_count INTEGER DEFAULT 0, short_count INTEGER DEFAULT 0,
                traded INTEGER DEFAULT 0, updated_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        await conn.execute(
            "INSERT INTO bot_stats (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    print("DB tables ready")

async def db_save_trade(pos, result, exit_price):
    if not _db_pool: return
    try:
        async with _db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO trades (symbol,direction,result,entry,exit_price,pnl,pnl_pct,
                  usdt_size,leverage,tp1_pct,tp2_pct,tp3_pct,sl_pct,score,label,mode,
                  tp1_hit,tp2_hit,close_ts)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            """, pos.get("symbol"), pos.get("direction"), result,
                pos.get("entry", 0), exit_price, pos.get("pnl", 0),
                pos.get("pnl_pct", 0), pos.get("usdt_size", 0),
                pos.get("leverage", 2), pos.get("tp1_pct", 0),
                pos.get("tp2_pct", 0), pos.get("tp3_pct", 0),
                pos.get("sl_pct", 0), pos.get("score", 0),
                pos.get("label", ""), pos.get("mode", "lead"),
                bool(pos.get("tp1_hit", False)),
                bool(pos.get("tp2_hit", False)), int(time.time()))
            w  = 1 if result == "win"   else 0
            l  = 1 if result == "loss"  else 0
            lg = 1 if pos.get("direction") == "long"  else 0
            sh = 1 if pos.get("direction") == "short" else 0
            await conn.execute("""
                UPDATE bot_stats SET
                    wins=wins+$1, losses=losses+$2, total_pnl=total_pnl+$3,
                    long_count=long_count+$4, short_count=short_count+$5,
                    traded=traded+1, updated_at=NOW()
                WHERE id=1""", w, l, pos.get("pnl", 0), lg, sh)
    except Exception as e:
        print(f"DB save error: {e}")

async def db_load_trades(limit=100):
    if not _db_pool: return []
    try:
        async with _db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM trades ORDER BY close_ts DESC LIMIT $1", limit)
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"DB load error: {e}"); return []

async def db_load_stats():
    if not _db_pool: return {}
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM bot_stats WHERE id=1")
            return dict(row) if row else {}
    except Exception as e:
        print(f"DB stats error: {e}"); return {}

# ============================================================
#  STATE
# ============================================================
state = {
    "api_key":        os.getenv("BYBIT_API_KEY") or "",
    "api_secret":     os.getenv("BYBIT_API_SECRET") or "",
    "leverage":       2,
    "max_pos":        3,
    "trail_pct":      10.0,
    "max_daily_loss": 5.0,
    "bot_on":         True,
    "balance":        0.0,
    "positions":      {},
    "trades":         [],
    "pending":        [],
    "stats":          {"scanned":0,"signals":0,"traded":0,"long":0,"short":0},
    "wins":           0,
    "losses":         0,
    "today_pnl":      0.0,
    "total_pnl":      0.0,
    "daily_loss_hit": False,
    "signal_log":     [],
    "start_balance":  0.0,
    "risk_pct":       2.0,
    "signal_mode":    "medium",
    "min_score":      45,
    "use_mtf":        False,
}

TOP50     = []
BYBIT     = "https://api.bybit.com"
MIN_ORDER = 10.0

# ============================================================
#  BYBIT API
# ============================================================
def by_sign(secret, ts, params):
    msg = ts + state["api_key"] + "5000" + params
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

async def by_get(path, params={}, signed=False):
    p  = dict(params)
    ts = str(int(time.time() * 1000))
    qs = urllib.parse.urlencode(p)
    h  = {
        "X-BAPI-API-KEY":      state["api_key"],
        "X-BAPI-TIMESTAMP":    ts,
        "X-BAPI-RECV-WINDOW":  "5000",
    }
    if signed:
        h["X-BAPI-SIGN"] = by_sign(state["api_secret"], ts, qs)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{BYBIT}{path}", params=p, headers=h)
        return r.json()

async def by_post(path, body, signed=True):
    ts = str(int(time.time() * 1000))
    bs = json.dumps(body)
    h  = {
        "X-BAPI-API-KEY":      state["api_key"],
        "X-BAPI-TIMESTAMP":    ts,
        "X-BAPI-RECV-WINDOW":  "5000",
        "Content-Type":        "application/json",
    }
    if signed:
        h["X-BAPI-SIGN"] = by_sign(state["api_secret"], ts, bs)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{BYBIT}{path}", content=bs, headers=h)
        return r.json()

async def get_balance():
    try:
        d = await by_get(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED"}, signed=True)
        if d.get("retCode") == 0:
            for w in d.get("result", {}).get("list", []):
                for c in w.get("coin", []):
                    if c.get("coin") == "USDT":
                        b = float(c.get("walletBalance") or
                                  c.get("equity") or 0)
                        if b > 0: return b
    except: pass
    return 0.0

async def get_price(symbol):
    d = await by_get("/v5/market/tickers",
                     {"category": "linear", "symbol": symbol})
    items = d.get("result", {}).get("list", [])
    return float(items[0].get("lastPrice", 0)) if items else 0.0

async def set_leverage(symbol):
    try:
        lev = str(state["leverage"])
        await by_post("/v5/position/set-leverage", {
            "category":    "linear",
            "symbol":      symbol,
            "buyLeverage": lev,
            "sellLeverage":lev,
        })
    except: pass

async def place_futures_order(symbol, side, qty, tp=None, sl=None):
    body = {
        "category":    "linear",
        "symbol":      symbol,
        "side":        side,
        "orderType":   "Market",
        "qty":         str(qty),
        "timeInForce": "IOC",
    }
    if tp: body["takeProfit"] = str(tp)
    if sl: body["stopLoss"]   = str(sl)
    d = await by_post("/v5/order/create", body)
    if d.get("retCode") != 0:
        raise Exception(d.get("retMsg", f"Order error {d.get('retCode')}"))
    return d.get("result", {})

async def close_futures_position(symbol, side, qty):
    close_side = "Sell" if side == "long" else "Buy"
    body = {
        "category":    "linear",
        "symbol":      symbol,
        "side":        close_side,
        "orderType":   "Market",
        "qty":         str(qty),
        "timeInForce": "IOC",
        "reduceOnly":  True,
    }
    d = await by_post("/v5/order/create", body)
    if d.get("retCode") != 0:
        print(f"Close failed {symbol}: {d.get('retMsg')}")
    return d.get("result", {})

async def cancel_all_orders(symbol):
    try:
        await by_post("/v5/order/cancel-all",
                      {"category": "linear", "symbol": symbol})
    except: pass

async def fetch_all_symbols():
    try:
        d = await by_get("/v5/market/instruments-info",
                         {"category": "linear", "limit": 1000})
        syms = []
        for item in d.get("result", {}).get("list", []):
            if (item.get("status") == "Trading" and
                    item.get("settleCoin") == "USDT" and
                    item.get("symbol", "").endswith("USDT")):
                syms.append(item["symbol"])
        syms.sort()
        return syms
    except Exception as e:
        print(f"fetch_all_symbols error: {e}")
        return TOP50 or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

# ============================================================
#  LEAD SIGNAL 1 -- ORDER BOOK IMBALANCE (OBI)
#  Gate signal. Detects where liquidity is sitting RIGHT NOW.
#  OBI > 0.65 = buyers dominating = LONG
#  OBI < 0.35 = sellers dominating = SHORT
# ============================================================
async def get_orderbook_imbalance(symbol):
    try:
        d = await by_get("/v5/market/orderbook",
                         {"category": "linear", "symbol": symbol, "limit": 50})
        r    = d.get("result", {})
        bids = r.get("b", [])
        asks = r.get("a", [])
        if not bids or not asks:
            return {"obi": 0.5, "signal": "neutral"}
        bid_vol = sum(float(b[1]) for b in bids)
        ask_vol = sum(float(a[1]) for a in asks)
        total   = bid_vol + ask_vol
        if total == 0:
            return {"obi": 0.5, "signal": "neutral"}
        obi = bid_vol / total
        # Stacking check -- top 5 vs bottom 5 levels
        top5b = sum(float(b[1]) for b in bids[:5])
        bot5b = (sum(float(b[1]) for b in bids[-5:])
                 if len(bids) >= 10 else top5b)
        top5a = sum(float(a[1]) for a in asks[:5])
        bot5a = (sum(float(a[1]) for a in asks[-5:])
                 if len(asks) >= 10 else top5a)
        bid_stack = top5b > bot5b * 1.5
        ask_stack = top5a > bot5a * 1.5
        if   obi > 0.65 or (obi > 0.58 and bid_stack): signal = "long"
        elif obi < 0.35 or (obi < 0.42 and ask_stack): signal = "short"
        else:                                            signal = "neutral"
        return {
            "obi":       round(obi, 3),
            "signal":    signal,
            "bid_vol":   round(bid_vol, 2),
            "ask_vol":   round(ask_vol, 2),
            "bid_stack": bid_stack,
            "ask_stack": ask_stack,
        }
    except Exception as e:
        print(f"OBI error {symbol}: {e}")
        return {"obi": 0.5, "signal": "neutral"}

# ============================================================
#  LEAD SIGNAL 2 -- TRADE FLOW AGGRESSION
#  Gate signal. Counts actual executed buy vs sell volume.
#  Cannot be spoofed. Confirms OBI is genuine.
#  buy_ratio > 0.60 = aggressive buyers = LONG
#  buy_ratio < 0.40 = aggressive sellers = SHORT
# ============================================================
async def get_trade_flow_aggression(symbol):
    try:
        d = await by_get("/v5/market/recent-trade",
                         {"category": "linear", "symbol": symbol, "limit": 200})
        trades = d.get("result", {}).get("list", [])
        if not trades:
            return {"buy_ratio": 0.5, "signal": "neutral", "trade_count": 0}
        buy_vol  = sum(float(t["size"]) for t in trades
                       if t.get("side") == "Buy")
        sell_vol = sum(float(t["size"]) for t in trades
                       if t.get("side") == "Sell")
        total    = buy_vol + sell_vol
        if total == 0:
            return {"buy_ratio": 0.5, "signal": "neutral",
                    "trade_count": len(trades)}
        buy_ratio = buy_vol / total
        # Acceleration check -- newest 50 vs oldest 50
        recent = trades[:50]
        older  = trades[150:] if len(trades) >= 150 else []
        accel_buy = accel_sell = False
        if older:
            r_buy = sum(float(t["size"]) for t in recent
                        if t.get("side") == "Buy")
            r_tot = sum(float(t["size"]) for t in recent) or 1
            o_buy = sum(float(t["size"]) for t in older
                        if t.get("side") == "Buy")
            o_tot = sum(float(t["size"]) for t in older) or 1
            accel_buy  = (r_buy / r_tot) > (o_buy / o_tot) * 1.2
            accel_sell = (r_buy / r_tot) < (o_buy / o_tot) * 0.8
        if   buy_ratio > 0.60 or (buy_ratio > 0.53 and accel_buy):
            signal = "long"
        elif buy_ratio < 0.40 or (buy_ratio < 0.47 and accel_sell):
            signal = "short"
        else:
            signal = "neutral"
        return {
            "buy_ratio":   round(buy_ratio, 3),
            "signal":      signal,
            "trade_count": len(trades),
            "accel_buy":   accel_buy,
            "accel_sell":  accel_sell,
        }
    except Exception as e:
        print(f"Trade flow error {symbol}: {e}")
        return {"buy_ratio": 0.5, "signal": "neutral", "trade_count": 0}

# ============================================================
#  LEAD SIGNAL 3 -- CVD DIVERGENCE (strongest predictor)
#  Gate signal. Cumulative Volume Delta vs Price.
#
#  CVD = running total of (buy_vol - sell_vol) per candle
#  Divergence cases:
#    Price UP  + CVD FLAT/DOWN = weak rally, fake breakout -> SHORT
#    Price DOWN + CVD FLAT/UP  = weak selloff, fake breakdown -> LONG
#    Price FLAT + CVD RISING   = silent accumulation -> LONG imminent
#    Price FLAT + CVD FALLING  = silent distribution -> SHORT imminent
#
#  This catches moves BEFORE the candle forms.
# ============================================================
async def get_cvd_divergence(symbol):
    try:
        # Get last 20 one-minute klines for price trend
        kd = await by_get("/v5/market/kline", {
            "category": "linear",
            "symbol":   symbol,
            "interval": "1",
            "limit":    20,
        })
        klines = kd.get("result", {}).get("list", [])
        if len(klines) < 10:
            return {"signal": "neutral", "cvd_delta": 0, "price_delta": 0}

        klines = list(reversed(klines))  # oldest first
        closes = [float(k[4]) for k in klines]

        # Get recent trades to build CVD
        td = await by_get("/v5/market/recent-trade", {
            "category": "linear",
            "symbol":   symbol,
            "limit":    200,
        })
        trades = td.get("result", {}).get("list", [])
        if not trades:
            return {"signal": "neutral", "cvd_delta": 0, "price_delta": 0}

        trades = list(reversed(trades))  # oldest first

        # Split into two halves -- compare CVD trend vs price trend
        mid = len(trades) // 2
        first_half  = trades[:mid]
        second_half = trades[mid:]

        def cvd(t_list):
            b = sum(float(t["size"]) for t in t_list if t.get("side") == "Buy")
            s = sum(float(t["size"]) for t in t_list if t.get("side") == "Sell")
            return b - s

        cvd_first  = cvd(first_half)
        cvd_second = cvd(second_half)
        cvd_delta  = cvd_second - cvd_first  # positive = CVD rising

        # Price direction over same window
        price_start = closes[0] if closes else 0
        price_end   = closes[-1] if closes else 0
        price_delta = price_end - price_start  # positive = price rising

        # Normalize to detect divergence
        price_up   = price_delta  >  price_start * 0.001   # >0.1% up
        price_down = price_delta  < -price_start * 0.001   # >0.1% down
        price_flat = not price_up and not price_down

        cvd_rising  = cvd_delta > 0
        cvd_falling = cvd_delta < 0

        signal = "neutral"

        # Silent accumulation: price flat/down but CVD rising
        # Buyers absorbing every sell -> breakout up coming
        if (price_flat or price_down) and cvd_rising:
            signal = "long"

        # Silent distribution: price flat/up but CVD falling
        # Sellers absorbing every buy -> breakdown coming
        elif (price_flat or price_up) and cvd_falling:
            signal = "short"

        # Strong confirmation: price AND CVD agree
        elif price_up and cvd_rising:
            signal = "long"    # genuine uptrend confirmed
        elif price_down and cvd_falling:
            signal = "short"   # genuine downtrend confirmed

        return {
            "signal":      signal,
            "cvd_delta":   round(cvd_delta, 2),
            "price_delta": round(price_delta, 6),
            "cvd_rising":  cvd_rising,
            "price_up":    price_up,
            "divergence":  (price_up and cvd_falling) or
                           (price_down and cvd_rising),
        }
    except Exception as e:
        print(f"CVD error {symbol}: {e}")
        return {"signal": "neutral", "cvd_delta": 0, "price_delta": 0}

# ============================================================
#  SCORE BOOSTER 1 -- WHALE PRINT DETECTION
#  Not a gate. Adds +15 pts to score when detected.
#  A single trade > 3x average trade size = institutional order.
#  Whales buy BEFORE price moves, not with it.
# ============================================================
async def get_whale_print(symbol):
    try:
        d = await by_get("/v5/market/recent-trade", {
            "category": "linear",
            "symbol":   symbol,
            "limit":    100,
        })
        trades = d.get("result", {}).get("list", [])
        if len(trades) < 10:
            return {"detected": False, "direction": "neutral", "size_ratio": 0}

        sizes    = [float(t["size"]) for t in trades]
        avg_size = sum(sizes) / len(sizes)
        max_size = max(sizes)
        ratio    = max_size / avg_size if avg_size > 0 else 0

        if ratio >= 3.0:
            # Find the whale trade and its direction
            whale_trade = max(trades, key=lambda t: float(t["size"]))
            direction   = ("long"  if whale_trade.get("side") == "Buy"
                           else "short")
            # Recency check -- whale in last 20 trades = more relevant
            recent_idx  = next(
                (i for i, t in enumerate(trades)
                 if float(t["size"]) == max_size), len(trades))
            recency_bonus = recent_idx < 20

            return {
                "detected":      True,
                "direction":     direction,
                "size_ratio":    round(ratio, 1),
                "recency_bonus": recency_bonus,
            }
        return {"detected": False, "direction": "neutral", "size_ratio": round(ratio, 1)}
    except Exception as e:
        print(f"Whale error {symbol}: {e}")
        return {"detected": False, "direction": "neutral", "size_ratio": 0}

# ============================================================
#  SCORE BOOSTER 2 -- FUNDING RATE MOMENTUM
#  Not a gate. Adds +10 pts to score when aligned.
#  Rapidly negative funding = shorts being squeezed = LONG
#  Rapidly positive funding = longs being squeezed = SHORT
# ============================================================
async def get_funding_momentum(symbol):
    try:
        td    = await by_get("/v5/market/tickers",
                             {"category": "linear", "symbol": symbol})
        items = td.get("result", {}).get("list", [])
        current = float(items[0].get("fundingRate", 0)) * 100 if items else 0
        hd    = await by_get("/v5/market/funding/history",
                             {"category": "linear", "symbol": symbol, "limit": 10})
        hist  = hd.get("result", {}).get("list", [])
        rates = ([float(h["fundingRate"]) * 100 for h in hist]
                 if hist else [current])
        avg      = sum(rates) / len(rates) if rates else current
        momentum = current - avg
        if   current < -0.05 and momentum < -0.02: signal = "long"
        elif current >  0.05 and momentum >  0.02: signal = "short"
        else:                                       signal = "neutral"
        return {
            "rate":     round(current, 4),
            "avg":      round(avg, 4),
            "momentum": round(momentum, 4),
            "signal":   signal,
        }
    except Exception as e:
        print(f"Funding error {symbol}: {e}")
        return {"rate": 0, "avg": 0, "momentum": 0, "signal": "neutral"}

# ============================================================
#  MASTER SIGNAL ENGINE
#
#  GATE (must pass -- 2 of 3 minimum):
#    OBI + Flow + CVD
#    CVD required if only 2/3 agree (no CVD = no trade)
#
#  SCORE BOOSTERS (add pts, do not gate):
#    Whale print aligned  -> +15 pts
#    Funding aligned      -> +10 pts
#    All 3 gate agree     -> +10 pts (existing)
# ============================================================
async def master_signal(symbol):
    try:
        # Run all 5 indicators in parallel
        obi, flow, cvd, whale, fund = await asyncio.gather(
            get_orderbook_imbalance(symbol),
            get_trade_flow_aggression(symbol),
            get_cvd_divergence(symbol),
            get_whale_print(symbol),
            get_funding_momentum(symbol),
        )
    except Exception as e:
        print(f"Master signal error {symbol}: {e}")
        return None, {}

    obi_sig  = obi.get("signal",  "neutral")
    flow_sig = flow.get("signal", "neutral")
    cvd_sig  = cvd.get("signal",  "neutral")

    # Gate voting -- OBI + Flow + CVD
    gate_signals  = [obi_sig, flow_sig, cvd_sig]
    long_votes    = gate_signals.count("long")
    short_votes   = gate_signals.count("short")
    cvd_is_long   = cvd_sig == "long"
    cvd_is_short  = cvd_sig == "short"

    # Fire rules:
    #   All 3 agree                       -> fire
    #   OBI + Flow agree + CVD neutral    -> fire (strong combo)
    #   CVD + any one other agree         -> fire (CVD required)
    #   Only 2 agree but CVD disagrees    -> blocked
    obi_flow_long  = obi_sig  == "long"  and flow_sig == "long"
    obi_flow_short = obi_sig  == "short" and flow_sig == "short"
    cvd_obi_long   = cvd_is_long  and obi_sig  == "long"
    cvd_obi_short  = cvd_is_short and obi_sig  == "short"
    cvd_flow_long  = cvd_is_long  and flow_sig == "long"
    cvd_flow_short = cvd_is_short and flow_sig == "short"

    if long_votes == 3 or obi_flow_long or cvd_obi_long or cvd_flow_long:
        direction = "long"
    elif (short_votes == 3 or obi_flow_short or
          cvd_obi_short or cvd_flow_short):
        direction = "short"
    else:
        direction = None

    details = {
        # Gate signals
        "obi":          obi.get("obi", 0.5),
        "obi_signal":   obi_sig,
        "bid_stack":    obi.get("bid_stack", False),
        "buy_ratio":    flow.get("buy_ratio", 0.5),
        "flow_signal":  flow_sig,
        "accel_buy":    flow.get("accel_buy", False),
        "cvd_delta":    cvd.get("cvd_delta", 0),
        "cvd_signal":   cvd_sig,
        "cvd_diverge":  cvd.get("divergence", False),
        # Boosters
        "whale":        whale.get("detected", False),
        "whale_dir":    whale.get("direction", "neutral"),
        "whale_ratio":  whale.get("size_ratio", 0),
        "whale_recent": whale.get("recency_bonus", False),
        "funding_rate": fund.get("rate", 0),
        "fund_signal":  fund.get("signal", "neutral"),
        # Vote counts
        "long_votes":   long_votes,
        "short_votes":  short_votes,
    }
    return direction, details

# ============================================================
#  SIGNAL SCORER -- 0 to 100
#
#  Gate components (max 70 pts total):
#    OBI strength     -> 0-25 pts
#    Flow strength    -> 0-25 pts
#    CVD strength     -> 0-20 pts  (weighted higher)
#
#  Score boosters (max 35 pts):
#    Whale aligned    -> +15 pts
#    Whale recent     -> +5 extra
#    Funding aligned  -> +10 pts
#    All 3 gate agree -> +10 pts  (existing bonus)
#
#  CVD divergence bonus:
#    If price/CVD diverge (fake move detected) -> +5 pts extra
# ============================================================
def score_signal(direction, details):
    score = 0

    obi       = details.get("obi", 0.5)
    buy_ratio = details.get("buy_ratio", 0.5)
    cvd_delta = details.get("cvd_delta", 0)
    bid_stack = details.get("bid_stack", False)
    accel_buy = details.get("accel_buy", False)
    cvd_div   = details.get("cvd_diverge", False)
    whale     = details.get("whale", False)
    whale_dir = details.get("whale_dir", "neutral")
    whale_rec = details.get("whale_recent", False)
    fund_sig  = details.get("fund_signal", "neutral")
    long_votes = details.get("long_votes", 0)
    short_votes = details.get("short_votes", 0)
    votes     = long_votes if direction == "long" else short_votes

    # OBI (0-25 pts)
    obi_dist  = (max(0, obi - 0.5) * 2 if direction == "long"
                 else max(0, 0.5 - obi) * 2)
    score += min(obi_dist * 25, 25)
    if bid_stack and direction == "long":  score += 3
    if bid_stack and direction == "short": score -= 3

    # Flow (0-25 pts)
    flow_dist = (max(0, buy_ratio - 0.5) * 2 if direction == "long"
                 else max(0, 0.5 - buy_ratio) * 2)
    score += min(flow_dist * 25, 25)
    if accel_buy and direction == "long":  score += 3

    # CVD (0-20 pts) -- weighted highest of the 3 gate signals
    cvd_abs   = abs(cvd_delta)
    cvd_score = min(cvd_abs / 100.0 * 20, 20)  # scales with delta size
    if direction == "long"  and cvd_delta > 0: score += cvd_score
    if direction == "short" and cvd_delta < 0: score += cvd_score
    if cvd_div: score += 5  # divergence bonus -- fake move detected

    # Whale booster (+15, +5 recency)
    if whale and whale_dir == direction:
        score += 15
        if whale_rec: score += 5

    # Funding booster (+10)
    if fund_sig == direction: score += 10
    elif fund_sig == "neutral": score += 2

    # All 3 gate agree bonus (+10)
    if votes == 3: score += 10

    score = max(0, min(100, int(score)))

    if   score >= 80: label = "VERY STRONG"
    elif score >= 65: label = "STRONG"
    elif score >= 40: label = "MEDIUM"
    else:             label = "WEAK"

    return score, label

# ============================================================
#  DYNAMIC TP/SL -- score driven
#  WEAK:        TP1 +3%  TP2 +7%  TP3 +12%  SL -2%
#  MEDIUM:      TP1 +5%  TP2 +12% TP3 +20%  SL -3%
#  STRONG:      TP1 +7%  TP2 +18% TP3 +30%  SL -3.5%
#  VERY STRONG: TP1 +10% TP2 +25% TP3 +40%  SL -4%
# ============================================================
def get_dynamic_tp_sl(score, entry, signal):
    if   score >= 80:
        tp1_pct,tp2_pct,tp3_pct,sl_pct = 10.0,25.0,40.0,4.0
        label = "VERY STRONG"
    elif score >= 65:
        tp1_pct,tp2_pct,tp3_pct,sl_pct = 7.0,18.0,30.0,3.5
        label = "STRONG"
    elif score >= 40:
        tp1_pct,tp2_pct,tp3_pct,sl_pct = 5.0,12.0,20.0,3.0
        label = "MEDIUM"
    else:
        tp1_pct,tp2_pct,tp3_pct,sl_pct = 3.0,7.0,12.0,2.0
        label = "WEAK"
    if signal == "long":
        tp1 = round(entry * (1 + tp1_pct / 100), 6)
        tp2 = round(entry * (1 + tp2_pct / 100), 6)
        tp3 = round(entry * (1 + tp3_pct / 100), 6)
        sl  = round(entry * (1 - sl_pct  / 100), 6)
    else:
        tp1 = round(entry * (1 - tp1_pct / 100), 6)
        tp2 = round(entry * (1 - tp2_pct / 100), 6)
        tp3 = round(entry * (1 - tp3_pct / 100), 6)
        sl  = round(entry * (1 + sl_pct  / 100), 6)
    return {
        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl,
        "tp1_pct": tp1_pct, "tp2_pct": tp2_pct,
        "tp3_pct": tp3_pct, "sl_pct":  sl_pct,
        "label":   label,
    }

# ============================================================
#  DYNAMIC POSITION SIZE -- score driven, leverage fixed 2x
#  WEAK:        10% of balance (floor $10)
#  MEDIUM:      15% of balance (floor $15)
#  STRONG:      20% of balance (floor $20)
#  VERY STRONG: 25% of balance (floor $25)
#  Hard cap:    30% of balance
# ============================================================
def get_dynamic_position_size(score, balance):
    if   score >= 80: pct = 25.0
    elif score >= 65: pct = 20.0
    elif score >= 40: pct = 15.0
    else:             pct = 10.0
    size  = balance * pct / 100
    floor = max(10.0, pct * 0.4)
    cap   = balance * 0.30
    return round(min(max(size, floor), cap), 2)

# ============================================================
#  MARKET CONTEXT FILTERS -- OI + Liquidations
#  Still active as final gate after signal fires
# ============================================================
async def get_oi_trend(symbol):
    try:
        d = await by_get("/v5/market/open-interest", {
            "category":    "linear",
            "symbol":      symbol,
            "intervalTime":"5min",
            "limit":       10,
        })
        items = d.get("result", {}).get("list", [])
        if len(items) < 4: return "neutral"
        recent = float(items[0].get("openInterest", 0))
        older  = float(items[-1].get("openInterest", 0))
        if older == 0: return "neutral"
        chg = (recent - older) / older * 100
        if chg >  1: return "rising"
        if chg < -1: return "falling"
        return "neutral"
    except: return "neutral"

async def get_liquidation_bias(symbol):
    try:
        d = await by_get("/v5/market/liquidation", {
            "category": "linear",
            "symbol":   symbol,
            "limit":    50,
        })
        items = d.get("result", {}).get("list", [])
        if not items: return "neutral"
        lv = sum(float(i.get("size", 0)) * float(i.get("price", 0))
                 for i in items if i.get("side") == "Buy")
        sv = sum(float(i.get("size", 0)) * float(i.get("price", 0))
                 for i in items if i.get("side") == "Sell")
        t  = lv + sv
        if t == 0: return "neutral"
        if lv / t * 100 > 70: return "longs_liq"
        if sv / t * 100 > 70: return "shorts_liq"
        return "neutral"
    except: return "neutral"

async def get_market_context(symbol):
    oi, liq = "neutral", "neutral"
    try:
        r = await asyncio.gather(
            get_oi_trend(symbol),
            get_liquidation_bias(symbol),
            return_exceptions=True)
        if not isinstance(r[0], Exception): oi  = r[0]
        if not isinstance(r[1], Exception): liq = r[1]
    except: pass
    return {"oi_trend": oi, "liq_bias": liq}

def market_context_allows(signal, ctx):
    oi  = ctx.get("oi_trend", "neutral")
    liq = ctx.get("liq_bias",  "neutral")
    if signal == "long":
        if oi == "falling" and liq != "shorts_liq":
            return False, "OI falling - weak rally"
        if liq == "shorts_liq":
            return True, "shorts_liq - strong bullish"
        return True, f"OI={oi}"
    if signal == "short":
        if oi == "falling" and liq != "longs_liq":
            return False, "OI falling - weak drop"
        if liq == "longs_liq":
            return True, "longs_liq - strong bearish"
        return True, f"OI={oi}"
    return True, "ok"

# ============================================================
#  SCANNER
# ============================================================
async def scan_once():
    if state["daily_loss_hit"]: return
    try:
        all_syms = await fetch_all_symbols()
        if all_syms:
            TOP50.clear()
            TOP50.extend(all_syms)
        sample = random.sample(TOP50, min(20, len(TOP50)))
        state["stats"]["scanned"] = len(TOP50)
        print(f"Scan: {len(sample)} of {len(TOP50)} | "
              f"positions={len(state['positions'])}")
        for sym in sample:
            if sym in state["positions"]: continue
            if any(p["symbol"] == sym for p in state["pending"]): continue
            await analyse(sym)
            await asyncio.sleep(0.3)
        for sig in list(state["pending"]):
            if len(state["positions"]) >= state["max_pos"]: break
            if sig["symbol"] in state["positions"]:
                state["pending"] = [s for s in state["pending"]
                                    if s["symbol"] != sig["symbol"]]
                continue
            try:
                score = sig.get("score", 50)
                usdt  = max(
                    get_dynamic_position_size(score, state["balance"]),
                    MIN_ORDER)
                await execute_trade(sig["symbol"], sig["direction"],
                                    usdt, sig)
                state["pending"] = [s for s in state["pending"]
                                    if s["symbol"] != sig["symbol"]]
            except Exception as e:
                print(f"Trade failed {sig['symbol']}: {e}")
                state["pending"] = [s for s in state["pending"]
                                    if s["symbol"] != sig["symbol"]]
    except Exception as e:
        print(f"Scanner error: {e}")

async def analyse(symbol):
    try:
        if symbol in state["positions"]: return
        if any(p["symbol"] == symbol for p in state["pending"]): return

        direction, details = await master_signal(symbol)

        log = {
            "symbol":    symbol,
            "signal":    direction,
            "obi":       details.get("obi", 0.5),
            "buy_ratio": details.get("buy_ratio", 0.5),
            "cvd":       details.get("cvd_delta", 0),
            "whale":     details.get("whale", False),
            "funding":   details.get("funding_rate", 0),
            "ts":        int(time.time()),
        }
        state["signal_log"] = ([log] + state["signal_log"])[:30]

        if direction not in ("long", "short"): return
        if symbol in state["positions"]: return
        if any(p["symbol"] == symbol for p in state["pending"]): return

        ctx = await get_market_context(symbol)
        allowed, reason = market_context_allows(direction, ctx)
        if not allowed:
            print(f"MARKET BLOCK {symbol} [{direction}]: {reason}")
            return

        score, label = score_signal(direction, details)
        min_score = state.get("min_score", 45)
        if score < min_score:
            print(f"SCORE LOW {symbol}: {score}/100 < {min_score} ({state.get("signal_mode","medium")}) - skip")
            return

        price = await get_price(symbol)
        if price <= 0: return

        tp_sl    = get_dynamic_tp_sl(score, price, direction)
        est_size = get_dynamic_position_size(score, state["balance"])

        whale_tag = (f" WHALE {details.get('whale_ratio',0):.1f}x"
                     if details.get("whale") else "")
        cvd_tag   = (" CVD-DIV" if details.get("cvd_diverge") else "")

        state["pending"].append({
            "symbol":    symbol,
            "direction": direction,
            "price":     price,
            "tp1":       tp_sl["tp1"],
            "tp2":       tp_sl["tp2"],
            "tp3":       tp_sl["tp3"],
            "sl":        tp_sl["sl"],
            "tp1_pct":   tp_sl["tp1_pct"],
            "tp2_pct":   tp_sl["tp2_pct"],
            "tp3_pct":   tp_sl["tp3_pct"],
            "sl_pct":    tp_sl["sl_pct"],
            "score":     score,
            "label":     label,
            "est_size":  est_size,
            "obi":       details.get("obi", 0.5),
            "buy_ratio": details.get("buy_ratio", 0.5),
            "cvd_delta": details.get("cvd_delta", 0),
            "whale":     details.get("whale", False),
            "funding":   details.get("funding_rate", 0),
            "oi":        ctx.get("oi_trend", "neutral"),
            "liq":       ctx.get("liq_bias",  "neutral"),
            "mode":      "lead",
            "ts":        int(time.time()),
        })
        state["stats"]["signals"] += 1

        print(
            f"SIGNAL [{label} {score}/100]{whale_tag}{cvd_tag} "
            f"{direction.upper()} {symbol} @ ${price} "
            f"size=${est_size:.0f} | "
            f"OBI={details.get('obi',0.5):.3f} "
            f"flow={details.get('buy_ratio',0.5):.3f} "
            f"CVD={details.get('cvd_delta',0):.1f} "
            f"TP1=+{tp_sl['tp1_pct']}% "
            f"TP2=+{tp_sl['tp2_pct']}% "
            f"TP3=+{tp_sl['tp3_pct']}% "
            f"SL=-{tp_sl['sl_pct']}%"
        )
    except Exception as e:
        print(f"Analyse {symbol}: {e}")

# ============================================================
#  TRADE EXECUTION
# ============================================================
async def execute_trade(symbol, direction, usdt_amount, sig=None):
    if symbol in state["positions"]:
        raise Exception("Already in position")
    await set_leverage(symbol)
    await asyncio.sleep(0.3)
    price = await get_price(symbol)
    if price <= 0: raise Exception("Cannot get price")
    qty = round((usdt_amount * state["leverage"]) / price, 3)
    if qty <= 0: raise Exception("Qty too small")
    side  = "Buy" if direction == "long" else "Sell"
    score = sig.get("score", 50) if sig else 50
    tp_sl = get_dynamic_tp_sl(score, price, direction)
    await place_futures_order(symbol, side, qty, sl=tp_sl["sl"])
    if direction == "long": state["stats"]["long"]  += 1
    else:                   state["stats"]["short"] += 1
    state["stats"]["traded"] += 1
    print(
        f"TRADE OPEN [{tp_sl['label']} {score}/100] "
        f"{direction.upper()} {symbol} qty={qty} @ ${price} "
        f"TP1=+{tp_sl['tp1_pct']}% TP2=+{tp_sl['tp2_pct']}% "
        f"TP3=+{tp_sl['tp3_pct']}% SL=-{tp_sl['sl_pct']}%"
    )
    state["positions"][symbol] = {
        "symbol":      symbol,
        "direction":   direction,
        "entry":       price,
        "current":     price,
        "qty":         qty,
        "qty_remaining": qty,
        "usdt_size":   usdt_amount,
        "tp1":         tp_sl["tp1"],
        "tp2":         tp_sl["tp2"],
        "tp3":         tp_sl["tp3"],
        "sl":          tp_sl["sl"],
        "tp1_pct":     tp_sl["tp1_pct"],
        "tp2_pct":     tp_sl["tp2_pct"],
        "tp3_pct":     tp_sl["tp3_pct"],
        "sl_pct":      tp_sl["sl_pct"],
        "score":       score,
        "label":       tp_sl["label"],
        "highest":     price,
        "lowest":      price,
        "tp1_hit":     False,
        "tp2_hit":     False,
        "pnl":         0.0,
        "pnl_pct":     0.0,
        "mode":        "lead",
        "leverage":    state["leverage"],
        "entry_ts":    int(time.time()),
    }
    state["balance"] = await get_balance()

# ============================================================
#  POSITION MONITOR -- every 5 seconds
#  TP1 -> close 40% | TP2 -> close 40% | TP3 -> close 20%
#  Trailing SL updated live on Bybit
# ============================================================
async def monitor_positions():
    while True:
        if (state["bot_on"] and state["api_key"]
                and state["positions"]):
            for sym in list(state["positions"]):
                try:
                    pos       = state["positions"][sym]
                    price     = await get_price(sym)
                    if price <= 0: continue
                    pos["current"] = price
                    orig_qty  = pos["qty"]
                    qty_rem   = pos.get("qty_remaining", orig_qty)
                    direction = pos["direction"]

                    if direction == "long":
                        pos["pnl"] = (price - pos["entry"]) * qty_rem
                        pos["pnl_pct"] = ((price - pos["entry"]) /
                                          pos["entry"] * 100 *
                                          pos["leverage"])
                        # Trailing SL
                        if price > pos["highest"]:
                            pos["highest"] = price
                            new_sl = round(
                                price * (1 - state["trail_pct"] / 100), 6)
                            if new_sl > pos["sl"]:
                                pos["sl"] = new_sl
                                try:
                                    await by_post(
                                        "/v5/position/trading-stop", {
                                            "category":    "linear",
                                            "symbol":      sym,
                                            "stopLoss":    str(new_sl),
                                            "slTriggerBy": "LastPrice",
                                            "positionIdx": 0,
                                        })
                                    print(f"Trail SL up {sym} SL={new_sl}")
                                except: pass
                        # TP1 -> close 40%
                        if not pos["tp1_hit"] and price >= pos["tp1"]:
                            pos["tp1_hit"] = True
                            cq = round(orig_qty * 0.4, 3)
                            if cq > 0:
                                pos["qty_remaining"] = round(qty_rem - cq, 3)
                                ppnl = ((price - pos["entry"]) *
                                        cq * pos["leverage"])
                                state["today_pnl"] += ppnl
                                state["total_pnl"] += ppnl
                                await close_futures_position(sym, "long", cq)
                                print(f"TP1 LONG {sym} @{price} "
                                      f"closed 40% pnl=${ppnl:.2f}")
                        # TP2 -> close 40%
                        elif (pos["tp1_hit"] and not pos["tp2_hit"]
                              and price >= pos["tp2"]):
                            pos["tp2_hit"] = True
                            cq = min(round(orig_qty * 0.4, 3),
                                     pos["qty_remaining"])
                            if cq > 0:
                                pos["qty_remaining"] = round(
                                    pos["qty_remaining"] - cq, 3)
                                ppnl = ((price - pos["entry"]) *
                                        cq * pos["leverage"])
                                state["today_pnl"] += ppnl
                                state["total_pnl"] += ppnl
                                await close_futures_position(sym, "long", cq)
                                print(f"TP2 LONG {sym} @{price} "
                                      f"closed 40% pnl=${ppnl:.2f}")
                        # TP3 -> close final 20%
                        elif (pos["tp1_hit"] and pos["tp2_hit"]
                              and price >= pos["tp3"]):
                            print(f"TP3 LONG {sym} @{price} final 20%")
                            await close_position_and_record(sym, "win")
                            continue
                        # SL backup
                        if sym in state["positions"] and price <= pos["sl"]:
                            print(f"SL LONG {sym} @{price}")
                            await close_position_and_record(sym, "loss")
                            continue

                    else:  # SHORT
                        pos["pnl"] = (pos["entry"] - price) * qty_rem
                        pos["pnl_pct"] = ((pos["entry"] - price) /
                                          pos["entry"] * 100 *
                                          pos["leverage"])
                        # Trailing SL
                        if price < pos["lowest"]:
                            pos["lowest"] = price
                            new_sl = round(
                                price * (1 + state["trail_pct"] / 100), 6)
                            if new_sl < pos["sl"]:
                                pos["sl"] = new_sl
                                try:
                                    await by_post(
                                        "/v5/position/trading-stop", {
                                            "category":    "linear",
                                            "symbol":      sym,
                                            "stopLoss":    str(new_sl),
                                            "slTriggerBy": "LastPrice",
                                            "positionIdx": 0,
                                        })
                                    print(f"Trail SL down {sym} SL={new_sl}")
                                except: pass
                        # TP1 -> close 40%
                        if not pos["tp1_hit"] and price <= pos["tp1"]:
                            pos["tp1_hit"] = True
                            cq = round(orig_qty * 0.4, 3)
                            if cq > 0:
                                pos["qty_remaining"] = round(qty_rem - cq, 3)
                                ppnl = ((pos["entry"] - price) *
                                        cq * pos["leverage"])
                                state["today_pnl"] += ppnl
                                state["total_pnl"] += ppnl
                                await close_futures_position(sym, "short", cq)
                                print(f"TP1 SHORT {sym} @{price} "
                                      f"closed 40% pnl=${ppnl:.2f}")
                        # TP2 -> close 40%
                        elif (pos["tp1_hit"] and not pos["tp2_hit"]
                              and price <= pos["tp2"]):
                            pos["tp2_hit"] = True
                            cq = min(round(orig_qty * 0.4, 3),
                                     pos["qty_remaining"])
                            if cq > 0:
                                pos["qty_remaining"] = round(
                                    pos["qty_remaining"] - cq, 3)
                                ppnl = ((pos["entry"] - price) *
                                        cq * pos["leverage"])
                                state["today_pnl"] += ppnl
                                state["total_pnl"] += ppnl
                                await close_futures_position(sym, "short", cq)
                                print(f"TP2 SHORT {sym} @{price} "
                                      f"closed 40% pnl=${ppnl:.2f}")
                        # TP3 -> close final 20%
                        elif (pos["tp1_hit"] and pos["tp2_hit"]
                              and price <= pos["tp3"]):
                            print(f"TP3 SHORT {sym} @{price} final 20%")
                            await close_position_and_record(sym, "win")
                            continue
                        # SL backup
                        if sym in state["positions"] and price >= pos["sl"]:
                            print(f"SL SHORT {sym} @{price}")
                            await close_position_and_record(sym, "loss")
                            continue

                except Exception as e:
                    print(f"Monitor {sym}: {e}")

        check_daily_loss()
        await asyncio.sleep(5)

async def close_position_and_record(symbol, result):
    pos = state["positions"].get(symbol)
    if not pos: return
    try:
        qty_rem    = pos.get("qty_remaining", pos["qty"])
        await close_futures_position(symbol, pos["direction"], qty_rem)
        await cancel_all_orders(symbol)
        pnl        = pos["pnl"]
        exit_price = pos.get("current", pos.get("entry", 0))
        state["today_pnl"] += pnl
        state["total_pnl"] += pnl
        if result == "win": state["wins"]   += 1
        else:               state["losses"] += 1
        trade = {**pos, "result": result, "close_ts": int(time.time())}
        state["trades"].insert(0, trade)
        del state["positions"][symbol]
        state["balance"] = await get_balance()
        print(f"{result.upper()}: {symbol} pnl=${pnl:.2f}")
        await db_save_trade(trade, result, exit_price)
    except Exception as e:
        print(f"Close error {symbol}: {e}")

def check_daily_loss():
    if state["start_balance"] <= 0: return
    loss_pct = ((state["start_balance"] - state["balance"]) /
                state["start_balance"] * 100)
    if loss_pct >= state["max_daily_loss"] and not state["daily_loss_hit"]:
        state["daily_loss_hit"] = True
        state["bot_on"]         = False
        print(f"DAILY LOSS LIMIT HIT: {loss_pct:.1f}% - bot paused")

# ============================================================
#  BOT LOOP
# ============================================================
async def bot_loop():
    print("Bot loop started - OBI + Flow + CVD + Whale + Funding")
    while True:
        if (state["bot_on"] and state["api_key"]
                and not state["daily_loss_hit"]):
            await scan_once()
            try: state["balance"] = await get_balance()
            except: pass
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    await db_connect()
    saved = await db_load_trades(100)
    if saved:
        state["trades"] = saved
        print(f"Loaded {len(saved)} trades from DB")
    stats = await db_load_stats()
    if stats:
        state["wins"]            = stats.get("wins", 0)
        state["losses"]          = stats.get("losses", 0)
        state["total_pnl"]       = stats.get("total_pnl", 0.0)
        state["stats"]["traded"] = stats.get("traded", 0)
        state["stats"]["long"]   = stats.get("long_count", 0)
        state["stats"]["short"]  = stats.get("short_count", 0)
        print(f"Restored: W={state['wins']} L={state['losses']} "
              f"PnL=${state['total_pnl']:.2f}")
    print(f"APEX Pro starting - key:{bool(state['api_key'])}")
    if state["api_key"]:
        try:
            state["balance"]       = await get_balance()
            state["start_balance"] = state["balance"]
            print(f"Balance: ${state['balance']:.2f}")
        except Exception as e:
            print(f"Balance error: {e}")
    syms = await fetch_all_symbols()
    if syms:
        TOP50.clear()
        TOP50.extend(syms)
        print(f"Loaded {len(TOP50)} symbols from Bybit")
    asyncio.create_task(bot_loop())
    asyncio.create_task(monitor_positions())

# ============================================================
#  API ROUTES
# ============================================================
@app.post("/api/connect")
async def connect(req: Request):
    b = await req.json()
    state["api_key"]        = b.get("api_key", "").strip()
    state["api_secret"]     = b.get("api_secret", "").strip()
    state["leverage"]       = int(b.get("leverage", 2))
    state["max_pos"]        = int(b.get("max_pos", 3))
    state["trail_pct"]      = float(b.get("trail_pct", 10.0))
    state["max_daily_loss"] = float(b.get("max_daily_loss", 5.0))
    state["risk_pct"]       = float(b.get("risk_pct", 2.0))
    state["balance"]        = await get_balance()
    state["start_balance"]  = state["balance"]
    state["daily_loss_hit"] = False
    state["signal_mode"]    = b.get("signal_mode", state.get("signal_mode", "medium"))
    mode_scores = {"lenient": 30, "medium": 45, "strict": 60}
    state["min_score"]      = mode_scores.get(state["signal_mode"], 45)
    state["bot_on"]         = True
    print(f"Connected - balance: ${state['balance']:.2f} mode={state['signal_mode']} min_score={state['min_score']}")
    return {"ok": True, "balance": state["balance"]}

@app.get("/api/status")
async def get_status():
    t   = state["wins"] + state["losses"]
    dlp = 0
    if state["start_balance"] > 0:
        dlp = round(
            (state["start_balance"] - state["balance"]) /
            state["start_balance"] * 100, 2)
    return {
        "balance":        round(state["balance"], 2),
        "today_pnl":      round(state["today_pnl"], 2),
        "total_pnl":      round(state["total_pnl"], 2),
        "bot_on":         state["bot_on"],
        "signal_mode":    "medium",
    "min_score":      45,
        "mode_desc":      {
            "lenient": "Lenient - more signals, score >= 30",
            "medium":  "Medium - balanced, score >= 45",
            "strict":  "Strict - high conviction, score >= 60",
        }.get(state["signal_mode"], "Medium - balanced, score >= 45"),
        "min_score":      state.get("min_score", 45),
        "leverage":       state["leverage"],
        "trail_pct":      state["trail_pct"],
        "max_daily_loss": state["max_daily_loss"],
        "use_mtf":        False,
        "daily_loss_hit": state["daily_loss_hit"],
        "daily_loss_pct": dlp,
        "positions":      [
            {**p, "score": p.get("score", 0), "label": p.get("label", "--")}
            for p in state["positions"].values()
        ],
        "trades":         state["trades"][:20],
        "pending":        state["pending"][:3],
        "stats":          state["stats"],
        "wins":           state["wins"],
        "losses":         state["losses"],
        "win_rate":       round(state["wins"] / t * 100) if t else 0,
        "signal_log":     state["signal_log"][:10],
    }

@app.post("/api/bot/toggle")
async def toggle():
    state["bot_on"] = not state["bot_on"]
    if state["bot_on"]: state["daily_loss_hit"] = False
    return {"bot_on": state["bot_on"]}

@app.post("/api/mode/{mode}")
async def set_mode(mode: str):
    MODE_CONFIG = {
        "lenient": {"min_score": 30,  "desc": "Lenient - more signals, lower conviction threshold"},
        "medium":  {"min_score": 45,  "desc": "Medium - balanced (recommended)"},
        "strict":  {"min_score": 60,  "desc": "Strict - high conviction only, fewer signals"},
    }
    cfg = MODE_CONFIG.get(mode, MODE_CONFIG["medium"])
    state["signal_mode"] = mode
    state["min_score"]   = cfg["min_score"]
    state["pending"]     = []  # clear pending signals on mode change
    print(f"Mode: {mode.upper()} min_score={cfg['min_score']}")
    return {"ok": True, "mode": mode, "desc": cfg["desc"]}

@app.post("/api/settings")
async def update_settings(req: Request):
    b = await req.json()
    if "leverage"       in b: state["leverage"]       = int(b["leverage"])
    if "trail_pct"      in b: state["trail_pct"]      = float(b["trail_pct"])
    if "max_daily_loss" in b: state["max_daily_loss"] = float(b["max_daily_loss"])
    if "risk_pct"       in b: state["risk_pct"]       = float(b["risk_pct"])
    if "max_pos"        in b: state["max_pos"]        = int(b["max_pos"])
    return {"ok": True}

@app.post("/api/close/{symbol}")
async def close_pos(symbol: str):
    pos = state["positions"].get(symbol)
    if not pos:
        return JSONResponse({"error": "Not found"}, 400)
    try:
        await close_position_and_record(
            symbol, "win" if pos["pnl"] >= 0 else "loss")
        return {"ok": True, "pnl": pos.get("pnl", 0)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 400)

@app.post("/api/reset_daily")
async def reset_daily():
    state["daily_loss_hit"] = False
    state["bot_on"]         = True
    state["start_balance"]  = state["balance"]
    state["today_pnl"]      = 0.0
    return {"ok": True}

@app.get("/")
async def root():
    with open("index.html") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", 8000)))
