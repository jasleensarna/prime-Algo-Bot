"""
APEX Pro — v2.0
ENTRY: Trend Gate (UT Bot 15m + ADX>25 + VWAP) → Score ≥75 (OBI+CVD+OI Delta)
EXIT:  TP1→SL to breakeven | Momentum Exhaustion | Structure Break
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn, os, time, asyncio, hmac, hashlib
import httpx

app = FastAPI()

API_KEY       = os.getenv("BYBIT_API_KEY", "")
API_SECRET    = os.getenv("BYBIT_API_SECRET", "")
BASE_URL      = "https://api.bybit.com"
MIN_SCORE     = 75
LEVERAGE      = 2
SCAN_INTERVAL = 30

SB_URL = os.getenv("SUPABASE_URL", "")
SB_KEY = os.getenv("SUPABASE_KEY", "")

WATCHLIST = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "DOGEUSDT","AVAXUSDT","LINKUSDT","NEARUSDT","APTUSDT",
    "OPUSDT","ARBUSDT","INJUSDT","SUIUSDT","THETAUSDT",
    "LDOUSDT","STXUSDT","FTMUSDT","GRTUSDT","SANDUSDT",
]

state = {
    "wins": 0, "losses": 0, "total_pnl": 0.0,
    "trades": [], "position": None, "balance": 0.0,
    "last_scan": 0, "status": "starting",
    "trend_blocks": 0, "score_blocks": 0, "last_signal": None,
}

# ── BYBIT HELPERS ────────────────────────────────────────────

def _sign(params):
    params["api_key"]     = API_KEY
    params["timestamp"]   = str(int(time.time() * 1000))
    params["recv_window"] = "5000"
    q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params["sign"] = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return params

async def _get(path, params={}, signed=False):
    if signed: params = _sign(dict(params))
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BASE_URL + path, params=params)
        return r.json()

async def _post(path, body):
    body = _sign(dict(body))
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(BASE_URL + path, json=body)
        return r.json()

async def get_balance():
    """
    Fixed: tries multiple balance fields in order
    Unified accounts use walletBalance, not availableToWithdraw
    """
    try:
        r = await _get("/v5/account/wallet-balance",
                       {"accountType": "UNIFIED"}, signed=True)
        coins = r["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                # Try each field in order — first non-zero wins
                for field in ["availableToWithdraw", "walletBalance", "equity", "availableBalance"]:
                    val = c.get(field, "")
                    if val and str(val).strip() not in ("", "0", "0.0"):
                        return float(val)
        # Fallback: totalWalletBalance at account level
        total = r["result"]["list"][0].get("totalWalletBalance", "0")
        if total and str(total).strip() not in ("", "0"):
            return float(total)
    except Exception as e:
        print(f"Balance err: {e}")
    return 0.0

async def get_klines(symbol, interval="5", limit=60):
    try:
        r = await _get("/v5/market/kline", {
            "category": "linear", "symbol": symbol,
            "interval": interval, "limit": str(limit)
        })
        return list(reversed(r["result"]["list"]))
    except Exception as e:
        print(f"Klines err {symbol}: {e}")
        return []

async def get_orderbook(symbol):
    try:
        r = await _get("/v5/market/orderbook",
                       {"category": "linear", "symbol": symbol, "limit": "50"})
        return r.get("result", {})
    except Exception as e:
        print(f"OB err {symbol}: {e}")
        return {}

async def get_recent_trades(symbol):
    try:
        r = await _get("/v5/market/recent-trade",
                       {"category": "linear", "symbol": symbol, "limit": "200"})
        return r.get("result", {}).get("list", [])
    except Exception as e:
        print(f"Trades err {symbol}: {e}")
        return []

async def get_open_interest(symbol):
    try:
        r = await _get("/v5/market/open-interest", {
            "category": "linear", "symbol": symbol,
            "intervalTime": "5min", "limit": "6"
        })
        return r.get("result", {}).get("list", [])
    except Exception as e:
        print(f"OI err {symbol}: {e}")
        return []

async def get_ticker(symbol):
    try:
        r = await _get("/v5/market/tickers",
                       {"category": "linear", "symbol": symbol})
        items = r.get("result", {}).get("list", [])
        return items[0] if items else {}
    except Exception as e:
        print(f"Ticker err {symbol}: {e}")
        return {}

async def place_order(symbol, side, qty, sl, tp1):
    return await _post("/v5/order/create", {
        "category": "linear", "symbol": symbol,
        "side": "Buy" if side == "long" else "Sell",
        "orderType": "Market", "qty": str(qty),
        "stopLoss": str(round(sl, 6)), "takeProfit": str(round(tp1, 6)),
        "tpslMode": "Full", "leverage": str(LEVERAGE), "positionIdx": "0",
    })

async def close_full(symbol, qty, side):
    return await _post("/v5/order/create", {
        "category": "linear", "symbol": symbol,
        "side": "Sell" if side == "long" else "Buy",
        "orderType": "Market", "qty": str(qty),
        "reduceOnly": True, "positionIdx": "0",
    })

async def update_sl(symbol, sl_price):
    return await _post("/v5/position/trading-stop", {
        "category": "linear", "symbol": symbol,
        "stopLoss": str(round(sl_price, 6)), "positionIdx": "0",
    })

# ── LAYER 1: TREND GATE ──────────────────────────────────────

def _atr(candles, period=14):
    if len(candles) < period + 1: return 0.0
    trs = []
    for i in range(1, len(candles)):
        h=float(candles[i][2]); l=float(candles[i][3]); pc=float(candles[i-1][4])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-period:]) / period

def _adx(candles, period=14):
    if len(candles) < period*2+1: return 0.0
    pdms, ndms, trs = [], [], []
    for i in range(1, len(candles)):
        h=float(candles[i][2]); ph=float(candles[i-1][2])
        l=float(candles[i][3]); pl=float(candles[i-1][3])
        pc=float(candles[i-1][4])
        up=h-ph; dn=pl-l
        pdms.append(up if up>dn and up>0 else 0)
        ndms.append(dn if dn>up and dn>0 else 0)
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    def smooth(arr):
        s=sum(arr[:period]); out=[s]
        for v in arr[period:]: s=s-s/period+v; out.append(s)
        return out
    st=smooth(trs); sp=smooth(pdms); sn=smooth(ndms); dxs=[]
    for i in range(len(st)):
        if st[i]==0: continue
        pip=100*sp[i]/st[i]; nim=100*sn[i]/st[i]; d=pip+nim
        if d: dxs.append(100*abs(pip-nim)/d)
    return sum(dxs[-period:])/min(len(dxs),period) if dxs else 0.0

def _ut_bot(candles, mult=1.5, period=10):
    if len(candles) < period+5: return "neutral"
    atr=_atr(candles, period)
    if atr==0: return "neutral"
    trail=float(candles[0][4]); sig="neutral"
    for i in range(1, len(candles)):
        cl=float(candles[i][4]); pcl=float(candles[i-1][4]); dist=mult*atr
        trail=max(trail, cl-dist) if cl>trail else min(trail, cl+dist)
        if cl>trail and pcl<=trail: sig="long"
        elif cl<trail and pcl>=trail: sig="short"
    return sig

def _vwap(candles):
    tpv=tv=0.0
    for c in candles:
        h=float(c[2]); l=float(c[3]); cl=float(c[4]); v=float(c[5])
        tp=(h+l+cl)/3; tpv+=tp*v; tv+=v
    return tpv/tv if tv>0 else 0.0

async def trend_gate(symbol, direction):
    c15=await get_klines(symbol,"15",60)
    c5 =await get_klines(symbol,"5", 30)
    if not c15 or not c5: return False,{"reason":"no data"}
    ut=_ut_bot(c15); adx=_adx(c15); vwap=_vwap(c5); price=float(c5[-1][4])
    ut_ok  =ut==direction or ut=="neutral"
    adx_ok =adx>=25
    vwap_ok=price>vwap if direction=="long" else price<vwap
    passed =ut_ok and adx_ok and vwap_ok
    info   ={"ut":ut,"adx":round(adx,1),"vwap":round(vwap,4),"price":round(price,4),"passed":passed}
    if not passed:
        fails=[]
        if not ut_ok:   fails.append(f"UT={ut}")
        if not adx_ok:  fails.append(f"ADX={adx:.1f}<25")
        if not vwap_ok: fails.append("Price wrong side of VWAP")
        info["reason"]=" | ".join(fails)
    return passed, info

# ── LAYER 2: ENTRY SCORE ─────────────────────────────────────

async def score_signal(symbol, direction):
    ob_data,trade_data,oi_data,ticker = await asyncio.gather(
        get_orderbook(symbol), get_recent_trades(symbol),
        get_open_interest(symbol), get_ticker(symbol),
    )
    score=0; details={}; obi=0.5; buy_ratio=0.5; oi_signal="neutral"

    # OBI 35pts
    bids=ob_data.get("b",[]); asks=ob_data.get("a",[])
    if bids and asks:
        bv=sum(float(b[1]) for b in bids); av=sum(float(a[1]) for a in asks); tot=bv+av
        obi=bv/tot if tot>0 else 0.5
        dist=max(0,obi-0.5)*2 if direction=="long" else max(0,0.5-obi)*2
        obi_pts=min(dist*35,35); score+=obi_pts
        bvols=[float(b[1]) for b in bids]
        stack=len(bvols)>=10 and sum(bvols[:5])/5>=sum(bvols[-5:])/5*1.5
        if stack and direction=="long": score+=5
        details.update({"obi":round(obi,4),"bid_stack":stack,"obi_pts":round(obi_pts,1)})

    # CVD 35pts
    if trade_data:
        bvol=sum(float(t["size"]) for t in trade_data if t.get("side")=="Buy")
        svol=sum(float(t["size"]) for t in trade_data if t.get("side")=="Sell")
        tot=bvol+svol; buy_ratio=bvol/tot if tot>0 else 0.5
        dist2=max(0,buy_ratio-0.5)*2 if direction=="long" else max(0,0.5-buy_ratio)*2
        flow_pts=min(dist2*35,35); score+=flow_pts
        f50=trade_data[100:150] if len(trade_data)>=150 else trade_data[:len(trade_data)//2]
        l50=trade_data[:50]
        fb=sum(float(t["size"]) for t in f50 if t.get("side")=="Buy")
        lb=sum(float(t["size"]) for t in l50 if t.get("side")=="Buy")
        if lb>fb*1.2 and direction=="long": score+=3
        if len(trade_data)>=100:
            early=trade_data[len(trade_data)//2:]; late=trade_data[:50]
            ep=float(early[0]["price"]) if early else 0
            lp=float(late[0]["price"])  if late  else 0
            ec=sum(float(t["size"]) for t in early if t.get("side")=="Buy")-sum(float(t["size"]) for t in early if t.get("side")=="Sell")
            lc=sum(float(t["size"]) for t in late  if t.get("side")=="Buy")-sum(float(t["size"]) for t in late  if t.get("side")=="Sell")
            if ep>0 and (lp>ep)!=(lc>ec): score-=8
        details.update({"buy_ratio":round(buy_ratio,4),"cvd_delta":round(bvol-svol,2),"flow_pts":round(flow_pts,1)})

    # OI Delta 20pts
    if oi_data and len(oi_data)>=2:
        try:
            oi_now=float(oi_data[0]["openInterest"]); oi_prev=float(oi_data[-1]["openInterest"])
            oi_pct=(oi_now-oi_prev)/oi_prev*100 if oi_prev>0 else 0
            price_now=float(ticker.get("lastPrice",0))
            price_prev=float(ticker.get("prevPrice24h",price_now) or price_now)
            price_up=price_now>price_prev
            if oi_pct>0.3 and price_up:          oi_signal="long";  score+=20
            elif oi_pct>0.3 and not price_up:    oi_signal="short"; score+=(20 if direction=="short" else 0)
            elif oi_pct<-0.3:                    oi_signal="unwind"; score-=5
            else:                                score+=5
            details.update({"oi_pct":round(oi_pct,3),"oi_signal":oi_signal})
        except Exception as e:
            print(f"OI score err: {e}"); score+=5
    else:
        score+=5

    # All 3 agree +10
    votes=0
    if obi>0.58 and direction=="long":    votes+=1
    if obi<0.42 and direction=="short":   votes+=1
    if buy_ratio>0.55 and direction=="long":  votes+=1
    if buy_ratio<0.45 and direction=="short": votes+=1
    if oi_signal==direction:              votes+=1
    if votes==3: score+=10
    details["votes"]=votes

    score=max(0,min(100,int(score)))
    if   score>=90: label="VERY STRONG"
    elif score>=75: label="STRONG"
    elif score>=55: label="MEDIUM"
    else:           label="WEAK"
    return score, label, details

# ── SIZING & TP/SL ───────────────────────────────────────────

def get_sizing(score, balance):
    pct=25.0 if score>=90 else 20.0 if score>=80 else 15.0
    return {"pct":pct,"size":round(min(balance*pct/100, balance*0.30),2)}

def get_tp_sl(score, entry, direction):
    if   score>=90: tp1,tp2,tp3,sl=10.0,25.0,40.0,4.0
    elif score>=80: tp1,tp2,tp3,sl=7.0,18.0,30.0,3.5
    else:           tp1,tp2,tp3,sl=5.0,12.0,20.0,3.0
    m=1 if direction=="long" else -1
    return {"tp1":round(entry*(1+m*tp1/100),6),"tp2":round(entry*(1+m*tp2/100),6),
            "tp3":round(entry*(1+m*tp3/100),6),"sl":round(entry*(1-m*sl/100),6),
            "tp1_pct":tp1,"tp2_pct":tp2,"tp3_pct":tp3,"sl_pct":sl}

# ── EXIT 2: MOMENTUM EXHAUSTION ──────────────────────────────

async def check_momentum_exit(symbol, pos):
    entry=pos["entry"]; side=pos["side"]
    c5=await get_klines(symbol,"5",15)
    if not c5 or len(c5)<10: return False,""
    price=float(c5[-1][4])
    pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100
    if pnl<2.0: return False,""
    bodies=[abs(float(c[4])-float(c[1])) for c in c5]
    shrinking=bodies[-3]>bodies[-2]>bodies[-1]
    avg=sum(bodies[-10:])/10; tiny=bodies[-1]<avg*0.40
    trades=await get_recent_trades(symbol); cvd_falling=False
    if trades and len(trades)>=100:
        f50=trades[50:100]; l50=trades[:50]
        fc=sum(float(t["size"]) for t in f50 if t.get("side")=="Buy")-sum(float(t["size"]) for t in f50 if t.get("side")=="Sell")
        lc=sum(float(t["size"]) for t in l50 if t.get("side")=="Buy")-sum(float(t["size"]) for t in l50 if t.get("side")=="Sell")
        cvd_falling=(lc<fc*0.6 if side=="long" else lc>fc*0.6)
    should=shrinking and tiny and cvd_falling
    reason=f"Momentum exhaustion at {pnl:.1f}% profit" if should else ""
    return should, reason

# ── EXIT 3: STRUCTURE BREAK ──────────────────────────────────

async def check_structure_break(symbol, pos):
    entry=pos["entry"]; side=pos["side"]
    c5=await get_klines(symbol,"5",10)
    if not c5: return False,""
    price=float(c5[-1][4])
    pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100
    if pnl<1.0: return False,""
    trades=await get_recent_trades(symbol)
    if not trades or len(trades)<100: return False,""
    early=trades[len(trades)//2:]; late=trades[:50]
    ep=float(early[0]["price"]) if early else 0; lp=float(late[0]["price"]) if late else 0
    if ep==0: return False,""
    ec=sum(float(t["size"]) for t in early if t.get("side")=="Buy")-sum(float(t["size"]) for t in early if t.get("side")=="Sell")
    lc=sum(float(t["size"]) for t in late  if t.get("side")=="Buy")-sum(float(t["size"]) for t in late  if t.get("side")=="Sell")
    bad=(side=="long" and not(lp>ep) and (lc>ec)) or (side=="short" and (lp>ep) and not(lc>ec))
    reason=f"Structure break at {pnl:.1f}% profit" if bad else ""
    return bad, reason

# ── SCAN LOOP ────────────────────────────────────────────────

async def scan_once():
    if state["position"]:
        await manage_position(); return
    state["balance"]=await get_balance()
    if state["balance"]<10:
        print(f"Low balance: {state['balance']}"); return
    print(f"\n── Scan │ Balance:${state['balance']:.2f} ──")
    for symbol in WATCHLIST:
        if state["position"]: break
        try:
            await scan_symbol(symbol)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Scan err {symbol}: {e}")

async def scan_symbol(symbol):
    ticker=await get_ticker(symbol)
    if not ticker: return
    price=float(ticker.get("lastPrice",0))
    price24=float(ticker.get("prevPrice24h",price) or price)
    change=(price-price24)/price24*100 if price24>0 else 0
    vol=float(ticker.get("volume24h",0))
    if vol<500_000 or abs(change)>15: return
    direction="long" if change>0 else "short"
    gate_ok,gate_info=await trend_gate(symbol,direction)
    if not gate_ok:
        state["trend_blocks"]+=1
        print(f"  BLOCKED {symbol} │ {gate_info.get('reason','')}"); return
    score,label,details=await score_signal(symbol,direction)
    if score<MIN_SCORE:
        state["score_blocks"]+=1
        print(f"  BLOCKED {symbol} │ Score {score}<{MIN_SCORE}"); return
    print(f"  ✅ SIGNAL {symbol} │ {direction.upper()} │ {score} ({label})")
    state["last_signal"]={"symbol":symbol,"direction":direction,"score":score,
                           "label":label,"gate":gate_info,"details":details,"time":int(time.time())}
    await enter_trade(symbol,direction,score,label,price)

async def enter_trade(symbol,direction,score,label,price):
    sizing=get_sizing(score,state["balance"]); tpsl=get_tp_sl(score,price,direction)
    qty=round(sizing["size"]*LEVERAGE/price,3)
    if qty<=0: return
    resp=await place_order(symbol,direction,qty,tpsl["sl"],tpsl["tp1"])
    if not resp or resp.get("retCode")!=0:
        print(f"Order failed {symbol}: {resp}"); return
    print(f"  ORDER {symbol} qty={qty} TP1={tpsl['tp1']} TP2={tpsl['tp2']} TP3={tpsl['tp3']} SL={tpsl['sl']}")
    state["position"]={
        "symbol":symbol,"side":direction,"entry":price,"qty":qty,"qty_remaining":qty,
        "score":score,"label":label,"tp1":tpsl["tp1"],"tp2":tpsl["tp2"],"tp3":tpsl["tp3"],
        "sl":tpsl["sl"],"tp1_pct":tpsl["tp1_pct"],"tp2_pct":tpsl["tp2_pct"],"tp3_pct":tpsl["tp3_pct"],
        "sl_pct":tpsl["sl_pct"],"tp1_hit":False,"tp2_hit":False,"be_moved":False,"open_time":int(time.time()),
    }
    await save_to_supabase(state["position"],"open",price)

async def manage_position():
    pos=state["position"]
    if not pos: return
    symbol=pos["symbol"]; side=pos["side"]; entry=pos["entry"]
    ticker=await get_ticker(symbol)
    if not ticker: return
    price=float(ticker.get("lastPrice",0))
    if price==0: return
    pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100

    if not pos["tp1_hit"]:
        if (side=="long" and price>=pos["tp1"]) or (side=="short" and price<=pos["tp1"]):
            print(f"  TP1 HIT {symbol} │ SL→breakeven")
            pos["tp1_hit"]=True; pos["be_moved"]=True
            await update_sl(symbol, entry*1.001 if side=="long" else entry*0.999)
            cq=round(pos["qty"]*0.40,3)
            await close_full(symbol,cq,side)
            pos["qty_remaining"]=round(pos["qty_remaining"]-cq,3)

    if pos["tp1_hit"] and not pos["tp2_hit"]:
        if (side=="long" and price>=pos["tp2"]) or (side=="short" and price<=pos["tp2"]):
            print(f"  TP2 HIT {symbol} │ Trailing SL")
            pos["tp2_hit"]=True
            trail=pos["tp1"]*1.001 if side=="long" else pos["tp1"]*0.999
            await update_sl(symbol,trail)
            cq=round(pos["qty"]*0.40,3)
            await close_full(symbol,cq,side)
            pos["qty_remaining"]=round(pos["qty_remaining"]-cq,3)

    if pos["tp2_hit"]:
        if (side=="long" and price>=pos["tp3"]) or (side=="short" and price<=pos["tp3"]):
            print(f"  TP3 HIT {symbol}")
            await close_full(symbol,pos["qty_remaining"],side)
            await record_close(pos,price,"win","TP3"); return

    if (side=="long" and price<=pos["sl"]) or (side=="short" and price>=pos["sl"]):
        print(f"  SL HIT {symbol}")
        await close_full(symbol,pos["qty_remaining"],side)
        await record_close(pos,price,"loss","SL"); return

    me,mr=await check_momentum_exit(symbol,pos)
    if me:
        print(f"  MOMENTUM EXIT {symbol} │ {mr}")
        await close_full(symbol,pos["qty_remaining"],side)
        await record_close(pos,price,"win" if pnl>0 else "loss","MomentumExit"); return

    sb,sr=await check_structure_break(symbol,pos)
    if sb:
        print(f"  STRUCTURE BREAK {symbol} │ {sr}")
        await close_full(symbol,pos["qty_remaining"],side)
        await record_close(pos,price,"win" if pnl>0 else "loss","StructureBreak"); return

    print(f"  HOLDING {symbol} │ {pnl:+.2f}% │ TP1={'✓' if pos['tp1_hit'] else '○'} TP2={'✓' if pos['tp2_hit'] else '○'}")

async def record_close(pos,exit_price,result,exit_type):
    entry=pos["entry"]; side=pos["side"]
    pnl_pct=(exit_price-entry)/entry*100 if side=="long" else (entry-exit_price)/entry*100
    pnl_usd=pos["qty"]*entry*(pnl_pct/100)*LEVERAGE
    trade={"symbol":pos["symbol"],"direction":side,"result":result,"entry":entry,
           "exit_price":exit_price,"pnl":round(pnl_usd,4),"pnl_pct":round(pnl_pct,3),
           "score":pos["score"],"label":pos["label"],"tp1_hit":pos["tp1_hit"],
           "tp2_hit":pos["tp2_hit"],"exit_type":exit_type,"open_time":pos["open_time"],
           "close_time":int(time.time())}
    state["trades"].append(trade); state["trades"]=state["trades"][-100:]
    if result=="win": state["wins"]+=1
    else:             state["losses"]+=1
    state["total_pnl"]+=pnl_usd; state["position"]=None
    print(f"  CLOSED {result.upper()} │ {exit_type} │ {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
    await save_to_supabase(trade,result,exit_price)

# ── SUPABASE ─────────────────────────────────────────────────

async def save_to_supabase(trade,result,price):
    if not SB_URL or not SB_KEY: return
    try:
        payload={"symbol":trade.get("symbol"),"direction":trade.get("direction",trade.get("side")),
                 "result":result,"entry":trade.get("entry"),"exit_price":price,
                 "pnl":trade.get("pnl",0),"pnl_pct":trade.get("pnl_pct",0),
                 "score":trade.get("score"),"label":trade.get("label"),
                 "tp1_hit":trade.get("tp1_hit",False),"tp2_hit":trade.get("tp2_hit",False),
                 "exit_type":trade.get("exit_type","")}
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{SB_URL}/rest/v1/trades",json=payload,
                        headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                                 "Content-Type":"application/json","Prefer":"return=minimal"})
    except Exception as e:
        print(f"Supabase save err: {e}")

async def load_from_supabase():
    if not SB_URL or not SB_KEY: return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r=await c.get(f"{SB_URL}/rest/v1/trades?select=result,pnl&order=created_at.asc",
                         headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}"})
            trades=r.json()
            if isinstance(trades,list):
                state["wins"]     =sum(1 for t in trades if (t.get("result","")).lower()=="win")
                state["losses"]   =sum(1 for t in trades if (t.get("result","")).lower()=="loss")
                state["total_pnl"]=round(sum(float(t.get("pnl",0)) for t in trades),4)
                state["trades"]   =trades[-50:]
                print(f"Restored: W={state['wins']} L={state['losses']} PnL=${state['total_pnl']:.2f}")
    except Exception as e:
        print(f"Supabase load err: {e}")

# ── BOT LOOP ─────────────────────────────────────────────────

async def bot_loop():
    print("APEX Pro v2.0 starting...")
    await load_from_supabase()
    state["status"]="running"
    while True:
        try:
            state["last_scan"]=int(time.time())
            await scan_once()
        except Exception as e:
            print(f"Loop err: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_loop())

# ── DASHBOARD ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    pos=state["position"]
    wins=state["wins"]; losses=state["losses"]; total=wins+losses
    wr=(wins/total*100) if total>0 else 0
    trades=state["trades"]
    avg_win =sum(t.get("pnl",0) for t in trades if (t.get("result","")).lower()=="win") /max(wins,1)
    avg_loss=sum(t.get("pnl",0) for t in trades if (t.get("result","")).lower()=="loss")/max(losses,1)
    pnl_col="#2d7a4f" if state['total_pnl']>=0 else "#8b1a2e"
    pnl_sign="+" if state['total_pnl']>=0 else ""

    # ── Active position card ──
    pos_html=""
    if pos:
        try:
            tk=await get_ticker(pos["symbol"]); price=float(tk.get("lastPrice",pos["entry"]))
        except: price=pos["entry"]
        pnl_p=(price-pos["entry"])/pos["entry"]*100 if pos["side"]=="long" else (pos["entry"]-price)/pos["entry"]*100
        pcol="#2d7a4f" if pnl_p>=0 else "#8b1a2e"
        pcol_t="#1a9e5c" if pnl_p>=0 else "#cc3355"
        tp1c="tp-hit" if pos["tp1_hit"] else "tp-open"
        tp2c="tp-hit" if pos["tp2_hit"] else "tp-open"
        side_cls="side-long" if pos["side"]=="long" else "side-short"
        pos_html=f"""
        <div class="pos-card">
          <div class="pos-top">
            <div class="pos-left">
              <span class="pos-sym">{pos['symbol'].replace('USDT','')}<span class="pos-usdt">/USDT</span></span>
              <span class="pos-dir {side_cls}">{pos['side'].upper()}</span>
            </div>
            <div class="pos-score-wrap">
              <span class="pos-score-num">{pos['score']}</span>
              <span class="pos-score-lbl">{pos['label']}</span>
            </div>
          </div>
          <div class="pos-metrics">
            <div class="pm"><span class="pm-l">Entry</span><span class="pm-v">${pos['entry']:.4f}</span></div>
            <div class="pm"><span class="pm-l">Mark</span><span class="pm-v" style="color:{pcol_t}">${price:.4f}</span></div>
            <div class="pm"><span class="pm-l">P&L</span><span class="pm-v" style="color:{pcol_t}">{pnl_p:+.2f}%</span></div>
            <div class="pm"><span class="pm-l">Stop</span><span class="pm-v loss-col">${pos['sl']:.4f}</span></div>
          </div>
          <div class="tp-track">
            <div class="tp-item {tp1c}">
              <span class="tp-label">TP1</span>
              <span class="tp-pct">+{pos['tp1_pct']}%</span>
              {"<span class='tp-check'>✓</span>" if pos['tp1_hit'] else ""}
            </div>
            <div class="tp-line"></div>
            <div class="tp-item {tp2c}">
              <span class="tp-label">TP2</span>
              <span class="tp-pct">+{pos['tp2_pct']}%</span>
              {"<span class='tp-check'>✓</span>" if pos['tp2_hit'] else ""}
            </div>
            <div class="tp-line"></div>
            <div class="tp-item tp-open">
              <span class="tp-label">TP3</span>
              <span class="tp-pct">+{pos['tp3_pct']}%</span>
            </div>
          </div>
          {"<div class='be-pill'>⬆ Stop moved to breakeven</div>" if pos.get('be_moved') else ""}
        </div>"""

    # ── Trade log ──
    tlog=""
    for t in reversed(trades[-15:]):
        res=(t.get("result","")).lower(); pnl=t.get("pnl",0)
        is_win=res=="win"
        d=(t.get("direction",t.get("side",""))).upper()
        is_long=d in ["LONG","BUY"]
        tlog+=f"""
        <div class="trow">
          <div class="trow-left">
            <span class="tsym">{t.get('symbol','').replace('USDT','')}</span>
            <span class="tdir {'tlong' if is_long else 'tshort'}">{d}</span>
          </div>
          <div class="trow-mid">
            <span class="tscore">{t.get('score','—')}</span>
            <span class="texit">{t.get('exit_type','—')}</span>
          </div>
          <div class="trow-right">
            <span class="tres {'tres-w' if is_win else 'tres-l'}">{res.upper()}</span>
            <span class="tpnl {'pnl-pos' if pnl>=0 else 'pnl-neg'}">{'+' if pnl>=0 else ''}${pnl:.2f}</span>
          </div>
        </div>"""

    last_sig=state.get("last_signal")
    sig_html=""
    if last_sig:
        age=int(time.time())-last_sig.get("time",0)
        sig_html=f"""
        <div class="sig-pill">
          Last signal: <strong>{last_sig['symbol']}</strong> {last_sig['direction'].upper()}
          · Score {last_sig['score']} · {age}s ago
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX Pro</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap');

:root {{
  --cream:   #f5f0e8;
  --cream2:  #ede7d9;
  --cream3:  #e0d8c8;
  --green:   #1a6b45;
  --green2:  #1f8554;
  --green3:  #2aad6e;
  --green4:  #d4edd8;
  --red:     #8b1a2e;
  --red2:    #cc3355;
  --red4:    #f2d4d8;
  --gold:    #8b6914;
  --gold2:   #b8892a;
  --ink:     #1c1a16;
  --ink2:    #3d3a32;
  --ink3:    #6b6555;
  --border:  #d4ccb8;
  --border2: #c4baa8;
  --shadow:  rgba(28,26,22,0.08);
}}

* {{ margin:0; padding:0; box-sizing:border-box; }}

body {{
  background: var(--cream);
  color: var(--ink);
  font-family: 'DM Sans', sans-serif;
  min-height: 100vh;
  padding: 0 0 60px;
}}

/* ── Header ── */
.header {{
  background: var(--ink);
  padding: 18px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 100;
}}

.logo {{
  font-family: 'Cormorant Garamond', serif;
  font-size: 22px;
  font-weight: 600;
  color: var(--cream);
  letter-spacing: 0.04em;
}}

.logo-accent {{ color: var(--green3); }}
.logo-ver {{ font-size: 11px; color: var(--ink3); font-family: 'DM Mono', monospace; margin-left: 6px; vertical-align: middle; }}

.status-pill {{
  display: flex;
  align-items: center;
  gap: 7px;
  background: rgba(42,173,110,0.12);
  border: 1px solid rgba(42,173,110,0.25);
  border-radius: 30px;
  padding: 5px 14px;
  font-family: 'DM Mono', monospace;
  font-size: 11px;
  color: var(--green3);
  letter-spacing: 0.08em;
}}

.status-dot {{
  width: 7px; height: 7px;
  background: var(--green3);
  border-radius: 50%;
  animation: blink 1.8s infinite;
}}

@keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:0.2}} }}

/* ── Main layout ── */
.main {{ padding: 20px 16px; max-width: 520px; margin: 0 auto; }}

/* ── Section label ── */
.section-label {{
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink3);
  margin: 24px 0 10px;
  display: flex;
  align-items: center;
  gap: 10px;
}}
.section-label::after {{
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}}

/* ── Stat cards ── */
.stat-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}

.stat-card {{
  background: white;
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 18px 16px;
  box-shadow: 0 2px 12px var(--shadow);
  position: relative;
  overflow: hidden;
}}

.stat-card::before {{
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: var(--cream2);
}}

.stat-card.green-top::before {{ background: var(--green3); }}
.stat-card.red-top::before   {{ background: var(--red2); }}
.stat-card.gold-top::before  {{ background: var(--gold2); }}

.stat-lbl {{
  font-family: 'DM Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink3);
  margin-bottom: 8px;
  display: block;
}}

.stat-val {{
  font-family: 'Cormorant Garamond', serif;
  font-size: 28px;
  font-weight: 600;
  line-height: 1;
  color: var(--ink);
}}

.stat-val.positive {{ color: var(--green2); }}
.stat-val.negative {{ color: var(--red2); }}
.stat-val.amber    {{ color: var(--gold); }}

.stat-sub {{
  font-size: 11px;
  color: var(--ink3);
  margin-top: 5px;
  font-family: 'DM Mono', monospace;
}}

/* ── Info card ── */
.info-card {{
  background: white;
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 4px 0;
  box-shadow: 0 2px 12px var(--shadow);
  margin-bottom: 4px;
}}

.info-row {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 18px;
  border-bottom: 1px solid var(--cream2);
  font-size: 13px;
}}

.info-row:last-child {{ border-bottom: none; }}

.info-row .lbl {{
  color: var(--ink3);
  font-size: 12px;
  font-family: 'DM Mono', monospace;
}}

.info-row .val {{
  font-family: 'DM Mono', monospace;
  font-weight: 500;
  color: var(--ink);
  font-size: 13px;
}}

.score-badge-info {{
  background: var(--green4);
  color: var(--green);
  border: 1px solid rgba(26,107,69,0.2);
  border-radius: 20px;
  padding: 2px 12px;
  font-size: 11px;
  font-family: 'DM Mono', monospace;
  font-weight: 500;
}}

/* ── Position card ── */
.pos-card {{
  background: white;
  border: 1px solid var(--border2);
  border-radius: 20px;
  padding: 20px;
  box-shadow: 0 4px 20px var(--shadow);
  position: relative;
  overflow: hidden;
}}

.pos-card::before {{
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 4px;
  background: linear-gradient(90deg, var(--green2), var(--green3));
}}

.pos-top {{
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 18px;
}}

.pos-left {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}

.pos-sym {{
  font-family: 'Cormorant Garamond', serif;
  font-size: 24px;
  font-weight: 600;
  color: var(--ink);
}}

.pos-usdt {{ font-size: 14px; color: var(--ink3); }}

.pos-dir {{
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  font-weight: 500;
  padding: 3px 12px;
  border-radius: 20px;
  letter-spacing: 0.08em;
}}

.side-long  {{ background: var(--green4); color: var(--green);  border: 1px solid rgba(26,107,69,0.2); }}
.side-short {{ background: var(--red4);   color: var(--red);    border: 1px solid rgba(139,26,46,0.2); }}

.pos-score-wrap {{ text-align: right; }}
.pos-score-num {{
  display: block;
  font-family: 'Cormorant Garamond', serif;
  font-size: 32px;
  font-weight: 600;
  color: var(--green2);
  line-height: 1;
}}
.pos-score-lbl {{
  font-family: 'DM Mono', monospace;
  font-size: 9px;
  color: var(--ink3);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}}

.pos-metrics {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 18px;
  background: var(--cream2);
  border-radius: 12px;
  padding: 14px;
}}

.pm {{ display: flex; flex-direction: column; gap: 3px; }}
.pm-l {{ font-size: 10px; color: var(--ink3); font-family: 'DM Mono', monospace; letter-spacing: 0.08em; text-transform: uppercase; }}
.pm-v {{ font-family: 'DM Mono', monospace; font-size: 14px; font-weight: 500; color: var(--ink); }}
.loss-col {{ color: var(--red2) !important; }}

/* ── TP track ── */
.tp-track {{
  display: flex;
  align-items: center;
  gap: 0;
  margin-bottom: 12px;
}}

.tp-line {{
  flex: 1;
  height: 2px;
  background: var(--border);
}}

.tp-item {{
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  position: relative;
}}

.tp-item::before {{
  content: '';
  width: 10px; height: 10px;
  border-radius: 50%;
  border: 2px solid var(--border2);
  background: white;
  margin-bottom: 2px;
}}

.tp-hit::before {{ background: var(--green3); border-color: var(--green3); }}

.tp-label {{ font-size: 10px; font-family: 'DM Mono', monospace; color: var(--ink3); letter-spacing: 0.06em; }}
.tp-pct   {{ font-size: 11px; font-family: 'DM Mono', monospace; color: var(--ink2); font-weight: 500; }}
.tp-check {{ font-size: 10px; color: var(--green3); }}

.be-pill {{
  background: var(--green4);
  border: 1px solid rgba(26,107,69,0.2);
  border-radius: 20px;
  padding: 5px 14px;
  font-size: 11px;
  color: var(--green);
  font-family: 'DM Mono', monospace;
  text-align: center;
}}

/* ── No position ── */
.no-pos {{
  background: white;
  border: 1px dashed var(--border2);
  border-radius: 20px;
  padding: 32px;
  text-align: center;
  color: var(--ink3);
  font-size: 13px;
}}

.no-pos-icon {{ font-size: 28px; margin-bottom: 10px; opacity: 0.4; }}

/* ── Trade rows ── */
.trades-card {{
  background: white;
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 2px 12px var(--shadow);
}}

.trow {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  border-bottom: 1px solid var(--cream2);
  gap: 8px;
}}

.trow:last-child {{ border-bottom: none; }}

.trow-left  {{ display: flex; align-items: center; gap: 8px; min-width: 110px; }}
.trow-mid   {{ display: flex; flex-direction: column; align-items: center; gap: 2px; flex: 1; }}
.trow-right {{ display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }}

.tsym {{
  font-family: 'Cormorant Garamond', serif;
  font-size: 16px;
  font-weight: 600;
  color: var(--ink);
}}

.tdir {{
  font-family: 'DM Mono', monospace;
  font-size: 9px;
  padding: 2px 8px;
  border-radius: 4px;
  letter-spacing: 0.06em;
}}

.tlong  {{ background: var(--green4); color: var(--green); }}
.tshort {{ background: var(--red4);   color: var(--red); }}

.tscore {{
  font-family: 'DM Mono', monospace;
  font-size: 13px;
  color: var(--ink2);
  font-weight: 500;
}}

.texit {{
  font-size: 10px;
  color: var(--ink3);
  font-family: 'DM Mono', monospace;
}}

.tres {{
  font-family: 'DM Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.1em;
  padding: 2px 8px;
  border-radius: 4px;
}}

.tres-w {{ background: var(--green4); color: var(--green); }}
.tres-l {{ background: var(--red4);   color: var(--red); }}

.tpnl {{
  font-family: 'DM Mono', monospace;
  font-size: 14px;
  font-weight: 500;
}}

.pnl-pos {{ color: var(--green2); }}
.pnl-neg {{ color: var(--red2); }}

/* ── Signal pill ── */
.sig-pill {{
  background: white;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px 16px;
  font-size: 12px;
  color: var(--ink3);
  font-family: 'DM Mono', monospace;
  margin-bottom: 8px;
}}

.sig-pill strong {{ color: var(--ink); }}

/* ── Footer bar ── */
.footer-bar {{
  position: fixed;
  bottom: 0; left: 0; right: 0;
  background: var(--ink);
  padding: 10px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  color: var(--ink3);
  letter-spacing: 0.06em;
}}

.footer-bar span {{ color: var(--cream); opacity: 0.4; }}

.no-trades {{
  text-align: center;
  padding: 24px;
  color: var(--ink3);
  font-size: 12px;
  font-family: 'DM Mono', monospace;
}}
</style>
</head>
<body>

<div class="header">
  <div class="logo">APEX <span class="logo-accent">Pro</span><span class="logo-ver">v2.0</span></div>
  <div class="status-pill"><span class="status-dot"></span>{state['status'].upper()}</div>
</div>

<div class="main">

  <!-- Stats -->
  <div class="section-label">Performance</div>
  <div class="stat-grid">
    <div class="stat-card {'green-top' if state['total_pnl']>=0 else 'red-top'}">
      <span class="stat-lbl">Total P&L</span>
      <div class="stat-val {'positive' if state['total_pnl']>=0 else 'negative'}">{pnl_sign}${state['total_pnl']:.2f}</div>
      <div class="stat-sub">{total} trades closed</div>
    </div>
    <div class="stat-card gold-top">
      <span class="stat-lbl">Win Rate</span>
      <div class="stat-val amber">{wr:.0f}%</div>
      <div class="stat-sub">{wins}W &nbsp;/&nbsp; {losses}L</div>
    </div>
    <div class="stat-card green-top">
      <span class="stat-lbl">Avg Win</span>
      <div class="stat-val positive">+${avg_win:.2f}</div>
      <div class="stat-sub">per trade</div>
    </div>
    <div class="stat-card red-top">
      <span class="stat-lbl">Avg Loss</span>
      <div class="stat-val negative">−${abs(avg_loss):.2f}</div>
      <div class="stat-sub">per trade</div>
    </div>
  </div>

  <!-- Bot info -->
  <div class="section-label">System</div>
  <div class="info-card">
    <div class="info-row">
      <span class="lbl">Balance</span>
      <span class="val">${state['balance']:.2f} USDT</span>
    </div>
    <div class="info-row">
      <span class="lbl">Min Score</span>
      <span class="score-badge-info">{MIN_SCORE} · STRONG only</span>
    </div>
    <div class="info-row">
      <span class="lbl">Trend Blocks</span>
      <span class="val">{state['trend_blocks']}</span>
    </div>
    <div class="info-row">
      <span class="lbl">Score Blocks</span>
      <span class="val">{state['score_blocks']}</span>
    </div>
    <div class="info-row">
      <span class="lbl">Last Scan</span>
      <span class="val">{int(time.time())-state['last_scan']}s ago</span>
    </div>
  </div>

  {sig_html}

  <!-- Position -->
  <div class="section-label">Open Position</div>
  {pos_html if pos_html else '''
  <div class="no-pos">
    <div class="no-pos-icon">◎</div>
    Scanning for signals...
  </div>'''}

  <!-- Trades -->
  <div class="section-label">Recent Trades</div>
  <div class="trades-card">
    {tlog if tlog else '<div class="no-trades">No trades recorded yet</div>'}
  </div>

</div>

<div class="footer-bar">
  <span>APEX PRO v2.0</span>
  <span>Bybit USDT Perps · {SCAN_INTERVAL}s scan</span>
</div>

<script>setTimeout(()=>location.reload(), 15000)</script>
</body></html>"""

@app.get("/api/status")
async def api_status():
    return JSONResponse({
        "status":state["status"],"balance":state["balance"],
        "wins":state["wins"],"losses":state["losses"],
        "total_pnl":state["total_pnl"],"min_score":MIN_SCORE,
        "trend_blocks":state["trend_blocks"],"score_blocks":state["score_blocks"],
        "position":state["position"],"last_signal":state["last_signal"],
    })

@app.get("/api/trades")
async def api_trades():
    return JSONResponse(state["trades"])

if __name__=="__main__":
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT",8000)))
