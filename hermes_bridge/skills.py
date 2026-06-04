"""
Skills module for the Hermes Bridge API.

Scans ~/.hermes/skills/ for available skills and provides CRUD for
agent-skill attachments persisted in ~/.hermes/hermes.db SQLite.
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .hermes_env import get_hermes_home

logger = logging.getLogger("hermes_bridge.skills")


def _skills_dir() -> Path:
    """Resolve ~/.hermes/skills lazily (never at import time)."""
    return get_hermes_home() / "skills"

# ── SQLite persistence ────────────────────────────────────────────────

_local = threading.local()
_db_path: str | None = None
_lock = threading.Lock()


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        # Same DB the Hermes CLI uses — HERMES_HOME / profile-aware.
        _db_path = str(get_hermes_home() / "hermes.db")
    return _db_path


def _get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        path = _get_db_path()
        _local.conn = sqlite3.connect(path, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _init_table(_local.conn)
    return _local.conn


def _init_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            attached_at REAL NOT NULL,
            UNIQUE(agent_id, skill_name)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_skills_agent
        ON agent_skills(agent_id)
    """)
    conn.commit()


# ── Frontmatter parser ────────────────────────────────────────────────


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Minimal YAML frontmatter parser for SKILL.md files."""
    import re as _re
    if not content.startswith("---"):
        return {}, content
    m = _re.search(r"\n---\s*\n", content[3:])
    if not m:
        return {}, content
    fm_end = m.start() + 3 + 3
    fm_text = content[3 : m.start() + 3]
    body = content[fm_end:]

    frontmatter = {}
    for line in fm_text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                try:
                    frontmatter[key] = json.loads(val.replace("'", '"'))
                except json.JSONDecodeError:
                    frontmatter[key] = val
            else:
                frontmatter[key] = val
    return frontmatter, body


# ── Skill scanning ────────────────────────────────────────────────────


def list_skills() -> list[dict]:
    """List all available skills in ~/.hermes/skills/.

    Returns list of {name, description, category}.
    """
    skills_dir = _skills_dir()
    if not skills_dir.exists():
        return []

    skills = []
    seen_names: set[str] = set()

    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        rel = skill_md.relative_to(skills_dir)
        parts = rel.parts
        if len(parts) >= 3:
            category = parts[0]
        elif len(parts) == 2:
            category = ""
        else:
            continue

        try:
            content = skill_md.read_text(encoding="utf-8")[:4000]
            frontmatter, body = _parse_frontmatter(content)

            name = frontmatter.get("name", skill_md.parent.name)
            if name in seen_names:
                continue
            seen_names.add(name)

            description = frontmatter.get("description", "")
            if not description:
                for line in body.strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        description = line
                        break

            skills.append({
                "name": name,
                "description": str(description)[:1024],
                "category": category,
            })
        except Exception as e:
            logger.debug("Skipping skill %s: %s", skill_md, e)
            continue

    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))


def get_skill(name: str) -> Optional[dict]:
    """Get full detail for a skill by name.

    Returns {name, description, category, tags, content, linked_files}
    or None if not found.
    """
    skills_dir = _skills_dir()
    if not skills_dir.exists():
        return None

    for skill_md in skills_dir.rglob("SKILL.md"):
        rel = skill_md.relative_to(skills_dir)
        parts = rel.parts
        if len(parts) >= 3:
            category = parts[0]
        elif len(parts) == 2:
            category = ""
        else:
            continue

        try:
            full_content = skill_md.read_text(encoding="utf-8")
            frontmatter, body = _parse_frontmatter(full_content)

            skill_name = frontmatter.get("name", skill_md.parent.name)
            if skill_name != name:
                continue

            tags = []
            metadata = frontmatter.get("metadata", {})
            if isinstance(metadata, dict):
                hermes_meta = metadata.get("hermes", {})
                if isinstance(hermes_meta, dict):
                    tags = hermes_meta.get("tags", [])
            if not tags:
                raw_tags = frontmatter.get("tags", [])
                if isinstance(raw_tags, list):
                    tags = raw_tags
                elif isinstance(raw_tags, str):
                    tags = [t.strip() for t in raw_tags.split(",")]

            skill_dir = skill_md.parent
            linked_files: dict[str, list[str]] = {}
            for subdir_name in ("references", "templates", "scripts", "assets"):
                subdir = skill_dir / subdir_name
                if subdir.exists() and subdir.is_dir():
                    files = sorted(f.name for f in subdir.iterdir() if f.is_file())
                    if files:
                        linked_files[subdir_name] = files

            return {
                "name": skill_name,
                "description": str(frontmatter.get("description", ""))[:1024],
                "category": category,
                "tags": tags,
                "content": full_content,
                "linked_files": linked_files,
            }
        except Exception:
            continue

    return None


def get_skill_linked_file(name: str, file_path: str) -> Optional[str]:
    """Get content of a linked file within a skill."""
    skills_dir = _skills_dir()
    if not skills_dir.exists():
        return None

    for skill_md in skills_dir.rglob("SKILL.md"):
        try:
            full_content = skill_md.read_text(encoding="utf-8")[:2000]
            frontmatter, _ = _parse_frontmatter(full_content)
            skill_name = frontmatter.get("name", skill_md.parent.name)
            if skill_name != name:
                continue
            linked = skill_md.parent / file_path
            if not linked.exists() or not linked.is_file():
                return None
            return linked.read_text(encoding="utf-8")
        except Exception:
            continue

    return None


# ── Agent skill attachments (SQLite persisted) ────────────────────────


def attach_skill_to_agent(agent_id: str, skill_name: str) -> dict:
    """Attach a skill to an agent. Persisted in SQLite. Returns the record."""
    conn = _get_connection()
    now = time.time()
    with _lock:
        # Check if already attached
        existing = conn.execute(
            "SELECT attached_at FROM agent_skills WHERE agent_id = ? AND skill_name = ?",
            (agent_id, skill_name),
        ).fetchone()
        if existing:
            return {"skill_name": skill_name, "attached_at": existing["attached_at"]}

        conn.execute(
            "INSERT INTO agent_skills (agent_id, skill_name, attached_at) VALUES (?, ?, ?)",
            (agent_id, skill_name, now),
        )
        conn.commit()

    logger.info("Attached skill '%s' to agent '%s'", skill_name, agent_id)
    return {"skill_name": skill_name, "attached_at": now}


def detach_skill_from_agent(agent_id: str, skill_name: str) -> bool:
    """Remove a skill attachment. Returns True if found and removed."""
    conn = _get_connection()
    with _lock:
        cursor = conn.execute(
            "DELETE FROM agent_skills WHERE agent_id = ? AND skill_name = ?",
            (agent_id, skill_name),
        )
        conn.commit()
    removed = cursor.rowcount > 0
    if removed:
        logger.info("Detached skill '%s' from agent '%s'", skill_name, agent_id)
    return removed


def get_agent_skills(agent_id: str) -> list[dict]:
    """List skills attached to an agent with full metadata."""
    conn = _get_connection()
    rows = conn.execute(
        "SELECT skill_name, attached_at FROM agent_skills WHERE agent_id = ? ORDER BY attached_at ASC",
        (agent_id,),
    ).fetchall()

    result = []
    for row in rows:
        skill = get_skill(row["skill_name"])
        if skill:
            result.append({
                "skill_name": row["skill_name"],
                "attached_at": row["attached_at"],
                "description": skill.get("description", ""),
                "category": skill.get("category", ""),
                "tags": skill.get("tags", []),
            })
        else:
            result.append({
                "skill_name": row["skill_name"],
                "attached_at": row["attached_at"],
                "description": "(skill no longer available on disk)",
                "category": "",
                "tags": [],
            })
    return result
