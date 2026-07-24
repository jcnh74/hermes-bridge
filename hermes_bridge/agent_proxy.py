"""
Agent proxy — wraps Hermes AIAgent for programmatic use from the bridge API.

This mirrors how the gateway creates agents for incoming messages, but without
the platform adapter layer. It loads the same config, resolves the same runtime
provider, and calls the same AIAgent.run_conversation() under the hood.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# ── Hermes imports (lazy to avoid circular deps on import) ──────────────

from .hermes_env import ensure_hermes_env as _ensure_hermes_env  # noqa: F401


def load_config():
    """Load Hermes config.yaml similar to how the gateway does it."""
    _ensure_hermes_env()
    from hermes_cli.config import load_config as _load_cfg
    return _load_cfg()


def resolve_runtime():
    """Resolve the provider runtime config for creating AIAgent instances."""
    _ensure_hermes_env()
    from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error

    try:
        runtime = resolve_runtime_provider(
            requested=os.getenv("HERMES_INFERENCE_PROVIDER"),
        )
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    return {
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
        "credential_pool": runtime.get("credential_pool"),
    }


def _load_env_file():
    """Load ~/.hermes/.env into os.environ if not already loaded."""
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.is_file():
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
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


def _has_api_key(name: str) -> bool:
    """Check if an API key env var is set (checking os.environ + .env file)."""
    # Try os.environ first (already loaded)
    val = os.environ.get(name, "")
    if val.strip().strip("'\""):
        return True
    # Fall back to reading .env directly
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.is_file():
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{name}="):
                        val = line.split("=", 1)[1].strip().strip("'\"")
                        return bool(val)
        except Exception:
            pass
    return False


def get_agent_list():
    """Return a list of available agents from the Hermes config.

    Reads the agents config from ~/.hermes/config.yaml or the default agent list.
    Prefer the config entry for ``main`` when present so the API matches what
    sessions actually run (do not hardcode HERMES_DEFAULT_MODEL for main).
    """
    _ensure_hermes_env()

    default_model = os.getenv("HERMES_DEFAULT_MODEL", "grok-4.20-0309-reasoning")
    main_entry = {
        "id": "main",
        "name": "Hermes",
        "model": default_model,
        "description": "Main Hermes assistant",
        "emoji": "Sparkles",
    }
    others: list[dict] = []

    try:
        config = load_config()
        agent_configs = config.get("agents", {}).get("list", [])
        if isinstance(agent_configs, list):
            for ac in agent_configs:
                agent_id = ac.get("id") or ac.get("agentId")
                if not agent_id:
                    continue
                entry = {
                    "id": agent_id,
                    "name": ac.get("identity", {}).get("name", ac.get("name", agent_id)),
                    "model": ac.get("model", "") or default_model,
                    "description": ac.get("identity", {}).get("description", ""),
                    "emoji": ac.get("identity", {}).get("emoji", ""),
                }
                if agent_id == "main":
                    main_entry = entry
                else:
                    others.append(entry)
    except Exception:
        pass  # Non-fatal — main agent is always available

    return [main_entry, *others]


def create_custom_agent(
    name: str,
    emoji: str = "🤖",
    model: str | None = None,
    description: str | None = None,
) -> str:
    """Create a new custom agent in the Hermes config.

    Saves the agent to config.yaml so it persists across restarts.
    Returns the agent ID.
    """
    _ensure_hermes_env()

    agent_id = name.lower().replace(" ", "_")
    # Ensure uniqueness
    existing = get_agent_list()
    existing_ids = {a["id"] for a in existing}
    if agent_id in existing_ids:
        import uuid
        agent_id = f"{agent_id}_{uuid.uuid4().hex[:4]}"

    agent_entry = {
        "id": agent_id,
        "model": model or os.getenv("HERMES_DEFAULT_MODEL", "grok-4.20-0309-reasoning"),
        "identity": {
            "name": name,
            "emoji": emoji or "🤖",
            "description": description or f"{name} — custom agent",
        },
        "system_prompt": (
            f"You are {name}, {description or 'a custom agent created by the user.'}\n\n"
            "Be helpful, conversational, and honest. "
            "Earn trust through competence."
        ),
    }

    try:
        from hermes_cli.config import save_config
        config = load_config()

        if "agents" not in config:
            config["agents"] = {}
        if "list" not in config["agents"]:
            config["agents"]["list"] = []

        # Replace if same ID, append otherwise
        agent_list = config["agents"]["list"]
        for i, a in enumerate(agent_list):
            existing_id = a.get("id") or a.get("agentId")
            if existing_id == agent_id:
                agent_list[i] = agent_entry
                break
        else:
            agent_list.append(agent_entry)

        save_config(config)
        logger.info(f"Created custom agent '{name}' (id={agent_id})")
    except Exception as e:
        logger.warning(f"Could not save agent to config: {e}")
        logger.info("Agent will be available for this session only")

    return agent_id


def get_available_models() -> list[dict]:
    """Return models from providers with configured API keys.

    Shows only the curated model list from Hermes' built-in _PROVIDER_MODELS
    for providers that have keys configured (deepseek, anthropic, openai, gemini,
    minimax) plus the curated OpenRouter list. No live API calls — these are
    the models Hermes knows about and can actually route to.

    Each model entry includes:

      - `status`: one of:
          "available"      — provider key is set, model should work
          "needs_credits"  — OpenRouter model that requires paid credits
          "needs_key"      — provider key is NOT set (model won't work)
          "check_routing"  — model name routing is ambiguous
      - `payment_required`: legacy boolean, True if OpenRouter paid model
    """
    _ensure_hermes_env()
    result = []
    seen = set()

    # Providers with keys configured on this system.
    # NOTE: 'openai-codex' is intentionally excluded — those models use the Codex
    # CLI transport (not a direct API) and are all available via OpenRouter as
    # openai/gpt-* models anyway. Including them here would show duplicate entries
    # with misleading "available" status when they actually need OpenRouter credits.
    configured_providers = [
        "deepseek",     # DEEPSEEK_API_KEY — always works if key is present
        "anthropic",    # ANTHROPIC_API_KEY — works, but may have usage caps
        "gemini",       # GOOGLE_API_KEY — needs Generative Language API enabled
        "minimax",      # MINIMAX_API_KEY — needs correct endpoint
        "kimi-coding",  # KIMI_API_KEY / MOONSHOT_API_KEY — Moonshot direct (kimi-k3, k2.x)
    ]

    # Providers that don't have a direct routing rule in _create_agent()
    # These models fall through to the default provider (DeepSeek), not their own API.
    # Mark them as "needs_route" until routing is added.
    no_direct_route: set[str] = set()

    # Map provider keys to their env var names for status checking
    _KEY_MAP = {
        "deepseek": "DEEPSEEK_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GOOGLE_API_KEY",
        "minimax": "MINIMAX_API_KEY",
        # Hermes resolves kimi-coding via KIMI_API_KEY (preferred) or MOONSHOT_API_KEY
        "kimi-coding": "KIMI_API_KEY",
    }

    # Pre-check which provider keys are present
    _has_key = {k: _has_api_key(v) for k, v in _KEY_MAP.items()}

    try:
        from hermes_cli.models import _PROVIDER_MODELS as PM
        from hermes_cli.models import OPENROUTER_MODELS

        # 1. Curated models from providers with configured API keys
        #    Status: "available" if key is present and routing exists,
        #            "needs_key" if key is missing,
        #            "check_routing" if the model name can't be routed to the right provider
        for provider_key in configured_providers:
            models = PM.get(provider_key, [])
            for m in models:
                if m not in seen:
                    seen.add(m)
                    if not _has_key.get(provider_key, False):
                        # kimi-coding also accepts MOONSHOT_API_KEY
                        if provider_key == "kimi-coding" and _has_api_key("MOONSHOT_API_KEY"):
                            status = "available"
                        else:
                            status = "needs_key"
                    elif provider_key in ("gemini", "minimax", "kimi-coding", "anthropic"):
                        status = "available"
                    else:
                        status = "available" if _has_key.get(provider_key, False) else "needs_key"
                    # Friendlier display names for Kimi flagship models
                    display = m
                    if m == "kimi-k3":
                        display = "Kimi K3"
                    elif m == "kimi-k2.7-code":
                        display = "Kimi K2.7 Code"
                    elif m == "kimi-k2.7-code-highspeed":
                        display = "Kimi K2.7 Code Highspeed"
                    elif m == "kimi-k2.6":
                        display = "Kimi K2.6"
                    result.append({
                        "id": m,
                        "name": display,
                        "provider": provider_key if provider_key != "kimi-coding" else "moonshot",
                        "base_url": "",
                        "payment_required": False,
                        "status": status,
                    })

        # 2. Curated OpenRouter list
        #    OpenRouter uses credits. :free models are no-cost, others need credits.
        #    If the OpenRouter key is missing, ALL OpenRouter models are "needs_key".
        or_key_available = _has_api_key("OPENROUTER_API_KEY")
        for mid, description in OPENROUTER_MODELS:
            if mid not in seen:
                seen.add(mid)
                name = mid.split("/")[-1].replace("-", " ").title()
                is_free = description == "free"
                if is_free:
                    name += " 🆓"
                elif description == "recommended":
                    name += " ★"
                if not or_key_available:
                    status = "needs_key"
                elif is_free:
                    status = "available"
                else:
                    status = "needs_credits"
                result.append({
                    "id": mid,
                    "name": name,
                    "provider": "openrouter",
                    "base_url": "",
                    "payment_required": not is_free,
                    "status": status,
                })

    except Exception as e:
        logger.debug("Could not load model catalog: %s", e)

    # Always ensure at least these are available
    fallback = ["deepseek-chat", "deepseek-reasoner"]
    for m in fallback:
        if m not in seen:
            result.append({"id": m, "name": m, "provider": "deepseek", "base_url": "", "payment_required": False, "status": "available"})

    return result


def update_custom_agent(
    agent_id: str,
    name: str | None = None,
    emoji: str | None = None,
    model: str | None = None,
    description: str | None = None,
) -> dict:
    """Update an existing custom agent in the Hermes config.

    Args:
        agent_id: The agent ID to update.
        name: New name (None = no change).
        emoji: New emoji (None = no change).
        model: New model (None = no change).
        description: New description (None = no change).

    Returns:
        Dict with 'id', 'name', 'emoji', 'model', 'status'.

    Raises:
        ValueError if agent not found.
    """
    _ensure_hermes_env()
    from hermes_cli.config import save_config
    config = load_config()

    agent_list = config.get("agents", {}).get("list", [])
    found = None
    for a in agent_list:
        existing_id = a.get("id") or a.get("agentId")
        if existing_id == agent_id:
            found = a
            break

    if not found:
        if agent_id == "main":
            # The main agent isn't stored in config, but we can update it in memory.
            # Re-read the hardcoded emoji from the list_agents function won't persist
            # across restarts, but we can store it in config for persistence.
            config.setdefault("agents", {}).setdefault("list", [])
            config["agents"]["list"].append({
                "id": "main",
                "model": os.getenv("HERMES_DEFAULT_MODEL", "grok-4.20-0309-reasoning"),
                "identity": {
                    "name": name or "Hermes",
                    "emoji": emoji or "Sparkles",
                    "description": description or "Main Hermes assistant",
                },
                "system_prompt": (
                    f"You are {name or 'Hermes'}, the main Hermes assistant.\n\n"
                    "Be helpful, conversational, and honest. "
                    "Earn trust through competence."
                ),
            })
            found = config["agents"]["list"][-1]
            found_in_config = found
        else:
            raise ValueError(f"Agent '{agent_id}' not found in config")

    if name:
        found["identity"]["name"] = name
    if emoji:
        found["identity"]["emoji"] = emoji
    if description:
        found["identity"]["description"] = description
    if model:
        found["model"] = model

    # Update system prompt to reflect new name
    if name:
        display_name = name
        desc = found.get("identity", {}).get("description", "a custom agent")
        found["system_prompt"] = (
            f"You are {display_name}, {desc}\\n\\n"
            "Be helpful, conversational, and honest. "
            "Earn trust through competence."
        )

    save_config(config)
    logger.info(f"Updated custom agent '{agent_id}'")

    return {
        "id": agent_id,
        "name": name or found.get("identity", {}).get("name", agent_id),
        "emoji": emoji or found.get("identity", {}).get("emoji", ""),
        "model": model or found.get("model", ""),
        "status": "updated",
    }


class AgentProxy:
    """Wraps a Hermes AIAgent for programmatic use.

    Manages a persistent AIAgent instance per session key. Each call to
    run_conversation() sends a message and returns the final response.
    Streaming callbacks are supported for real-time delta output.
    """

    def __init__(self, agent_id: str = "main", model: str = "", platform: str = "bridge"):
        self.agent_id = agent_id
        self.override_model = model
        self.platform = platform
        self._agent = None
        self._lock = threading.Lock()
        self._session_id = None
        self._message_history: list[dict] = []

    def _create_agent(self):
        """Create a new AIAgent instance, mirroring the gateway pattern."""
        _ensure_hermes_env()
        from run_agent import AIAgent
        from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error

        model = self.override_model or os.getenv("HERMES_DEFAULT_MODEL", "grok-4.20-0309-reasoning")

        # Resolve runtime — start with default provider
        runtime = resolve_runtime()

        # For OpenRouter models, override the runtime to use OpenRouter's
        # API endpoint and key. OpenRouter models have a slash prefix
        # (e.g. openai/gpt-4.1, anthropic/claude-opus-4.7).
        is_openrouter_model = "/" in model

        if is_openrouter_model:
            try:
                or_runtime = resolve_runtime_provider(requested="openrouter")
                runtime["base_url"] = or_runtime.get("base_url") or runtime["base_url"]
                runtime["api_key"] = or_runtime.get("api_key") or runtime["api_key"]
                runtime["provider"] = "openrouter"
                runtime["api_mode"] = or_runtime.get("api_mode", "chat_completions")
            except Exception as exc:
                logger.warning("Could not resolve OpenRouter runtime: %s", exc)
                # Fall through to default runtime
        elif model.startswith(("gpt-", "o1-", "o3-", "chatgpt-")):
            # OpenAI models (gpt-4o, gpt-5, gpt-5.4, o1, o3) — use OpenAI runtime
            # Hermes doesn't have a native "openai" provider, but the API key
            # is set in the environment. Build the runtime manually.
            # NOTE: 'os' is already imported at module level — do NOT re-import here
            oa_key = os.environ.get("OPENAI_API_KEY", "")
            # Strip any surrounding quotes from the .env file
            oa_key = oa_key.strip().strip("'\"")
            if oa_key:
                runtime["base_url"] = "https://api.openai.com/v1"
                runtime["api_key"] = oa_key
                runtime["provider"] = "openai"
                runtime["api_mode"] = "chat_completions"
                logger.info("Routing model '%s' to OpenAI (api.openai.com/v1)", model)
            else:
                logger.warning("OPENAI_API_KEY not set — falling back to default provider")
        elif model.startswith("claude-"):
            # Anthropic Claude models — use Anthropic runtime
            # NOTE: 'os' is already imported at module level — do NOT re-import here
            an_key = os.environ.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_TOKEN", ""))
            an_key = an_key.strip().strip("'\"")
            if an_key:
                runtime["base_url"] = "https://api.anthropic.com/v1"
                runtime["api_key"] = an_key
                runtime["provider"] = "anthropic"
                runtime["api_mode"] = "chat_completions"
                logger.info("Routing model '%s' to Anthropic (api.anthropic.com/v1)", model)
            else:
                logger.warning("ANTHROPIC_API_KEY not set — falling back to default provider")
        elif model.startswith("gemini-"):
            # Google Gemini models — use Google AI runtime
            gm_key = os.environ.get("GOOGLE_API_KEY", "")
            gm_key = gm_key.strip().strip("'\"")
            if gm_key:
                runtime["base_url"] = "https://generativelanguage.googleapis.com/v1beta"
                runtime["api_key"] = gm_key
                runtime["provider"] = "google"
                runtime["api_mode"] = "chat_completions"
                logger.info("Routing model '%s' to Google Gemini (generativelanguage.googleapis.com)", model)
            else:
                logger.warning("GOOGLE_API_KEY not set — falling back to default provider")
        elif model.startswith("MiniMax-"):
            # MiniMax models — use MiniMax API
            mm_key = os.environ.get("MINIMAX_API_KEY", "")
            mm_key = mm_key.strip().strip("'\"")
            if mm_key:
                # MiniMax API endpoint — check common patterns
                runtime["base_url"] = "https://api.minimax.chat/v1"
                runtime["api_key"] = mm_key
                runtime["provider"] = "minimax"
                runtime["api_mode"] = "chat_completions"
                logger.info("Routing model '%s' to MiniMax (api.minimax.chat)", model)
            else:
                logger.warning("MINIMAX_API_KEY not set — falling back to default provider")
        elif model.startswith("kimi-") or model.startswith("moonshot-"):
            # Moonshot / Kimi direct API (kimi-k3, kimi-k2.6, kimi-k2.7-code, …)
            try:
                kimi_runtime = resolve_runtime_provider(requested="kimi-coding")
                runtime["base_url"] = kimi_runtime.get("base_url") or "https://api.moonshot.ai/v1"
                runtime["api_key"] = kimi_runtime.get("api_key") or runtime.get("api_key")
                runtime["provider"] = kimi_runtime.get("provider") or "kimi-coding"
                runtime["api_mode"] = kimi_runtime.get("api_mode", "chat_completions")
                logger.info("Routing model '%s' to Kimi/Moonshot (%s)", model, runtime["base_url"])
            except Exception as exc:
                logger.warning("Could not resolve Kimi/Moonshot runtime: %s", exc)

        # Read toolsets for this platform from config
        try:
            config = load_config()
            platform_tools = (
                config
                .get("platform_toolsets", {})
                .get(self.platform, [])
            )
            # Fall back to cli-level tools if bridge has no specific config
            if not platform_tools:
                platform_tools = config.get("platform_toolsets", {}).get("cli", [])
        except Exception:
            platform_tools = None

        # Look up the agent's system prompt from config
        ephemeral_system_prompt = None
        agent_max_tokens = None
        if self.agent_id != "main":
            try:
                agent_list = config.get("agents", {}).get("list", [])
                for a in agent_list:
                    existing_id = a.get("id") or a.get("agentId")
                    if existing_id == self.agent_id:
                        ephemeral_system_prompt = a.get("system_prompt")
                        agent_max_tokens = a.get("max_tokens")
                        break
            except Exception:
                pass

        # Add bridge platform hints about file delivery
        bridge_hint = (
            "\n\n[Bridge Platform - FILE DELIVERY]\n"
            "IMPORTANT: You MUST use MEDIA: path syntax to deliver files to the client app.\n"
            "Do NOT use markdown image syntax like ![alt](path) — it will not work.\n"
            "Do NOT just mention the path in text — the file won't be sent.\n"
            "Instead, after creating a file, include on its own line:\n"
            "  MEDIA:/full/path/to/file\n"
            "The bridge will serve the file for download, and the client app will\n"
            "display it as an inline attachment (image preview, video card, or file icon).\n"
            "Examples:\n"
            "  MEDIA:/tmp/generated_image.png\n"
            "  MEDIA:/tmp/report.pdf\n"
            "  MEDIA:/tmp/screenshot.jpg\n"
            "\n"
            "⚠️ AVAILABLE TOOLS LIMITATION:\n"
            "- The image_generate_tool (FAL.ai) is NOT available — FAL_KEY is not configured.\n"
            "- The vision_tool is NOT available — the provider (DeepSeek) doesn't support image inputs.\n"
            "If the user asks you to CREATE an image, use Python/Pillow instead:\n"
            "  import PIL, then save to /tmp/filename and include MEDIA:/tmp/filename\n"
            "If the user asks you to ANALYZE or DESCRIBE an image, say you can't process images.\n"
        )
        if ephemeral_system_prompt:
            ephemeral_system_prompt += bridge_hint
        else:
            ephemeral_system_prompt = bridge_hint.lstrip()

        max_tokens_kwargs = {}
        if agent_max_tokens is not None:
            max_tokens_kwargs["max_tokens"] = agent_max_tokens

        return AIAgent(
            model=model,
            **runtime,
            max_iterations=int(os.getenv("HERMES_MAX_ITERATIONS", "90")),
            quiet_mode=True,
            verbose_logging=False,
            enabled_toolsets=platform_tools,
            platform=self.platform,
            session_id=self._session_id,
            ephemeral_system_prompt=ephemeral_system_prompt,
            **max_tokens_kwargs,
        )

    def run_conversation(
        self,
        text: str,
        stream_delta_callback: Optional[Callable[[str], None]] = None,
        history: Optional[list] = None,
    ) -> dict:
        """Send a message and get the response.

        Args:
            text: The user's message.
            stream_delta_callback: Optional callback for real-time text deltas.
            history: Optional conversation history to include.

        Returns:
            Dict with 'final_response', 'messages', and optionally 'error'.
        """
        with self._lock:
            if self._agent is None:
                self._agent = self._create_agent()
                self._session_id = self._agent.session_id

            try:
                # If we have accumulated message history and no explicit
                # history was passed, inject it so the new model has context
                effective_history = history if history is not None else (
                    list(self._message_history) if self._message_history else None
                )

                result = self._agent.run_conversation(
                    user_message=text,
                    conversation_history=effective_history,
                    stream_callback=stream_delta_callback,
                )

                response = result.get("final_response", "") if result else ""
                messages = result.get("messages", [])

                # Track message history
                self._message_history.append({"role": "user", "content": text})
                if response:
                    self._message_history.append({"role": "assistant", "content": response})

                return {
                    "final_response": response,
                    "messages": messages,
                    "session_id": self._session_id,
                    "message_history": self._message_history,
                    **({"error": result["error"]} if result and "error" in result else {}),
                }

            except Exception as e:
                logger.error("AgentProxy.run_conversation failed: %s", e, exc_info=True)
                return {
                    "final_response": "",
                    "error": str(e),
                    "session_id": self._session_id,
                }

    def switch_model(self, new_model: str, preserve_history: bool = True) -> dict:
        """Switch the agent's model, optionally preserving conversation history.

        By default, history IS preserved — the new model carries forward context.
        Set preserve_history=False to start completely fresh.
        """
        with self._lock:
            old_model = self.override_model
            self.override_model = new_model

            # Preserve history before destroying the old agent
            history = list(self._message_history) if self._agent else []

            # Destroy old agent instance
            self._agent = None

            if not preserve_history:
                # Clear message history so the new model starts fresh
                self._message_history.clear()

            # Create new agent with the new model — history stays in _message_history
            # and will be passed as conversation_history on the next run_conversation
            self._agent = self._create_agent()

            return {
                "session_id": self._session_id,
                "previous_model": old_model or "default",
                "new_model": new_model,
                "preserved_messages": len(history),
            }

    def get_message_history(self) -> list[dict]:
        """Return the tracked message history for this session."""
        return self._message_history

    def cleanup(self):
        """Release agent resources."""
        with self._lock:
            self._agent = None
