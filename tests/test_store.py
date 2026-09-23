"""raven/store.py — SQLite history persistence."""


def test_round_trip(store):
    assert store.load() == []
    store.append([{"role": "user", "content": "hi"}])
    assert store.load() == [{"role": "user", "content": "hi"}]


def test_append_preserves_order_and_extra_keys(store):
    """Assistant messages carry extra keys (tool_calls, reasoning) beyond
    role/content — these must survive a round trip unchanged."""
    store.append([
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}], "reasoning": "thinking"},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ])
    store.append([{"role": "assistant", "content": "done"}])
    loaded = store.load()
    assert [m["role"] for m in loaded] == ["user", "assistant", "tool", "assistant"]
    assert loaded[1]["tool_calls"] == [{"id": "c1"}]
    assert loaded[1]["reasoning"] == "thinking"


def test_clear(store):
    store.append([{"role": "user", "content": "hi"}])
    store.clear()
    assert store.load() == []


def test_new_store_reads_back_what_a_previous_one_wrote(tmp_path):
    """Simulates two separate runs of the CLI against the same db file."""
    from raven.store import Store
    path = tmp_path / "history.db"
    Store(path).append([{"role": "user", "content": "hi"}])
    assert Store(path).load() == [{"role": "user", "content": "hi"}]
