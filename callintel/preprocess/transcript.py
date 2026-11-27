"""Transcript parsing, speaker-role inference, turn building, chunking and the full
preprocessing stage (parse -> denoise -> mask PII -> turns -> chunks).

Everything here is deterministic CPU work; it runs in a worker thread so the asyncio
loop keeps LLM calls of other files flowing. Original line numbers are preserved on
every segment, so every LLM fact can be traced back to the source file.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from ..utils import Stopwatch, estimate_tokens
from .cleaning import NoiseCleaner
from .pii import MaskResult, PIIMasker, Recognizer

AGENT, CUSTOMER, SYSTEM, UNKNOWN = "AGENT", "CUSTOMER", "SYSTEM", "UNKNOWN"
ROLE_CODE = {AGENT: "A", CUSTOMER: "C", SYSTEM: "S", UNKNOWN: "?"}

_LINE = re.compile(
    r"^\s*(?:[\[(]?(?P<ts>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)[\])]?\s*[-–|]?\s*)?"
    r"(?:(?P<spk>[A-Za-z][A-Za-z0-9 ._'-]{0,30}?)\s*(?:[\[(]\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\])])?\s*[:：>]\s*)?"
    r"(?P<text>.*)$")
_AGENT_CUES = re.compile(r"thank you for calling|how (?:may|can) i (?:help|assist)|is there anything else|"
                         r"this call (?:is|may be) recorded|let me check|as per (?:our|the) (?:policy|records)|"
                         r"may i know|for verification|i can help you|please hold|calling from", re.IGNORECASE)
_CUSTOMER_CUES = re.compile(r"\bi want to\b|\bi need\b|\bi'd like\b|\bmy card\b|\bmy account\b|"
                            r"\bi got charged\b|\bwhy (?:was|is) my\b|\bi called\b", re.IGNORECASE)


@dataclass
class Line:
    no: int
    speaker: str
    text: str
    ts: str | None = None


@dataclass
class Segment:
    line: int
    text: str


@dataclass
class Turn:
    idx: int
    speaker: str
    segments: list[Segment]

    @property
    def line_start(self) -> int:
        return self.segments[0].line

    @property
    def line_end(self) -> int:
        return self.segments[-1].line

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def render(self) -> str:
        return f"{self.line_start}|{ROLE_CODE.get(self.speaker, '?')}: {self.text}"


@dataclass
class Chunk:
    idx: int
    turns: list[Turn]
    tokens: int
    overlap_turns: int = 0
    score: float = 0.0
    signals: dict[str, int] = field(default_factory=dict)
    selected: bool = True

    @property
    def line_start(self) -> int:
        return self.turns[0].line_start

    @property
    def line_end(self) -> int:
        return self.turns[-1].line_end

    def render(self) -> str:
        return "\n".join(t.render() for t in self.turns)


@dataclass
class PreparedTranscript:
    call_id: str
    file_name: str
    sha256: str
    raw_line_count: int
    lines: dict[int, Line]            # kept lines, masked + cleaned, by original line number
    turns: list[Turn]
    chunks: list[Chunk]
    pii_counts: Counter
    pii_mapping: dict[str, str]       # surrogate -> original; NEVER serialised / logged
    noise_stats: dict[str, int]
    timings_ms: dict[str, float]
    detectors: list[str]

    @property
    def masked_token_count(self) -> int:
        return sum(estimate_tokens(t.render()) for t in self.turns)


# ----------------------------------------------------------------------------- parsing
def _label_role_map(labels: Counter, lines_by_label: dict[str, list[str]], settings: Settings) -> dict[str, str]:
    sp = settings.preprocess.speakers
    known = {**{x.lower(): AGENT for x in sp.agent}, **{x.lower(): CUSTOMER for x in sp.customer},
             **{x.lower(): SYSTEM for x in sp.system}}
    total = sum(labels.values()) or 1
    roles: dict[str, str] = {}
    unknown: list[str] = []
    for lab, cnt in labels.items():
        key = lab.lower().strip()
        if key in known:
            roles[lab] = known[key]
        elif cnt / total >= 0.03 and len(key.split()) <= 3:
            unknown.append(lab)  # frequent label that is probably a person's name
    if unknown:
        def agent_score(lab: str) -> int:
            txt = "\n".join(lines_by_label.get(lab, [])[:400])
            return len(_AGENT_CUES.findall(txt)) - len(_CUSTOMER_CUES.findall(txt))
        ranked = sorted(unknown, key=agent_score, reverse=True)
        have_agent = AGENT in roles.values()
        for i, lab in enumerate(ranked):
            if not have_agent and i == 0 and agent_score(lab) > 0:
                roles[lab] = AGENT
                have_agent = True
            else:
                roles[lab] = CUSTOMER
    return roles


def parse_lines(raw_text: str, settings: Settings) -> list[Line]:
    raw_lines = raw_text.splitlines()
    parsed: list[tuple[int, str | None, str | None, str]] = []
    labels: Counter = Counter()
    by_label: dict[str, list[str]] = {}
    for i, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        spk = (m.group("spk") or "").strip() if m else ""
        text = (m.group("text") if m else raw).strip()
        ts = m.group("ts") if m else None
        if spk:
            labels[spk] += 1
            by_label.setdefault(spk, []).append(text)
        parsed.append((i, spk or None, ts, text))
    roles = _label_role_map(labels, by_label, settings)
    out: list[Line] = []
    current = UNKNOWN
    for no, spk, ts, text in parsed:
        if spk and spk in roles:
            current = roles[spk]
        elif spk:  # rare label that is not a speaker (e.g. "Note: ...") -> keep as text
            text = f"{spk}: {text}"
        out.append(Line(no, current, text, ts))
    return out


# ----------------------------------------------------------------------------- turns & chunks
def build_turns(lines: list[Line], merge: bool, max_chars: int) -> list[Turn]:
    turns: list[Turn] = []
    for ln in lines:
        if (merge and turns and turns[-1].speaker == ln.speaker
                and len(turns[-1].text) + len(ln.text) <= max_chars):
            turns[-1].segments.append(Segment(ln.no, ln.text))
        else:
            turns.append(Turn(len(turns), ln.speaker, [Segment(ln.no, ln.text)]))
    return turns


def _split_long_turn(turn: Turn, max_tokens: int) -> list[Turn]:
    parts: list[Turn] = []
    cur: list[Segment] = []
    cur_tok = 0
    for seg in turn.segments:
        t = estimate_tokens(seg.text)
        if cur and cur_tok + t > max_tokens:
            parts.append(Turn(turn.idx, turn.speaker, cur))
            cur, cur_tok = [], 0
        cur.append(seg)
        cur_tok += t
    if cur:
        parts.append(Turn(turn.idx, turn.speaker, cur))
    return parts


def chunk_turns(turns: list[Turn], target_tokens: int, max_tokens: int, overlap_turns: int) -> list[Chunk]:
    """Turn-aware chunking: never splits a turn (unless one turn alone exceeds max),
    carries `overlap_turns` turns of context into the next chunk."""
    units: list[Turn] = []
    for t in turns:
        units.extend(_split_long_turn(t, max_tokens) if estimate_tokens(t.render()) > max_tokens else [t])
    chunks: list[Chunk] = []
    cur: list[Turn] = []
    cur_tok = 0
    overlap_n = 0
    for u in units:
        ut = estimate_tokens(u.render()) + 1
        if cur and cur_tok + ut > target_tokens and len(cur) > overlap_n:
            chunks.append(Chunk(len(chunks), cur, cur_tok, overlap_n))
            carry = cur[-overlap_turns:] if overlap_turns > 0 else []
            cur = list(carry)
            cur_tok = sum(estimate_tokens(t.render()) + 1 for t in cur)
            overlap_n = len(cur)
        cur.append(u)
        cur_tok += ut
    if cur and len(cur) > overlap_n:
        chunks.append(Chunk(len(chunks), cur, cur_tok, overlap_n))
    return chunks


# ----------------------------------------------------------------------------- stage entrypoint
_BACKCHANNEL_ONLY = re.compile(r"^(?:mm+|hmm+|uh-huh|mm-hmm|mhm)[.!?]*$", re.IGNORECASE)
_TEMPLATE_NUM = re.compile(r"\d[\d,.]*(?:st|nd|rd|th)?")
_TEMPLATE_PUNCT = re.compile(r"[^\w#\s]")


def prepare_transcript(raw_text: str, *, call_id: str, file_name: str, sha256: str, settings: Settings,
                       cheap: list[Recognizer], ner: list[Recognizer],
                       protected_spans: Callable[[str], list[tuple[int, int]]] | None) -> PreparedTranscript:
    sw = Stopwatch()
    pp = settings.preprocess
    with sw.stage("parse"):
        lines = parse_lines(raw_text, settings)
    raw_count = len(raw_text.splitlines())
    stats: Counter = Counter(raw_lines=raw_count, nonempty_lines=len(lines))

    with sw.stage("denoise"):
        cleaner = NoiseCleaner(pp.remove_fillers, pp.collapse_repeats, settings.privacy.spoken_digits)
        keep_sys = re.compile("|".join(map(re.escape, pp.keep_system_if_matches)), re.IGNORECASE) \
            if pp.keep_system_if_matches else None
        kept: list[Line] = []
        prev_key: tuple[str, str] | None = None
        seen_templates: Counter = Counter()
        for ln in lines:
            if ln.speaker == SYSTEM and pp.drop_system_turns and not (keep_sys and keep_sys.search(ln.text)):
                stats["dropped_system"] += 1
                continue
            if pp.drop_backchannels and _BACKCHANNEL_ONLY.match(ln.text.strip()):
                stats["dropped_backchannel"] += 1
                continue
            text = cleaner.clean(ln.text)
            if not text:
                stats["dropped_empty_after_clean"] += 1
                continue
            key = (ln.speaker, text.lower())
            if key == prev_key:
                stats["dropped_duplicate"] += 1
                continue
            prev_key = key
            # Repetition compression: hold loops ("still checking, please stay on the line" x50) and
            # read-outs ("on the 9th there is a debit of rupees 1,250 ...") repeat one template.
            # Keep the first N; short answers ("yes", "I agree") are never touched.
            if pp.max_repeats and len(text.split()) >= pp.repeat_min_words:
                tmpl = (ln.speaker, _TEMPLATE_PUNCT.sub("", _TEMPLATE_NUM.sub("#", text.lower())).strip())
                seen_templates[tmpl] += 1
                if seen_templates[tmpl] > pp.max_repeats:
                    stats["dropped_repetitive"] += 1
                    continue
            kept.append(Line(ln.no, ln.speaker, text, ln.ts))

    with sw.stage("pii_mask"):
        masker = PIIMasker(cheap, ner, protected_spans, settings.privacy.allow_terms)
        mres: MaskResult = masker.mask_lines([(ln.text, ln.speaker) for ln in kept])
        for ln, masked in zip(kept, mres.lines):
            ln.text = masked

    with sw.stage("chunk"):
        turns = build_turns(kept, pp.merge_same_speaker, pp.max_merged_chars)
        ch = settings.chunking
        chunks = chunk_turns(turns, ch.target_tokens, ch.max_tokens, ch.overlap_turns)

    stats.update(kept_lines=len(kept), turns=len(turns), chunks=len(chunks),
                 raw_tokens=estimate_tokens(raw_text),
                 clean_tokens=sum(estimate_tokens(t.render()) for t in turns))
    return PreparedTranscript(
        call_id=call_id, file_name=file_name, sha256=sha256, raw_line_count=raw_count,
        lines={ln.no: ln for ln in kept}, turns=turns, chunks=chunks,
        pii_counts=mres.counts, pii_mapping=mres.mapping, noise_stats=dict(stats),
        timings_ms=sw.marks, detectors=mres.detectors,
    )
