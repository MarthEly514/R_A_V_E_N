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


def test_store_is_usable_from_a_different_thread_than_it_was_created_on(tmp_path):
    """Regression (found live in the desktop UI): server.py creates the Store
    on the event-loop thread and Assistant.ask() uses it from a worker thread,
    which used to raise sqlite3.ProgrammingError on every request."""
    import threading
    from raven.store import Store
    store = Store(tmp_path / "h.db")
    errors, loaded = [], []

    def worker():
        try:
            store.append([{"role": "user", "content": "hi"}])
            loaded.extend(store.load())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=worker); t.start(); t.join()
    assert errors == []
    assert loaded == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _user(text):
    return {"role": "user", "content": text}


def test_a_fresh_database_gets_one_empty_default_session(tmp_path):
    from raven.store import Store, DEFAULT_TITLE
    s = Store(tmp_path / "h.db")
    assert [x["title"] for x in s.list_sessions()] == [DEFAULT_TITLE]
    assert s.load() == []


def test_sessions_keep_their_messages_separate(tmp_path):
    from raven.store import Store
    path = tmp_path / "h.db"
    a = Store(path)
    a.append([_user("in A")])
    b_id = a.create_session()
    b = Store(path, session_id=b_id)
    b.append([_user("in B")])
    assert a.load() == [_user("in A")]
    assert b.load() == [_user("in B")]
    assert Store(path, session_id=a.session_id).load() == [_user("in A")]


def test_title_comes_from_the_first_user_message_and_is_not_overwritten(tmp_path):
    from raven.store import Store
    s = Store(tmp_path / "h.db")
    s.append([_user("Summarize the GOD2 pdf please")])
    s.append([_user("something later")])
    assert s.list_sessions()[0]["title"] == "Summarize the GOD2 pdf please"


def test_long_multiline_titles_are_tidied_and_truncated():
    from raven.store import make_title, MAX_TITLE_CHARS
    t = make_title("  a   very long first line " + "x" * 100 + "\nsecond line")
    assert "\n" not in t and len(t) <= MAX_TITLE_CHARS and t.endswith("…")
    assert make_title("   ") == "New chat"


def test_a_non_string_first_message_does_not_become_a_title(tmp_path):
    from raven.store import Store, DEFAULT_TITLE
    s = Store(tmp_path / "h.db")
    s.append([{"role": "user", "content": [{"type": "image_url"}]}])
    assert s.list_sessions()[0]["title"] == DEFAULT_TITLE


def test_sessions_are_listed_most_recently_active_first(tmp_path, monkeypatch):
    import time as _time
    from raven import store as store_mod
    ticks = iter(range(100, 200))
    monkeypatch.setattr(store_mod.time, "time", lambda: float(next(ticks)))
    s = store_mod.Store(tmp_path / "h.db")
    first = s.session_id
    second = s.create_session("second")
    s.append([_user("touch first")])            # first becomes the most recent
    assert [x["id"] for x in s.list_sessions()] == [first, second]


def test_default_store_opens_the_most_recently_active_session(tmp_path):
    from raven.store import Store
    path = tmp_path / "h.db"
    a = Store(path)
    a.append([_user("older")])
    other = a.create_session()
    Store(path, session_id=other).append([_user("newer")])
    assert Store(path).load() == [_user("newer")]


def test_delete_session_removes_its_messages_only(tmp_path):
    from raven.store import Store
    path = tmp_path / "h.db"
    a = Store(path); a.append([_user("keep")])
    other = a.create_session(); Store(path, session_id=other).append([_user("drop")])
    a.delete_session(other)
    assert [x["id"] for x in a.list_sessions()] == [a.session_id]
    assert a.message_count(other) == 0 and a.load() == [_user("keep")]


def test_clear_only_clears_the_current_session_and_resets_its_title(tmp_path):
    from raven.store import Store, DEFAULT_TITLE
    path = tmp_path / "h.db"
    a = Store(path); a.append([_user("mine")])
    other = a.create_session(); b = Store(path, session_id=other); b.append([_user("theirs")])
    a.clear()
    assert a.load() == [] and b.load() == [_user("theirs")]
    assert next(x for x in a.list_sessions() if x["id"] == a.session_id)["title"] == DEFAULT_TITLE


def test_opening_an_unknown_session_is_an_error(tmp_path):
    import pytest
    from raven.store import Store
    Store(tmp_path / "h.db")
    with pytest.raises(ValueError):
        Store(tmp_path / "h.db", session_id=999)


def test_rename_session(tmp_path):
    from raven.store import Store
    s = Store(tmp_path / "h.db")
    s.rename_session(s.session_id, "  My chat ")
    assert s.list_sessions()[0]["title"] == "My chat"


def test_a_pre_sessions_database_is_migrated_into_one_session(tmp_path):
    """The real ~/.raven/history.db predates sessions: a flat messages table."""
    import json, sqlite3
    from raven.store import Store, MIGRATED_TITLE
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, message TEXT NOT NULL)")
    db.executemany("INSERT INTO messages (message) VALUES (?)",
                   [(json.dumps(_user("old 1")),), (json.dumps({"role": "assistant", "content": "old 2"}),)])
    db.commit(); db.close()
    s = Store(path)
    assert [x["title"] for x in s.list_sessions()] == [MIGRATED_TITLE]
    assert [m["content"] for m in s.load()] == ["old 1", "old 2"]
    assert len(Store(path).list_sessions()) == 1   # migrating twice must not duplicate anything
