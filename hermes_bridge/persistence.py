"""
Message persistence for the Hermes Bridge API.

Stores all session messages in `~/.hermes/hermes.db` so they survive
bridge restarts. When the bridge starts back up, any past sessions
with stored history are still accessible via the GET messages endpoint.

Schema:
  session_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL,           -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at REAL NOT NULL,     -- unix timestamp
    UNIQUE(session_key, role, created_at)  -- no duplicate tracking
  )
"""

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("hermes_bridge.persistence")

_local = threading.local()
_db_path: str | None = None
_lock = threading.Lock()


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        _db_path = str(Path.home() / ".hermes" / "hermes.db")
    return _db_path


def _get_connection() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        path = _get_db_path()
        _local.conn = sqlite3.connect(path, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _init_table(_local.conn)
    return _local.conn


def _init_table(conn: sqlite3.Connection):
    """Create the session_messages table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (julianday('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_messages_key
        ON session_messages(session_key, created_at)
    """)
    conn.commit()


def store_message(session_key: str, agent_id: str, role: str, content: str):
    """Persist a single message to the database.
    
    Thread-safe. Handles the case where the row already exists (idempotent).
    """
    try:
        conn = _get_connection()
        now = time.time()
        with _lock:
            conn.execute(
                "INSERT OR IGNORE INTO session_messages "
                "(session_key, agent_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_key, agent_id, role, content, now),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to persist message: %s", e)


def get_messages(session_key: str) -> list[dict]:
    """Retrieve all messages for a session, ordered chronologically."""
    try:
        conn = _get_connection()
        cursor = conn.execute(
            "SELECT role, content, created_at FROM session_messages "
            "WHERE session_key = ? ORDER BY created_at ASC",
            (session_key,),
        )
        return [
            {"role": row["role"], "content": row["content"], "created_at": row["created_at"]}
            for row in cursor.fetchall()
        ]
    except Exception as e:
        logger.warning("Failed to retrieve messages: %s", e)
        return []


def get_all_session_keys(agent_id: str) -> list[str]:
    """Get all unique session keys for an agent. Used for sync."""
    try:
        conn = _get_connection()
        cursor = conn.execute(
            "SELECT session_key, MAX(created_at) as last_at "
            "FROM session_messages "
            "WHERE agent_id = ? "
            "GROUP BY session_key "
            "ORDER BY last_at DESC",
            (agent_id,),
        )
        return [row["session_key"] for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to retrieve session keys: %s", e)
        return []


def get_session_summary(session_key: str, agent_id: str) -> dict | None:
    """Get summary info for a session (message count, last message time)."""
    try:
        conn = _get_connection()
        cursor = conn.execute(
            "SELECT COUNT(*) as msg_count, MAX(created_at) as last_at "
            "FROM session_messages WHERE session_key = ? AND agent_id = ?",
            (session_key, agent_id),
        )
        row = cursor.fetchone()
        if row and row["msg_count"] > 0:
            return {
                "key": session_key,
                "agent_id": agent_id,
                "message_count": row["msg_count"],
                "last_message_at": row["last_at"],
                "status": "active",
            }
        return None
    except Exception as e:
        logger.warning("Failed to get session summary: %s", e)
        return None
