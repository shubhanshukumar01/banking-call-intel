"""Synthetic banking call transcripts with ground truth (for smoke tests, load tests and
the evaluation golden set). All PII is fake but *valid-looking* (Luhn-valid cards,
Verhoeff-valid Aadhaar numbers, PAN format, Indian mobiles), read out the way callers
do ("nine eight double seven ..."), and buried in ASR noise.

    python -m tools.synth --n 5 --lines 6000 --out data/incoming
    python -m tools.synth --n 8 --lines 500 --out evaluation/golden/transcripts --truth evaluation/golden/truth
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

AGENTS = ["Priya", "Arjun", "Neha", "Vikram", "Sneha", "Rohan", "Ananya", "Karan"]
CUSTOMERS = ["Rahul Sharma", "Meera Iyer", "Sanjay Gupta", "Pooja Nair", "Amit Verma", "Kavita Rao",
             "Deepak Joshi", "Fatima Khan", "Suresh Reddy", "Lakshmi Menon"]
STREETS = ["MG Road", "Linking Road", "Park Street", "Anna Salai", "Brigade Road", "FC Road"]
CITIES = ["Bengaluru", "Mumbai", "Kolkata", "Chennai", "Pune", "Hyderabad"]
DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
ORD = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
       "eleventh", "twelfth", "fifteenth", "twentieth", "twenty second", "twenty eighth"]

ALIASES = {
    "CC_PLATINUM_REWARDS": ["platinum rewards card", "plat card", "platinum card", "the platinum one",
                            "plat rewards card", "platinum credit card"],
    "CC_CASHBACK": ["cashback card", "cash back card", "the cashback one", "cashbak card"],
    "CC_MILES_TRAVEL": ["miles card", "travel card", "air miles card"],
    "PL_PERSONAL_LOAN": ["personal loan", "pre approved loan", "instant loan", "insta loan", "personal lone"],
    "HL_HOME_LOAN": ["home loan", "housing loan"],
    "SA_SAVINGS_MAX": ["savings max account", "savings max", "premium savings account"],
    "FD_FIXED_DEPOSIT": ["fixed deposit", "FD", "term deposit"],
    "INV_MUTUAL_FUND_SIP": ["SIP", "mutual fund", "systematic investment plan"],
    "DC_DEBIT_CARD": ["debit card", "ATM card"],
}

_V_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
        [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
        [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
        [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_V_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
        [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
        [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]
_V_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def luhn_card(rng: random.Random) -> str:
    body = [4] + [rng.randint(0, 9) for _ in range(14)]
    total = 0
    for i, d in enumerate(reversed(body)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return "".join(map(str, body + [(10 - total % 10) % 10]))


def aadhaar(rng: random.Random) -> str:
    body = [rng.randint(2, 9)] + [rng.randint(0, 9) for _ in range(10)]
    c = 0
    for i, d in enumerate(reversed(body)):
        c = _V_D[c][_V_P[(i + 1) % 8][d]]
    return "".join(map(str, body + [_V_INV[c]]))


def spoken(num: str, rng: random.Random) -> str:
    out, i = [], 0
    while i < len(num):
        if i + 1 < len(num) and num[i] == num[i + 1] and rng.random() < 0.5:
            out.append(f"double {DIGIT_WORDS[int(num[i])]}")
            i += 2
        else:
            out.append(DIGIT_WORDS[int(num[i])])
            i += 1
    return " ".join(out)


class CallBuilder:
    def __init__(self, rng: random.Random, labels: tuple[str, str]) -> None:
        self.rng = rng
        self.a, self.c = labels
        self.lines: list[str] = []
        self.sec = rng.randint(0, 20)

    def _ts(self) -> str:
        self.sec += self.rng.randint(1, 6)
        return f"[{self.sec // 3600:02d}:{self.sec % 3600 // 60:02d}:{self.sec % 60:02d}]"

    def say(self, who: str, text: str) -> None:
        label = {"A": self.a, "C": self.c, "S": "IVR"}[who]
        if who != "S" and self.rng.random() < 0.15:
            text = self.rng.choice(["um, ", "uh ", "so, ", "okay so "]) + text
        if self.rng.random() < 0.05:
            w = text.split()
            if len(w) > 3:
                k = self.rng.randint(0, len(w) - 2)
                w.insert(k, w[k])  # stutter
                text = " ".join(w)
        self.lines.append(f"{self._ts()} {label}: {text}")
        if self.rng.random() < 0.02:
            self.lines.append(self.lines[-1])  # duplicated ASR segment

    def noise_block(self, n: int) -> None:
        rng = self.rng
        for _ in range(n):
            r = rng.random()
            if r < 0.25:
                self.lines.append(f"{self._ts()} {self.a}: Please stay on the line, I am still checking.")
                for _ in range(rng.randint(2, 6)):
                    self.lines.append(f"{self._ts()} [hold music]")
                self.lines.append(f"{self._ts()} {self.a}: Thank you for holding.")
            elif r < 0.45:
                d, amt = rng.randint(1, 28), rng.choice(["1,250", "640", "2,999", "18,400", "75", "310"])
                place = rng.choice(["a grocery store", "an online merchant", "a fuel station", "a pharmacy",
                                    "a restaurant", "a utility biller"])
                self.say("A", f"On the {d}th there is a debit of rupees {amt} at {place}.")
                self.say("C", rng.choice(["mm-hmm", "okay", "yes that is mine", "right", "uh-huh"]))
            elif r < 0.6:
                self.say("C", rng.choice(["Sorry, the network is a bit weak here.", "Can you repeat that?",
                                          "Hello? Am I audible?", "[inaudible] yes I can hear you now"]))
                self.say("A", rng.choice(["Yes sir, you are audible.", "No problem, I will repeat.",
                                          "Yes, I can hear you clearly now."]))
            elif r < 0.75:
                self.say("A", rng.choice(["The system is a little slow today, just a moment.",
                                          "Let me pull up the details on my screen.",
                                          "I am just updating the notes on your profile."]))
                self.say("C", rng.choice(["Sure, take your time.", "Okay.", "mm-hmm", "No issues."]))
            elif r < 0.85:
                self.say("C", rng.choice(["It has been raining a lot here.", "I am actually driving right now.",
                                          "My son usually handles these things.", "I am in office now."]))
                self.say("A", rng.choice(["Oh, I understand.", "Please take care.", "Sure, no problem at all."]))
            else:
                self.say("A", "[crosstalk] sorry, please go ahead.")
                self.say("C", "No no, you go ahead.")


def build_call(seed: int, target_lines: int) -> tuple[str, dict]:
    rng = random.Random(seed)
    labels = rng.choice([("Agent", "Customer"), ("Agent", "Customer"), ("Rep", "Caller"), ("Speaker 1", "Speaker 2")])
    b = CallBuilder(rng, labels)
    agent, cust = rng.choice(AGENTS), rng.choice(CUSTOMERS)
    first = cust.split()[0]
    card, aad, acct = luhn_card(rng), aadhaar(rng), "".join(str(rng.randint(0, 9)) for _ in range(12))
    phone = str(rng.choice([9, 8, 7])) + "".join(str(rng.randint(0, 9)) for _ in range(9))
    email = f"{first.lower()}.{cust.split()[1].lower()}{rng.randint(1, 99)}@gmail.com"
    dob = f"{rng.choice(ORD)} {rng.choice(MONTHS)} nineteen {rng.choice(['eighty', 'ninety', 'seventy'])} {rng.choice(DIGIT_WORDS[1:])}"
    address = f"{rng.randint(2, 99)} {rng.choice(STREETS)}, {rng.choice(CITIES)}"
    scenario = rng.choice(["sales_card", "sales_loan", "service_dispute", "complaint_fee", "mixed_block_cross",
                           "investment_sip"])
    rec_met = rng.random() < 0.8
    verify = scenario != "sales_card" or rng.random() < 0.6
    truth: dict = {"scenario": scenario, "products": [], "objections": [], "mentions": [], "pii": [],
                   "compliance": {"REC_DISCLOSURE": "met" if rec_met else "missed"},
                   "identity_verification": "performed" if verify else "not_performed"}

    def gap() -> None:
        # spread business content through the call, like real long calls (not all at the start)
        b.noise_block(max(3, target_lines // 40 + rng.randint(-2, 2)))

    def mention(pid: str) -> str:
        alias = rng.choice(ALIASES[pid])
        truth["mentions"].append({"surface": alias, "product_id": pid})
        if pid not in truth["products"]:
            truth["products"].append(pid)
        return alias

    b.lines.append(f"{b._ts()} IVR: Welcome to Horizon Bank. For credit cards press 1, for loans press 2.")
    b.lines.append(f"{b._ts()} [hold music]")
    b.lines.append(f"{b._ts()} IVR: Your call is important to us. Please hold.")
    if rec_met:
        b.say("A", "This call is being recorded for quality and training purposes.")
    b.say("A", f"Thank you for calling Horizon Bank, my name is {agent}, how may I help you today?")
    b.say("C", f"Hi {agent}, this is {cust}.")
    truth["pii"] += [{"type": "PERSON", "value": cust}, {"type": "PERSON", "value": agent}]
    if verify:
        b.say("A", "Before we proceed, for verification can you please confirm your date of birth?")
        b.say("C", f"It is {dob}.")
        b.say("A", "And your registered mobile number please?")
        b.say("C", spoken(phone, rng))
        b.say("A", f"Thank you Mr. {cust.split()[1]}, your identity is verified.")
        truth["pii"] += [{"type": "PHONE", "value": phone}, {"type": "DOB", "value": dob}]
    gap()

    def apr_disclosure(pid: str) -> None:
        if pid.startswith("CC_"):
            b.say("A", "The interest rate is three point six percent per month and the annual fee is rupees 2,500.")
        else:
            b.say("A", "The interest rate starts at 10.99% per annum with a processing fee of up to 2%.")

    outcome = "no_pitch"
    if scenario in ("sales_card", "sales_loan", "investment_sip", "mixed_block_cross"):
        if scenario == "mixed_block_cross":
            truth.update(call_type="mixed", primary_intent="card_block_or_replace")
            b.say("C", f"I lost my {mention('DC_DEBIT_CARD')} yesterday, I want to block it immediately.")
            b.say("A", "I am sorry to hear that. I have blocked the card now. A replacement will reach you in 7 working days.")
            b.say("C", f"Please send it to my address, {address}.")
            truth["pii"].append({"type": "ADDRESS", "value": address})
            gap()
            pid = "CC_CASHBACK"
        elif scenario == "sales_loan":
            truth.update(call_type="sales", primary_intent="loan_enquiry")
            pid = "PL_PERSONAL_LOAN"
            b.say("C", f"I got an SMS about a {mention(pid)}, I wanted to know more about it.")
        elif scenario == "investment_sip":
            truth.update(call_type="sales", primary_intent="new_product_enquiry")
            pid = "INV_MUTUAL_FUND_SIP"
            b.say("C", f"I want to start investing, my friend suggested a {mention(pid)}.")
        else:
            truth.update(call_type="sales", primary_intent="rewards_or_offers")
            pid = rng.choice(["CC_PLATINUM_REWARDS", "CC_MILES_TRAVEL"])
            b.say("C", "I was calling to check my reward points balance.")
            b.say("A", "Sure, you have 4,200 points. Also, you are pre-approved for an upgrade.")
        b.say("A", f"You are eligible for our {mention(pid)} with special benefits.")
        apr_ok = rng.random() < 0.7
        if pid.startswith(("CC_", "PL_")):
            truth["compliance"]["APR_FEE_DISCLOSURE"] = "met" if apr_ok else "missed"
            if apr_ok:
                apr_disclosure(pid)
        if pid == "INV_MUTUAL_FUND_SIP":
            if rng.random() < 0.5:
                b.say("A", "Honestly this gives guaranteed returns of twelve percent, there is no risk at all.")
                truth["risk_flag"] = "guaranteed_returns_claim"
                truth["compliance"]["NO_GUARANTEED_RETURNS"] = "missed"
            else:
                b.say("A", "Mutual fund investments are subject to market risks, returns are not guaranteed.")
                truth["compliance"]["NO_GUARANTEED_RETURNS"] = "met"
        gap()
        obj = rng.choice(["fees_too_high", "needs_time_to_think", "already_has_product", None])
        if obj == "fees_too_high":
            b.say("C", "Honestly the annual fee is too high for me, I don't spend that much.")
        elif obj == "needs_time_to_think":
            b.say("C", "Let me think about it, I don't want to decide right now.")
        elif obj == "already_has_product":
            other = "CC_CASHBACK" if pid != "CC_CASHBACK" else "CC_PLATINUM_REWARDS"
            b.say("C", f"But I already have your {mention(other)}, why would I need another one?")
        if obj:
            truth["objections"].append(obj)
            b.say("A", "I understand. The fee is waived if you spend one lakh in a year, and the benefits are worth it.")
        gap()
        outcome = rng.choice(["converted", "declined", "interested_follow_up"])
        if outcome == "converted":
            b.say("C", "Okay fine, sounds good, please go ahead and apply.")
            b.say("A", "Do you agree to the terms and conditions and give your consent to proceed?")
            b.say("C", "Yes, I agree.")
            truth["compliance"]["TNC_CONSENT"] = "met"
        elif outcome == "declined":
            b.say("C", "No thanks, I am not interested.")
        else:
            b.say("C", "Can you call me back next week? I will decide by then.")
            b.say("A", "Sure, I will schedule a callback for next Monday.")
        if scenario == "mixed_block_cross":
            truth["sales_outcome"] = outcome
    else:
        if scenario == "service_dispute":
            truth.update(call_type="service", primary_intent="transaction_dispute")
            pid = rng.choice(["CC_PLATINUM_REWARDS", "CC_CASHBACK"])
            b.say("C", f"There is a transaction on my {mention(pid)} that I did not make, rupees 18,400.")
            b.say("C", f"The card number is {spoken(card, rng)}.")
            truth["pii"].append({"type": "CARD", "value": card})
            b.say("A", "I have raised a dispute. Your dispute reference number is DSP4471928. It will be resolved within 10 working days.")
            truth["compliance"]["COMPLAINT_REFERENCE"] = "met"
        else:
            truth.update(call_type="complaint", primary_intent="fee_reversal_request")
            pid = rng.choice(["CC_CASHBACK", "SA_SAVINGS_MAX"])
            b.say("C", f"I am really frustrated, I was charged a late fee on my {mention(pid)} even though I paid on time.")
            b.say("C", f"My account number is {spoken(acct, rng)}.")
            truth["pii"].append({"type": "ACCOUNT", "value": acct})
            b.say("A", "I apologise for the inconvenience. I have requested a reversal of the fee.")
            if rng.random() < 0.6:
                b.say("A", "Your complaint number is CMP9920113, it will be resolved within 7 working days.")
                truth["compliance"]["COMPLAINT_REFERENCE"] = "met"
            else:
                truth["compliance"]["COMPLAINT_REFERENCE"] = "missed"
        truth["sales_outcome"] = "no_pitch"
    truth.setdefault("sales_outcome", outcome)
    gap()
    if rng.random() < 0.5:
        b.say("C", f"Also, please update my email to {email.replace('@', ' at ').replace('.', ' dot ')}.")
        truth["pii"].append({"type": "EMAIL", "value": email})
    if rng.random() < 0.4:
        b.say("C", f"For KYC my Aadhaar is {spoken(aad, rng)}.")
        truth["pii"].append({"type": "AADHAAR", "value": aad})

    # pad to target length with realistic low-signal conversation, keeping the closing at the end
    closing = [("A", "Is there anything else I can help you with today?"), ("C", "No, that's all. Thank you."),
               ("A", f"Thank you for calling Horizon Bank, have a great day {first}.")]
    while len(b.lines) < target_lines - len(closing) - 2:
        b.noise_block(1)
    for who, text in closing:
        b.say(who, text)
    return "\n".join(b.lines[: max(target_lines, len(b.lines))]) + "\n", truth


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--lines", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data/incoming")
    ap.add_argument("--truth", default=None, help="folder for ground-truth JSON (evaluation golden set)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for i in range(args.n):
        text, truth = build_call(args.seed + i, args.lines)
        name = f"call_{args.seed + i:05d}.txt"
        (out / name).write_text(text, encoding="utf-8")
        if args.truth:
            t = Path(args.truth)
            t.mkdir(parents=True, exist_ok=True)
            (t / name.replace(".txt", ".json")).write_text(json.dumps({"file": name, **truth}, indent=2),
                                                           encoding="utf-8")
        print(f"{name}: {len(text.splitlines())} lines, scenario={truth['scenario']}")


if __name__ == "__main__":
    main()
