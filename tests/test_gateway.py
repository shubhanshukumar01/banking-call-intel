import json

from callintel.llm.gateway import salvage_truncated_json
from callintel.llm.schema_utils import strict_schema
from callintel.normalize.catalog import ProductCatalog
from callintel.rag.knowledge_base import KnowledgeBase
from callintel.schemas import LLMSchemas, Taxonomy


def _schemas(settings, max_facts=15):
    tax = Taxonomy.load(settings.path(settings.paths.taxonomy_file))
    cat = ProductCatalog(settings.path(settings.paths.products_file))
    return LLMSchemas(tax, cat.ids, KnowledgeBase(settings, cat).rule_ids, max_facts)


def test_salvage_truncated_json():
    cut = '{"facts": [{"type": "a", "q": "x]}"}, {"type": "b", "q": "y"}, {"type": "c", "q": "unfinish'
    fixed = salvage_truncated_json(cut)
    assert json.loads(fixed) == {"facts": [{"type": "a", "q": "x]}"}, {"type": "b", "q": "y"}]}
    assert salvage_truncated_json('{"facts": []}') is None          # complete JSON: nothing to salvage
    assert salvage_truncated_json('{"facts": [{"type": "a"') is None  # no complete item


def test_strict_schema_openai_vs_grammar(settings):
    sc = _schemas(settings, max_facts=7)
    strict = strict_schema(sc.MapOutput)
    grammar = strict_schema(sc.MapOutput, keep_limits=True)
    assert "maxItems" not in json.dumps(strict)                       # OpenAI strict mode
    assert grammar["properties"]["facts"]["maxItems"] == 7             # Ollama grammar enforces it
    fact = strict["properties"]["facts"]["items"]
    assert fact["additionalProperties"] is False and set(fact["required"]) == set(fact["properties"])
    assert list(fact["properties"])[:4] == ["type", "label", "line", "quote"]  # generation order kept
    # validation never rejects an over-long list (truncate instead of failing the call)
    many = {"facts": [{"type": "need", "label": "other", "line": 1, "quote": "q", "value": "v",
                       "product": None, "number": None}] * 9}
    assert len(sc.MapOutput.model_validate(many).facts) == 9
