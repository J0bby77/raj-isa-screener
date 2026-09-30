"""
vci_review_universe.py — ISA-0769: THE canonical VCI review population, conserved across stages.

Authority: ChatGPT Astra 6 Audit/ISA_BuildSpec_VCI_Input_Population_Integrity_27Sep2026.md §6 (VCI-B),
validated against TB-2026-09-27-04. Binding standard: ISA_Engineering_Rules.md (R2.10, R4.3, R4.9, R6.2).

WHY. The 13-Sep-2026 VCI run's Stage-0 advancement set (`vci_prescore.py all --advance 15`) returned a
complete-looking 37-name list that OMITTED QBTS - the one position the sleeve had deployed new capital
into - and IONQ, an active watchlist name. Held names are purged from vci_watchlist on purchase, the held
injection read a source QBTS was never written to, and nothing asserted that every held/watchlist name
reached review. Both were added by hand. A plausible list with a silent omission is the failure.

CONTRACT. build() returns the REVIEW POPULATION (never a capital verdict) as the union, merged by stable
identity with every source's provenance kept, of:
  CURRENT_HELD_VCI        held direct stocks with VCI provenance, from the framework's CURRENT position
                          authority: the latest broker snapshot (portfolio_data_[mmm_yyyy].json) rolled
                          forward by transaction_ledger.json entries dated after it. VCI provenance = a
                          'vci'-route decision in decision_ledger.json or a VCI underwriting declaration
                          (asset_structure) in vci_binary_positions.json. A snapshot/ledger conflict keeps
                          the name IN the population, typed holding_state=UNKNOWN_CONFLICT. Historical VCI
                          records never make a name held.
  ACTIVE_VCI_WATCHLIST    watchlist_tickers.json vci_watchlist.
  QMS_TOP_N               top N by QMS (configured N passed by the caller - no new constant).
  PRE_INFLECTION_OVERRIDE the existing Stage-1 auto-advance rule (unchanged).
  AUTHORISED_OVERRIDE     explicit caller-named additions with a reason (e.g. hyperscaler cascade).
Invariants asserted on every build: each held and each watchlist name appears EXACTLY ONCE; duplicates
merge by identity keeping all roles; count conservation (inputs - merged duplicates == members). An
unreadable held authority is UNKNOWN and REFUSES the review path (a hard failure, not a warning).
Downstream, vci_run_capture.validate() enforces stage conservation: every member is scored or carries a
NAMED refusal/discard; a CURRENT_HELD_VCI member that is neither is an ERROR.
Learning paradigm: NONE; the population/provenance record is retained for later calibration.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "1.0.0"

try:   # ISA-0699 execution-ledger observation (never raises into the caller)
    from framework_integrity import _mark as _fi_mark
except Exception:                                                       # noqa: BLE001
    def _fi_mark(*_a, **_k):                                            # noqa: D103
        return None
ROLE_HELD, ROLE_WATCH, ROLE_QMS, ROLE_PREINF, ROLE_OVERRIDE = (
    "CURRENT_HELD_VCI", "ACTIVE_VCI_WATCHLIST", "QMS_TOP_N", "PRE_INFLECTION_OVERRIDE", "AUTHORISED_OVERRIDE")
HELD, UNKNOWN_CONFLICT = "HELD", "UNKNOWN_CONFLICT"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                       "oct", "nov", "dec"), 1)}


class ReviewUniverseError(RuntimeError):
    """The VCI review path cannot proceed: held authority UNKNOWN or a held name lost."""


def key(ticker) -> str:
    """Stable identity key: upper-case base symbol (broker 'ONT' == VCI 'ONT.L'). The display symbol and
    any scoring venue are kept on the member; the key only decides what is the SAME security."""
    t = str(ticker or "").strip().upper()
    return re.sub(r"\.[A-Z]{1,3}$", "", t)


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                                   # noqa: BLE001
        return default


def latest_portfolio(root: str = None):
    """(path, doc) of the newest portfolio_data_[mmm_yyyy].json by its month label, or (None, None)."""
    root = root or HERE
    best = None
    for p in glob.glob(os.path.join(root, "portfolio_data_*_*.json")):
        m = re.search(r"portfolio_data_([a-z]{3})_(\d{4})\.json$", os.path.basename(p))
        if m and m.group(1) in _MONTHS:
            k = (int(m.group(2)), _MONTHS[m.group(1)])
            if best is None or k > best[0]:
                best = (k, p)
    return (best[1], _read(best[1])) if best else (None, None)


def _snapshot_date(doc) -> str:
    meta = (doc or {}).get("_meta") or {}
    raw = meta.get("data_date") or meta.get("file_date")
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.datetime.strptime(str(raw), fmt).date().isoformat()
        except (TypeError, ValueError):
            pass
    return None


def current_held_vci(root: str = None, *, portfolio_doc=None, ledger=None, decisions=None,
                     binaries=None) -> dict:
    """The CURRENT held VCI names from the framework's position authority. -> {state OK|UNKNOWN, members,
    authority, why}. Every input may be injected (fixtures); production reads disk."""
    root = root or HERE
    ppath = None
    if portfolio_doc is None:
        ppath, portfolio_doc = latest_portfolio(root)
    if not portfolio_doc or not isinstance(portfolio_doc.get("stocks"), list):
        return {"state": "UNKNOWN", "members": [], "authority": {"portfolio": ppath},
                "why": "no readable broker snapshot (portfolio_data_[mmm_yyyy].json) - held VCI UNKNOWN (R4.3)"}
    snap = _snapshot_date(portfolio_doc)
    _last_import = None
    if ledger is None:
        _tl = _read(os.path.join(root, "transaction_ledger.json"), {}) or {}
        ledger = _tl.get("entries")
        _last_import = (_tl.get("_meta") or {}).get("last_import")
    if ledger is None:
        return {"state": "UNKNOWN", "members": [], "authority": {"portfolio": ppath, "snapshot_date": snap},
                "why": "transaction_ledger.json unreadable - cannot roll the snapshot forward (R4.3)"}
    if decisions is None:
        decisions = (_read(os.path.join(root, "decision_ledger.json"), {}) or {}).get("entries") or []
    if binaries is None:
        binaries = (_read(os.path.join(root, "vci_binary_positions.json"), {}) or {}).get("positions") or {}
    qty, disp = {}, {}
    for s in portfolio_doc["stocks"]:
        k = key(s.get("ticker"))
        if k:
            qty[k] = qty.get(k, 0.0) + float(s.get("quantity") or 0.0)
            disp[k] = s.get("ticker")
    rolled, conflicts = [], []
    ledger_through = max((str(e.get("date") or "") for e in ledger), default=None)
    for e in ledger:
        if (e.get("asset_class") != "stock" or e.get("type") not in ("buy", "sell")
                or not snap or str(e.get("date") or "") <= snap):
            continue
        k = key(e.get("ticker"))
        q = float(e.get("quantity") or 0.0) * (1 if e["type"] == "buy" else -1)
        qty[k] = qty.get(k, 0.0) + q
        disp.setdefault(k, e.get("ticker"))
        rolled.append({"date": e.get("date"), "ticker": e.get("ticker"), "type": e["type"], "quantity": e.get("quantity")})
    vci_prov = {}
    for d in decisions:
        if str(d.get("route") or "") == "vci":
            vci_prov.setdefault(key(d.get("ticker")), set()).add("decision_ledger:vci-route")
    for t, p in (binaries or {}).items():
        if (p or {}).get("asset_structure"):
            vci_prov.setdefault(key(t), set()).add("vci_binary_positions:asset_structure")
    members = []
    for k, q in sorted(qty.items()):
        if k not in vci_prov:
            continue
        if q < -1e-9:
            state = UNKNOWN_CONFLICT
            conflicts.append(k)
        elif q <= 1e-9:
            continue                     # fully sold after the snapshot: history, not a current holding
        else:
            state = HELD
        members.append({"key": k, "ticker": disp.get(k) or k, "holding_state": state, "quantity": round(q, 6),
                        "vci_provenance": sorted(vci_prov[k])})
    return {"state": "OK", "members": members,
            "authority": {"portfolio": os.path.basename(ppath) if ppath else "injected", "snapshot_date": snap,
                          "ledger_through": ledger_through, "ledger_last_import": _last_import,
                          "rolled_forward": rolled, "conflicts": conflicts},
            "why": "broker snapshot %s rolled forward by %d ledger trade(s) through %s" % (snap, len(rolled), ledger_through)}


def active_watchlist(root: str = None) -> list:
    wt = _read(os.path.join(root or HERE, "watchlist_tickers.json"), {}) or {}
    return [r.get("ticker") for r in (wt.get("vci_watchlist") or []) if r.get("ticker")]


def build(qms_rows, n, *, held=None, watchlist=None, overrides=None, root: str = None,
          as_of: str = None) -> dict:
    """THE review population. RAISES ReviewUniverseError when the held authority is UNKNOWN."""
    _fi_mark("vci_review_universe", "build")
    held = held if held is not None else current_held_vci(root)
    if held.get("state") != "OK":
        raise ReviewUniverseError("ISA-0769: current held VCI authority UNKNOWN - %s. The review path "
                                  "cannot prove it reviews the capital already at risk." % held.get("why"))
    watchlist = watchlist if watchlist is not None else active_watchlist(root)
    rows = list(qms_rows or [])
    by_key = {key(r.get("ticker")): r for r in rows}
    inputs = []
    for h in held["members"]:
        inputs.append((h["key"], h["ticker"], ROLE_HELD, {"holding_state": h["holding_state"],
                                                          "vci_provenance": h["vci_provenance"]}))
    for t in watchlist:
        inputs.append((key(t), t, ROLE_WATCH, {"source": "watchlist_tickers.json vci_watchlist"}))
    for i, r in enumerate(rows[:n] if n else [], 1):
        inputs.append((key(r.get("ticker")), r.get("ticker"), ROLE_QMS, {"qms_rank": i, "qms": r.get("qms")}))
    for r in rows:
        if r.get("pre_inflection_override"):
            inputs.append((key(r.get("ticker")), r.get("ticker"), ROLE_PREINF, {"qms": r.get("qms")}))
    for o in overrides or []:
        t, _, why = str(o).partition(":")
        if not why.strip():
            raise ReviewUniverseError("override %r has no reason - an unexplained addition is not authorised" % o)
        inputs.append((key(t), t.strip().upper(), ROLE_OVERRIDE, {"reason": why.strip()}))
    members, order = {}, []
    for k, t, role, prov in inputs:
        if k not in members:
            q = by_key.get(k)
            members[k] = {"key": k, "ticker": (q or {}).get("ticker") or t, "roles": [], "provenance": [],
                          "qms_row": bool(q), "qms": (q or {}).get("qms"),
                          "scoring_venue": (q or {}).get("scoring_venue")}
            order.append(k)
        m = members[k]
        if role not in m["roles"]:
            m["roles"].append(role)
        m["provenance"].append(dict(prov, role=role, as_supplied=t))
        if role == ROLE_HELD:
            m["holding_state"] = prov["holding_state"]
    out = [members[k] for k in order]
    try:
        import vci_deploy_eval as _vde
        for m in out:
            m["security_identity"] = _vde.security_identity(m["ticker"], m.get("scoring_venue"))
    except Exception as exc:                                            # noqa: BLE001
        for m in out:
            m["security_identity"] = {"state": "UNKNOWN", "why": str(exc)}
    errors = []
    for role, src in ((ROLE_HELD, [h["key"] for h in held["members"]]), (ROLE_WATCH, [key(t) for t in watchlist])):
        for k in src:
            c = sum(1 for m in out if m["key"] == k and role in m["roles"])
            if c != 1:
                errors.append("%s %s appears %d time(s) in the review population (must be exactly 1)" % (role, k, c))
    n_in = len(inputs)
    n_dup = n_in - len({i[0] for i in inputs})
    if len(out) != n_in - n_dup:
        errors.append("count conservation failed: %d inputs - %d merged duplicates != %d members" % (n_in, n_dup, len(out)))
    doc = {"schema_version": SCHEMA_VERSION, "as_of": as_of or datetime.date.today().isoformat(),
           "members": out,
           "counts": {"inputs": n_in, "merged_duplicates": n_dup, "members": len(out),
                      "by_role": {r: sum(1 for m in out if r in m["roles"]) for r in
                                  (ROLE_HELD, ROLE_WATCH, ROLE_QMS, ROLE_PREINF, ROLE_OVERRIDE)},
                      "not_in_qms_cache": [m["ticker"] for m in out if not m["qms_row"]]},
           "held_authority": held["authority"], "held_why": held["why"],
           "population_check": {"state": "PASS" if not errors else "FAIL", "errors": errors}}
    if errors:
        raise ReviewUniverseError("ISA-0769 population invariant FAILED: " + "; ".join(errors))
    return doc


def conservation(universe_doc, reviewed: dict) -> dict:
    """Stage conservation. `reviewed` = {"scored": [tickers], "named": [tickers with a named refusal /
    discard]}. -> {state PASS|FAIL, missing_held, unexplained, counts}. input == scored + named, zero
    unexplained; a missing CURRENT_HELD_VCI member is the hard failure."""
    scored = {key(t) for t in (reviewed or {}).get("scored") or []}
    named = {key(t) for t in (reviewed or {}).get("named") or []}
    members = (universe_doc or {}).get("members") or []
    unexplained = [m["ticker"] for m in members if m["key"] not in scored and m["key"] not in named]
    missing_held = [m["ticker"] for m in members if ROLE_HELD in m["roles"] and m["key"] not in scored | named]
    return {"state": "PASS" if not unexplained else "FAIL", "missing_held": missing_held,
            "unexplained": unexplained,
            "counts": {"input": len(members), "scored": sum(1 for m in members if m["key"] in scored),
                       "named_refusal_or_rejection": sum(1 for m in members if m["key"] in named and m["key"] not in scored),
                       "unexplained": len(unexplained)}}


def check_review_universe_states() -> bool:
    """CAP-vci_review_universe must-fire through build() + conservation(): PASS, the held-name drop
    (FAIL + missing_held) and the UNKNOWN-authority refusal. RAISES on a miss."""
    held = {"state": "OK", "why": "fixture", "authority": {},
            "members": [{"key": "QBTS", "ticker": "QBTS", "holding_state": "HELD", "vci_provenance": ["f"]}]}
    u = build([{"ticker": "ALAB", "qms": 90}], 1, held=held, watchlist=["IONQ"])
    assert conservation(u, {"scored": ["ALAB", "QBTS", "IONQ"]})["state"] == "PASS"
    c = conservation(u, {"scored": ["ALAB", "IONQ"]})
    assert c["state"] == "FAIL" and c["missing_held"] == ["QBTS"]
    try:
        build([], 1, held={"state": "UNKNOWN", "why": "x"}, watchlist=[])
        raise AssertionError("UNKNOWN held authority must refuse")
    except ReviewUniverseError:
        pass
    return True


def _selftest(verbose: bool = True) -> int:
    n = [0]

    def ok(c, m):
        assert c, m
        n[0] += 1
    snap = {"_meta": {"data_date": "31-Aug-2026"}, "stocks": [
        {"ticker": "MU", "quantity": 7}, {"ticker": "ABCL", "quantity": 209}, {"ticker": "ONT", "quantity": 880},
        {"ticker": "QBTS", "quantity": 68}]}
    dec = [{"route": "vci", "ticker": "QBTS"}, {"route": "vci", "ticker": "ONT.L"}, {"route": "vci", "ticker": "BEAM"},
           {"route": "growth", "ticker": "MU"}]
    binr = {"ABCL": {"asset_structure": "platform"}, "MU": {"asset_structure": None}}
    h = current_held_vci(portfolio_doc=snap, ledger=[], decisions=dec, binaries=binr)
    ok(h["state"] == "OK" and sorted(m["key"] for m in h["members"]) == ["ABCL", "ONT", "QBTS"],
       "H1 held VCI = held names with VCI provenance (MU is growth; BEAM is not held): %s" % h["members"])
    # NEGATIVE CONTROL: a September sale after the snapshot removes the name from CURRENT_HELD_VCI
    # (synthetic fixture - real September trades are not yet in the ledger; never hard-coded)
    led = [{"date": "2026-09-15", "asset_class": "stock", "type": "sell", "ticker": "ABCL", "quantity": 209},
           {"date": "2026-09-16", "asset_class": "stock", "type": "sell", "ticker": "ONT", "quantity": 880}]
    h2 = current_held_vci(portfolio_doc=snap, ledger=led, decisions=dec, binaries=binr)
    ok(sorted(m["key"] for m in h2["members"]) == ["QBTS"],
       "H2 NEGATIVE CONTROL: sold names stay history, never CURRENT_HELD_VCI: %s" % h2["members"])
    h3 = current_held_vci(portfolio_doc=snap, ledger=[dict(led[0], quantity=300)], decisions=dec, binaries=binr)
    ok(any(m["key"] == "ABCL" and m["holding_state"] == UNKNOWN_CONFLICT for m in h3["members"]),
       "H3 an over-sale conflicts: kept IN the population, typed UNKNOWN_CONFLICT")
    ok(current_held_vci(portfolio_doc={"stocks": None})["state"] == "UNKNOWN", "H4 no snapshot -> UNKNOWN")
    # the September shape: QMS advancement omitted QBTS (held) and IONQ (watchlist)
    qms = [{"ticker": t, "qms": 90 - i} for i, t in enumerate(["ALAB", "RGTI", "ABCL", "ONT.L", "INFQ", "CRSP"])]
    qms.append({"ticker": "LOWP", "qms": 10, "pre_inflection_override": True})
    u = build(qms, 4, held=h, watchlist=["INFQ", "CRSP", "IONQ", "RGTI"], overrides=["NBIS:hyperscaler cascade"])
    keys = [m["key"] for m in u["members"]]
    ok("QBTS" in keys and "IONQ" in keys, "U1 MUST-FIRE: the held QBTS and watchlist IONQ enter the review population")
    ok(len(keys) == len(set(keys)), "U2 no duplicate rows after the union")
    ont = next(m for m in u["members"] if m["key"] == "ONT")
    ok(set(ont["roles"]) == {ROLE_HELD, ROLE_QMS} and ont["ticker"] == "ONT.L",
       "U3 duplicates merge by identity keeping every role's provenance: %s" % ont)
    q = next(m for m in u["members"] if m["key"] == "QBTS")
    ok(q["qms_row"] is False and "QBTS" in u["counts"]["not_in_qms_cache"],
       "U4 a held name missing from the QMS cache is named, not dropped")
    ok(u["counts"]["members"] == u["counts"]["inputs"] - u["counts"]["merged_duplicates"], "U5 count conservation")
    ok(any(m["key"] == "LOWP" and ROLE_PREINF in m["roles"] for m in u["members"]), "U6 pre-inflection rule unchanged")
    try:
        build(qms, 4, held={"state": "UNKNOWN", "why": "x"}, watchlist=[])
        ok(False, "U7 must raise")
    except ReviewUniverseError:
        ok(True, "U7 MUST-FIRE: UNKNOWN held authority is a hard failure, never a smaller list")
    try:
        build(qms, 4, held=h, watchlist=[], overrides=["ZZZ"])
        ok(False, "U8 must raise")
    except ReviewUniverseError:
        ok(True, "U8 an override with no reason is refused")
    # stage conservation
    c = conservation(u, {"scored": [m["ticker"] for m in u["members"]]})
    ok(c["state"] == "PASS" and c["counts"]["unexplained"] == 0, "C1 all scored -> PASS")
    c2 = conservation(u, {"scored": [m["ticker"] for m in u["members"] if m["key"] != "QBTS"]})
    ok(c2["state"] == "FAIL" and c2["missing_held"] == ["QBTS"],
       "C2 NEGATIVE CONTROL: a held name dropped between stages FIRES by name")
    c3 = conservation(u, {"scored": [m["ticker"] for m in u["members"] if m["key"] != "IONQ"], "named": ["IONQ"]})
    ok(c3["state"] == "PASS" and c3["counts"]["named_refusal_or_rejection"] == 1,
       "C3 a named refusal/rejection explains a non-scored member")
    ok(check_review_universe_states(), "CAP must-fire: PASS / held-drop FAIL / UNKNOWN refusal")
    if verbose:
        print("vci_review_universe selftest: %d checks PASS" % n[0])
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps(current_held_vci(), indent=1, default=str))
