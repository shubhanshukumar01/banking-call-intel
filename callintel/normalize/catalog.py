"""Canonical product registry loaded from config/products.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_LEADING = re.compile(r"^(?:(?:the|a|an|my|your|our|this|that|these|those|its|their|his|her|new|existing|current|"
                      r"same|other|another one of the|one of your)\s+)+")
_NON_ALNUM = re.compile(r"[^a-z0-9%+ ]+")
_SPACES = re.compile(r"\s+")


def norm_mention(text: str) -> str:
    t = text.lower().replace("&", " and ").replace("-", " ")
    t = _NON_ALNUM.sub(" ", t)
    t = _SPACES.sub(" ", t).strip()
    t = _LEADING.sub("", t)
    return t.strip()


@dataclass
class Product:
    id: str
    name: str
    category: str
    aliases: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    description: str = ""
    generic: bool = False

    @property
    def all_names(self) -> list[str]:
        return [self.name] + self.aliases


class ProductCatalog:
    def __init__(self, path: str | Path) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        self.products: dict[str, Product] = {}
        for p in data.get("products", []):
            prod = Product(id=p["id"], name=p["name"], category=p.get("category", "other"),
                           aliases=list(p.get("aliases", [])), attributes=dict(p.get("attributes") or {}),
                           description=p.get("description", ""), generic=bool(p.get("generic", False)))
            self.products[prod.id] = prod
        self.alias_map: dict[str, str] = {}
        for prod in self.products.values():
            for a in prod.all_names:
                n = norm_mention(a)
                # specific products win over generic fall-backs on alias collisions
                if n and (n not in self.alias_map or self.products[self.alias_map[n]].generic):
                    self.alias_map[n] = prod.id
        # longest-first alternation so "platinum rewards card" beats "card"
        specific = sorted({a for a, pid in self.alias_map.items() if len(a) >= 3}, key=len, reverse=True)
        self._matcher = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in specific) + r")\b", re.IGNORECASE) \
            if specific else None
        self._name_matcher = re.compile(
            r"\b(?:" + "|".join(sorted((re.escape(n) for p in self.products.values() if not p.generic
                                        for n in p.all_names if len(n) >= 4), key=len, reverse=True)) + r")\b",
            re.IGNORECASE)

    @property
    def ids(self) -> list[str]:
        return list(self.products)

    def get(self, pid: str) -> Product | None:
        return self.products.get(pid)

    def find_mentions(self, text: str) -> list[tuple[str, str, int, int]]:
        """(surface, product_id, start, end) for catalogue aliases found in text."""
        if not self._matcher:
            return []
        out = []
        for m in self._matcher.finditer(text):
            pid = self.alias_map.get(norm_mention(m.group(0)))
            if pid:
                out.append((m.group(0), pid, m.start(), m.end()))
        return out

    def protected_spans(self, text: str) -> list[tuple[int, int]]:
        """Spans of specific product names - protected from NER over-masking."""
        return [(m.start(), m.end()) for m in self._name_matcher.finditer(text)]
