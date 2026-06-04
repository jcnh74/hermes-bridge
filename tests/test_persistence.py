"""Tests for persistence — message storage in SQLite."""

import time

from hermes_bridge import persistence


def test_store_and_get_single_message(temp_db):
    persistence.store_message("sess1", "main", "user", "hello")
    msgs = persistence.get_messages("sess1")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"
    assert "created_at" in msgs[0]


def test_messages_ordered_chronologically(temp_db):
    persistence.store_message("sess1", "main", "user", "first")
    time.sleep(0.01)
    persistence.store_message("sess1", "main", "assistant", "second")
    time.sleep(0.01)
    persistence.store_message("sess1", "main", "user", "third")
    msgs = persistence.get_messages("sess1")
    assert [m["content"] for m in msgs] == ["first", "second", "third"]


def test_get_messages_isolated_by_session(temp_db):
    persistence.store_message("sessA", "main", "user", "a-msg")
    persistence.store_message("sessB", "main", "user", "b-msg")
    a = persistence.get_messages("sessA")
    b = persistence.get_messages("sessB")
    assert len(a) == 1 and a[0]["content"] == "a-msg"
    assert len(b) == 1 and b[0]["content"] == "b-msg"


def test_get_messages_empty_session(temp_db):
    assert persistence.get_messages("does-not-exist") == []


def test_get_all_session_keys_ordered_by_recency(temp_db):
    persistence.store_message("old", "agent1", "user", "x")
    time.sleep(0.01)
    persistence.store_message("new", "agent1", "user", "y")
    keys = persistence.get_all_session_keys("agent1")
    assert keys == ["new", "old"]  # most recent first


def test_get_all_session_keys_filtered_by_agent(temp_db):
    persistence.store_message("s1", "agent1", "user", "x")
    persistence.store_message("s2", "agent2", "user", "y")
    assert persistence.get_all_session_keys("agent1") == ["s1"]
    assert persistence.get_all_session_keys("agent2") == ["s2"]


def test_session_summary_counts_messages(temp_db):
    persistence.store_message("s1", "agent1", "user", "a")
    persistence.store_message("s1", "agent1", "assistant", "b")
    summary = persistence.get_session_summary("s1", "agent1")
    assert summary is not None
    assert summary["message_count"] == 2
    assert summary["status"] == "active"
    assert summary["key"] == "s1"


def test_session_summary_none_when_empty(temp_db):
    assert persistence.get_session_summary("ghost", "agent1") is None


def test_store_message_survives_reconnect(temp_db, monkeypatch):
    """Messages persist across thread-local connection resets (restart sim)."""
    persistence.store_message("persist", "main", "user", "durable")
    # Simulate a restart: drop the cached connection.
    if hasattr(persistence._local, "conn"):
        persistence._local.conn.close()
        del persistence._local.conn
    msgs = persistence.get_messages("persist")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "durable"
