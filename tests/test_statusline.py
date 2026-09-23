"""raven/statusline.py — bottom status-line segments."""
from raven import statusline


class FakeProvider:
    model = "meta-llama/llama-3.1-8b-instruct"
    last_model = "meta-llama/llama-3.1-8b-instruct:free"
    total_tokens = 12345


class FakeAssistant:
    context_len = 7
    context_chars = 2000


def test_model_segment_prefers_last_model_and_strips_free_suffix():
    assert statusline._model(FakeProvider(), FakeAssistant()) == "llama-3.1-8b-instruct"


def test_model_segment_falls_back_to_requested_model():
    p = FakeProvider()
    p.last_model = None
    assert statusline._model(p, FakeAssistant()) == "llama-3.1-8b-instruct"


def test_fmt_tokens_under_1000_is_the_raw_number():
    assert statusline._fmt_tokens(340) == "340"
    assert statusline._fmt_tokens(0) == "0"


def test_fmt_tokens_over_1000_uses_k_suffix():
    assert statusline._fmt_tokens(12000) == "12k"
    assert statusline._fmt_tokens(8400) == "8k"


def test_context_segment_shows_approx_tokens_used_over_the_budget():
    from raven.assistant import MAX_CONTEXT_CHARS
    a = FakeAssistant()
    a.context_chars = 2000  # -> 500 tokens at chars/4
    out = statusline._context(FakeProvider(), a)
    assert out == f"ctx ~500/{MAX_CONTEXT_CHARS // 4 // 1000}k tok"


def test_tokens_segment_is_comma_formatted():
    assert statusline._tokens(FakeProvider(), FakeAssistant()) == "12,345 tok"


def test_render_joins_known_segments_and_skips_unknown():
    out = statusline.render(FakeProvider(), FakeAssistant(), ["model", "bogus", "tokens"])
    assert out == "llama-3.1-8b-instruct  •  12,345 tok"


def test_render_empty_list_is_empty_string():
    assert statusline.render(FakeProvider(), FakeAssistant(), []) == ""


def test_render_survives_a_segment_that_raises():
    """memory/cpu call psutil, which can occasionally raise on odd platforms;
    render() must not let one bad segment take down the whole status line."""
    from raven import statusline as sl
    broken = dict(sl.SEGMENTS)
    broken["boom"] = lambda p, a: 1 / 0
    orig = sl.SEGMENTS
    sl.SEGMENTS = broken
    try:
        out = sl.render(FakeProvider(), FakeAssistant(), ["model", "boom", "tokens"])
    finally:
        sl.SEGMENTS = orig
    assert out == "llama-3.1-8b-instruct  •  12,345 tok"
