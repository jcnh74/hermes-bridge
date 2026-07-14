# Phase 1 — Unified ACP Backend: VERIFIED

**Branch:** `feat/unified-acp-backend` · **Date:** 2026-07-13

## What shipped

A pluggable agent-backend layer in the existing `hermes-bridge` repo (NOT a new codebase). The bridge's FastAPI HTTP/SSE surface is unchanged; agent execution now goes through an `AgentBackend` ABC with three implementations:

| Backend | Runtime | Routing prefix |
|---|---|---|
| `HermesDirectBackend` | in-process `AIAgent` (unchanged path) | *(default, no prefix)* |
| `AcpBackend` → Hermes | `hermes acp` subprocess (JSON-RPC/stdio) | `acp:<name>` |
| `AcpBackend` → OpenClaw | `openclaw acp` subprocess | `openclaw:<name>` / `oc:<name>` |

`make_backend(agent_id)` routes by prefix. One session = one backend.

## Verification evidence (all real, this machine)

**Backend unit (direct):**
```
TURN 1: streamed chunks → "Got it — ocean blue it is."  stop_reason: end_turn
TURN 2: "Ocean blue."   ← session persistence via ACP
```

**Full HTTP stack (test server :8770):**
```
POST /api/v1/sessions {"agent_id":"acp:main"} → bridge:acp:main:...  (ACP-routed)
POST .../messages {"text":...,"stream":false}  → "The unified ACP bridge works."
```

**SSE streaming:**
```
event: meta  → session info
event: delta → "One"
event: delta → "... two... three... four... five."
event: done  → {has_response:true, has_error:false}
```

**Tests:** 59 passed (51 existing + 8 new routing tests). No regressions.

## Path proven
HTTP → server.py → make_backend → AcpBackend → `hermes acp` → JSON-RPC → agent turn → SSE deltas → HTTP. The ACP `agent_message_chunk` notifications map cleanly to `event: delta` frames — exactly what Agentfy already consumes.

## Next (Phase 2)
- [ ] OpenClaw ACP end-to-end over HTTP (needs `openclaw acp` gateway running, or add an `openclaw agent --local` backend variant)
- [ ] Merge both agents' lists in `/agents` (tag each with its backend)
- [ ] Agentfy: surface unified agent list (no backend toggle needed)
- [ ] Decide model-switch semantics for ACP sessions (currently restarts session)
