"""Shared pytest fixtures for the Hermes Bridge test suite.

None of these tests require a live Hermes install or a running server —
they exercise the bridge's own pure logic (discovery, persistence, skill
scanning, model validation, CLI helpers) against temp dirs and temp DBs.
"""

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point persistence + skills modules at a throwaway SQLite DB.

    Both modules keep a module-level ``_db_path`` and a thread-local
    connection. We reset both so every test gets an isolated database.
    """
    from hermes_bridge import persistence, skills

    db_file = str(tmp_path / "test.db")

    for mod in (persistence, skills):
        monkeypatch.setattr(mod, "_db_path", db_file, raising=False)
        # Drop any cached thread-local connection from a previous test.
        if hasattr(mod._local, "conn"):
            try:
                mod._local.conn.close()
            except Exception:
                pass
            del mod._local.conn

    yield db_file

    # Teardown: close connections so the temp file can be removed cleanly.
    for mod in (persistence, skills):
        if hasattr(mod._local, "conn"):
            try:
                mod._local.conn.close()
            except Exception:
                pass
            del mod._local.conn


@pytest.fixture
def temp_skills_dir(tmp_path, monkeypatch):
    """Create a fake ~/.hermes home with a skills/ tree and point the
    skills module at it via get_hermes_home()."""
    from hermes_bridge import skills

    home = tmp_path / "hermes_home"
    skills_root = home / "skills"
    skills_root.mkdir(parents=True)

    monkeypatch.setattr(skills, "get_hermes_home", lambda: home)
    return skills_root


def write_skill(skills_root: Path, rel: str, name: str, description: str,
                extra_frontmatter: str = "", body: str = "Body text."):
    """Helper: write a SKILL.md at skills_root/rel/SKILL.md."""
    skill_dir = skills_root / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {description}\n{extra_frontmatter}---\n\n{body}\n"
    (skill_dir / "SKILL.md").write_text(fm, encoding="utf-8")
    return skill_dir
