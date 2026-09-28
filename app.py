import os, math, time, sqlite3, threading
from datetime import date, timedelta, datetime
from flask import Flask, render_template, request, jsonify
import requests

app=Flask(__name__); BASE='https://api.massive.com'; API_KEY=os.getenv('MASSIVE_API_KEY','').strip(); DB_PATH=os.getenv('DB_PATH','/tmp/option_bands.db'); CACHE={}; CACHE_TTL=900
SCAN={'running':False,'done':0,'total':0,'ok':0,'errors':0,'ticker':'','started':None,'finished':None,'message':'Pronto'}; LOCK=threading.Lock()

def db():
    con=sqlite3.connect(DB_PATH,check_same_thread=False); con.execute('''CREATE TABLE IF NOT EXISTS band_history(day TEXT NOT NULL,ticker TEXT NOT NULL,expiration TEXT NOT NULL,spot REAL,put_iv REAL,atm_iv REAL,call_iv REAL,skew REAL,lower2 REAL,lower1 REAL,upper1 REAL,upper2 REAL,iv_position REAL,reference_spot REAL,current_spot REAL,put_delta REAL,call_delta REAL,put_strike REAL,call_strike REAL,PRIMARY KEY(day,ticker,expiration))'''); con.execute('''CREATE TABLE IF NOT EXISTS scan_results(day TEXT NOT NULL,ticker TEXT NOT NULL,expiration TEXT,spot REAL,put_iv REAL,call_iv REAL,skew REAL,lower2 REAL,lower1 REAL,upper1 REAL,upper2 REAL,iv_position REAL,zone TEXT,call_wall_below_score REAL,call_wall_below_strike REAL,call_wall_below_oi REAL,put_wall_above_score REAL,put_wall_above_strike REAL,put_wall_above_oi REAL,put_call_ratio REAL,gex_estimate REAL,PRIMARY KEY(day,ticker))'''); 
    # Lightweight migration for databases created by previous versions.
    cols={r[1] for r in con.execute('PRAGMA table_info(scan_results)').fetchall()}
    for name in ['call_wall_below_score','call_wall_below_strike','call_wall_below_oi','put_wall_above_score','put_wall_above_strike','put_wall_above_oi','put_call_ratio','gex_estimate','call_wall_below_volume_score','call_wall_below_volume_strike','call_wall_below_volume','put_wall_above_volume_score','put_wall_above_volume_strike','put_wall_above_volume','put_call_ratio_volume']:
        if name not in cols: con.execute(f'ALTER TABLE scan_results ADD COLUMN {name} REAL')
    con.commit(); return con

def cached(key,fn,ttl=CACHE_TTL):
    now=time.time()
    if key in CACHE and now-CACHE[key][0]<ttl:return CACHE[key][1]
    v=fn(); CACHE[key]=(now,v); return v

def api(path_or_url,params=None):
    if not API_KEY: raise RuntimeError('MASSIVE_API_KEY non configurata.')
    url=path_or_url if path_or_url.startswith('http') else BASE+path_or_url; p=dict(params or {}); p['apiKey']=API_KEY; r=requests.get(url,params=p,timeout=45)
    if r.status_code==429: raise RuntimeError('Limite API Massive raggiunto.')
    if r.status_code==403: raise RuntimeError('Endpoint non incluso nel piano Massive attivo.')
    r.raise_for_status(); return r.json()

def bars(t,days=365):
    def load():
        end=date.today(); start=end-timedelta(days=days); return api(f'/v2/aggs/ticker/{t}/range/1/day/{start}/{end}',{'adjusted':'true','sort':'asc','limit':5000}).get('results',[])
    return cached(('bars',t,days),load,900)

def details(t): return cached(('det',t),lambda:api(f'/v3/reference/tickers/{t}').get('results',{}),3600)
def expirations(t):
    def load():
        j=api('/v3/reference/options/contracts',{'underlying_ticker':t,'expired':'false','limit':1000,'sort':'expiration_date','order':'asc'}); return sorted({x.get('expiration_date') for x in j.get('results',[]) if x.get('expiration_date')})
    return cached(('exp',t),load,3600)
def target_exp(exps,target=30):
    today=date.today(); valid=[]
    for e in exps:
        try:
            d=(datetime.strptime(e,'%Y-%m-%d').date()-today).days
            if d>0: valid.append((abs(d-target),e))
        except: pass
    return min(valid)[1] if valid else None

def chain(t,exp):
    def load():
        j=api(f'/v3/snapshot/options/{t}',{'expiration_date':exp,'limit':250,'sort':'strike_price','order':'asc'}); rows=list(j.get('results',[])); nxt=j.get('next_url'); n=1
        while nxt and n<8: j=api(nxt); rows+=j.get('results',[]); nxt=j.get('next_url'); n+=1
        return rows
    return cached(('chain',t,exp),load,300)
def valid(x):
    try: return float(x.get('implied_volatility'))>0 and math.isfinite(float((x.get('greeks') or {}).get('delta'))) and float((x.get('details') or {}).get('strike_price'))>0
    except:return False
def pick(rows,kind):
    target=-.25 if kind=='put' else .25; p=[x for x in rows if valid(x) and (x.get('details') or {}).get('contract_type')==kind]; return min(p,key=lambda x:abs(float((x.get('greeks') or {}).get('delta'))-target)) if p else None
def atm_iv(rows,S):
    p=sorted([x for x in rows if valid(x)],key=lambda x:abs(float((x.get('details') or {}).get('strike_price'))-S))[:6]; return sum(float(x['implied_volatility']) for x in p)/len(p) if p else None

def option_oi(x):
    try: return max(0.0,float(x.get('open_interest') or 0))
    except: return 0.0

def option_volume(x):
    try: return max(0.0,float((x.get('day') or {}).get('volume') or 0))
    except: return 0.0

def wall_profile(rows,S,metric='oi'):
    key='volume' if metric=='volume' else 'oi'
    levels=[]
    for x in rows:
        d=x.get('details') or {}; kind=d.get('contract_type'); strike=d.get('strike_price')
        try: strike=float(strike)
        except: continue
        if kind not in {'call','put'} or strike<=0: continue
        value=option_volume(x) if metric=='volume' else option_oi(x)
        if value<=0: continue
        levels.append({'strike':strike,'type':kind,key:value})
    agg={}
    for x in levels:
        k=(x['strike'],x['type']); agg[k]=agg.get(k,0.0)+x[key]
    levels=[{'strike':k[0],'type':k[1],key:v} for k,v in agg.items()]
    maxv=max([x[key] for x in levels],default=1.0)
    for x in levels: x['relative']=100.0*x[key]/maxv if maxv else 0.0
    def wall(kind,side):
        pool=[x for x in levels if x['type']==kind and ((x['strike']<S) if side=='below' else (x['strike']>S))]
        if not pool:return {'score':0.0,'strike':None,key:0.0}
        vals=sorted(x[key] for x in pool); med=vals[len(vals)//2] if vals else 0.0
        best=max(pool,key=lambda x:x[key]); ratio=best[key]/med if med>0 else (1.0 if best[key]>0 else 0.0)
        share=best[key]/sum(x[key] for x in pool) if pool else 0.0
        score=min(100.0,100.0*(0.65*min(1.0,share*4.0)+0.35*min(1.0,ratio/5.0)))
        return {'score':score,'strike':best['strike'],key:best[key]}
    return levels,wall('call','below'),wall('put','above')

def chain_metrics(rows,S):
    call_oi=put_oi=call_volume=put_volume=0.0; call_gex=put_gex=0.0
    for x in rows:
        d=x.get('details') or {}; kind=d.get('contract_type')
        if kind not in {'call','put'}: continue
        oi=option_oi(x); vol=option_volume(x)
        if kind=='call': call_oi+=oi; call_volume+=vol
        else: put_oi+=oi; put_volume+=vol
        if oi<=0: continue
        try: gamma=max(0.0,float((x.get('greeks') or {}).get('gamma') or 0))
        except: gamma=0.0
        gex=gamma*oi*100.0*S*S*0.01
        if kind=='call': call_gex+=gex
        else: put_gex+=gex
    return {'call_oi':call_oi,'put_oi':put_oi,'put_call_ratio_oi':(put_oi/call_oi) if call_oi>0 else None,'call_volume':call_volume,'put_volume':put_volume,'put_call_ratio_volume':(put_volume/call_volume) if call_volume>0 else None,'call_gex':call_gex,'put_gex':-put_gex,'net_gex':call_gex-put_gex}

def calc(t,exp,save=True):
    bs=bars(t,30); S=float(bs[-1]['c']) if bs else 0
    if S<=0: raise RuntimeError('Prezzo non disponibile')
    rows=chain(t,exp); oi_levels,call_wall_oi,put_wall_oi=wall_profile(rows,S,'oi'); volume_levels,call_wall_volume,put_wall_volume=wall_profile(rows,S,'volume'); cm=chain_metrics(rows,S); put=pick(rows,'put'); call=pick(rows,'call')
    if not put or not call: raise RuntimeError('25Δ non disponibili')
    piv=float(put['implied_volatility']); civ=float(call['implied_volatility']); aiv=atm_iv(rows,S) or (piv+civ)/2; dte=max((datetime.strptime(exp,'%Y-%m-%d').date()-date.today()).days,1); T=dte/365; down=piv*math.sqrt(T); up=civ*math.sqrt(T); b={'lower2':S*math.exp(-2*down),'lower1':S*math.exp(-down),'upper1':S*math.exp(up),'upper2':S*math.exp(2*up)}
    # Position vs previous saved box: meaningful for screener and oscillator.
    con=db(); con.row_factory=sqlite3.Row; prev=con.execute('SELECT * FROM band_history WHERE ticker=? AND day<? ORDER BY day DESC LIMIT 1',(t,date.today().isoformat())).fetchone(); pos=0.0
    if prev and prev['reference_spot'] and S>0:
        move=math.log(S/prev['reference_spot']); oldT=max((datetime.strptime(prev['expiration'],'%Y-%m-%d').date()-date.today()).days,1)/365; sig=(prev['call_iv'] if move>=0 else prev['put_iv'])*math.sqrt(oldT); pos=move/sig if sig>0 else 0
    pd,pg=put['details'],put['greeks']; cd,cg=call['details'],call['greeks']; out={'expiration':exp,'dte':dte,'spot':S,'reference_spot':S,'put_iv':piv,'atm_iv':aiv,'call_iv':civ,'skew':piv-civ,'iv_position':pos,'bands':b,'put_strike':float(pd['strike_price']),'call_strike':float(cd['strike_price']),'put_delta':float(pg['delta']),'call_delta':float(cg['delta']),'mode':'Snapshot · 25Δ reali','option_profile_oi':oi_levels,'option_profile_volume':volume_levels,'call_wall_below_oi':call_wall_oi,'put_wall_above_oi':put_wall_oi,'call_wall_below_volume':call_wall_volume,'put_wall_above_volume':put_wall_volume,'put_call_ratio_oi':cm['put_call_ratio_oi'],'put_call_ratio_volume':cm['put_call_ratio_volume'],'call_oi_total':cm['call_oi'],'put_oi_total':cm['put_oi'],'call_volume_total':cm['call_volume'],'put_volume_total':cm['put_volume'],'gex_estimate':cm['net_gex'],'call_gex_estimate':cm['call_gex'],'put_gex_estimate':cm['put_gex']}
    if save:
        con.execute('''INSERT OR REPLACE INTO band_history(day,ticker,expiration,spot,put_iv,atm_iv,call_iv,skew,lower2,lower1,upper1,upper2,iv_position,reference_spot,current_spot,put_delta,call_delta,put_strike,call_strike) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(date.today().isoformat(),t,exp,S,piv,aiv,civ,piv-civ,b['lower2'],b['lower1'],b['upper1'],b['upper2'],pos,S,S,out['put_delta'],out['call_delta'],out['put_strike'],out['call_strike'])); con.commit()
    con.close(); return out

def universe(limit=0):
    rows=[]; url='/v3/reference/tickers'; params={'market':'stocks','active':'true','type':'CS','limit':1000,'sort':'ticker','order':'asc'}
    while url:
        j=api(url,params if not url.startswith('http') else None); params=None; rows += [x for x in j.get('results',[]) if x.get('primary_exchange') in {'XNAS','XNYS'}]; url=j.get('next_url')
        if limit and len(rows)>=limit: break
    return rows[:limit] if limit else rows

def zone(pos):
    if pos<=-2:return '≤ -2σ'
    if pos<=-1:return '-2σ / -1σ'
    if pos>=2:return '≥ +2σ'
    if pos>=1:return '+1σ / +2σ'
    return 'Dentro ±1σ'

def scan_worker(limit=0):
    try:
        uni=universe(limit); 
        with LOCK: SCAN.update(total=len(uni),done=0,ok=0,errors=0,message='Scansione in corso')
        for i,x in enumerate(uni,1):
            t=x['ticker'];
            with LOCK: SCAN.update(ticker=t,done=i-1)
            try:
                ex=expirations(t); e=target_exp(ex,30)
                if not e: raise RuntimeError('no expiry')
                m=calc(t,e,True); b=m['bands']; z=zone(m['iv_position']); con=db(); con.execute('''INSERT OR REPLACE INTO scan_results(day,ticker,expiration,spot,put_iv,call_iv,skew,lower2,lower1,upper1,upper2,iv_position,zone,call_wall_below_score,call_wall_below_strike,call_wall_below_oi,put_wall_above_score,put_wall_above_strike,put_wall_above_oi,put_call_ratio,gex_estimate,call_wall_below_volume_score,call_wall_below_volume_strike,call_wall_below_volume,put_wall_above_volume_score,put_wall_above_volume_strike,put_wall_above_volume,put_call_ratio_volume) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(date.today().isoformat(),t,e,m['spot'],m['put_iv'],m['call_iv'],m['skew'],b['lower2'],b['lower1'],b['upper1'],b['upper2'],m['iv_position'],z,m['call_wall_below_oi']['score'],m['call_wall_below_oi']['strike'],m['call_wall_below_oi']['oi'],m['put_wall_above_oi']['score'],m['put_wall_above_oi']['strike'],m['put_wall_above_oi']['oi'],m['put_call_ratio_oi'],m['gex_estimate'],m['call_wall_below_volume']['score'],m['call_wall_below_volume']['strike'],m['call_wall_below_volume']['volume'],m['put_wall_above_volume']['score'],m['put_wall_above_volume']['strike'],m['put_wall_above_volume']['volume'],m['put_call_ratio_volume'])); con.commit(); con.close()
                with LOCK: SCAN['ok']+=1
            except Exception: 
                with LOCK: SCAN['errors']+=1
            with LOCK: SCAN['done']=i
        with LOCK: SCAN.update(running=False,finished=datetime.utcnow().isoformat(),ticker='',message='Scansione completata')
    except Exception as e:
        with LOCK: SCAN.update(running=False,finished=datetime.utcnow().isoformat(),message=str(e))

@app.route('/')
def home(): return render_template('index.html')
@app.get('/api/ticker/<t>')
def ticker(t):
    try:
        t=t.upper(); d=details(t); return jsonify(ticker=t,name=d.get('name',t),market_cap=d.get('market_cap'),exchange=d.get('primary_exchange'),bars=bars(t),expirations=expirations(t))
    except Exception as e:return jsonify(error=str(e)),500
@app.get('/api/options/<t>')
def options(t):
    try:return jsonify(calc(t.upper(),request.args.get('expiration'),True))
    except Exception as e:return jsonify(error=str(e)),500
@app.get('/api/history/<t>')
def history(t):
    con=db(); con.row_factory=sqlite3.Row; r=con.execute('SELECT * FROM band_history WHERE ticker=? ORDER BY day ASC',(t.upper(),)).fetchall(); con.close(); return jsonify(results=[dict(x) for x in r])
@app.post('/api/scan/start')
def scan_start():
    with LOCK:
        if SCAN['running']: return jsonify(SCAN)
        SCAN.update(running=True,started=datetime.utcnow().isoformat(),finished=None,message='Preparazione universo…')
    limit=max(0,int((request.get_json(silent=True) or {}).get('limit',0))); threading.Thread(target=scan_worker,args=(limit,),daemon=True).start(); return jsonify(SCAN)
@app.get('/api/scan/status')
def scan_status():
    with LOCK:return jsonify(dict(SCAN))
@app.get('/api/screener')
def screener():
    z=request.args.get('zone','all'); metric=request.args.get('metric','volume'); con=db(); con.row_factory=sqlite3.Row; q='SELECT * FROM scan_results WHERE day=?'; a=[date.today().isoformat()]
    if z=='minus2': q+=' AND iv_position<=-2'
    elif z=='minus1': q+=' AND iv_position<=-1'
    elif z=='plus1': q+=' AND iv_position>=1'
    elif z=='plus2': q+=' AND iv_position>=2'
    elif z=='callwall': q+=(' AND call_wall_below_volume_score>=70' if metric=='volume' else ' AND call_wall_below_score>=70')
    elif z=='putwall': q+=(' AND put_wall_above_volume_score>=70' if metric=='volume' else ' AND put_wall_above_score>=70')
    elif z=='walls': q+=( ' AND (call_wall_below_volume_score>=70 OR put_wall_above_volume_score>=70)' if metric=='volume' else ' AND (call_wall_below_score>=70 OR put_wall_above_score>=70)')
    elif z=='pcr1': q+=( ' AND put_call_ratio_volume>1' if metric=='volume' else ' AND put_call_ratio>1')
    if z=='callwall': q+=(' ORDER BY call_wall_below_volume_score DESC' if metric=='volume' else ' ORDER BY call_wall_below_score DESC')
    elif z=='putwall': q+=(' ORDER BY put_wall_above_volume_score DESC' if metric=='volume' else ' ORDER BY put_wall_above_score DESC')
    elif z=='walls': q+=( ' ORDER BY MAX(COALESCE(call_wall_below_volume_score,0),COALESCE(put_wall_above_volume_score,0)) DESC' if metric=='volume' else ' ORDER BY MAX(COALESCE(call_wall_below_score,0),COALESCE(put_wall_above_score,0)) DESC')
    elif z=='pcr1': q+=(' ORDER BY put_call_ratio_volume DESC' if metric=='volume' else ' ORDER BY put_call_ratio DESC')
    else: q+=' ORDER BY iv_position ASC'
    r=con.execute(q,a).fetchall(); con.close(); return jsonify(results=[dict(x) for x in r])
@app.get('/health')
def health():return jsonify(ok=True,mode='v5.3-volume-oi-selector')
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')))
