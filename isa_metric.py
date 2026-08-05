"""
isa_metric.py -- Engineering Standard rule 1 + 2, enforced as types.

    1. "Missing" cannot be a number.
    2. Every figure carries as_of + source.

A decision-grade number is a Metric. An absent one is a Missing carrying a REASON.
Missing deliberately raises on every arithmetic and float coercion, so the
absent-read-as-zero class (score_panel=0, FETCH_WORKERS undefined, source_score
absent-read-as-zero) becomes a loud TypeError at the boundary instead of a
plausible wrong answer three steps downstream.

Never write `d.get(k, 0)` for a decision-grade number. Use `get_metric()`.
"""
from __future__ import annotations
import datetime as _dt

__all__ = ["Metric", "Missing", "MissingValueError", "require", "value_or",
           "get_metric", "is_present", "as_dict", "from_dict", "fmt"]

SCHEMA_VERSION = 1


class MissingValueError(TypeError):
    """Raised when an absent value is used as if it were a number."""


def _iso(d) -> str:
    if d is None:
        raise ValueError("as_of is mandatory (Engineering Standard rule 2)")
    if isinstance(d, str):
        return d[:10]
    if isinstance(d, (_dt.date, _dt.datetime)):
        return d.strftime("%Y-%m-%d")
    raise TypeError(f"as_of must be date or ISO string, got {type(d).__name__}")


class Metric:
    """A present number that knows when it was true and where it came from."""
    __slots__ = ("value", "as_of", "source", "confidence", "unit", "note")

    def __init__(self, value, as_of, source, confidence=1.0, unit="", note=""):
        if value is None:
            raise MissingValueError(
                "Metric(value=None) is forbidden -- return Missing(reason) instead")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Metric value must be numeric, got {type(value).__name__}")
        if not source:
            raise ValueError("source is mandatory (Engineering Standard rule 2)")
        if not (0.0 <= float(confidence) <= 1.0):
            raise ValueError(f"confidence must be 0..1, got {confidence}")
        object.__setattr__(self, "value", float(value))
        object.__setattr__(self, "as_of", _iso(as_of))
        object.__setattr__(self, "source", str(source))
        object.__setattr__(self, "confidence", float(confidence))
        object.__setattr__(self, "unit", str(unit))
        object.__setattr__(self, "note", str(note))

    def __setattr__(self, *a):
        raise AttributeError("Metric is immutable")

    def __repr__(self):
        u = self.unit or ""
        return f"Metric({self.value:g}{u} @{self.as_of} src={self.source})"

    # Explicit unwrap only. No __float__: a Metric must not silently become a
    # bare float, because a bare float loses as_of and source (rule 2).
    def unwrap(self):
        return self.value

    def with_value(self, v):
        return Metric(v, self.as_of, self.source, self.confidence, self.unit, self.note)

    def stale_days(self, reference):
        ref = _dt.date.fromisoformat(_iso(reference))
        return (ref - _dt.date.fromisoformat(self.as_of)).days


class Missing:
    """An absent number that knows WHY it is absent. Refuses to be a number."""
    __slots__ = ("reason", "as_of", "source")

    def __init__(self, reason, as_of=None, source=None):
        if not reason:
            raise ValueError("Missing requires a reason -- a bare None is the bug")
        object.__setattr__(self, "reason", str(reason))
        object.__setattr__(self, "as_of", _iso(as_of) if as_of else None)
        object.__setattr__(self, "source", source)

    def __setattr__(self, *a):
        raise AttributeError("Missing is immutable")

    def __repr__(self):
        return f"Missing({self.reason!r})"

    def _boom(self, *a, **k):
        raise MissingValueError(
            f"absent value used as a number: {self.reason}. "
            "Handle it explicitly with is_present()/value_or() -- do not default it to 0.")

    # every arithmetic / coercion path is a hard error
    __float__ = __int__ = __index__ = __complex__ = _boom
    __add__ = __radd__ = __sub__ = __rsub__ = _boom
    __mul__ = __rmul__ = __truediv__ = __rtruediv__ = _boom
    __floordiv__ = __rfloordiv__ = __pow__ = __rpow__ = _boom
    __neg__ = __pos__ = __abs__ = __round__ = _boom
    __lt__ = __le__ = __gt__ = __ge__ = _boom

    def unwrap(self):
        self._boom()


def is_present(m) -> bool:
    return isinstance(m, Metric)


def require(m, what="value"):
    """Unwrap or raise. Use where absence genuinely cannot be tolerated."""
    if isinstance(m, Metric):
        return m.value
    if isinstance(m, Missing):
        raise MissingValueError(f"{what} is required but absent: {m.reason}")
    raise TypeError(f"{what} must be Metric or Missing, got {type(m).__name__}")


def value_or(m, default):
    """Explicit, auditable opt-in to a default. The ONLY sanctioned way to
    substitute for an absent value -- and it is visible at the call site."""
    return m.value if isinstance(m, Metric) else default


def get_metric(d: dict, key, as_of, source, unit="", confidence=1.0):
    """dict -> Metric | Missing. Replaces every `d.get(key, 0)` on a
    decision-grade number."""
    if not isinstance(d, dict) or key not in d:
        return Missing(f"key {key!r} absent from {source}", as_of, source)
    v = d[key]
    if v is None:
        return Missing(f"key {key!r} is null in {source}", as_of, source)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return Missing(f"key {key!r} is non-numeric ({type(v).__name__}) in {source}",
                       as_of, source)
    return Metric(v, as_of, source, confidence, unit)


def as_dict(m):
    """Serialise for JSON artefacts -- as_of and source always travel with it."""
    if isinstance(m, Metric):
        return {"value": m.value, "as_of": m.as_of, "source": m.source,
                "confidence": m.confidence, "unit": m.unit,
                "note": m.note, "present": True}
    if isinstance(m, Missing):
        return {"value": None, "as_of": m.as_of, "source": m.source,
                "reason": m.reason, "present": False}
    raise TypeError(f"not a Metric/Missing: {type(m).__name__}")


def from_dict(d):
    if not isinstance(d, dict) or "present" not in d:
        raise TypeError("not a serialised Metric/Missing")
    if d["present"]:
        return Metric(d["value"], d["as_of"], d["source"],
                      d.get("confidence", 1.0), d.get("unit", ""), d.get("note", ""))
    return Missing(d.get("reason", "unspecified"), d.get("as_of"), d.get("source"))


def fmt(m, dp=2, suffix="%", dated=True):
    """Render for the email/dashboard. Rule 2: an undated figure is a build
    error, so the as_of is appended unless the caller shows it in a column."""
    if isinstance(m, Missing):
        return f"n/a ({m.reason})" if len(m.reason) < 40 else "n/a"
    s = f"{m.value:+.{dp}f}{suffix}"
    return f"{s} ({m.as_of})" if dated else s
