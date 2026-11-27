"""Standard metric functions used by the evaluation harness.

All metrics are plain, reproducible definitions (accuracy, precision/recall/F1,
rates, percentiles) so results are comparable across models, prompts and releases.
"""
from __future__ import annotations

import re
from statistics import mean
from typing import Any, Iterable

from rapidfuzz import fuzz

_D = re.compile(r"\D")


def prf(pred: Iterable[str], gold: Iterable[str]) -> dict[str, float]:
    p, g = set(pred), set(gold)
    tp = len(p & g)
    prec = tp / len(p) if p else (1.0 if not g else 0.0)
    rec = tp / len(g) if g else 1.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4), "tp": tp,
            "n_pred": len(p), "n_gold": len(g)}


def micro_prf(pairs: list[tuple[Iterable[str], Iterable[str]]]) -> dict[str, float]:
    tp = n_p = n_g = 0
    for pred, gold in pairs:
        p, g = set(pred), set(gold)
        tp += len(p & g)
        n_p += len(p)
        n_g += len(g)
    prec = tp / n_p if n_p else 1.0
    rec = tp / n_g if n_g else 1.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def accuracy(pairs: list[tuple[Any, Any]]) -> float:
    pairs = [(p, g) for p, g in pairs if g is not None]
    return round(sum(1 for p, g in pairs if p == g) / len(pairs), 4) if pairs else 1.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


def pii_variants(value: str, ptype: str) -> list[str]:
    """Forms a leaked value could take (raw, digits-only, digits with spaces)."""
    out = {value}
    digits = _D.sub("", value)
    if ptype in ("CARD", "PHONE", "ACCOUNT", "AADHAAR") and len(digits) >= 6:
        out |= {digits, " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))}
    if ptype == "PERSON":
        out |= {t for t in value.split() if len(t) >= 4}
    if ptype == "EMAIL":
        local = value.split("@")[0]
        out |= {local, local.replace(".", " dot "), value.replace("@", " at ").replace(".", " dot ")}
    return [o for o in out if o]


def contains_pii(text: str, value: str, ptype: str) -> bool:
    low = text.lower()
    for v in pii_variants(value, ptype):
        if ptype == "PERSON":
            if re.search(rf"\b{re.escape(v.lower())}\b", low):
                return True
        elif v.lower() in low:
            return True
    return False


def quote_grounded(quote: str, line_text: str, threshold: float = 0.8) -> bool:
    q, t = quote.lower().strip(" .\"'"), line_text.lower()
    if not q:
        return False
    if q.endswith("..."):
        q = q[:-3]
    return q in t or fuzz.partial_ratio(q, t) / 100 >= threshold


def mean_or_zero(xs: list[float]) -> float:
    return round(mean(xs), 4) if xs else 0.0
