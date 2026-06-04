"""Tests for hermes_env — Hermes install discovery.

These never touch a real Hermes install: we fabricate a directory with a
run_agent.py sentinel and point discovery at it via $HERMES_AGENT_ROOT.
"""

import importlib

import pytest

from hermes_bridge import hermes_env


@pytest.fixture(autouse=True)
def reset_env_cache():
    """hermes_env caches the resolved root in module globals — reset between tests."""
    hermes_env._HERMES_ROOT = None
    hermes_env._LOADED = False
    yield
    hermes_env._HERMES_ROOT = None
    hermes_env._LOADED = False


def _make_fake_hermes(tmp_path):
    root = tmp_path / "fake-hermes"
    root.mkdir()
    (root / "run_agent.py").write_text("# sentinel\n")
    return root


def test_find_root_via_env_override(tmp_path, monkeypatch):
    root = _make_fake_hermes(tmp_path)
    monkeypatch.setenv("HERMES_AGENT_ROOT", str(root))
    found = hermes_env.find_hermes_root()
    assert found == str(root.resolve())


def test_find_root_returns_none_when_missing(tmp_path, monkeypatch):
    # Point the override at an empty dir (no run_agent.py) and ensure the
    # standard location + parents don't accidentally contain a sentinel.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("HERMES_AGENT_ROOT", str(empty))
    monkeypatch.setattr(hermes_env.Path, "home", lambda: tmp_path / "no_home")
    # Neutralize the parent-walk by pointing __file__ resolution at an isolated tree.
    monkeypatch.setattr(
        hermes_env, "_candidate_roots",
        lambda: [empty, tmp_path / "no_home" / ".hermes" / "hermes-agent"],
    )
    assert hermes_env.find_hermes_root() is None


def test_env_override_ignored_when_no_sentinel(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("HERMES_AGENT_ROOT", str(empty))
    # A dir without run_agent.py must not be accepted.
    candidates = hermes_env._candidate_roots()
    assert empty.expanduser() in candidates
    # but it shouldn't resolve as the root
    if hermes_env.find_hermes_root() is not None:
        assert hermes_env.find_hermes_root() != str(empty.resolve())


def test_ensure_env_adds_to_syspath(tmp_path, monkeypatch):
    import sys
    root = _make_fake_hermes(tmp_path)
    monkeypatch.setenv("HERMES_AGENT_ROOT", str(root))
    resolved = hermes_env.ensure_hermes_env()
    assert resolved == str(root.resolve())
    assert str(root.resolve()) in sys.path


def test_ensure_env_idempotent(tmp_path, monkeypatch):
    root = _make_fake_hermes(tmp_path)
    monkeypatch.setenv("HERMES_AGENT_ROOT", str(root))
    first = hermes_env.ensure_hermes_env()
    second = hermes_env.ensure_hermes_env()
    assert first == second


def test_ensure_env_raises_when_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hermes_env, "_candidate_roots",
        lambda: [tmp_path / "nope"],
    )
    with pytest.raises(hermes_env.HermesNotFoundError) as exc:
        hermes_env.ensure_hermes_env()
    # The error must be actionable.
    msg = str(exc.value)
    assert "HERMES_AGENT_ROOT" in msg
    assert "run_agent.py" in msg


def test_get_hermes_home_falls_back(tmp_path, monkeypatch):
    # When Hermes can't be imported, get_hermes_home falls back to ~/.hermes.
    monkeypatch.setattr(hermes_env.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        hermes_env, "_candidate_roots",
        lambda: [tmp_path / "nope"],
    )
    home = hermes_env.get_hermes_home()
    assert home == tmp_path / ".hermes"
