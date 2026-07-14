"""Tests for the agent backend abstraction and routing factory.

These are pure-routing unit tests — they do NOT spawn agents or import Hermes.
"""

import pytest

from hermes_bridge.backends import make_backend, AcpBackend
from hermes_bridge.backends.base import AgentBackend


def test_openclaw_prefix_routes_to_acp_openclaw():
    be = make_backend("openclaw:main")
    assert isinstance(be, AcpBackend)
    assert be.server_command == ["openclaw", "acp"]
    assert be.agent_id == "main"


def test_oc_shortprefix_routes_to_acp_openclaw():
    be = make_backend("oc:director")
    assert isinstance(be, AcpBackend)
    assert be.server_command == ["openclaw", "acp"]
    assert be.agent_id == "director"


def test_acp_prefix_routes_to_hermes_acp():
    be = make_backend("acp:main")
    assert isinstance(be, AcpBackend)
    assert be.server_command == ["hermes", "acp"]
    assert be.agent_id == "main"


def test_acp_backend_satisfies_contract():
    be = make_backend("acp:main")
    assert isinstance(be, AgentBackend)
    for method in ("run_conversation", "switch_model", "get_message_history", "cleanup"):
        assert callable(getattr(be, method))


def test_empty_name_defaults_to_main():
    be = make_backend("openclaw:")
    assert be.agent_id == "main"


def test_model_override_passthrough():
    be = make_backend("acp:main", model="anthropic/claude-sonnet-4-5-20250929")
    assert be.override_model == "anthropic/claude-sonnet-4-5-20250929"


def test_acp_backend_kind():
    be = make_backend("acp:main")
    assert be.kind == "acp"


def test_default_agent_id_is_not_acp():
    # A plain agent id must NOT route to ACP — it should attempt the in-process
    # Hermes backend. We can't construct that without Hermes importable, so just
    # assert it does not return an AcpBackend for a non-prefixed id by checking
    # the routing decision via prefix rules.
    from hermes_bridge.backends import _OPENCLAW_PREFIXES, _ACP_PREFIXES
    aid = "main"
    assert not aid.lower().startswith(_OPENCLAW_PREFIXES)
    assert not aid.lower().startswith(_ACP_PREFIXES)
