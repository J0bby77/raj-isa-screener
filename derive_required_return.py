#!/usr/bin/env python3
"""
derive_required_return.py — Fix Pack A19 v1 (12-Jul-2026). THE anchor derivation.

Solves the annualised return (monthly compounding, month-end contributions) required for the
current portfolio + contribution schedule to reach the floor (£1.0m) and stretch (£1.5m)
targets by target_date, then writes target_state.json with D1c guardrails applied:

  operative = clamp(derived_floor, 10.0, 18.0)
    derived > 18  -> guardrail_state = TARGET_ATTAINABILITY_REVIEW (never auto-ratchet gates)
    derived < 10  -> hard-floored at 10.0 (outperformance banks a buffer, never lowers the bar);
                     glidepath (B6) triggers on age/value ONLY, not on a low anchor.

Runs (D1b): inside the April pre-run (tax-year start) and on ANY contribution_schedule change
(edit the schedule + schedule_updated_at, rerun this; consistency_check.py A18 asserts
derived_at >= schedule_updated_at). Appends a derivation_history row on every write.

Usage:
  python3 derive_required_return.py                 # derive + write + history row
  python3 derive_required_return.py --check         # recompute, compare to stored, NO write (exit 1 on drift > 0.2pp)
  python3 derive_required_return.py --portfolio-value 150000 --value-date 2026-12-31   # override inputs
  python3 derive_required_return.py --selftest      # U-A19 unit fixtures

Stdlib only. Consumers read target_state.json via scoring_config's loader (hard fallback + warning).
"""
import argparse, json, os, sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "target_state.json")
OPERATIVE_FLOOR_PCT = 10.0   # D1c amended (Raj 12-Jul)
OPERATIVE_CAP_PCT = 18.0     # D1c: above this -> human review, not mechanical ratchet


def _months_between(d0: date, d1: date) -> int:
    return (d1.year - d0.year) * 12 + (d1.month - d0.month)


def _add_months(d0: date, k: int) -> date:
    y, m = divmod((d0.year * 12 + d0.month - 1) + k, 12)
    return date(y, m + 1, 1)


def _monthly_amount(schedule, when: date) -> float:
    """Contribution in force at month `when` (schedule = [{from, monthly_gbp}], sorted or not)."""
    amt = 0.0
    for seg in sorted(schedule, key=lambda s: s["from"]):
        if date.fromisoformat(seg["from"]) <= when:
            amt = float(seg["monthly_gbp"])
    return amt


def fv_at_rate(m: float, principal: float, start: date, end: date, schedule) -> float:
    """Future value at monthly rate m; contributions land at the START of each month k=1..n
    (amount per the schedule segment in force that month) and compound to end."""
    n = _months_between(start, end)
    fv = principal * (1 + m) ** n
    for k in range(1, n + 1):
        c = _monthly_amount(schedule, _add_months(start, k))
        if c:
            fv += c * (1 + m) ** (n - k)
    return fv


def solve_required_annual_pct(target: float, principal: float, start: date, end: date,
                              schedule, tol=1e-10) -> float:
    """Bisect the monthly rate so FV == target; return effective annual % ((1+m)^12 - 1)."""
    lo, hi = -0.02, 0.08          # -21%..+152% annual — generous, monotone in m
    if fv_at_rate(hi, principal, start, end, schedule) < target:
        raise ValueError("target unreachable inside solver bounds")
    for _ in range(200):
        mid = (lo + hi) / 2
        if fv_at_rate(mid, principal, start, end, schedule) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    m = (lo + hi) / 2
    return round(((1 + m) ** 12 - 1) * 100, 1)


def apply_guardrails(derived_floor_pct: float):
    """D1c. Returns (operative_pct, guardrail_state)."""
    if derived_floor_pct > OPERATIVE_CAP_PCT:
        return OPERATIVE_CAP_PCT, "TARGET_ATTAINABILITY_REVIEW"
    if derived_floor_pct < OPERATIVE_FLOOR_PCT:
        return OPERATIVE_FLOOR_PCT, "OK"   # buffer banked; glidepath is age/value-driven only (B6)
    return derived_floor_pct, "OK"


def derive(state: dict, portfolio_value=None, value_date=None) -> dict:
    pv = float(portfolio_value if portfolio_value is not None else state["portfolio_value_gbp"])
    vd = date.fromisoformat(value_date or state["portfolio_value_date"])
    td = date.fromisoformat(state["target_date"])
    sched = state["contribution_schedule"]
    floor = solve_required_annual_pct(float(state["target_floor_gbp"]), pv, vd, td, sched)
    stretch = solve_required_annual_pct(float(state["target_stretch_gbp"]), pv, vd, td, sched)
    operative, gstate = apply_guardrails(floor)
    return {"portfolio_value_gbp": pv, "portfolio_value_date": vd.isoformat(),
            "required_return_floor_pct": floor, "required_return_stretch_pct": stretch,
            "required_return_operative_pct": operative, "guardrail_state": gstate}


def _selftest():
    sched0 = [{"from": "2026-07-01", "monthly_gbp": 0},
              {"from": "2027-01-01", "monthly_gbp": 1250}]
    s, e = date(2026, 6, 30), date(2037, 12, 31)
    # U-A19-1: current derivation lands near the review's 13.9 / 18.7 (±0.3pp)
    f = solve_required_annual_pct(1_000_000, 144_342.19, s, e, sched0)
    g = solve_required_annual_pct(1_500_000, 144_342.19, s, e, sched0)
    assert abs(f - 13.9) <= 0.3, f"floor {f} != ~13.9"
    assert abs(g - 18.7) <= 0.4, f"stretch {g} != ~18.7"
    # U-A19-2: inversion — FV at the solved rate reproduces the target (<0.5% error)
    m = (1 + f / 100) ** (1 / 12) - 1
    fv = fv_at_rate(m, 144_342.19, s, e, sched0)
    assert abs(fv - 1_000_000) / 1_000_000 < 0.005, f"inversion FV {fv:.0f}"
    # U-A19-3: guardrail branches (D1c)
    assert apply_guardrails(19.4) == (18.0, "TARGET_ATTAINABILITY_REVIEW")
    assert apply_guardrails(8.2) == (10.0, "OK")
    assert apply_guardrails(13.9) == (13.9, "OK")
    # U-A19-4: monotonicity — more capital => lower requirement
    f2 = solve_required_annual_pct(1_000_000, 250_000, s, e, sched0)
    assert f2 < f, (f2, f)
    # U-A19-5: schedule segments respected — zero-forever needs more than resume-Jan-27
    f3 = solve_required_annual_pct(1_000_000, 144_342.19, s, e, [{"from": "2026-07-01", "monthly_gbp": 0}])
    assert f3 > f, (f3, f)
    print(f"derive_required_return SELF-TEST OK (floor {f} / stretch {g} / zero-contrib {f3})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="recompute vs stored; no write; exit 1 on >0.2pp drift")
    ap.add_argument("--portfolio-value", type=float, default=None)
    ap.add_argument("--value-date", default=None)
    ap.add_argument("--trigger", default="manual run")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return

    with open(a.state, encoding="utf-8") as f:
        state = json.load(f)
    out = derive(state, a.portfolio_value, a.value_date)
    print(json.dumps(out, indent=2))

    if a.check:
        drift = abs(out["required_return_floor_pct"] - float(state["required_return_floor_pct"]))
        print(f"drift vs stored floor: {drift:.1f}pp")
        if drift > 0.2:
            print("CHECK FAIL — stored anchor stale; rerun without --check to re-derive")
            sys.exit(1)
        print("CHECK OK")
        return

    today = date.today().isoformat()
    state.update(out)
    state["derived_at"] = today
    state["derivation"] = "monthly-compounded solve, derive_required_return.py v1"
    # next scheduled re-derivation = next April pre-run (tax-year start, D1b)
    state["next_derivation_due"] = f"{date.today().year + 1}-04-01" if date.today().month >= 4 else f"{date.today().year}-04-01"
    state.setdefault("derivation_history", []).append(
        {"derived_at": today, "floor_pct": out["required_return_floor_pct"],
         "stretch_pct": out["required_return_stretch_pct"],
         "operative_pct": out["required_return_operative_pct"],
         "portfolio_value_gbp": out["portfolio_value_gbp"],
         "schedule": "; ".join(f"{s['monthly_gbp']}/mo from {s['from']}" for s in state["contribution_schedule"]),
         "trigger": a.trigger})
    with open(a.state, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    print(f"WROTE {a.state} (guardrail_state={out['guardrail_state']}, history rows="
          f"{len(state['derivation_history'])})")


if __name__ == "__main__":
    main()
