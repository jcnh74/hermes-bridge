"""
Hermes environment discovery — single source of truth.

The bridge runs *alongside* a Hermes install: it imports Hermes' Python modules
(run_agent, hermes_cli, hermes_constants) at runtime by adding the Hermes code
root to sys.path. This module locates that root robustly and fails with a clear,
actionable message when it can't.

Discovery order (first hit wins):
  1. $HERMES_AGENT_ROOT            — explicit override (dir containing run_agent.py)
  2. ~/.hermes/hermes-agent        — the standard install location
  3. walking up from this file     — supports running from inside a Hermes checkout

Nothing here imports Hermes at module load time, so importing the bridge package
never fails just because Hermes isn't on the path yet.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_HERMES_ROOT: Optional[str] = None
_LOADED = False

# A directory is the Hermes code root if it contains this entrypoint.
_SENTINEL = "run_agent.py"


class HermesNotFoundError(RuntimeError):
    """Raised when the Hermes agent install cannot be located."""


def _candidate_roots() -> list[Path]:
    candidates: list[Path] = []

    env = os.getenv("HERMES_AGENT_ROOT")
    if env:
        candidates.append(Path(env).expanduser())

    candidates.append(Path.home() / ".hermes" / "hermes-agent")

    # Walk up from this file (covers running inside a Hermes checkout / monorepo).
    here = Path(__file__).resolve()
    candidates.extend(here.parents)

    return candidates


def find_hermes_root() -> Optional[str]:
    """Return the Hermes code root (dir containing run_agent.py), or None."""
    for p in _candidate_roots():
        try:
            if (p / _SENTINEL).exists():
                return str(p.resolve())
        except (OSError, PermissionError):
            continue
    return None


def ensure_hermes_env() -> str:
    """
    Locate Hermes and add it to sys.path (idempotent).

    Returns the resolved Hermes root path.
    Raises HermesNotFoundError with install guidance if not found.
    """
    global _HERMES_ROOT, _LOADED
    if _LOADED and _HERMES_ROOT:
        return _HERMES_ROOT

    root = find_hermes_root()
    if not root:
        raise HermesNotFoundError(
            "Could not find a Hermes Agent installation.\n\n"
            "The bridge needs Hermes installed to run agents. Looked in:\n"
            "  • $HERMES_AGENT_ROOT (not set)\n"
            "  • ~/.hermes/hermes-agent\n"
            "  • parent directories of the bridge package\n\n"
            "Fix it by either:\n"
            "  1. Installing Hermes Agent (https://hermes-agent.nousresearch.com), or\n"
            "  2. Pointing the bridge at an existing install:\n"
            "       export HERMES_AGENT_ROOT=/path/to/hermes-agent\n\n"
            "The directory must contain 'run_agent.py'."
        )

    if root not in sys.path:
        sys.path.insert(0, root)
    _HERMES_ROOT = root
    _LOADED = True
    return root


def get_hermes_home() -> Path:
    """
    Return the Hermes data/config home (~/.hermes, profile-aware).

    Delegates to Hermes' own hermes_constants once the env is set up; falls back
    to ~/.hermes if that import is unavailable for any reason.
    """
    try:
        ensure_hermes_env()
        from hermes_constants import get_hermes_home as _ghh  # type: ignore
        return _ghh()
    except Exception:
        return Path.home() / ".hermes"
