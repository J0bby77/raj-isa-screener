#!/usr/bin/env python3
"""
bottleneck_fv.py — VCI win-case fair value, asymmetry, floor selection, deployment eligibility. v2.

Spec: VCI_Forward_Led_Framework_Implementation_Jul2026.md §9.1/§10/§4 (FWDVCI) +
      VCI_Framework_Enhancements_Implementation_Jul2026.md E1/E2/E7 (v2).

v2 CHANGES
  E1 — `derive_floor(...)`: probability-weighted floor. applied = max(A_min(p,L,h), fixed 2.0/2.5),
       A_min = 1 + [(1-p)L + h]/p, h = (1+req_annual)^T - 1, T = max(days_to_catalyst/365, 0.5).
       p/L priors from vci_base_rates.json (learning-overridden once its gate passes). The max()
       clamp means the EV math can only RAISE the bar above the sleeve-character minimum, never lower.
       Gated by VCI_FLOOR_MODE ("fixed" default -> FWDVCI behaviour until the P6 flip).
  E2 — `compute_bottleneck_fv_ci(...)`: FV confidence interval (P25/P50/P75) by perturbing the two
       highest-leverage inputs; eligibility uses the conservative P25 asymmetry. Scalar-only FV ->
       estimated -> manual confirm. Analyst-FV cross-check band -> warn/manual.
  E7 — `compression_cause(...)`: split asymmetry compression into price-up (harvest) vs FV-down
       (thesis erosion) using persisted fv_prev / price_prev.

Pure functions, stdlib-only (+ optional scoring_config). Safe to import anywhere.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import json
import math
import os

try:
    import scoring_config as cfg
except Exception:
    cfg = object()


# --- config accessors -----------------------------------------------------------------------
def _c(name, default):
    return getattr(cfg, name, default)


def _floor_platform() -> float:     return float(_c("VCI_FV_ASYMMETRY_MIN_PLATFORM", 2.0))
def _floor_single() -> float:       return float(_c("VCI_FV_ASYMMETRY_MIN_SINGLE", 2.5))
def _deploy_threshold() -> float:   return float(_c("VCI_DEPLOY_THRESHOLD", 75))


FV_INPUT_FIELDS = (
    "latent_tam_usd_bn", "capture_share", "steady_margin",
    "exit_multiple", "fully_diluted_shares", "fx_to_local",
)


# --- E1 base-rate priors (vci_base_rates.json, cached) --------------------------------------
_BR_CACHE = {"path": None, "data": None}


def _base_rates() -> dict:
    path = _c("VCI_BASE_RATES_PATH", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                            "vci_base_rates.json")
    if _BR_CACHE["path"] == path and _BR_CACHE["data"] is not None:
        return _BR_CACHE["data"]
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    _BR_CACHE["path"], _BR_CACHE["data"] = path, data
    return data


def lookup_priors(asset_structure: Optional[str], catalyst_type: Optional[str] = None):
    """Return (p_thesis, L) from vci_base_rates.json, falling back to config priors then defaults."""
    struct = str(asset_structure or "single_asset").lower()
    ctype = str(catalyst_type or "_default").lower()
    br = _base_rates()
    p = None
    pmap = br.get("p_thesis", {})
    for key in (f"{struct}/{ctype}", f"{struct}/_default"):
        if key in pmap and isinstance(pmap[key], dict):
            p = pmap[key].get("p"); break
    if p is None:
        p = _c("VCI_P_THESIS_PRIORS", {}).get(f"{struct}/_default")
    L = None
    lmap = br.get("L_by_structure", {})
    if struct in lmap and isinstance(lmap[struct], dict):
        L = lmap[struct].get("L")
    if L is None:
        L = _c("VCI_L_PRIORS", {"platform": 0.35, "single_asset": 0.60}).get(struct)
    return p, L


def select_floor(asset_structure: Optional[str]) -> float:
    """FWDVCI fixed tier — the sleeve-character minimum and the E1 clamp floor."""
    return _floor_platform() if str(asset_structure).lower() == "platform" else _floor_single()


def derive_floor(asset_structure: Optional[str], catalyst_type: Optional[str] = None,
                 p_thesis: Optional[float] = None, L: Optional[float] = None,
                 days_to_catalyst: Optional[float] = None, mode: Optional[str] = None,
                 req_annual: Optional[float] = None):
    """E1 applied floor. Returns (floor, floor_source). mode 'fixed' -> FWDVCI tier."""
    fixed = select_floor(asset_structure)
    mode = (mode or _c("VCI_FLOOR_MODE", "fixed"))
    if mode != "derived":
        return fixed, "fixed"
    if p_thesis is None or L is None:
        lp, ll = lookup_priors(asset_structure, catalyst_type)
        p_thesis = p_thesis if p_thesis is not None else lp
        L = L if L is not None else ll
    if p_thesis is None or L is None or p_thesis <= 0:
        return fixed, "prior_default"          # no odds -> fall back, never auto-deploy on a guess
    T = max((float(days_to_catalyst) / 365.0) if days_to_catalyst else 1.0, 0.5)
    req = float(req_annual if req_annual is not None else _c("VCI_REQUIRED_ANNUAL_RETURN", 0.14))
    h = (1.0 + req) ** T - 1.0
    a_min = 1.0 + ((1.0 - p_thesis) * L + h) / p_thesis
    applied = min(max(a_min, fixed), float(_c("VCI_FLOOR_MAX", 4.0)))
    return round(applied, 4), "derived"


@dataclass
class BottleneckFV:
    bottleneck_ev_usd_bn: Optional[float] = None
    bottleneck_fv_per_share: Optional[float] = None
    fv_asymmetry: Optional[float] = None
    fv_source: str = "estimated"
    asset_structure: str = "single_asset"
    floor: float = field(default_factory=_floor_single)
    floor_source: str = "fixed"
    missing_inputs: tuple = ()
    # E2 confidence interval
    fv_p25: Optional[float] = None
    fv_p50: Optional[float] = None
    fv_p75: Optional[float] = None
    fv_asymmetry_p25: Optional[float] = None
    fv_asymmetry_p50: Optional[float] = None
    fv_crosscheck_warn: bool = False
    raw: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _missing(inputs: dict) -> tuple:
    miss = []
    for k in FV_INPUT_FIELDS:
        v = inputs.get(k)
        if v is None:
            miss.append(k)
        elif k != "fx_to_local" and (not isinstance(v, (int, float)) or v <= 0):
            miss.append(k)
    return tuple(miss)


def compute_bottleneck_fv(inputs: dict, current_price: Optional[float],
                          asset_structure: Optional[str] = None,
                          analyst_fv_per_share: Optional[float] = None) -> BottleneckFV:
    """WIN-case fair value + asymmetry (§10.1)."""
    struct = (asset_structure or inputs.get("asset_structure") or "single_asset")
    floor = select_floor(struct)
    miss = _missing(inputs)
    res = BottleneckFV(asset_structure=str(struct).lower(), floor=floor, missing_inputs=miss)
    if miss:
        res.fv_source = "estimated"
        res.raw = f"ESTIMATED (missing: {', '.join(miss)}) — manual confirm required before deploy"
        return res
    tam = float(inputs["latent_tam_usd_bn"]); cap = float(inputs["capture_share"])
    mar = float(inputs["steady_margin"]); mult = float(inputs["exit_multiple"])
    shares = float(inputs["fully_diluted_shares"]); fx = float(inputs.get("fx_to_local", 1.0) or 1.0)
    ev = tam * cap * mar * mult
    fv_ps = (ev * 1e9 / shares) * fx
    res.bottleneck_ev_usd_bn = round(ev, 4)
    res.bottleneck_fv_per_share = round(fv_ps, 4)
    res.fv_source = "modeled"
    if current_price and current_price > 0:
        res.fv_asymmetry = round(fv_ps / float(current_price), 4)
    xcheck = ""
    if analyst_fv_per_share:
        gap = (fv_ps / float(analyst_fv_per_share) - 1.0) * 100
        xcheck = f"; analyst-FV xcheck {analyst_fv_per_share:.2f} ({gap:+.0f}% vs modeled)"
    res.raw = (f"WIN-case FV {fv_ps:.2f} (EV ${ev:.2f}bn / {shares/1e6:.0f}m sh × fx {fx})"
               f" | asym {res.fv_asymmetry} vs floor {floor} [{res.asset_structure}]{xcheck}")
    return res


def compute_bottleneck_fv_ci(inputs: dict, current_price: Optional[float],
                             asset_structure: Optional[str] = None,
                             analyst_fv_per_share: Optional[float] = None) -> BottleneckFV:
    """E2: FV with a P25/P50/P75 confidence interval, obtained by perturbing the two highest-leverage
    inputs (capture_share, exit_multiple) by VCI_FV_CI_DELTAS. Eligibility should read the P25
    asymmetry. Sets fv_crosscheck_warn if the modeled FV deviates from an analyst FV by > MAXDEV."""
    base = compute_bottleneck_fv(inputs, current_price, asset_structure, analyst_fv_per_share)
    if base.fv_source != "modeled":
        return base                                    # missing inputs -> estimated, no CI
    # v2 calibration (Jul-2026): FV is a PRODUCT of INDEPENDENT uncertain factors. Combine their
    # fractional 1-sigma uncertainties in QUADRATURE (root-sum-square in log space) and take the
    # true 25th/75th percentile at +/- z*sigma. The prior approach drove BOTH inputs to their
    # downside together — a joint tail (~P5), not P25 — producing a punitive ~47% FV haircut. This
    # gives ~0.77x point at the default deltas (a ~23% haircut), so a name needs ~2.6x point
    # asymmetry (not ~3.8x) for its P25 to clear a 2.0x floor.
    deltas = _c("VCI_FV_CI_DELTAS", {"capture_share": 0.30, "exit_multiple": 0.25})  # per-input 1-sigma fractional
    sigma = math.sqrt(sum(float(d) ** 2 for d in deltas.values())) if deltas else 0.0
    z = float(_c("VCI_FV_CI_Z", 0.6745))          # 25th/75th percentile z-score (standard normal)
    fv0 = base.bottleneck_fv_per_share
    base.fv_p50 = fv0
    base.fv_p25 = round(fv0 * math.exp(-z * sigma), 4)
    base.fv_p75 = round(fv0 * math.exp(z * sigma), 4)
    base.fv_asymmetry_p50 = base.fv_asymmetry
    if current_price and current_price > 0:
        base.fv_asymmetry_p25 = round(base.fv_p25 / float(current_price), 4)
    if analyst_fv_per_share and base.bottleneck_fv_per_share:
        dev = abs(base.bottleneck_fv_per_share / float(analyst_fv_per_share) - 1.0)
        base.fv_crosscheck_warn = dev > float(_c("VCI_FV_CROSSCHECK_MAXDEV", 0.40))
    return base


@dataclass
class DeployEligibility:
    deploy_eligible: bool
    require_manual_confirm: bool
    reasons: tuple
    fv_asymmetry: Optional[float]
    floor: float
    adjusted_acs: Optional[float]


def evaluate_deploy_eligibility(*, acs_total: Optional[float], fv: BottleneckFV,
                                has_catalyst: bool, mgmt_unstable: bool, falls_on_beat: bool,
                                mgmt_penalty: Optional[float] = None,
                                elig_pctile: Optional[str] = None,
                                require_structured: Optional[bool] = None,
                                has_structured_inputs: bool = True) -> DeployEligibility:
    """§4 forward-led eligibility. v2: uses the P25 asymmetry (E2) when available; scalar-only FV or
    cross-check warning -> manual confirm; floor is whatever `fv.floor` was set to (derive_floor)."""
    pen = float(mgmt_penalty if mgmt_penalty is not None else _c("VCI_MGMT_PENALTY", 5.0))
    thr = _deploy_threshold()
    reasons = []
    adj = (acs_total if acs_total is not None else 0.0)
    if mgmt_unstable:
        adj -= pen; reasons.append(f"F1 mgmt_instability:-{pen:g}")

    # E2: eligibility asymmetry = conservative P25 when present and configured
    pctile = (elig_pctile or _c("VCI_ASYM_ELIG_PCTILE", "p25"))
    asym = fv.fv_asymmetry
    if pctile == "p25" and fv.fv_asymmetry_p25 is not None:
        asym = fv.fv_asymmetry_p25

    blocked = False
    if falls_on_beat:
        blocked = True; reasons.append("F3 price_falls_on_beat:NO_DEPLOY")
    if not has_catalyst:
        blocked = True; reasons.append("F2 catalyst_unconfirmed")
    if adj < thr:
        blocked = True; reasons.append(f"acs {adj:.1f} < {thr:g} floor")
    if asym is None:
        blocked = True; reasons.append("fv_asymmetry unavailable")
    elif asym < fv.floor:
        blocked = True; reasons.append(f"fv_asymmetry {asym} < {fv.floor} floor ({fv.asset_structure}/{fv.floor_source})")

    require_structured = (_c("VCI_FV_REQUIRE_STRUCTURED", True) if require_structured is None else require_structured)
    manual = (fv.fv_source == "estimated")
    if manual:
        reasons.append("fv_source=estimated:manual_confirm")
    if require_structured and not has_structured_inputs:
        manual = True; reasons.append("fv_scalar_only:manual_confirm")   # E2 structured-input mandate
    if fv.fv_crosscheck_warn:
        manual = True; reasons.append("fv_analyst_crosscheck>maxdev:manual_confirm")   # E2

    eligible = (not blocked) and (not manual)
    if eligible and not reasons:
        reasons.append("eligible")
    return DeployEligibility(deploy_eligible=eligible, require_manual_confirm=manual,
                             reasons=tuple(reasons), fv_asymmetry=asym, floor=fv.floor,
                             adjusted_acs=round(adj, 1))


# --- E7 compression cause -------------------------------------------------------------------
def compression_cause(fv_now: Optional[float], fv_prev: Optional[float],
                      price_now: Optional[float], price_prev: Optional[float],
                      erosion_threshold: Optional[float] = None) -> str:
    """Classify why asymmetry compressed: 'price_up' (harvest/success), 'fv_down' (thesis erosion),
    'both', or 'none'. FV-down and price-up are opposite signals despite the same symptom."""
    thr = float(erosion_threshold if erosion_threshold is not None else _c("VCI_FV_EROSION_THRESHOLD", 0.15))
    fv_down = (fv_prev is not None and fv_now is not None and fv_prev > 0 and fv_now < fv_prev * (1.0 - thr))
    price_up = (price_prev is not None and price_now is not None and price_prev > 0 and price_now > price_prev * 1.05)
    if fv_down and price_up:
        return "both"
    if fv_down:
        return "fv_down"
    if price_up:
        return "price_up"
    return "none"


# --- inline self-test -----------------------------------------------------------------------
if __name__ == "__main__":
    abcl = dict(latent_tam_usd_bn=20.0, capture_share=0.12, steady_margin=0.35,
                exit_multiple=7.0, fully_diluted_shares=300e6, fx_to_local=1.0, asset_structure="platform")
    fv = compute_bottleneck_fv_ci(abcl, 8.11, asset_structure="platform")
    print("F-T5 ABCL:", fv.raw, "| P25", fv.fv_asymmetry_p25, "P50", fv.fv_asymmetry_p50)
    assert fv.fv_source == "modeled" and fv.fv_asymmetry_p25 < fv.fv_asymmetry_p50   # CI monotone

    # E1 derived floor: single-asset phase-2 (low p) rises above 2.5; platform clamps to 2.0
    fs, src = derive_floor("single_asset", "phase2_biotech", p_thesis=0.28, L=0.60, days_to_catalyst=550, mode="derived")
    fp, _ = derive_floor("platform", "revenue_ramp", p_thesis=0.55, L=0.35, days_to_catalyst=365, mode="derived")
    print(f"E1 floors: single_asset={fs} ({src})  platform={fp}")
    assert fs > 2.5 and fp == 2.0, (fs, fp)
    # fixed mode = FWDVCI
    assert derive_floor("single_asset", mode="fixed")[0] == 2.5

    # E2 eligibility uses P25; scalar-only -> manual
    fv.floor, fv.floor_source = 2.0, "fixed"
    e = evaluate_deploy_eligibility(acs_total=78, fv=fv, has_catalyst=True, mgmt_unstable=False, falls_on_beat=False)
    print("E2 ABCL@8.11 P25:", e.deploy_eligible, e.reasons)
    scal = BottleneckFV(bottleneck_fv_per_share=14.0, fv_asymmetry=1.73, fv_source="modeled",
                        asset_structure="platform", floor=2.0)
    e2 = evaluate_deploy_eligibility(acs_total=80, fv=scal, has_catalyst=True, mgmt_unstable=False,
                                     falls_on_beat=False, has_structured_inputs=False)
    assert e2.require_manual_confirm and not e2.deploy_eligible

    # E7 cause
    assert compression_cause(14.0, 14.0, 8.11, 5.25) == "price_up"      # price rose, FV flat
    assert compression_cause(11.0, 14.0, 8.0, 8.0) == "fv_down"          # FV revised down 21%
    print("bottleneck_fv v2 self-test PASSED")
