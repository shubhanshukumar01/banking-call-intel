"""PII egress guard: last line of defence in the gateway.

Every outbound prompt to a provider in an *external* trust zone (or all providers, if
configured) is re-scanned with the high-precision PII detectors. Any unmasked
identifier -> the request is blocked (default) or redacted, and an audit record with
entity *types only* is written. This turns a masking bug into a failed job instead of
a data leak.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from ..config import Settings
from ..preprocess.pii import RegexRecognizer, find_pii
from ..utils import dumps, now_iso, today

log = logging.getLogger(__name__)


class EgressBlocked(RuntimeError):
    """Raised when outbound LLM content contains unmasked PII or cloud use is disabled."""


class EgressGuard:
    def __init__(self, settings: Settings) -> None:
        self.cfg = settings.privacy.egress_guard
        self.allow_cloud = settings.privacy.allow_cloud_llm
        self.audit_dir = settings.path(settings.paths.audit_dir)
        self.capture = settings.audit.capture_llm_payloads
        self.rec = RegexRecognizer()
        self._lock = threading.Lock()

    def _audit(self, name: str, record: dict[str, Any]) -> None:
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        path = Path(self.audit_dir) / f"{name}_{today()}_{os.getpid()}.jsonl"
        with self._lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(dumps(record) + "\n")

    def check(self, messages: list[dict[str, str]], trust_zone: str, *, call_id: str | None,
              role: str, model: str) -> list[dict[str, str]]:
        if trust_zone == "external" and not self.allow_cloud:
            raise EgressBlocked("privacy.allow_cloud_llm=false: external LLM providers are disabled")
        applies = self.cfg.enabled and (self.cfg.apply_to == "all" or trust_zone == "external")
        out = messages
        if applies:
            findings = []
            for i, m in enumerate(messages):
                for s in find_pii(m["content"], self.rec):
                    findings.append((i, s))
            if findings:
                types = sorted({s.etype for _, s in findings})
                self._audit("egress_events", {"ts": now_iso(), "call_id": call_id, "role": role, "model": model,
                                              "action": self.cfg.action, "entity_types": types,
                                              "count": len(findings)})
                if self.cfg.action == "block":
                    raise EgressBlocked(f"unmasked PII detected in outbound prompt: {types}")
                out = [dict(m) for m in messages]
                for i in sorted({i for i, _ in findings}):
                    spans = sorted((s for j, s in findings if j == i), key=lambda s: s.start, reverse=True)
                    text = out[i]["content"]
                    for s in spans:
                        text = text[:s.start] + f"[REDACTED_{s.etype}]" + text[s.end:]
                    out[i]["content"] = text
                log.warning("egress guard redacted %d PII span(s) for call %s", len(findings), call_id)
        if self.capture:
            self._audit("llm_payloads", {"ts": now_iso(), "call_id": call_id, "role": role, "model": model,
                                         "messages": out})
        return out
