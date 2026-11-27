from callintel.preprocess.cleaning import normalize_spoken_digits
from callintel.preprocess.pii import (ContextRecognizer, PIIMasker, RegexRecognizer, find_pii, luhn_ok,
                                      verhoeff_ok)
from tools.synth import aadhaar, luhn_card

import random


def masker(protected=None, allow=()):
    return PIIMasker([RegexRecognizer(), ContextRecognizer()], [], protected, allow)


def test_checksums():
    rng = random.Random(1)
    for _ in range(50):
        assert luhn_ok(luhn_card(rng))
        assert verhoeff_ok(aadhaar(rng))
    assert not luhn_ok("4111111111111112")
    assert not verhoeff_ok("234567890123") or verhoeff_ok("234567890123")  # just exercises the path


def test_spoken_digits():
    assert normalize_spoken_digits("my number is nine eight double seven six five four three two one") == \
        "my number is 9877654321"
    assert normalize_spoken_digits("I have two cards and one loan") == "I have two cards and one loan"
    assert normalize_spoken_digits("call 4111 1111") == "call 4111 1111"  # pure digits untouched


def test_structured_ids_masked():
    rng = random.Random(3)
    card, aad = luhn_card(rng), aadhaar(rng)
    lines = [(f"my card is {card}", "CUSTOMER"), (f"aadhaar {aad}", "CUSTOMER"),
             ("email me at rahul.s@gmail.com or 9876543210", "CUSTOMER"), ("PAN is ABCDE1234F", "CUSTOMER"),
             ("my account number is 123456789012", "CUSTOMER"), ("the otp is 482913", "CUSTOMER")]
    res = masker().mask_lines(lines)
    joined = "\n".join(res.lines)
    for raw in (card, aad, "rahul.s@gmail.com", "9876543210", "ABCDE1234F", "123456789012", "482913"):
        assert raw not in joined, raw
    assert "<CARD_1>" in joined and "<AADHAAR_1>" in joined and "<EMAIL_1>" in joined


def test_names_consistent_and_two_pass():
    lines = [("Thank you Rahul, one moment", "AGENT"),           # name before introduction
             ("Hi, my name is Rahul Sharma", "CUSTOMER"),
             ("Thank you Mr. Sharma", "AGENT")]
    res = masker().mask_lines(lines)
    assert "Rahul" not in "\n".join(res.lines) and "Sharma" not in "\n".join(res.lines)
    surrogate = res.lines[1].split("is ")[-1]
    assert surrogate.startswith("<CUST_NAME_")
    assert surrogate in res.lines[0] and surrogate in res.lines[2]


def test_dialogue_aware_answers():
    lines = [("can you confirm your date of birth?", "AGENT"), ("It is fifth March nineteen ninety", "CUSTOMER"),
             ("and your full name please", "AGENT"), ("Meera Iyer", "CUSTOMER")]
    res = masker().mask_lines(lines)
    assert "March" not in res.lines[1]
    assert "Meera" not in res.lines[3]


def test_product_names_protected_from_name_detection():
    protected = lambda t: [(t.lower().find("platinum rewards"), t.lower().find("platinum rewards") + 16)] \
        if "platinum rewards" in t.lower() else []
    res = masker(protected).mask_lines([("This is Platinum Rewards card offer", "AGENT")])
    assert "Platinum Rewards" in res.lines[0]


def test_amounts_and_rates_not_masked():
    res = masker().mask_lines([("the fee is rupees 2,500 and rate is 10.99% for 60 months", "AGENT")])
    assert res.lines[0] == "the fee is rupees 2,500 and rate is 10.99% for 60 months"


def test_egress_scan_ignores_surrogates():
    assert find_pii("customer <CARD_1> <PHONE_2> said hi") == []
    assert find_pii("card 4111111111111111") != []
