"""Deterministic noise handling for ASR call transcripts (no LLM involved).

Removes: ASR tags ([inaudible], [crosstalk], [hold music]...), fillers (um, uh, erm,
mm-hmm), stutters ("I I I want"), duplicated ASR segments, IVR/system prompts and
backchannel-only lines. Preserves meaning-bearing short answers ("yes", "no",
"okay", "not", "cancel") because they carry consent / intent.

Also normalises spoken digit strings ("four one one one double two ...") into digits
so PII detectors can find card / account / phone numbers read out loud.
"""
from __future__ import annotations

import re

_DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "nil": "0", "nought": "0", "one": "1", "two": "2",
                "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
                "nine": "9", "niner": "9"}
_AMBIGUOUS = {"oh", "o"}
_MULT = {"double": 2, "triple": 3}
_TOKEN = re.compile(r"[A-Za-z]+|\d+")
_RUN_SEP = re.compile(r"^[\s,\-–.]*$")

_TAGS = re.compile(
    r"[\[\(<{]\s*(?:inaudible|unintelligible|indiscernible|crosstalk|cross talk|overlapping(?: speech)?|"
    r"laughter|laughs|laughing|noise|background noise|music|hold music|silence|pause|beep|static|"
    r"cough(?:s|ing)?|sighs?|breath(?:ing)?|unclear|foreign|no speech|blank_audio|applause|ringing|"
    r"dial tone|hold)\b[^\]\)>}]*[\]\)>}]", re.IGNORECASE)
_FILLERS = re.compile(r"(?<![\w'-])(?:u+m+|u+h+|e+r+m+|e+r+|a+h+|h+m+|m+h*m+|uh-huh|mm-hmm|mhm|um-hmm)(?![\w'-])[,.]?",
                      re.IGNORECASE)
_REPEAT = re.compile(r"\b(\w+)(?:[\s,]+\1\b)+", re.IGNORECASE)
_SPACES = re.compile(r"\s{2,}")
_ORPHAN_PUNCT = re.compile(r"^\W+$")


def normalize_spoken_digits(text: str, min_run: int = 4) -> str:
    """'nine eight seven six five' -> '98765' when a run has >= min_run digits and at
    least one spoken digit word. Pure numeric text is left for the regexes."""
    toks = list(_TOKEN.finditer(text))
    if not toks:
        return text
    out: list[str] = []
    last = i = 0
    n = len(toks)
    while i < n:
        digits, has_word, mult = "", False, 1
        j, start, end = i, toks[i].start(), toks[i].end()
        while j < n:
            if j > i and not _RUN_SEP.match(text[toks[j - 1].end():toks[j].start()]):
                break
            w = toks[j].group()
            lw = w.lower()
            if lw in _MULT and j + 1 < n and (toks[j + 1].group().lower() in _DIGIT_WORDS or toks[j + 1].group().isdigit()):
                mult, has_word = _MULT[lw], True
                j += 1
                continue
            if w.isdigit():
                d = w
            elif lw in _DIGIT_WORDS and (lw not in _AMBIGUOUS or digits):
                d, has_word = _DIGIT_WORDS[lw], True
            else:
                break
            digits += d * mult if len(d) == 1 else d
            mult = 1
            end = toks[j].end()
            j += 1
        if has_word and len(digits) >= min_run:
            out.append(text[last:start])
            out.append(digits)
            last = end
            i = j
        else:
            i += 1
    out.append(text[last:])
    return "".join(out)


def _collapse_repeats(text: str) -> str:
    def repl(m: re.Match) -> str:
        word = m.group(1)
        # never collapse digits / digit words: "one one one" may be part of a number
        if word.isdigit() or word.lower() in _DIGIT_WORDS:
            return m.group(0)
        return word
    return _REPEAT.sub(repl, text)


class NoiseCleaner:
    def __init__(self, remove_fillers: bool = True, collapse_repeats: bool = True,
                 spoken_digits: bool = True) -> None:
        self.remove_fillers = remove_fillers
        self.collapse_repeats = collapse_repeats
        self.spoken_digits = spoken_digits

    def clean(self, text: str) -> str:
        t = _TAGS.sub(" ", text)
        if self.remove_fillers:
            t = _FILLERS.sub(" ", t)
        if self.spoken_digits:
            t = normalize_spoken_digits(t)
        if self.collapse_repeats:
            t = _collapse_repeats(t)
        t = _SPACES.sub(" ", t).strip(" ,")
        if _ORPHAN_PUNCT.match(t or "x"):
            return ""
        return t
