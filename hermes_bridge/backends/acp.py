"""
ACP (Agent Client Protocol) backend.

Drives an external agent over JSON-RPC on stdio. Verified interop-compatible
with both ``hermes acp`` (agent-client-protocol 0.9.0, protocol v1) and
``openclaw acp`` on 2026-07-13.

Per-turn wire sequence (see skill: acp-unified-agent-control):
    1. initialize   -> agentInfo / agentCapabilities
    2. session/new  -> {sessionId}
    3. session/prompt -> streams agent_message_chunk notifications,
                         then returns {stopReason}

The subprocess is long-lived: we initialize + create the session once, then
reuse it for every turn (ACP keeps history server-side, mirroring how the
direct backend relies on session continuity). Assistant text arrives as
notifications *before* the final result, so a reader thread demuxes responses
(matched by id) from streaming notifications.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .base import AgentBackend

logger = logging.getLogger(__name__)


def _load_env(env: dict) -> dict:
    """Merge ~/.hermes/.env into an env dict (provider keys for the child)."""
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


class AcpBackend(AgentBackend):
    """Run a turn against any ACP server subprocess."""

    kind = "acp"

    def __init__(
        self,
        server_command: list[str],
        *,
        agent_id: str = "main",
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        init_timeout: float = 30.0,
        turn_timeout: float = 120.0,
    ):
        self.server_command = server_command
        self.agent_id = agent_id
        self.override_model = model
        self.cwd = cwd or str(Path.home())
        self.init_timeout = init_timeout
        self.turn_timeout = turn_timeout

        self._proc: Optional[subprocess.Popen] = None
        self._session_id: Optional[str] = None
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._chunks: list[str] = []
        self._chunk_cb: Optional[Callable[[str], None]] = None
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._message_history: list[dict] = []

    # ── process lifecycle ────────────────────────────────────────────────

    def _ensure_started(self):
        if self._proc and self._proc.poll() is None and self._session_id:
            return
        self._spawn()
        self._initialize()
        self._new_session()

    def _spawn(self):
        env = _load_env(os.environ.copy())
        logger.info("AcpBackend spawning: %s", " ".join(self.server_command))
        self._proc = subprocess.Popen(
            self.server_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            bufsize=1,
            cwd=self.cwd,
        )
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()

    def _reader(self):
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                self._responses[msg["id"]] = msg
            else:
                self._handle_notification(msg)

    def _handle_notification(self, msg: dict):
        upd = (msg.get("params") or {}).get("update") or {}
        if upd.get("sessionUpdate") == "agent_message_chunk":
            content = upd.get("content") or {}
            if content.get("type") == "text":
                text = content.get("text", "")
                self._chunks.append(text)
                if self._chunk_cb:
                    try:
                        self._chunk_cb(text)
                    except Exception:
                        logger.debug("chunk callback raised", exc_info=True)

    # ── JSON-RPC helpers ─────────────────────────────────────────────────

    def _send(self, method: str, params: dict) -> int:
        with self._io_lock:
            self._next_id += 1
            rid = self._next_id
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            return rid

    def _wait(self, rid: int, timeout: float) -> Optional[dict]:
        start = time.time()
        while time.time() - start < timeout:
            if rid in self._responses:
                return self._responses.pop(rid)
            if self._proc and self._proc.poll() is not None:
                return None
            time.sleep(0.02)
        return None

    def _initialize(self):
        rid = self._send(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False}
                },
            },
        )
        r = self._wait(rid, self.init_timeout)
        if not r or "result" not in r:
            raise RuntimeError(f"ACP initialize failed: {r}")
        logger.info("ACP initialized: %s", r["result"].get("agentInfo"))

    def _new_session(self):
        rid = self._send("session/new", {"cwd": self.cwd, "mcpServers": []})
        r = self._wait(rid, self.init_timeout)
        if not r or "result" not in r:
            raise RuntimeError(f"ACP session/new failed: {r}")
        self._session_id = r["result"].get("sessionId")
        logger.info("ACP session created: %s", self._session_id)

    # ── AgentBackend contract ────────────────────────────────────────────

    def run_conversation(
        self,
        text: str,
        stream_delta_callback: Optional[Callable[[str], None]] = None,
        history: Optional[list] = None,
    ) -> dict:
        with self._lock:
            try:
                self._ensure_started()
                self._chunks = []
                self._chunk_cb = stream_delta_callback

                rid = self._send(
                    "session/prompt",
                    {
                        "sessionId": self._session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                )
                r = self._wait(rid, self.turn_timeout)
                self._chunk_cb = None

                if not r or "result" not in r:
                    err = (r or {}).get("error", "no response / timeout")
                    return {"final_response": "", "error": str(err),
                            "session_id": self._session_id}

                response = "".join(self._chunks)
                self._message_history.append({"role": "user", "content": text})
                if response:
                    self._message_history.append(
                        {"role": "assistant", "content": response}
                    )
                return {
                    "final_response": response,
                    "messages": self._message_history,
                    "session_id": self._session_id,
                    "message_history": self._message_history,
                    "stop_reason": r["result"].get("stopReason"),
                }
            except Exception as e:
                logger.error("AcpBackend.run_conversation failed: %s", e, exc_info=True)
                return {"final_response": "", "error": str(e),
                        "session_id": self._session_id}

    def switch_model(self, new_model: str, preserve_history: bool = True) -> dict:
        # ACP model switching is server-specific; for now restart the session.
        with self._lock:
            old = self.override_model
            self.override_model = new_model
            preserved = len(self._message_history)
            if not preserve_history:
                self._message_history.clear()
                preserved = 0
            self.cleanup()  # force fresh session on next turn
            return {
                "session_id": self._session_id,
                "previous_model": old or "default",
                "new_model": new_model,
                "preserved_messages": preserved,
            }

    def get_message_history(self) -> list[dict]:
        return self._message_history

    def cleanup(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None
        self._session_id = None
