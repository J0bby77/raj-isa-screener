#!/usr/bin/env bash
# run_s5_shadow.sh — §3.1 full shadow run driver for the Composio REMOTE sandbox.
# Prepared 24-Jun-2026 for the S5 activation runbook (ISA_S5_Activation_Runbook_Jun2026.md §3).
#
# WHAT IT DOES: clones/pulls the s5-shadow branch, installs yfinance, then runs the
# Standard(SP500)+Energy+VCI screens TWICE each — once flags-OFF (baseline) and once
# flags-ON (shadow) — to separate output dirs, and produces a golden-master diff so the
# only deltas should be the intended S5 changes. Flags are flipped in a LOCAL working copy
# of scoring_config.py and reverted after every screen; the committed repo stays flags-OFF.
#
# WHY REMOTE: the non-batched energy/VCI screeners + the 500-name SP500 screen exceed the
# local 45s sandbox; the Composio remote sandbox has a 180s/cell budget and an un-throttled
# Yahoo path (full 28-name energy = ~19s remotely vs local timeout).
#
# USAGE (run cell-by-cell in COMPOSIO_REMOTE_BASH_TOOL, each <180s):
#   bash run_s5_shadow.sh setup           # clone + pip install + compile check
#   bash run_s5_shadow.sh sp500 baseline  # repeat until it prints ALL_DONE
#   bash run_s5_shadow.sh sp500 shadow    # repeat until ALL_DONE (flags ON)
#   bash run_s5_shadow.sh energy baseline ; bash run_s5_shadow.sh energy shadow
#   bash run_s5_shadow.sh vci             # VCI final-gates OFF vs ON
#   bash run_s5_shadow.sh diff sp500      # golden-master diff baseline vs shadow
set -uo pipefail
ISA="$HOME/isa"; DATE="2026-06-24"
FLAGS="RELAX_GM_GATE RELAX_FCF_GATE RELAX_CAGR_GATE RELAX_PARTA_HARDGATES RELAX_ND_MANDATORY FORWARD_ELIGIBILITY FORWARD_AXIS_IN_RANKING SUMMARY_COUNT_BASED BUILD_ACTION_STACK FLUID_POOL_DECAY ENERGY_VALUATION_PARITY"

flip(){ python3 - "$1" <<'PY'
import re,sys,os
state=sys.argv[1]; p=os.path.join(os.environ['HOME'],'isa','scoring_config.py')
fl="RELAX_GM_GATE RELAX_FCF_GATE RELAX_CAGR_GATE RELAX_PARTA_HARDGATES RELAX_ND_MANDATORY FORWARD_ELIGIBILITY FORWARD_AXIS_IN_RANKING SUMMARY_COUNT_BASED BUILD_ACTION_STACK FLUID_POOL_DECAY ENERGY_VALUATION_PARITY".split()
s=open(p,encoding='utf-8').read(); frm,to=('False','True') if state=='on' else ('True','False')
for v in fl: s=re.sub(rf'^({v}\s*=\s*){frm}', r'\1'+to, s, flags=re.M)
open(p,'w',encoding='utf-8').write(s)
print('flags',state,'->',sum(1 for v in fl if re.search(rf'^{v}\s*=\s*True',open(p,encoding='utf-8').read(),re.M)),'ON')
PY
}

cmd="${1:-}"; arg="${2:-}"
case "$cmd" in
setup)
  cd "$HOME" && rm -rf isa && git clone --depth 1 -b s5-shadow https://github.com/J0bby77/raj-isa-screener.git isa
  pip install --quiet yfinance pandas numpy openpyxl lxml requests
  cd "$ISA"; cp scoring_config.py scoring_config.PRISTINE.bak
  fail=0; for f in *.py; do python3 -m py_compile "$f" || fail=1; done
  [ $fail -eq 0 ] && echo "SETUP OK: $(ls *.py|wc -l) modules compile; flags-ON=$(grep -cE '= True' <(grep -E '^(RELAX_|FORWARD_|SUMMARY_COUNT|BUILD_ACTION|FLUID_POOL|ENERGY_VAL)' scoring_config.py))" ;;
sp500)
  cd "$ISA"; OUT="/tmp/sp_$arg"; mkdir -p "$OUT"
  [ "$arg" = shadow ] && flip on
  # run as many batches as fit in the cell budget; call repeatedly until ALL_DONE
  for i in $(seq 1 40); do
    o=$(python3 screener_local.py --group SP500 --date "$DATE" --outputs "$OUT" --inv-dir . --partial "$OUT/p.json" --skip-preflight 2>&1 | tail -1); echo "$o"
    echo "$o" | grep -q ALL_DONE && break
  done
  cp scoring_config.PRISTINE.bak scoring_config.py; echo "reverted flags-ON=$(grep -cE '= True' <(grep -E '^(RELAX_|FORWARD_|SUMMARY|BUILD_ACTION|FLUID_POOL|ENERGY_VAL)' scoring_config.py))" ;;
energy)
  cd "$ISA"; OUT="/tmp/en_$arg"; mkdir -p "$OUT"
  [ "$arg" = shadow ] && flip on
  python3 energy_screener.py --date "$DATE" --outputs "$OUT" --inv-dir . 2>&1 | tail -6
  cp scoring_config.PRISTINE.bak scoring_config.py; echo "reverted." ;;
vci)
  cd "$ISA"
  python3 vci_acs_scorer.py RXRX ABCL ONT.L --mgmt-unstable RXRX --json-out /tmp/vci_base.json 2>&1 | tail -2
  python3 vci_acs_scorer.py RXRX ABCL ONT.L --mgmt-unstable RXRX --final-gates --json-out /tmp/vci_shadow.json 2>&1 | tail -2
  echo "compare /tmp/vci_base.json vs /tmp/vci_shadow.json (shadow rows gain vci_final_gates)" ;;
diff)
  python3 - "$arg" <<'PY'
import csv,glob,sys
g=sys.argv[1]; b=glob.glob(f'/tmp/{g}_baseline/*full_data.csv'); s=glob.glob(f'/tmp/{g}_shadow/*full_data.csv')
if not(b and s): print('missing outputs'); sys.exit()
B={r['ticker']:r for r in csv.DictReader(open(b[0]))}; S={r['ticker']:r for r in csv.DictReader(open(s[0]))}
print('baseline n=',len(B),'shadow n=',len(S))
print('NEW in shadow (admitted):',sorted(set(S)-set(B))[:40])
from collections import defaultdict
dc=defaultdict(list)
for t in set(B)&set(S):
    for k in B[t]:
        if B[t].get(k)!=S[t].get(k): dc[k].append(t)
print('cols changed on pass-both names:', {k:len(v) for k,v in sorted(dc.items())})
PY
  ;;
*) echo "usage: bash run_s5_shadow.sh {setup|sp500 baseline|sp500 shadow|energy baseline|energy shadow|vci|diff sp500}" ;;
esac
