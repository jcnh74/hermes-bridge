"""Agent backends for the bridge.

``AgentBackend`` is the uniform contract; concrete backends live alongside it.
Use :func:`make_backend` to construct the right one for a given agent id.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import AgentBackend
from .acp import AcpBackend

logger = logging.getLogger(__name__)


# Agent-id prefixes route to a backend. Everything without a known prefix
# defaults to the in-process Hermes backend (backwards compatible).
_OPENCLAW_PREFIXES = ("openclaw:", "oc:")
_OPENCLAW_ACP_PREFIXES = ("openclaw-acp:",)
_ACP_PREFIXES = ("acp:",)


def _hermes_acp_server() -> list[str]:
    """Command that launches Hermes as an ACP server."""
    return ["hermes", "acp"]


def _openclaw_acp_server() -> list[str]:
    """Command that launches OpenClaw as an ACP server.

    NOTE: OpenClaw's ACP bridge is backed by its Gateway. If the gateway is not
    running, prefer the local single-turn path (openclaw agent --local) via a
    dedicated backend. For now this targets the ACP bridge directly.
    """
    return ["openclaw", "acp"]


def make_backend(
    agent_id: str,
    *,
    model: Optional[str] = None,
    platform: str = "bridge",
    cwd: Optional[str] = None,
) -> AgentBackend:
    """Construct the backend for ``agent_id``.

    Routing:
      - ``openclaw:<name>`` / ``oc:<name>`` -> ACP backend driving ``openclaw acp``
      - ``acp:<name>``                      -> ACP backend driving ``hermes acp``
      - anything else                       -> in-process Hermes (HermesDirectBackend)
    """
    lid = agent_id.lower()

    # OpenClaw via ACP bridge (needs gateway running) — explicit opt-in.
    if lid.startswith(_OPENCLAW_ACP_PREFIXES):
        name = agent_id.split(":", 1)[1] or "main"
        return AcpBackend(
            _openclaw_acp_server(),
            agent_id=name,
            model=model,
            cwd=cwd,
        )

    # OpenClaw gateway-free (openclaw agent --local) — the default OpenClaw path.
    if lid.startswith(_OPENCLAW_PREFIXES):
        from .openclaw_local import OpenClawLocalBackend

        name = agent_id.split(":", 1)[1] or "main"
        return OpenClawLocalBackend(agent_id=name, model=model)

    if lid.startswith(_ACP_PREFIXES):
        name = agent_id.split(":", 1)[1] or "main"
        return AcpBackend(
            _hermes_acp_server(),
            agent_id=name,
            model=model,
            cwd=cwd,
        )

    # Default: in-process Hermes (import here to avoid a hard dependency when
    # only ACP backends are used, and to keep Hermes import cost lazy).
    from .hermes_direct import HermesDirectBackend

    return HermesDirectBackend(agent_id=agent_id, model=model, platform=platform)


__all__ = ["AgentBackend", "AcpBackend", "make_backend"]
