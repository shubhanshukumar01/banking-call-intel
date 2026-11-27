"""Banking knowledge base (RAG): compliance rules, playbooks/policies, product reference.

Collections
  compliance - structured rules (knowledge_base/compliance_rules.yaml)
  playbook   - markdown sections from knowledge_base/*.md (split on '## ')
  product    - catalogue entries (config/products.yaml) incl. reference fees/rates
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import Settings
from ..normalize.catalog import ProductCatalog
from .retrieval import Doc, HybridIndex

log = logging.getLogger(__name__)


@dataclass
class Rule:
    id: str
    title: str
    description: str
    applies_to_call_types: list[str]
    applies_to_categories: list[str]
    search_phrases: list[str] = field(default_factory=list)
    severity_if_missed: str = "medium"

    def applies(self, call_types: set[str], categories: set[str]) -> bool:
        ct_ok = "all" in self.applies_to_call_types or bool(call_types & set(self.applies_to_call_types))
        cat_ok = "all" in self.applies_to_categories or bool(categories & set(self.applies_to_categories))
        return ct_ok and cat_ok


class KnowledgeBase:
    def __init__(self, settings: Settings, catalog: ProductCatalog) -> None:
        self.s = settings
        self.catalog = catalog
        kb_dir = settings.path(settings.paths.knowledge_base_dir)
        self.rules: list[Rule] = []
        rules_file = kb_dir / "compliance_rules.yaml"
        if rules_file.exists():
            with open(rules_file, "r", encoding="utf-8") as fh:
                self.rules = [Rule(**r) for r in (yaml.safe_load(fh) or {}).get("rules", [])]
        docs: list[Doc] = []
        for r in self.rules:
            docs.append(Doc(f"rule:{r.id}", f"{r.title}. {r.description}", {"collection": "compliance", "rule_id": r.id}))
        for md in sorted(Path(kb_dir).glob("*.md")):
            text = md.read_text(encoding="utf-8")
            for sec in re.split(r"\n(?=## )", text):
                sec = sec.strip()
                if not sec.startswith("## "):
                    continue
                title, _, body = sec[3:].partition("\n")
                docs.append(Doc(f"{md.stem}:{title.strip()}", f"{title.strip()}: {body.strip()}",
                                {"collection": "playbook", "source": md.name, "title": title.strip()}))
        for p in catalog.products.values():
            attrs = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in p.attributes.items())
            docs.append(Doc(f"product:{p.id}", f"{p.name} ({p.category}). {p.description} {attrs}",
                            {"collection": "product", "product_id": p.id}))
        self.index = HybridIndex(docs)

    async def enable_dense(self, embed_fn) -> None:
        await self.index.enable_dense(embed_fn)

    @property
    def rule_ids(self) -> list[str]:
        return [r.id for r in self.rules] or ["NONE"]

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def rules_for(self, call_types: set[str], categories: set[str]) -> list[Rule]:
        return [r for r in self.rules if r.applies(call_types, categories)]

    async def search(self, query: str, collection: str, k: int | None = None) -> list[Doc]:
        hits = await self.index.search(query, k or self.s.rag.kb_top_k,
                                       where=lambda d: d.meta.get("collection") == collection)
        return [h.doc for h in hits]

    def product_reference(self, product_ids: list[str]) -> str:
        lines = []
        for pid in product_ids:
            p = self.catalog.products.get(pid)
            if p and p.attributes:
                attrs = "; ".join(f"{k}: {v}" for k, v in p.attributes.items())
                lines.append(f"{pid} ({p.name}): {attrs}")
        return "\n".join(lines) or "(none)"
