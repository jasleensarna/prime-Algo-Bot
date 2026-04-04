"""
APEX Sniper — PrimeFlow Algo Engine (Lenient + Medium + Strict)
$12 minimum order so TP splits are always above Bybit minimum.
Auto-loads API keys from Railway env vars.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn, os, time, asyncio, hmac, hashlib, urllib.parse, json, random
import httpx

app = FastAPI()

state = {
    "api_key":    os.getenv("BYBIT_API_KEY", ""),
    "api_secret": os.getenv("BYBIT_API_SECRET", ""),
    "risk_pct": 5.0, "tp_mult1": 1.0, "tp_mult2": 2.0, "tp_mult3": 3.2,
    "sl_mult": 1.2, "max_pos": 2,
    "signal_mode": "lenient",
    "bot_on": True, "balance": 0.0,
    "positions": {}, "trades": [], "pending": [],
    "stats": {"scanned": 0, "new": 0, "signals": 0, "traded": 0},
    "wins": 0, "losses": 0, "today_pnl": 0.0, "total_pnl": 0.0,
    "signal_log": [],
}

MODES = {
    "lenient": {
        "ema_fast": 9, "ema_slow": 21, "ema_trend": 50,
        "st_factor": 2.0, "st_period": 10,
        "rsi_ob": 75, "rsi_os": 25,
        "require_macd": False, "require_volume": False,
        "require_bull_candle": False,
        "desc": "More signals · Higher risk · EMA9/21/50"
    },
    "medium": {
        "ema_fast": 13, "ema_slow": 34, "ema_trend": 100,
        "st_factor": 2.5, "st_period": 10,
        "rsi_ob": 68, "rsi_os": 32,
        "require_macd": True, "require_volume": True,
        "require_bull_candle": True,
        "desc": "Balanced · MACD + Volume filter · EMA13/34/100"
    },
    "strict": {
        "ema_fast": 13, "ema_slow": 34, "ema_trend": 200,
        "st_factor": 3.0, "st_period": 10,
        "rsi_ob": 62, "rsi_os": 38,
        "require_macd": True, "require_volume": True,
        "require_bull_candle": True,
        "desc": "Fewer signals · Lower risk · All filters active"
    },
}

BYBIT = "https://api.bybit.com"
seen_symbols = set()
MIN_ORDER = 12.0  # $12 minimum so each TP half = $6, above Bybit minimum

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
    for acct in ["UNIFIED", "SPOT", "CONTRACT"]:
        try:
            data = await by_get("/v5/account/wallet-balance", {"accountType": acct}, signed=True)
            if data.get("retCode") != 0: continue
            for wallet in data.get("result", {}).get("list", []):
                for c in wallet.get("coin", []):
                    if c.get("coin") == "USDT":
                        bal = float(c.get("walletBalance") or c.get("availableToWithdraw") or c.get("equity") or 0)
                        if bal > 0: return bal
        except: continue
    try:
        data = await by_get("/v5/asset/transfer/query-account-coins-balance",
                            {"accountType": "FUND", "coin": "USDT"}, signed=True)
        if data.get("retCode") == 0:
            bal = float(data.get("result", {}).get("balance", {}).get("walletBalance", 0) or 0)
            if bal > 0: return bal
    except: pass
    return 0.0

async def get_price(symbol):
    data = await by_get("/v5/market/tickers", {"category": "spot", "symbol": symbol})
    items = data.get("result", {}).get("list", [])
    return float(items[0].get("lastPrice", 0)) if items else 0.0

async def get_klines(symbol, interval="30", limit=220):
    data = await by_get("/v5/market/kline", {"category": "spot", "symbol": symbol, "interval": interval, "limit": limit})
    return data.get("result", {}).get("list", []) if data.get("retCode") == 0 else []

async def get_instruments():
    data = await by_get("/v5/market/instruments-info", {"category": "spot", "limit": 1000})
    return data.get("result", {}).get("list", []) if data.get("retCode") == 0 else []

async def place_order(symbol, side, order_type, qty=None, usdt_qty=None, price=None, trigger_price=None):
    body = {"category": "spot", "symbol": symbol, "side": side, "orderType": order_type,
            "timeInForce": "GTC" if order_type == "Limit" else "IOC"}
    if qty: body["qty"] = str(round(qty, 6))
    if usdt_qty: body["marketUnit"] = "quoteCoin"; body["qty"] = str(round(usdt_qty, 2))
    if price: body["price"] = str(round(price, 8))
    if trigger_price:
        body["triggerPrice"] = str(round(trigger_price, 8)); body["orderType"] = "Limit"
        body["price"] = str(round(trigger_price * 0.99, 8)); body["triggerDirection"] = 2
        body["orderFilter"] = "StopOrder"
    data = await by_post("/v5/order/create", body)
    if data.get("retCode") != 0: raise Exception(data.get("retMsg", f"Order failed: {data.get('retCode')}"))
    return data.get("result", {})

async def cancel_all_orders(symbol):
    try: await by_post("/v5/order/cancel-all", {"category": "spot", "symbol": symbol})
    except: pass

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
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    if not trs: return 0
    a = sum(trs[:period]) / min(len(trs), period)
    for t in trs[period:]: a = (a * (period - 1) + t) / period
    return a

def calc_supertrend(highs, lows, closes, factor, period):
    n = len(closes)
    if n < period + 2: return True
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, n)]
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
    ms = [calc_ema(closes[:i], fast) - calc_ema(closes[:i], slow) for i in range(slow, len(closes) + 1)]
    if len(ms) < signal: return ms[-1], ms[-1]
    return ms[-1], calc_ema(ms, signal)

def ema_crossover(closes, pf, ps):
    if len(closes) < 2: return False, False
    fn = calc_ema(closes, pf); sn = calc_ema(closes, ps)
    fp = calc_ema(closes[:-1], pf); sp = calc_ema(closes[:-1], ps)
    return fp <= sp and fn > sn, fp >= sp and fn < sn

def primeflow_signal(opens, highs, lows, closes, volumes, mode="lenient"):
    cfg = MODES[mode]; ef, es, et = cfg["ema_fast"], cfg["ema_slow"], cfg["ema_trend"]
    if len(closes) < et + 30: return None, {}
    ema_fast = calc_ema(closes, ef); ema_slow = calc_ema(closes, es); ema_trend = calc_ema(closes, et)
    cross_up, cross_down = ema_crossover(closes, ef, es)
    st_bull = calc_supertrend(highs, lows, closes, cfg["st_factor"], cfg["st_period"])
    rsi_val = calc_rsi_wilder(closes, 14); atr_val = calc_atr_wilder(highs, lows, closes, 14)
    bull_trend = closes[-1] > ema_trend; bear_trend = closes[-1] < ema_trend
    bull_candle = closes[-1] > opens[-1]; bear_candle = closes[-1] < opens[-1]
    vol_sma = calc_sma(volumes, 20); vol_spike = volumes[-1] > vol_sma * 1.2
    macd_line, signal_line = calc_macd(closes)
    macd_bull = macd_line > signal_line; macd_bear = macd_line < signal_line
    details = {"rsi": round(rsi_val,1), "atr": atr_val, "st_bull": st_bull,
               "macd_bull": macd_bull, "vol_spike": vol_spike,
               "vol_ratio": round(volumes[-1]/vol_sma,1) if vol_sma else 0,
               "cross_up": cross_up, "bull_trend": bull_trend}
    long_base = cross_up and st_bull and rsi_val < cfg["rsi_ob"] and bull_trend
    if cfg["require_macd"]:        long_base = long_base and macd_bull
    if cfg["require_volume"]:      long_base = long_base and vol_spike
    if cfg["require_bull_candle"]: long_base = long_base and bull_candle
    short_base = cross_down and not st_bull and rsi_val > cfg["rsi_os"] and bear_trend
    if cfg["require_macd"]:        short_base = short_base and macd_bear
    if cfg["require_volume"]:      short_base = short_base and vol_spike
    if cfg["require_bull_candle"]: short_base = short_base and bear_candle
    if long_base:  return "long",  details
    if short_base: return "short", details
    return None, details

SKIP = {"BTC","ETH","BNB","SOL","XRP","ADA","DOT","LINK","AVAX","MATIC","UNI","AAVE","LTC","BCH","ETC","ATOM","NEAR","ARB","OP","SUI","TRX","DOGE","SHIB","APT","INJ","TON","FTM","ALGO"}

async def scan_once():
    try:
        instruments = await get_instruments()
        pairs = [i for i in instruments if i.get("quoteCoin")=="USDT" and i.get("status")=="Trading" and i.get("baseCoin","").upper() not in SKIP]
        state["stats"]["scanned"] = len(pairs)
        new_syms = [i.get("symbol","") for i in pairs if i.get("symbol","") not in seen_symbols]
        for s in new_syms: seen_symbols.add(s)
        state["stats"]["new"] = len(seen_symbols)
        print(f"Scan: {len(pairs)} pairs | {len(new_syms)} new | signals={state['stats']['signals']} traded={state['stats']['traded']}")

        for sym in new_syms[:4]:
            if sym in state["positions"] or any(p["symbol"]==sym for p in state["pending"]): continue
            await analyse(sym); await asyncio.sleep(0.5)

        all_syms = [s for s in seen_symbols if s not in state["positions"] and not any(p["symbol"]==s for p in state["pending"])]
        sample = random.sample(all_syms, min(20, len(all_syms)))
        for sym in sample:
            await analyse(sym); await asyncio.sleep(0.2)

        for sig in list(state["pending"]):
            if len(state["positions"]) >= state["max_pos"]: break
            if sig["symbol"] in state["positions"]:
                state["pending"] = [s for s in state["pending"] if s["symbol"] != sig["symbol"]]; continue
            try:
                usdt = max(state["balance"] * state["risk_pct"] / 100, MIN_ORDER)
                await execute_buy(sig["symbol"], usdt, sig["atr"], sig["price"])
                state["pending"] = [s for s in state["pending"] if s["symbol"] != sig["symbol"]]
                print(f"AUTO-BOUGHT [{state['signal_mode'].upper()}]: {sig['symbol']}")
            except Exception as e:
                print(f"Auto-buy failed {sig['symbol']}: {e}")
                state["pending"] = [s for s in state["pending"] if s["symbol"] != sig["symbol"]]
    except Exception as e: print(f"Scanner error: {e}")

async def analyse(symbol):
    try:
        if symbol in state["positions"]: return
        if any(p["symbol"] == symbol for p in state["pending"]): return
        mode = state["signal_mode"]; cfg = MODES[mode]; limit = cfg["ema_trend"] + 80
        klines = await get_klines(symbol, "30", limit)
        if len(klines) < cfg["ema_trend"] + 30: return
        klines = list(reversed(klines))
        opens=[float(k[1]) for k in klines]; highs=[float(k[2]) for k in klines]
        lows=[float(k[3]) for k in klines]; closes=[float(k[4]) for k in klines]
        volumes=[float(k[5]) for k in klines]
        signal, details = primeflow_signal(opens, highs, lows, closes, volumes, mode)
        log = {"symbol":symbol,"signal":signal,"rsi":details.get("rsi",0),"vol":details.get("vol_ratio",0),
               "st":details.get("st_bull",False),"cross":details.get("cross_up",False),"mode":mode,"ts":int(time.time())}
        state["signal_log"] = ([log] + state["signal_log"])[:30]
        if signal != "long": return
        if symbol in state["positions"]: return
        if any(p["symbol"] == symbol for p in state["pending"]): return
        atr_val = details.get("atr", 0); price = closes[-1]
        tp1 = round(price + atr_val * state["tp_mult1"], 8)
        tp2 = round(price + atr_val * state["tp_mult2"], 8)
        sl  = round(price - atr_val * state["sl_mult"],  8)
        sl_pct = (price - sl) / price * 100 if price > 0 else 99
        if sl_pct > 25: return
        state["pending"].append({"symbol":symbol,"price":price,"tp1":tp1,"tp2":tp2,"sl":sl,
                                  "atr":atr_val,"rsi":details.get("rsi",0),"vol":details.get("vol_ratio",0),
                                  "mode":mode,"ts":int(time.time())})
        state["stats"]["signals"] += 1
        print(f"SIGNAL [{mode.upper()}]: {symbol} rsi={details.get('rsi')} vol={details.get('vol_ratio')}x st={details.get('st_bull')} cross={details.get('cross_up')}")
    except Exception as e: print(f"Analyse {symbol}: {e}")

async def get_symbol_precision(symbol):
    try:
        data = await by_get("/v5/market/instruments-info", {"category": "spot", "symbol": symbol})
        info = data.get("result", {}).get("list", [])
        if not info: return 6, 8
        s = info[0].get("lotSizeFilter", {}); step = s.get("basePrecision", "0.000001")
        dq = len(step.rstrip("0").split(".")[-1]) if "." in step else 0
        p = info[0].get("priceFilter", {}); tick = p.get("tickSize", "0.00000001")
        dp = len(tick.rstrip("0").split(".")[-1]) if "." in tick else 8
        return dq, dp
    except: return 6, 8

async def place_limit_sell(symbol, qty, price, qty_dec, price_dec):
    qty_str = f"{qty:.{qty_dec}f}"; price_str = f"{price:.{price_dec}f}"
    body = {"category":"spot","symbol":symbol,"side":"Sell","orderType":"Limit",
            "qty":qty_str,"price":price_str,"timeInForce":"GTC"}
    data = await by_post("/v5/order/create", body)
    if data.get("retCode") != 0:
        print(f"Limit sell failed {symbol}: {data.get('retMsg')} code={data.get('retCode')}"); return None
    print(f"Limit SELL placed {symbol} qty={qty_str} @ {price_str}")
    return data.get("result", {})

async def execute_buy(symbol, usdt_amount, atr_val=0, entry_price=0):
    if symbol in state["positions"]: raise Exception("Already in position")
    print(f"Attempting BUY: {symbol} usdt={usdt_amount}")
    buy_body = {"category":"spot","symbol":symbol,"side":"Buy","orderType":"Market",
                "marketUnit":"quoteCoin","qty":str(round(usdt_amount,2)),"timeInForce":"IOC"}
    buy_data = await by_post("/v5/order/create", buy_body)
    if buy_data.get("retCode") != 0:
        raise Exception(buy_data.get("retMsg", f"Buy failed: {buy_data.get('retCode')}"))
    print(f"BUY placed: {symbol} — waiting for fill...")
    await asyncio.sleep(1.5)
    price = await get_price(symbol)
    if price <= 0: raise Exception("Cannot get price after fill")
    order_id = buy_data.get("result", {}).get("orderId", "")
    filled_qty = usdt_amount / price
    try:
        hist = await by_get("/v5/order/history", {"category":"spot","symbol":symbol,"orderId":order_id}, signed=True)
        orders = hist.get("result", {}).get("list", [])
        if orders:
            fq = float(orders[0].get("cumExecQty", 0))
            if fq > 0: filled_qty = fq
            print(f"Filled qty: {filled_qty}")
    except: pass
    qty_dec, price_dec = await get_symbol_precision(symbol)
    print(f"Precision: qty_dec={qty_dec} price_dec={price_dec}")
    if atr_val > 0:
        tp1 = price + atr_val * state["tp_mult1"]; tp2 = price + atr_val * state["tp_mult2"]
        sl  = price - atr_val * state["sl_mult"]
    else:
        tp1 = price * 1.30; tp2 = price * 1.70; sl = price * 0.85
    tp1=round(tp1,price_dec); tp2=round(tp2,price_dec); sl=round(sl,price_dec)
    if sl >= price: sl = round(price * 0.85, price_dec)
    if tp1 <= price: tp1 = round(price * 1.10, price_dec)
    if tp2 <= tp1: tp2 = round(tp1 * 1.30, price_dec)
    half_qty = round(filled_qty / 2, qty_dec); total_qty = round(filled_qty, qty_dec)
    print(f"Placing TPs: TP1={tp1} TP2={tp2} SL={sl} half={half_qty} total={total_qty}")
    tp1_order = await place_limit_sell(symbol, half_qty, tp1, qty_dec, price_dec)
    await asyncio.sleep(0.5)
    tp2_order = await place_limit_sell(symbol, half_qty, tp2, qty_dec, price_dec)
    state["positions"][symbol] = {
        "symbol":symbol,"entry":price,"current":price,"qty":total_qty,
        "usdt_size":usdt_amount,"tp1":tp1,"tp2":tp2,"sl":sl,
        "tp1_hit":False,"pnl":0.0,"pnl_pct":0.0,"mode":state["signal_mode"],
        "tp1_order":tp1_order.get("orderId","") if tp1_order else "",
        "tp2_order":tp2_order.get("orderId","") if tp2_order else "",
        "entry_ts":int(time.time()),
    }
    state["stats"]["traded"] += 1; state["balance"] = await get_balance()
    print(f"POSITION OPEN: {symbol} @ {price} | TP1={tp1} TP2={tp2} SL={sl} qty={total_qty}")

async def update_positions():
    for sym in list(state["positions"]):
        try:
            pos = state["positions"][sym]; price = await get_price(sym)
            if price <= 0: continue
            pos["current"]=price; pos["pnl"]=(price-pos["entry"])*pos["qty"]
            pos["pnl_pct"]=(price-pos["entry"])/pos["entry"]*100
            if not pos["tp1_hit"] and price >= pos["tp1"]:
                pos["tp1_hit"]=True; pos["sl"]=pos["entry"]; pos["qty"]=round(pos["qty"]/2,6)
                state["today_pnl"]+=pos["pnl"]*0.5; state["total_pnl"]+=pos["pnl"]*0.5
                print(f"TP1 HIT: {sym} @ {price} — SL to breakeven")
            if price >= pos["tp2"]:
                await cancel_all_orders(sym)
                state["today_pnl"]+=pos["pnl"]; state["total_pnl"]+=pos["pnl"]; state["wins"]+=1
                state["trades"].insert(0,{**pos,"result":"win","close_ts":int(time.time())})
                del state["positions"][sym]; state["balance"]=await get_balance()
                print(f"TP2 HIT: {sym} WIN @ {price}"); continue
            if price <= pos["sl"]:
                print(f"SL HIT: {sym} @ {price} — market selling")
                await cancel_all_orders(sym); await asyncio.sleep(0.5)
                qty_dec, _ = await get_symbol_precision(sym)
                sell_body = {"category":"spot","symbol":sym,"side":"Sell","orderType":"Market",
                             "qty":str(round(pos["qty"],qty_dec)),"timeInForce":"IOC"}
                sell_data = await by_post("/v5/order/create", sell_body)
                if sell_data.get("retCode") != 0: print(f"SL sell failed {sym}: {sell_data.get('retMsg')}")
                state["today_pnl"]+=pos["pnl"]; state["total_pnl"]+=pos["pnl"]; state["losses"]+=1
                state["trades"].insert(0,{**pos,"result":"loss","close_ts":int(time.time())})
                del state["positions"][sym]; state["balance"]=await get_balance()
                print(f"SL EXECUTED: {sym} LOSS @ {price}")
        except Exception as e: print(f"Pos {sym}: {e}")

async def bot_loop():
    print("Bot loop started")
    while True:
        if state["bot_on"] and state["api_key"]:
            await scan_once(); await update_positions()
            try: state["balance"] = await get_balance()
            except: pass
        else:
            print(f"Waiting — bot_on={state['bot_on']} key={bool(state['api_key'])}")
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    print(f"APEX Bot starting — key loaded: {bool(state['api_key'])}")
    if state["api_key"]:
        try: state["balance"] = await get_balance(); print(f"Balance: ${state['balance']:.2f}")
        except Exception as e: print(f"Balance error: {e}")
    asyncio.create_task(bot_loop())

@app.post("/api/connect")
async def connect(req: Request):
    b = await req.json()
    state["api_key"]=b.get("api_key","").strip(); state["api_secret"]=b.get("api_secret","").strip()
    state["risk_pct"]=float(b.get("risk_pct",5.0)); state["max_pos"]=int(b.get("max_pos",2))
    state["signal_mode"]=b.get("signal_mode","lenient")
    state["balance"] = await get_balance()
    print(f"Connected via UI — balance: ${state['balance']:.2f}")
    return {"ok": True, "balance": state["balance"]}

@app.get("/api/status")
async def get_status():
    t = state["wins"]+state["losses"]
    return {"balance":round(state["balance"],2),"today_pnl":round(state["today_pnl"],2),
            "total_pnl":round(state["total_pnl"],2),"bot_on":state["bot_on"],
            "signal_mode":state["signal_mode"],"mode_desc":MODES[state["signal_mode"]]["desc"],
            "positions":list(state["positions"].values()),"trades":state["trades"][:20],
            "pending":state["pending"][:3],"stats":state["stats"],"wins":state["wins"],
            "losses":state["losses"],"win_rate":round(state["wins"]/t*100) if t else 0,
            "signal_log":state["signal_log"][:10]}

@app.post("/api/bot/toggle")
async def toggle(): state["bot_on"]=not state["bot_on"]; return {"bot_on":state["bot_on"]}

@app.post("/api/mode/{mode}")
async def set_mode(mode: str):
    if mode not in MODES: return JSONResponse({"error":"Invalid mode"},400)
    state["signal_mode"]=mode; state["pending"]=[]
    print(f"Mode: {mode}"); return {"ok":True,"mode":mode,"desc":MODES[mode]["desc"]}

@app.post("/api/dismiss/{symbol}")
async def dismiss(symbol: str):
    state["pending"]=[s for s in state["pending"] if s["symbol"]!=symbol]; return {"ok":True}

@app.post("/api/close/{symbol}")
async def close_pos(symbol: str):
    pos=state["positions"].get(symbol)
    if not pos: return JSONResponse({"error":"Not found"},400)
    try:
        await cancel_all_orders(symbol)
        await place_order(symbol,"Sell","Market",qty=round(pos["qty"],6))
        pnl=pos["pnl"]; state["today_pnl"]+=pnl; state["total_pnl"]+=pnl
        state["wins" if pnl>=0 else "losses"]+=1
        state["trades"].insert(0,{**pos,"result":"win" if pnl>=0 else "loss","close_ts":int(time.time())})
        del state["positions"][symbol]; state["balance"]=await get_balance()
        return {"ok":True,"pnl":pnl}
    except Exception as e: return JSONResponse({"error":str(e)},400)

@app.post("/api/webhook")
async def tradingview_webhook(req: Request):
    try:
        body=await req.json()
        symbol=body.get("symbol","").replace("/","").replace(":","").upper()
        action=body.get("action","").lower(); secret=body.get("secret","")
        if secret!=os.getenv("WEBHOOK_SECRET","apex123"): return JSONResponse({"error":"Unauthorized"},401)
        if not symbol or not action: return JSONResponse({"error":"Missing params"},400)
        if action=="buy":
            if symbol in state["positions"]: return {"ok":False,"msg":"Already in position"}
            if len(state["positions"])>=state["max_pos"]: return {"ok":False,"msg":"Max positions"}
            usdt=max(state["balance"]*state["risk_pct"]/100, MIN_ORDER)
            await execute_buy(symbol,usdt); return {"ok":True,"msg":f"BUY: {symbol}"}
        if action=="sell":
            if symbol not in state["positions"]: return {"ok":False,"msg":"No position"}
            pos=state["positions"][symbol]; await cancel_all_orders(symbol)
            await place_order(symbol,"Sell","Market",qty=round(pos["qty"],6))
            pnl=pos["pnl"]; state["today_pnl"]+=pnl; state["total_pnl"]+=pnl
            state["wins" if pnl>=0 else "losses"]+=1
            state["trades"].insert(0,{**pos,"result":"win" if pnl>=0 else "loss","close_ts":int(time.time())})
            del state["positions"][symbol]; state["balance"]=await get_balance()
            return {"ok":True,"msg":f"SELL: {symbol} pnl={pnl:.2f}"}
        return {"ok":False,"msg":"Unknown action"}
    except Exception as e: return JSONResponse({"error":str(e)},400)

@app.get("/")
async def root():
    with open("index.html") as f: return HTMLResponse(f.read())

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
