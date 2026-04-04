"""
APEX Pro — Bybit Linear Futures Edition
- LONG + SHORT signals via PrimeFlow Algo
- Top 50 coins by market cap on Bybit Futures
- 2x leverage
- Trailing SL 10% below highest price reached
- SL monitored every 5 seconds
- Max daily loss limit
- Multi-timeframe: 5m + 30m confirmation
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn, os, time, asyncio, hmac, hashlib, urllib.parse, json, random
import httpx

app = FastAPI()

state = {
    "api_key":    os.getenv("BYBIT_API_KEY", ""),
    "api_secret": os.getenv("BYBIT_API_SECRET", ""),
    "risk_pct":   2.0,       # % of balance per trade
    "leverage":   2,          # 2x leverage
    "max_pos":    3,          # max concurrent positions
    "signal_mode": "medium",  # lenient | medium | strict
    "trail_pct":  10.0,       # trailing SL: 10% below highest
    "max_daily_loss": 5.0,    # pause bot if daily loss > 5%
    "use_mtf":    True,       # multi-timeframe confirmation
    "bot_on":     True,
    "balance":    0.0,
    "positions":  {},
    "trades":     [],
    "pending":    [],
    "stats":      {"scanned": 0, "signals": 0, "traded": 0, "long": 0, "short": 0},
    "wins":       0,
    "losses":     0,
    "today_pnl":  0.0,
    "total_pnl":  0.0,
    "daily_loss_hit": False,
    "signal_log": [],
    "start_balance": 0.0,
}

MODES = {
    "lenient": {
        "ema_fast": 9, "ema_slow": 21, "ema_trend": 50,
        "st_factor": 2.0, "st_period": 10,
        "rsi_ob": 75, "rsi_os": 25,
        "require_macd": False, "require_volume": False,
        "require_bull_candle": False,
        "desc": "EMA9/21/50 · Supertrend only · More signals"
    },
    "medium": {
        "ema_fast": 13, "ema_slow": 34, "ema_trend": 100,
        "st_factor": 2.5, "st_period": 10,
        "rsi_ob": 68, "rsi_os": 32,
        "require_macd": True, "require_volume": True,
        "require_bull_candle": True,
        "desc": "EMA13/34/100 · MACD + Volume · Balanced"
    },
    "strict": {
        "ema_fast": 13, "ema_slow": 34, "ema_trend": 200,
        "st_factor": 3.0, "st_period": 10,
        "rsi_ob": 62, "rsi_os": 38,
        "require_macd": True, "require_volume": True,
        "require_bull_candle": True,
        "desc": "EMA13/34/200 · All filters · Fewer signals"
    },
}

# Top 50 coins by market cap — Bybit Linear Futures symbols
TOP50 = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","LTCUSDT","UNIUSDT","ATOMUSDT","ETCUSDT",
    "XLMUSDT","BCHUSDT","ALGOUSDT","VETUSDT","FILUSDT",
    "AAVEUSDT","APTUSDT","ARBUSDT","OPUSDT","SUIUSDT",
    "INJUSDT","TONUSDT","SEIUSDT","TIAUSDT","STXUSDT",
    "RUNEUSDT","LDOUSDT","MKRUSDT","SNXUSDT","COMPUSDT",
    "CRVUSDT","SANDUSDT","MANAUSDT","AXSUSDT","GALAUSDT",
    "NEARUSDT","FTMUSDT","FLOWUSDT","EGLDUSDT","THETAUSDT",
    "XTZUSDT","KSMUSDT","WAVESUSDT","ZILUSDT","IOTAUSDT",
]

BYBIT = "https://api.bybit.com"
MIN_ORDER = 12.0  # minimum USDT per trade

# ── BYBIT API ─────────────────────────────────────────────────
def by_sign(secret, timestamp, params):
    msg = timestamp + state["api_key"] + "5000" + params
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

async def by_get(path, params={}, signed=False):
    p = dict(params); ts = str(int(time.time() * 1000)); qs = urllib.parse.urlencode(p)
    headers = {"X-BAPI-API-KEY": state["api_key"], "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": "5000"}
    if signed: headers["X-BAPI-SIGN"] = by_sign(state["api_secret"], ts, qs)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{BYBIT}{path}", params=p, headers=headers); return r.json()

async def by_post(path, body, signed=True):
    ts = str(int(time.time() * 1000)); body_str = json.dumps(body)
    headers = {"X-BAPI-API-KEY": state["api_key"], "X-BAPI-TIMESTAMP": ts,
               "X-BAPI-RECV-WINDOW": "5000", "Content-Type": "application/json"}
    if signed: headers["X-BAPI-SIGN"] = by_sign(state["api_secret"], ts, body_str)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{BYBIT}{path}", content=body_str, headers=headers); return r.json()

async def get_balance():
    try:
        data = await by_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}, signed=True)
        if data.get("retCode") == 0:
            for wallet in data.get("result", {}).get("list", []):
                for c in wallet.get("coin", []):
                    if c.get("coin") == "USDT":
                        bal = float(c.get("walletBalance") or c.get("equity") or 0)
                        if bal > 0: return bal
    except: pass
    return 0.0

async def get_price(symbol):
    data = await by_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    items = data.get("result", {}).get("list", [])
    return float(items[0].get("lastPrice", 0)) if items else 0.0

async def get_klines(symbol, interval="30", limit=220):
    data = await by_get("/v5/market/kline", {
        "category": "linear", "symbol": symbol, "interval": interval, "limit": limit
    })
    return data.get("result", {}).get("list", []) if data.get("retCode") == 0 else []

async def set_leverage(symbol):
    try:
        lev = str(state["leverage"])
        await by_post("/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": lev, "sellLeverage": lev
        })
    except: pass

async def place_futures_order(symbol, side, qty, order_type="Market",
                              price=None, tp=None, sl=None, reduce_only=False):
    body = {
        "category": "linear", "symbol": symbol,
        "side": side, "orderType": order_type,
        "qty": str(qty), "timeInForce": "GTC" if order_type == "Limit" else "IOC",
    }
    if price: body["price"] = str(price)
    if tp:    body["takeProfit"] = str(tp)
    if sl:    body["stopLoss"]   = str(sl)
    if reduce_only: body["reduceOnly"] = True
    data = await by_post("/v5/order/create", body)
    if data.get("retCode") != 0:
        raise Exception(data.get("retMsg", f"Order error: {data.get('retCode')}"))
    return data.get("result", {})

async def close_futures_position(symbol, side, qty):
    """Close by placing reduce-only market order in opposite direction"""
    close_side = "Sell" if side == "long" else "Buy"
    body = {
        "category": "linear", "symbol": symbol,
        "side": close_side, "orderType": "Market",
        "qty": str(qty), "timeInForce": "IOC",
        "reduceOnly": True,
    }
    data = await by_post("/v5/order/create", body)
    if data.get("retCode") != 0:
        print(f"Close failed {symbol}: {data.get('retMsg')}")
    return data.get("result", {})

async def cancel_all_orders(symbol):
    try:
        await by_post("/v5/order/cancel-all", {"category": "linear", "symbol": symbol})
    except: pass

# ── INDICATORS ────────────────────────────────────────────────
def calc_ema(values, period):
    if len(values) < period: return values[-1] if values else 0
    k = 2 / (period + 1); e = sum(values[:period]) / period
    for v in values[period:]: e = v * k + e * (1 - k)
    return e

def calc_sma(values, period):
    if not values: return 0
    return sum(values[-period:]) / min(len(values), period)

def calc_rsi_wilder(closes, period=14):
    if len(closes) < period + 1: return 50.0
    d = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    ag = sum(max(x, 0) for x in d[:period]) / period
    al = sum(max(-x, 0) for x in d[:period]) / period
    for x in d[period:]:
        ag = (ag * (period - 1) + max(x, 0)) / period
        al = (al * (period - 1) + max(-x, 0)) / period
    return 100.0 if al == 0 else 100 - (100 / (1 + ag / al))

def calc_atr_wilder(highs, lows, closes, period=14):
    if len(closes) < 2: return 0
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    if not trs: return 0
    a = sum(trs[:period]) / min(len(trs), period)
    for t in trs[period:]: a = (a * (period - 1) + t) / period
    return a

def calc_supertrend(highs, lows, closes, factor, period):
    n = len(closes)
    if n < period + 2: return True
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, n)]
    atr_s = []; a = sum(trs[:period]) / period; atr_s.append(a)
    for t in trs[period:]: a = (a * (period - 1) + t) / period; atr_s.append(a)
    prev_upper = prev_lower = 0; last_dir = 1
    for i in range(len(atr_s)):
        ci = i + period
        if ci >= n: break
        hl2 = (highs[ci] + lows[ci]) / 2
        bu = hl2 + factor * atr_s[i]; bl = hl2 - factor * atr_s[i]
        fu = bu if bu < prev_upper or closes[ci-1] > prev_upper else prev_upper
        fl = bl if bl > prev_lower or closes[ci-1] < prev_lower else prev_lower
        if closes[ci] > fu: last_dir = -1
        elif closes[ci] < fl: last_dir = 1
        prev_upper = fu; prev_lower = fl
    return last_dir < 0

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return 0, 0
    ms = [calc_ema(closes[:i], fast) - calc_ema(closes[:i], slow)
          for i in range(slow, len(closes) + 1)]
    if len(ms) < signal: return ms[-1], ms[-1]
    return ms[-1], calc_ema(ms, signal)

def ema_crossover(closes, pf, ps):
    if len(closes) < 2: return False, False
    fn = calc_ema(closes, pf); sn = calc_ema(closes, ps)
    fp = calc_ema(closes[:-1], pf); sp = calc_ema(closes[:-1], ps)
    return fp <= sp and fn > sn, fp >= sp and fn < sn

def primeflow_signal(opens, highs, lows, closes, volumes, mode):
    cfg = MODES[mode]
    ef, es, et = cfg["ema_fast"], cfg["ema_slow"], cfg["ema_trend"]
    if len(closes) < et + 30: return None, {}

    ema_trend = calc_ema(closes, et)
    cross_up, cross_down = ema_crossover(closes, ef, es)
    st_bull = calc_supertrend(highs, lows, closes, cfg["st_factor"], cfg["st_period"])
    rsi_val = calc_rsi_wilder(closes, 14)
    atr_val = calc_atr_wilder(highs, lows, closes, 14)
    bull_trend = closes[-1] > ema_trend
    bear_trend = closes[-1] < ema_trend
    bull_candle = closes[-1] > opens[-1]
    bear_candle = closes[-1] < opens[-1]
    vol_sma = calc_sma(volumes, 20)
    vol_spike = volumes[-1] > vol_sma * 1.2
    macd_line, signal_line = calc_macd(closes)
    macd_bull = macd_line > signal_line
    macd_bear = macd_line < signal_line

    details = {
        "rsi": round(rsi_val, 1), "atr": atr_val,
        "st_bull": st_bull, "macd_bull": macd_bull,
        "vol_spike": vol_spike,
        "vol_ratio": round(volumes[-1]/vol_sma, 1) if vol_sma else 0,
        "cross_up": cross_up, "cross_down": cross_down,
        "bull_trend": bull_trend, "bear_trend": bear_trend,
    }

    # LONG
    long_base = cross_up and st_bull and rsi_val < cfg["rsi_ob"] and bull_trend
    if cfg["require_macd"]:        long_base = long_base and macd_bull
    if cfg["require_volume"]:      long_base = long_base and vol_spike
    if cfg["require_bull_candle"]: long_base = long_base and bull_candle

    # SHORT
    short_base = cross_down and not st_bull and rsi_val > cfg["rsi_os"] and bear_trend
    if cfg["require_macd"]:        short_base = short_base and macd_bear
    if cfg["require_volume"]:      short_base = short_base and vol_spike
    if cfg["require_bull_candle"]: short_base = short_base and bear_candle

    if long_base:  return "long",  details
    if short_base: return "short", details
    return None, details

async def get_5m_trend(symbol):
    """Quick 5m trend check for multi-timeframe confirmation"""
    try:
        klines = await get_klines(symbol, "5", 60)
        if len(klines) < 30: return None
        klines = list(reversed(klines))
        closes = [float(k[4]) for k in klines]
        ema9  = calc_ema(closes, 9)
        ema21 = calc_ema(closes, 21)
        rsi   = calc_rsi_wilder(closes, 14)
        if ema9 > ema21 and rsi > 45: return "bull"
        if ema9 < ema21 and rsi < 55: return "bear"
        return "neutral"
    except: return None

# ── SCANNER ───────────────────────────────────────────────────
async def scan_once():
    if state["daily_loss_hit"]:
        print("Daily loss limit hit — bot paused for today")
        return

    try:
        sample = random.sample(TOP50, min(15, len(TOP50)))
        state["stats"]["scanned"] = len(TOP50)
        print(f"Scan: {len(sample)} coins sampled from TOP50 | mode={state['signal_mode']} | positions={len(state['positions'])}")

        for sym in sample:
            if sym in state["positions"]: continue
            if any(p["symbol"] == sym for p in state["pending"]): continue
            await analyse(sym)
            await asyncio.sleep(0.4)

        # Execute pending signals
        for sig in list(state["pending"]):
            if len(state["positions"]) >= state["max_pos"]: break
            if sig["symbol"] in state["positions"]:
                state["pending"] = [s for s in state["pending"] if s["symbol"] != sig["symbol"]]
                continue
            try:
                usdt = max(state["balance"] * state["risk_pct"] / 100, MIN_ORDER)
                await execute_trade(sig["symbol"], sig["direction"], usdt, sig["atr"], sig["price"])
                state["pending"] = [s for s in state["pending"] if s["symbol"] != sig["symbol"]]
            except Exception as e:
                print(f"Trade failed {sig['symbol']}: {e}")
                state["pending"] = [s for s in state["pending"] if s["symbol"] != sig["symbol"]]

    except Exception as e:
        print(f"Scanner error: {e}")

async def analyse(symbol):
    try:
        if symbol in state["positions"]: return
        if any(p["symbol"] == symbol for p in state["pending"]): return

        mode = state["signal_mode"]
        cfg  = MODES[mode]
        limit = cfg["ema_trend"] + 80

        klines = await get_klines(symbol, "30", limit)
        if len(klines) < cfg["ema_trend"] + 30: return
        klines = list(reversed(klines))
        opens   = [float(k[1]) for k in klines]
        highs   = [float(k[2]) for k in klines]
        lows    = [float(k[3]) for k in klines]
        closes  = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        signal, details = primeflow_signal(opens, highs, lows, closes, volumes, mode)

        log = {"symbol": symbol, "signal": signal,
               "rsi": details.get("rsi", 0), "vol": details.get("vol_ratio", 0),
               "st": details.get("st_bull", False), "cross_up": details.get("cross_up", False),
               "cross_down": details.get("cross_down", False), "mode": mode, "ts": int(time.time())}
        state["signal_log"] = ([log] + state["signal_log"])[:30]

        if signal not in ("long", "short"): return
        if symbol in state["positions"]: return
        if any(p["symbol"] == symbol for p in state["pending"]): return

        # Multi-timeframe confirmation
        if state["use_mtf"]:
            trend_5m = await get_5m_trend(symbol)
            if signal == "long"  and trend_5m == "bear": return
            if signal == "short" and trend_5m == "bull": return
            print(f"MTF OK: {symbol} 5m={trend_5m} 30m={signal}")

        atr_val = details.get("atr", 0)
        price   = closes[-1]
        tp1     = round(price + atr_val * 1.0, 6) if signal == "long" else round(price - atr_val * 1.0, 6)
        tp2     = round(price + atr_val * 2.0, 6) if signal == "long" else round(price - atr_val * 2.0, 6)
        sl      = round(price - atr_val * 1.2, 6) if signal == "long" else round(price + atr_val * 1.2, 6)

        state["pending"].append({
            "symbol": symbol, "direction": signal,
            "price": price, "tp1": tp1, "tp2": tp2, "sl": sl,
            "atr": atr_val, "rsi": details.get("rsi", 0),
            "vol": details.get("vol_ratio", 0), "mode": mode,
            "ts": int(time.time()),
        })
        state["stats"]["signals"] += 1
        print(f"SIGNAL [{mode.upper()}] {signal.upper()}: {symbol} rsi={details.get('rsi')} vol={details.get('vol_ratio')}x")

    except Exception as e:
        print(f"Analyse {symbol}: {e}")

# ── TRADE EXECUTION ───────────────────────────────────────────
async def execute_trade(symbol, direction, usdt_amount, atr_val=0, entry_price=0):
    if symbol in state["positions"]: raise Exception("Already in position")

    # Set leverage
    await set_leverage(symbol)
    await asyncio.sleep(0.3)

    # Calculate qty
    price = await get_price(symbol)
    if price <= 0: raise Exception("Cannot get price")
    qty = round((usdt_amount * state["leverage"]) / price, 3)
    if qty <= 0: raise Exception("Qty too small")

    side = "Buy" if direction == "long" else "Sell"

    # Calculate TP/SL
    if atr_val > 0:
        tp1 = price + atr_val * 1.0 if direction == "long" else price - atr_val * 1.0
        tp2 = price + atr_val * 2.0 if direction == "long" else price - atr_val * 2.0
        sl  = price - atr_val * 1.2 if direction == "long" else price + atr_val * 1.2
    else:
        tp1 = price * 1.20 if direction == "long" else price * 0.80
        tp2 = price * 1.40 if direction == "long" else price * 0.60
        sl  = price * 0.88 if direction == "long" else price * 1.12

    tp1 = round(tp1, 6); tp2 = round(tp2, 6); sl = round(sl, 6)

    print(f"Opening {direction.upper()} {symbol} qty={qty} @ {price} | TP1={tp1} TP2={tp2} SL={sl}")

    # Place market order with TP/SL
    await place_futures_order(symbol, side, qty, tp=round(tp1, 6), sl=round(sl, 6))

    if direction == "long": state["stats"]["long"] += 1
    else: state["stats"]["short"] += 1
    state["stats"]["traded"] += 1

    state["positions"][symbol] = {
        "symbol": symbol, "direction": direction,
        "entry": price, "current": price,
        "qty": qty, "usdt_size": usdt_amount,
        "tp1": tp1, "tp2": tp2, "sl": sl,
        "highest": price,  # for trailing SL
        "lowest":  price,  # for trailing SL on shorts
        "tp1_hit": False,
        "pnl": 0.0, "pnl_pct": 0.0,
        "mode": state["signal_mode"],
        "leverage": state["leverage"],
        "entry_ts": int(time.time()),
    }
    state["balance"] = await get_balance()
    print(f"POSITION OPEN: {direction.upper()} {symbol} @ {price} x{state['leverage']}")

# ── POSITION MONITORING (every 5 seconds) ─────────────────────
async def monitor_positions():
    """Fast loop — checks prices and trailing SL every 5 seconds"""
    while True:
        if state["bot_on"] and state["api_key"] and state["positions"]:
            for sym in list(state["positions"]):
                try:
                    pos   = state["positions"][sym]
                    price = await get_price(sym)
                    if price <= 0: continue

                    pos["current"] = price
                    direction = pos["direction"]

                    if direction == "long":
                        pos["pnl"]     = (price - pos["entry"]) * pos["qty"]
                        pos["pnl_pct"] = (price - pos["entry"]) / pos["entry"] * 100 * pos["leverage"]

                        # Update trailing high
                        if price > pos["highest"]:
                            pos["highest"] = price
                            # Trail SL: 10% below highest
                            new_sl = round(price * (1 - state["trail_pct"] / 100), 6)
                            if new_sl > pos["sl"]:
                                pos["sl"] = new_sl
                                print(f"Trailing SL moved up: {sym} SL={new_sl}")

                        # TP1 hit
                        if not pos["tp1_hit"] and price >= pos["tp1"]:
                            pos["tp1_hit"] = True
                            pos["qty"] = round(pos["qty"] / 2, 3)
                            partial_pnl = pos["pnl"] * 0.5
                            state["today_pnl"] += partial_pnl
                            state["total_pnl"] += partial_pnl
                            await close_futures_position(sym, "long", pos["qty"])
                            print(f"TP1 HIT LONG: {sym} @ {price}")

                        # TP2 hit
                        if price >= pos["tp2"]:
                            await close_position_and_record(sym, "win")
                            continue

                        # SL hit
                        if price <= pos["sl"]:
                            await close_position_and_record(sym, "loss")
                            continue

                    else:  # short
                        pos["pnl"]     = (pos["entry"] - price) * pos["qty"]
                        pos["pnl_pct"] = (pos["entry"] - price) / pos["entry"] * 100 * pos["leverage"]

                        # Update trailing low
                        if price < pos["lowest"]:
                            pos["lowest"] = price
                            # Trail SL: 10% above lowest
                            new_sl = round(price * (1 + state["trail_pct"] / 100), 6)
                            if new_sl < pos["sl"]:
                                pos["sl"] = new_sl
                                print(f"Trailing SL moved down: {sym} SL={new_sl}")

                        # TP1 hit
                        if not pos["tp1_hit"] and price <= pos["tp1"]:
                            pos["tp1_hit"] = True
                            pos["qty"] = round(pos["qty"] / 2, 3)
                            partial_pnl = pos["pnl"] * 0.5
                            state["today_pnl"] += partial_pnl
                            state["total_pnl"] += partial_pnl
                            await close_futures_position(sym, "short", pos["qty"])
                            print(f"TP1 HIT SHORT: {sym} @ {price}")

                        # TP2 hit
                        if price <= pos["tp2"]:
                            await close_position_and_record(sym, "win")
                            continue

                        # SL hit
                        if price >= pos["sl"]:
                            await close_position_and_record(sym, "loss")
                            continue

                except Exception as e:
                    print(f"Monitor {sym}: {e}")

        # Check daily loss limit
        check_daily_loss()
        await asyncio.sleep(5)  # check every 5 seconds

async def close_position_and_record(symbol, result):
    pos = state["positions"].get(symbol)
    if not pos: return
    try:
        await close_futures_position(symbol, pos["direction"], pos["qty"])
        await cancel_all_orders(symbol)
        pnl = pos["pnl"]
        state["today_pnl"] += pnl
        state["total_pnl"] += pnl
        if result == "win": state["wins"] += 1
        else: state["losses"] += 1
        state["trades"].insert(0, {**pos, "result": result, "close_ts": int(time.time())})
        del state["positions"][symbol]
        state["balance"] = await get_balance()
        print(f"{result.upper()}: {symbol} pnl={pnl:.2f}")
    except Exception as e:
        print(f"Close error {symbol}: {e}")

def check_daily_loss():
    if state["start_balance"] <= 0: return
    loss_pct = (state["start_balance"] - state["balance"]) / state["start_balance"] * 100
    if loss_pct >= state["max_daily_loss"] and not state["daily_loss_hit"]:
        state["daily_loss_hit"] = True
        state["bot_on"] = False
        print(f"DAILY LOSS LIMIT HIT: {loss_pct:.1f}% — bot paused")

# ── BOT LOOP ──────────────────────────────────────────────────
async def bot_loop():
    print("Bot loop started — Futures mode")
    while True:
        if state["bot_on"] and state["api_key"] and not state["daily_loss_hit"]:
            await scan_once()
            try: state["balance"] = await get_balance()
            except: pass
        else:
            if state["daily_loss_hit"]:
                print("Bot paused: daily loss limit reached")
            elif not state["api_key"]:
                print("Waiting for API keys...")
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    print(f"APEX Pro starting — key loaded: {bool(state['api_key'])}")
    if state["api_key"]:
        try:
            state["balance"] = await get_balance()
            state["start_balance"] = state["balance"]
            print(f"Balance: ${state['balance']:.2f}")
        except Exception as e:
            print(f"Balance error: {e}")
    asyncio.create_task(bot_loop())
    asyncio.create_task(monitor_positions())  # fast 5s loop

# ── API ROUTES ────────────────────────────────────────────────
@app.post("/api/connect")
async def connect(req: Request):
    b = await req.json()
    state["api_key"]       = b.get("api_key", "").strip()
    state["api_secret"]    = b.get("api_secret", "").strip()
    state["risk_pct"]      = float(b.get("risk_pct", 2.0))
    state["leverage"]      = int(b.get("leverage", 2))
    state["max_pos"]       = int(b.get("max_pos", 3))
    state["signal_mode"]   = b.get("signal_mode", "medium")
    state["trail_pct"]     = float(b.get("trail_pct", 10.0))
    state["max_daily_loss"]= float(b.get("max_daily_loss", 5.0))
    state["use_mtf"]       = bool(b.get("use_mtf", True))
    state["balance"]       = await get_balance()
    state["start_balance"] = state["balance"]
    state["daily_loss_hit"]= False
    state["bot_on"]        = True
    print(f"Connected — balance: ${state['balance']:.2f}")
    return {"ok": True, "balance": state["balance"]}

@app.get("/api/status")
async def get_status():
    t = state["wins"] + state["losses"]
    daily_loss_pct = 0
    if state["start_balance"] > 0:
        daily_loss_pct = round((state["start_balance"] - state["balance"]) / state["start_balance"] * 100, 2)
    return {
        "balance":        round(state["balance"], 2),
        "today_pnl":      round(state["today_pnl"], 2),
        "total_pnl":      round(state["total_pnl"], 2),
        "bot_on":         state["bot_on"],
        "signal_mode":    state["signal_mode"],
        "mode_desc":      MODES[state["signal_mode"]]["desc"],
        "leverage":       state["leverage"],
        "trail_pct":      state["trail_pct"],
        "max_daily_loss": state["max_daily_loss"],
        "use_mtf":        state["use_mtf"],
        "daily_loss_hit": state["daily_loss_hit"],
        "daily_loss_pct": daily_loss_pct,
        "positions":      list(state["positions"].values()),
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
    if mode not in MODES: return JSONResponse({"error": "Invalid mode"}, 400)
    state["signal_mode"] = mode; state["pending"] = []
    return {"ok": True, "mode": mode, "desc": MODES[mode]["desc"]}

@app.post("/api/settings")
async def update_settings(req: Request):
    b = await req.json()
    if "leverage"       in b: state["leverage"]       = int(b["leverage"])
    if "trail_pct"      in b: state["trail_pct"]      = float(b["trail_pct"])
    if "max_daily_loss" in b: state["max_daily_loss"] = float(b["max_daily_loss"])
    if "use_mtf"        in b: state["use_mtf"]        = bool(b["use_mtf"])
    if "risk_pct"       in b: state["risk_pct"]       = float(b["risk_pct"])
    if "max_pos"        in b: state["max_pos"]        = int(b["max_pos"])
    return {"ok": True}

@app.post("/api/close/{symbol}")
async def close_pos(symbol: str):
    pos = state["positions"].get(symbol)
    if not pos: return JSONResponse({"error": "Not found"}, 400)
    try:
        await close_position_and_record(symbol, "win" if pos["pnl"] >= 0 else "loss")
        return {"ok": True, "pnl": pos.get("pnl", 0)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 400)

@app.post("/api/reset_daily")
async def reset_daily():
    state["daily_loss_hit"] = False
    state["bot_on"] = True
    state["start_balance"] = state["balance"]
    state["today_pnl"] = 0.0
    return {"ok": True}

@app.post("/api/webhook")
async def webhook(req: Request):
    try:
        body   = await req.json()
        symbol = body.get("symbol", "").replace("/", "").replace(":", "").upper()
        action = body.get("action", "").lower()
        secret = body.get("secret", "")
        if secret != os.getenv("WEBHOOK_SECRET", "apex123"):
            return JSONResponse({"error": "Unauthorized"}, 401)
        if action in ("buy", "long"):
            usdt = max(state["balance"] * state["risk_pct"] / 100, MIN_ORDER)
            await execute_trade(symbol, "long", usdt)
            return {"ok": True, "msg": f"LONG: {symbol}"}
        if action in ("sell", "short"):
            usdt = max(state["balance"] * state["risk_pct"] / 100, MIN_ORDER)
            await execute_trade(symbol, "short", usdt)
            return {"ok": True, "msg": f"SHORT: {symbol}"}
        if action == "close":
            if symbol in state["positions"]:
                await close_position_and_record(symbol, "win" if state["positions"][symbol]["pnl"] >= 0 else "loss")
            return {"ok": True}
        return {"ok": False, "msg": "Unknown action"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 400)

@app.get("/")
async def root():
    with open("index.html") as f: return HTMLResponse(f.read())

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
