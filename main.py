"""
APEX Pro v2.0 — Interactive Dashboard
Full signal parameter visibility + interactive UI
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn, os, time, asyncio, hmac, hashlib, json
import httpx

app = FastAPI()

API_KEY       = os.getenv("BYBIT_API_KEY", "")
API_SECRET    = os.getenv("BYBIT_API_SECRET", "")
BASE_URL      = "https://api.bybit.com"
MIN_SCORE     = 70
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
    "trend_blocks": 0, "score_blocks": 0,
    "last_signal": None,
    "scan_log": [],        # last 10 scanned coins with full params
    "signal_params": {},   # latest signal parameters for display
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
    try:
        r = await _get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}, signed=True)
        coins = r["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                for field in ["availableToWithdraw","walletBalance","equity","availableBalance"]:
                    val = c.get(field, "")
                    if val and str(val).strip() not in ("","0","0.0"):
                        return float(val)
        total = r["result"]["list"][0].get("totalWalletBalance","0")
        if total and str(total).strip() not in ("","0"):
            return float(total)
    except Exception as e:
        print(f"Balance err: {e}")
    return 0.0

async def get_klines(symbol, interval="5", limit=60):
    try:
        r = await _get("/v5/market/kline", {
            "category":"linear","symbol":symbol,"interval":interval,"limit":str(limit)
        })
        return list(reversed(r["result"]["list"]))
    except Exception as e:
        print(f"Klines err {symbol}: {e}")
        return []

async def get_orderbook(symbol):
    try:
        r = await _get("/v5/market/orderbook",{"category":"linear","symbol":symbol,"limit":"50"})
        return r.get("result",{})
    except Exception as e:
        print(f"OB err {symbol}: {e}")
        return {}

async def get_recent_trades(symbol):
    try:
        r = await _get("/v5/market/recent-trade",{"category":"linear","symbol":symbol,"limit":"200"})
        return r.get("result",{}).get("list",[])
    except Exception as e:
        print(f"Trades err {symbol}: {e}")
        return []

async def get_open_interest(symbol):
    try:
        r = await _get("/v5/market/open-interest",{
            "category":"linear","symbol":symbol,"intervalTime":"5min","limit":"6"
        })
        return r.get("result",{}).get("list",[])
    except Exception as e:
        print(f"OI err {symbol}: {e}")
        return []

async def get_ticker(symbol):
    try:
        r = await _get("/v5/market/tickers",{"category":"linear","symbol":symbol})
        items = r.get("result",{}).get("list",[])
        return items[0] if items else {}
    except Exception as e:
        print(f"Ticker err {symbol}: {e}")
        return {}

async def place_order(symbol,side,qty,sl,tp1):
    return await _post("/v5/order/create",{
        "category":"linear","symbol":symbol,
        "side":"Buy" if side=="long" else "Sell",
        "orderType":"Market","qty":str(qty),
        "stopLoss":str(round(sl,6)),"takeProfit":str(round(tp1,6)),
        "tpslMode":"Full","leverage":str(LEVERAGE),"positionIdx":"0",
    })

async def close_full(symbol,qty,side):
    return await _post("/v5/order/create",{
        "category":"linear","symbol":symbol,
        "side":"Sell" if side=="long" else "Buy",
        "orderType":"Market","qty":str(qty),"reduceOnly":True,"positionIdx":"0",
    })

async def update_sl(symbol,sl_price):
    return await _post("/v5/position/trading-stop",{
        "category":"linear","symbol":symbol,
        "stopLoss":str(round(sl_price,6)),"positionIdx":"0",
    })

# ── LAYER 1: TREND GATE ──────────────────────────────────────

def _atr(candles,period=14):
    if len(candles)<period+1: return 0.0
    trs=[]
    for i in range(1,len(candles)):
        h=float(candles[i][2]);l=float(candles[i][3]);pc=float(candles[i-1][4])
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(trs[-period:])/period

def _adx(candles,period=14):
    if len(candles)<period*2+1: return 0.0
    pdms,ndms,trs=[],[],[]
    for i in range(1,len(candles)):
        h=float(candles[i][2]);ph=float(candles[i-1][2])
        l=float(candles[i][3]);pl=float(candles[i-1][3]);pc=float(candles[i-1][4])
        up=h-ph;dn=pl-l
        pdms.append(up if up>dn and up>0 else 0)
        ndms.append(dn if dn>up and dn>0 else 0)
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    def smooth(arr):
        s=sum(arr[:period]);out=[s]
        for v in arr[period:]:s=s-s/period+v;out.append(s)
        return out
    st=smooth(trs);sp=smooth(pdms);sn=smooth(ndms);dxs=[]
    for i in range(len(st)):
        if st[i]==0:continue
        pip=100*sp[i]/st[i];nim=100*sn[i]/st[i];d=pip+nim
        if d:dxs.append(100*abs(pip-nim)/d)
    return sum(dxs[-period:])/min(len(dxs),period) if dxs else 0.0

def _ut_bot(candles,mult=1.5,period=10):
    if len(candles)<period+5:return "neutral"
    atr=_atr(candles,period)
    if atr==0:return "neutral"
    trail=float(candles[0][4]);sig="neutral"
    for i in range(1,len(candles)):
        cl=float(candles[i][4]);pcl=float(candles[i-1][4]);dist=mult*atr
        trail=max(trail,cl-dist) if cl>trail else min(trail,cl+dist)
        if cl>trail and pcl<=trail:sig="long"
        elif cl<trail and pcl>=trail:sig="short"
    return sig

def _vwap(candles):
    tpv=tv=0.0
    for c in candles:
        h=float(c[2]);l=float(c[3]);cl=float(c[4]);v=float(c[5])
        tp=(h+l+cl)/3;tpv+=tp*v;tv+=v
    return tpv/tv if tv>0 else 0.0

async def trend_gate(symbol,direction):
    # VWAP only — real-time, no lag, fully symmetric
    # Price above VWAP = buyers in control = long valid
    # Price below VWAP = sellers in control = short valid
    c5=await get_klines(symbol,"5",30)
    if not c5:return False,{"reason":"no data"}
    vwap=_vwap(c5);price=float(c5[-1][4])
    vwap_ok=price>vwap if direction=="long" else price<vwap
    pct=round((price-vwap)/vwap*100,3)
    info={"ut":"removed","adx":"removed","vwap":round(vwap,6),
          "price":round(price,6),"price_vs_vwap_pct":pct,
          "ut_ok":True,"adx_ok":True,"vwap_ok":vwap_ok,"passed":vwap_ok}
    if not vwap_ok:
        info["reason"]=f"Price {'below' if direction=='long' else 'above'} VWAP ({pct:+.2f}%)"
    return vwap_ok,info

# ── LAYER 2: ENTRY SCORE ─────────────────────────────────────

async def score_signal(symbol,direction):
    """
    FULLY SYMMETRIC scoring — long and short get identical treatment.
    Long signals:  high bids, buy aggression, sell accel for shorts etc.
    Short signals: high asks, sell aggression, ask stacking, OI rising+price falling
    Neither direction has structural advantage.
    """
    ob_data,trade_data,oi_data,ticker=await asyncio.gather(
        get_orderbook(symbol),get_recent_trades(symbol),
        get_open_interest(symbol),get_ticker(symbol),
    )
    score=0;details={};obi=0.5;buy_ratio=0.5;oi_signal="neutral"

    # ── OBI — 35 pts + 5 stacking bonus (symmetric) ──────────
    # LONG:  bids dominate → buyers loading up
    # SHORT: asks dominate → sellers loading up
    bids=ob_data.get("b",[]);asks=ob_data.get("a",[])
    if bids and asks:
        bv=sum(float(b[1]) for b in bids)
        av=sum(float(a[1]) for a in asks)
        tot=bv+av
        obi=bv/tot if tot>0 else 0.5
        # Both directions use same distance formula — just mirrored
        dist=max(0,obi-0.5)*2 if direction=="long" else max(0,0.5-obi)*2
        obi_pts=min(dist*35,35);score+=obi_pts

        # Stacking bonus — symmetric
        # LONG:  top bid levels bigger than bottom bid levels
        # SHORT: top ask levels bigger than bottom ask levels
        bvols=[float(b[1]) for b in bids]
        avols=[float(a[1]) for a in asks]
        bid_stack=len(bvols)>=10 and sum(bvols[:5])/5>=sum(bvols[-5:])/5*1.5
        ask_stack=len(avols)>=10 and sum(avols[:5])/5>=sum(avols[-5:])/5*1.5
        if direction=="long"  and bid_stack: score+=5
        if direction=="short" and ask_stack: score+=5

        details.update({
            "obi":round(obi,4),"obi_pct":round(obi*100,1),
            "bid_vol":round(bv,2),"ask_vol":round(av,2),
            "bid_stack":bid_stack,"ask_stack":ask_stack,
            "obi_pts":round(obi_pts,1),
        })

    # ── CVD — 35 pts + 3 acceleration bonus (symmetric) ──────
    # LONG:  buy_ratio > 0.5, buying accelerating in recent trades
    # SHORT: sell_ratio > 0.5 (buy_ratio < 0.5), selling accelerating
    if trade_data:
        bvol=sum(float(t["size"]) for t in trade_data if t.get("side")=="Buy")
        svol=sum(float(t["size"]) for t in trade_data if t.get("side")=="Sell")
        tot=bvol+svol
        buy_ratio=bvol/tot if tot>0 else 0.5
        sell_ratio=1-buy_ratio
        cvd_delta=bvol-svol

        dist2=max(0,buy_ratio-0.5)*2 if direction=="long" else max(0,0.5-buy_ratio)*2
        flow_pts=min(dist2*35,35);score+=flow_pts

        # Acceleration — symmetric
        # LONG:  recent buys > early buys (buy momentum building)
        # SHORT: recent sells > early sells (sell momentum building)
        f50=trade_data[100:150] if len(trade_data)>=150 else trade_data[:len(trade_data)//2]
        l50=trade_data[:50]
        fb=sum(float(t["size"]) for t in f50 if t.get("side")=="Buy")
        fs=sum(float(t["size"]) for t in f50 if t.get("side")=="Sell")
        lb=sum(float(t["size"]) for t in l50 if t.get("side")=="Buy")
        ls=sum(float(t["size"]) for t in l50 if t.get("side")=="Sell")
        buy_accel  = lb > fb*1.2   # buying accelerating
        sell_accel = ls > fs*1.2   # selling accelerating
        if direction=="long"  and buy_accel:  score+=3
        if direction=="short" and sell_accel: score+=3

        # CVD Divergence penalty — symmetric
        # Price moving one way but CVD moving other = fake move = penalty both directions
        diverge=False
        if len(trade_data)>=100:
            early=trade_data[len(trade_data)//2:];late=trade_data[:50]
            ep=float(early[0]["price"]) if early else 0
            lp=float(late[0]["price"])  if late  else 0
            ec=sum(float(t["size"]) for t in early if t.get("side")=="Buy")-\
               sum(float(t["size"]) for t in early if t.get("side")=="Sell")
            lc=sum(float(t["size"]) for t in late  if t.get("side")=="Buy")-\
               sum(float(t["size"]) for t in late  if t.get("side")=="Sell")
            if ep>0 and (lp>ep)!=(lc>ec):diverge=True;score-=8

        details.update({
            "buy_ratio":round(buy_ratio,4),"buy_pct":round(buy_ratio*100,1),
            "sell_pct":round(sell_ratio*100,1),
            "cvd_delta":round(cvd_delta,2),
            "buy_accel":buy_accel,"sell_accel":sell_accel,
            "accel": buy_accel if direction=="long" else sell_accel,
            "cvd_diverge":diverge,"flow_pts":round(flow_pts,1),
        })

    # ── OI Delta — 20 pts (symmetric) ────────────────────────
    # LONG:  OI rising + price rising   = real longs entering  → +20
    # SHORT: OI rising + price falling  = real shorts entering → +20
    # Both: OI falling = positions unwinding = no conviction   → -5
    if oi_data and len(oi_data)>=2:
        try:
            oi_now=float(oi_data[0]["openInterest"])
            oi_prev=float(oi_data[-1]["openInterest"])
            oi_pct=(oi_now-oi_prev)/oi_prev*100 if oi_prev>0 else 0
            price_now=float(ticker.get("lastPrice",0))
            price_prev=float(ticker.get("prevPrice24h",price_now) or price_now)
            price_up=price_now>price_prev

            if   oi_pct>0.3 and price_up:       oi_signal="long";  score+=20
            elif oi_pct>0.3 and not price_up:   oi_signal="short"; score+=20  # ← was 0 before!
            elif oi_pct<-0.3:                   oi_signal="unwind";score-=5
            else:                               oi_signal="neutral";score+=5

            details.update({
                "oi_pct":round(oi_pct,3),"oi_now":round(oi_now,0),
                "oi_signal":oi_signal,"price_up":price_up,
            })
        except Exception as e:
            print(f"OI score err:{e}");score+=5
    else:
        score+=5

    # ── All 3 agree bonus — +10 pts (symmetric) ──────────────
    votes=0
    if direction=="long":
        if obi>0.58:          votes+=1   # bids dominating
        if buy_ratio>0.55:    votes+=1   # buyers aggressive
        if oi_signal=="long": votes+=1   # new longs entering
    else:
        if obi<0.42:           votes+=1  # asks dominating
        if buy_ratio<0.45:     votes+=1  # sellers aggressive
        if oi_signal=="short": votes+=1  # new shorts entering
    if votes==3:score+=10
    details["votes"]=votes;details["votes_agree"]=(votes==3)

    score=max(0,min(100,int(score)))
    if   score>=90:label="VERY STRONG"
    elif score>=70:label="STRONG"
    elif score>=55:label="MEDIUM"
    else:          label="WEAK"
    return score,label,details

# ── SIZING & TP/SL ───────────────────────────────────────────

def get_sizing(score,balance):
    pct=25.0 if score>=90 else 20.0 if score>=80 else 15.0
    return {"pct":pct,"size":round(min(balance*pct/100,balance*0.30),2)}

def get_tp_sl(score,entry,direction):
    if   score>=90:tp1,tp2,tp3,sl=10.0,25.0,40.0,4.0
    elif score>=80:tp1,tp2,tp3,sl=7.0,18.0,30.0,3.5
    else:          tp1,tp2,tp3,sl=5.0,12.0,20.0,3.0
    m=1 if direction=="long" else -1
    return {"tp1":round(entry*(1+m*tp1/100),6),"tp2":round(entry*(1+m*tp2/100),6),
            "tp3":round(entry*(1+m*tp3/100),6),"sl":round(entry*(1-m*sl/100),6),
            "tp1_pct":tp1,"tp2_pct":tp2,"tp3_pct":tp3,"sl_pct":sl}

# ── EXIT 2 & 3 ───────────────────────────────────────────────

async def check_momentum_exit(symbol,pos):
    entry=pos["entry"];side=pos["side"]
    c5=await get_klines(symbol,"5",15)
    if not c5 or len(c5)<10:return False,""
    price=float(c5[-1][4])
    pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100
    if pnl<2.0:return False,""
    bodies=[abs(float(c[4])-float(c[1])) for c in c5]
    shrinking=bodies[-3]>bodies[-2]>bodies[-1]
    avg=sum(bodies[-10:])/10;tiny=bodies[-1]<avg*0.40
    trades=await get_recent_trades(symbol);cvd_falling=False
    if trades and len(trades)>=100:
        f50=trades[50:100];l50=trades[:50]
        fc=sum(float(t["size"]) for t in f50 if t.get("side")=="Buy")-sum(float(t["size"]) for t in f50 if t.get("side")=="Sell")
        lc=sum(float(t["size"]) for t in l50 if t.get("side")=="Buy")-sum(float(t["size"]) for t in l50 if t.get("side")=="Sell")
        cvd_falling=(lc<fc*0.6 if side=="long" else lc>fc*0.6)
    should=shrinking and tiny and cvd_falling
    reason=f"Momentum exhaustion at {pnl:.1f}% profit" if should else ""
    return should,reason

async def check_structure_break(symbol,pos):
    entry=pos["entry"];side=pos["side"]
    c5=await get_klines(symbol,"5",10)
    if not c5:return False,""
    price=float(c5[-1][4])
    pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100
    if pnl<1.0:return False,""
    trades=await get_recent_trades(symbol)
    if not trades or len(trades)<100:return False,""
    early=trades[len(trades)//2:];late=trades[:50]
    ep=float(early[0]["price"]) if early else 0;lp=float(late[0]["price"]) if late else 0
    if ep==0:return False,""
    ec=sum(float(t["size"]) for t in early if t.get("side")=="Buy")-sum(float(t["size"]) for t in early if t.get("side")=="Sell")
    lc=sum(float(t["size"]) for t in late  if t.get("side")=="Buy")-sum(float(t["size"]) for t in late  if t.get("side")=="Sell")
    bad=(side=="long" and not(lp>ep) and (lc>ec)) or (side=="short" and (lp>ep) and not(lc>ec))
    reason=f"Structure break at {pnl:.1f}% profit" if bad else ""
    return bad,reason

# ── SCAN LOOP ────────────────────────────────────────────────

async def scan_once():
    if state["position"]:
        await manage_position();return
    state["balance"]=await get_balance()
    if state["balance"]<10:
        print(f"Low balance:{state['balance']}");return
    print(f"\n── Scan │ Balance:${state['balance']:.2f} ──")
    for symbol in WATCHLIST:
        if state["position"]:break
        try:
            await scan_symbol(symbol)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Scan err {symbol}:{e}")

async def scan_symbol(symbol):
    ticker=await get_ticker(symbol)
    if not ticker:return
    price=float(ticker.get("lastPrice",0))
    price24=float(ticker.get("prevPrice24h",price) or price)
    change=(price-price24)/price24*100 if price24>0 else 0
    vol=float(ticker.get("volume24h",0))
    if vol<500_000 or abs(change)>15:return

    # ── 24h change filter — only trade FRESH moves ───────────
    # Already up >5%  → long exhausted, skip
    # Already down >5% → short exhausted, skip
    skip_long  = change >  5.0
    skip_short = change < -5.0

    best_score=0; best_dir=None; best_gate=None; best_details=None; best_label=None

    for direction in ["long","short"]:
        if direction=="long"  and skip_long:  continue
        if direction=="short" and skip_short: continue
        gate_ok,gate_info=await trend_gate(symbol,direction)
        if not gate_ok:
            state["trend_blocks"]+=1
            print(f"  BLOCKED {symbol} {direction.upper()} | {gate_info.get('reason','')}");continue
        score,label,details=await score_signal(symbol,direction)
        print(f"  SCORED {symbol} {direction.upper()} | {score} ({label})")
        if score>best_score:
            best_score=score;best_dir=direction
            best_gate=gate_info;best_details=details;best_label=label

    scan_entry={"symbol":symbol,"direction":best_dir or "none","price":round(price,4),
                "change":round(change,2),"vol":round(vol/1e6,1),
                "gate":best_gate or {},"score":best_score if best_dir else None,
                "label":best_label,"details":best_details,
                "blocked_by":None if (best_dir and best_score>=MIN_SCORE) else ("score" if best_dir else "trend_gate"),
                "time":int(time.time())}

    if not best_dir:
        _add_scan_log(scan_entry);return

    if best_score<MIN_SCORE:
        state["score_blocks"]+=1
        _add_scan_log(scan_entry)
        print(f"  BLOCKED {symbol} | Best score {best_score}<{MIN_SCORE}");return

    scan_entry["blocked_by"]=None
    _add_scan_log(scan_entry)
    print(f"  SIGNAL {symbol} | {best_dir.upper()} | {best_score} ({best_label})")
    state["last_signal"]={"symbol":symbol,"direction":best_dir,"score":best_score,
                           "label":best_label,"gate":best_gate,"details":best_details,"time":int(time.time())}
    state["signal_params"]={"symbol":symbol,"direction":best_dir,"score":best_score,
                             "label":best_label,"gate":best_gate,"details":best_details}
    await enter_trade(symbol,best_dir,best_score,best_label,price)

def _add_scan_log(entry):
    state["scan_log"].insert(0,entry)
    state["scan_log"]=state["scan_log"][:20]

async def enter_trade(symbol,direction,score,label,price):
    sizing=get_sizing(score,state["balance"]);tpsl=get_tp_sl(score,price,direction)
    qty=round(sizing["size"]*LEVERAGE/price,3)
    if qty<=0:return
    resp=await place_order(symbol,direction,qty,tpsl["sl"],tpsl["tp1"])
    if not resp or resp.get("retCode")!=0:
        print(f"Order failed {symbol}:{resp}");return
    state["position"]={
        "symbol":symbol,"side":direction,"entry":price,"qty":qty,"qty_remaining":qty,
        "score":score,"label":label,"tp1":tpsl["tp1"],"tp2":tpsl["tp2"],"tp3":tpsl["tp3"],
        "sl":tpsl["sl"],"tp1_pct":tpsl["tp1_pct"],"tp2_pct":tpsl["tp2_pct"],"tp3_pct":tpsl["tp3_pct"],
        "sl_pct":tpsl["sl_pct"],"tp1_hit":False,"tp2_hit":False,"be_moved":False,"open_time":int(time.time()),
    }
    await save_to_supabase(state["position"],"open",price)

async def manage_position():
    pos=state["position"]
    if not pos:return
    symbol=pos["symbol"];side=pos["side"];entry=pos["entry"]
    ticker=await get_ticker(symbol)
    if not ticker:return
    price=float(ticker.get("lastPrice",0))
    if price==0:return
    pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100

    if not pos["tp1_hit"]:
        if (side=="long" and price>=pos["tp1"]) or (side=="short" and price<=pos["tp1"]):
            pos["tp1_hit"]=True;pos["be_moved"]=True
            await update_sl(symbol,entry*1.001 if side=="long" else entry*0.999)
            cq=round(pos["qty"]*0.40,3)
            await close_full(symbol,cq,side)
            pos["qty_remaining"]=round(pos["qty_remaining"]-cq,3)

    if pos["tp1_hit"] and not pos["tp2_hit"]:
        if (side=="long" and price>=pos["tp2"]) or (side=="short" and price<=pos["tp2"]):
            pos["tp2_hit"]=True
            trail=pos["tp1"]*1.001 if side=="long" else pos["tp1"]*0.999
            await update_sl(symbol,trail)
            cq=round(pos["qty"]*0.40,3)
            await close_full(symbol,cq,side)
            pos["qty_remaining"]=round(pos["qty_remaining"]-cq,3)

    if pos["tp2_hit"]:
        if (side=="long" and price>=pos["tp3"]) or (side=="short" and price<=pos["tp3"]):
            await close_full(symbol,pos["qty_remaining"],side)
            await record_close(pos,price,"win","TP3");return

    if (side=="long" and price<=pos["sl"]) or (side=="short" and price>=pos["sl"]):
        await close_full(symbol,pos["qty_remaining"],side)
        await record_close(pos,price,"loss","SL");return

    me,mr=await check_momentum_exit(symbol,pos)
    if me:
        await close_full(symbol,pos["qty_remaining"],side)
        await record_close(pos,price,"win" if pnl>0 else "loss","MomentumExit");return

    sb,sr=await check_structure_break(symbol,pos)
    if sb:
        await close_full(symbol,pos["qty_remaining"],side)
        await record_close(pos,price,"win" if pnl>0 else "loss","StructureBreak");return

    print(f"  HOLDING {symbol} │ {pnl:+.2f}% │ TP1={'✓' if pos['tp1_hit'] else '○'} TP2={'✓' if pos['tp2_hit'] else '○'}")

async def record_close(pos,exit_price,result,exit_type):
    entry=pos["entry"];side=pos["side"]
    pnl_pct=(exit_price-entry)/entry*100 if side=="long" else (entry-exit_price)/entry*100
    pnl_usd=pos["qty"]*entry*(pnl_pct/100)*LEVERAGE
    trade={"symbol":pos["symbol"],"direction":side,"result":result,"entry":entry,
           "exit_price":exit_price,"pnl":round(pnl_usd,4),"pnl_pct":round(pnl_pct,3),
           "score":pos["score"],"label":pos["label"],"tp1_hit":pos["tp1_hit"],
           "tp2_hit":pos["tp2_hit"],"exit_type":exit_type,"open_time":pos["open_time"],
           "close_time":int(time.time())}
    state["trades"].append(trade);state["trades"]=state["trades"][-100:]
    if result=="win":state["wins"]+=1
    else:            state["losses"]+=1
    state["total_pnl"]+=pnl_usd;state["position"]=None
    await save_to_supabase(trade,result,exit_price)

# ── SUPABASE ─────────────────────────────────────────────────

async def save_to_supabase(trade,result,price):
    if not SB_URL or not SB_KEY:return
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
        print(f"Supabase err:{e}")

async def load_from_supabase():
    if not SB_URL or not SB_KEY:return
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
        print(f"Supabase load err:{e}")

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
            print(f"Loop err:{e}")
        await asyncio.sleep(SCAN_INTERVAL)

@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_loop())

# ── API ENDPOINTS ────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    pos=state["position"]
    wins=state["wins"];losses=state["losses"];total=wins+losses
    # live price for open position
    pos_data=None
    if pos:
        try:
            tk=await get_ticker(pos["symbol"])
            live_price=float(tk.get("lastPrice",pos["entry"]))
            pnl_pct=(live_price-pos["entry"])/pos["entry"]*100 if pos["side"]=="long" else (pos["entry"]-live_price)/pos["entry"]*100
            pos_data={**pos,"live_price":round(live_price,6),"live_pnl_pct":round(pnl_pct,3)}
        except:
            pos_data=pos
    return JSONResponse({
        "status":state["status"],"balance":state["balance"],
        "wins":wins,"losses":losses,"total":total,
        "total_pnl":state["total_pnl"],"min_score":MIN_SCORE,
        "trend_blocks":state["trend_blocks"],"score_blocks":state["score_blocks"],
        "position":pos_data,"last_signal":state["last_signal"],
        "scan_log":state["scan_log"][:10],
        "signal_params":state["signal_params"],
        "last_scan":state["last_scan"],"now":int(time.time()),
    })

@app.get("/api/trades")
async def api_trades():
    return JSONResponse(state["trades"])

@app.get("/api/scan_log")
async def api_scan_log():
    return JSONResponse(state["scan_log"])

# ── DASHBOARD (Single Page App — polls /api/status) ──────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>APEX Pro</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;500;700&family=IBM+Plex+Mono:wght@300;400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

:root {
  --cream:  #f7f3ec;
  --cream2: #ede7da;
  --cream3: #e2d9c8;
  --white:  #ffffff;
  --green:  #1a5c3a;
  --green2: #217a4d;
  --green3: #28a868;
  --green4: #e8f5ee;
  --green5: #c8ecd8;
  --red:    #7d1a2e;
  --red2:   #b02040;
  --red4:   #fce8ec;
  --gold:   #7a5c10;
  --gold2:  #c49a22;
  --gold4:  #fdf3d8;
  --ink:    #1a1814;
  --ink2:   #3a3630;
  --ink3:   #7a7060;
  --ink4:   #b0a898;
  --border: #d8d0c0;
  --shadow: 0 2px 16px rgba(26,24,20,0.07);
  --shadow2:0 8px 32px rgba(26,24,20,0.12);
}

* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }

body {
  background: var(--cream);
  color: var(--ink);
  font-family: 'IBM Plex Sans', sans-serif;
  min-height: 100vh;
  padding-bottom: 80px;
}

/* ── HEADER ── */
.hdr {
  background: var(--ink);
  padding: 14px 20px;
  display: flex; align-items: center; justify-content: space-between;
  position: sticky; top: 0; z-index: 200;
}
.logo {
  font-family: 'Playfair Display', serif;
  font-size: 20px; font-weight: 700;
  color: var(--cream); letter-spacing: 0.02em;
}
.logo em { color: var(--green3); font-style: normal; }
.logo sub { font-size: 10px; color: var(--ink3); font-family: 'IBM Plex Mono', monospace; vertical-align: middle; margin-left: 4px; }

.status-pill {
  display: flex; align-items: center; gap: 6px;
  background: rgba(40,168,104,0.12);
  border: 1px solid rgba(40,168,104,0.3);
  border-radius: 20px; padding: 4px 12px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px; letter-spacing: 0.1em; color: var(--green3);
}
.sdot { width:6px; height:6px; border-radius:50%; background:var(--green3); animation: blink 2s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.2} }

/* ── NAV TABS ── */
.nav {
  background: var(--white);
  border-bottom: 1px solid var(--border);
  display: flex; overflow-x: auto;
  scrollbar-width: none; padding: 0 16px;
  position: sticky; top: 48px; z-index: 190;
}
.nav::-webkit-scrollbar { display: none; }
.nav-tab {
  padding: 12px 16px; font-size: 12px; font-weight: 500;
  color: var(--ink3); cursor: pointer; white-space: nowrap;
  border-bottom: 2px solid transparent;
  font-family: 'IBM Plex Mono', monospace;
  letter-spacing: 0.06em; text-transform: uppercase;
  transition: all 0.2s;
}
.nav-tab.active { color: var(--green2); border-bottom-color: var(--green2); }
.nav-tab:hover { color: var(--ink2); }

/* ── PAGES ── */
.page { display: none; padding: 16px; max-width: 600px; margin: 0 auto; }
.page.active { display: block; }

/* ── SECTION LABEL ── */
.slbl {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 9px; letter-spacing: 0.2em;
  text-transform: uppercase; color: var(--ink4);
  margin: 20px 0 10px;
  display: flex; align-items: center; gap: 10px;
}
.slbl::after { content:''; flex:1; height:1px; background:var(--border); }

/* ── STAT CARDS ── */
.sg { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.sc {
  background: var(--white); border: 1px solid var(--border);
  border-radius: 16px; padding: 16px;
  box-shadow: var(--shadow); position: relative; overflow: hidden;
}
.sc-top { height: 3px; position: absolute; top:0;left:0;right:0; }
.sc-g .sc-top { background: var(--green3); }
.sc-r .sc-top { background: var(--red2); }
.sc-a .sc-top { background: var(--gold2); }
.sc-n .sc-top { background: var(--border); }
.sc-lbl { font-family:'IBM Plex Mono',monospace; font-size:9px; letter-spacing:0.14em; text-transform:uppercase; color:var(--ink3); margin-bottom:8px; display:block; }
.sc-val { font-family:'Playfair Display',serif; font-size:26px; font-weight:700; line-height:1; }
.pos-col { color: var(--green2); }
.neg-col { color: var(--red2); }
.amb-col { color: var(--gold); }
.sc-sub { font-size:11px; color:var(--ink3); margin-top:4px; font-family:'IBM Plex Mono',monospace; }

/* ── INFO ROWS ── */
.icard { background:var(--white); border:1px solid var(--border); border-radius:14px; overflow:hidden; box-shadow:var(--shadow); }
.ir {
  display:flex; justify-content:space-between; align-items:center;
  padding: 11px 16px; border-bottom: 1px solid var(--cream2);
  font-size: 13px;
}
.ir:last-child { border-bottom: none; }
.ir .k { font-family:'IBM Plex Mono',monospace; font-size:11px; color:var(--ink3); }
.ir .v { font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:500; color:var(--ink); }

/* ── SIGNAL SCORE GAUGE ── */
.gauge-wrap {
  background: var(--white); border:1px solid var(--border);
  border-radius:20px; padding: 24px 20px;
  box-shadow: var(--shadow2); text-align: center;
  margin-bottom: 12px;
}
.gauge-title { font-family:'IBM Plex Mono',monospace; font-size:9px; letter-spacing:0.18em; text-transform:uppercase; color:var(--ink3); margin-bottom:16px; }
.gauge-svg { width:180px; height:100px; overflow:visible; margin:0 auto 12px; display:block; }
.gauge-score { font-family:'Playfair Display',serif; font-size:48px; font-weight:700; color:var(--ink); line-height:1; }
.gauge-label { font-family:'IBM Plex Mono',monospace; font-size:11px; letter-spacing:0.12em; text-transform:uppercase; margin-top:4px; }
.gauge-sym { font-family:'IBM Plex Mono',monospace; font-size:13px; color:var(--ink3); margin-top:6px; }

/* ── PARAM GROUPS ── */
.param-group {
  background: var(--white); border:1px solid var(--border);
  border-radius:16px; overflow:hidden; margin-bottom:10px;
  box-shadow: var(--shadow);
}
.pg-header {
  display:flex; align-items:center; justify-content:space-between;
  padding:14px 16px; cursor:pointer;
  border-bottom:1px solid var(--cream2);
  user-select:none;
}
.pg-header:active { background: var(--cream2); }
.pg-title { display:flex; align-items:center; gap:10px; }
.pg-icon { font-size:16px; }
.pg-name { font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:500; letter-spacing:0.06em; color:var(--ink); }
.pg-pts { font-family:'IBM Plex Mono',monospace; font-size:11px; }
.pg-arrow { font-size:12px; color:var(--ink4); transition:transform 0.2s; }
.pg-arrow.open { transform: rotate(90deg); }
.pg-body { display:none; }
.pg-body.open { display:block; }

/* ── PARAM ROWS ── */
.pr {
  display:flex; justify-content:space-between; align-items:center;
  padding:10px 16px; border-bottom:1px solid var(--cream2);
}
.pr:last-child { border-bottom:none; }
.pr-key { font-size:12px; color:var(--ink3); font-family:'IBM Plex Mono',monospace; }
.pr-val { font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:500; }

/* ── PROGRESS BAR ── */
.pbar-wrap { padding: 10px 16px 14px; border-bottom:1px solid var(--cream2); }
.pbar-top { display:flex; justify-content:space-between; margin-bottom:6px; }
.pbar-label { font-size:11px; color:var(--ink3); font-family:'IBM Plex Mono',monospace; }
.pbar-val { font-size:12px; font-weight:500; font-family:'IBM Plex Mono',monospace; }
.pbar { height:6px; background:var(--cream2); border-radius:6px; overflow:hidden; }
.pbar-fill { height:100%; border-radius:6px; transition: width 0.6s ease; }
.pbar-g { background: linear-gradient(90deg,var(--green2),var(--green3)); }
.pbar-r { background: linear-gradient(90deg,var(--red),var(--red2)); }
.pbar-a { background: linear-gradient(90deg,var(--gold),var(--gold2)); }

/* ── STATUS BADGES ── */
.badge {
  font-family:'IBM Plex Mono',monospace; font-size:10px;
  padding:2px 10px; border-radius:20px; font-weight:500;
  letter-spacing:0.06em;
}
.b-pass  { background:var(--green4); color:var(--green); border:1px solid var(--green5); }
.b-fail  { background:var(--red4);   color:var(--red2);  border:1px solid #f0c0c8; }
.b-warn  { background:var(--gold4);  color:var(--gold);  border:1px solid #f0dca0; }
.b-neu   { background:var(--cream2); color:var(--ink3);  border:1px solid var(--border); }
.b-long  { background:var(--green4); color:var(--green); }
.b-short { background:var(--red4);   color:var(--red2); }

/* ── POSITION CARD ── */
.pos-card {
  background:var(--white); border:1px solid var(--border);
  border-radius:20px; padding:20px;
  box-shadow:var(--shadow2); position:relative; overflow:hidden;
}
.pos-card::before {
  content:''; position:absolute; top:0;left:0;right:0; height:4px;
  background:linear-gradient(90deg,var(--green2),var(--green3));
}
.pos-hdr { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:16px; }
.pos-sym { font-family:'Playfair Display',serif; font-size:22px; font-weight:700; }
.pos-sym span { font-size:13px; color:var(--ink3); font-family:'IBM Plex Mono',monospace; }
.pos-meta { display:flex; gap:8px; align-items:center; margin-top:4px; flex-wrap:wrap; }
.pos-score-big { font-family:'Playfair Display',serif; font-size:36px; font-weight:700; color:var(--green2); text-align:right; line-height:1; }
.pos-score-sub { font-size:9px; font-family:'IBM Plex Mono',monospace; color:var(--ink3); text-align:right; letter-spacing:0.1em; text-transform:uppercase; }

.pos-metrics {
  display:grid; grid-template-columns:1fr 1fr;
  gap:8px; background:var(--cream2); border-radius:12px;
  padding:12px; margin-bottom:16px;
}
.pm { display:flex; flex-direction:column; gap:2px; }
.pm-k { font-size:9px; font-family:'IBM Plex Mono',monospace; color:var(--ink3); letter-spacing:0.1em; text-transform:uppercase; }
.pm-v { font-family:'IBM Plex Mono',monospace; font-size:13px; font-weight:500; }

.tp-row { display:flex; align-items:center; gap:4px; margin-bottom:12px; }
.tp-node {
  display:flex; flex-direction:column; align-items:center; gap:3px;
}
.tp-circle {
  width:12px; height:12px; border-radius:50%;
  border:2px solid var(--border); background:var(--white);
}
.tp-hit .tp-circle { background:var(--green3); border-color:var(--green3); }
.tp-k { font-size:9px; font-family:'IBM Plex Mono',monospace; color:var(--ink3); }
.tp-v { font-size:10px; font-family:'IBM Plex Mono',monospace; font-weight:500; color:var(--ink2); }
.tp-line { flex:1; height:2px; background:var(--border); }
.tp-hit-line { background:var(--green3); }

.be-badge {
  background:var(--green4); border:1px solid var(--green5);
  border-radius:10px; padding:7px 14px;
  font-size:11px; font-family:'IBM Plex Mono',monospace; color:var(--green);
  text-align:center;
}

.no-pos {
  background:var(--white); border:1px dashed var(--border);
  border-radius:20px; padding:36px 20px; text-align:center;
  color:var(--ink3);
}
.no-pos-icon { font-size:32px; opacity:0.3; margin-bottom:10px; }
.no-pos-txt { font-size:13px; font-family:'IBM Plex Mono',monospace; }

/* ── SCAN LOG ── */
.scan-item {
  background:var(--white); border:1px solid var(--border);
  border-radius:14px; margin-bottom:8px; overflow:hidden;
  cursor:pointer; box-shadow:var(--shadow);
  transition: box-shadow 0.2s;
}
.scan-item:hover { box-shadow:var(--shadow2); }
.scan-hdr {
  display:flex; align-items:center; justify-content:space-between;
  padding:12px 14px;
}
.scan-sym { font-family:'Playfair Display',serif; font-size:16px; font-weight:700; }
.scan-sym span { font-size:11px; color:var(--ink3); font-family:'IBM Plex Mono',monospace; }
.scan-right { display:flex; flex-direction:column; align-items:flex-end; gap:4px; }
.scan-score { font-family:'IBM Plex Mono',monospace; font-size:14px; font-weight:500; }
.scan-time { font-size:10px; color:var(--ink4); font-family:'IBM Plex Mono',monospace; }
.scan-tags { display:flex; gap:6px; flex-wrap:wrap; padding:0 14px 12px; }
.scan-detail { padding:0 14px 14px; display:none; }
.scan-detail.open { display:block; }
.scan-detail-inner { background:var(--cream2); border-radius:10px; padding:12px; }
.sdrow { display:flex; justify-content:space-between; padding:4px 0; font-size:11px; font-family:'IBM Plex Mono',monospace; border-bottom:1px solid rgba(0,0,0,0.04); }
.sdrow:last-child { border-bottom:none; }
.sdrow .k { color:var(--ink3); }
.sdrow .v { font-weight:500; color:var(--ink); }
.signal-flash { animation: flash 1s ease; }
@keyframes flash { 0%{background:rgba(40,168,104,0.2)} 100%{background:transparent} }

/* ── TRADES ── */
.trade-item {
  background:var(--white); border:1px solid var(--border);
  border-radius:12px; padding:14px 16px;
  margin-bottom:8px; display:flex; align-items:center; gap:12px;
  box-shadow:var(--shadow);
}
.t-left { flex:1; }
.t-sym { font-family:'Playfair Display',serif; font-size:16px; font-weight:700; }
.t-meta { display:flex; gap:6px; margin-top:4px; flex-wrap:wrap; }
.t-right { text-align:right; }
.t-pnl { font-family:'IBM Plex Mono',monospace; font-size:16px; font-weight:600; }
.t-exit { font-size:10px; font-family:'IBM Plex Mono',monospace; color:var(--ink3); margin-top:3px; }

/* ── BOTTOM NAV ── */
.bnav {
  position:fixed; bottom:0; left:0; right:0;
  background:var(--ink); display:flex;
  border-top:1px solid rgba(255,255,255,0.05);
  z-index:200;
}
.bn {
  flex:1; padding:10px 4px 14px; text-align:center; cursor:pointer;
  display:flex; flex-direction:column; align-items:center; gap:3px;
}
.bn-icon { font-size:18px; }
.bn-label { font-size:9px; font-family:'IBM Plex Mono',monospace; letter-spacing:0.08em; text-transform:uppercase; color:var(--ink3); }
.bn.active .bn-label { color:var(--green3); }
.bn.active .bn-icon { filter:brightness(1.4); }

.countdown {
  font-family:'IBM Plex Mono',monospace; font-size:10px;
  color:var(--ink3); text-align:center; padding:8px;
}

.empty-state { text-align:center; padding:48px 20px; color:var(--ink3); }
.empty-icon { font-size:36px; opacity:0.3; margin-bottom:12px; }
.empty-txt { font-size:12px; font-family:'IBM Plex Mono',monospace; }

.pulse-dot {
  display:inline-block; width:8px; height:8px; border-radius:50%;
  background:var(--green3); margin-right:6px;
  animation: blink 1.5s infinite;
}

.refresh-bar {
  height:2px; background:var(--green4);
  position:fixed; top:48px; left:0; z-index:300;
  transition: width 0.1s linear;
}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">APEX <em>Pro</em><sub>v2</sub></div>
  <div class="status-pill" id="spill"><span class="sdot"></span><span id="stxt">LOADING</span></div>
</div>

<div class="refresh-bar" id="rbar" style="width:0%"></div>

<div class="nav">
  <div class="nav-tab active" onclick="showPage('overview',this)">Overview</div>
  <div class="nav-tab" onclick="showPage('signal',this)">Signal</div>
  <div class="nav-tab" onclick="showPage('position',this)">Position</div>
  <div class="nav-tab" onclick="showPage('scanlog',this)">Scan Log</div>
  <div class="nav-tab" onclick="showPage('trades',this)">Trades</div>
</div>

<!-- OVERVIEW PAGE -->
<div class="page active" id="page-overview">
  <div class="slbl">Performance</div>
  <div class="sg">
    <div class="sc sc-n" id="pnl-card">
      <div class="sc-top"></div>
      <span class="sc-lbl">Total P&L</span>
      <div class="sc-val" id="ov-pnl">—</div>
      <div class="sc-sub" id="ov-trades">— trades</div>
    </div>
    <div class="sc sc-a">
      <div class="sc-top"></div>
      <span class="sc-lbl">Win Rate</span>
      <div class="sc-val amb-col" id="ov-wr">—</div>
      <div class="sc-sub" id="ov-wl">—W / —L</div>
    </div>
    <div class="sc sc-g">
      <div class="sc-top"></div>
      <span class="sc-lbl">Avg Win</span>
      <div class="sc-val pos-col" id="ov-aw">—</div>
      <div class="sc-sub">per trade</div>
    </div>
    <div class="sc sc-r">
      <div class="sc-top"></div>
      <span class="sc-lbl">Avg Loss</span>
      <div class="sc-val neg-col" id="ov-al">—</div>
      <div class="sc-sub">per trade</div>
    </div>
  </div>

  <div class="slbl">System</div>
  <div class="icard">
    <div class="ir"><span class="k">Balance</span><span class="v" id="ov-bal">—</span></div>
    <div class="ir"><span class="k">Min Score</span><span class="v"><span class="badge b-pass">70 · STRONG</span></span></div>
    <div class="ir"><span class="k">Trend Blocks</span><span class="v" id="ov-tb">—</span></div>
    <div class="ir"><span class="k">Score Blocks</span><span class="v" id="ov-sb">—</span></div>
    <div class="ir"><span class="k">Last Scan</span><span class="v" id="ov-ls">—</span></div>
    <div class="ir"><span class="k">Leverage</span><span class="v">2×</span></div>
    <div class="ir"><span class="k">Scan Interval</span><span class="v">30s</span></div>
  </div>

  <div class="slbl">Last Signal</div>
  <div class="icard" id="last-sig-card">
    <div class="ir"><span class="k">—</span><span class="v">—</span></div>
  </div>
</div>

<!-- SIGNAL PAGE -->
<div class="page" id="page-signal">
  <div id="no-signal-state" class="empty-state">
    <div class="empty-icon">◎</div>
    <div class="empty-txt">Waiting for next signal...</div>
    <div style="margin-top:8px;font-size:11px;color:var(--ink4);font-family:'IBM Plex Mono',monospace" id="sig-countdown"></div>
  </div>
  <div id="signal-content" style="display:none">
    <!-- Score gauge -->
    <div class="gauge-wrap">
      <div class="gauge-title">Signal Score</div>
      <svg class="gauge-svg" viewBox="0 0 180 100" id="gauge-svg">
        <path d="M20,90 A70,70,0,0,1,160,90" fill="none" stroke="#e2d9c8" stroke-width="12" stroke-linecap="round"/>
        <path d="M20,90 A70,70,0,0,1,160,90" fill="none" stroke="#1a5c3a" stroke-width="12"
              stroke-linecap="round" stroke-dasharray="220" stroke-dashoffset="220" id="gauge-arc"
              style="transition:stroke-dashoffset 1s ease"/>
        <text x="90" y="86" text-anchor="middle" font-family="Playfair Display,serif" font-size="36" font-weight="700" fill="#1a1814" id="gauge-num">0</text>
      </svg>
      <div class="gauge-label" id="gauge-lbl">—</div>
      <div class="gauge-sym" id="gauge-sym">—</div>
    </div>

    <!-- Layer 1: Trend Gate -->
    <div class="param-group">
      <div class="pg-header" onclick="toggleGroup(this)">
        <div class="pg-title">
          <span class="pg-icon">🔒</span>
          <span class="pg-name">Layer 1 · Trend Gate</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="badge" id="gate-badge">—</span>
          <span class="pg-arrow">▶</span>
        </div>
      </div>
      <div class="pg-body">
        <!-- UT Bot -->
        <div class="pbar-wrap">
          <div class="pbar-top">
            <span class="pbar-label">UT Bot (15m)</span>
            <span class="pbar-val" id="ut-val">—</span>
          </div>
        </div>
        <div class="pbar-wrap">
          <div class="pbar-top">
            <span class="pbar-label">ADX (14) · need &gt;18</span>
            <span class="pbar-val" id="adx-num">—</span>
          </div>
          <div class="pbar"><div class="pbar-fill pbar-a" id="adx-bar" style="width:0%"></div></div>
        </div>
        <div class="pr"><span class="pr-key">ADX Status</span><span class="pr-val" id="adx-val">—</span></div>
        <div class="pr"><span class="pr-key">VWAP</span><span class="pr-val" id="vwap-val">—</span></div>
        <div class="pr"><span class="pr-key">Price vs VWAP</span><span class="pr-val" id="pvwap-val">—</span></div>
        <div class="pr"><span class="pr-key">Gate Result</span><span class="pr-val" id="gate-result">—</span></div>
      </div>
    </div>

    <!-- Layer 2a: OBI -->
    <div class="param-group">
      <div class="pg-header" onclick="toggleGroup(this)">
        <div class="pg-title">
          <span class="pg-icon">📊</span>
          <span class="pg-name">Order Book Imbalance</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="pg-pts" id="obi-pts-badge">— pts</span>
          <span class="pg-arrow">▶</span>
        </div>
      </div>
      <div class="pg-body">
        <div class="pbar-wrap">
          <div class="pbar-top">
            <span class="pbar-label">Bid/Ask Ratio</span>
            <span class="pbar-val" id="obi-pct">—%</span>
          </div>
          <div class="pbar"><div class="pbar-fill pbar-g" id="obi-bar" style="width:50%"></div></div>
        </div>
        <div class="pr"><span class="pr-key">Bid Volume</span><span class="pr-val" id="bid-vol">—</span></div>
        <div class="pr"><span class="pr-key">Ask Volume</span><span class="pr-val" id="ask-vol">—</span></div>
        <div class="pr"><span class="pr-key">Bid Stacking</span><span class="pr-val" id="bid-stack">—</span></div>
        <div class="pr"><span class="pr-key">Points Scored</span><span class="pr-val" id="obi-pts">— / 40</span></div>
      </div>
    </div>

    <!-- Layer 2b: CVD -->
    <div class="param-group">
      <div class="pg-header" onclick="toggleGroup(this)">
        <div class="pg-title">
          <span class="pg-icon">💧</span>
          <span class="pg-name">CVD · Trade Flow</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="pg-pts" id="cvd-pts-badge">— pts</span>
          <span class="pg-arrow">▶</span>
        </div>
      </div>
      <div class="pg-body">
        <div class="pbar-wrap">
          <div class="pbar-top">
            <span class="pbar-label">Buy Aggression</span>
            <span class="pbar-val" id="buy-pct">—%</span>
          </div>
          <div class="pbar"><div class="pbar-fill pbar-g" id="cvd-bar" style="width:50%"></div></div>
        </div>
        <div class="pr"><span class="pr-key">CVD Delta</span><span class="pr-val" id="cvd-delta">—</span></div>
        <div class="pr"><span class="pr-key">Acceleration</span><span class="pr-val" id="cvd-accel">—</span></div>
        <div class="pr"><span class="pr-key">CVD Divergence</span><span class="pr-val" id="cvd-div">—</span></div>
        <div class="pr"><span class="pr-key">Points Scored</span><span class="pr-val" id="cvd-pts">— / 38</span></div>
      </div>
    </div>

    <!-- Layer 2c: OI Delta -->
    <div class="param-group">
      <div class="pg-header" onclick="toggleGroup(this)">
        <div class="pg-title">
          <span class="pg-icon">📈</span>
          <span class="pg-name">Open Interest Delta</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="pg-pts" id="oi-pts-badge">— pts</span>
          <span class="pg-arrow">▶</span>
        </div>
      </div>
      <div class="pg-body">
        <div class="pr"><span class="pr-key">OI Change %</span><span class="pr-val" id="oi-pct">—</span></div>
        <div class="pr"><span class="pr-key">OI Now</span><span class="pr-val" id="oi-now">—</span></div>
        <div class="pr"><span class="pr-key">OI Signal</span><span class="pr-val" id="oi-signal">—</span></div>
        <div class="pr"><span class="pr-key">Price Direction</span><span class="pr-val" id="oi-price-dir">—</span></div>
        <div class="pr"><span class="pr-key">Points Scored</span><span class="pr-val" id="oi-pts">— / 20</span></div>
      </div>
    </div>

    <!-- All 3 agree -->
    <div class="param-group">
      <div class="pg-header" onclick="toggleGroup(this)">
        <div class="pg-title">
          <span class="pg-icon">🎯</span>
          <span class="pg-name">Confluence Bonus</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="pg-pts" id="conf-pts-badge">— pts</span>
          <span class="pg-arrow">▶</span>
        </div>
      </div>
      <div class="pg-body">
        <div class="pr"><span class="pr-key">Votes Agreeing</span><span class="pr-val" id="votes-val">— / 3</span></div>
        <div class="pr"><span class="pr-key">All 3 Agree</span><span class="pr-val" id="votes-agree">—</span></div>
        <div class="pr"><span class="pr-key">Bonus Points</span><span class="pr-val" id="conf-pts">— / 10</span></div>
      </div>
    </div>
  </div>
</div>

<!-- POSITION PAGE -->
<div class="page" id="page-position">
  <div id="pos-content"></div>
</div>

<!-- SCAN LOG PAGE -->
<div class="page" id="page-scanlog">
  <div class="slbl">Last 10 Scanned</div>
  <div id="scan-log-list">
    <div class="empty-state">
      <div class="empty-icon">🔍</div>
      <div class="empty-txt">Scan results will appear here</div>
    </div>
  </div>
</div>

<!-- TRADES PAGE -->
<div class="page" id="page-trades">
  <div class="slbl">Trade History</div>
  <div id="trade-list">
    <div class="empty-state">
      <div class="empty-icon">📋</div>
      <div class="empty-txt">No trades yet</div>
    </div>
  </div>
</div>

<div class="bnav">
  <div class="bn active" onclick="showPage('overview',null,'bn-overview')">
    <div class="bn-icon">◈</div><div class="bn-label">Overview</div>
  </div>
  <div class="bn" onclick="showPage('signal',null,'bn-signal')">
    <div class="bn-icon">◉</div><div class="bn-label">Signal</div>
  </div>
  <div class="bn" onclick="showPage('position',null,'bn-position')">
    <div class="bn-icon">◐</div><div class="bn-label">Position</div>
  </div>
  <div class="bn" onclick="showPage('scanlog',null,'bn-scanlog')">
    <div class="bn-icon">◎</div><div class="bn-label">Scan Log</div>
  </div>
  <div class="bn" onclick="showPage('trades',null,'bn-trades')">
    <div class="bn-icon">◇</div><div class="bn-label">Trades</div>
  </div>
</div>

<script>
let data = {};
let refreshTimer = null;
let countdown = 15;

// ── Navigation ──
function showPage(name, tab, bnId) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.bn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (tab) tab.classList.add('active');
  if (bnId) document.querySelector('.bn:nth-child(' +
    (['bn-overview','bn-signal','bn-position','bn-scanlog','bn-trades'].indexOf(bnId)+1) + ')').classList.add('active');
}

function toggleGroup(hdr) {
  const body = hdr.nextElementSibling;
  const arrow = hdr.querySelector('.pg-arrow');
  body.classList.toggle('open');
  arrow.classList.toggle('open');
}

// ── Refresh bar ──
function startRefreshBar() {
  countdown = 15;
  const bar = document.getElementById('rbar');
  bar.style.width = '0%';
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    countdown--;
    const pct = ((15 - countdown) / 15) * 100;
    bar.style.width = pct + '%';
    const cd = document.getElementById('sig-countdown');
    if (cd) cd.textContent = 'Next refresh in ' + countdown + 's';
    if (countdown <= 0) {
      clearInterval(refreshTimer);
      bar.style.width = '100%';
      fetchData();
    }
  }, 1000);
}

// ── Fetch ──
async function fetchData() {
  try {
    const r = await fetch('/api/status');
    data = await r.json();
    render();
    startRefreshBar();
  } catch (e) {
    console.error(e);
    startRefreshBar();
  }
}

// ── Render ──
function render() {
  renderOverview();
  renderSignal();
  renderPosition();
  renderScanLog();
  renderTrades();
  // Update status pill
  document.getElementById('stxt').textContent = (data.status || 'unknown').toUpperCase();
}

function fmt(n, decimals=2) {
  if (n === null || n === undefined) return '—';
  return parseFloat(n).toFixed(decimals);
}

function pnlColor(v) {
  return parseFloat(v) >= 0 ? 'var(--green2)' : 'var(--red2)';
}

function badge(ok, yesText='PASS', noText='FAIL') {
  return ok ? `<span class="badge b-pass">${yesText}</span>` : `<span class="badge b-fail">${noText}</span>`;
}

// ── Overview ──
function renderOverview() {
  const wins = data.wins || 0;
  const losses = data.losses || 0;
  const total = wins + losses;
  const wr = total > 0 ? (wins/total*100).toFixed(0) : '0';
  const pnl = data.total_pnl || 0;
  const trades = data.trades || [];
  const wTrades = trades.filter(t => (t.result||'').toLowerCase() === 'win');
  const lTrades = trades.filter(t => (t.result||'').toLowerCase() === 'loss');
  const avgW = wTrades.length ? wTrades.reduce((s,t)=>s+(t.pnl||0),0)/wTrades.length : 0;
  const avgL = lTrades.length ? lTrades.reduce((s,t)=>s+(t.pnl||0),0)/lTrades.length : 0;

  const pnlEl = document.getElementById('ov-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + fmt(pnl);
  pnlEl.style.color = pnlColor(pnl);
  const pnlCard = document.getElementById('pnl-card');
  pnlCard.querySelector('.sc-top').style.background = pnl >= 0 ? 'var(--green3)' : 'var(--red2)';

  document.getElementById('ov-trades').textContent = total + ' trades closed';
  document.getElementById('ov-wr').textContent = wr + '%';
  document.getElementById('ov-wl').textContent = wins + 'W / ' + losses + 'L';
  document.getElementById('ov-aw').textContent = '+$' + fmt(avgW);
  document.getElementById('ov-al').textContent = '−$' + fmt(Math.abs(avgL));
  document.getElementById('ov-bal').textContent = '$' + fmt(data.balance || 0) + ' USDT';
  document.getElementById('ov-tb').textContent = data.trend_blocks || 0;
  document.getElementById('ov-sb').textContent = data.score_blocks || 0;
  const age = data.now && data.last_scan ? (data.now - data.last_scan) : '?';
  document.getElementById('ov-ls').textContent = age + 's ago';

  // Last signal card
  const ls = data.last_signal;
  const lsCard = document.getElementById('last-sig-card');
  if (ls) {
    const sigAge = data.now ? (data.now - ls.time) : '?';
    const dir = ls.direction === 'long' ? '<span class="badge b-long">LONG</span>' : '<span class="badge b-short">SHORT</span>';
    lsCard.innerHTML = `
      <div class="ir"><span class="k">Symbol</span><span class="v">${ls.symbol}</span></div>
      <div class="ir"><span class="k">Direction</span><span class="v">${dir}</span></div>
      <div class="ir"><span class="k">Score</span><span class="v" style="color:var(--green2);font-weight:600">${ls.score} · ${ls.label}</span></div>
      <div class="ir"><span class="k">Age</span><span class="v">${sigAge}s ago</span></div>
    `;
  }
}

// ── Signal Page ──
function renderSignal() {
  const sp = data.signal_params || data.last_signal;
  const noState = document.getElementById('no-signal-state');
  const content = document.getElementById('signal-content');

  if (!sp || !sp.score) {
    noState.style.display = 'block';
    content.style.display = 'none';
    return;
  }
  noState.style.display = 'none';
  content.style.display = 'block';

  const score = sp.score || 0;
  const gate  = sp.gate || {};
  const det   = sp.details || {};

  // Gauge
  const arc = document.getElementById('gauge-arc');
  const totalLen = 220;
  const offset = totalLen - (score / 100) * totalLen;
  arc.style.strokeDashoffset = offset;
  const gColor = score >= 75 ? 'var(--green2)' : score >= 55 ? 'var(--gold2)' : 'var(--red2)';
  arc.style.stroke = gColor;
  document.getElementById('gauge-num').textContent = score;
  document.getElementById('gauge-num').style.fill = gColor;
  document.getElementById('gauge-lbl').textContent = sp.label || '—';
  document.getElementById('gauge-lbl').style.color = gColor;
  document.getElementById('gauge-sym').textContent = (sp.symbol || '—') + ' · ' + (sp.direction || '').toUpperCase();

  // Gate badge
  const gateBadge = document.getElementById('gate-badge');
  gateBadge.textContent = gate.passed ? 'PASSED' : 'BLOCKED';
  gateBadge.className = 'badge ' + (gate.passed ? 'b-pass' : 'b-fail');

  // UT Bot
  const utEl = document.getElementById('ut-val');
  const ut = gate.ut || 'neutral';
  utEl.innerHTML = `<span class="badge ${ut==='long'?'b-long':ut==='short'?'b-short':'b-neu'}">${ut.toUpperCase()}</span> ${gate.ut_ok ? '✓' : '✗'}`;

  // ADX
  const adx = gate.adx || 0;
  document.getElementById('adx-val').innerHTML = badge(gate.adx_ok, 'TRENDING', 'CHOPPY');
  document.getElementById('adx-num').textContent = fmt(adx, 1);
  document.getElementById('adx-bar').style.width = Math.min(adx / 40 * 100, 100) + '%';
  document.getElementById('adx-bar').className = 'pbar-fill ' + (adx >= 18 ? 'pbar-g' : 'pbar-r');

  // VWAP
  document.getElementById('vwap-val').textContent = '$' + fmt(gate.vwap || 0, 4);
  const pvPct = gate.price_vs_vwap_pct || 0;
  document.getElementById('pvwap-val').innerHTML =
    `<span style="color:${pvPct>=0?'var(--green2)':'var(--red2)'}">${pvPct>=0?'+':''}${fmt(pvPct,3)}%</span> ${badge(gate.vwap_ok,'✓','✗')}`;
  document.getElementById('gate-result').innerHTML = gate.passed ? badge(true,'ALL PASSED') : `<span class="badge b-fail">${gate.reason||'FAILED'}</span>`;

  // OBI
  const obiPct = (det.obi || 0.5) * 100;
  document.getElementById('obi-pct').textContent = fmt(obiPct, 1) + '%';
  document.getElementById('obi-bar').style.width = obiPct + '%';
  document.getElementById('obi-bar').className = 'pbar-fill ' + (obiPct > 55 ? 'pbar-g' : obiPct < 45 ? 'pbar-r' : 'pbar-a');
  document.getElementById('bid-vol').textContent = fmt(det.bid_vol || 0, 0);
  document.getElementById('ask-vol').textContent = fmt(det.ask_vol || 0, 0);
  document.getElementById('bid-stack').innerHTML = det.bid_stack ? '<span class="badge b-pass">YES ✓</span>' : '<span class="badge b-neu">NO</span>';
  const obiPts = det.obi_pts || 0;
  document.getElementById('obi-pts').textContent = fmt(obiPts,1) + ' / 40';
  document.getElementById('obi-pts-badge').textContent = fmt(obiPts,0) + ' pts';
  document.getElementById('obi-pts-badge').style.color = obiPts >= 25 ? 'var(--green2)' : 'var(--ink3)';

  // CVD
  const buyPct = (det.buy_ratio || 0.5) * 100;
  document.getElementById('buy-pct').textContent = fmt(buyPct,1) + '%';
  document.getElementById('cvd-bar').style.width = buyPct + '%';
  document.getElementById('cvd-bar').className = 'pbar-fill ' + (buyPct > 55 ? 'pbar-g' : buyPct < 45 ? 'pbar-r' : 'pbar-a');
  const cvdD = det.cvd_delta || 0;
  document.getElementById('cvd-delta').innerHTML = `<span style="color:${cvdD>=0?'var(--green2)':'var(--red2)'}">${cvdD>=0?'+':''}${fmt(cvdD,1)}</span>`;
  document.getElementById('cvd-accel').innerHTML = det.accel ? '<span class="badge b-pass">YES ✓</span>' : '<span class="badge b-neu">NO</span>';
  document.getElementById('cvd-div').innerHTML = det.cvd_diverge ? '<span class="badge b-fail">DIVERGE ⚠</span>' : '<span class="badge b-pass">CLEAN ✓</span>';
  const flowPts = det.flow_pts || 0;
  document.getElementById('cvd-pts').textContent = fmt(flowPts,1) + ' / 38';
  document.getElementById('cvd-pts-badge').textContent = fmt(flowPts,0) + ' pts';
  document.getElementById('cvd-pts-badge').style.color = flowPts >= 20 ? 'var(--green2)' : 'var(--ink3)';

  // OI
  const oiPct = det.oi_pct || 0;
  document.getElementById('oi-pct').innerHTML = `<span style="color:${oiPct>=0?'var(--green2)':'var(--red2)'}">${oiPct>=0?'+':''}${fmt(oiPct,3)}%</span>`;
  document.getElementById('oi-now').textContent = fmt(det.oi_now || 0, 0);
  const oiSig = det.oi_signal || 'neutral';
  document.getElementById('oi-signal').innerHTML = `<span class="badge ${oiSig==='long'?'b-long':oiSig==='short'?'b-short':oiSig==='unwind'?'b-fail':'b-neu'}">${oiSig.toUpperCase()}</span>`;
  document.getElementById('oi-price-dir').innerHTML = det.price_up ? '<span class="badge b-long">UP ↑</span>' : '<span class="badge b-short">DOWN ↓</span>';
  // estimate OI pts
  const oiPtsEst = oiSig === sp.direction ? 20 : oiSig === 'unwind' ? 0 : 5;
  document.getElementById('oi-pts').textContent = oiPtsEst + ' / 20';
  document.getElementById('oi-pts-badge').textContent = oiPtsEst + ' pts';
  document.getElementById('oi-pts-badge').style.color = oiPtsEst >= 15 ? 'var(--green2)' : 'var(--ink3)';

  // Confluence
  const votes = det.votes || 0;
  const confPts = det.votes_agree ? 10 : 0;
  document.getElementById('votes-val').textContent = votes + ' / 3';
  document.getElementById('votes-agree').innerHTML = det.votes_agree ? '<span class="badge b-pass">YES +10 pts</span>' : '<span class="badge b-neu">NO</span>';
  document.getElementById('conf-pts').textContent = confPts + ' / 10';
  document.getElementById('conf-pts-badge').textContent = confPts + ' pts';
  document.getElementById('conf-pts-badge').style.color = confPts > 0 ? 'var(--green2)' : 'var(--ink3)';
}

// ── Position ──
function renderPosition() {
  const pos = data.position;
  const el = document.getElementById('pos-content');
  if (!pos) {
    el.innerHTML = `<div class="no-pos"><div class="no-pos-icon">◎</div><div class="no-pos-txt">No open position</div></div>`;
    return;
  }
  const lp = pos.live_price || pos.entry;
  const pnl = pos.live_pnl_pct || 0;
  const pnlCol = pnl >= 0 ? 'var(--green2)' : 'var(--red2)';
  const isLong = pos.side === 'long';
  const tp1c = pos.tp1_hit ? 'tp-hit' : '';
  const tp2c = pos.tp2_hit ? 'tp-hit' : '';
  const line1c = pos.tp1_hit ? 'tp-hit-line' : '';
  const line2c = pos.tp2_hit ? 'tp-hit-line' : '';
  const dur = data.now ? Math.floor((data.now - (pos.open_time||data.now)) / 60) : 0;

  el.innerHTML = `
  <div class="pos-card">
    <div class="pos-hdr">
      <div>
        <div class="pos-sym">${pos.symbol.replace('USDT','')}<span>/USDT</span></div>
        <div class="pos-meta">
          <span class="badge ${isLong?'b-long':'b-short'}">${pos.side.toUpperCase()}</span>
          <span class="badge b-neu">${dur}m open</span>
          ${pos.be_moved ? '<span class="badge b-pass">BE ✓</span>' : ''}
        </div>
      </div>
      <div>
        <div class="pos-score-big">${pos.score}</div>
        <div class="pos-score-sub">${pos.label}</div>
      </div>
    </div>

    <div class="pos-metrics">
      <div class="pm"><span class="pm-k">Entry</span><span class="pm-v">$${fmt(pos.entry,4)}</span></div>
      <div class="pm"><span class="pm-k">Mark Price</span><span class="pm-v" style="color:${pnlCol}">$${fmt(lp,4)}</span></div>
      <div class="pm"><span class="pm-k">Unrealised P&L</span><span class="pm-v" style="color:${pnlCol}">${pnl>=0?'+':''}${fmt(pnl,2)}%</span></div>
      <div class="pm"><span class="pm-k">Stop Loss</span><span class="pm-v" style="color:var(--red2)">$${fmt(pos.sl,4)}</span></div>
      <div class="pm"><span class="pm-k">Qty</span><span class="pm-v">${pos.qty}</span></div>
      <div class="pm"><span class="pm-k">Remaining</span><span class="pm-v">${pos.qty_remaining}</span></div>
    </div>

    <div class="tp-row">
      <div class="tp-node ${tp1c}">
        <div class="tp-circle"></div>
        <span class="tp-k">TP1</span>
        <span class="tp-v">+${pos.tp1_pct}%</span>
        ${pos.tp1_hit ? '<span style="font-size:10px;color:var(--green3)">✓</span>' : ''}
      </div>
      <div class="tp-line ${line1c}"></div>
      <div class="tp-node ${tp2c}">
        <div class="tp-circle"></div>
        <span class="tp-k">TP2</span>
        <span class="tp-v">+${pos.tp2_pct}%</span>
        ${pos.tp2_hit ? '<span style="font-size:10px;color:var(--green3)">✓</span>' : ''}
      </div>
      <div class="tp-line ${line2c}"></div>
      <div class="tp-node">
        <div class="tp-circle"></div>
        <span class="tp-k">TP3</span>
        <span class="tp-v">+${pos.tp3_pct}%</span>
      </div>
    </div>
    ${pos.be_moved ? '<div class="be-badge">⬆ Stop loss moved to breakeven</div>' : ''}
  </div>

  <div class="slbl" style="margin-top:20px">Entry Signal Params</div>
  <div class="icard">
    <div class="ir"><span class="k">OBI Score</span><span class="v">${pos.score} pts</span></div>
    <div class="ir"><span class="k">Label</span><span class="v">${pos.label}</span></div>
    <div class="ir"><span class="k">Direction</span><span class="v"><span class="badge ${isLong?'b-long':'b-short'}">${pos.side.toUpperCase()}</span></span></div>
    <div class="ir"><span class="k">TP1 / TP2 / TP3</span><span class="v">${pos.tp1_pct}% / ${pos.tp2_pct}% / ${pos.tp3_pct}%</span></div>
    <div class="ir"><span class="k">Stop Loss %</span><span class="v">−${pos.sl_pct}%</span></div>
  </div>`;
}

// ── Scan Log ──
function renderScanLog() {
  const log = data.scan_log || [];
  const el = document.getElementById('scan-log-list');
  if (!log.length) return;

  el.innerHTML = log.map((s,i) => {
    const blocked = s.blocked_by;
    const score = s.score;
    const det = s.details || {};
    const gate = s.gate || {};
    const scoreColor = !score ? 'var(--ink3)' : score >= 75 ? 'var(--green2)' : score >= 55 ? 'var(--gold2)' : 'var(--red2)';
    const blockedTag = blocked === 'trend_gate'
      ? '<span class="badge b-fail">TREND BLOCKED</span>'
      : blocked === 'score'
      ? `<span class="badge b-warn">SCORE ${score}</span>`
      : '<span class="badge b-pass">SIGNAL ✓</span>';

    const dirTag = s.direction === 'long'
      ? '<span class="badge b-long">LONG</span>'
      : '<span class="badge b-short">SHORT</span>';

    const age = data.now ? (data.now - (s.time||data.now)) : 0;
    const ageStr = age < 60 ? age+'s' : Math.floor(age/60)+'m';

    let detailRows = '';
    // ADX in gate detail
    if (gate.adx && gate.adx !== 'removed') detailRows += `<div class="sdrow"><span class="k">ADX</span><span class="v">${fmt(gate.adx,1)} ${gate.adx_ok?'✓ Trending':'✗ <18 (choppy)'}</span></div>`;
    if (gate.ut)  detailRows += `<div class="sdrow"><span class="k">UT Bot</span><span class="v">${gate.ut.toUpperCase()} ${gate.ut_ok?'✓':''}</span></div>`;
    if (gate.vwap_ok !== undefined) detailRows += `<div class="sdrow"><span class="k">VWAP</span><span class="v">${gate.vwap_ok?'Price on right side ✓':'Wrong side of VWAP ✗'}</span></div>`;
    if (det.obi)  detailRows += `<div class="sdrow"><span class="k">OBI</span><span class="v">${fmt(det.obi_pct,1)}% bids · ${fmt(det.obi_pts,0)} pts</span></div>`;
    if (det.buy_ratio) detailRows += `<div class="sdrow"><span class="k">Buy Flow</span><span class="v">${fmt(det.buy_pct,1)}% buy · ${fmt(det.flow_pts,0)} pts</span></div>`;
    if (det.oi_signal) detailRows += `<div class="sdrow"><span class="k">OI Signal</span><span class="v">${det.oi_signal.toUpperCase()}</span></div>`;
    if (blocked === 'trend_gate' && gate.reason) detailRows += `<div class="sdrow"><span class="k">Block reason</span><span class="v" style="color:var(--red2)">${gate.reason}</span></div>`;

    return `
    <div class="scan-item ${!blocked?'signal-flash':''}">
      <div class="scan-hdr" onclick="toggleScan(${i})">
        <div>
          <div class="scan-sym">${s.symbol.replace('USDT','')}<span>/USDT</span></div>
          <div style="margin-top:4px;font-size:11px;color:var(--ink3);font-family:'IBM Plex Mono',monospace">Δ${s.change>=0?'+':''}${fmt(s.change,2)}% · ${s.vol}M vol</div>
        </div>
        <div class="scan-right">
          <span class="scan-score" style="color:${scoreColor}">${score !== null ? score : '—'}</span>
          <span class="scan-time">${ageStr} ago</span>
        </div>
      </div>
      <div class="scan-tags">
        ${dirTag} ${blockedTag}
      </div>
      <div class="scan-detail" id="scan-det-${i}">
        <div class="scan-detail-inner">
          ${detailRows || '<div class="sdrow"><span class="k">—</span><span class="v">no detail</span></div>'}
        </div>
      </div>
    </div>`;
  }).join('');
}

function toggleScan(i) {
  const el = document.getElementById('scan-det-' + i);
  el.classList.toggle('open');
}

// ── Trades ──
function renderTrades() {
  const trades = (data.trades || []).slice().reverse();
  const el = document.getElementById('trade-list');
  if (!trades.length) return;

  el.innerHTML = trades.map(t => {
    const isWin = (t.result||'').toLowerCase() === 'win';
    const pnl = t.pnl || 0;
    const isLong = (t.direction || t.side || '').toLowerCase() === 'long';
    return `
    <div class="trade-item">
      <div class="t-left">
        <div class="t-sym">${(t.symbol||'').replace('USDT','')}<span style="font-size:11px;color:var(--ink3);font-family:'IBM Plex Mono',monospace">/USDT</span></div>
        <div class="t-meta">
          <span class="badge ${isLong?'b-long':'b-short'}">${isLong?'LONG':'SHORT'}</span>
          <span class="badge b-neu" style="font-size:9px">${t.score||'—'} · ${t.label||'—'}</span>
        </div>
        <div style="margin-top:6px;font-size:10px;color:var(--ink3);font-family:'IBM Plex Mono',monospace">
          Entry $${fmt(t.entry,4)} → Exit $${fmt(t.exit_price,4)}
        </div>
      </div>
      <div class="t-right">
        <div class="t-pnl" style="color:${pnl>=0?'var(--green2)':'var(--red2)'}">${pnl>=0?'+':''}$${fmt(pnl,2)}</div>
        <div style="margin-top:3px"><span class="badge ${isWin?'b-pass':'b-fail'}">${(t.result||'').toUpperCase()}</span></div>
        <div class="t-exit">${t.exit_type||'—'} · ${fmt(t.pnl_pct,2)}%</div>
      </div>
    </div>`;
  }).join('');
}

// ── Start ──
fetchData();
</script>
</body></html>
"""

if __name__=="__main__":
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT",8000)))
