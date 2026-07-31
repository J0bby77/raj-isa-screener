#!/usr/bin/env python3
"""Phase 3: source score + doors + SUMMARY re-selection under CURRENT config, with a diff."""
import sys, os, json, glob
sys.path.insert(0,'/tmp/pylibs'); sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import source_score as ss, scoring_config as cfg, screener_core as sc
fv=json.load(open('/tmp/restamp_fv.json'))
OLD_SUMMARY={}
def num(v):
    try: return float(v)
    except: return None
report={}
for f in sorted(glob.glob('/tmp/restamp/*.json')):
    d=json.load(open(f)); g=d['group']; rows=d['rows']
    for r in rows:
        t=r['ticker']; e=fv.get(t) or {}
        if not r.get('current_price'):
            r['current_price']=e.get('currentPrice') or e.get('regularMarketPrice')
        if not r.get('target_price_mean'): r['target_price_mean']=e.get('targetMeanPrice')
        if not r.get('analyst_rating'):    r['analyst_rating']=e.get('recommendationKey')
        if not r.get('num_analysts'):      r['num_analysts']=e.get('numberOfAnalystOpinions')
        if not r.get('trailing_pe'):       r['trailing_pe']=e.get('trailingPE')
        if not r.get('sector'):            r['sector']=e.get('sector')
        if not r.get('industry'):          r['industry']=e.get('industry')
        r.setdefault('est_rev_direction', 'neutral')
        # revisions_score from the 4 canonical sub-signals (same recipe as compute_forward_axis)
        subs=[num(r.get(k)) for k in ('score_f_eps_trend','score_f_rev_est','score_b_est_rev','revision_runway')]
        subs=[v for v in subs if v is not None]
        r['revisions_score']=round(100.0*sum(subs)/(len(subs)*2.0),1) if subs else None
        try: r.update(ss.source_score_components_for_row(r))
        except Exception as ex: r['screen_source']=None; r['_src_err']=str(ex)[:60]
    ok=[r for r in rows if num(r.get('screen_source')) is not None]
    ok.sort(key=lambda r:-num(r['screen_source']))
    floor=cfg.SUMMARY_SOURCE_FLOOR
    new_sel=[r for r in ok if ss.summary_eligible(r) and num(r['screen_source'])>=floor]
    old_sel=[r for r in rows if r.get('_was_summary')]
    report[g]={'rows':rows,'ranked':ok,'new_sel':[r['ticker'] for r in new_sel]}
    # old summary = names present in the SUMMARY tab of the workbook (had current_price from SUMMARY map)
    print(f"\n=== {g} ===  rankable {len(rows)} | scored {len(ok)} | NEW SUMMARY {len(new_sel)} (floor {floor})")
    for i,r in enumerate(new_sel[:14],1):
        print(f"   {i:2d} {r['ticker']:9s} src={num(r['screen_source']):5.1f} fwd={num(r['forward_axis_score']):5.1f} "
              f"tot={str(int(num(r.get('total_score')) or 0)):>2s}/50 {str(r.get('momentum_state') or '-'):13s}{str(r.get('industry') or '')[:26]}")
json.dump({g:{'new_sel':v['new_sel']} for g,v in report.items()}, open('/tmp/restamp_newsel.json','w'), indent=1)
