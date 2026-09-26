
import os, math, time, json
from datetime import date, timedelta, datetime
from flask import Flask, render_template, request, jsonify
import requests

app = Flask(__name__)
BASE = "https://api.massive.com"
API_KEY = os.getenv("MASSIVE_API_KEY", "").strip()
MIN_CAP = 1_000_000_000
ALLOWED_EXCHANGES = {"XNAS", "XNYS"}  # Nasdaq, NYSE MICs

def api(path, params=None):
    if not API_KEY:
        raise RuntimeError("MASSIVE_API_KEY non configurata sul server.")
    p = dict(params or {})
    p["apiKey"] = API_KEY
    r = requests.get(BASE + path, params=p, timeout=30)
    if r.status_code == 403:
        raise RuntimeError("Il tuo piano Massive non include questo endpoint.")
    if r.status_code == 429:
        raise RuntimeError("Limite API Massive raggiunto. Riprova tra poco.")
    r.raise_for_status()
    return r.json()

def ticker_details(ticker):
    return api(f"/v3/reference/tickers/{ticker.upper()}").get("results", {})

def eligible(d):
    return (
        d.get("active", True)
        and d.get("market") == "stocks"
        and d.get("primary_exchange") in ALLOWED_EXCHANGES
        and (d.get("market_cap") or 0) >= MIN_CAP
    )

def aggs(ticker, days=365):
    end = date.today()
    start = end - timedelta(days=days)
    data = api(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
               {"adjusted":"true","sort":"asc","limit":5000})
    return data.get("results", [])

def all_chain(ticker):
    # Follow Massive pagination while preserving auth.
    url = f"{BASE}/v3/snapshot/options/{ticker}"
    params = {"limit":250, "apiKey":API_KEY}
    out = []
    for _ in range(20):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 403:
            raise RuntimeError("Il piano Massive attuale non include Option Chain Snapshot.")
        if r.status_code == 429:
            raise RuntimeError("Limite API Massive raggiunto.")
        r.raise_for_status()
        j = r.json()
        out.extend(j.get("results", []))
        nxt = j.get("next_url")
        if not nxt: break
        url = nxt
        params = {"apiKey":API_KEY}
    return out

def expirations(chain):
    vals = set()
    for x in chain:
        e = (x.get("details") or {}).get("expiration_date")
        if e: vals.add(e)
    return sorted(vals)

def pick_delta(chain, expiration, contract_type, target_abs=.25):
    candidates = []
    for x in chain:
        d = x.get("details") or {}
        if d.get("expiration_date") != expiration or d.get("contract_type") != contract_type:
            continue
        iv = x.get("implied_volatility")
        delta = (x.get("greeks") or {}).get("delta")
        if iv is None or delta is None: continue
        candidates.append((abs(abs(delta)-target_abs), x))
    return min(candidates, key=lambda z:z[0])[1] if candidates else None

def pick_atm(chain, expiration, spot):
    candidates=[]
    for x in chain:
        d=x.get("details") or {}
        if d.get("expiration_date") != expiration: continue
        iv=x.get("implied_volatility"); k=d.get("strike_price")
        if iv is None or k is None: continue
        candidates.append((abs(k-spot),x))
    return min(candidates,key=lambda z:z[0])[1] if candidates else None

def option_metrics(chain, expiration):
    rows=[x for x in chain if (x.get("details") or {}).get("expiration_date")==expiration]
    if not rows: raise RuntimeError("Nessun contratto per questa scadenza.")
    spot=None
    for x in rows:
        spot=(x.get("underlying_asset") or {}).get("price")
        if spot: break
    if not spot:
        raise RuntimeError("Prezzo sottostante non disponibile nello snapshot.")
    put=pick_delta(rows, expiration, "put")
    call=pick_delta(rows, expiration, "call")
    atm=pick_atm(rows, expiration, spot)
    if not put or not call or not atm:
        raise RuntimeError("IV/Greeks insufficienti per calcolare le bande.")
    piv=float(put["implied_volatility"]); civ=float(call["implied_volatility"])
    aiv=float(atm["implied_volatility"])
    exp=datetime.strptime(expiration,"%Y-%m-%d").date()
    dte=max((exp-date.today()).days,1)
    down=piv*math.sqrt(dte/365)
    up=civ*math.sqrt(dte/365)
    # Lognormal-style multiplicative levels; asymmetric by IV wing.
    bands={
        "lower2": spot*math.exp(-2*down), "lower1": spot*math.exp(-down),
        "spot":spot, "upper1":spot*math.exp(up), "upper2":spot*math.exp(2*up)
    }
    return {
        "expiration":expiration,"dte":dte,"spot":spot,
        "put_iv":piv,"atm_iv":aiv,"call_iv":civ,
        "skew":piv-civ,"bands":bands,
        "put_strike":(put.get("details") or {}).get("strike_price"),
        "call_strike":(call.get("details") or {}).get("strike_price"),
        "put_delta":(put.get("greeks") or {}).get("delta"),
        "call_delta":(call.get("greeks") or {}).get("delta"),
    }

@app.route("/")
def home():
    return render_template("index.html")

@app.get("/api/ticker/<ticker>")
def ticker(ticker):
    try:
        d=ticker_details(ticker)
        if not d: return jsonify(error="Ticker non trovato."),404
        if not eligible(d):
            return jsonify(error="Il titolo non rientra nell'universo: NYSE/Nasdaq e market cap ≥ $1 mld."),400
        bars=aggs(ticker)
        chain=all_chain(ticker.upper())
        exps=expirations(chain)
        return jsonify(
            ticker=ticker.upper(), name=d.get("name"), market_cap=d.get("market_cap"),
            exchange=d.get("primary_exchange"), bars=bars, expirations=exps
        )
    except Exception as e:
        return jsonify(error=str(e)),500

@app.get("/api/options/<ticker>")
def options(ticker):
    try:
        exp=request.args.get("expiration")
        if not exp: return jsonify(error="Scadenza mancante."),400
        d=ticker_details(ticker)
        if not eligible(d): return jsonify(error="Titolo fuori universo."),400
        chain=all_chain(ticker.upper())
        return jsonify(option_metrics(chain, exp))
    except Exception as e:
        return jsonify(error=str(e)),500

@app.get("/api/search")
def search():
    # Massive reference ticker search; each candidate is verified for cap/exchange.
    q=request.args.get("q","").strip()
    if not q: return jsonify(results=[])
    try:
        j=api("/v3/reference/tickers",{"market":"stocks","active":"true","search":q,"limit":10})
        results=[]
        for t in j.get("results",[])[:10]:
            try:
                d=ticker_details(t.get("ticker",""))
                if eligible(d):
                    results.append({"ticker":d.get("ticker"),"name":d.get("name"),"market_cap":d.get("market_cap")})
            except Exception:
                pass
        return jsonify(results=results)
    except Exception as e:
        return jsonify(error=str(e)),500

@app.get("/health")
def health():
    return jsonify(ok=True, api_key_configured=bool(API_KEY))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")), debug=True)
