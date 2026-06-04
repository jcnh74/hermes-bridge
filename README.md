# Hermes Bridge API

A lightweight **REST + SSE bridge** that exposes [Hermes Agent](https://hermes-agent.nousresearch.com) to mobile apps and any HTTP client. Built for the [Agentfy](https://github.com/jcnh74/agentfy_app) iOS app, but provider-agnostic — it's plain HTTP, no WebSocket or custom handshake required.

The bridge runs alongside a Hermes installation, discovers the agents and skills configured under `~/.hermes/`, and streams agent responses over Server-Sent Events.

---

## Features

- **Chat with any Hermes agent** over HTTP, with token-by-token streaming via SSE
- **Multi-agent** — list, create, and edit agents; each gets isolated sessions
- **Skill management** — browse the `~/.hermes/skills/` catalog and attach/detach skills per agent
- **Model routing** — list available models, switch an agent's model on the fly
- **Provider key management** — read/write API keys in `~/.hermes/.env` so mobile clients never hold secrets
- **TTS proxy** — ElevenLabs / OpenAI text-to-speech and transcription routed server-side
- **Session persistence** — message history stored in the Hermes SQLite DB, importable/exportable
- **Device pairing** — QR-code pairing flow for connecting the Agentfy app

---

## Requirements

- Python **3.11+** (the same interpreter that runs your Hermes install)
- A working [Hermes Agent](https://hermes-agent.nousresearch.com) install at `~/.hermes/hermes-agent` (the bridge imports it at runtime and reads config from `~/.hermes/`)

---

## Install (one command)

```bash
git clone https://github.com/jcnh74/hermes-bridge.git
cd hermes-bridge
./install.sh
```

The installer:
1. Finds your Hermes install (or honors `HERMES_AGENT_ROOT=/path`)
2. Picks the **same Python venv** that runs Hermes (so imports resolve)
3. Installs the bridge + optional QR support
4. Runs a preflight check and prints exactly how to start

If anything's off, it tells you precisely what to fix — no stack traces.

### Verify the environment anytime

```bash
hermes-bridge doctor
```

```
✓ Hermes Agent found: /Users/you/.hermes/hermes-agent
✓ Hermes Python modules import cleanly
✓ Hermes config loaded (default model: claude-opus-4-8)
All checks passed. Start the bridge with: hermes-bridge start
```

### Start it

```bash
hermes-bridge start              # daemonized; runs preflight first
hermes-bridge start --foreground # run in the foreground for debugging
hermes-bridge status             # is it running?
hermes-bridge pair               # QR + URL to connect the Agentfy app
hermes-bridge stop
```

Verify it's up:

```bash
curl http://localhost:8765/api/v1/health
# {"status":"ok","version":"0.1.0","agents_available":5,"sessions_active":0, ...}
```

### Manual install (if you'd rather not use the script)

```bash
# Use the Python that runs Hermes
~/.hermes/hermes-agent/venv/bin/python -m pip install -e ".[qr]"
~/.hermes/hermes-agent/venv/bin/python -m hermes_bridge.cli doctor
```

### Pointing at a non-standard Hermes location

```bash
export HERMES_AGENT_ROOT=/opt/hermes-agent   # dir containing run_agent.py
hermes-bridge doctor
```

### CLI commands

| Command | Description |
|---------|-------------|
| `hermes-bridge doctor` | Check the environment is ready (run this first) |
| `hermes-bridge start [--port 8765] [--host 0.0.0.0] [--foreground] [--skip-checks]` | Start the server (runs preflight unless `--skip-checks`) |
| `hermes-bridge stop` | Stop the running server |
| `hermes-bridge status` | Check whether the server is running |
| `hermes-bridge restart` | Restart the server |
| `hermes-bridge pair` | Print pairing URL + QR code for the Agentfy app |

---

## API Reference

Base path: `/api/v1`

### Health & pairing
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check + feature flags |
| `GET` | `/pairing` | Pairing payload for the mobile app |

### Agents & models
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/agents` | List available Hermes agents |
| `POST` | `/agents` | Create a new agent |
| `PATCH` | `/agents/{agent_id}` | Update an agent (name, model, description, emoji) |
| `GET` | `/models` | List available models + status |

### Sessions & messages
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/sessions` | Create a session with an agent |
| `GET` | `/agents/{agent_id}/sessions` | List an agent's sessions |
| `POST` | `/sessions/{session_key}/messages` | Send a message — **returns an SSE stream** |
| `GET` | `/sessions/{session_key}/messages` | Get message history |
| `POST` | `/sessions/import` | Import sessions/messages |

### Skills
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/skills` | List all skills (search/category filters) |
| `GET` | `/skills/{skill_name}` | Get a skill's detail |
| `GET` | `/skills/{skill_name}/files/{file_path}` | Read a linked skill file |
| `GET` | `/agents/{agent_id}/skills` | List skills attached to an agent |
| `POST` | `/agents/{agent_id}/skills` | Attach a skill to an agent |
| `DELETE` | `/agents/{agent_id}/skills/{skill_name}` | Detach a skill from an agent |

### Keys & TTS
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/settings/keys` | List provider key status (never returns secret values) |
| `POST` | `/settings/keys` | Set a provider key (written to `~/.hermes/.env`) |
| `GET` | `/tts/status` | TTS provider availability + credit status |
| `GET` | `/tts/speak` | Synthesize speech |
| `POST` | `/tts/transcribe` | Transcribe audio |
| `GET` | `/files/{file_id}` | Download a file referenced in an agent response |

Full interactive docs are served at `http://localhost:8765/docs` (FastAPI/OpenAPI) while the server is running.

---

## Connecting the Agentfy App

In Agentfy, add a bridge connection pointing at:

```
http://<hermes-host>:8765/api/v1
```

Or run `hermes-bridge pair` and scan the QR code. For remote access, put the bridge behind a tunnel (e.g. Cloudflare Tunnel) and point the app at the public HTTPS URL — the API path stays `/api/v1`.

---

## Architecture

```
Agentfy app  ──HTTP/SSE──▶  Hermes Bridge (FastAPI)  ──sys.path import──▶  Hermes Agent
                                   │                                          (~/.hermes/)
                                   ├── server.py       REST + SSE routes
                                   ├── agent_proxy.py  spawns/streams the agent
                                   ├── hermes_env.py   locates Hermes, hardens imports
                                   ├── skills.py       scans ~/.hermes/skills, attach/detach
                                   ├── persistence.py  SQLite (~/.hermes/hermes.db)
                                   ├── models.py       pydantic request/response schemas
                                   └── cli.py          start/stop/status/restart/pair/doctor
```

The bridge does not bundle Hermes — it locates an existing install at `~/.hermes/hermes-agent` and imports `run_agent` / `hermes_cli` at runtime. Keys and config are read from and written to the standard `~/.hermes/` locations so the bridge and the Hermes CLI stay in sync.

---

## Security Notes

- API keys live only in `~/.hermes/.env` on the host. The `/settings/keys` endpoints report status but never echo secret values.
- The bridge binds `0.0.0.0` by default for LAN access. **Do not expose it directly to the public internet** — front it with an authenticated tunnel or reverse proxy.

---

## License

MIT
