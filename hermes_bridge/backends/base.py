"""
Agent backend abstraction for the bridge.

The bridge's HTTP/SSE layer (server.py) talks to agents ONLY through the
``AgentBackend`` interface. Concrete backends decide how a turn is actually
executed:

  - ``HermesDirectBackend`` — imports Hermes' AIAgent and runs it in-process
    (lowest latency, preserves Hermes prompt caching). This wraps the existing
    ``AgentProxy`` implementation.
  - ``AcpBackend`` — drives an external agent over the Agent Client Protocol
    (JSON-RPC over stdio): spawns ``hermes acp`` / ``openclaw acp`` and speaks
    initialize -> session/new -> session/prompt. Any ACP-compliant agent
    (Hermes, OpenClaw, Claude Code, Codex, Gemini CLI, OpenCode) plugs in here.

A session is bound to exactly one backend, chosen by agent id at creation time.
The contract mirrors the methods server.py already depends on, so wiring a new
backend requires no route changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class AgentBackend(ABC):
    """Uniform contract every agent runtime must satisfy.

    Method signatures intentionally match the historical ``AgentProxy`` surface
    so ``server.py`` can hold an ``AgentBackend`` wherever it used to hold an
    ``AgentProxy``.
    """

    #: Stable identifier for the backend kind, e.g. "hermes-direct" | "acp".
    kind: str = "base"

    @abstractmethod
    def run_conversation(
        self,
        text: str,
        stream_delta_callback: Optional[Callable[[str], None]] = None,
        history: Optional[list] = None,
    ) -> dict:
        """Execute one turn.

        Returns a dict with at least ``final_response`` (str) and ``session_id``.
        On failure include an ``error`` key with a human-readable message and
        an empty ``final_response`` — never raise across this boundary.
        """
        raise NotImplementedError

    @abstractmethod
    def switch_model(self, new_model: str, preserve_history: bool = True) -> dict:
        """Change the model for this session.

        Returns a dict with ``session_id``, ``previous_model``, ``new_model``,
        and ``preserved_messages``.
        """
        raise NotImplementedError

    @abstractmethod
    def get_message_history(self) -> list[dict]:
        """Return the tracked message history (list of {role, content})."""
        raise NotImplementedError

    @abstractmethod
    def cleanup(self) -> None:
        """Release any resources (subprocesses, agent instances)."""
        raise NotImplementedError
