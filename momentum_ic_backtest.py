#!/usr/bin/env python3
"""Forward-return IC backtest for the 3-month price-momentum signal (screener _price_momentum_score, lookback=63).
Measures how well 3m trailing momentum predicts FORWARD 21d/63d returns across a large panel and many
monthly formation dates -> calibrates how much weight price momentum deserves in the Forward Axis.
Resumable price cache; run repeatedly to fetch chunks, auto-computes when cache complete.
NOTE: analyst-estimate sub-signals are NOT point-in-time reconstructable via yfinance, so only the
price-momentum dimension is empirically testable here; estimate weight rests on literature + this result."""
import argparse, os, sys, statistics as stx
import warnings; warnings.filterwarnings("ignore")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--universe", required=True)
    ap.add_argument("--cache", default="/sessions/upbeat-kind-einstein/mnt/outputs/_bt_prices.csv")
    ap.add_argument("--years", default="4")
    ap.add_argument("--chunk", type=int, default=80)
    ap.add_argument("--shm", default=None)
    a=ap.parse_args()
    if a.shm and os.path.isdir(a.shm): sys.path.insert(0,a.shm)
    import pandas as pd, yfinance as yf
    uni=sorted(set(open(a.universe).read().split()))
    cache=pd.read_csv(a.cache,index_col=0,parse_dates=True) if os.path.exists(a.cache) else pd.DataFrame()
    have=set(cache.columns); todo=[t for t in uni if t not in have]
    if todo:
        chunk=todo[:a.chunk]
        px=yf.download(chunk,period=f"{a.years}y",progress=False,auto_adjust=True)["Close"]
        if isinstance(px,pd.Series): px=px.to_frame(chunk[0])
        cache=pd.concat([cache,px],axis=1); cache=cache.loc[:,~cache.columns.duplicated()]
        cache.to_csv(a.cache)
        rem=len(todo)-len(chunk)
        print(f"DOWNLOADED {len(chunk)}; cached {len(cache.columns)}/{len(uni)}; remaining {rem}")
        if rem>0: print("NOT_DONE - call again"); return
    px=cache[[c for c in uni if c in cache.columns]].sort_index().dropna(how="all")
    idx=px.index; monthpos={}
    for i,d in enumerate(idx): monthpos[(d.year,d.month)]=i
    form=sorted(monthpos.values()); L=63;F1=21;F3=63
    ic21=[];ic63=[];dec=[];nper=0
    for p in form:
        if p-L<0 or p+F3>=len(idx): continue
        mom=px.iloc[p]/px.iloc[p-L]-1
        f1=px.iloc[p+F1]/px.iloc[p]-1; f3=px.iloc[p+F3]/px.iloc[p]-1
        d=pd.DataFrame({"mom":mom,"f1":f1,"f3":f3}).dropna()
        if len(d)<20: continue
        ic21.append(d["mom"].corr(d["f1"],method="spearman"))
        ic63.append(d["mom"].corr(d["f3"],method="spearman"))
        d=d.sort_values("mom"); n=len(d); k=max(1,n//10)
        dec.append(d["f3"].iloc[-k:].mean()-d["f3"].iloc[:k].mean()); nper+=1
    def summ(x):
        m=stx.mean(x); sd=stx.pstdev(x) or 1e-9; return m, m/(sd/len(x)**0.5), sum(1 for v in x if v>0)/len(x)
    m1,t1,h1=summ(ic21); m3,t3,h3=summ(ic63); dm=stx.mean(dec)
    print(f"PANEL names={px.shape[1]} formation_dates={nper} (~{nper*px.shape[1]} obs)")
    print(f"3m-mom -> FWD 21d: rankIC={m1:.4f} t={t1:.2f} hit={h1:.0%}")
    print(f"3m-mom -> FWD 63d: rankIC={m3:.4f} t={t3:.2f} hit={h3:.0%}")
    print(f"FWD 63d top-decile minus bottom-decile: {dm*100:.2f}% per 3m")
main()
