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
    base.fv_p25 