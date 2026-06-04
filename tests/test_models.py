"""Tests for Pydantic models — validation and defaults."""

import pytest
from pydantic import ValidationError

from hermes_bridge import models


def test_message_send_requires_nonempty_text():
    with pytest.raises(ValidationError):
        models.MessageSend(text="")


def test_message_send_defaults_stream_true():
    m = models.MessageSend(text="hi")
    assert m.stream is True


def test_session_create_defaults():
    s = models.SessionCreate()
    assert s.agent_id == "main"
    assert s.platform == "bridge"


def test_agent_create_requires_name():
    with pytest.raises(ValidationError):
        models.AgentCreate(name="")


def test_agent_create_name_max_length():
    with pytest.raises(ValidationError):
        models.AgentCreate(name="x" * 65)


def test_agent_create_defaults():
    a = models.AgentCreate(name="Bot")
    assert a.emoji == "🤖"
    assert a.model == ""


def test_health_response_defaults():
    h = models.HealthResponse()
    assert h.status == "ok"
    assert h.agents_available == 0


def test_skill_detail_defaults():
    s = models.SkillDetail(name="x", description="y")
    assert s.tags == []
    assert s.linked_files == {}
    assert s.category == ""


def test_session_import_roundtrip():
    payload = {
        "session_key": "s1",
        "agent_id": "main",
        "messages": [
            {"role": "user", "content": "hi", "created_at": 1.0},
            {"role": "assistant", "content": "yo", "created_at": 2.0},
        ],
    }
    imp = models.SessionImport(**payload)
    assert len(imp.messages) == 2
    assert imp.messages[0].role == "user"


def test_pairing_code_defaults():
    p = models.PairingCode(code="ABC", url="http://x")
    assert p.expires_in == 300
