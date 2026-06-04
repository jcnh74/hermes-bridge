"""
FastAPI server for the Hermes Bridge API.

Exposes Hermes agents via REST endpoints with SSE streaming for responses.
No OpenClaw protocol needed — plain HTTP that any client can use.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .models import (
    AgentCreate,
    AgentUpdate,
    AgentInfo,
    HealthResponse,
    MessageInfo,
    MessageSend,
    SessionCreate,
    SessionImport,
    SessionInfo,
)
from .agent_proxy import AgentProxy, get_agent_list, get_available_models, resolve_runtime, create_custom_agent, update_custom_agent
from .persistence import store_message, get_messages as get_persisted_messages, get_all_session_keys, get_session_summary
from .skills import (
    list_skills as _list_skills,
    get_skill as _get_skill,
    get_skill_linked_file as _get_skill_linked_file,
    attach_skill_to_agent as _attach_skill_to_agent,
    detach_skill_from_agent as _detach_skill_from_agent,
    get_agent_skills as _get_agent_skills,
)

logger = logging.getLogger("hermes_bridge")

# ── In-memory session store ─────────────────────────────────────────────

# session_key -> { "proxy": AgentProxy, "info": SessionInfo }
_sessions: dict[str, dict] = {}

# agent_id -> session_key (one active session per agent by default)
_active_session_per_agent: dict[str, str] = {}


def _make_session_key(agent_id: str) -> str:
    return f"bridge:{agent_id}:{uuid.uuid4().hex[:8]}"


def _get_or_create_session(agent_id: str, platform: str = "bridge") -> dict:
    """Get the active session for an agent, or create a new one."""
    existing_key = _active_session_per_agent.get(agent_id)
    if existing_key and existing_key in _sessions:
        return _sessions[existing_key]

    # Read the saved model for this agent from config (if any)
    saved_model = ""
    try:
        from .agent_proxy import load_config
        config = load_config()
        agent_list = config.get("agents", {}).get("list", [])
        for a in agent_list:
            existing_id = a.get("id") or a.get("agentId")
            if existing_id == agent_id:
                saved_model = a.get("model", "")
                break
    except Exception:
        pass

    key = _make_session_key(agent_id)
    proxy = AgentProxy(agent_id=agent_id, model=saved_model, platform=platform)
    info = SessionInfo(
        key=key,
        agent_id=agent_id,
        created_at=time.time(),
        status="active",
    )
    _sessions[key] = {"proxy": proxy, "info": info}
    _active_session_per_agent[agent_id] = key
    return _sessions[key]


# ── Lifespan ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    logger.info("Hermes Bridge API starting up...")

    # Verify Hermes environment is loadable
    try:
        from .agent_proxy import _ensure_hermes_env
        _ensure_hermes_env()
        rt = resolve_runtime()
        logger.info("Hermes runtime resolved: provider=%s, model=%s",
                     rt.get("provider"), os.getenv("HERMES_DEFAULT_MODEL", "default"))
    except Exception as e:
        logger.warning("Hermes runtime NOT available: %s. Bridge will start but agents won't work.", e)

    yield

    # Shutdown: clean up all agents
    for key, session in _sessions.items():
        try:
            session["proxy"].cleanup()
        except Exception:
            pass
    _sessions.clear()
    _active_session_per_agent.clear()
    logger.info("Hermes Bridge API shut down.")


# ── App factory ─────────────────────────────────────────────────────────

app = FastAPI(
    title="Hermes Bridge API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow any origin (mobile apps, web clients, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ──────────────────────────────────────────────────────────────

def _load_env_file():
    """Load ~/.hermes/.env into os.environ using dotenv-style parsing."""
    import re as _re
    env_path = os.path.expanduser("~/.hermes/.env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                # Strip quotes
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


# ── Media file handling ────────────────────────────────────────────────

# Matches MEDIA:<path> tags
_MEDIA_TAG_RE = re.compile(r'''[`\"']?MEDIA:\s*\S+[`\"']?''')

# Matches bare local file paths that look like generated media files
# (absolute paths ending in image/video/doc extensions)
_LOCAL_MEDIA_EXTS = (
    '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.mp4', '.mov', '.avi', '.mkv', '.webm',
    '.ogg', '.opus', '.mp3', '.wav', '.m4a',
    '.pdf', '.zip', '.txt', '.csv', '.epub',
)
_LOCAL_MEDIA_RE = re.compile(
    r"(?<![:\w`\"'/])"
    r"(/[^\s<>\"'`]+\.(?:jpg|jpeg|png|gif|webp|mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|pdf|zip|txt|csv|epub))"
    r"(?=[\s,;:)\]}<]|$)"
)

# file_id -> {name, mime_type, size, file_path}
_served_files: dict[str, dict] = {}

# MIME type map
_MIME_MAP = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.webp': 'image/webp',
    '.mp4': 'video/mp4', '.mov': 'video/quicktime',
    '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav', '.m4a': 'audio/mp4',
    '.pdf': 'application/pdf', '.zip': 'application/zip',
    '.txt': 'text/plain', '.csv': 'text/csv',
    '.epub': 'application/epub+zip',
}


def _register_file(path: str, media_list: list[dict]):
    """Register a file for download and append metadata to media_list."""
    if not os.path.isfile(path):
        return
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME_MAP.get(ext, 'application/octet-stream')
    size = os.path.getsize(path)
    file_id = uuid.uuid4().hex[:12]
    file_info = {
        "name": os.path.basename(path),
        "mime_type": mime,
        "size": size,
        "file_path": path,
    }
    _served_files[file_id] = file_info
    media_list.append({**file_info, "file_id": file_id})


def _clean_path(p: str) -> str:
    """Strip wrapping quotes/parens from a path string."""
    p = p.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in '`"\'(':
        p = p[1:-1]
    return os.path.expanduser(p.strip())


def _extract_media(text: str) -> tuple[list[dict], str]:
    """Extract MEDIA:<path> tags and bare local file paths from response text.

    Returns (media_metadata, cleaned_text):
      - media_metadata: list of {name, mime_type, size, file_path, file_id}
      - cleaned_text: original text with MEDIA: tags and file paths removed
    """
    media = []
    # Handle None or non-string input gracefully
    if not text or not isinstance(text, str):
        return media, (text or "").strip()
    cleaned = text

    # 1. Extract MEDIA: tags (explicit file delivery)
    for match in _MEDIA_TAG_RE.finditer(text):
        full = match.group(0).strip()
        if 'MEDIA:' in full:
            path = full[full.index('MEDIA:') + 6:].strip()
        else:
            path = full
        path = _clean_path(path)
        if not path:
            continue
        _register_file(path, media)

    # 2. Extract bare local file paths (fallback for agents that don't use MEDIA:)
    for match in _LOCAL_MEDIA_RE.finditer(text):
        path = _clean_path(match.group(0))
        if not path:
            continue
        # Don't double-register if a MEDIA: tag already caught this file
        already_registered = any(
            os.path.abspath(m.get("file_path", "")) == os.path.abspath(path)
            for m in media
        )
        if not already_registered and os.path.isfile(path):
            _register_file(path, media)

    # Remove MEDIA: tags from cleaned text
    if media:
        # Check if we found any via MEDIA: tags
        if _MEDIA_TAG_RE.search(text):
            cleaned = _MEDIA_TAG_RE.sub('', cleaned)
        # Also try to remove bare local file paths from cleaned text
        # but be conservative — only remove paths that matched and are real files
        # This is done below per-file

    # Build the cleaned text: remove matched MEDIA tags, keep bare paths in text
    # (the app client will render them as markdown)
    cleaned_no_media = _MEDIA_TAG_RE.sub('', text) if _MEDIA_TAG_RE.search(text) else text
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned_no_media).strip()

    return media, cleaned


@app.get("/api/v1/files/{file_id}")
async def download_file(file_id: str):
    """Download a file that was referenced in an agent response."""
    file_info = _served_files.get(file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")
    file_path = file_info.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File no longer available")
    return FileResponse(
        path=file_path,
        media_type=file_info.get("mime_type", "application/octet-stream"),
        filename=file_info.get("name", "file"),
    )


# ── Routes ──────────────────────────────────────────────────────────────

@app.get("/api/v1/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    _load_env_file()

    # Check which TTS providers are available
    elevenlabs_available = bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())
    openai_tts_available = bool(os.environ.get("OPENAI_API_KEY", "").strip())

    return HealthResponse(
        status="ok",
        version="0.1.0",
        agents_available=len(get_agent_list()),
        sessions_active=len(_sessions),
        features={
            "tts": {
                "openai": openai_tts_available,
                "elevenlabs": elevenlabs_available,
            },
        },
    )


@app.get("/api/v1/tts/status")
async def tts_status():
    """Check TTS provider credit status.

    Returns remaining character counts for each configured TTS provider.
    For ElevenLabs, calls the user/subscription API to get remaining credits.
    """
    _load_env_file()

    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    result = {
        "elevenlabs": {
            "configured": bool(elevenlabs_key),
            "available": False,
            "remaining": 0,
            "limit": 0,
            "status": "unknown",
        },
        "openai": {
            "configured": bool(openai_key),
            "available": bool(openai_key),
            "remaining": None,
            "limit": None,
            "status": "available" if openai_key else "unconfigured",
        },
    }

    if elevenlabs_key:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.elevenlabs.io/v1/user/subscription",
                    headers={"xi-api-key": elevenlabs_key},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    char_count = data.get("character_count", 0)
                    char_limit = data.get("character_limit", 0)
                    sub_status = data.get("status", "unknown")
                    has_credits = char_count < char_limit or char_limit == 0
                    result["elevenlabs"] = {
                        "configured": True,
                        "available": has_credits,
                        "remaining": max(0, char_limit - char_count),
                        "limit": char_limit,
                        "used": char_count,
                        "status": sub_status,
                    }
                else:
                    body = resp.text
                    is_auth_error = resp.status_code == 401
                    is_quota = "quota" in body.lower() or "credit" in body.lower()
                    result["elevenlabs"] = {
                        "configured": True,
                        "available": not is_auth_error,
                        "remaining": 0,
                        "limit": 0,
                        "status": "unauthorized" if is_auth_error else "error",
                        "error": f"HTTP {resp.status_code}" if not is_quota else "out_of_credits",
                    }
        except Exception as e:
            logger.warning("Failed to query ElevenLabs subscription: %s", e)
            result["elevenlabs"] = {
                "configured": True,
                "available": True,
                "remaining": None,
                "limit": None,
                "status": "unknown",
                "error": str(e),
            }

    return result


@app.post("/api/v1/tts/transcribe")
async def tts_transcribe(request: Request):
    """Proxy Whisper transcription through the bridge.

    The app sends an audio file, the bridge calls OpenAI Whisper
    so the API key stays server-side. Returns the transcribed text.
    """
    _load_env_file()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return JSONResponse({"error": "OpenAI not configured for transcription"}, status_code=503)

    import httpx

    form = await request.form()
    audio_file = form.get("file")
    if not audio_file:
        return JSONResponse({"error": "No audio file provided"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_file.filename, audio_file.file.read(), audio_file.content_type or "audio/m4a")},
                data={"model": "whisper-1", "response_format": "text"},
            )
            if resp.status_code == 200:
                return PlainTextResponse(resp.text)
            return JSONResponse({"error": f"Whisper {resp.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/v1/tts/speak")
async def tts_speak(request: Request):
    """Proxy TTS synthesis through the bridge.

    The app sends text and voice params, the bridge calls ElevenLabs
    (or OpenAI) so the API key stays server-side.
    """
    _load_env_file()
    body = await request.json()
    text = body.get("text", "")
    provider = body.get("provider", "elevenlabs")
    voice_id = body.get("voiceId", "21m00Tcm4TlvDq8ikWAM")  # Rachel default

    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    if provider == "elevenlabs":
        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            return JSONResponse({"error": "ElevenLabs not configured"}, status_code=503)

        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
                    headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                    json={
                        "text": text,
                        "model_id": "eleven_turbo_v2",
                        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                    },
                )
                if resp.status_code == 200:
                    # Return audio bytes as response
                    from fastapi.responses import Response
                    return Response(content=resp.content, media_type="audio/mpeg")
                elif resp.status_code == 401:
                    return JSONResponse({"error": "elevenlabs_unauthorized"}, status_code=402)
                elif resp.status_code == 402:
                    return JSONResponse({"error": "elevenlabs_out_of_credits"}, status_code=402)
                else:
                    body_text = resp.text
                    if "quota" in body_text.lower() or "credit" in body_text.lower():
                        return JSONResponse({"error": "elevenlabs_out_of_credits"}, status_code=402)
                    return JSONResponse({"error": f"ElevenLabs {resp.status_code}"}, status_code=502)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return JSONResponse({"error": "OpenAI not configured"}, status_code=503)

        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": "tts-1", "input": text, "voice": voice_id, "response_format": "mp3"},
                )
                if resp.status_code == 200:
                    from fastapi.responses import Response
                    return Response(content=resp.content, media_type="audio/mpeg")
                return JSONResponse({"error": f"OpenAI TTS {resp.status_code}"}, status_code=502)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"error": f"Unknown TTS provider: {provider}"}, status_code=400)


@app.get("/api/v1/agents")
async def list_agents():
    """List all available Hermes agents."""
    agents = get_agent_list()
    return {"agents": agents, "count": len(agents)}


@app.get("/api/v1/models")
async def list_models():
    """List all models available via the current Hermes provider.

    Reads from Hermes' built-in _PROVIDER_MODELS catalog so new models
    show up automatically in the app when they're added to Hermes.
    """
    models = get_available_models()
    return {"models": models, "count": len(models)}


@app.post("/api/v1/agents")
async def create_agent(body: "AgentCreate"):
    """Create a new Hermes agent with name, emoji, and optional model.

    The agent is saved to the Hermes config so it persists across restarts
    and is immediately available for conversations.
    """
    from .agent_proxy import create_custom_agent
    agent_id = create_custom_agent(
        name=body.name,
        emoji=body.emoji,
        model=body.model or None,
        description=body.description or None,
    )
    return {
        "id": agent_id,
        "name": body.name,
        "emoji": body.emoji,
        "model": body.model or "",
        "status": "created",
    }


@app.patch("/api/v1/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate):
    """Update an existing Hermes agent's name, emoji, model, or description.

    Only non-empty fields in the request body are applied.

    When the model changes, the active session (if any) is re-created with
    the new model, preserving all conversation history so context is handed
    off seamlessly between models.
    """
    from .agent_proxy import update_custom_agent

    try:
        result = update_custom_agent(
            agent_id=agent_id,
            name=body.name or None,
            emoji=body.emoji or None,
            model=body.model or None,
            description=body.description or None,
        )

        # If model changed and there's an active session, hand off context
        if body.model and agent_id in _active_session_per_agent:
            existing_key = _active_session_per_agent[agent_id]
            if existing_key in _sessions:
                proxy: AgentProxy = _sessions[existing_key]["proxy"]
                switch_result = proxy.switch_model(body.model, preserve_history=True)
                result["model_switch"] = switch_result

        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/v1/pairing")
async def get_pairing_code():
    """Generate a pairing code for the Agentfy app.

    Returns a base64-encoded setup code that the app can scan/paste
    to auto-connect to this Hermes bridge.
    """
    import base64, json, time

    payload = {
        "url": f"http://{_get_local_ip()}:8765",
        "bootstrapToken": _generate_bootstrap_token(),
        "instanceName": "Hermes",
        "agents": get_agent_list(),
        "created_at": time.time(),
    }

    code = base64.b64encode(json.dumps(payload).encode()).decode()

    return {
        "code": code,
        "url": payload["url"],
        "agents": payload["agents"],
        "expires_in": 300,
    }


# ── Skills Routes ─────────────────────────────────────────────────────


@app.get("/api/v1/skills")
async def list_skills(search: str = "", category: str = ""):
    """List all available Hermes skills, optionally filtered by search query or category.

    Returns skills from ~/.hermes/skills/ with name, description, and category.
    """
    skills = _list_skills()

    # Filter by category if specified
    if category:
        skills = [s for s in skills if s.get("category") == category]

    # Filter by search query if specified
    if search:
        q = search.lower()
        skills = [
            s for s in skills
            if q in s["name"].lower() or q in s.get("description", "").lower()
        ]

    # Extract unique categories
    categories = sorted({s.get("category") for s in skills if s.get("category")})

    return {"skills": skills, "categories": categories, "count": len(skills)}


@app.get("/api/v1/skills/{skill_name}")
async def get_skill(skill_name: str):
    """Get full details for a specific skill, including content and linked files.

    The content field contains the full SKILL.md, and linked_files lists
    any references/, templates/, scripts/, or assets/ files available.
    """
    skill = _get_skill(skill_name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")
    return skill


@app.get("/api/v1/skills/{skill_name}/files/{file_path:path}")
async def get_skill_file(skill_name: str, file_path: str):
    """Get content of a linked file within a skill.

    file_path is like 'references/api.md' or 'templates/template.yaml'.
    """
    content = _get_skill_linked_file(skill_name, file_path)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail=f"File '{file_path}' not found in skill '{skill_name}'",
        )
    return {"content": content}


@app.get("/api/v1/agents/{agent_id}/skills")
async def list_agent_skills(agent_id: str):
    """List all skills attached to an agent.

    Returns skill metadata including name, description, category, tags,
    and when it was attached.
    """
    skills = _get_agent_skills(agent_id)
    return {"agent_id": agent_id, "skills": skills, "count": len(skills)}


@app.post("/api/v1/agents/{agent_id}/skills")
async def attach_skill(agent_id: str, body: dict):
    """Attach a skill to an agent.

    Body: { "skill_name": "my-skill" }
    The skill must exist in ~/.hermes/skills/.
    """
    from .models import AgentSkillAttach

    attach = AgentSkillAttach(**body)

    # Verify the skill exists
    skill = _get_skill(attach.skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{attach.skill_name}' not found in Hermes skills",
        )

    record = _attach_skill_to_agent(agent_id, attach.skill_name)
    return {
        "agent_id": agent_id,
        "skill_name": record["skill_name"],
        "attached_at": record["attached_at"],
        "status": "attached",
    }


@app.delete("/api/v1/agents/{agent_id}/skills/{skill_name}")
async def detach_skill(agent_id: str, skill_name: str):
    """Detach a skill from an agent.

    Removes the skill attachment without deleting the actual skill file.
    """
    removed = _detach_skill_from_agent(agent_id, skill_name)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{skill_name}' not attached to agent '{agent_id}'",
        )
    return {"agent_id": agent_id, "skill_name": skill_name, "status": "detached"}


def _get_local_ip() -> str:
    """Get the local network IP address."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── Settings / API Keys ──────────────────────────────────────────────

# Map friendly provider names to their .env variable names
PROVIDER_ENV_MAP: dict[str, tuple[str, str]] = {
    "deepseek": ("DEEPSEEK_API_KEY", "DeepSeek"),
    "anthropic": ("ANTHROPIC_API_KEY", "Anthropic (Claude)"),
    "openai": ("OPENAI_API_KEY", "OpenAI"),
    "google": ("GOOGLE_API_KEY", "Google (Gemini)"),
    "minimax": ("MINIMAX_API_KEY", "MiniMax"),
    "openrouter": ("OPENROUTER_API_KEY", "OpenRouter"),
}

PROVIDER_BY_ENV: dict[str, str] = {v[0]: k for k, v in PROVIDER_ENV_MAP.items()}


def _read_env_keys() -> dict[str, str]:
    """Read all known API keys from the .env file.
    Returns dict of provider_name -> "set"|"missing".
    """
    from pathlib import Path
    env_path = Path.home() / ".hermes" / ".env"
    result: dict[str, str] = {}
    # Always include all known providers
    for provider, (env_var, label) in PROVIDER_ENV_MAP.items():
        result[provider] = "missing"

    if env_path.is_file():
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key in PROVIDER_BY_ENV:
                        result[PROVIDER_BY_ENV[key]] = "set" if val else "missing"
        except Exception:
            pass
    return result


def _write_env_key(env_var: str, api_key: str) -> None:
    """Write or update a single API key in ~/.hermes/.env.

    If the env var already exists, its value is replaced in-place.
    If not, it's appended to the end.
    """
    from pathlib import Path
    env_path = Path.home() / ".hermes" / ".env"
    lines: list[str] = []
    found = False
    if env_path.is_file():
        with open(env_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{env_var}="):
                    lines.append(f"{env_var}={api_key}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{env_var}={api_key}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)
    # Also update the running process's environment so it takes effect immediately
    os.environ[env_var] = api_key
    logger.info("Updated %s in %s", env_var, env_path)


@app.get("/api/v1/settings/keys")
async def list_api_keys():
    """List all configured API key providers and their status."""
    keys = _read_env_keys()
    return {
        "providers": [
            {
                "id": provider,
                "label": PROVIDER_ENV_MAP[provider][1],
                "status": status,
            }
            for provider, status in keys.items()
        ]
    }


@app.post("/api/v1/settings/keys")
async def set_api_key(body: dict):
    """Set or update an API key for a provider.

    Body: { "provider": "google", "apiKey": "AIz..." }
    """
    provider = body.get("provider", "")
    api_key = body.get("apiKey", "")

    if provider not in PROVIDER_ENV_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}'. Valid: {', '.join(PROVIDER_ENV_MAP.keys())}")
    if not api_key or not api_key.strip():
        raise HTTPException(status_code=400, detail="apiKey must not be empty")

    env_var, label = PROVIDER_ENV_MAP[provider]
    _write_env_key(env_var, api_key.strip())

    return {
        "provider": provider,
        "label": label,
        "status": "updated",
    }


def _generate_bootstrap_token() -> str:
    """Generate a random bootstrap token for pairing."""
    import secrets
    return secrets.token_hex(16)


@app.post("/api/v1/sessions", response_model=SessionInfo)
async def create_session(body: SessionCreate):
    """Create a new session with an agent.

    Returns session info including the session key for subsequent requests.
    """
    session = _get_or_create_session(body.agent_id, body.platform)
    info = session["info"]

    # If the session was just created, message_count stays 0
    return SessionInfo(
        key=info.key,
        agent_id=info.agent_id,
        created_at=info.created_at,
        message_count=info.message_count,
        status=info.status,
    )


@app.get("/api/v1/agents/{agent_id}/sessions")
async def list_agent_sessions(agent_id: str):
    """List all persisted sessions for an agent.

    Returns session keys and summaries so the app can sync history
    after a reinstall.
    """
    session_keys = get_all_session_keys(agent_id)
    sessions = []
    for key in session_keys:
        summary = get_session_summary(key, agent_id)
        if summary:
            sessions.append(summary)
    return {"agent_id": agent_id, "sessions": sessions, "count": len(sessions)}


@app.post("/api/v1/sessions/import")
async def import_session_messages(body: SessionImport):
    """Bulk-import messages into a session (for sync from old installs).

    Accepts a list of messages with role, content, and created_at timestamps.
    Messages are deduplicated by (session_key, role, content[:40]).
    Returns the count of messages actually imported.
    """
    from .persistence import store_message as _store

    imported = 0
    for msg in body.messages:
        try:
            _store(body.session_key, body.agent_id, msg.role, msg.content)
            imported += 1
        except Exception:
            pass

    return {
        "session_key": body.session_key,
        "agent_id": body.agent_id,
        "imported": imported,
        "total": len(body.messages),
    }


@app.get("/api/v1/sessions/{session_key}/messages")
async def get_messages(session_key: str):
    """Get message history for a session.

    Returns messages from both persistent storage (survives restarts)
    and the current in-memory session state.
    """
    # Try persisted messages first
    persisted = get_persisted_messages(session_key)
    if persisted:
        return {"session_key": session_key, "messages": persisted, "count": len(persisted)}

    # Fall back to in-memory session (existing sessions before persistence was added)
    session = _sessions.get(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    proxy: AgentProxy = session["proxy"]
    history = proxy.get_message_history()

    messages = [
        MessageInfo(role=m["role"], content=m["content"], created_at=time.time())
        for m in history
    ]

    return {"session_key": session_key, "messages": messages, "count": len(messages)}


@app.post("/api/v1/sessions/{session_key}/messages")
async def send_message(session_key: str, body: MessageSend, request: Request):
    """Send a message to the agent in a session.

    If stream=true (default), the response is returned as an SSE stream.
    If stream=false, the complete response is returned as JSON.
    """
    session = _sessions.get(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    proxy: AgentProxy = session["proxy"]
    info = session["info"]

    if body.stream:
        return await _stream_response(proxy, body.text, info, request)
    else:
        return await _json_response(proxy, body.text, info)


async def _stream_response(proxy: AgentProxy, text: str, info: SessionInfo, request: Request):
    """Stream the agent response via SSE."""
    import queue as thr_queue
    stream_queue: thr_queue.Queue = thr_queue.Queue()
    done_event = threading.Event()
    final_result = {}

    def on_delta(delta: str):
        """Called by the agent for each text delta."""
        stream_queue.put(delta)

    # Run agent in a thread — AIAgent.run_conversation is synchronous
    def _run():
        nonlocal final_result
        try:
            result = proxy.run_conversation(text, stream_delta_callback=on_delta)
            final_result = result
        except Exception as e:
            final_result = {"error": str(e)}
        finally:
            done_event.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    async def event_generator():
        # Send the user message as a metadata event first
        yield {
            "event": "meta",
            "data": json.dumps({"session_key": info.key, "agent_id": info.agent_id}),
        }

        # Stream deltas as they arrive
        sent_first = False
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            try:
                delta = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: stream_queue.get(timeout=0.5)
                )
            except Exception:
                if done_event.is_set() and stream_queue.empty():
                    break
                continue

            if delta:
                if not sent_first:
                    sent_first = True
                    # Update message count on first token
                    info.message_count += 1

                yield {
                    "event": "delta",
                    "data": delta,
                }

        # Send done event with the final result summary
        yield {
            "event": "done",
            "data": json.dumps({
                "has_response": bool(final_result.get("final_response")),
                "has_error": "error" in final_result,
                "session_key": info.key,
            }),
        }

    return EventSourceResponse(event_generator())


async def _json_response(proxy: AgentProxy, text: str, info: SessionInfo):
    """Get the complete response without streaming."""
    def _run():
        return proxy.run_conversation(text)

    result = await asyncio.get_event_loop().run_in_executor(None, _run)

    info.message_count += 1

    # Helper: classify model errors into user-friendly messages
    def _classify_error(error_str: str) -> tuple[str, str]:
        """Returns (error_type, user_friendly_message)."""
        el = error_str.lower()
        if any(x in el for x in ["402", "credit", "billing", "quota", "payment"]):
            return ("billing", (
                "⚠️ **This agent can't respond right now** — the API provider "
                "reported that billing or credits are exhausted for its model.\n\n"
                f"Error: `{error_str}`\n\n"
                "Try switching the agent to a different model in settings, "
                "or add credits to your provider account."
            ))
        elif any(x in el for x in ["429", "rate limit", "too many requests"]):
            return ("rate_limit", (
                "⚠️ **Rate limited** — the API provider is receiving too many requests.\n\n"
                f"Error: `{error_str}`\n\n"
                "Wait a moment and try again."
            ))
        elif any(x in el for x in ["400", "usage limit", "access on"]):
            return ("usage_limit", (
                "⚠️ **Usage limit reached** — this model's API usage cap has been hit.\n\n"
                f"Error: `{error_str}`\n\n"
                "Try switching to a different model or provider."
            ))
        elif any(x in el for x in ["403", "blocked", "permission_denied", "api_key_service_blocked"]):
            return ("api_blocked", (
                "⚠️ **API access blocked** — the API key doesn't have access to this model.\n\n"
                f"Error: `{error_str}`\n\n"
                "You may need to enable the API in your provider's console."
            ))
        elif any(x in el for x in ["404", "not found", "model not found"]):
            return ("model_not_found", (
                "⚠️ **Model not found** — this model name isn't recognized.\n\n"
                f"Error: `{error_str}`\n\n"
                "The model may have been renamed or removed."
            ))
        elif any(x in el for x in ["timeout", "timed out"]):
            return ("timeout", (
                "⏱️ **Request timed out** — the model took too long to respond.\n\n"
                f"Error: `{error_str}`\n\n"
                "Try again, or switch to a faster model."
            ))
        else:
            return ("model_error", (
                "⚠️ **The model returned an error** — it couldn't process this request.\n\n"
                f"Error: `{error_str}`\n\n"
                "Try switching to a different model."
            ))

    if "error" in result:
        error_str = str(result["error"])
        error_type, user_msg = _classify_error(error_str)
        attachments, response_text = _extract_media(user_msg)
        try:
            store_message(info.key, info.agent_id, "user", text)
            if response_text:
                store_message(info.key, info.agent_id, "assistant", response_text)
        except Exception:
            pass
        return {
            "session_key": info.key,
            "response": response_text,
            "attachments": attachments,
            "agent_id": info.agent_id,
        }

    raw_response = result.get("final_response")
    # Safety net: if final_response is None, check the raw agent response for errors
    if raw_response is None:
        # The Hermes agent often puts error info in the 'response' field
        agent_output = result.get("response", "")
        if agent_output:
            error_type, user_msg = _classify_error(str(agent_output))
            attachments, response_text = _extract_media(user_msg)
            try:
                store_message(info.key, info.agent_id, "user", text)
                if response_text:
                    store_message(info.key, info.agent_id, "assistant", response_text)
            except Exception:
                pass
            return {
                "session_key": info.key,
                "response": response_text,
                "attachments": attachments,
                "agent_id": info.agent_id,
                "error_type": error_type,
            }
        # No error info at all — generic fallback
        raw_response = "⚠️ The agent returned an empty response. This could be a temporary issue — try again."
    # Extract MEDIA: tags and bare local file paths, serve files
    attachments, response_text = _extract_media(raw_response)

    # Persist messages to survive restarts
    try:
        store_message(info.key, info.agent_id, "user", text)
        if response_text:
            store_message(info.key, info.agent_id, "assistant", response_text)
    except Exception:
        pass  # Non-critical — messages still work in-memory

    return {
        "session_key": info.key,
        "response": response_text,
        "attachments": attachments,
        "agent_id": info.agent_id,
    }


# ── Error handlers ──────────────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": f"Internal server error: {str(exc)}"},
    )
