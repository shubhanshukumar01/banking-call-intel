"""Per-call transcript index (masked text only): evidence retrieval for agents and the critic.

Used for (a) compliance rule checks - retrieve candidate lines for each rule's phrases,
(b) agent tool calls (search_transcript / get_lines), (c) the critic, which judges a
claim against the *actual* cited lines, and (d) deterministic grounding.
"""
from __future__ import annotations

import bisect

from ..preprocess.transcript import Line, PreparedTranscript, ROLE_CODE, Turn
from .retrieval import BM25, tokenize


class TranscriptIndex:
    def __init__(self, prepared: PreparedTranscript) -> None:
        self.p = prepared
        self.line_nos = sorted(prepared.lines)
        self.turns: list[Turn] = prepared.turns
        self.bm25 = BM25([tokenize(t.text) for t in self.turns])
        self._turn_starts = [t.line_start for t in self.turns]

    def line(self, no: int) -> Line | None:
        return self.p.lines.get(no)

    def nearest_line(self, no: int, max_dist: int = 3) -> Line | None:
        """Line `no` or the closest kept line (cited line may have been merged/dropped)."""
        if no in self.p.lines:
            return self.p.lines[no]
        i = bisect.bisect_left(self.line_nos, no)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.line_nos) and abs(self.line_nos[j] - no) <= max_dist:
                if best is None or abs(self.line_nos[j] - no) < abs(best - no):
                    best = self.line_nos[j]
        return self.p.lines.get(best) if best is not None else None

    def window(self, no: int, radius: int = 2) -> list[Line]:
        i = bisect.bisect_left(self.line_nos, no)
        lo, hi = max(0, i - radius), min(len(self.line_nos), i + radius + 1)
        return [self.p.lines[n] for n in self.line_nos[lo:hi]]

    def range(self, start: int, end: int, limit: int = 15) -> list[Line]:
        i = bisect.bisect_left(self.line_nos, start)
        out = []
        while i < len(self.line_nos) and self.line_nos[i] <= end and len(out) < limit:
            out.append(self.p.lines[self.line_nos[i]])
            i += 1
        return out

    def search_lines(self, query: str, k: int = 3) -> list[Line]:
        """Best matching turns -> their most relevant line."""
        qtok = set(tokenize(query))
        out: list[Line] = []
        for ti, _score in self.bm25.search(query, k):
            turn = self.turns[ti]
            best = max(turn.segments, key=lambda s: len(qtok & set(tokenize(s.text))))
            ln = self.p.lines.get(best.line)
            if ln:
                out.append(ln)
        return out

    @staticmethod
    def fmt(line: Line, max_chars: int = 240) -> str:
        text = line.text if len(line.text) <= max_chars else line.text[:max_chars] + "..."
        return f'L{line.no} {ROLE_CODE.get(line.speaker, "?")}: "{text}"'
