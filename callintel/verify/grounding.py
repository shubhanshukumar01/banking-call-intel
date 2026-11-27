"""Deterministic grounding checks (no LLM): quote-vs-line matching, numeric fidelity,
and the computed confidence score.

Confidence is *computed*, never self-reported by the model (LLM self-confidence is
poorly calibrated):  grounding x support x critic.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..rag.transcript_index import TranscriptIndex

_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_SUR = re.compile(r"<[A-Z_]+_\d+>")


def _norm(s: str) -> str:
    return _WS.sub(" ", s.lower()).strip(" .,!?\"'")


def quote_score(quote: str, text: str) -> float:
    q, t = _norm(quote), _norm(text)
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0
    if len(q) > len(t):
        return fuzz.ratio(q, t) / 100.0
    return fuzz.partial_ratio(q, t) / 100.0


def ground_quote(line_no: int, quote: str, tindex: TranscriptIndex,
                 fallback_lines: list[int] | None = None) -> tuple[int | None, float]:
    """Best (line, score) for a quote cited at `line_no`; searches +-2 lines, then the chunk."""
    best_line, best = None, 0.0
    for ln in tindex.window(line_no, 2):
        s = quote_score(quote, ln.text)
        if s > best:
            best_line, best = ln.no, s
    if best < 0.9 and fallback_lines:
        for no in fallback_lines:
            ln = tindex.line(no)
            if ln is None:
                continue
            s = quote_score(quote, ln.text)
            if s > best:
                best_line, best = ln.no, s
    return best_line, best


def numbers_in(text: str) -> set[str]:
    out = set()
    for m in _NUM.finditer(_SUR.sub(" ", text)):
        v = m.group(0).replace(",", "")
        if "." in v:
            v = v.rstrip("0").rstrip(".")
        out.add(v)
    return out


def number_supported(number: str | None, texts: list[str]) -> bool:
    """True if every figure in `number` appears in the cited text."""
    if not number:
        return True
    wanted = numbers_in(number)
    if not wanted:  # spoken number ("two point five percent") - fall back to fuzzy text match
        return any(quote_score(number, t) >= 0.85 for t in texts)
    have: set[str] = set()
    for t in texts:
        have |= numbers_in(t)
    return wanted <= have


CRITIC_FACTOR = {"supported": 1.0, "partially_supported": 0.8, "unsupported": 0.3, None: 0.92}


def confidence(grounding: float, n_evidence: int, verdict: str | None) -> float:
    if n_evidence <= 0:
        return 0.0
    support = 0.9 + 0.1 * min(n_evidence, 3) / 3
    return round(max(0.0, min(1.0, grounding * support * CRITIC_FACTOR.get(verdict, 0.92))), 3)
