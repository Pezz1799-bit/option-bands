import os, math, time, sqlite3, threading, json
from datetime import date, timedelta, datetime
from flask import Flask, render_template, request, jsonify
import requests

app=Flask(__name__); BASE='https://api.massive.com'; API_KEY=os.getenv('MASSIVE_API_KEY','').strip(); DB_PATH=os.getenv('DB_PATH','/tmp/option_bands.db'); CACHE={}; CACHE_TTL=900
SCAN={'running':False,'done':0,'total':0,'ok':0,'no_data':0,'api_errors':0,'errors':0,'ticker':'','started':None,'finished':None,'message':'Pronto','batch':0,'batch_size':200}; LOCK=threading.Lock()

def db():
    con=sqlite3.connect(DB_PATH,check_same_thread=False); con.execute('''CREATE TABLE IF NOT EXISTS band_history(day TEXT NOT NULL,ticker TEXT NOT NULL,expiration TEXT NOT NULL,spot REAL,put_iv REAL,atm_iv REAL,call_iv REAL,skew REAL,lower2 REAL,lower1 REAL,upper1 REAL,upper2 REAL,iv_position REAL,reference_spot REAL,current_spot REAL,put_delta REAL,call_delta REAL,put_strike REAL,call_strike REAL,PRIMARY KEY(day,ticker,expiration))'''); con.execute('''CREATE TABLE IF NOT EXISTS scan_results(day TEXT NOT NULL,ticker TEXT NOT NULL,expiration TEXT,spot REAL,put_iv REAL,call_iv REAL,skew REAL,lower2 REAL,lower1 REAL,upper1 REAL,upper2 REAL,iv_position REAL,zone TEXT,call_wall_below_score REAL,call_wall_below_strike REAL,call_wall_below_oi REAL,put_wall_above_score REAL,put_wall_above_strike REAL,put_wall_above_oi REAL,put_call_ratio REAL,gex_estimate REAL,skew_left_score REAL,skew_spot_position REAL,skew_left_iv REAL,skew_right_iv REAL,market_cap REAL,PRIMARY KEY(day,ticker))'''); 
    # Lightweight migration for databases created by previous versions.
    cols={r[1] for r in con.execute('PRAGMA table_info(scan_results)').fetchall()}
    for name in ['call_wall_below_score','call_wall_below_strike','call_wall_below_oi','put_wall_above_score','put_wall_above_strike','put_wall_above_oi','put_call_ratio','gex_estimate','call_wall_below_volume_score','call_wall_below_volume_strike','call_wall_below_volume','put_wall_above_volume_score','put_wall_above_volume_strike','put_wall_above_volume','put_call_ratio_volume','skew_left_score','skew_spot_position','skew_left_iv','skew_right_iv','market_cap','call_negative_proxy','call_negative_strike','call_negative_oi']:
        if name not in cols: con.execute(f'ALTER TABLE scan_results ADD COLUMN {name} REAL')
    for name,kind in [('iv_regime','TEXT'),('iv_valid_expiries','INTEGER'),('iv_missing_expiries','INTEGER'),('gamma_flip','REAL')]:
        if name not in cols: con.execute(f'ALTER TABLE scan_results ADD COLUMN {name} {kind}')
    con.execute('''CREATE TABLE IF NOT EXISTS scan_blocks(day TEXT NOT NULL, block INTEGER NOT NULL, ticker TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(day,block,ticker))''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_scan_blocks_day_block ON scan_blocks(day,block)')
    con.execute('''CREATE TABLE IF NOT EXISTS smile_history(day TEXT NOT NULL,ticker TEXT NOT NULL,expiration TEXT NOT NULL,recorded_at TEXT NOT NULL,spot REAL,spot_date TEXT,min_strike REAL,min_iv REAL,min_status TEXT,curve_json TEXT NOT NULL,PRIMARY KEY(day,ticker,expiration))''')
    con.commit(); return con

def cached(key,fn,ttl=CACHE_TTL):
    now=time.time()
    if key in CACHE and now-CACHE[key][0]<ttl:return CACHE[key][1]
    v=fn(); CACHE[key]=(now,v); return v

def api(path_or_url,params=None):
    if not API_KEY: raise RuntimeError('MASSIVE_API_KEY non configurata.')
    url=path_or_url if path_or_url.startswith('http') else BASE+path_or_url; p=dict(params or {}); p['apiKey']=API_KEY
    last=None
    for attempt in range(5):
        try:
            r=requests.get(url,params=p,timeout=45)
            if r.status_code==429:
                time.sleep(min(12,1.5*(2**attempt))); continue
            if r.status_code==403: raise RuntimeError('Endpoint non incluso nel piano Massive attivo.')
            r.raise_for_status(); return r.json()
        except requests.RequestException as e:
            last=e
            if attempt<4: time.sleep(min(8,1.0*(2**attempt)))
    raise RuntimeError('Errore API Massive dopo retry: '+str(last or 'rate limit'))

def bars(t,days=365):
    def load():
        end=date.today(); start=end-timedelta(days=days); return api(f'/v2/aggs/ticker/{t}/range/1/day/{start}/{end}',{'adjusted':'true','sort':'asc','limit':5000}).get('results',[])
    return cached(('bars',t,days),load,900)

def details(t): return cached(('det',t),lambda:api(f'/v3/reference/tickers/{t}').get('results',{}),3600)
def expirations(t):
    def load():
        url='/v3/reference/options/contracts'; params={'underlying_ticker':t,'expired':'false','limit':1000,'sort':'expiration_date','order':'asc'}; found=set()
        while url:
            j=api(url,params); params=None
            found.update(x['expiration_date'] for x in j.get('results',[]) if x.get('expiration_date'))
            url=j.get('next_url')
        return sorted(found)
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
        while nxt: j=api(nxt); rows+=j.get('results',[]); nxt=j.get('next_url'); n+=1
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
    call_oi=put_oi=call_volume=put_volume=0.0; call_gex=put_gex=0.0; gp={}
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
        try: strike=float(d.get('strike_price') or 0)
        except: strike=0
        if strike>0:
            z=gp.setdefault(strike,{'strike':strike,'call_gex':0.0,'put_gex':0.0,'net_gex':0.0})
            if kind=='call': z['call_gex']+=gex; z['net_gex']+=gex
            else: z['put_gex']-=gex; z['net_gex']-=gex
        if kind=='call': call_gex+=gex
        else: put_gex+=gex
    return {'call_oi':call_oi,'put_oi':put_oi,'put_call_ratio_oi':(put_oi/call_oi) if call_oi>0 else None,'call_volume':call_volume,'put_volume':put_volume,'put_call_ratio_volume':(put_volume/call_volume) if call_volume>0 else None,'call_gex':call_gex,'put_gex':-put_gex,'net_gex':call_gex-put_gex,'gamma_profile':[gp[k] for k in sorted(gp)]}

def call_negative_proxy(rows,S):
    """Scenario only: dealer short listed calls above spot. OI cannot establish dealer sign."""
    vals=[]
    for x in rows:
        d=x.get('details') or {}; g=x.get('greeks') or {}
        if d.get('contract_type')!='call': continue
        try:
            k=float(d.get('strike_price') or 0); oi=float(x.get('open_interest') or 0); gamma=float(g.get('gamma') or 0)
            if k>S and oi>0 and gamma>0: vals.append((gamma*oi*100*S*S*.01,k,oi))
        except (ValueError,TypeError): pass
    if not vals: return {'score':None,'strike':None,'oi':None}
    v=max(vals)
    return {'score':-sum(a for a,k,o in vals),'strike':v[1],'oi':v[2]}

def volatility_skew(rows,S):
    # One IV point per strike: prefer OTM put below spot and OTM call above spot; near spot use available contract.
    by={}
    for x in rows:
        try:
            iv=float(x.get('implied_volatility') or 0); d=x.get('details') or {}; k=float(d.get('strike_price') or 0); kind=d.get('contract_type')
        except: continue
        if iv<=0 or not math.isfinite(iv) or k<=0 or kind not in {'call','put'}: continue
        pref=(k<=S and kind=='put') or (k>S and kind=='call')
        cur=by.get(k)
        if cur is None or (pref and not cur[1]): by[k]=(iv,pref,kind)
    pts=[{'strike':k,'iv':v[0],'type':v[2]} for k,v in sorted(by.items())]
    if len(pts)<4: return pts,{'score':0.0,'spot_position':None,'left_iv':None,'right_iv':None,'qualifies':False}
    lo,hi=pts[0]['strike'],pts[-1]['strike']; span=hi-lo
    spotpos=(S-lo)/span if span>0 else .5
    left=[p['iv'] for p in pts if p['strike']<S]; right=[p['iv'] for p in pts if p['strike']>S]
    def med(a):
        a=sorted(a); n=len(a); return None if not n else (a[n//2] if n%2 else (a[n//2-1]+a[n//2])/2)
    li,ri=med(left),med(right); diff=(li-ri) if li is not None and ri is not None else 0.0
    score=max(0.0,min(100.0,50.0+diff*1000.0)) if li is not None and ri is not None else 0.0
    return pts,{'score':score,'spot_position':spotpos,'left_iv':li,'right_iv':ri,'qualifies':bool(li is not None and ri is not None and li>ri and spotpos>0.5)}

def gex_price_curve(rows,spot,dte,rate=.04,dividend=0.0):
    """European Black-Scholes scenario, fixed IV/OI; call +, put - assumption."""
    tau=max(dte,1)/365.0; contracts=[]; ivs=[]
    for row in rows:
        try:
            d=row.get('details') or {}; k=float(d.get('strike_price',0)); iv=float(row.get('implied_volatility',0)); oi=option_oi(row); kind=d.get('contract_type'); mult=float(d.get('shares_per_contract') or 100)
            if kind not in ('call','put') or not all(math.isfinite(x) and x>0 for x in (k,iv,oi,mult)): continue
            contracts.append((k,iv,oi,mult,1 if kind=='call' else -1)); ivs.append((abs(k-spot),iv))
        except (ValueError,TypeError): continue
    if not contracts: return {'points':[],'flips':[],'contracts':0}
    ref_iv=sum(v for _,v in sorted(ivs)[:6])/min(6,len(ivs)); span=min(.4,max(.1,2*ref_iv*math.sqrt(tau)))
    def value(price):
        total=0.0
        for k,iv,oi,mult,sign in contracts:
            d1=(math.log(price/k)+(rate-dividend+.5*iv*iv)*tau)/(iv*math.sqrt(tau))
            gamma=math.exp(-dividend*tau-.5*d1*d1)/(math.sqrt(2*math.pi)*price*iv*math.sqrt(tau))
            total+=sign*gamma*oi*mult*price*price*.01
        return total
    points=[{'price':spot*(1-span+2*span*i/200),'gex':value(spot*(1-span+2*span*i/200))} for i in range(201)]
    flips=[]
    for a,b in zip(points,points[1:]):
        if a['gex']*b['gex']<0:
            lo,hi=a['price'],b['price']; vlo=a['gex']
            for _ in range(35):
                mid=(lo+hi)/2; vm=value(mid)
                if vm*vlo>0: lo=mid; vlo=vm
                else: hi=mid
            flips.append((lo+hi)/2)
        elif a['gex']==0 and (not flips or abs(flips[-1]-a['price'])>spot*1e-8): flips.append(a['price'])
    return {'points':points,'flips':flips,'spot_gex':value(spot),'contracts':len(contracts),'rate':rate,'dividend_yield':dividend,'dte':dte,'span_pct':span*100,'model':'Black-Scholes europeo; IV e OI fissi; call + / put −; moltiplicatore da contratto o 100. Scenario, non posizionamento dealer osservato.'}

def smile_minimum(curve,spot):
    pts=sorted([p for p in curve if math.isfinite(p['iv']) and p['iv']>0],key=lambda p:p['strike'])
    if len(pts)<3: return {'strike':None,'iv':None,'status':'Dati insufficienti'}
    lowest=min(p['iv'] for p in pts); tied=[p for p in pts if abs(p['iv']-lowest)<1e-10]
    point=min(tied,key=lambda p:abs(p['strike']-spot)); interior=pts[0]['strike']<point['strike']<pts[-1]['strike']
    return {'strike':point['strike'],'iv':point['iv'],'status':'Minimo osservato interno' if interior else 'Minimo al bordo: vertice non identificato'}

def calc(t,exp,save=True):
    bs=bars(t,30); S=float(bs[-1]['c']) if bs else 0
    if S<=0: raise RuntimeError('Prezzo non disponibile')
    rows=chain(t,exp); skew_curve,skew_shape=volatility_skew(rows,S); oi_levels,call_wall_oi,put_wall_oi=wall_profile(rows,S,'oi'); volume_levels,call_wall_volume,put_wall_volume=wall_profile(rows,S,'volume'); cm=chain_metrics(rows,S); put=pick(rows,'put'); call=pick(rows,'call')
    if not put:
        pp=[x for x in rows if valid(x) and (x.get('details') or {}).get('contract_type')=='put']
        put=min(pp,key=lambda x:abs(float((x.get('details') or {}).get('strike_price'))-S)) if pp else None
    if not call:
        cc=[x for x in rows if valid(x) and (x.get('details') or {}).get('contract_type')=='call']
        call=min(cc,key=lambda x:abs(float((x.get('details') or {}).get('strike_price'))-S)) if cc else None
    if not put or not call: raise ValueError('NO_DATA: IV/Greeks insufficienti')
    piv=float(put['implied_volatility']); civ=float(call['implied_volatility']); aiv=atm_iv(rows,S) or (piv+civ)/2; dte=max((datetime.strptime(exp,'%Y-%m-%d').date()-date.today()).days,1); T=dte/365; down=piv*math.sqrt(T); up=civ*math.sqrt(T); b={'lower2':S*math.exp(-2*down),'lower1':S*math.exp(-down),'upper1':S*math.exp(up),'upper2':S*math.exp(2*up)}
    # Position vs previous saved box: meaningful for screener and oscillator.
    con=db(); con.row_factory=sqlite3.Row; prev=con.execute('SELECT * FROM band_history WHERE ticker=? AND day<? ORDER BY day DESC LIMIT 1',(t,date.today().isoformat())).fetchone(); pos=0.0
    if prev and prev['reference_spot'] and S>0:
        move=math.log(S/prev['reference_spot']); oldT=max((datetime.strptime(prev['expiration'],'%Y-%m-%d').date()-date.today()).days,1)/365; sig=(prev['call_iv'] if move>=0 else prev['put_iv'])*math.sqrt(oldT); pos=move/sig if sig>0 else 0
    pd,pg=put['details'],put['greeks']; cd,cg=call['details'],call['greeks']; out={'expiration':exp,'dte':dte,'spot':S,'reference_spot':S,'put_iv':piv,'atm_iv':aiv,'call_iv':civ,'skew':piv-civ,'iv_position':pos,'bands':b,'put_strike':float(pd['strike_price']),'call_strike':float(cd['strike_price']),'put_delta':float(pg['delta']),'call_delta':float(cg['delta']),'mode':'Snapshot · 25Δ reali','option_profile_oi':oi_levels,'option_profile_volume':volume_levels,'call_wall_below_oi':call_wall_oi,'put_wall_above_oi':put_wall_oi,'call_wall_below_volume':call_wall_volume,'put_wall_above_volume':put_wall_volume,'put_call_ratio_oi':cm['put_call_ratio_oi'],'put_call_ratio_volume':cm['put_call_ratio_volume'],'call_oi_total':cm['call_oi'],'put_oi_total':cm['put_oi'],'call_volume_total':cm['call_volume'],'put_volume_total':cm['put_volume'],'call_negative_scenario':call_negative_proxy(rows,S),'gex_estimate':cm['net_gex'],'call_gex_estimate':cm['call_gex'],'put_gex_estimate':cm['put_gex'],'gamma_exposure_profile':cm['gamma_profile'],'volatility_skew_curve':skew_curve,'skew_left_score':skew_shape['score'],'skew_spot_position':skew_shape['spot_position'],'skew_left_iv':skew_shape['left_iv'],'skew_right_iv':skew_shape['right_iv'],'skew_left_qualifies':skew_shape['qualifies']}
    minimum=smile_minimum(skew_curve,S); out['smile_minimum']=minimum
    if save:
        from zoneinfo import ZoneInfo
        observed=datetime.now(ZoneInfo('America/New_York')); observed_day=observed.date().isoformat()
        spot_day=datetime.fromtimestamp(bs[-1]['t']/1000,ZoneInfo('America/New_York')).date().isoformat()
        con.execute('''INSERT OR REPLACE INTO smile_history(day,ticker,expiration,recorded_at,spot,spot_date,min_strike,min_iv,min_status,curve_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',(observed_day,t,exp,observed.isoformat(),S,spot_day,minimum['strike'],minimum['iv'],minimum['status'],json.dumps(skew_curve,allow_nan=False)))
        con.execute('''INSERT OR REPLACE INTO band_history(day,ticker,expiration,spot,put_iv,atm_iv,call_iv,skew,lower2,lower1,upper1,upper2,iv_position,reference_spot,current_spot,put_delta,call_delta,put_strike,call_strike) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(date.today().isoformat(),t,exp,S,piv,aiv,civ,piv-civ,b['lower2'],b['lower1'],b['upper1'],b['upper2'],pos,S,S,out['put_delta'],out['call_delta'],out['put_strike'],out['call_strike'])); con.commit()
    con.close(); return out

def universe(limit=0):
    # Fixed universe: ordinary Nasdaq/NYSE companies with market cap >= $700M.
    base=[]; url='/v3/reference/tickers'; params={'market':'stocks','active':'true','type':'CS','limit':1000,'sort':'ticker','order':'asc'}
    while url:
        j=api(url,params if not url.startswith('http') else None); params=None; base += [x for x in j.get('results',[]) if x.get('primary_exchange') in {'XNAS','XNYS'}]; url=j.get('next_url')
    rows=[]
    for x in base:
        try:
            d=details(x['ticker']); mc=float(d.get('market_cap') or 0)
            if mc>=700_000_000:
                y=dict(x); y['market_cap']=mc; rows.append(y)
        except Exception:
            continue
    rows.sort(key=lambda x:x.get('market_cap',0),reverse=True)
    return rows[:limit] if limit else rows

def zone(pos):
    if pos<=-2:return '≤ -2σ'
    if pos<=-1:return '-2σ / -1σ'
    if pos>=2:return '≥ +2σ'
    if pos>=1:return '+1σ / +2σ'
    return 'Dentro ±1σ'

def scan_worker(limit=200,offset=0):
    try:
        uni=universe()[offset:offset+limit];
        con=db(); day=date.today().isoformat(); block=offset//limit+1
        con.executemany('INSERT OR IGNORE INTO scan_blocks(day,block,ticker,status) VALUES(?,?,?,?)',[(day,block,x['ticker'],'pending') for x in uni]); con.commit(); con.close() 
        with LOCK: SCAN.update(batch=offset//limit+1,batch_size=limit,total=len(uni),done=0,ok=0,no_data=0,api_errors=0,errors=0,message='Scansione in corso')
        for i,x in enumerate(uni,1):
            t=x['ticker'];
            with LOCK: SCAN.update(ticker=t,done=i-1)
            try:
                ex=expirations(t); e=target_exp(ex,30)
                if not e: raise RuntimeError('no expiry')
                m=calc(t,e,True); b=m['bands']; z=zone(m['iv_position']); con=db(); con.execute('''INSERT OR REPLACE INTO scan_results(day,ticker,expiration,spot,put_iv,call_iv,skew,lower2,lower1,upper1,upper2,iv_position,zone,call_wall_below_score,call_wall_below_strike,call_wall_below_oi,put_wall_above_score,put_wall_above_strike,put_wall_above_oi,put_call_ratio,gex_estimate,call_wall_below_volume_score,call_wall_below_volume_strike,call_wall_below_volume,put_wall_above_volume_score,put_wall_above_volume_strike,put_wall_above_volume,put_call_ratio_volume,skew_left_score,skew_spot_position,skew_left_iv,skew_right_iv,market_cap) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(date.today().isoformat(),t,e,m['spot'],m['put_iv'],m['call_iv'],m['skew'],b['lower2'],b['lower1'],b['upper1'],b['upper2'],m['iv_position'],z,m['call_wall_below_oi']['score'],m['call_wall_below_oi']['strike'],m['call_wall_below_oi']['oi'],m['put_wall_above_oi']['score'],m['put_wall_above_oi']['strike'],m['put_wall_above_oi']['oi'],m['put_call_ratio_oi'],m['gex_estimate'],m['call_wall_below_volume']['score'],m['call_wall_below_volume']['strike'],m['call_wall_below_volume']['volume'],m['put_wall_above_volume']['score'],m['put_wall_above_volume']['strike'],m['put_wall_above_volume']['volume'],m['put_call_ratio_volume'],m['skew_left_score'],m['skew_spot_position'],m['skew_left_iv'],m['skew_right_iv'],float(x.get('market_cap') or 0))); con.execute('UPDATE scan_results SET call_negative_proxy=?,call_negative_strike=?,call_negative_oi=? WHERE day=? AND ticker=?',(m['call_negative_scenario']['score'],m['call_negative_scenario']['strike'],m['call_negative_scenario']['oi'],date.today().isoformat(),t)); con.commit(); con.close()
                try:
                    term=term_structure_data(t)
                except Exception:
                    term={'regime':'Dati insufficienti','points':[],'missing':[]}
                curve=gex_price_curve(chain(t,e),m['spot'],m['dte'])
                flips=curve.get('flips',[]); flip=min(flips,key=lambda f:abs(f-m['spot'])) if flips else None
                con=db(); con.execute('UPDATE scan_results SET iv_regime=?,iv_valid_expiries=?,iv_missing_expiries=?,gamma_flip=? WHERE day=? AND ticker=?',(term['regime'],len(term['points']),len(term['missing']),flip,day,t)); con.commit(); con.close()
                con=db(); con.execute('UPDATE scan_blocks SET status=? WHERE day=? AND block=? AND ticker=?',('ok',day,block,t)); con.commit(); con.close()
                with LOCK: SCAN['ok']+=1
            except ValueError:
                with LOCK: SCAN['no_data']+=1
            except RuntimeError as e:
                with LOCK:
                    if 'API Massive' in str(e) or 'Endpoint' in str(e) or 'retry' in str(e): SCAN['api_errors']+=1
                    else: SCAN['errors']+=1
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
    try:
        symbol=t.upper(); exp=request.args.get('expiration'); result=calc(symbol,exp,True)
        result['gex_price_curve']=gex_price_curve(chain(symbol,exp),result['spot'],result['dte'])
        return jsonify(result)
    except Exception as e:return jsonify(error=str(e)),500
def term_atm(rows,spot):
    """Interpolate each side at spot; no delta required. Reject distant strikes."""
    result={}
    for kind in ('call','put'):
        by={}
        for row in rows:
            try:
                d=row.get('details') or {}; k=float(d.get('strike_price',0)); iv=float(row.get('implied_volatility',0))
                if d.get('contract_type')==kind and k>0 and math.isfinite(iv) and iv>0 and abs(k/spot-1)<=.10:
                    by.setdefault(k,[]).append(iv)
            except (ValueError,TypeError): continue
        pts=sorted((k,sum(v)/len(v)) for k,v in by.items())
        lower=[x for x in pts if x[0]<=spot]; upper=[x for x in pts if x[0]>=spot]
        if not lower or not upper: continue
        lo,hi=lower[-1],upper[0]
        result[kind]=lo[1] if lo[0]==hi[0] else lo[1]+(hi[1]-lo[1])*(spot-lo[0])/(hi[0]-lo[0])
    # Require both sides so every maturity uses the same definition.
    if len(result)!=2: return None
    return {'iv':(result['call']+result['put'])/2,'call_iv':result['call'],'put_iv':result['put']}

def term_structure_data(t):
    from concurrent.futures import ThreadPoolExecutor
    t=t.upper(); today=date.today(); bs=bars(t,30)
    if not bs or float(bs[-1]['c'])<=0: raise ValueError('Prezzo non disponibile')
    spot=float(bs[-1]['c']); dates=[e for e in expirations(t) if 0<(date.fromisoformat(e)-today).days<=365]
    def point(e):
        try:
            value=term_atm(chain(t,e),spot)
            if value is None: return {'expiration':e,'error':'IV ATM call/put insufficienti entro ±10% dallo spot'}
            return dict(value,expiration=e,dte=(date.fromisoformat(e)-today).days)
        except Exception:
            return {'expiration':e,'error':'Chain non disponibile; controllare accesso e limiti del piano dati'}
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(point,dates))
    points=[x for x in results if 'iv' in x]; missing=[x for x in results if 'error' in x]
    regime='Dati insufficienti'
    if len(points)>=2:
        diffs=[b['iv']-a['iv'] for a,b in zip(points,points[1:])]; tol=.001
        regime='Piatta' if all(abs(x)<=tol for x in diffs) else ('Contango' if all(x>=-tol for x in diffs) else ('Backwardation' if all(x<=tol for x in diffs) else 'Mista / con gobbe'))
    return dict(ticker=t,spot=spot,spot_date=datetime.fromtimestamp(bs[-1]['t']/1000).date().isoformat(),points=points,missing=missing,regime=regime,method='Media IV call/put interpolata allo spot; IV annualizzata; scadenze 1–365 giorni; tolleranza pendenza 0,1 punti IV. Snapshot in cache fino a 5 minuti.')

@app.get('/api/iv-term-structure/<t>')
def iv_term_structure(t):
    try: return jsonify(term_structure_data(t))
    except Exception as e: return jsonify(error=str(e)),500

def combine_smiles(curves,spot):
    # Equal-weight IV across exactly three maturities on overlapping strike ranges.
    from bisect import bisect_left
    if len(curves)!=3 or any(len(c)<3 for c in curves): return []
    curves=[sorted(c,key=lambda p:p['strike']) for c in curves]
    lo=max(c[0]['strike'] for c in curves); hi=min(c[-1]['strike'] for c in curves)
    strikes=sorted({p['strike'] for c in curves for p in c if lo<=p['strike']<=hi})
    if len(strikes)<3: return []
    def iv_at(c,k):
        keys=[p['strike'] for p in c]; i=bisect_left(keys,k)
        if i<len(c) and c[i]['strike']==k: return c[i]['iv']
        a,b=c[i-1],c[i]; return a['iv']+(b['iv']-a['iv'])*(k-a['strike'])/(b['strike']-a['strike'])
    return [{'strike':k,'iv':sum(iv_at(c,k) for c in curves)/3,'type':'composito'} for k in strikes]

@app.get('/api/first-smiles/<t>')
def first_smiles(t):
    try:
        from zoneinfo import ZoneInfo
        from concurrent.futures import ThreadPoolExecutor
        t=t.upper(); now=datetime.now(ZoneInfo('America/New_York')); bs=bars(t,30)
        if not bs or float(bs[-1]['c'])<=0: return jsonify(error='Prezzo non disponibile'),422
        spot=float(bs[-1]['c']); dates=sorted({e for e in expirations(t) if date.fromisoformat(e)>=now.date()})[:3]
        def one(e):
            try:
                curve,_=volatility_skew(chain(t,e),spot)
                if len(curve)<3: return {'expiration':e,'error':'Meno di 3 strike con IV valida'}
                return {'expiration':e,'curve':curve,'minimum':smile_minimum(curve,spot)}
            except Exception: return {'expiration':e,'error':'Chain non disponibile per questa scadenza'}
        with ThreadPoolExecutor(max_workers=3) as pool: results=list(pool.map(one,dates))
        smiles=[x for x in results if 'curve' in x]; missing=[x for x in results if 'error' in x]
        combined=combine_smiles([x['curve'] for x in smiles],spot)
        spot_day=datetime.fromtimestamp(bs[-1]['t']/1000,ZoneInfo('America/New_York')).date().isoformat()
        con=db()
        for item in smiles:
            m=item['minimum']; con.execute('INSERT OR REPLACE INTO smile_history(day,ticker,expiration,recorded_at,spot,spot_date,min_strike,min_iv,min_status,curve_json) VALUES(?,?,?,?,?,?,?,?,?,?)',(now.date().isoformat(),t,item['expiration'],now.isoformat(),spot,spot_day,m['strike'],m['iv'],m['status'],json.dumps(item['curve'],allow_nan=False)))
        con.commit(); con.close()
        return jsonify(ticker=t,spot=spot,spot_date=spot_day,requested_expirations=dates,smiles=smiles,missing=missing,composite={'curve':combined,'minimum':smile_minimum(combined,spot)},method='Media aritmetica IV 1/3 per scadenza, interpolata agli stessi strike solo nel range comune. Indice composito, non IV di una singola opzione.')
    except Exception as e: return jsonify(error=str(e)),500

@app.get('/api/smile-history/<t>')
def smile_history(t):
    exp=request.args.get('expiration',''); con=db(); con.row_factory=sqlite3.Row
    rows=con.execute('SELECT * FROM smile_history WHERE ticker=? AND expiration=? ORDER BY day DESC LIMIT 365',(t.upper(),exp)).fetchall(); con.close()
    results=[]
    for row in reversed(rows):
        item=dict(row); item['curve']=json.loads(item.pop('curve_json')); results.append(item)
    return jsonify(results=results,expiration=exp)

@app.get('/api/history/<t>')
def history(t):
    con=db(); con.row_factory=sqlite3.Row; r=con.execute('SELECT * FROM band_history WHERE ticker=? ORDER BY day ASC',(t.upper(),)).fetchall(); con.close(); return jsonify(results=[dict(x) for x in r])
@app.post('/api/scan/start')
def scan_start():
    with LOCK:
        if SCAN['running']: return jsonify(SCAN)
        SCAN.update(running=True,started=datetime.utcnow().isoformat(),finished=None,message='Preparazione universo…')
    payload=request.get_json(silent=True) or {}
    try:
        block=int(payload.get('block',1))
        if block<1 or block>15: raise ValueError()
    except (TypeError,ValueError):
        with LOCK: SCAN.update(running=False,message='Blocco non valido')
        return jsonify(error='Seleziona un blocco tra 1 e 15'),400
    offset=(block-1)*200
    with LOCK: SCAN.update(batch=block,message=f'Preparazione blocco {block}…')
    threading.Thread(target=scan_worker,args=(200,offset),daemon=True).start()
    return jsonify(dict(SCAN))
@app.get('/api/scan/status')
def scan_status():
    with LOCK:return jsonify(dict(SCAN))
@app.get('/api/scan/blocks')
def scan_blocks():
    con=db(); con.row_factory=sqlite3.Row
    rows=con.execute('''SELECT block,COUNT(*) AS total,SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS valid FROM scan_blocks WHERE day=? GROUP BY block ORDER BY block''',(date.today().isoformat(),)).fetchall()
    con.close(); return jsonify(blocks=[dict(r) for r in rows])
@app.get('/api/screener')
def screener():
    z=request.args.get('zone','all'); metric=request.args.get('metric','volume'); con=db(); con.row_factory=sqlite3.Row; q='SELECT scan_results.* FROM scan_results WHERE scan_results.day=?'; a=[date.today().isoformat()]
    block=request.args.get('block','all')
    if block!='all':
        try: block_num=int(block); assert block_num>0
        except (ValueError,AssertionError): con.close(); return jsonify(error='Blocco non valido'),400
        q+=' AND EXISTS (SELECT 1 FROM scan_blocks sb WHERE sb.day=scan_results.day AND sb.ticker=scan_results.ticker AND sb.block=? AND sb.status=?)'; a.extend([block_num,'ok'])
    if z=='contango': q+=" AND iv_regime='Contango'"
    elif z=='backwardation': q+=" AND iv_regime='Backwardation'"
    elif z=='aboveflip': q+=' AND gamma_flip IS NOT NULL AND spot>gamma_flip'
    elif z=='belowflip': q+=' AND gamma_flip IS NOT NULL AND spot<gamma_flip'
    elif z=='callwall': q+=(' AND call_wall_below_volume_score>=70' if metric=='volume' else ' AND call_wall_below_score>=70')
    elif z=='putwall': q+=(' AND put_wall_above_volume_score>=70' if metric=='volume' else ' AND put_wall_above_score>=70')
    elif z=='walls': q+=( ' AND (call_wall_below_volume_score>=70 OR put_wall_above_volume_score>=70)' if metric=='volume' else ' AND (call_wall_below_score>=70 OR put_wall_above_score>=70)')
    elif z=='pcr1': q+=( ' AND put_call_ratio_volume>1' if metric=='volume' else ' AND put_call_ratio>1')
    elif z=='negativecalls': q+=' AND call_negative_proxy<0'
    elif z=='leftskew': q+=' AND skew_spot_position IS NOT NULL'
    sort=request.args.get('sort','market_cap'); direction='ASC' if request.args.get('dir','desc').lower()=='asc' else 'DESC'
    allowed={'market_cap':'market_cap','ticker':'ticker','spot':'spot','iv_position':'iv_position','skew':'skew','pcr':'put_call_ratio_volume' if metric=='volume' else 'put_call_ratio','gex':'gex_estimate','skew_spot_position':'skew_spot_position','call_negative_proxy':'call_negative_proxy'}
    if z=='leftskew' and sort=='market_cap': sort='skew_spot_position'; direction='DESC'
    if z=='negativecalls' and sort=='market_cap': sort='call_negative_proxy'; direction='ASC'
    col=allowed.get(sort,'market_cap'); q+=f' ORDER BY COALESCE({col},0) {direction}'
    r=con.execute(q,a).fetchall(); con.close(); return jsonify(results=[dict(x) for x in r])
@app.get('/health')
def health():return jsonify(ok=True,mode='v6.7-price-smiles')
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')))
