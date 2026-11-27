"""Builds the client-facing CallIntel diagrams.

One source of truth for four figures, written in two variants:
  * export  - light colours as literals, Segoe UI -> standalone .svg (PowerPoint-editable) and 2x .png
  * page    - colours as CSS variables -> inline SVG in callintel-architecture.html (light + dark theme)

    python docs/diagrams/build_diagrams.py            # writes SVG + HTML, then PNG via headless Edge
"""
from __future__ import annotations

import html
import shutil
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent

LIGHT = {
    "bg": "#FFFFFF", "ink": "#13202A", "muted": "#55656F", "line": "#B8C4CC", "soft": "#DCE3E8",
    "surface": "#FFFFFF", "panel": "#F3F6F8",
    "privacy": "#0E7C72", "privacy_bg": "#E3F3F0",
    "ai": "#A85A06", "ai_bg": "#FDF2E4",
    "ok": "#1F7A3A", "ok_bg": "#E7F4EA",
    "danger": "#B42318", "danger_bg": "#FDECEA",
    "band": "#E2E8ED", "on_color": "#FFFFFF",
}
EXPORT_FONT = "Segoe UI, Arial, sans-serif"
EXPORT_MONO = "Consolas, Menlo, monospace"


class Pen:
    """Tiny SVG writer. `c(name)` resolves a colour for the current variant."""

    def __init__(self, fig: str, w: int, h: int, page: bool) -> None:
        self.fig, self.w, self.h, self.page = fig, w, h, page
        self.parts: list[str] = []
        self.markers: set[str] = set()

    def c(self, name: str) -> str:
        return f"var(--{name.replace('_', '-')})" if self.page else LIGHT[name]

    @property
    def font(self) -> str:
        return "var(--font-sans)" if self.page else EXPORT_FONT

    @property
    def mono(self) -> str:
        return "var(--font-mono)" if self.page else EXPORT_MONO

    # ---- primitives
    def rect(self, x, y, w, h, fill="surface", stroke="line", rx=10, dash=None, sw=1.5):
        st = f"fill:{self.c(fill) if fill else 'none'};stroke:{self.c(stroke) if stroke else 'none'};stroke-width:{sw}"
        if dash:
            st += f";stroke-dasharray:{dash}"
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" style="{st}"/>')

    def circle(self, cx, cy, r, fill):
        self.parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" style="fill:{self.c(fill)}"/>')

    def text(self, x, y, s, size=15, weight=400, color="ink", anchor="start", mono=False, spacing=0):
        fam = self.mono if mono else self.font
        st = f"fill:{self.c(color)};font-family:{fam};font-size:{size}px;font-weight:{weight}"
        if spacing:
            st += f";letter-spacing:{spacing}px"
        self.parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" style="{st}">{html.escape(s)}</text>')

    def rich(self, x, y, runs, size=15, anchor="start", mono=False):
        """runs: [(text, color, weight)] on one line."""
        fam = self.mono if mono else self.font
        spans = "".join(f'<tspan style="fill:{self.c(col)};font-weight:{wt}">{html.escape(t)}</tspan>'
                        for t, col, wt in runs)
        self.parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
                          f'style="font-family:{fam};font-size:{size}px">{spans}</text>')

    def _marker(self, color: str) -> str:
        mid = f"{self.fig}-ah-{color}"
        self.markers.add(color)
        return mid

    def arrow(self, pts, color="muted", both=False, dash=None, sw=1.8):
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        mid = self._marker(color)
        st = f"fill:none;stroke:{self.c(color)};stroke-width:{sw};stroke-linejoin:round"
        if dash:
            st += f";stroke-dasharray:{dash}"
        ends = f' marker-end="url(#{mid})"' + (f' marker-start="url(#{mid})"' if both else "")
        self.parts.append(f'<path d="{d}" style="{st}"{ends}/>')

    def line(self, x1, y1, x2, y2, color="soft", sw=1.2, dash=None):
        st = f"stroke:{self.c(color)};stroke-width:{sw}" + (f";stroke-dasharray:{dash}" if dash else "")
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" style="{st}"/>')

    def svg(self, label: str) -> str:
        defs = "".join(
            f'<marker id="{self.fig}-ah-{col}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
            f'style="fill:{self.c(col)}"/></marker>' for col in sorted(self.markers))
        bg = "" if self.page else f'<rect width="{self.w}" height="{self.h}" style="fill:{LIGHT["bg"]}"/>'
        attrs = f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{html.escape(label)}"'
        if not self.page:
            attrs = f'xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" ' + attrs
        return f'<svg {attrs}><defs>{defs}</defs>{bg}{"".join(self.parts)}</svg>'


# ============================================================================ figure 1
def fig_architecture(page: bool) -> tuple[str, str]:
    p = Pen("f1", 1600, 850, page)
    label = ("CallIntel end-to-end flow: call transcripts are cleaned and masked inside a raw-data zone, triaged, "
             "turned into grounded facts by AI, analysed by specialist agents with the bank knowledge base, "
             "verified against the transcript and delivered as one JSON per call to dashboards.")
    p.text(40, 50, "How CallIntel turns call transcripts into verified insights", 26, 700)
    p.text(40, 78, "Agentic AI with retrieval (RAG)  ·  personal data masked before any AI step  ·  "
                   "every insight traceable to the transcript", 15, color="muted")

    # secure environment
    p.rect(30, 100, 1540, 616, fill="panel", stroke="line", rx=18, dash="7 6")
    p.text(52, 128, "BANK'S SECURE ENVIRONMENT (ON-PREMISE)", 12.5, 700, "muted", spacing=1.2)

    X = [50 + i * 219 for i in range(7)]
    W, Y0, H = 186, 214, 250
    cx = [x + W / 2 for x in X]

    # raw-data zone (stages 1-2)
    p.rect(40, 180, 426, 298, fill="privacy_bg", stroke="privacy", rx=14, dash="5 4")
    p.text(54, 202, "RAW PERSONAL DATA STAYS IN THIS ZONE", 11.5, 700, "privacy", spacing=1)

    # knowledge base
    p.rect(X[3], 112, X[4] + W - X[3], 58, fill="surface", stroke="line")
    p.text((X[3] + X[4] + W) / 2, 136, "Bank knowledge base (RAG)", 15.5, 700, anchor="middle")
    p.text((X[3] + X[4] + W) / 2, 158, "product catalogue  ·  compliance rules  ·  sales playbook", 13,
           color="muted", anchor="middle")
    p.arrow([(cx[3], 171), (cx[3], Y0 - 3)])
    p.text(cx[3] + 8, 194, "catalogue", 12, color="muted")
    p.arrow([(cx[4], 171), (cx[4], Y0 - 3)])
    p.text(cx[4] + 8, 194, "rules & playbook", 12, color="muted")

    stages = [
        ("Call transcripts", "privacy", ["1,000+ files a day", "6,000+ lines each", "Agent & customer turns"], "raw text"),
        ("Clean & protect", "privacy", ["Remove noise & repeats", "Mask personal data:"], "masked text"),
        ("Focus", "muted", ["Score every section", "for business signals", "Skip hold time and", "small talk"], "key sections"),
        ("Extract facts", "ai", ["AI reads sections", "in parallel", "Each fact keeps its", "line & exact quote"], "grounded facts"),
        ("Analyse", "ai", [], "insights + evidence"),
        ("Verify", "ok", ["Every claim checked", "against the transcript", "Unsupported: removed", "Unsure: human review"], "verified insights"),
        ("Deliver", "muted", ["Same JSON structure", "for every call", "Dashboards & BI", "(Power BI, Parquet)"], "insight tables"),
    ]
    for i, (title, cat, lines, out) in enumerate(stages):
        x = X[i]
        fill = {"ai": "ai_bg", "ok": "ok_bg"}.get(cat, "surface")
        stroke = {"ai": "ai", "ok": "ok", "privacy": "privacy"}.get(cat, "line")
        p.rect(x, Y0, W, H, fill=fill, stroke=stroke)
        p.circle(x + 22, Y0 + 26, 13, cat if cat != "muted" else "ink")
        p.text(x + 22, Y0 + 31, str(i + 1), 13.5, 700, "on_color" if cat != "muted" else "surface", "middle")
        p.text(x + 44, Y0 + 32, title, 17, 700)
        for j, ln in enumerate(lines):
            p.text(x + 14, Y0 + 70 + j * 23, ln, 14.5)
        p.line(x + 14, Y0 + H - 40, x + W - 14, Y0 + H - 40)
        p.text(x + 14, Y0 + H - 16, "→ " + out, 13, 600, "muted")
    # stage 2 examples
    p.text(X[1] + 14, Y0 + 125, "Rahul → <CUST_NAME_1>", 12, 600, "privacy", mono=True)
    p.text(X[1] + 14, Y0 + 147, "4111… → <CARD_1>", 12, 600, "privacy", mono=True)
    p.text(X[1] + 14, Y0 + 169, "Aadhaar, PAN, phone…", 12, 600, "privacy", mono=True)
    # stage 4 extra
    p.text(X[3] + 14, Y0 + 162, "Product names unified", 14.5, 600, "ai")
    # stage 5 chips
    chips = [("Sales", 0, 0), ("Customer", 1, 0), ("Product", 0, 1), ("Compliance", 1, 1)]
    for name, col, row in chips:
        cxx, cyy = X[4] + 14 + col * 82, Y0 + 54 + row * 36
        p.rect(cxx, cyy, 76, 28, fill="surface", stroke="ai", rx=14, sw=1.2)
        p.text(cxx + 38, cyy + 19, name, 12.5, 600, anchor="middle")
    p.text(X[4] + 14, Y0 + 146, "Specialist agents work", 14.5)
    p.text(X[4] + 14, Y0 + 169, "in parallel, + summary", 14.5)
    # pipeline arrows
    for i in range(6):
        p.arrow([(X[i] + W + 3, Y0 + 125), (X[i + 1] - 3, Y0 + 125)], color="ink")

    # Redis backbone
    # text kept left of the AI-call arrows that cross the band
    p.rect(50, 497, 1500, 54, fill="band", stroke=None, rx=12)
    p.rich(72, 519, [("Redis backbone  ", "ink", 700),
                     ("work queue for parallel workers  ·  progress log (no call twice)", "muted", 400)], 14)
    p.text(72, 540, "AI answer cache  ·  learned product names  ·  rate limits", 14, color="muted")

    # AI gateway
    gx, gw = X[3], X[5] + W - X[3]
    p.rect(gx, 592, gw, 80, fill="ai_bg", stroke="ai", rx=12)
    p.text(gx + gw / 2, 620, "AI model gateway", 16, 700, anchor="middle")
    p.text(gx + gw / 2, 643, "local model on bank GPUs, or an approved cloud model", 13.5, color="muted", anchor="middle")
    p.text(gx + gw / 2, 663, "privacy guard blocks any request that still has personal data", 13.5,
           color="muted", anchor="middle")
    for i in (3, 4, 5):
        p.arrow([(cx[i], Y0 + H + 3), (cx[i], 589)], color="ai", both=True)
    p.text(cx[3] + 9, 484, "AI calls", 12, 600, "ai")

    # quality gate
    p.rect(X[6], 592, W, 80, fill="ok_bg", stroke="ok", rx=12)
    p.text(cx[6], 620, "Quality gate", 16, 700, anchor="middle")
    p.text(cx[6], 643, "scored on labelled calls", 13, color="muted", anchor="middle")
    p.text(cx[6], 663, "before every release", 13, color="muted", anchor="middle")
    p.arrow([(cx[6], Y0 + H + 3), (cx[6], 589)], color="ok")
    p.text(cx[6] + 9, 484, "measured", 12, 600, "ok")

    # legend
    lx, ly = 54, 606
    items = [("privacy_bg", "privacy", "5 4", "raw personal data zone"), ("ai_bg", "ai", None, "AI step"),
             ("ok_bg", "ok", None, "verification"), ("surface", "line", None, "rule-based step (no AI)")]
    for k, (fill, stroke, dash, name) in enumerate(items):
        ix, iy = lx + (k % 2) * 300, ly + (k // 2) * 34
        p.rect(ix, iy, 30, 20, fill=fill, stroke=stroke, rx=5, dash=dash, sw=1.3)
        p.text(ix + 42, iy + 15, name, 13.5, color="muted")

    # optional cloud model outside the environment
    p.rect(cx[4] - 185, 752, 370, 74, fill="surface", stroke="line", rx=12, dash="6 5")
    p.text(cx[4], 781, "Cloud AI model (optional)", 15.5, 700, anchor="middle")
    p.text(cx[4], 805, "e.g. OpenAI  ·  receives masked text only", 13, color="muted", anchor="middle")
    p.arrow([(cx[4], 675), (cx[4], 749)], color="privacy", both=True, dash="6 4")
    p.text(cx[4] + 10, 741, "masked text only", 12.5, 700, "privacy")
    return p.svg(label), label


# ============================================================================ figure 2
def fig_journey(page: bool) -> tuple[str, str]:
    p = Pen("f2", 1600, 560, page)
    label = ("One 6,000-line call: 65.8 thousand tokens shrink to 12.1 thousand after cleaning; 6,001 lines become "
             "1,882; 3 of 6 sections go to the AI; 17 AI calls; one verified JSON in 133 seconds on a laptop.")
    p.text(40, 50, "One 6,000-line call, start to finish", 26, 700)
    p.text(40, 78, "Measured on a synthetic 6,000-line test call, on a laptop GPU (RTX 3060, 4B local model). "
                   "Bank servers process faster.", 15, color="muted")

    # tokens, drawn to scale
    full, kept = 65.8, 12.1
    bw = 1500
    p.text(50, 128, "AMOUNT OF TEXT (TOKENS), TO SCALE", 12.5, 700, "muted", spacing=1.2)
    p.rect(50, 142, bw, 34, fill="soft", stroke=None, rx=6)
    p.text(64, 165, "Raw transcript: 65.8k tokens", 14.5, 600)
    kw = bw * kept / full
    p.rect(50, 188, kw, 34, fill="privacy", stroke=None, rx=6)
    p.text(50 + kw + 14, 211, "After cleaning and repetition removal: 12.1k tokens (82% less to process)", 14.5, 600,
           "privacy")

    cards = [("6,001", ["lines in the", "transcript"]), ("1,882", ["lines kept after", "removing noise & repeats"]),
             ("3 of 6", ["sections worth sending", "to the AI"]), ("17", ["AI calls to extract,", "analyse and verify"]),
             ("133 s", ["to one verified JSON", "file, on a laptop"])]
    steps = ["clean", "focus", "AI", "verify"]
    cw, gap, y = 258, 52, 280
    for i, (big, lines) in enumerate(cards):
        x = 50 + i * (cw + gap)
        last = i == len(cards) - 1
        p.rect(x, y, cw, 150, fill="ok_bg" if last else "surface", stroke="ok" if last else "line", rx=12)
        p.text(x + 20, y + 58, big, 38, 700, "ok" if last else "ink")
        for j, ln in enumerate(lines):
            p.text(x + 20, y + 94 + j * 22, ln, 14.5, color="muted")
        if i < len(steps):
            ax = x + cw
            p.arrow([(ax + 4, y + 75), (ax + gap - 4, y + 75)], color="ink")
            p.text(ax + gap / 2, y + 62, steps[i], 12, 600, "muted", "middle")
    p.text(50, 490, "One laptop handles roughly 27 long calls an hour. For 1,000+ calls a day, the same pipeline "
                    "runs on a GPU server or an approved cloud model, with many workers in parallel.", 15,
           color="muted")
    return p.svg(label), label


# ============================================================================ figure 3
def fig_privacy(page: bool) -> tuple[str, str]:
    p = Pen("f3", 1600, 640, page)
    label = ("Privacy masking example: names, date of birth, card and mobile numbers in the raw transcript are "
             "replaced locally with placeholders such as CUST_NAME_1 and CARD_1 before any AI step.")
    p.text(40, 50, "What the AI model sees", 26, 700)
    p.text(40, 78, "Personal data is found and replaced on the bank's own servers, before any AI step. The same person "
                   "or card always gets the same placeholder, so the analysis still makes sense.", 15, color="muted")

    raw = [("12", "C", [("Hi, this is ", "ink", 400), ("Rahul Sharma", "danger", 700), (".", "ink", 400)]),
           ("13", "A", [("Can you confirm your date of birth?", "ink", 400)]),
           ("14", "C", [("It is ", "ink", 400), ("fifth March nineteen ninety", "danger", 700), (".", "ink", 400)]),
           ("15", "C", [("My card is ", "ink", 400), ("4111 1111 1111 1111", "danger", 700), (".", "ink", 400)]),
           ("16", "C", [("Mobile: ", "ink", 400), ("nine eight seven six five…", "danger", 700)]),
           ("17", "A", [("Thank you Mr. ", "ink", 400), ("Sharma", "danger", 700), (".", "ink", 400)])]
    masked = [("12", "C", [("Hi, this is ", "ink", 400), ("<CUST_NAME_1>", "privacy", 700), (".", "ink", 400)]),
              ("13", "A", [("Can you confirm your date of birth?", "ink", 400)]),
              ("14", "C", [("<DOB_1>", "privacy", 700), (".", "ink", 400)]),
              ("15", "C", [("My card is ", "ink", 400), ("<CARD_1>", "privacy", 700), (".", "ink", 400)]),
              ("16", "C", [("Mobile: ", "ink", 400), ("<PHONE_1>", "privacy", 700)]),
              ("17", "A", [("Thank you Mr. ", "ink", 400), ("<CUST_NAME_1>", "privacy", 700), (".", "ink", 400)])]
    panels = [(40, "RAW TRANSCRIPT (NEVER LEAVES THE BANK)", "danger", "danger_bg", raw),
              (900, "WHAT REACHES THE AI MODEL", "privacy", "privacy_bg", masked)]
    for x, title, col, bg, rows in panels:
        p.rect(x, 112, 660, 330, fill="surface", stroke=col, rx=14, sw=1.6)
        p.rect(x, 112, 660, 46, fill=bg, stroke=None, rx=14)
        p.rect(x, 140, 660, 18, fill=bg, stroke=None, rx=0)
        p.text(x + 22, 142, title, 13, 700, col, spacing=1.1)
        for i, (no, spk, runs) in enumerate(rows):
            yy = 196 + i * 40
            p.text(x + 22, yy, no, 14, 600, "muted", mono=True)
            p.text(x + 58, yy, spk + ":", 14, 700, "muted", mono=True)
            p.rich(x + 88, yy, runs, 15.5, mono=True)

    # middle: what happens
    p.arrow([(708, 277), (892, 277)], color="privacy", sw=2.4)
    p.text(800, 252, "masked locally", 14, 700, "privacy", "middle")
    p.text(800, 306, "no AI involved", 13, color="muted", anchor="middle")

    notes = [("Checksums", "card (Luhn) and Aadhaar (Verhoeff) checks recognise real card and ID numbers"),
             ("Spoken numbers", "'nine eight double seven…' is turned into digits first, so it cannot slip through"),
             ("Context aware", "the answer after 'your date of birth?' is masked; names are remembered for the whole call"),
             ("Last line of defence", "a privacy guard re-checks every AI request and blocks it if anything remains")]
    for i, (head, body) in enumerate(notes):
        x, y = 40 + (i % 2) * 770, 478 + (i // 2) * 64
        p.circle(x + 10, y + 12, 6, "privacy")
        p.text(x + 26, y + 18, head, 15, 700)
        p.text(x + 26, y + 40, body, 14, color="muted")
    return p.svg(label), label


# ============================================================================ figure 4
def fig_verification(page: bool) -> tuple[str, str]:
    p = Pen("f4", 1600, 560, page)
    label = ("Verification: each AI claim must pass five checks: allowed values, real transcript line references, "
             "matching quotes, matching figures and an independent fact-checker. Failures get one retry, then are "
             "removed or flagged for human review; passing claims are reported with evidence.")
    p.text(40, 50, "How every insight is checked before it is reported", 26, 700)
    p.text(40, 78, "Each claim must pass every gate. A failed claim gets one retry; if it still fails, it is removed or "
                   "marked 'unclear' and the call is flagged for a person to review.", 15, color="muted")

    # claim
    p.rect(40, 150, 210, 150, fill="ai_bg", stroke="ai", rx=12)
    p.text(58, 180, "AI agent claim", 15.5, 700)
    p.text(58, 210, "\"Customer declined", 14, color="muted")
    p.text(58, 232, "the loan offer\"", 14, color="muted")
    p.text(58, 262, "cites line L211", 13, 600, "ai", mono=True)

    gates = [("Allowed values", ["answers use the bank's", "fixed lists only"]),
             ("Cites the transcript", ["refers to real", "line numbers"]),
             ("Quote matches", ["quoted words appear", "on that line"]),
             ("Figures match", ["rates and fees appear", "exactly as stated"]),
             ("Fact-checker AI", ["a second AI checks", "risky claims"])]
    gw, gap, gx0 = 196, 26, 282
    for i, (title, lines) in enumerate(gates):
        x = gx0 + i * (gw + gap)
        p.rect(x, 150, gw, 150, fill="surface", stroke="ok", rx=12)
        p.circle(x + 22, 176, 12, "ok")
        p.text(x + 22, 181, str(i + 1), 13, 700, "on_color", "middle")
        p.text(x + 42, 182, title, 15, 700)
        for j, ln in enumerate(lines):
            p.text(x + 18, 222 + j * 22, ln, 14, color="muted")
        p.text(x + 18, 284, "pass ✓", 13, 700, "ok")
        prev_right = 250 if i == 0 else gx0 + (i - 1) * (gw + gap) + gw
        p.arrow([(prev_right + 3, 225), (x - 3, 225)], color="ok")
        p.line(x + gw / 2, 302, x + gw / 2, 342, color="danger", sw=1.6, dash="4 3")
    last_right = gx0 + 4 * (gw + gap) + gw
    # reported
    p.rect(last_right + 30, 150, 1560 - last_right - 30, 150, fill="ok_bg", stroke="ok", rx=12)
    ox = last_right + 30 + 18
    p.text(ox, 182, "Reported", 15.5, 700, "ok")
    for j, ln in enumerate(["with line numbers,", "quotes and a", "calculated confidence"]):
        p.text(ox, 214 + j * 22, ln, 14, color="muted")
    p.arrow([(last_right + 3, 225), (last_right + 27, 225)], color="ok")

    # failure path
    first_c, last_c = gx0 + gw / 2, gx0 + 4 * (gw + gap) + gw / 2
    p.line(first_c, 342, last_c, 342, color="danger", sw=1.6, dash="4 3")
    p.text(first_c - 4, 336, "fails", 12, 700, "danger", "end")
    p.rect(282, 372, 520, 60, fill="danger_bg", stroke="danger", rx=12)
    p.text(302, 399, "Retry once", 15, 700, "danger")
    p.text(302, 420, "the agent is asked again, with the reason it failed", 13.5, color="muted")
    p.arrow([(542, 345), (542, 369)], color="danger")
    p.arrow([(282, 402), (145, 402), (145, 303)], color="danger", dash="5 4")
    p.text(157, 372, "new answer,", 12, 600, "danger")
    p.text(157, 388, "re-checked", 12, 600, "danger")
    p.rect(850, 372, 710, 60, fill="danger_bg", stroke="danger", rx=12)
    p.text(870, 399, "Still failing: removed, or set to 'unclear'", 15, 700, "danger")
    p.text(870, 420, "the call is marked 'needs review' and sent to the review queue", 13.5, color="muted")
    p.arrow([(805, 402), (847, 402)], color="danger")

    p.text(40, 486, "Confidence is calculated from the evidence (how well quotes match, how many lines support the "
                    "claim, the fact-checker's verdict), never taken from the AI's own opinion of itself.", 15,
           color="muted")
    p.text(40, 514, "Measured on 8 labelled test calls: 100% of reported evidence matched the transcript; no invented "
                    "products.", 15, color="muted")
    return p.svg(label), label


FIGURES = [
    ("callintel-architecture", fig_architecture, "End-to-end flow",
     "Seven steps, left to right. Raw personal data exists only in steps 1 and 2; everything after that works on "
     "masked text. The AI steps reach the model through one gateway, which can use a model on the bank's own "
     "servers or an approved cloud model. Redis connects the steps so many calls run in parallel and none is "
     "processed twice."),
    ("callintel-call-journey", fig_journey, "One call, start to finish",
     "Most of a long call is hold time, identity checks and small talk. Cleaning and triage remove it before any AI "
     "step, so the AI reads a fraction of the text. Numbers are from a synthetic test call on a laptop."),
    ("callintel-privacy", fig_privacy, "What the AI model sees",
     "Personal details are replaced with placeholders on the bank's own servers. The AI model only ever receives "
     "the right-hand version, and a final guard blocks any request that still contains personal data."),
    ("callintel-verification", fig_verification, "How every insight is checked",
     "Every reported insight points to the exact transcript lines it came from. Claims that cannot be backed by the "
     "transcript are never reported as facts."),
]

PAGE_TOKENS = """
:root {
  --page: #F6F8F9; --bg: #FFFFFF; --ink: #13202A; --muted: #55656F; --line: #B8C4CC; --soft: #DCE3E8;
  --surface: #FFFFFF; --panel: #F3F6F8; --privacy: #0E7C72; --privacy-bg: #E3F3F0; --ai: #A85A06; --ai-bg: #FDF2E4;
  --ok: #1F7A3A; --ok-bg: #E7F4EA; --danger: #B42318; --danger-bg: #FDECEA; --band: #E2E8ED; --on-color: #FFFFFF;
  --font-sans: "IBM Plex Sans", "Segoe UI", Arial, sans-serif; --font-mono: "IBM Plex Mono", Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { DARK color-scheme: dark } }
:root[data-theme="dark"] { DARK color-scheme: dark }
"""
DARK = ("--page: #0B1216; --bg: #111A20; --ink: #E4EBF0; --muted: #9AABB6; --line: #3B4A55; --soft: #2A3740; "
        "--surface: #16232C; --panel: #121D24; --privacy: #4CC2B2; --privacy-bg: #10302B; --ai: #F2A65A; "
        "--ai-bg: #33240F; --ok: #63C987; --ok-bg: #13301F; --danger: #F1877B; --danger-bg: #3A1916; "
        "--band: #1D2A33; --on-color: #0B1216;")

PAGE_CSS = """
body { background: var(--page); color: var(--ink); font-family: var(--font-sans); font-size: 16px; line-height: 1.55 }
.wrap { max-width: 1180px; margin: 0 auto; padding-block: 44px 72px;
        padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)) }
.eyebrow { font-size: 12.5px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--privacy) }
h1 { font-size: clamp(30px, 4vw, 42px); line-height: 1.1; margin: 6px 0 14px; text-wrap: balance; letter-spacing: -.01em }
.lede { max-width: 68ch; color: var(--muted); font-size: 17px; margin: 0 }
nav { display: flex; flex-wrap: wrap; gap: 8px; margin: 26px 0 8px }
nav a { color: var(--ink); text-decoration: none; font-size: 14px; font-weight: 600; padding: 7px 14px;
        border: 1px solid var(--line); border-radius: 999px; background: var(--bg) }
nav a:hover, nav a:focus-visible { border-color: var(--privacy); color: var(--privacy); outline: none }
section { margin-top: 56px; display: grid; gap: 12px; scroll-margin-top: 16px }
section h2 { font-size: 24px; margin: 0; text-wrap: balance }
section p { margin: 0; max-width: 72ch; color: var(--muted) }
figure { margin: 8px 0 0 }
.scroll { overflow-x: auto; background: var(--bg); border: 1px solid var(--soft); border-radius: 14px; padding: 10px }
.scroll svg { display: block; width: 100%; min-width: 920px; height: auto }
figcaption { font-size: 13px; color: var(--muted); margin-top: 8px; max-width: 90ch }
.files { margin-top: 56px; padding-top: 18px; border-top: 1px solid var(--soft); color: var(--muted); font-size: 14px }
.files code { font-family: var(--font-mono); font-size: 13px; color: var(--ink) }
"""


def build_page(figs: list[tuple[str, str, str, str, str]]) -> str:
    sections = "\n".join(
        f'<section id="{name}"><h2>{html.escape(title)}</h2><p>{html.escape(intro)}</p>'
        f'<figure><div class="scroll">{svg}</div><figcaption>{html.escape(label)}</figcaption></figure></section>'
        for name, title, intro, svg, label in figs)
    nav = "".join(f'<a href="#{name}">{html.escape(title)}</a>' for name, title, *_ in figs)
    return f"""<title>CallIntel Architecture</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=IBM+Plex+Sans:wght@400;600;700&display=swap">
<style>{PAGE_TOKENS.replace("DARK", DARK)}{PAGE_CSS}</style>
<div class="wrap">
<header>
<div class="eyebrow">Banking call analytics · solution overview</div>
<h1>CallIntel: from call transcript to verified insight</h1>
<p class="lede">CallIntel reads recorded sales and service calls and turns each one into the same structured record:
why the customer called, which products came up and in what way, how the sales conversation ended, and whether
compliance rules were followed. Every finding links back to the exact lines of the conversation, and customer
personal data is masked before any AI model sees the text.</p>
<nav aria-label="Diagrams">{nav}</nav>
</header>
{sections}
<p class="files">Slide-ready copies of these diagrams are in the project folder <code>docs/diagrams/</code>:
high-resolution <code>.png</code> images and editable <code>.svg</code> files (PowerPoint: Insert, Pictures; right-click,
Convert to Shape to edit). Timings and counts are from synthetic test calls; production figures depend on the bank's
servers and real call content.</p>
</div>"""


def render_png(svg_path: Path, w: int, h: int) -> Path | None:
    edge = shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not Path(edge).exists():
        print("  (Edge not found: PNG skipped; open the .svg in a browser and screenshot it instead)")
        return None
    wrapper = svg_path.with_suffix(".render.html")
    wrapper.write_text(f'<html><body style="margin:0;background:#fff">{svg_path.read_text(encoding="utf-8")}</body></html>',
                       encoding="utf-8")
    png = svg_path.with_suffix(".png")
    subprocess.run([edge, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                    f"--user-data-dir={OUT / '.edge-profile'}", f"--window-size={w},{h}",
                    "--force-device-scale-factor=2", f"--screenshot={png}", wrapper.as_uri()],
                   capture_output=True, timeout=120)
    wrapper.unlink(missing_ok=True)
    return png if png.exists() else None


def main() -> None:
    page_figs = []
    for name, fn, title, intro in FIGURES:
        svg, label = fn(page=False)
        path = OUT / f"{name}.svg"
        path.write_text(svg, encoding="utf-8")
        w = int(svg.split('width="')[1].split('"')[0])
        h = int(svg.split('height="')[1].split('"')[0])
        png = render_png(path, w, h) if "--no-png" not in sys.argv else None
        print(f"{path.name}" + (f"  +  {png.name} ({w * 2}x{h * 2})" if png else ""))
        page_svg, _ = fn(page=True)
        page_figs.append((name, title, intro, page_svg, label))
    shutil.rmtree(OUT / ".edge-profile", ignore_errors=True)
    (OUT / "_figures.html").unlink(missing_ok=True)
    page = OUT / "callintel-architecture.html"
    page.write_text(build_page(page_figs), encoding="utf-8")
    print(page.name)


if __name__ == "__main__":
    main()
