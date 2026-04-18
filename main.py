"""
APEX Pro — Clean Rebuild
════════════════════════════════════════════════════════
Signal stack:
  Entry gate  : VWAP direction check
  Score (0-100): OBI 35pts + CVD 35pts + OI Delta 20pts + bonus 10pts
  Min score   : 75
  Max positions: 3 concurrent
  Exit        : Dynamic (momentum exhaustion) + hard SL

Dashboard: Server-rendered HTML — no JS fetch issues
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn, os, time, asyncio, hmac, hashlib, json, math
import httpx

app = FastAPI()

# ── CONFIG ───────────────────────────────────────────────────
API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BASE       = "https://api.bybit.com"
SB_URL     = os.getenv("SUPABASE_URL", "")
SB_KEY     = os.getenv("SUPABASE_KEY", "")
MIN_SCORE  = 75
MAX_POS    = 3
LEVERAGE   = 2
INTERVAL   = 30

WATCHLIST = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "DOGEUSDT","AVAXUSDT","LINKUSDT","NEARUSDT","APTUSDT",
    "OPUSDT","ARBUSDT","INJUSDT","SUIUSDT","THETAUSDT",
    "LDOUSDT","STXUSDT","FTMUSDT","GRTUSDT","SANDUSDT",
]

state = {
    "wins":0,"losses":0,"pnl":0.0,"balance":0.0,
    "positions":{},"trades":[],"scan_log":[],
    "trend_blocks":0,"score_blocks":0,
    "last_signal":None,"status":"starting",
    "last_scan":0,"start":int(time.time()),
}

# ── BYBIT ────────────────────────────────────────────────────

def _sign(p):
    p["api_key"]=API_KEY; p["timestamp"]=str(int(time.time()*1000)); p["recv_window"]="5000"
    q="&".join(f"{k}={v}" for k,v in sorted(p.items()))
    p["sign"]=hmac.new(API_SECRET.encode(),q.encode(),hashlib.sha256).hexdigest()
    return p

async def _get(path,params={},signed=False):
    if signed: params=_sign(dict(params))
    async with httpx.AsyncClient(timeout=8) as c:
        r=await c.get(BASE+path,params=params); return r.json()

async def _post(path,body):
    body=_sign(dict(body))
    async with httpx.AsyncClient(timeout=8) as c:
        r=await c.post(BASE+path,json=body); return r.json()

async def get_balance():
    try:
        r=await _get("/v5/account/wallet-balance",{"accountType":"UNIFIED"},signed=True)
        if r.get("retCode")==0:
            account=r["result"]["list"][0]
            # Use account-level totalAvailableBalance first (most reliable)
            bal=float(account.get("totalAvailableBalance") or 0)
            if bal>0:
                print(f"Balance: ${bal:.2f} (account level)")
                return bal
            # Fallback: find USDT coin entry
            for c in account.get("coin",[]):
                if c["coin"]=="USDT":
                    val=float(c.get("walletBalance") or c.get("equity") or 0)
                    if val>0:
                        print(f"Balance: ${val:.2f} (USDT coin)")
                        return val
        print(f"Balance API retCode={r.get('retCode')} msg={r.get('retMsg')}")
    except Exception as e:
        print(f"Balance err: {e}")
    return 0.0

async def get_klines(symbol,interval="5",limit=30):
    try:
        r=await _get("/v5/market/kline",{"category":"linear","symbol":symbol,"interval":interval,"limit":str(limit)})
        return list(reversed(r["result"]["list"]))
    except: return []

async def get_orderbook(symbol):
    try:
        r=await _get("/v5/market/orderbook",{"category":"linear","symbol":symbol,"limit":"50"})
        return r.get("result",{})
    except: return {}

async def get_trades(symbol):
    try:
        r=await _get("/v5/market/recent-trade",{"category":"linear","symbol":symbol,"limit":"200"})
        return r.get("result",{}).get("list",[])
    except: return []

async def get_oi(symbol):
    try:
        r=await _get("/v5/market/open-interest",{"category":"linear","symbol":symbol,"intervalTime":"5min","limit":"6"})
        return r.get("result",{}).get("list",[])
    except: return []

async def get_ticker(symbol):
    try:
        r=await _get("/v5/market/tickers",{"category":"linear","symbol":symbol})
        items=r.get("result",{}).get("list",[])
        return items[0] if items else {}
    except: return {}

async def get_instrument(symbol):
    try:
        r=await _get("/v5/market/instruments-info",{"category":"linear","symbol":symbol})
        info=r.get("result",{}).get("list",[])
        if info:
            lot=info[0].get("lotSizeFilter",{})
            return float(lot.get("qtyStep","0.001")),float(lot.get("minOrderQty","0.001"))
    except: pass
    return 0.001,0.001

async def place_order(symbol,side,qty,sl):
    return await _post("/v5/order/create",{
        "category":"linear","symbol":symbol,
        "side":"Buy" if side=="long" else "Sell",
        "orderType":"Market","qty":str(qty),
        "stopLoss":str(round(sl,6)),
        "leverage":str(LEVERAGE),"positionIdx":"0",
    })

async def close_order(symbol,qty,side):
    return await _post("/v5/order/create",{
        "category":"linear","symbol":symbol,
        "side":"Sell" if side=="long" else "Buy",
        "orderType":"Market","qty":str(qty),
        "reduceOnly":True,"positionIdx":"0",
    })

async def check_open(symbol,side):
    try:
        r=await _get("/v5/position/list",{"category":"linear","symbol":symbol},signed=True)
        for p in r.get("result",{}).get("list",[]):
            if p.get("symbol")==symbol and float(p.get("size",0))>0:
                return True
        return False
    except: return True

# ── VWAP GATE ────────────────────────────────────────────────

def _vwap(candles):
    tpv=tv=0.0
    for c in candles:
        h=float(c[2]);l=float(c[3]);cl=float(c[4]);v=float(c[5])
        tpv+=(h+l+cl)/3*v; tv+=v
    return tpv/tv if tv>0 else 0.0

async def vwap_gate(symbol,direction):
    c=await get_klines(symbol,"5",30)
    if not c: return False,{}
    vwap=_vwap(c); price=float(c[-1][4])
    pct=round((price-vwap)/vwap*100,3)
    ok=price>vwap if direction=="long" else price<vwap
    info={"vwap":round(vwap,4),"price":round(price,4),"pct":pct,"ok":ok}
    if not ok: info["reason"]=f"Price {'below' if direction=='long' else 'above'} VWAP ({pct:+.2f}%)"
    return ok,info

# ── SCORE ────────────────────────────────────────────────────

async def score_signal(symbol,direction):
    ob,tr,oi,tk=await asyncio.gather(
        get_orderbook(symbol),get_trades(symbol),
        get_oi(symbol),get_ticker(symbol),
    )
    score=0; det={}; obi=0.5; buy_ratio=0.5; oi_sig="neutral"

    # OBI — 35pts
    bids=ob.get("b",[]); asks=ob.get("a",[])
    if bids and asks:
        bv=sum(float(b[1]) for b in bids); av=sum(float(a[1]) for a in asks)
        tot=bv+av; obi=bv/tot if tot>0 else 0.5
        dist=max(0,obi-0.5)*2 if direction=="long" else max(0,0.5-obi)*2
        pts=min(dist*35,35); score+=pts
        bvols=[float(b[1]) for b in bids]
        stack=len(bvols)>=10 and sum(bvols[:5])/5>=sum(bvols[-5:])/5*1.5
        if stack and direction=="long": score+=5
        det["obi"]=round(obi,4); det["obi_pts"]=round(pts,1); det["stack"]=stack

    # CVD — 35pts
    if tr:
        bvol=sum(float(t["size"]) for t in tr if t.get("side")=="Buy")
        svol=sum(float(t["size"]) for t in tr if t.get("side")=="Sell")
        tot=bvol+svol; buy_ratio=bvol/tot if tot>0 else 0.5
        dist2=max(0,buy_ratio-0.5)*2 if direction=="long" else max(0,0.5-buy_ratio)*2
        pts2=min(dist2*35,35); score+=pts2
        f50=tr[100:150] if len(tr)>=150 else tr[:len(tr)//2]
        l50=tr[:50]
        fb=sum(float(t["size"]) for t in f50 if t.get("side")=="Buy")
        lb=sum(float(t["size"]) for t in l50 if t.get("side")=="Buy")
        if lb>fb*1.2 and direction=="long": score+=3
        # Divergence penalty
        if len(tr)>=100:
            early=tr[len(tr)//2:]; late=tr[:50]
            ep=float(early[0]["price"]) if early else 0
            lp=float(late[0]["price"]) if late else 0
            ec=sum(float(t["size"]) for t in early if t.get("side")=="Buy")-\
               sum(float(t["size"]) for t in early if t.get("side")=="Sell")
            lc=sum(float(t["size"]) for t in late if t.get("side")=="Buy")-\
               sum(float(t["size"]) for t in late if t.get("side")=="Sell")
            if ep>0 and (lp>ep)!=(lc>ec): score-=8
        det["buy_ratio"]=round(buy_ratio,4); det["cvd_pts"]=round(pts2,1)

    # OI Delta — 20pts
    if oi and len(oi)>=2:
        try:
            oi_now=float(oi[0]["openInterest"]); oi_prev=float(oi[-1]["openInterest"])
            pct=(oi_now-oi_prev)/oi_prev*100 if oi_prev>0 else 0
            price_now=float(tk.get("lastPrice",0))
            price_prev=float(tk.get("prevPrice24h",price_now) or price_now)
            price_up=price_now>price_prev
            if pct>0.3 and price_up: oi_sig="long"; score+=20
            elif pct>0.3 and not price_up: oi_sig="short"; score+=(20 if direction=="short" else 0)
            elif pct<-0.3: oi_sig="unwind"; score-=5
            else: score+=5
            det["oi_pct"]=round(pct,3); det["oi_sig"]=oi_sig
        except: score+=5
    else: score+=5

    # All agree bonus
    votes=0
    if obi>0.58 and direction=="long": votes+=1
    if obi<0.42 and direction=="short": votes+=1
    if buy_ratio>0.55 and direction=="long": votes+=1
    if buy_ratio<0.45 and direction=="short": votes+=1
    if oi_sig==direction: votes+=1
    if votes==3: score+=10
    det["votes"]=votes

    score=max(0,min(100,int(score)))
    label="VERY STRONG" if score>=90 else "STRONG" if score>=75 else "MEDIUM" if score>=55 else "WEAK"
    return score,label,det

# ── SIZING + SL ──────────────────────────────────────────────

def get_sl(score,entry,direction):
    sl_pct=3.0 if score>=90 else 2.5 if score>=80 else 2.0
    m=1 if direction=="long" else -1
    return round(entry*(1-m*sl_pct/100),6), sl_pct

def get_size(score,balance):
    pct=25 if score>=90 else 20 if score>=80 else 15
    return round(min(balance*pct/100,balance*0.30),2)

def round_qty(qty,step):
    if step<=0: return round(qty,3)
    dec=max(0,-int(math.floor(math.log10(step))))
    return round(math.floor(qty/step)*step,dec)

# ── MOMENTUM EXIT ────────────────────────────────────────────

async def check_exit(symbol,pos):
    entry=pos["entry"]; side=pos["side"]
    c=await get_klines(symbol,"5",15)
    if not c or len(c)<10: return False,""
    price=float(c[-1][4])
    pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100
    if pnl<0.3: return False,""
    bodies=[abs(float(x[4])-float(x[1])) for x in c]
    shrinking=bodies[-3]>bodies[-2]>bodies[-1]
    avg=sum(bodies[-10:])/10; tiny=avg>0 and bodies[-1]<avg*0.4
    tr=await get_trades(symbol); cvd_flat=False
    if tr and len(tr)>=100:
        f50=tr[50:100]; l50=tr[:50]
        fc=sum(float(t["size"]) for t in f50 if t.get("side")=="Buy")-\
           sum(float(t["size"]) for t in f50 if t.get("side")=="Sell")
        lc=sum(float(t["size"]) for t in l50 if t.get("side")=="Buy")-\
           sum(float(t["size"]) for t in l50 if t.get("side")=="Sell")
        cvd_flat=(lc<fc*0.65 if side=="long" else lc>fc*0.65)
    fired=sum([shrinking,tiny,cvd_flat])
    if fired>=2:
        parts=[]
        if shrinking: parts.append("shrinking candles")
        if tiny: parts.append("tiny body")
        if cvd_flat: parts.append("CVD flat")
        return True,f"Exhaustion ({', '.join(parts)}) at {pnl:.2f}%"
    return False,""

# ── SCAN LOOP ────────────────────────────────────────────────

async def scan_once():
    # Monitor existing positions
    if state["positions"]:
        await asyncio.gather(*[monitor(sym,pos) for sym,pos in list(state["positions"].items())])

    # Scan for new signals if slots available
    slots=MAX_POS-len(state["positions"])
    if slots<=0: return
    state["balance"]=await get_balance()
    if state["balance"]<10: return

    new=0
    for sym in WATCHLIST:
        if new>=slots: break
        if sym in state["positions"]: continue
        try:
            entered=await scan_symbol(sym)
            if entered: new+=1
            await asyncio.sleep(0.8)
        except Exception as e:
            print(f"Scan err {sym}: {e}")

async def scan_symbol(sym):
    tk=await get_ticker(sym)
    if not tk: return False
    price=float(tk.get("lastPrice",0))
    p24=float(tk.get("prevPrice24h",price) or price)
    chg=(price-p24)/p24*100 if p24>0 else 0
    vol=float(tk.get("volume24h",0))
    if vol<500_000 or abs(chg)>15: return False

    direction="long" if chg>0 else "short"
    if chg>5 and direction=="long": return False
    if chg<-5 and direction=="short": return False

    gate_ok,gate=await vwap_gate(sym,direction)
    log_entry={"symbol":sym,"direction":direction,"price":round(price,4),
               "change":round(chg,2),"vol":round(vol/1e6,1),
               "gate":gate,"score":None,"label":None,"blocked_by":None,"time":int(time.time())}

    if not gate_ok:
        state["trend_blocks"]+=1
        log_entry["blocked_by"]="trend"
        log_entry["block_reason"]=gate.get("reason","")
        _log(log_entry); return False

    score,label,det=await score_signal(sym,direction)
    log_entry["score"]=score; log_entry["label"]=label; log_entry["details"]=det

    if score<MIN_SCORE:
        state["score_blocks"]+=1
        log_entry["blocked_by"]="score"
        _log(log_entry); return False

    log_entry["blocked_by"]=None
    _log(log_entry)
    print(f"  SIGNAL {sym} {direction.upper()} {score} ({label})")
    state["last_signal"]={"symbol":sym,"direction":direction,"score":score,"label":label,"time":int(time.time())}
    return await enter(sym,direction,score,label,price)

def _log(entry):
    state["scan_log"].insert(0,entry)
    state["scan_log"]=state["scan_log"][:20]

async def enter(sym,direction,score,label,price):
    per_slot=state["balance"]/MAX_POS
    size=get_size(score,per_slot)
    sl,sl_pct=get_sl(score,price,direction)
    step,min_qty=await get_instrument(sym)
    qty=round_qty(size*LEVERAGE/price,step)
    if qty<min_qty:
        qty=round_qty(state["balance"]*0.20*LEVERAGE/price,step)
    if qty<min_qty:
        print(f"  SKIP {sym}: qty {qty} < min {min_qty}"); return False
    resp=await place_order(sym,direction,qty,sl)
    if not resp or resp.get("retCode")!=0:
        print(f"  ORDER FAIL {sym}: {resp}"); return False
    state["positions"][sym]={
        "symbol":sym,"side":direction,"entry":price,"qty":qty,
        "sl":sl,"sl_pct":sl_pct,"score":score,"label":label,
        "peak_pnl":0.0,"open_time":int(time.time()),
    }
    print(f"  ENTERED {sym} {direction.upper()} qty={qty} SL={sl} ({sl_pct}%)")
    await sb_save(state["positions"][sym],"open",price)
    return True

async def monitor(sym,pos):
    side=pos["side"]; entry=pos["entry"]
    still=await check_open(sym,side)
    if not still:
        tk=await get_ticker(sym)
        price=float(tk.get("lastPrice",entry)) if tk else entry
        pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100
        await close_trade(pos,price,"win" if pnl>0 else "loss","ManualClose"); return

    tk=await get_ticker(sym)
    if not tk: return
    price=float(tk.get("lastPrice",0))
    if price==0: return
    pnl=(price-entry)/entry*100 if side=="long" else (entry-price)/entry*100
    if pnl>pos.get("peak_pnl",0): pos["peak_pnl"]=round(pnl,3)

    sl_hit=(side=="long" and price<=pos["sl"]) or (side=="short" and price>=pos["sl"])
    if sl_hit:
        r=await close_order(sym,pos["qty"],side)
        if r and r.get("retCode")==0:
            await close_trade(pos,price,"loss","SL"); return

    exit_now,reason=await check_exit(sym,pos)
    if exit_now:
        r=await close_order(sym,pos["qty"],side)
        if r and r.get("retCode")==0:
            result="win" if pnl>0 else "loss"
            await close_trade(pos,price,result,"DynamicExit"); return

    print(f"  HOLD {sym} | {pnl:+.2f}% | Peak:{pos.get('peak_pnl',0):+.2f}%")

async def close_trade(pos,exit_price,result,exit_type):
    entry=pos["entry"]; side=pos["side"]
    pnl_pct=(exit_price-entry)/entry*100 if side=="long" else (entry-exit_price)/entry*100
    pnl_usd=pos["qty"]*entry*(pnl_pct/100)*LEVERAGE
    trade={**pos,"exit_price":exit_price,"pnl":round(pnl_usd,4),
           "pnl_pct":round(pnl_pct,3),"result":result,"exit_type":exit_type,
           "close_time":int(time.time())}
    state["trades"].append(trade); state["trades"]=state["trades"][-100:]
    if result=="win": state["wins"]+=1
    else: state["losses"]+=1
    state["pnl"]+=pnl_usd
    state["positions"].pop(pos["symbol"],None)
    print(f"  CLOSED {pos['symbol']} {result.upper()} {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
    await sb_save(trade,result,exit_price)

# ── SUPABASE ─────────────────────────────────────────────────

async def sb_save(trade,result,price):
    if not SB_URL or not SB_KEY: return
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            await c.post(f"{SB_URL}/rest/v1/trades",
                json={"symbol":trade.get("symbol"),"direction":trade.get("side",trade.get("direction")),
                      "result":result,"entry":trade.get("entry"),"exit_price":price,
                      "pnl":trade.get("pnl",0),"pnl_pct":trade.get("pnl_pct",0),
                      "score":trade.get("score"),"label":trade.get("label"),
                      "exit_type":trade.get("exit_type","")},
                headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                         "Content-Type":"application/json","Prefer":"return=minimal"})
    except: pass

async def sb_load():
    if not SB_URL or not SB_KEY: return
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r=await c.get(f"{SB_URL}/rest/v1/trades?select=result,pnl&order=created_at.asc",
                         headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}"})
            trades=r.json()
            if isinstance(trades,list):
                state["wins"]=sum(1 for t in trades if (t.get("result","")).lower()=="win")
                state["losses"]=sum(1 for t in trades if (t.get("result","")).lower()=="loss")
                state["pnl"]=round(sum(float(t.get("pnl",0)) for t in trades),4)
                print(f"Restored: W={state['wins']} L={state['losses']} PnL=${state['pnl']:.2f}")
    except Exception as e:
        print(f"Supabase load err: {e}")

# ── BOT LOOP ─────────────────────────────────────────────────

async def bot_loop():
    print("APEX Pro starting...")
    await sb_load()
    state["balance"]=await get_balance()
    state["status"]="running"
    state["last_scan"]=int(time.time())
    while True:
        try:
            state["last_scan"]=int(time.time())
            await scan_once()
        except Exception as e:
            print(f"Loop err: {e}")
        await asyncio.sleep(INTERVAL)

@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_loop())

# ── API ──────────────────────────────────────────────────────

@app.get("/api/debug")
async def debug():
    """Shows raw Bybit balance response — use to diagnose balance issues"""
    results={}
    for acct_type in ["UNIFIED","CONTRACT"]:
        try:
            r=await _get("/v5/account/wallet-balance",{"accountType":acct_type},signed=True)
            results[acct_type]=r
        except Exception as e:
            results[acct_type]={"error":str(e)}
    return JSONResponse(results)

@app.get("/api/health")
async def health():
    return {"ok":True,"status":state["status"],"balance":state["balance"],
            "positions":len(state["positions"]),"uptime":int(time.time())-state["start"]}

@app.get("/api/status")
async def api_status():
    pos_data={}
    for sym,pos in state["positions"].items():
        try:
            tk=await asyncio.wait_for(get_ticker(sym),timeout=3)
            lp=float(tk.get("lastPrice",pos["entry"]))
            pnl=(lp-pos["entry"])/pos["entry"]*100 if pos["side"]=="long" else (pos["entry"]-lp)/pos["entry"]*100
            pos_data[sym]={**pos,"live_price":round(lp,6),"live_pnl":round(pnl,3)}
        except:
            pos_data[sym]={**pos,"live_price":pos["entry"],"live_pnl":0.0}
    return JSONResponse({
        "status":state["status"],"balance":state["balance"],
        "wins":state["wins"],"losses":state["losses"],"pnl":state["pnl"],
        "min_score":MIN_SCORE,"max_pos":MAX_POS,"open":len(state["positions"]),
        "trend_blocks":state["trend_blocks"],"score_blocks":state["score_blocks"],
        "positions":pos_data,"last_signal":state["last_signal"],
        "scan_log":state["scan_log"][:15],"trades":state["trades"][-30:],
        "last_scan":state["last_scan"],"now":int(time.time()),
    })

# ── DASHBOARD — pure server-rendered, no JS fetch needed ─────

@app.get("/",response_class=HTMLResponse)
async def dashboard():
    wins=state["wins"]; losses=state["losses"]; total=wins+losses
    wr=f"{wins/total*100:.0f}%" if total>0 else "—"
    pnl=state["pnl"]; bal=state["balance"]
    trades=state["trades"]
    avg_win=sum(t["pnl"] for t in trades if t.get("result")=="win")/max(wins,1)
    avg_loss=sum(t["pnl"] for t in trades if t.get("result")=="loss")/max(losses,1)
    age=int(time.time())-state["last_scan"] if state["last_scan"]>0 else "?"

    def pnl_col(v): return "#22c55e" if v>=0 else "#ef4444"
    def fmt(v,d=2): return f"{v:.{d}f}"

    # Positions HTML
    pos_html=""
    if state["positions"]:
        for sym,pos in state["positions"].items():
            try:
                tk=await asyncio.wait_for(get_ticker(sym),timeout=2)
                lp=float(tk.get("lastPrice",pos["entry"]))
            except:
                lp=pos["entry"]
            pnl_pct=(lp-pos["entry"])/pos["entry"]*100 if pos["side"]=="long" else (pos["entry"]-lp)/pos["entry"]*100
            dur=int((time.time()-pos["open_time"])/60)
            col=pnl_col(pnl_pct)
            pos_html+=f"""
            <div class="card pos-card">
              <div class="row"><b>{sym}</b>
                <span class="badge {'long' if pos['side']=='long' else 'short'}">{pos['side'].upper()}</span>
                <span class="score-badge">{pos['score']} · {pos['label']}</span>
              </div>
              <div class="row mt8">
                <span>Entry <b>${fmt(pos['entry'],4)}</b></span>
                <span>Now <b style="color:{col}">${fmt(lp,4)}</b></span>
                <span>P&L <b style="color:{col}">{pnl_pct:+.2f}%</b></span>
              </div>
              <div class="row mt4">
                <span>SL <b style="color:#ef4444">${fmt(pos['sl'],4)}</b></span>
                <span>Peak <b style="color:#22c55e">{pos.get('peak_pnl',0):+.2f}%</b></span>
                <span style="color:#888">{dur}m open</span>
              </div>
            </div>"""
    else:
        pos_html='<div class="empty">No open positions — scanning...</div>'

    # Scan log HTML
    log_html=""
    for s in state["scan_log"][:12]:
        blocked=s.get("blocked_by")
        score=s.get("score")
        age_s=int(time.time())-s.get("time",int(time.time()))
        age_str=f"{age_s//60}m" if age_s>=60 else f"{age_s}s"
        if blocked=="trend":
            tag=f'<span class="tag tag-blocked">TREND BLOCKED</span>'
        elif blocked=="score":
            tag=f'<span class="tag tag-score">SCORE {score}</span>'
        else:
            tag=f'<span class="tag tag-signal">SIGNAL ✓ {score}</span>'
        dir_tag=f'<span class="tag tag-{"long" if s.get("direction")=="long" else "short"}">{(s.get("direction","")).upper()}</span>'
        log_html+=f"""
        <div class="log-row">
          <span class="sym">{s.get('symbol','').replace('USDT','')}/USDT</span>
          {dir_tag} {tag}
          <span class="age">{age_str} ago</span>
        </div>"""

    if not log_html:
        log_html='<div class="empty">Scanning coins...</div>'

    # Trades HTML
    trades_html=""
    for t in reversed(state["trades"][-15:]):
        res=t.get("result","")
        p=t.get("pnl",0)
        col=pnl_col(p)
        trades_html+=f"""
        <div class="log-row">
          <span class="sym">{t.get('symbol','').replace('USDT','')}</span>
          <span class="tag tag-{'long' if (t.get('side',t.get('direction',''))).lower() in ['long','buy'] else 'short'}">{(t.get('side',t.get('direction',''))).upper()}</span>
          <span style="font-size:11px;color:#888">{t.get('score','—')}</span>
          <span style="font-size:11px;color:#888">{t.get('exit_type','—')}</span>
          <span style="color:{col};font-weight:700;margin-left:auto">{'+' if p>=0 else ''}${fmt(p)}</span>
          <span class="tag tag-{'signal' if res=='win' else 'blocked'}">{res.upper()}</span>
        </div>"""
    if not trades_html:
        trades_html='<div class="empty">No trades yet</div>'

    html=f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX Pro</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#f5f2ed;color:#1a1a1a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:16px;max-width:600px;margin:0 auto}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid #ddd}}
.logo{{font-size:20px;font-weight:700;letter-spacing:-.5px}}
.logo span{{color:#16a34a}}
.status{{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:#16a34a}}
.dot{{width:7px;height:7px;background:#16a34a;border-radius:50%;animation:pulse 1.5s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}}
.card{{background:#fff;border-radius:12px;padding:14px;border:1px solid #e5e5e5}}
.card-label{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#888;margin-bottom:6px}}
.card-val{{font-size:22px;font-weight:700;font-family:'Courier New',monospace}}
.card-sub{{font-size:11px;color:#888;margin-top:3px}}
.sec-title{{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:#888;margin:20px 0 8px;display:flex;align-items:center;gap:8px}}
.sec-title::after{{content:'';flex:1;height:1px;background:#e5e5e5}}
.card.pos-card{{border-left:3px solid #16a34a;padding:12px}}
.row{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px}}
.mt8{{margin-top:8px}}.mt4{{margin-top:4px}}
.badge{{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px}}
.long{{background:rgba(22,163,74,.1);color:#16a34a}}
.short{{background:rgba(239,68,68,.1);color:#ef4444}}
.score-badge{{font-size:11px;color:#ca8a04;margin-left:auto}}
.tag{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px}}
.tag-blocked{{background:#fee2e2;color:#dc2626}}
.tag-score{{background:#fef3c7;color:#d97706}}
.tag-signal{{background:#dcfce7;color:#16a34a}}
.tag-long{{background:rgba(22,163,74,.1);color:#16a34a}}
.tag-short{{background:rgba(239,68,68,.1);color:#ef4444}}
.log-row{{display:flex;align-items:center;gap:8px;padding:9px 0;border-bottom:1px solid #f0f0f0;flex-wrap:wrap}}
.log-row:last-child{{border-bottom:none}}
.sym{{font-weight:700;font-size:13px;min-width:90px}}
.age{{font-size:11px;color:#aaa;margin-left:auto}}
.empty{{text-align:center;padding:20px;color:#aaa;font-size:13px}}
.sys-row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:13px}}
.sys-row span:first-child{{color:#888}}
.sys-row span:last-child{{font-weight:600}}
.refresh{{text-align:center;margin-top:20px;font-size:11px;color:#aaa}}
</style>
</head><body>

<div class="header">
  <div class="logo">APEX <span>Pro</span></div>
  <div class="status"><span class="dot"></span>{state["status"].upper()}</div>
</div>

<div class="grid">
  <div class="card">
    <div class="card-label">Total P&L</div>
    <div class="card-val" style="color:{pnl_col(pnl)}">{'+' if pnl>=0 else ''}${fmt(pnl)}</div>
    <div class="card-sub">{total} trades closed</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-val" style="color:#ca8a04">{wr}</div>
    <div class="card-sub">{wins}W / {losses}L</div>
  </div>
  <div class="card">
    <div class="card-label">Avg Win</div>
    <div class="card-val" style="color:#22c55e">+${fmt(avg_win)}</div>
    <div class="card-sub">per trade</div>
  </div>
  <div class="card">
    <div class="card-label">Avg Loss</div>
    <div class="card-val" style="color:#ef4444">-${fmt(abs(avg_loss))}</div>
    <div class="card-sub">per trade</div>
  </div>
</div>

<div class="card">
  <div class="sys-row"><span>Balance</span><span>${fmt(bal,2)} USDT</span></div>
  <div class="sys-row"><span>Open Positions</span><span>{len(state['positions'])} / {MAX_POS}</span></div>
  <div class="sys-row"><span>Min Score</span><span style="color:#16a34a">{MIN_SCORE} · STRONG</span></div>
  <div class="sys-row"><span>Trend Blocks</span><span>{state['trend_blocks']}</span></div>
  <div class="sys-row"><span>Score Blocks</span><span>{state['score_blocks']}</span></div>
  <div class="sys-row"><span>Last Scan</span><span>{age}s ago</span></div>
</div>

<div class="sec-title">Open Positions</div>
{pos_html}

<div class="sec-title">Scan Log</div>
<div class="card">{log_html}</div>

<div class="sec-title">Recent Trades</div>
<div class="card">{trades_html}</div>

<div class="refresh">Auto-refreshes every 20s</div>
<meta http-equiv="refresh" content="20">
</body></html>"""
    return HTMLResponse(html)

if __name__=="__main__":
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT",8000)))
