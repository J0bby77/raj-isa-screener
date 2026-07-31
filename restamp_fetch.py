#!/usr/bin/env python3
"""Bounded FV/analyst fetch for restamp shortlist names. Resumable JSON cache."""
import sys, os, json, time
sys.path.insert(0,'/tmp/pylibs')
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor
CACHE='/tmp/restamp_fv.json'
KEYS=("currentPrice","regularMarketPrice","targetMeanPrice","recommendationKey",
      "numberOfAnalystOpinions","trailingPE","sector","industry")
def one(t):
    try:
        i=yf.Ticker(t).info or {}
        return t, {k:i.get(k) for k in KEYS}
    except Exception as e:
        return t, {"_err":str(e)[:60]}
def main():
    todo_all=json.load(open('/tmp/restamp_fetch.json'))
    cache=json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    todo=[t for t in todo_all if t not in cache]
    n=int(sys.argv[1]) if len(sys.argv)>1 else 40
    chunk=todo[:n]
    if not chunk: print('ALL DONE',len(cache),'/',len(todo_all)); return
    t0=time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t,d in ex.map(one, chunk): cache[t]=d
    json.dump(cache, open(CACHE,'w'))
    ok=sum(1 for t in chunk if cache[t].get("targetMeanPrice"))
    print(f'fetched {len(chunk)} in {time.time()-t0:.0f}s | target_ok {ok}/{len(chunk)} | cached {len(cache)}/{len(todo_all)} | remaining {len(todo)-len(chunk)}')
main()
