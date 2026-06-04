from pydantic import BaseModel, Field
from typing import Optional


class AgentInfo(BaseModel):
    """A Hermes agent exposed via the bridge API."""
    id: str
    name: str
    model: str = ""
    description: str = ""
    emoji: str = ""
    status: str = "active"


class SessionCreate(BaseModel):
    """Request to create a new session."""
    agent_id: str = Field(default="main", description="Agent ID (e.g. 'main', 'maxbot')")
    platform: str = Field(default="bridge", description="Platform identifier for the session")


class SessionInfo(BaseModel):
    """Information about an active session."""
    key: str
    agent_id: str
    created_at: float
    message_count: int = 0
    status: str = "active"


class MessageSend(BaseModel):
    """Request to send a message to an agent."""
    text: str = Field(..., min_length=1, description="Message text")
    stream: bool = Field(default=True, description="If true, stream response via SSE")


class MessageInfo(BaseModel):
    """A single message in a session."""
    role: str  # "user" or "assistant"
    content: str
    created_at: float


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    agents_available: int = 0
    sessions_active: int = 0
    features: dict = {}


class AgentCreate(BaseModel):
    """Request to create a new Hermes agent."""
    name: str = Field(..., min_length=1, max_length=64, description="Agent name")
    emoji: str = Field(default="🤖", max_length=32, description="Icon name")
    model: str = Field(default="", description="Model override (empty = use default)")
    description: str = Field(default="", max_length=500, description="Short description")


class AgentUpdate(BaseModel):
    """Request to update an existing Hermes agent."""
    name: str = Field(default="", max_length=64, description="New agent name (empty = no change)")
    emoji: str = Field(default="", max_length=32, description="New icon name (empty = no change)")
    model: str = Field(default="", description="New model override (empty = no change)")
    description: str = Field(default="", max_length=500, description="New description (empty = no change)")


class SessionExportMessage(BaseModel):
    """A single message for bulk import into a session."""
    role: str
    content: str
    created_at: float


class SessionImport(BaseModel):
    """Request to bulk-import messages into a session."""
    session_key: str
    agent_id: str = "main"
    messages: list[SessionExportMessage]


class SkillInfo(BaseModel):
    """A skill available via the Hermes skills system."""
    name: str
    description: str
    category: str = ""


class SkillDetail(BaseModel):
    """Full skill content with frontmatter metadata."""
    name: str
    description: str
    category: str = ""
    tags: list[str] = []
    content: str = ""
    linked_files: dict[str, list[str]] = {}


class AgentSkillAttach(BaseModel):
    """Request to attach a skill to an agent."""
    skill_name: str = Field(..., description="Name of the skill to attach")


class AgentSkillResponse(BaseModel):
    """A skill attached to an agent."""
    skill_name: str
    attached_at: float = 0.0


class PairingCode(BaseModel):
    """Pairing code for the Agentfy app to connect."""
    code: str
    url: str
    expires_in: int = 300
