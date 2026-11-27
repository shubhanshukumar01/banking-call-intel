"""Versioned prompt registry (config/prompts.yaml) with {{var}} rendering."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..utils import sha256_text

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(template: str, **vars: Any) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in vars:
            raise KeyError(f"prompt variable '{key}' not provided")
        return str(vars[key])
    return _VAR.sub(repl, template)


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str
    user: str

    @property
    def tag(self) -> str:
        """id@version+contenthash - recorded in outputs for reproducibility."""
        return f"{self.id}@{self.version}+{sha256_text(self.system + self.user)[:8]}"

    def with_static(self, **vars: Any) -> "Prompt":
        """Pre-render the static system part once (keeps it byte-identical -> prefix cache hits)."""
        return Prompt(self.id, self.version, render(self.system, **vars), self.user)

    def messages(self, **user_vars: Any) -> list[dict[str, str]]:
        return [{"role": "system", "content": self.system.strip()},
                {"role": "user", "content": render(self.user, **user_vars).strip()}]


class PromptRegistry:
    def __init__(self, path: str | Path) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        self._prompts = {pid: Prompt(pid, str(p.get("version", "0")), p["system"], p["user"])
                         for pid, p in data.items()}

    def get(self, pid: str) -> Prompt:
        return self._prompts[pid]

    def versions(self) -> dict[str, str]:
        return {pid: p.tag for pid, p in self._prompts.items()}
