"""
In-process Hermes backend.

Thin adapter that wraps the existing :class:`AgentProxy` (which imports Hermes'
AIAgent and runs it in-process) so it satisfies the :class:`AgentBackend`
contract. This is the lowest-latency path and preserves Hermes prompt caching;
it is the default backend for any agent id without an ACP routing prefix.

Wrapping rather than modifying ``agent_proxy.py`` keeps the proven code path
byte-for-byte identical — the ABC is satisfied by delegation.
"""

from __future__ import annotations

from typing import Callable, Optional

from .base import AgentBackend
from ..agent_proxy import AgentProxy


class HermesDirectBackend(AgentBackend):
    kind = "hermes-direct"

    def __init__(
        self,
        agent_id: str,
        *,
        model: Optional[str] = None,
        platform: str = "bridge",
    ):
        self._proxy = AgentProxy(agent_id=agent_id, model=model, platform=platform)

    @property
    def override_model(self):
        return self._proxy.override_model

    def run_conversation(
        self,
        text: str,
        stream_delta_callback: Optional[Callable[[str], None]] = None,
        history: Optional[list] = None,
    ) -> dict:
        return self._proxy.run_conversation(
            text, stream_delta_callback=stream_delta_callback, history=history
        )

    def switch_model(self, new_model: str, preserve_history: bool = True) -> dict:
        return self._proxy.switch_model(new_model, preserve_history=preserve_history)

    def get_message_history(self) -> list[dict]:
        return self._proxy.get_message_history()

    def cleanup(self) -> None:
        self._proxy.cleanup()
