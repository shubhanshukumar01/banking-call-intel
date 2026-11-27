from callintel.normalize.catalog import ProductCatalog, norm_mention
from callintel.normalize.normalizer import ProductNormalizer
from callintel.redis_layer.client import connect_redis
from callintel.verify.grounding import confidence, number_supported, quote_score


def _norm(settings, r):
    s = settings
    s.rag.normalizer.t3_llm_enabled = False
    cat = ProductCatalog(s.path(s.paths.products_file))
    return ProductNormalizer(s, cat, r, None, None, None), cat


async def test_tiers(settings):
    r = await connect_redis(settings)
    n, _ = _norm(settings, r)
    res = await n.normalize("The Platinum Card")
    assert (res.product_id, res.tier) == ("CC_PLATINUM_REWARDS", "exact")
    res = await n.normalize("cashbak credit card")             # ASR typo -> fuzzy
    assert res.product_id == "CC_CASHBACK" and res.tier in ("exact", "fuzzy")
    res = await n.normalize("rewards platinum card")            # re-ordered words
    assert res.product_id == "CC_PLATINUM_REWARDS"
    res2 = await n.normalize("it")                              # pronoun -> unresolved, not guessed
    assert res2.product_id is None
    for not_a_product in ("debit transaction", "late fee", "reward points balance"):
        assert (await n.normalize(not_a_product)).product_id is None, not_a_product


async def test_learned_alias_shared_via_redis(settings):
    r = await connect_redis(settings)
    n1, _ = _norm(settings, r)
    first = await n1.normalize("personal lone")
    assert first.product_id == "PL_PERSONAL_LOAN"
    n2, _ = _norm(settings, r)                                   # another worker process
    again = await n2.normalize("personal lone")
    assert again.product_id == "PL_PERSONAL_LOAN" and again.tier in ("learned", "exact")


def test_catalog_spans_prefer_longest(settings):
    cat = ProductCatalog(settings.path(settings.paths.products_file))
    found = cat.find_mentions("I want the platinum rewards card not a credit card")
    assert found[0][1] == "CC_PLATINUM_REWARDS"
    assert norm_mention("My new Cash-Back Card!") == "cash back card"


def test_grounding_helpers():
    assert quote_score("annual fee is too high", "Honestly the annual fee is too high for me") == 1.0
    assert quote_score("I want a loan", "the weather is nice today") < 0.6
    assert number_supported("3.6%", ["the rate is 3.6% per month"])
    assert not number_supported("2.5%", ["the rate is 3.6% per month"])
    assert confidence(1.0, 2, "supported") > confidence(1.0, 2, None) > confidence(1.0, 2, "unsupported")
