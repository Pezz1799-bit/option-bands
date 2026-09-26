
import os, math, time, sqlite3
from datetime import date, timedelta, datetime
from flask import Flask, render_template, request, jsonify
import requests

app=Flask(__name__)
BASE="https://api.massive.com"
API_KEY=os.getenv("MASSIVE_API_KEY","").strip()
MIN_CAP=1_000_000_000
ALLOWED_EXCHANGES={"XNAS","XNYS"}
RISK_FREE=0.04
CACHE={}
CACHE_TTL=3600
DB_PATH=os.getenv("DB_PATH","/tmp/option_bands.db")

def db():
    con=sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS band_history (
        day TEXT NOT NULL, ticker TEXT NOT NULL, expiration TEXT NOT NULL,
        spot REAL, put_iv REAL, atm_iv REAL, call_iv REAL, skew REAL,
        lower2 REAL, lower1 REAL, upper1 REAL, upper2 REAL, iv_position REAL,
        PRIMARY KEY(day,ticker,expiration)
    )""")
    return con

def iv_position(spot, reference_spot, put_iv, call_iv, T):
    if not reference_spot or reference_spot<=0 or T<=0: return 0.0
    move=math.log(spot/reference_spot)
    sigma=(call_iv if move>=0 else put_iv)*math.sqrt(T)
    return move/sigma if sigma>0 else 0.0

def save_history(ticker, data):
    con=db()
    b=data["bands"]
    con.execute("""INSERT OR REPLACE INTO band_history
      (day,ticker,expiration,spot,put_iv,atm_iv,call_iv,skew,lower2,lower1,upper1,upper2,iv_position)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (date.today().isoformat(),ticker,data["expiration"],data["spot"],data["put_iv"],data["atm_iv"],
       data["call_iv"],data["skew"],b["lower2"],b["lower1"],b["upper1"],b["upper2"],data["iv_position"]))
    con.commit(); con.close()


def cached(key, fn):
    now=time.time()
    if key in CACHE and now-CACHE[key][0] < CACHE_TTL: return CACHE[key][1]
    v=fn(); CACHE[key]=(now,v); return v

def api(path,params=None):
    if not API_KEY: raise RuntimeError("MASSIVE_API_KEY non configurata.")
    p=dict(params or {}); p["apiKey"]=API_KEY
    r=requests.get(BASE+path,params=p,timeout=30)
    if r.status_code==429: raise RuntimeError("Limite gratuito Massive: 5 chiamate/minuto. Attendi circa un minuto.")
    if r.status_code==403: raise RuntimeError("Questo endpoint non è incluso nel piano Massive Basic.")
    r.raise_for_status(); return r.json()

def details(t):
    return cached(("det",t),lambda:api(f"/v3/reference/tickers/{t}").get("results",{}))

def eligible(d):
    return d.get("market")=="stocks" and d.get("primary_exchange") in ALLOWED_EXCHANGES and (d.get("market_cap") or 0)>=MIN_CAP

def bars(t,days=365):
    def load():
        e=date.today(); s=e-timedelta(days=days)
        return api(f"/v2/aggs/ticker/{t}/range/1/day/{s}/{e}",{"adjusted":"true","sort":"asc","limit":5000}).get("results",[])
    return cached(("bars",t,days),load)

def contracts(t):
    # One reference request. Limit 1000 is normally enough for near expiries;
    # UI uses expiries present in this first page to keep Basic API usage low.
    def load():
        j=api("/v3/reference/options/contracts",{"underlying_ticker":t,"expired":"false","limit":1000,"sort":"expiration_date","order":"asc"})
        return j.get("results",[])
    return cached(("contracts",t),load)

def option_prev(opticker):
    def load():
        j=api(f"/v2/aggs/ticker/{opticker}/prev",{"adjusted":"true"})
        a=j.get("results",[])
        return a[0] if a else None
    return cached(("oprev",opticker),load)

def norm_cdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))

def bs_price(S,K,T,r,sigma,is_call):
    if T<=0 or sigma<=0: return max(0,S-K) if is_call else max(0,K-S)
    d1=(math.log(S/K)+(r+0.5*sigma*sigma)*T)/(sigma*math.sqrt(T)); d2=d1-sigma*math.sqrt(T)
    if is_call: return S*norm_cdf(d1)-K*math.exp(-r*T)*norm_cdf(d2)
    return K*math.exp(-r*T)*norm_cdf(-d2)-S*norm_cdf(-d1)

def implied_vol(price,S,K,T,r,is_call):
    intrinsic=max(0,S-K*math.exp(-r*T)) if is_call else max(0,K*math.exp(-r*T)-S)
    if price<=intrinsic or price<=0: return None
    lo,hi=0.01,5.0
    if bs_price(S,K,T,r,hi,is_call)<price: return None
    for _ in range(70):
        mid=(lo+hi)/2
        if bs_price(S,K,T,r,mid,is_call)>price: hi=mid
        else: lo=mid
    return (lo+hi)/2

def delta(S,K,T,r,sigma,is_call):
    d1=(math.log(S/K)+(r+0.5*sigma*sigma)*T)/(sigma*math.sqrt(T))
    return norm_cdf(d1) if is_call else norm_cdf(d1)-1

def expirations(cs):
    return sorted({c.get("expiration_date") for c in cs if c.get("expiration_date")})

def choose_candidates(cs,exp,S):
    same=[c for c in cs if c.get("expiration_date")==exp and c.get("strike_price")]
    calls=sorted([c for c in same if c.get("contract_type")=="call"],key=lambda c:abs(c["strike_price"]-S))
    puts=sorted([c for c in same if c.get("contract_type")=="put"],key=lambda c:abs(c["strike_price"]-S))
    # Basic = 5 calls/min. Use only 3 option-price calls:
    # one ATM-ish call + one downside put + one upside call.
    atm=min(same,key=lambda c:abs(c["strike_price"]-S)) if same else None
    put_pool=[c for c in puts if c["strike_price"]<S] or puts
    call_pool=[c for c in calls if c["strike_price"]>S] or calls
    # Approximate 25-delta wings by strikes ~8% away, then solve IV/delta from EOD.
    p=min(put_pool,key=lambda c:abs(c["strike_price"]-S*0.92)) if put_pool else None
    q=min(call_pool,key=lambda c:abs(c["strike_price"]-S*1.08)) if call_pool else None
    return atm,p,q

def metrics(t,exp):
    bs=bars(t,30)
    if not bs: raise RuntimeError("Prezzo del sottostante non disponibile.")
    S=float(bs[-1]["c"])
    cs=contracts(t)
    atm,p,c=choose_candidates(cs,exp,S)
    if not atm or not p or not c: raise RuntimeError("Contratti insufficienti per questa scadenza.")
    ed=datetime.strptime(exp,"%Y-%m-%d").date(); dte=max((ed-date.today()).days,1); T=dte/365
    rows=[]
    for x in [atm,p,c]:
        b=option_prev(x["ticker"])
        if not b: rows.append(None); continue
        price=float(b["c"]); K=float(x["strike_price"]); is_call=x["contract_type"]=="call"
        iv=implied_vol(price,S,K,T,RISK_FREE,is_call)
        rows.append({"contract":x["ticker"],"strike":K,"type":x["contract_type"],"price":price,"iv":iv,
                     "delta":delta(S,K,T,RISK_FREE,iv,is_call) if iv else None})
    a,pr,ca=rows
    if not a or not pr or not ca or not pr["iv"] or not ca["iv"]:
        raise RuntimeError("Prezzi EOD insufficienti per calcolare IV su questa scadenza.")
    aiv=a["iv"] if a and a["iv"] else (pr["iv"]+ca["iv"])/2
    piv,civ=pr["iv"],ca["iv"]
    down=piv*math.sqrt(T); up=civ*math.sqrt(T)
    bands={"lower2":S*math.exp(-2*down),"lower1":S*math.exp(-down),"spot":S,"upper1":S*math.exp(up),"upper2":S*math.exp(2*up)}
    # Reference is today's EOD spot for the newly calculated surface. The live position is 0 at creation;
    # future stored observations can be compared with this reference.
    pos=iv_position(S,S,piv,civ,T)
    result={"expiration":exp,"dte":dte,"spot":S,"reference_spot":S,"put_iv":piv,"atm_iv":aiv,"call_iv":civ,
            "skew":piv-civ,"iv_position":pos,"bands":bands,"put_strike":pr["strike"],"call_strike":ca["strike"],
            "put_delta":pr["delta"],"call_delta":ca["delta"],"mode":"Massive Basic EOD · IV calcolata localmente",
            "note":"Le wing sono selezionate vicino a ±8% dal prezzo per rispettare il limite di 5 API call/min; il delta risultante è mostrato."}
    save_history(t, result)
    return result

@app.route("/")
def home(): return render_template("index.html")

@app.get("/api/ticker/<t>")
def ticker(t):
    try:
        t=t.upper(); d=details(t)
        if not d: return jsonify(error="Ticker non trovato."),404
        if not eligible(d): return jsonify(error="Fuori universo: NYSE/Nasdaq e market cap ≥ $1 mld."),400
        b=bars(t); cs=contracts(t)
        return jsonify(ticker=t,name=d.get("name"),market_cap=d.get("market_cap"),exchange=d.get("primary_exchange"),bars=b,expirations=expirations(cs))
    except Exception as e: return jsonify(error=str(e)),500

@app.get("/api/options/<t>")
def opts(t):
    try:
        exp=request.args.get("expiration")
        if not exp:return jsonify(error="Scadenza mancante."),400
        return jsonify(metrics(t.upper(),exp))
    except Exception as e:return jsonify(error=str(e)),500

@app.get("/api/history/<t>")
def history(t):
    con=db(); con.row_factory=sqlite3.Row
    rows=con.execute("SELECT * FROM band_history WHERE ticker=? ORDER BY day DESC LIMIT 365",(t.upper(),)).fetchall()
    con.close()
    return jsonify(results=[dict(x) for x in rows])

@app.get("/api/screener")
def screener():
    con=db(); con.row_factory=sqlite3.Row
    rows=con.execute("""SELECT h.* FROM band_history h JOIN
      (SELECT ticker,MAX(day) day FROM band_history GROUP BY ticker) x
      ON h.ticker=x.ticker AND h.day=x.day ORDER BY ABS(h.iv_position) DESC""").fetchall()
    con.close()
    return jsonify(results=[dict(x) for x in rows])

@app.get("/health")
def health(): return jsonify(ok=True,api_key_configured=bool(API_KEY),mode="basic-free")

if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=True)
