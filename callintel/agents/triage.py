"""Triage: cheap, deterministic signal scoring per chunk (no LLM).

A 6,000-line call is mostly greetings, verification, hold time and small talk.
Chunks with no business signal skip the LLM map step entirely; the first and last
chunks are always analysed (opening intent and closing outcome live there).
The aggregated signals also give the supervisor a call-type hint for routing.
"""
from __future__ import annotations

import re
from collections import Counter

from ..config import TriageCfg
from ..normalize.catalog import ProductCatalog
from ..preprocess.transcript import Chunk

LEXICONS: dict[str, re.Pattern] = {
    "sales": re.compile(r"\b(?:offer\w*|pre-?approved|eligib\w*|upgrade|interested|apply|application|benefits?|"
                        r"rewards?|cashback|limit|emi|sign up|special|discount|waive\w*|lounge|points)\b", re.I),
    "complaint": re.compile(r"\b(?:complain\w*|unhappy|frustrat\w*|disappoint\w*|charged|refund|revers\w*|dispute\w*|"
                            r"wrong\w*|escalat\w*|manager|not acceptable|worst|fraud\w*|blocked|deducted)\b", re.I),
    "compliance": re.compile(r"\b(?:record(?:ed|ing)?|consent|terms and conditions|interest rate|apr|per annum|"
                             r"processing fee|cooling|free look|guarantee\w*|risk\w*|verif\w*|otp|cvv|pin)\b", re.I),
    "intent": re.compile(r"\b(?:i want to|i would like|i'd like|i need|calling (?:about|regarding|for)|"
                         r"looking for|can you help|reason for (?:my|the) call)\b", re.I),
    # specific phrases only: bare "go ahead" / "proceed" are conversational filler ("please go ahead")
    "outcome": re.compile(r"\b(?:go ahead (?:and|with)|proceed with|confirmed|submitted|not interested|no thanks|"
                          r"call (?:me )?back|think about it|resolved|reference number|ticket|cancel\w*|close my)\b", re.I),
    "numbers": re.compile(r"\d+(?:\.\d+)?\s?%|\b(?:rs\.?|inr|rupees?|lakhs?|crores?)\b", re.I),
}
# numbers alone (e.g. an agent reading out a statement) must not select a chunk: max 3 x 0.5 < min_score
WEIGHTS = {"product": 3.0, "sales": 1.5, "complaint": 2.0, "compliance": 1.5, "intent": 2.0, "outcome": 2.0,
           "numbers": 0.5}


def score_chunks(chunks: list[Chunk], catalog: ProductCatalog, cfg: TriageCfg) -> Counter:
    totals: Counter = Counter()
    for c in chunks:
        text = "\n".join(t.text for t in c.turns)
        sig = {name: len(rx.findall(text)) for name, rx in LEXICONS.items()}
        sig["product"] = len(catalog.find_mentions(text))
        c.signals = sig
        c.score = round(sum(WEIGHTS[k] * min(v, 3) for k, v in sig.items()), 2)
        totals.update(sig)
        first_last = cfg.always_keep_first_last and c.idx in (0, len(chunks) - 1)
        c.selected = (not cfg.enabled) or first_last or c.score >= cfg.min_score
    return totals


def call_type_hint(fact_types: Counter, signals: Counter) -> str:
    sales = fact_types["offer_made"] + fact_types["buying_signal"] + 0.2 * signals["sales"]
    complaint = fact_types["complaint"] + 0.2 * signals["complaint"]
    if sales >= 1 and complaint >= 1:
        return "mixed"
    if sales >= 1:
        return "sales"
    if complaint >= 1:
        return "complaint"
    if fact_types["intent"] or fact_types["need"]:
        return "service"
    return "enquiry"


HINT_TO_CALL_TYPES = {
    "sales": {"sales"},
    "complaint": {"complaint", "service"},
    "service": {"service"},
    "enquiry": {"service", "enquiry"},
    "mixed": {"sales", "service", "complaint", "mixed"},
}
