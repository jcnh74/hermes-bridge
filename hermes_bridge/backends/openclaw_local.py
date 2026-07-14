"""
OpenClaw local backend (gateway-free).

Drives OpenClaw via ``openclaw agent --local --json`` — one subprocess per turn,
no gateway required. This is the simplest way to reach OpenClaw when its ACP
bridge / WebSocket gateway is not running.

Session continuity is handled by OpenClaw itself via ``--session-id`` (verified:
cross-turn recall). We reuse a stable session id for the life of this backend
instance, so history is preserved server-side without resending it.

Output contract (see skill: openclaw-agent-programmatic-invoke):
  - reply text: concatenation of ``.payloads[*].text``
  - success:    ``.meta.livenessState == "working"`` and ``.meta.stopReason == "stop"``
  - errors also land in payload text, so gate on meta before trusting it.

NOTE: ``--local`` runs the whole turn then returns JSON — there is no native
token stream. To satisfy the streaming callback contract we emit the finished
reply as a single delta. (Upgrade path: OpenClaw gateway / ACP for true tokens.)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from .base import AgentBackend

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-sonnet-4-5-20250929"


def _child_env() -> dict:
    env = os.environ.copy()
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            env.setdefault(k.strip(), v)
    return env


class OpenClawLocalBackend(AgentBackend):
    kind = "openclaw-local"

    def __init__(
        self,
        *,
        agent_id: str = "main",
        model: Optional[str] = None,
        session_id: Optional[str] = None,
        turn_timeout: int = 120,
    ):
        self.agent_id = agent_id
        self.override_model = model or DEFAULT_MODEL
        # Stable session id → OpenClaw preserves history across turns.
        self._session_id = session_id or f"bridge-{os.urandom(4).hex()}"
        self.turn_timeout = turn_timeout
        self._lock = threading.Lock()
        self._message_history: list[dict] = []

    def run_conversation(
        self,
        text: str,
        stream_delta_callback: Optional[Callable[[str], None]] = None,
        history: Optional[list] = None,
    ) -> dict:
        with self._lock:
            cmd = [
                "openclaw", "agent", "--local",
                "--agent", self.agent_id,
                "--session-id", self._session_id,
                "--model", self.override_model,
                "--json", "--timeout", str(self.turn_timeout),
                "-m", text,
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    env=_child_env(),
                    timeout=self.turn_timeout + 30,
                )
            except subprocess.TimeoutExpired:
                return {"final_response": "", "error": "openclaw agent timed out",
                        "session_id": self._session_id}

            if proc.returncode != 0 and not proc.stdout.strip():
                return {"final_response": "",
                        "error": f"openclaw exited {proc.returncode}: {proc.stderr[:300]}",
                        "session_id": self._session_id}

            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return {"final_response": "",
                        "error": f"unparseable openclaw output: {proc.stdout[:300]}",
                        "session_id": self._session_id}

            meta = data.get("meta", {})
            payloads = data.get("payloads", []) or []
            reply = "".join(p.get("text", "") for p in payloads if isinstance(p, dict))

            # Gate on meta: errors also land in payload text.
            if meta.get("stopReason") == "error" or meta.get("livenessState") == "blocked":
                return {"final_response": "", "error": reply or "openclaw run error",
                        "session_id": self._session_id}

            if stream_delta_callback and reply:
                try:
                    stream_delta_callback(reply)  # single delta (no native stream)
                except Exception:
                    logger.debug("chunk callback raised", exc_info=True)

            self._message_history.append({"role": "user", "content": text})
            if reply:
                self._message_history.append({"role": "assistant", "content": reply})

            return {
                "final_response": reply,
                "messages": self._message_history,
                "session_id": self._session_id,
                "message_history": self._message_history,
                "stop_reason": meta.get("stopReason"),
            }

    def switch_model(self, new_model: str, preserve_history: bool = True) -> dict:
        with self._lock:
            old = self.override_model
            self.override_model = new_model
            preserved = len(self._message_history)
            if not preserve_history:
                self._message_history.clear()
                preserved = 0
            return {
                "session_id": self._session_id,
                "previous_model": old,
                "new_model": new_model,
                "preserved_messages": preserved,
            }

    def get_message_history(self) -> list[dict]:
        return self._message_history

    def cleanup(self) -> None:
        # Nothing persistent to release — each turn is its own subprocess.
        pass
