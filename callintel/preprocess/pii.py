"""Local PII detection and masking (runs before anything leaves the process).

Layered detectors (configurable via privacy.detectors):
  1. regex    - structured identifiers with checksum validation
                (card + Luhn, Aadhaar + Verhoeff, PAN, IFSC, UPI, IBAN, SSN, phones, e-mail, long IDs)
  2. context  - cue-phrase recognisers that catch what regexes cannot:
                names after "my name is / speaking with / Mr.", account numbers after "account number",
                OTP/CVV/PIN, date of birth, address, PIN code, customer/reference IDs, spoken e-mails
  3. presidio - Microsoft Presidio NER (optional)
  4. gliner   - GLiNER multi-PII NER (optional)

Masking is two-pass per file: pass 1 detects everything (and learns the call's names),
pass 2 masks every line - so a name said *before* the introduction is still masked.
Surrogates are consistent per call: the same value always becomes the same token
(<CUST_NAME_1>, <CARD_1>, ...), so the LLM can still reason about "the same card".
Product names and allow-listed terms (e.g. the bank's name) are protected from NER.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

log = logging.getLogger(__name__)

SURROGATE_RE = re.compile(r"<[A-Z][A-Z0-9_]*_\d+>")

PREFIX = {
    "PERSON": "NAME", "CARD": "CARD", "ACCOUNT": "ACCT", "PHONE": "PHONE", "EMAIL": "EMAIL",
    "AADHAAR": "AADHAAR", "PAN": "PAN", "IFSC": "IFSC", "UPI": "UPI", "OTP": "OTP", "CVV": "CVV",
    "PIN": "PIN", "DOB": "DOB", "ADDRESS": "ADDR", "PINCODE": "PINCODE", "SSN": "SSN", "IBAN": "IBAN",
    "REFERENCE": "REF", "ID_NUMBER": "IDNUM", "LOCATION": "LOC", "PASSPORT": "PASSPORT",
    "PARTIAL_NUMBER": "NUM",
}
# Types that NER may over-fire on - these respect product/allow-list protection.
SOFT_TYPES = {"PERSON", "LOCATION", "ADDRESS"}


# ----------------------------------------------------------------------------- validators
def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_V_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
        [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
        [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
        [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_V_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
        [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
        [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) != 12:
        return False
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[i % 8][d]]
    return c == 0


# ----------------------------------------------------------------------------- spans
@dataclass(frozen=True)
class Span:
    start: int
    end: int
    etype: str
    text: str
    score: float = 1.0
    source: str = "regex"
    role: str | None = None  # CUSTOMER / AGENT for names


class Recognizer(Protocol):
    name: str

    def analyze(self, text: str, speaker: str | None = None) -> list[Span]: ...


@dataclass
class _Pat:
    etype: str
    regex: re.Pattern
    group: int = 0
    validator: Callable[[str], bool] | None = None
    score: float = 0.9


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


class RegexRecognizer:
    """High-precision structured identifiers (also used by the egress guard)."""

    name = "regex"

    def __init__(self) -> None:
        self.patterns: list[_Pat] = [
            _Pat("EMAIL", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), score=0.99),
            _Pat("UPI", re.compile(r"(?<![\w.-])[A-Za-z0-9._-]{2,64}@[A-Za-z]{2,32}\b(?!\.[A-Za-z])"), score=0.9),
            _Pat("CARD", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), validator=luhn_ok, score=0.97),
            _Pat("AADHAAR", re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)"), validator=verhoeff_ok, score=0.96),
            _Pat("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), score=0.9),
            _Pat("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), score=0.85),
            _Pat("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), score=0.8),
            _Pat("SSN", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"), score=0.85),
            _Pat("PHONE", re.compile(r"(?<![\d+])(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)"), score=0.88),
            _Pat("PHONE", re.compile(r"(?<![\w+])\+\d{1,3}[ -]?\(?\d{2,4}\)?[ -]?\d{3,4}[ -]?\d{3,4}(?!\d)"), score=0.85),
            _Pat("PHONE", re.compile(r"(?<!\d)\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"), score=0.7),
            # Catch-all: any remaining run of >= 9 digits is treated as an identifier
            _Pat("ID_NUMBER", re.compile(r"(?<!\d)\d(?:[ -]?\d){8,22}(?!\d)"), score=0.6),
        ]

    def analyze(self, text: str, speaker: str | None = None) -> list[Span]:
        spans: list[Span] = []
        for p in self.patterns:
            for m in p.regex.finditer(text):
                val = m.group(p.group)
                if p.validator and not p.validator(val):
                    continue
                if SURROGATE_RE.fullmatch(val):
                    continue
                spans.append(Span(m.start(p.group), m.end(p.group), p.etype, val, p.score, self.name))
        return spans


_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# Words that can follow a name cue but are not names (lower-case transcripts need this).
COMMON_WORDS = set("""
a an the and or but so to of in on at for from with by about as is am are was were be been being it its this that these
those there here what which who whom whose when where why how i me my mine we us our you your yours he him his she her
they them their not no yes yeah yep okay ok fine good great well just only also very really sure right correct actually
calling call called speaking talking regarding related concerning trying going looking wanting wondering checking hoping
glad happy sorry afraid interested unable able sure calling from bank team department customer care service support
executive manager officer agent representative advisor sir madam maam mam dear please thank thanks hello hi hey
mister miss mrs ms mr dr one two three four five six seven eight nine ten
card credit debit loan account savings personal home car fixed deposit insurance policy number amount fee fees charge
charges interest rate limit balance payment statement branch app online net mobile phone email address problem issue
complaint request query application offer plan scheme reward rewards cashback platinum gold silver premium classic
today tomorrow yesterday now then still already again currently basically honestly literally
not having getting doing done told telling asking said saying know knew think thought feel felt want need would could
should will can may might must shall do does did have has had get got make made see saw let lets give gave take took
new old same other another some any all every each much many more most less few little lot lots bit
out up down off over into onto back away due date payment payments emi deadline chance opportunity anything something
nothing everything someone anyone nobody everybody waiting holding line moment second minute while
""".split())

_NAME_TOKEN = r"[A-Za-z][a-z'-]{1,20}|[A-Z][A-Za-z'-]{1,20}"


class ContextRecognizer:
    """Cue-phrase recognisers for unstructured / spoken PII."""

    name = "context"

    def __init__(self) -> None:
        I = re.IGNORECASE
        self.patterns: list[_Pat] = [
            _Pat("ACCOUNT", re.compile(r"\b(?:account|a/c|acct|ac)\s*(?:number|no\.?|num|#)?\s*(?:is|was|:|-)?\s*((?:\d[ -]?){5,22}\d)", I), 1, score=0.95),
            _Pat("PARTIAL_NUMBER", re.compile(r"\b(?:ending|ends|last (?:four|4) digits?)\s*(?:with|in|is|are|of|:)?\s*(\d{4})\b", I), 1, score=0.8),
            _Pat("OTP", re.compile(r"\b(?:otp|one[ -]time password|verification code|security code|passcode)\b\D{0,25}?(\d{4,8})\b", I), 1, score=0.95),
            _Pat("CVV", re.compile(r"\b(?:cvv|cvc|card verification)\b\D{0,20}?(\d{3,4})\b", I), 1, score=0.95),
            _Pat("PIN", re.compile(r"\b(?:atm pin|card pin|m-?pin|pin number|my pin)\b\D{0,15}?(\d{4,6})\b", I), 1, score=0.95),
            _Pat("DOB", re.compile(r"\b(?:my date of birth|my dob|my d\.o\.b\.?|i was born on|born on|my birthday is|my birth ?date)\b(?:\s+(?:is|was|:))?\s+([^.?!,;]{4,40})", I), 1, score=0.9),
            _Pat("ADDRESS", re.compile(r"\b(?:my (?:home |office |new )?address(?: is)?|address is|current address(?: is)?|i live (?:at|in)|i stay (?:at|in)|residing at|resident of|deliver(?:ed)? (?:it )?to)\s*[:,]?\s+([^.?!]{6,100})", I), 1, score=0.85),
            _Pat("PINCODE", re.compile(r"\b(?:pin ?code|postal code|zip(?: code)?)\b\D{0,10}?(\d{5,6})\b", I), 1, score=0.9),
            _Pat("PAN", re.compile(r"\bpan(?: card)?(?: number| no\.?)?\s*(?:is|:)?\s*([a-z]{5}\d{4}[a-z])\b", I), 1, score=0.9),
            _Pat("PASSPORT", re.compile(r"\bpassport(?: number| no\.?)?\s*(?:is|:)?\s*([A-Z]\d{7})\b", I), 1, score=0.9),
            _Pat("REFERENCE", re.compile(r"\b(?:customer id|customer number|crn|cif(?: number)?|client id|reference number|ref(?:erence)? no\.?|ticket number|complaint number|request number|application number|loan account number|srn)\b\s*(?:is|:|-)?\s*([A-Z0-9][A-Z0-9-]{3,20})", I), 1, score=0.9),
            _Pat("EMAIL", re.compile(r"\b([a-z0-9]+(?:\s*(?:dot|underscore|_|\.)\s*[a-z0-9]+){0,4}\s+at(?: the rate)?\s+(?:gmail|yahoo|hotmail|outlook|rediffmail|icloud|protonmail|[a-z]+mail)\s*(?:dot|\.)\s*(?:com|in|net|org|co\s*(?:dot|\.)\s*in))\b", I), 1, score=0.9),
        ]
        # Dialogue-aware detection: a question from one speaker tells us what the
        # other speaker's next answer contains ("date of birth?" -> "12th March 1990").
        self.question_cues: list[tuple[str, re.Pattern]] = [
            ("DOB", re.compile(r"\b(?:date of birth|dob|d o b|born|birth ?date)\b", I)),
            ("PERSON", re.compile(r"\b(?:your (?:full |registered )?name|who am i speaking|may i (?:know|have) (?:your|the) name|name please|name of the (?:card ?holder|account ?holder))\b", I)),
            ("ACCOUNT", re.compile(r"\b(?:account number|a/c number|account no)\b", I)),
            ("CARD", re.compile(r"\bcard number\b", I)),
            ("PHONE", re.compile(r"\b(?:mobile|phone|contact|registered) number\b", I)),
            ("ADDRESS", re.compile(r"\b(?:your|the|registered|current|delivery|communication) address\b", I)),
            ("PINCODE", re.compile(r"\b(?:pin ?code|zip ?code|postal code)\b", I)),
            ("OTP", re.compile(r"\b(?:otp|one time password|verification code)\b", I)),
        ]
        self._answer_lead = re.compile(r"^\s*(?:(?:yes|yeah|yep|sure|okay|ok|so|um|uh|it's|it is|that's|that is|"
                                       r"my name is|my name's|this is|i am|i'm|its|the number is|number is|"
                                       r"it was|correct|right)[\s,.:]+)*", I)
        self._date_answer = re.compile(r"((?:\b\w+\s+){0,3}" + _MONTHS + r"\b(?:[\s,]+\w+){0,4}|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b)", I)
        self._digit_run = re.compile(r"(?<!\d)\d(?:[ -]?\d){3,22}(?!\d)")
        cue_self = r"(?:my name is|my name's|name is|this is|i am|i'm|myself)"
        cue_other = r"(?:speaking (?:with|to)|talking (?:with|to)|am i speaking (?:with|to)|is this|mr\.?|mrs\.?|ms\.?|miss|dr\.?|shri|smt\.?|mister)"
        self.name_self = re.compile(rf"\b{cue_self}\s+((?:{_NAME_TOKEN})(?:\s+(?:{_NAME_TOKEN})){{0,2}})", I)
        self.name_other = re.compile(rf"\b{cue_other}\s+((?:{_NAME_TOKEN})(?:\s+(?:{_NAME_TOKEN})){{0,2}})", I)

    @staticmethod
    def _clean_name(raw: str, mixed_case: bool) -> str | None:
        toks: list[str] = []
        for t in raw.split():
            lt = t.lower().strip("'")
            if lt in COMMON_WORDS or len(lt) < 2:
                break
            if mixed_case and not t[0].isupper():
                break
            toks.append(t)
        return " ".join(toks) if toks else None

    def analyze(self, text: str, speaker: str | None = None) -> list[Span]:
        spans: list[Span] = []
        for p in self.patterns:
            for m in p.regex.finditer(text):
                val = m.group(p.group)
                if not val or SURROGATE_RE.search(val):
                    continue
                if p.etype == "REFERENCE" and not re.search(r"\d", val):
                    continue
                spans.append(Span(m.start(p.group), m.end(p.group), p.etype, val, p.score, self.name))
        mixed_case = any(c.isupper() for c in text[1:])
        other_role = {"AGENT": "CUSTOMER", "CUSTOMER": "AGENT"}.get(speaker or "")
        for regex, role in ((self.name_self, speaker), (self.name_other, other_role)):
            for m in regex.finditer(text):
                name = self._clean_name(m.group(1), mixed_case)
                if not name:
                    continue
                s = m.start(1)
                spans.append(Span(s, s + len(name), "PERSON", name, 0.85, self.name, role))
        return spans

    def expected_types(self, text: str) -> set[str]:
        """PII types a question in `text` asks the other party for."""
        return {et for et, rx in self.question_cues if rx.search(text)}

    def analyze_answer(self, text: str, expected: set[str], speaker: str | None) -> list[Span]:
        spans: list[Span] = []
        lead = self._answer_lead.match(text)
        body_start = lead.end() if lead else 0
        body = text[body_start:].strip(" .,!?")
        if not body:
            return spans
        if "DOB" in expected:
            for m in self._date_answer.finditer(text):
                spans.append(Span(m.start(1), m.end(1), "DOB", m.group(1), 0.85, "dialogue"))
        if expected & {"ACCOUNT", "CARD", "PHONE", "PINCODE", "OTP"}:
            for m in self._digit_run.finditer(text):
                nd = len(_digits(m.group(0)))
                if "CARD" in expected and nd >= 12:
                    et = "CARD"
                elif "PHONE" in expected and nd >= 10:
                    et = "PHONE"
                elif "ACCOUNT" in expected and nd >= 6:
                    et = "ACCOUNT"
                elif "PINCODE" in expected and nd in (5, 6):
                    et = "PINCODE"
                elif "OTP" in expected and 4 <= nd <= 8:
                    et = "OTP"
                else:
                    et = "PARTIAL_NUMBER"
                spans.append(Span(m.start(), m.end(), et, m.group(0), 0.9, "dialogue"))
        if "PERSON" in expected:
            name = self._clean_name(body, any(c.isupper() for c in body[1:]))
            if name and len(name.split()) <= 3:
                s = text.find(name, body_start)
                if s >= 0:
                    spans.append(Span(s, s + len(name), "PERSON", name, 0.8, "dialogue", speaker))
        if "ADDRESS" in expected and len(body.split()) >= 3:
            s = text.find(body, body_start)
            if s >= 0:
                spans.append(Span(s, s + len(body), "ADDRESS", body, 0.75, "dialogue"))
        return spans


class PresidioRecognizer:
    name = "presidio"
    _MAP = {"PERSON": "PERSON", "LOCATION": "LOCATION", "EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE",
            "CREDIT_CARD": "CARD", "IBAN_CODE": "IBAN", "US_SSN": "SSN", "IN_PAN": "PAN", "IN_AADHAAR": "AADHAAR",
            "US_PASSPORT": "PASSPORT", "IN_PASSPORT": "PASSPORT", "DATE_TIME": None}

    def __init__(self, model: str) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": model}]})
        self.engine = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])
        self.entities = [k for k, v in self._MAP.items() if v]

    def analyze(self, text: str, speaker: str | None = None) -> list[Span]:
        out = []
        for r in self.engine.analyze(text=text, language="en", entities=self.entities, score_threshold=0.5):
            et = self._MAP.get(r.entity_type)
            if et:
                out.append(Span(r.start, r.end, et, text[r.start:r.end], float(r.score), self.name))
        return out


class GlinerRecognizer:
    name = "gliner"
    _LABELS = {"person": "PERSON", "address": "ADDRESS", "date of birth": "DOB", "email": "EMAIL",
               "phone number": "PHONE", "bank account number": "ACCOUNT", "credit card number": "CARD",
               "passport number": "PASSPORT"}

    def __init__(self, model: str, threshold: float) -> None:
        from gliner import GLiNER

        self.model = GLiNER.from_pretrained(model)
        self.threshold = threshold

    def analyze(self, text: str, speaker: str | None = None) -> list[Span]:
        ents = self.model.predict_entities(text, list(self._LABELS), threshold=self.threshold)
        return [Span(e["start"], e["end"], self._LABELS[e["label"]], e["text"], float(e["score"]), self.name)
                for e in ents if e["label"] in self._LABELS]


# NER is expensive: only run it on lines that plausibly contain a name/address.
_NER_GATE = re.compile(r"\b(?:name|mr|mrs|ms|miss|dr|speaking|this is|i am|i'm|address|live|stay|born|"
                       r"street|road|nagar|colony|sector|city|myself)\b", re.IGNORECASE)
_CAP_INSIDE = re.compile(r"(?<=[a-z,] )[A-Z][a-z]{2,}")


def build_recognizers(detectors: Iterable[str], presidio_model: str, gliner_model: str,
                      gliner_threshold: float) -> tuple[list[Recognizer], list[Recognizer]]:
    """Returns (cheap recognisers run on every line, NER recognisers run on gated lines)."""
    cheap: list[Recognizer] = []
    ner: list[Recognizer] = []
    for d in detectors:
        try:
            if d == "regex":
                cheap.append(RegexRecognizer())
            elif d == "context":
                cheap.append(ContextRecognizer())
            elif d == "presidio":
                ner.append(PresidioRecognizer(presidio_model))
            elif d == "gliner":
                ner.append(GlinerRecognizer(gliner_model, gliner_threshold))
        except ImportError:
            log.warning("PII detector '%s' not installed -> skipped (pip install -r requirements-pii.txt)", d)
        except Exception as exc:  # model download / load failure
            log.warning("PII detector '%s' failed to load (%s) -> skipped", d, exc)
    return cheap, ner


# ----------------------------------------------------------------------------- masker
@dataclass
class MaskResult:
    lines: list[str]
    counts: Counter = field(default_factory=Counter)
    mapping: dict[str, str] = field(default_factory=dict)  # surrogate -> original (vault only)
    detectors: list[str] = field(default_factory=list)


def _canon(etype: str, value: str) -> str:
    if etype in ("CARD", "ACCOUNT", "PHONE", "AADHAAR", "ID_NUMBER", "OTP", "CVV", "PIN", "PINCODE",
                 "PARTIAL_NUMBER", "SSN"):
        d = _digits(value)
        if etype == "PHONE" and len(d) > 10:
            d = d[-10:]
        return d
    return re.sub(r"[^a-z0-9@]", "", value.lower())


class PIIMasker:
    """Stateful per-call masker. Create one per transcript."""

    def __init__(self, cheap: list[Recognizer], ner: list[Recognizer],
                 protected_spans: Callable[[str], list[tuple[int, int]]] | None = None,
                 allow_terms: Iterable[str] = ()) -> None:
        self.cheap, self.ner = cheap, ner
        self.protected_spans = protected_spans
        allow = [re.escape(t) for t in allow_terms if t]
        self.allow_re = re.compile(r"\b(?:" + "|".join(allow) + r")\b", re.IGNORECASE) if allow else None
        self._sur: dict[tuple[str, str], str] = {}
        self._seq: Counter = Counter()
        self._name_alias: dict[str, tuple[str, str]] = {}  # token -> (canonical full name, role)

    # -- surrogate registry
    def _surrogate(self, etype: str, value: str, role: str | None) -> str:
        canon = _canon(etype, value)
        if etype == "PERSON":
            alias = self._name_alias.get(canon)
            if alias:
                canon, role = alias[0], role or alias[1]
        key = (etype, canon)
        if key not in self._sur:
            prefix = PREFIX.get(etype, etype)
            if etype == "PERSON" and role in ("CUSTOMER", "AGENT"):
                prefix = "CUST_NAME" if role == "CUSTOMER" else "AGENT_NAME"
            self._seq[prefix] += 1
            self._sur[key] = f"<{prefix}_{self._seq[prefix]}>"
        return self._sur[key]

    def _detect(self, text: str, speaker: str | None) -> list[Span]:
        spans: list[Span] = []
        for rec in self.cheap:
            spans.extend(rec.analyze(text, speaker))
        if self.ner and (_NER_GATE.search(text) or _CAP_INSIDE.search(text)):
            for rec in self.ner:
                try:
                    spans.extend(rec.analyze(text, speaker))
                except Exception as exc:  # pragma: no cover - NER runtime issues must not stop masking
                    log.debug("NER %s failed on a line: %s", rec.name, exc)
        return spans

    def _protected(self, text: str) -> list[tuple[int, int]]:
        prot: list[tuple[int, int]] = []
        if self.protected_spans:
            prot.extend(self.protected_spans(text))
        if self.allow_re:
            prot.extend((m.start(), m.end()) for m in self.allow_re.finditer(text))
        return prot

    def _learn_names(self, spans: list[Span]) -> None:
        for s in spans:
            if s.etype != "PERSON":
                continue
            full = _canon("PERSON", s.text)
            if not full:
                continue
            self._name_alias.setdefault(full, (full, s.role or ""))
            for tok in s.text.split():
                lt = tok.lower()
                if len(lt) >= 3 and lt not in COMMON_WORDS:
                    self._name_alias.setdefault(_canon("PERSON", tok), (full, s.role or ""))

    def _gazetteer(self) -> re.Pattern | None:
        toks = sorted({k for k in self._name_alias if len(k) >= 3}, key=len, reverse=True)
        if not toks:
            return None
        return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in toks) + r")\b", re.IGNORECASE)

    @staticmethod
    def _resolve(spans: list[Span]) -> list[Span]:
        spans = sorted(spans, key=lambda s: (-s.score, -(s.end - s.start), s.start))
        chosen: list[Span] = []
        for s in spans:
            if all(s.end <= c.start or s.start >= c.end for c in chosen):
                chosen.append(s)
        return sorted(chosen, key=lambda s: s.start)

    def mask_lines(self, lines: list[tuple[str, str | None]]) -> MaskResult:
        """lines: [(text, speaker)] -> masked text per line (same order/length)."""
        ctx = next((r for r in self.cheap if isinstance(r, ContextRecognizer)), None)
        detected: list[list[Span]] = []
        expected: set[str] = set()
        asker: str | None = None
        answers_left = 0
        for text, spk in lines:                       # pass 1: detect + learn names
            spans = self._detect(text, spk) if text else []
            if ctx and text:
                if expected and spk != asker and answers_left > 0:
                    spans.extend(ctx.analyze_answer(text, expected, spk))
                    answers_left -= 1
                q = ctx.expected_types(text)
                if q:
                    expected, asker, answers_left = q, spk, 2
                elif spk == asker:
                    expected, answers_left = set(), 0
            detected.append(spans)
            self._learn_names(spans)
        gaz = self._gazetteer()
        res = MaskResult(lines=[], detectors=[r.name for r in self.cheap + self.ner])
        for (text, spk), spans in zip(lines, detected):  # pass 2: mask
            if not text:
                res.lines.append(text)
                continue
            if gaz:
                spans = spans + [Span(m.start(), m.end(), "PERSON", m.group(0), 0.7, "gazetteer")
                                 for m in gaz.finditer(text)]
            prot = self._protected(text) if spans else []
            if prot:
                spans = [s for s in spans if s.etype not in SOFT_TYPES
                         or all(s.end <= a or s.start >= b for a, b in prot)]
            out, last = [], 0
            for s in self._resolve(spans):
                sur = self._surrogate(s.etype, s.text, s.role)
                out.append(text[last:s.start])
                out.append(sur)
                last = s.end
                res.counts[s.etype] += 1
                res.mapping.setdefault(sur, s.text)
            out.append(text[last:])
            res.lines.append("".join(out))
        return res


def find_pii(text: str, recognizer: RegexRecognizer | None = None) -> list[Span]:
    """High-precision scan used by the egress guard and output scanner."""
    rec = recognizer or RegexRecognizer()
    return [s for s in rec.analyze(text) if s.etype != "PARTIAL_NUMBER"]
