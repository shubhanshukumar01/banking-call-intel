from callintel.preprocess.cleaning import NoiseCleaner
from callintel.preprocess.pii import ContextRecognizer, RegexRecognizer
from callintel.preprocess.transcript import AGENT, CUSTOMER, chunk_turns, parse_lines, prepare_transcript


def test_noise_cleaner_keeps_meaning():
    c = NoiseCleaner()
    assert c.clean("um I I I want to uh cancel [inaudible] the card") == "I want to cancel the card"
    assert c.clean("[hold music]") == ""
    assert c.clean("No no no, I do not agree") == "No, I do not agree"
    assert c.clean("yes") == "yes"


def test_parse_roles_and_named_labels(settings):
    raw = "\n".join(["[00:00:01] Priya: Thank you for calling, how may I help you?",
                     "[00:00:03] Rahul: I want to block my card",
                     "continuation of rahul line",
                     "Priya: Let me check that for you",
                     "Rahul: ok"] * 3)
    lines = parse_lines(raw, settings)
    assert lines[0].speaker == AGENT and lines[1].speaker == CUSTOMER
    assert lines[2].speaker == CUSTOMER  # unlabeled continuation
    assert "Priya" not in lines[0].text  # label (a name) is not part of the text


def test_repetition_compression_keeps_short_answers(settings):
    lines = []
    for i in range(30):
        lines += [f"Agent: On the {i + 1}th there is a debit of rupees {100 + i} at a grocery store",
                  "Agent: Do you agree to proceed?", "Customer: yes"]
    p = prepare_transcript("\n".join(lines), call_id="c", file_name="f", sha256="s", settings=settings,
                           cheap=[RegexRecognizer(), ContextRecognizer()], ner=[], protected_spans=None)
    texts = [ln.text for ln in p.lines.values()]
    assert sum("debit of rupees" in t for t in texts) == settings.preprocess.max_repeats
    assert sum(t == "yes" for t in texts) == 30            # consent answers are never compressed
    # both 5+-word templates (read-out and the repeated agent question) are compressed
    assert p.noise_stats["dropped_repetitive"] == 2 * (30 - settings.preprocess.max_repeats)


def test_chunking_respects_turns_and_overlap(settings):
    settings.preprocess.max_repeats = 0  # lines below share one template; test chunking only
    raw = "\n".join(f"Agent: sentence number {i} about the platinum card offer and fees"
                    if i % 2 == 0 else f"Customer: reply {i} about interest rate" for i in range(400))
    p = prepare_transcript(raw, call_id="c", file_name="f", sha256="s", settings=settings,
                           cheap=[RegexRecognizer(), ContextRecognizer()], ner=[], protected_spans=None)
    assert len(p.chunks) > 1
    assert all(c.tokens <= settings.chunking.max_tokens + 50 for c in p.chunks)
    # overlap: next chunk starts with the last turns of the previous one
    assert p.chunks[1].turns[0].line_start == p.chunks[0].turns[-settings.chunking.overlap_turns].line_start
    # original line numbers preserved
    assert p.turns[0].segments[0].line == 1


def test_chunk_turns_single_long_turn():
    from callintel.preprocess.transcript import Segment, Turn
    t = Turn(0, AGENT, [Segment(i, "word " * 200) for i in range(1, 40)])
    chunks = chunk_turns([t], target_tokens=500, max_tokens=600, overlap_turns=1)
    assert len(chunks) > 1
