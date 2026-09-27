import os, math, time, sqlite3
from datetime import date, timedelta, datetime
from flask import Flask, render_template, request, jsonify
import requests

app = Flask(__name__)
BASE = "https://api.massive.com"
API_KEY = os.getenv("MASSIVE_API_KEY", "").strip()
MIN_CAP = 1_000_000_000
ALLOWED_EXCHANGES = {"XNAS", "XNYS"}
CACHE = {}
CACHE_TTL = 900
DB_PATH = os.getenv("DB_PATH", "/tmp/option_bands.db")


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS band_history (
        day TEXT NOT NULL, ticker TEXT NOT NULL, expiration TEXT NOT NULL,
        spot REAL, put_iv REAL, atm_iv REAL, call_iv REAL, skew REAL,
        lower2 REAL, lower1 REAL, upper1 REAL, upper2 REAL, iv_position REAL,
        PRIMARY KEY(day,ticker,expiration)
    )""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(band_history)").fetchall()}
    for name, typ in [("reference_spot","REAL"),("current_spot","REAL"),("put_delta","REAL"),("call_delta","REAL"),("put_strike","REAL"),("call_strike","REAL")]:
        if name not in cols:
            con.execute(f"ALTER TABLE band_history ADD COLUMN {name} {typ}")
    con.commit()
    return con


def cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    if key in CACHE and now - CACHE[key][0] < ttl:
        return CACHE[key][1]
    value = fn()
    CACHE[key] = (now, value)
    return value


def api(path_or_url, params=None):
    if not API_KEY:
        raise RuntimeError("MASSIVE_API_KEY non configurata.")
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    p = dict(params or {})
    p["apiKey"] = API_KEY
    r = requests.get(url, params=p, timeout=40)
    if r.status_code == 429:
        raise RuntimeError("Limite API Massive raggiunto. Riprova tra poco.")
    if r.status_code == 403:
        raise RuntimeError("Massive ha rifiutato l'endpoint. Verifica che Options Starter sia attivo sulla stessa API key.")
    r.raise_for_status()
    return r.json()


def details(t):
    return cached(("det", t), lambda: api(f"/v3/reference/tickers/{t}").get("results", {}), 3600)


def eligible(d):
    return d.get("market") == "stocks" and d.get("primary_exchange") in ALLOWED_EXCHANGES and (d.get("market_cap") or 0) >= MIN_CAP


def bars(t, days=365):
    def load():
        end = date.today(); start = end - timedelta(days=days)
        return api(f"/v2/aggs/ticker/{t}/range/1/day/{start}/{end}", {"adjusted":"true","sort":"asc","limit":5000}).get("results", [])
    return cached(("bars", t, days), load, 900)


def chain_page(t, expiration):
    # Options Starter: one chain snapshot, already containing IV + Greeks.
    params = {"expiration_date": expiration, "limit": 250, "sort":"strike_price", "order":"asc"}
    j = api(f"/v3/snapshot/options/{t}", params)
    rows = list(j.get("results", []))
    # Follow pagination so 25-delta wings are not missed on large chains.
    next_url = j.get("next_url")
    pages = 1
    while next_url and pages < 8:
        j = api(next_url)
        rows.extend(j.get("results", []))
        next_url = j.get("next_url")
        pages += 1
    return rows


def chain(t, expiration):
    return cached(("chain", t, expiration), lambda: chain_page(t, expiration), 300)


def expirations_from_contracts(t):
    # Reference endpoint only to populate the expiration selector.
    def load():
        j = api("/v3/reference/options/contracts", {"underlying_ticker":t,"expired":"false","limit":1000,"sort":"expiration_date","order":"asc"})
        return sorted({x.get("expiration_date") for x in j.get("results", []) if x.get("expiration_date")})
    return cached(("exp", t), load, 3600)


def valid_option(x):
    d = x.get("details") or {}; g = x.get("greeks") or {}
    try:
        iv = float(x.get("implied_volatility")); delta = float(g.get("delta")); strike = float(d.get("strike_price"))
        return iv > 0 and math.isfinite(iv) and math.isfinite(delta) and strike > 0
    except (TypeError, ValueError):
        return False


def pick_25d(rows, kind):
    target = -0.25 if kind == "put" else 0.25
    pool = [x for x in rows if valid_option(x) and (x.get("details") or {}).get("contract_type") == kind]
    if not pool:
        return None
    # Prefer contracts with some OI/volume, but never discard a valid 25d candidate solely for liquidity.
    return min(pool, key=lambda x: (abs(float((x.get("greeks") or {}).get("delta")) - target), 0 if (x.get("open_interest") or 0) > 0 else 1))


def pick_atm(rows, spot):
    pool = [x for x in rows if valid_option(x)]
    if not pool:
        return None
    near = sorted(pool, key=lambda x: abs(float((x.get("details") or {}).get("strike_price")) - spot))[:6]
    ivs = [float(x["implied_volatility"]) for x in near]
    return sum(ivs) / len(ivs) if ivs else None


def sigma_position(current_spot, reference_spot, put_iv, call_iv, T):
    if not current_spot or not reference_spot or current_spot <= 0 or reference_spot <= 0 or T <= 0:
        return 0.0
    move = math.log(current_spot / reference_spot)
    sigma = (call_iv if move >= 0 else put_iv) * math.sqrt(T)
    return move / sigma if sigma > 0 else 0.0


def existing_today(ticker, expiration):
    con = db(); con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM band_history WHERE day=? AND ticker=? AND expiration=?", (date.today().isoformat(), ticker, expiration)).fetchone()
    con.close()
    return dict(row) if row else None


def save_or_update_history(ticker, result):
    con = db()
    old = con.execute("SELECT reference_spot,put_iv,call_iv FROM band_history WHERE day=? AND ticker=? AND expiration=?", (date.today().isoformat(), ticker, result["expiration"])).fetchone()
    if old:
        ref, piv, civ = old
        T = max((datetime.strptime(result["expiration"], "%Y-%m-%d").date() - date.today()).days, 1) / 365.0
        pos = sigma_position(result["spot"], ref or result["spot"], piv or result["put_iv"], civ or result["call_iv"], T)
        con.execute("UPDATE band_history SET current_spot=?,iv_position=? WHERE day=? AND ticker=? AND expiration=?", (result["spot"], pos, date.today().isoformat(), ticker, result["expiration"]))
        result["iv_position"] = pos
        result["reference_spot"] = ref or result["spot"]
    else:
        b = result["bands"]
        con.execute("""INSERT INTO band_history
          (day,ticker,expiration,spot,put_iv,atm_iv,call_iv,skew,lower2,lower1,upper1,upper2,iv_position,reference_spot,current_spot,put_delta,call_delta,put_strike,call_strike)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (date.today().isoformat(), ticker, result["expiration"], result["spot"], result["put_iv"], result["atm_iv"], result["call_iv"], result["skew"],
           b["lower2"], b["lower1"], b["upper1"], b["upper2"], 0.0, result["spot"], result["spot"], result["put_delta"], result["call_delta"], result["put_strike"], result["call_strike"]))
        result["iv_position"] = 0.0
        result["reference_spot"] = result["spot"]
    con.commit(); con.close()


def metrics(t, exp):
    bs = bars(t, 30)
    if not bs:
        raise RuntimeError("Prezzo del sottostante non disponibile.")
    S = float(bs[-1]["c"])
    rows = chain(t, exp)
    if not rows:
        raise RuntimeError("Option Chain Snapshot vuota per questa scadenza.")
    put = pick_25d(rows, "put"); call = pick_25d(rows, "call")
    if not put or not call:
        raise RuntimeError("Non trovo Put/Call con IV e delta validi per questa scadenza.")
    pd = put["details"]; pg = put["greeks"]; cd = call["details"]; cg = call["greeks"]
    piv = float(put["implied_volatility"]); civ = float(call["implied_volatility"]); aiv = pick_atm(rows, S) or (piv + civ) / 2
    ed = datetime.strptime(exp, "%Y-%m-%d").date(); dte = max((ed - date.today()).days, 1); T = dte / 365.0
    down = piv * math.sqrt(T); up = civ * math.sqrt(T)
    bands = {"lower2":S*math.exp(-2*down),"lower1":S*math.exp(-down),"spot":S,"upper1":S*math.exp(up),"upper2":S*math.exp(2*up)}
    result = {"expiration":exp,"dte":dte,"spot":S,"reference_spot":S,"put_iv":piv,"atm_iv":aiv,"call_iv":civ,"skew":piv-civ,
              "iv_position":0.0,"bands":bands,"put_strike":float(pd["strike_price"]),"call_strike":float(cd["strike_price"]),
              "put_delta":float(pg["delta"]),"call_delta":float(cg["delta"]),"put_contract":pd.get("ticker"),"call_contract":cd.get("ticker"),
              "mode":"Massive Options Starter · Snapshot · 25Δ reali",
              "note":"Put e Call selezionate come contratti con delta più vicino rispettivamente a −0,25 e +0,25."}
    save_or_update_history(t, result)
    return result


@app.route("/")
def home(): return render_template("index.html")

@app.get("/api/ticker/<t>")
def ticker(t):
    try:
        t = t.upper(); d = details(t)
        if not d: return jsonify(error="Ticker non trovato."), 404
        if not eligible(d): return jsonify(error="Fuori universo: NYSE/Nasdaq e market cap ≥ $1 mld."), 400
        b = bars(t); exps = expirations_from_contracts(t)
        return jsonify(ticker=t,name=d.get("name"),market_cap=d.get("market_cap"),exchange=d.get("primary_exchange"),bars=b,expirations=exps)
    except Exception as e: return jsonify(error=str(e)), 500

@app.get("/api/options/<t>")
def opts(t):
    try:
        exp = request.args.get("expiration")
        if not exp: return jsonify(error="Scadenza mancante."), 400
        return jsonify(metrics(t.upper(), exp))
    except Exception as e: return jsonify(error=str(e)), 500

@app.get("/api/history/<t>")
def history(t):
    con = db(); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM band_history WHERE ticker=? ORDER BY day DESC,expiration ASC LIMIT 365", (t.upper(),)).fetchall(); con.close()
    return jsonify(results=[dict(x) for x in rows])

@app.get("/api/screener")
def screener():
    con = db(); con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT h.* FROM band_history h JOIN
      (SELECT ticker,MAX(day) day FROM band_history GROUP BY ticker) x
      ON h.ticker=x.ticker AND h.day=x.day ORDER BY h.iv_position ASC""").fetchall(); con.close()
    return jsonify(results=[dict(x) for x in rows])

@app.get("/health")
def health(): return jsonify(ok=True,api_key_configured=bool(API_KEY),mode="options-starter-snapshot-25d")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
