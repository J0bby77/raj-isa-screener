#!/usr/bin/env python3
"""
expected_return.py — Fix Pack A2 (12-Jul-2026) + D-24 (09-Aug-2026). THE single E[r] implementation.

expected_return_12_24m = er_growth + er_rerate + er_yield   (annualised, % p.a.)
  er_growth = forward EPS growth (2y annualised; fallback: fwd-growth proxy, then rev growth x 0.8)
  er_rerate = clamp((anchor_multiple / current_multiple) ** 0.5 - 1, -CAP, +CAP), then
              C1-shaped: zeroed inside a neutral band, and the DE-RATE side damped by regime
              (RISK_ON 0.25 / LATE_CYCLE 0.50 / RISK_OFF 1.0). The RE-RATE credit for a cheap
              name is never damped. Set ER_RERATE_MODE="legacy" to restore the raw monotonic term.
  er_yield  = dividend_yield + net_buyback_yield (from 3y share-count change)

D-24 (09-Aug-2026) — WHAT CHANGED AND WHY
-----------------------------------------
Until today the row adapter resolved `current_multiple` from `trailing_pe` (populated on 8.7% of
SP500 rows) and the anchor from `val_hist_pe_anchor` (8.3%). A missing term contributed **0**, so
on 92-97% of every universe screened, E[r] silently asserted THAT THE MULTIPLE WOULD NOT CHANGE.
Meanwhile `fwd_pe` is populated on 100% of rows and Part B already scores valuation from it.
Measured consequence: 23 of 312 deploy verdicts (7.4%) flip once an anchor is supplied from data
already on disk, and the fundamentals evidence route was UNREACHABLE on 92% of names because the
attainable confidence ceiling without a re-rate is 0.70 against EVIDENCE_ER_CONF_MIN = 0.75.

Three changes:
  1. The multiple is chosen BY SECTOR from a DECLARED map (`ER_MULTIPLE_BY_SECTOR`) — nothing in
     it is fitted, so it adds no degrees of freedom.
  2. TWO anchors. `anchor_xs` = the sector median of the same field, computed once per screen and
     PERSISTED (never recomputed downstream on a handful of rows); `anchor_own` = own history, as
     before. Where both exist they must AGREE within ER_ANCHOR_AGREE_BAND, and a breach is
     published as `er_anchor_divergence` — the "two independent derivations must agree" rule at an
     artefact boundary. That is why the cross-sectional anchor does not simply become a new
     unchecked number.
  3. Where NO sector-appropriate multiple and no fallback resolve, the re-rate is **UNMEASURED**,
     not 0 (§5). `er_status` carries that to the gates, which treat it as NO_DATA — pass + flagged,
     never a pass on a fabricated number, and never eligible for `full` size.

⚑ CONSUMER LIST — VERIFIED ON DISK 09-Aug-2026. The old docstring claimed two consumers. There are
NINE live call sites, enumerated in `scoring_config.ER_CALLSITE_MANIFEST` and asserted by
`tests_jul2026/test_d24_expected_return.py`. `expected_return_for_row` therefore REFUSES to run
without an explicit anchor-table decision (`anchor_table=` or `allow_missing_anchor_table=True`):
a caller that is not updated fails loudly instead of silently keeping the defective behaviour.

⚑ ON `fwd_eps_growth` AND CONSENSUS (amended 09-Aug-2026, Stage 5). The previous docstring said
"consensus targets are sentiment data (Correction #5), never inputs here". That was contradicted by
the code: `fwd_eps_growth` is present on 91.3% of rows and drives ~90% of E[r]. The distinction is
real and is now stated rather than implied — a forward estimate of EARNINGS is an input to a
fundamental expectation; a consensus PRICE TARGET is an opinion about the multiple the market will
pay, which is the very thing E[r] is trying to estimate independently. Price targets remain
excluded (they survive as `display_target_gap` only). The input is unchanged; the claim is.

Gate consumption (P2, T1_QUALIFICATION_MODE): er >= scoring_config.ER_DEPLOY_FLOOR or documented
catalyst; `er_status == "unmeasured"` -> NO_DATA per t1_gates doctrine.
Stdlib only (pandas/numpy NEVER imported here). Self-test: python3 expected_return.py
"""
from __future__ import annotations

import json as _json
import math as _math
import os as _os

try:
    import scoring_config as _cfg
except Exception:                    # standalone/self-test safety — never block a screen
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


_CAP = float(_c("ER_RERATE_CAP", 0.10))
_RERATE_MODE = str(_c("ER_RERATE_MODE", "regime_aware"))
_NEUTRAL_BAND = float(_c("ER_RERATE_NEUTRAL_BAND", 0.05))
_REGIME_DAMPING = dict(_c("ER_RERATE_REGIME_DAMPING", {}) or {}) or {
    "RISK_ON": 0.25, "LATE_CYCLE": 0.50, "RISK_OFF": 1.00, "RECOVERY": 1.00}

# ISA-0377. ONE HOME: these were module literals; they are now read from scoring_config like
# every other operative threshold. _G_HI is now an ASYMPTOTE, not a value names are pinned to.
_G_HI = float(_c("ER_GROWTH_CAP_PP", 50.0))
_G_LO = float(_c("ER_GROWTH_FLOOR_PP", -25.0))
_KNEE_ON = bool(_c("ER_GROWTH_KNEE_ENABLED", True))
_KNEE_Q = float(_c("ER_GROWTH_KNEE_QUANTILE", 66.667))
_KNEE_FLOOR = float(_c("ER_GROWTH_KNEE_FLOOR_PP", 15.0))
_KNEE_CEIL = float(_c("ER_GROWTH_KNEE_CEIL_PP", 40.0))
_KNEE_MIN_N = int(_c("ER_GROWTH_KNEE_MIN_N", 30))


class AnchorTableMissing(RuntimeError):
    """Raised when a caller needs the persisted anchor table and it is not there.

    §3.3 THE PRE-RUN TRAP: the pre-run path runs on a handful of rows, so a sector median computed
    there is nonsense. It MUST load the table the screen persisted, and it MUST fail if absent — it
    may not recompute, and it may not fall back to "no re-rate", because that silently reinstates
    the defect this module exists to remove.
    """


def _num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            if v != v:               # NaN
                return None
        except Exception:            # noqa: BLE001
            pass
        return float(v)
    try:
        s = str(v).replace("$", "").replace("£", "").replace(",", "").replace("%", "").strip()
        if s == "" or s.lower() in ("nan", "none", "null", "-", "—"):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _here(base_dir=None):
    return base_dir or _os.path.dirname(_os.path.abspath(__file__))


# ══════════════════════════════════════════════════════════════════════════════════════════════
# STAGE 1 — THE ANCHOR TABLE
# `compute_expected_return` is a pure per-row function, but a cross-sectional anchor needs the
# whole frame. Medians are therefore NEVER computed inside the row function.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def multiple_field_for_sector(sector):
    """DECLARED sector -> multiple field. Not fitted; see scoring_config.ER_MULTIPLE_BY_SECTOR."""
    m = dict(_c("ER_MULTIPLE_BY_SECTOR", {}) or {})
    return m.get(str(sector or "").strip(), str(_c("ER_MULTIPLE_DEFAULT", "fwd_pe")))


def _sane(field, v):
    lo, hi = dict(_c("ER_MULTIPLE_SANITY", {}) or {}).get(field, (0.0, 200.0))
    n = _num(v)
    return n if (n is not None and lo < n < hi) else None


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    return float(s[n // 2]) if n % 2 else float((s[n // 2 - 1] + s[n // 2]) / 2.0)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ISA-0377 — THE GROWTH BOUND: cross-sectional basis, monotone shape
# ══════════════════════════════════════════════════════════════════════════════════════════════

def growth_transform(g, knee=None, hi=None, lo=None):
    """Map raw forward growth to the growth term. Returns (value, basis).

    THREE PROPERTIES, and each one is asserted rather than asserted-in-prose:
      MONOTONE   strictly increasing in g above the knee, so two names that differ in growth
                 differ in E[r]. The flat cut this replaces mapped an 80-16,971% span to a point.
      BOUNDED    tanh -> 1, so the value approaches `hi` and never reaches it. The LEVEL is
                 therefore unchanged: nothing can contribute more growth than the old cap allowed.
      IDENTITY   below the knee the function is g. The ordinary middle of the distribution is
                 untouched, so this is not a re-scoring of the whole universe.

    `knee=None`, a knee outside (lo, hi), or ER_GROWTH_KNEE_ENABLED=False all fall back to the
    LEGACY FLAT CUT — named in the basis, never silent (R4.13 rollback, and the honest state when
    a frame is too small to state its own quantile).
    """
    if g is None:
        return None, "MISSING"
    hi = _G_HI if hi is None else float(hi)
    lo = _G_LO if lo is None else float(lo)
    g = float(g)
    if g < lo:
        # The downside stays a flat floor and that is a MEASURED refusal (scoring_config, ISA-0377):
        # the lower tercile is positive on 5 of 6 retained frames, so no cross-sectional knee exists
        # down here, and no capital is ever ranked out of the bottom of a screen.
        return lo, "FLOOR_ABSOLUTE"
    if not _KNEE_ON or knee is None or not (lo < float(knee) < hi):
        return (hi, "CEIL_ABSOLUTE") if g > hi else (g, "RAW")
    knee = float(knee)
    if g <= knee:
        return g, "RAW"
    span = hi - knee
    # ⚑ THE SHAPE IS arctan, NOT tanh, AND THE REASON IS MEASURED — see ISA-0381. Both have unit
    # slope at the knee (so the join is smooth) and both approach 1, but `tanh(u)` REACHES 1.0 in
    # double precision at u = 19.06. On the two Nasdaq frames the knee sits at the 40pp ceiling, so
    # span = 10pp and tanh saturates at raw growth of 230pp — which is the 90th percentile of that
    # very frame. The named transform would have reinstated the flat cut across the top decile of
    # the universe where the defect was worst. (2/pi)*arctan(pi*u/2) has the same unit slope and
    # does not reach 1.0 until u ~ 3.7e15, i.e. never on any real frame.
    u = (g - knee) / span
    v = (2.0 / _math.pi) * _math.atan(_math.pi * u / 2.0)
    out = knee + span * v
    if not out < hi:
        # Unreachable on any real frame, but a value that HAS saturated must never be reported as
        # though it were measured -- that is the whole defect. It is named, not silently returned.
        return hi, "CEIL_SATURATED"
    return out, "COMPRESSED_XS"


def _row_growth_pct(row, get=None):
    """The growth INPUT in percent units, resolved exactly as the row adapter resolves it.

    ⚑ One home. `fwd_eps_growth` is a FRACTION in the frame and `er_growth` is a PERCENT; reading
    one as the other understates the distribution by ~100x, which is the first thing
    er_clamp_diagnostic got wrong. The scale factors live in _KEYS and are read from there.
    """
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    for k, scale in _KEYS["fwd_eps_growth_pct"]:
        v = _num(g(row, k))
        if v is not None:
            return v * scale
    for k, scale in _KEYS["rev_growth_pct"]:
        v = _num(g(row, k))
        if v is not None:
            return v * scale * 0.8
    return None


def _quantile(sorted_vals, q_pct):
    """Linear-interpolation quantile. No numpy dependency on the screen path."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (q_pct / 100.0) * (n - 1)
    lo_i = int(_math.floor(pos))
    hi_i = min(lo_i + 1, n - 1)
    frac = pos - lo_i
    return sorted_vals[lo_i] * (1 - frac) + sorted_vals[hi_i] * frac


def build_growth_bounds(rows, get=None):
    """The frame's own growth knee, published beside the anchor table (ISA-0377).

    `knee_state` is the FALSIFIER and it is stored, not inferred later: a knee that is
    FLOOR_BOUND or CEIL_BOUND on a majority of frames means the cross-sectional basis has
    collapsed back into a constant and the item must re-raise.
    """
    vals = sorted(v for v in (_row_growth_pct(r, get) for r in (rows or [])) if v is not None)
    n = len(vals)
    out = {"quantile": _KNEE_Q, "n_growth": n, "min_n": _KNEE_MIN_N,
           "hi_pp": _G_HI, "lo_pp": _G_LO, "shape": "tanh", "enabled": _KNEE_ON,
           "knee_floor_pp": _KNEE_FLOOR, "knee_ceil_pp": _KNEE_CEIL,
           "downside_basis": ("ABSOLUTE — measured refusal: the lower tercile is positive on 5 of "
                              "6 retained frames, so no cross-sectional knee exists below zero"),
           "built_by": "expected_return.build_growth_bounds (ISA-0377)"}
    if n < _KNEE_MIN_N or not _KNEE_ON:
        # R2.10 — "I could not measure it" and "it is average" must not produce the same output.
        out.update({"knee_pp": None, "quantile_value_pp": (round(_quantile(vals, _KNEE_Q), 3)
                                                           if n else None),
                    "knee_state": ("DISABLED" if not _KNEE_ON else "INSUFFICIENT_N"),
                    "basis": "ABSOLUTE_LEGACY",
                    "reason": ("the frame cannot state its own quantile from %d growth values "
                               "(ER_GROWTH_KNEE_MIN_N=%d), so the legacy flat cut applies and says "
                               "so on every row" % (n, _KNEE_MIN_N)) if _KNEE_ON else
                              "ER_GROWTH_KNEE_ENABLED is False (R4.13 rollback)"})
        return out
    qv = _quantile(vals, _KNEE_Q)
    knee = min(max(qv, _KNEE_FLOOR), _KNEE_CEIL)
    out.update({
        "quantile_value_pp": round(qv, 3),
        "knee_pp": round(knee, 3),
        "knee_state": ("INTERIOR" if _KNEE_FLOOR < qv < _KNEE_CEIL else
                       ("FLOOR_BOUND" if qv <= _KNEE_FLOOR else "CEIL_BOUND")),
        "basis": "CROSS_SECTIONAL",
        "share_above_knee_pct": round(100.0 * sum(1 for v in vals if v > knee) / n, 2),
        "share_above_hi_pct": round(100.0 * sum(1 for v in vals if v > _G_HI) / n, 2),
        "share_below_lo_pct": round(100.0 * sum(1 for v in vals if v < _G_LO) / n, 2),
        "median_pp": round(_quantile(vals, 50.0), 3),
    })
    return out


def build_anchor_table(rows, *, run_date, group, get=None):
    """Build the cross-sectional anchor table ONCE per screen, from the whole frame.

    Sectors with n < ER_ANCHOR_MIN_SECTOR_N are NAMED in `excluded` with a reason, never dropped
    silently (T6 asserts covered + excluded == all sectors observed).
    """
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    min_n = int(_c("ER_ANCHOR_MIN_SECTOR_N", 5))
    by_sector_field, vals_by_sector, seen_by_sector = {}, {}, {}
    all_field_vals, filtered = {}, {}

    for r in rows or []:
        sec = str(g(r, "sector") or "").strip() or "UNKNOWN"
        fld = multiple_field_for_sector(sec)
        by_sector_field[sec] = fld
        seen_by_sector[sec] = seen_by_sector.get(sec, 0) + 1
        raw = g(r, fld)
        v = _sane(fld, raw)
        if v is None:
            if _num(raw) is not None:
                filtered[fld] = filtered.get(fld, 0) + 1     # present but outside the sanity band
        else:
            vals_by_sector.setdefault(sec, []).append(v)
        # whole-screen medians for every field we might fall back to
        for f2 in set(list(dict(_c("ER_MULTIPLE_BY_SECTOR", {}) or {}).values())
                      + [str(_c("ER_MULTIPLE_DEFAULT", "fwd_pe")),
                         str(_c("ER_MULTIPLE_FALLBACK", "price_fcf"))]):
            v2 = _sane(f2, g(r, f2))
            if v2 is not None:
                all_field_vals.setdefault(f2, []).append(v2)

    median_by_sector, n_by_sector, excluded = {}, {}, {}
    for sec, vals in sorted(vals_by_sector.items()):
        n_by_sector[sec] = len(vals)
        if len(vals) >= min_n:
            median_by_sector[sec] = round(_median(vals), 4)
        else:
            excluded[sec] = f"n={len(vals)} < ER_ANCHOR_MIN_SECTOR_N={min_n}"
    for sec in seen_by_sector:
        if sec not in median_by_sector and sec not in excluded:
            n_by_sector.setdefault(sec, 0)
            excluded[sec] = (f"no sane {by_sector_field.get(sec)} values "
                             f"(rows seen={seen_by_sector[sec]})")

    return {
        "as_of": run_date,
        "group": group,
        "basis": "sector_median",
        "multiple_by_sector": by_sector_field,
        "median_by_sector": median_by_sector,
        "n_by_sector": n_by_sector,
        "whole_screen_median": {f: round(_median(v), 4) for f, v in sorted(all_field_vals.items())},
        "excluded": excluded,
        "rows_in": len(rows or []),
        "sanity_filtered": filtered,          # counted and published, per §3.1
        # JSON-native (lists, not tuples) — a table that does not survive persist->load unchanged
        # is not the same table, and the pre-run reads the PERSISTED copy.
        "sanity_bands": {k: list(v) for k, v in (_c("ER_MULTIPLE_SANITY", {}) or {}).items()},
        # ISA-0377 — the frame's own growth knee travels WITH the table, for the same reason the
        # anchor evidence does: the pre-run reads the persisted copy weeks later and must apply
        # the SCREEN's cross-section, never one recomputed from a handful of pre-run rows (§3.3).
        "growth_bounds": build_growth_bounds(rows, get=get),
        "min_sector_n": min_n,
        "min_rows": int(_c("ER_ANCHOR_MIN_ROWS", 30)),
        # ⚑ FIT FOR ANCHORING. A cross-sectional anchor built from a handful of rows is not a
        # cross-sectional anchor. Discovered live 09-Aug-2026 on a 6-ticker ad-hoc run: the table
        # had zero sector medians and, unguarded, would have become the pointer the monthly
        # pre-run loads — §3.3's trap re-entering from the other side. Unfit tables are still
        # persisted (a record of what ran) but never anchor and never become `latest`.
        "fit_for_anchoring": bool(len(rows or []) >= int(_c("ER_ANCHOR_MIN_ROWS", 30))
                                  and median_by_sector),
        "built_by": "expected_return.build_anchor_table (D-24)",
    }


def _store_path(base_dir=None):
    return _os.path.join(_here(base_dir), str(_c("ER_ANCHOR_STORE", "er_anchor_store.json")))


def persist_anchor_table(table, base_dir=None):
    """Write the table beside the scripts (like score_panel.csv) so the PRE-RUN can read it.

    outputs/ is session-temp and is cleared; the pre-run runs weeks later. Keyed `group:as_of`,
    with a per-group `latest` pointer and a global `latest_any`.
    """
    p = _store_path(base_dir)
    try:
        with open(p, encoding="utf-8") as f:
            store = _json.load(f) or {}
    except Exception:                                        # noqa: BLE001
        store = {}
    store.setdefault("tables", {})
    store.setdefault("latest", {})
    key = f"{table.get('group')}:{table.get('as_of')}"
    store["tables"][key] = table
    # Only a FIT table may become the pointer a later run reads.
    if table.get("fit_for_anchoring"):
        store["latest"][str(table.get("group"))] = key
        store["latest_any"] = key
    # keep the store bounded: newest 40 tables
    if len(store["tables"]) > 40:
        for k in sorted(store["tables"], key=lambda k: str(store["tables"][k].get("as_of")))[:-40]:
            if k not in store["latest"].values() and k != store.get("latest_any"):
                store["tables"].pop(k, None)
    with open(p, "w", encoding="utf-8") as f:
        _json.dump(store, f, indent=2, default=str)
    return p


def load_anchor_table(group=None, base_dir=None, as_of=None, required=True, fit_only=True):
    """READ the persisted table. NEVER recompute (§3.3). Raises AnchorTableMissing when absent.

    group=None resolves to the most recently persisted table across groups, and the row records
    which screen's medians it used (`er_anchor_table_group`) — the cross-group substitution is
    made visible rather than hidden.
    """
    p = _store_path(base_dir)
    try:
        with open(p, encoding="utf-8") as f:
            store = _json.load(f) or {}
    except Exception as e:                                   # noqa: BLE001
        if required:
            raise AnchorTableMissing(
                f"E[r] anchor table store unreadable at {p} ({type(e).__name__}: {e}). "
                "The pre-run may NOT recompute sector medians from a handful of rows and may NOT "
                "fall back to no-re-rate — run the screen first (D-24 §3.3).") from e
        return None
    key = None
    if as_of and group:
        key = f"{group}:{as_of}"
    elif group:
        key = (store.get("latest") or {}).get(str(group))
    if not key:
        key = store.get("latest_any")
    tbl = (store.get("tables") or {}).get(key) if key else None
    if tbl is not None and fit_only and not tbl.get("fit_for_anchoring", True):
        tbl = None                       # an unfit table is a record, not an anchor
    if tbl is None and required:
        raise AnchorTableMissing(
            f"No persisted E[r] anchor table for group={group!r} in {p} "
            f"(known: {sorted((store.get('latest') or {}).keys())}). D-24 §3.3: read, never recompute.")
    return tbl


# ══════════════════════════════════════════════════════════════════════════════════════════════
# STAGE 2/3/5 — THE ROW COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════════════════════

def compute_expected_return(*, fwd_eps_growth_pct=None, rev_growth_pct=None,
                            growth_knee_pp=None,
                            current_multiple=None, median_5y_multiple=None,
                            dividend_yield_pct=None, sharecount_change_3y_pct_pa=None,
                            regime=None,
                            anchor_xs=None, multiple_field=None, sector=None,
                            anchor_mode=None, anchor_table_as_of=None,
                            anchor_table_group=None, multiple_note=None):
    """Pure function. Percent-unit inputs (12 == 12%). Returns dict:
    {expected_return_12_24m, er_growth, er_rerate, er_yield, er_confidence, er_basis, er_status,
     er_rerate_status, er_anchor_xs, er_anchor_own, er_anchor_divergence_pct,
     er_anchor_divergence, er_multiple_field, er_multiple_value,
     er_growth_clamped, er_rerate_clamped}

    A missing GROWTH or YIELD term contributes 0 and lowers er_confidence (unchanged — both are
    additive returns and zero is the honest expectation for an absent dividend/buyback).
    A missing RE-RATE is **None**, not 0 (§5) — "the multiple will not change" is a claim, and it
    is the claim that was being made silently on 92% of rows.
    """
    mode = str(anchor_mode or _c("ER_ANCHOR_MODE", "cross_sectional_primary"))
    basis, present = [], 0.0

    # ── growth ────────────────────────────────────────────────────────────────────────────────
    g = _num(fwd_eps_growth_pct)
    if g is not None:
        basis.append("growth=fwd_eps"); present += 0.5
    else:
        rg = _num(rev_growth_pct)
        if rg is not None:
            g = rg * 0.8; basis.append("growth=rev_x0.8_fallback"); present += 0.3
        else:
            g = 0.0; basis.append("growth=MISSING")
    # ── the bound (ISA-0377) ──────────────────────────────────────────────────────────────────
    # `er_growth_clamped` KEEPS ITS ORIGINAL MEANING — raw growth outside the absolute bounds —
    # because it is a stored column with downstream readers, and quietly redefining a stored field
    # is the failure class this framework exists to prevent. What is NEW is a separate field.
    g_raw = g
    g_clamped = bool(g > _G_HI or g < _G_LO)
    g, g_shape = growth_transform(g, knee=growth_knee_pp)
    g_compressed = bool(abs(g - g_raw) > 1e-9)
    if g_shape == "COMPRESSED_XS":
        # §7 F9 was: on the 07-Aug SP500 g == 50.0 EXACTLY for STX, WDC, BMY, VRT and PLTR — the
        # ordering at the top of the screen was set by the CLAMP, not by the data. Above the knee
        # the value is now strictly increasing in raw growth, so those five names order again.
        basis.append(f"growth_COMPRESSED@knee{growth_knee_pp:g}->{g:.1f}")
    elif g_shape in ("CEIL_ABSOLUTE", "FLOOR_ABSOLUTE"):
        basis.append(f"growth_CLAMPED@{g:g}")

    # ── re-rate: two anchors, and they must agree ─────────────────────────────────────────────
    cur = _num(current_multiple)
    own = _num(median_5y_multiple)
    xs = _num(anchor_xs)
    if cur is not None and cur <= 0:
        cur = None
    if own is not None and own <= 0:
        own = None
    if xs is not None and xs <= 0:
        xs = None

    divergence_pct, divergence = None, False
    if xs is not None and own is not None:
        divergence_pct = round((xs / own - 1.0) * 100.0, 1)
        band = float(_c("ER_ANCHOR_AGREE_BAND", 0.25)) * 100.0
        if abs(divergence_pct) > band:
            divergence = True
            basis.append(f"anchor_divergence(xs={xs:g},own={own:g},{divergence_pct:+.1f}%>{band:g}%)")

    if mode == "own_history_only":
        anchor, anchor_kind = own, "own_history"
    elif mode == "own_history_primary":
        anchor, anchor_kind = (own, "own_history") if own is not None else (xs, "cross_sectional")
    else:                                        # cross_sectional_primary (default)
        anchor, anchor_kind = (xs, "cross_sectional") if xs is not None else (own, "own_history")

    rer, rer_clamped = None, False
    if cur is not None and anchor is not None:
        raw = (anchor / cur) ** 0.5 - 1.0
        rer_clamped = bool(abs(raw) >= _CAP - 1e-12)
        rer = 100.0 * max(min(raw, _CAP), -_CAP)
        _tag = f"rerate={anchor_kind}_median"

        # ── C1 (02-Aug-2026): SHAPE and REGIME ────────────────────────────────────────────────
        # Measured over 13.6 years, 1,680 names, multi-market: forward 52-week excess return by
        # own-history extension decile is U-SHAPED, not monotonic — D1 (cheapest) +2.4% · D2-D7
        # -2 to -3% · D10 (most extended) +6.8%. A monotonic penalty therefore punishes the BEST
        # decile hardest and rewards the WORST. It is also regime-conditional (+8.5pp Bull /
        # -10.2pp Bear) and flips by era. The sign is NOT flipped — rewarding extension would have
        # lost 15pp in 2020-22, the study universe has ZERO delistings, and the effective sample is
        # ~9 independent observations. Two conservative changes only: a NEUTRAL BAND, and
        # ASYMMETRIC REGIME DAMPING of the de-rate side. Damping is 0.25 not 0 in RISK_ON
        # deliberately: the evidence is directional, not strong enough to switch the term off.
        if _RERATE_MODE == "regime_aware":
            band = _NEUTRAL_BAND
            if abs(raw) <= band:
                rer = 0.0
                _tag = f"rerate=neutral_band(|{raw:+.3f}|<={band})"
            elif raw < 0:                      # expensive vs its anchor
                f = _REGIME_DAMPING.get(str(regime or "").upper(), 1.0)
                rer *= f
                _tag = f"rerate=de_rate_damped({anchor_kind},regime={regime or 'unknown'},x{f})"
            else:                              # cheap vs its anchor — kept in full
                _tag = f"rerate=re_rate_credit_full({anchor_kind})"
        basis.append(_tag)
        # §4.4 CONFIDENCE. A cross-sectional anchor earns LESS than own history (0.20 vs 0.30),
        # because it is a weaker claim and the confidence number must say so. But where BOTH
        # anchors exist and AGREE within the band, the re-rate is corroborated by two independent
        # derivations — the engineering-standard rule — and earns the full own-history credit
        # regardless of which one is operative. A DIVERGENT pair earns only the weaker credit and
        # carries the published flag: disagreement is evidence of less confidence, not more.
        corroborated = (own is not None and xs is not None and not divergence)
        if anchor_kind == "own_history" or corroborated:
            present += float(_c("ER_OWN_CONF_WEIGHT", 0.30))
            basis.append("rerate_conf=own_weight" + ("(corroborated_by_xs)" if corroborated
                                                     and anchor_kind != "own_history" else ""))
        else:
            present += float(_c("ER_XS_CONF_WEIGHT", 0.20))
            basis.append("rerate_conf=xs_weight" + ("(divergent)" if divergence else ""))
        rerate_status = anchor_kind
        if rer_clamped:
            basis.append(f"rerate_CLAMPED@cap{_CAP:g}")
    else:
        # §5 REFUSAL INSTEAD OF ZERO.
        rerate_status = "UNMEASURED"
        why = []
        if cur is None:
            why.append(f"no {multiple_field or 'multiple'}"
                       + (" and no fallback" if multiple_note else ""))
        if anchor is None:
            why.append("no anchor" if mode != "own_history_only" else "no own-history anchor")
        basis.append("rerate=UNMEASURED(" + "; ".join(why) + ")")

    # ── yield ─────────────────────────────────────────────────────────────────────────────────
    dy = _num(dividend_yield_pct) or 0.0
    sc = _num(sharecount_change_3y_pct_pa)
    bb = -sc if sc is not None else 0.0            # shrinking count (negative change) = positive yield
    if _num(dividend_yield_pct) is not None or sc is not None:
        basis.append("yield=div+buyback"); present += 0.2
    else:
        basis.append("yield=MISSING")
    y = max(min(dy + bb, 15.0), -10.0)

    if multiple_field:
        basis.append(f"mult={multiple_field}(sector={sector or 'unknown'})"
                     + (f";{multiple_note}" if multiple_note else ""))
    if anchor_table_as_of:
        basis.append(f"anchor_tbl={anchor_table_group or '?'}@{anchor_table_as_of}")

    er = round(g + (rer or 0.0) + y, 1)
    return {"expected_return_12_24m": er,
            "er_growth": round(g, 1),
            "er_rerate": (round(rer, 1) if rer is not None else None),
            "er_yield": round(y, 1),
            "er_confidence": round(min(present, 1.0), 2),
            "er_basis": "|".join(basis),
            "er_status": ("unmeasured" if rerate_status == "UNMEASURED" else "measured"),
            "er_rerate_status": rerate_status,
            "er_anchor_xs": xs,
            "er_anchor_own": own,
            "er_anchor_operative": anchor,
            "er_anchor_divergence_pct": divergence_pct,
            "er_anchor_divergence": divergence,
            "er_anchor_corroborated": bool(own is not None and xs is not None and not divergence),
            "er_multiple_field": multiple_field,
            "er_multiple_value": cur,
            "er_growth_clamped": g_clamped,
            "er_growth_raw": (None if g_raw is None else round(g_raw, 1)),
            "er_growth_basis": g_shape,
            "er_growth_knee_pp": growth_knee_pp,
            "er_growth_compressed": g_compressed,
            "er_rerate_clamped": bool(rer_clamped),
            "er_anchor_table_as_of": anchor_table_as_of,
            "er_anchor_table_group": anchor_table_group,
            "er_anchor_mode": mode}


# Row adapter — tolerant of the screen/pre-run field-name variants; extend lists, never rename here.
# VERIFIED 12-Jul-2026 against screener_core.FIELD_MAP (the authoritative full_data schema):
#   fwd_eps_growth       = FRACTION (0.12 == 12%)  -> scale 100   (screener_core Metric 9)
#   rev_est_fwd_pct      = percent                 -> scale 1
#   share_count_change   = FRACTION per annum      -> scale 100   (share_chg_ann, Part A)
#   dividend yield: NOT in full_data — er_yield at screen is buyback-only (honest, er_basis shows it)
# D-24 (09-Aug-2026): `current_multiple` is NO LONGER a fixed candidate list. It is resolved by
# sector via ER_MULTIPLE_BY_SECTOR, then ER_MULTIPLE_FALLBACK, then refused. The old list put
# `trailing_pe` (8.7% populated) ahead of `fwd_pe` (100%) — the defect.
# Each candidate is (field_name, scale_to_percent_units).
_KEYS = {
    "fwd_eps_growth_pct": [("fwd_eps_growth", 100), ("forward_eps_growth_pct", 1), ("eps_growth_fwd_pct", 1)],
    "rev_growth_pct": [("rev_est_fwd_pct", 1), ("revenue_growth_fwd_pct", 1), ("recent_revenue_growth_pct", 1)],
    # C2: the MEDIAN anchor first, the legacy 3-year MEAN only as a fallback.
    "median_5y_multiple": [("val_hist_pe_anchor", 1), ("val_hist_median_pe_5y", 1),
                           ("pe_5y_median", 1), ("val_hist_pe_3yr_avg", 1)],
    "dividend_yield_pct": [("dividend_yield_pct", 1), ("dividend_yield", 1)],
    "sharecount_change_3y_pct_pa": [("share_count_change", 100), ("share_count_change_3y_pct_pa", 1),
                                    ("sharecount_change_pct_pa", 1)],
}
# The own-history anchor is a P/E anchor. Pairing it with an EV/EBITDA current multiple would
# compare two different objects, so it is only admissible where the chosen multiple is P/E-like.
_PE_LIKE = {"fwd_pe", "trailing_pe", "val_hist_current_pe", "current_pe"}

_REGIME_CACHE = {}


def current_market_regime(here=None):
    """The MECHANICAL market regime (drawdown_monitor B7: RISK_ON / LATE_CYCLE / RISK_OFF /
    RECOVERY), read once per process from drawdown_state.json.

    This is the PRICE-state regime, not Step 4's macro judgement. Precedence is deliberate and
    matches the two-regime resolution already in the framework: mechanical price state governs
    anything that moves capital automatically; macro judgement only shifts a threshold. E[r] feeds
    a deploy floor, so it takes the mechanical one.

    Returns None when unavailable — and None means UNDAMPED (the conservative full penalty), never
    a guessed regime.
    """
    if "v" in _REGIME_CACHE:
        return _REGIME_CACHE["v"]
    try:
        with open(_os.path.join(_here(here), "drawdown_state.json"), encoding="utf-8") as f:
            _REGIME_CACHE["v"] = (_json.load(f) or {}).get("regime_state")
    except Exception:                                        # noqa: BLE001
        _REGIME_CACHE["v"] = None
    return _REGIME_CACHE["v"]


# The PRE-D-24 candidate list, retained for one purpose only: ER_ANCHOR_MODE="own_history_only"
# is the §11 rollback, and a rollback that changes the numbers is not a rollback. Under that mode
# the multiple resolves exactly as it did before 09-Aug-2026 — trailing_pe (8.7% populated) ahead
# of fwd_pe (100%) — so the negative control (T8) reproduces the stored 07-Aug frame to the digit.
_LEGACY_CURRENT_MULTIPLE = ["trailing_pe", "val_hist_current_pe", "current_pe", "fwd_pe"]


def resolve_multiple(row, get=None, anchor_table=None):
    """(field, value, xs_anchor, note, sector) — sector-declared multiple, fallback, or refusal."""
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    sector = str(g(row, "sector") or "").strip()
    if str(_c("ER_ANCHOR_MODE", "cross_sectional_primary")) == "own_history_only":
        for f in _LEGACY_CURRENT_MULTIPLE:
            v = _num(g(row, f))
            if v is not None and v > 0:
                return f, v, None, "legacy_candidate_list(rollback)", sector
        return None, None, None, "legacy_candidate_list(rollback)", sector
    field = multiple_field_for_sector(sector)
    note = None
    val = _sane(field, g(row, field))
    if val is None:
        fb = str(_c("ER_MULTIPLE_FALLBACK", "price_fcf"))
        fb_val = _sane(fb, g(row, fb))
        if fb_val is not None:
            note = f"fallback_from={field}"
            field, val = fb, fb_val
    xs = None
    if anchor_table:
        med = (anchor_table.get("median_by_sector") or {})
        fld_by_sec = (anchor_table.get("multiple_by_sector") or {})
        if fld_by_sec.get(sector) == field:
            xs = _num(med.get(sector))
        if xs is None:                       # sector excluded (n<min) or a fallback field in use
            xs = _num((anchor_table.get("whole_screen_median") or {}).get(field))
            if xs is not None:
                note = ((note + ";") if note else "") + "anchor=whole_screen_median"
    return field, val, xs, note, sector


def expected_return_for_row(row, get=None, regime=None, anchor_table=None,
                            allow_missing_anchor_table=False):
    """THE row adapter. ⚑ `anchor_table` is not optional by default.

    §1 of the D-24 spec: there are nine live call sites and every one of them used to call this
    with no context. If an anchor-table parameter were merely *added*, eight of them would keep
    running the old, defective behaviour and nothing would say so. So the default is to RAISE.

    A caller that legitimately has no screen anchor in scope (build_email / return_architecture run
    at REVIEW time on portfolio rows, §1.2) passes `allow_missing_anchor_table=True`, and the
    result is then explicitly `er_status="unmeasured"` with `er_rerate=None` — never a silently
    different E[r] from the screen that produced the candidate. That is the email-desync disease
    prevented by construction rather than by discipline.
    """
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    mode = str(_c("ER_ANCHOR_MODE", "cross_sectional_primary"))

    if anchor_table is None and mode != "own_history_only" and not allow_missing_anchor_table:
        raise AnchorTableMissing(
            "expected_return_for_row called without an anchor table. Pass anchor_table=... (screen "
            "builds it; pre-run loads it via load_anchor_table()), or allow_missing_anchor_table="
            "True to declare er_status='unmeasured'. See scoring_config.ER_CALLSITE_MANIFEST.")

    kw = {}
    for arg, cands in _KEYS.items():
        v = None
        for k, scale in cands:
            raw = g(row, k)
            if raw not in (None, ""):
                n = _num(raw)
                if n is not None:
                    v = n * scale
                    break
        kw[arg] = v

    field, val, xs, note, sector = resolve_multiple(row, get=g, anchor_table=anchor_table)
    kw["current_multiple"] = val
    kw["multiple_field"] = field
    kw["multiple_note"] = note
    kw["sector"] = sector
    kw["anchor_xs"] = xs
    if field not in _PE_LIKE and kw.get("median_5y_multiple") is not None:
        # own-history anchor is a P/E anchor; do not pair it with EV/EBITDA or P/FCF
        kw["median_5y_multiple"] = None
        kw["multiple_note"] = ((note + ";") if note else "") + "own_anchor_NA(non_pe_multiple)"
    if anchor_table is None and mode != "own_history_only":
        # explicitly declared absence (§1.2) — refuse the re-rate rather than compute a different one
        kw["anchor_xs"] = None
        kw["median_5y_multiple"] = None
        kw["multiple_note"] = ((kw.get("multiple_note") + ";") if kw.get("multiple_note") else "") \
            + "no_anchor_table_in_scope"
    else:
        kw["anchor_table_as_of"] = (anchor_table or {}).get("as_of")
        kw["anchor_table_group"] = (anchor_table or {}).get("group")

    # ISA-0377 — the knee is a property of the FRAME, so it arrives with the frame's table and
    # is never recomputed per row. No table in scope -> no knee -> the legacy flat cut, named in
    # er_growth_basis rather than assumed.
    kw["growth_knee_pp"] = ((anchor_table or {}).get("growth_bounds") or {}).get("knee_pp")
    kw["regime"] = regime if regime is not None else current_market_regime()
    return compute_expected_return(**kw)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# STAGE 4 — REACHABILITY + REFUSAL-SHARE ASSERTIONS (the orchestrator-parity lesson)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def max_attainable_er_confidence():
    """0.5 (fwd EPS growth) + best re-rate credit + 0.2 (yield)."""
    return round(0.5 + max(float(_c("ER_OWN_CONF_WEIGHT", 0.30)),
                           float(_c("ER_XS_CONF_WEIGHT", 0.20))) + 0.2, 4)


def assert_er_route_reachable(raise_on_fail=True):
    """Every screen asserts the fundamentals evidence route is ACHIEVABLE.

    Had this existed, F4 would have been caught the day the val-hist fetch degraded, instead of
    being invisible for months while the ceiling sat at 0.70 against a 0.75 floor.
    """
    floor = float(_c("EVIDENCE_ER_CONF_MIN", 0.75))
    top = max_attainable_er_confidence()
    ok = top >= floor - 1e-9
    msg = (f"E[r] evidence route {'reachable' if ok else 'UNREACHABLE'}: "
           f"max attainable er_confidence {top:.2f} vs EVIDENCE_ER_CONF_MIN {floor:.2f}")
    if not ok and raise_on_fail:
        raise AssertionError("D-24 §6 REACHABILITY FAILURE — " + msg)
    return {"ok": ok, "max_attainable": top, "floor": floor, "message": msg}


def unmeasured_share_verdict(rows, get=None):
    """WARN at ER_UNMEASURED_WARN_SHARE, FAIL at ER_UNMEASURED_FAIL_SHARE (§6)."""
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    rows = list(rows or [])
    n = len(rows)
    u = sum(1 for r in rows if str(g(r, "er_rerate_status") or "") == "UNMEASURED")
    share = (u / n) if n else 0.0
    warn = float(_c("ER_UNMEASURED_WARN_SHARE", 0.10))
    fail = float(_c("ER_UNMEASURED_FAIL_SHARE", 0.25))
    verdict = "FAIL" if share > fail else ("WARN" if share > warn else "OK")
    return {"verdict": verdict, "unmeasured": u, "rows": n, "share": round(share, 4),
            "warn_at": warn, "fail_at": fail,
            "message": f"er_rerate UNMEASURED on {u}/{n} ({share:.1%}) — {verdict} "
                       f"(warn>{warn:.0%}, fail>{fail:.0%})"}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L-10 (§13) — the learning record. Ships with Stage 1 or it will not ship.
# ══════════════════════════════════════════════════════════════════════════════════════════════

_L10_COLS = ["run_date", "group", "ticker", "sector", "er_multiple_field", "er_multiple_value",
             "er_anchor_xs", "er_anchor_own", "er_anchor_operative", "er_anchor_divergence_pct",
             "er_anchor_divergence", "er_rerate_status", "er_rerate", "er_growth", "er_yield",
             "expected_return_12_24m", "er_confidence", "er_growth_clamped", "er_rerate_clamped",
             "er_anchor_mode"]


def record_anchor_learning(rows, *, run_date, group, base_dir=None, get=None):
    """Append per-row anchor evidence to er_anchor_learning.csv.

    After ~6 screens this answers a question nobody can answer today — does the cross-sectional
    anchor or the own-history anchor produce better forward returns? — which is the only route to
    retiring ER_ANCHOR_MODE as a judgement call rather than a preference.
    """
    import csv as _csv
    p = _os.path.join(_here(base_dir), str(_c("ER_LEARNING_STORE", "er_anchor_learning.csv")))
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    # An EMPTY existing file must still get a header — otherwise the first 312 rows land as
    # headerless data and the store is unreadable. (Observed 09-Aug-2026 while seeding.)
    new = (not _os.path.exists(p)) or _os.path.getsize(p) == 0
    n = 0
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=_L10_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows or []:
            rec = {c: g(r, c) for c in _L10_COLS}
            rec["run_date"], rec["group"] = run_date, group
            rec["ticker"] = g(r, "ticker")
            rec["sector"] = g(r, "sector")
            w.writerow(rec)
            n += 1
    return {"path": p, "rows": n}


def stamp_frame(rows, *, run_date, group, regime=None, get=None, base_dir=None,
                persist=True, learn=True):
    """THE screen-side entry point: build -> persist -> stamp every row -> assert -> learn.

    Both `screener_core` and `screener_local` call THIS, so the live path cannot drift from the
    scheduled path the way PRICE_MOM_SCORING did for twelve days.
    """
    reach = assert_er_route_reachable(raise_on_fail=True)
    built = build_anchor_table(rows, run_date=run_date, group=group, get=get)
    if persist:
        persist_anchor_table(built, base_dir=base_dir)
    table, substituted = built, None
    if not built.get("fit_for_anchoring"):
        # This frame cannot form its own anchor. Fall back to the last FIT table that a real
        # screen persisted — READ, never recomputed — and say so on every row via er_basis. If
        # there is none, the re-rate is refused rather than invented.
        table = load_anchor_table(base_dir=base_dir, required=False, fit_only=True)
        substituted = (f"{table.get('group')}@{table.get('as_of')}" if table else None)
    for r in rows or []:
        try:
            # ⚑ §5: when no fit table exists the RE-RATE is refused — the growth and yield terms
            # are still measured and must still produce a number. Raising here would have thrown
            # the whole row away, which is a worse answer than the one this build exists to
            # replace (observed live 09-Aug-2026: a 6-row ad-hoc screen wrote NaN into
            # expected_return_12_24m for every name).
            r.update(expected_return_for_row(r, get=get, regime=regime, anchor_table=table,
                                             allow_missing_anchor_table=(table is None)))
            apply_capital_signal_conflict(r)
        except Exception as e:                               # noqa: BLE001
            r["er_status"] = "unmeasured"
            r["er_rerate_status"] = "UNMEASURED"
            r["er_basis"] = f"stamp_error:{type(e).__name__}:{e}"
    verdict = unmeasured_share_verdict(rows, get=get)
    if not built.get("fit_for_anchoring") and table is None:
        # Nothing upstream is broken — this frame is simply too small to anchor with. Reporting
        # FAIL here would train the reader to ignore a threshold that means something else.
        verdict = {**verdict, "verdict": "NOT_APPLICABLE",
                   "message": (f"frame of {built['rows_in']} rows is below "
                               f"ER_ANCHOR_MIN_ROWS={built['min_rows']} and no fit table exists "
                               f"to borrow — re-rate refused on every row, by design")}
    verdict["anchor_fit"] = bool(built.get("fit_for_anchoring"))
    verdict["anchor_substituted_from"] = substituted
    learned = record_anchor_learning(rows, run_date=run_date, group=group,
                                     base_dir=base_dir, get=get) if learn else None
    return {"anchor_table": table, "built_table": built, "reachability": reach,
            "unmeasured": verdict, "learning": learned}


def apply_capital_signal_conflict(row):
    """Review item 8 (18-Jul-26): E[r] is growth-anchored, implied_upside_fv is multiple-anchored;
    they can disagree violently with no flag (MU +58.5%pa vs FV -42.9%). Compare E[r] %pa against
    the ANNUALISED FV-implied return over the 12-24m window (18m midpoint: ((1+u)^(12/18)-1)*100).
    Gap > cfg.CAPITAL_SIGNAL_CONFLICT_PP -> capital_signal_conflict=True + er_confidence capped at
    cfg.CONFLICT_ER_CONF_CAP (below the A5 v3 0.75 full-size bar: conflicted signals size as
    starter, never full). Mutates + returns row; no-op when either input missing."""
    thr = float(_c("CAPITAL_SIGNAL_CONFLICT_PP", 25.0))
    cap = float(_c("CONFLICT_ER_CONF_CAP", 0.5))
    er = _num(row.get("expected_return_12_24m"))
    u = _num(row.get("implied_upside_fv"))
    row.setdefault("capital_signal_conflict", False)
    if er is None or u is None or u <= -1.0:
        return row
    fv_ann = ((1.0 + u) ** (12.0 / 18.0) - 1.0) * 100.0
    row["fv_annualised_pct"] = round(fv_ann, 1)
    if abs(er - fv_ann) > thr:
        row["capital_signal_conflict"] = True
        ec = _num(row.get("er_confidence"))
        if ec is None or ec > cap:
            row["er_confidence"] = cap
            row["er_basis"] = (str(row.get("er_basis") or "") + "|conflict_capped").lstrip("|")
    return row


if __name__ == "__main__":
    _TBL = {"as_of": "20260807", "group": "SP500", "basis": "sector_median",
            "multiple_by_sector": {"Technology": "fwd_pe", "Industrials": "ev_ebitda"},
            "median_by_sector": {"Technology": 19.5, "Industrials": 22.1},
            "n_by_sector": {"Technology": 67, "Industrials": 58},
            "whole_screen_median": {"fwd_pe": 18.0, "ev_ebitda": 14.0, "price_fcf": 25.0},
            "excluded": {}}

    # Fixture 1: growth compounder near its own median multiple, buyback
    a = compute_expected_return(fwd_eps_growth_pct=14, current_multiple=24, median_5y_multiple=25,
                                dividend_yield_pct=0.6, sharecount_change_3y_pct_pa=-1.5,
                                anchor_mode="own_history_only")
    # Fixture 2: maturing momentum name at a 90th-pct multiple (negative rerate, capped)
    b = compute_expected_return(fwd_eps_growth_pct=9, current_multiple=40, median_5y_multiple=22,
                                anchor_mode="own_history_only")
    # Fixture 3: sparse data (fallback growth only) — re-rate now REFUSES rather than reading 0
    c = compute_expected_return(rev_growth_pct=20)
    for name, r in (("compounder", a), ("late_cycle", b), ("sparse", c)):
        print(name, {k: r[k] for k in ("expected_return_12_24m", "er_rerate", "er_confidence",
                                       "er_status")})
    assert a["expected_return_12_24m"] > 14 and a["er_confidence"] == 1.0
    assert b["er_rerate"] == -10.0 and b["expected_return_12_24m"] < 0.5 + b["er_growth"]
    assert c["er_basis"].startswith("growth=rev_x0.8_fallback") and c["er_confidence"] < 0.5
    assert c["er_rerate"] is None and c["er_status"] == "unmeasured", c

    # Fixture 4 (12-Jul): row adapter on REAL screen field names/units — fwd_eps_growth and
    # share_count_change are fractions in full_data and must be scaled x100 by the adapter.
    d = expected_return_for_row({"fwd_eps_growth": 0.14, "trailing_pe": 24, "fwd_pe": 24,
                                 "sector": "Technology", "val_hist_pe_3yr_avg": 25,
                                 "share_count_change": -0.015}, anchor_table=_TBL)
    assert d["er_growth"] == 14.0 and d["er_yield"] == 1.5, d
    assert d["er_basis"].startswith("growth=fwd_eps") and "rerate=" in d["er_basis"], d

    # ── C1 (02-Aug-2026) ──────────────────────────────────────────────────────────────────────
    nb = compute_expected_return(fwd_eps_growth_pct=10, current_multiple=24,
                                 median_5y_multiple=25, anchor_mode="own_history_only")
    assert nb["er_rerate"] == 0.0 and "neutral_band" in nb["er_basis"], nb
    on = compute_expected_return(fwd_eps_growth_pct=10, current_multiple=40,
                                 median_5y_multiple=22, regime="RISK_ON",
                                 anchor_mode="own_history_only")
    off = compute_expected_return(fwd_eps_growth_pct=10, current_multiple=40,
                                  median_5y_multiple=22, regime="RISK_OFF",
                                  anchor_mode="own_history_only")
    assert off["er_rerate"] == -10.0 and on["er_rerate"] == -2.5, (on, off)
    ch_on = compute_expected_return(fwd_eps_growth_pct=5, current_multiple=10,
                                    median_5y_multiple=20, regime="RISK_ON",
                                    anchor_mode="own_history_only")
    ch_off = compute_expected_return(fwd_eps_growth_pct=5, current_multiple=10,
                                     median_5y_multiple=20, regime="RISK_OFF",
                                     anchor_mode="own_history_only")
    assert ch_on["er_rerate"] == ch_off["er_rerate"] == 10.0, (ch_on, ch_off)
    assert on["er_rerate"] <= 0.0, on

    # C2: the median anchor is preferred over the legacy 3-year mean when present.
    e = expected_return_for_row({"fwd_eps_growth": 0.10, "fwd_pe": 40, "sector": "Technology",
                                 "val_hist_pe_anchor": 22, "val_hist_pe_3yr_avg": 39},
                                anchor_table=_TBL, regime="RISK_OFF")
    assert e["er_anchor_own"] == 22.0, e

    # ── D-24 (09-Aug-2026) ────────────────────────────────────────────────────────────────────
    # A fwd_pe-only row (the 92% case) now measures a re-rate instead of asserting zero.
    f = expected_return_for_row({"fwd_eps_growth": 0.10, "fwd_pe": 30, "sector": "Technology",
                                 "share_count_change": -0.01}, anchor_table=_TBL, regime="RISK_OFF")
    assert f["er_rerate_status"] == "cross_sectional" and f["er_rerate"] not in (None, 0.0), f
    assert f["er_confidence"] == 0.90 and f["er_confidence"] >= float(_c("EVIDENCE_ER_CONF_MIN", .75)), f
    assert f["er_multiple_field"] == "fwd_pe" and f["er_anchor_xs"] == 19.5, f
    # ...and own history still scores strictly higher, because it is a stronger claim.
    fo = expected_return_for_row({"fwd_eps_growth": 0.10, "fwd_pe": 30, "sector": "Technology",
                                  "val_hist_pe_anchor": 19.0, "share_count_change": -0.01},
                                 anchor_table=_TBL, regime="RISK_OFF")
    assert fo["er_confidence"] == 1.00 > f["er_confidence"], (fo, f)
    # Divergence is PUBLISHED, never silently resolved.
    dv = expected_return_for_row({"fwd_eps_growth": 0.10, "fwd_pe": 30, "sector": "Technology",
                                  "val_hist_pe_anchor": 10.0}, anchor_table=_TBL, regime="RISK_OFF")
    assert dv["er_anchor_divergence"] and dv["er_anchor_divergence_pct"] == 95.0, dv
    # Industrials resolve on EV/EBITDA, and the P/E own-history anchor is NOT paired with it.
    ind = expected_return_for_row({"fwd_eps_growth": 0.08, "ev_ebitda": 30.0, "fwd_pe": 12,
                                   "sector": "Industrials", "val_hist_pe_anchor": 18.0},
                                  anchor_table=_TBL, regime="RISK_OFF")
    assert ind["er_multiple_field"] == "ev_ebitda" and ind["er_anchor_own"] is None, ind
    # No multiple at all, no fallback -> UNMEASURED, and NOT zero.
    um = expected_return_for_row({"fwd_eps_growth": 0.10, "sector": "Technology"}, anchor_table=_TBL)
    assert um["er_rerate"] is None and um["er_status"] == "unmeasured" and um["er_rerate"] != 0, um
    # Clamp flags.
    cl = compute_expected_return(fwd_eps_growth_pct=62.0, anchor_mode="own_history_only")
    assert cl["er_growth"] == 50.0 and cl["er_growth_clamped"] is True, cl
    cl2 = compute_expected_return(fwd_eps_growth_pct=-40.0, anchor_mode="own_history_only")
    assert cl2["er_growth"] == -25.0 and cl2["er_growth_clamped"] is True, cl2
    # An un-updated caller cannot silently keep the old behaviour.
    try:
        expected_return_for_row({"fwd_pe": 20, "sector": "Technology"})
        raise SystemExit("FAIL: missing anchor table did not raise")
    except AnchorTableMissing:
        pass
    # ...and a review-time caller that declares the absence gets an honest refusal.
    rt = expected_return_for_row({"fwd_eps_growth": 0.10, "fwd_pe": 30, "sector": "Technology",
                                  "val_hist_pe_anchor": 19.0}, allow_missing_anchor_table=True)
    assert rt["er_status"] == "unmeasured" and rt["er_rerate"] is None, rt
    # Reachability.
    assert assert_er_route_reachable()["ok"]
    # Anchor table build: excluded sectors are NAMED, and covered + excluded == all sectors seen.
    rows = ([{"sector": "Technology", "fwd_pe": 18 + i} for i in range(7)]
            + [{"sector": "Energy", "ev_ebitda": 9.0}] * 2)
    t = build_anchor_table(rows, run_date="20260807", group="TEST")
    assert set(t["median_by_sector"]) == {"Technology"} and "Energy" in t["excluded"], t
    assert set(t["median_by_sector"]) | set(t["excluded"]) == {"Technology", "Energy"}, t
    print("SELF-TEST OK (A2 + C1 shape/regime + C2 anchor preference + D-24 anchor/refusal/clamps)")
