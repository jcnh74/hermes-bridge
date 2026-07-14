# Dual-Backend OpenClaw Bridge — Architecture & Plan

**Goal:** Let the Agentfy iOS/Android app connect to **either** a Hermes backend (current) **or** an OpenClaw backend, selectable in-app. Keep Hermes untouched.

**Status:** Feasibility spike **PASSED** on 2026-07-13 (OpenClaw 2026.5.7, this machine). Everything below is grounded in verified behavior, not speculation.

---

## 1. Why this is cheaper than expected

Three facts collapse the risk:

1. **Agentfy is already backend-agnostic.** `src/ai/HermesBridgeClient.ts` is plain REST + SSE over a configurable `baseUrl` — its own header comment says *"no OpenClaw protocol — just plain HTTP fetch()."* The app doesn't care what's behind the URL as long as the `/api/v1/*` contract holds.

2. **The Hermes Bridge's backend coupling is quarantined to 2 files.** `hermes_bridge/agent_proxy.py` (imports Hermes `AIAgent`, calls `run_conversation()`) and `hermes_bridge/hermes_env.py` (locates the install). REST routes, SSE streaming, sessions, skills, TTS proxy, device pairing = all generic transport, reusable as-is.

3. **OpenClaw has a clean programmatic single-turn API.** Verified command:
   ```bash
   set -a && source ~/.hermes/.env && set +a
   openclaw agent --local --agent main --session-id <id> \
     --model "anthropic/claude-sonnet-4-5-20250929" --json -m "<msg>"
   ```
   - Reply text at `.payloads[*].text`
   - Success = `.meta.livenessState=="working"` && `.meta.stopReason=="stop"`
   - **Session persistence works via `--session-id` alone** (verified: cross-turn recall of "ocean blue"). Caller never resends history.

---

## 2. Target architecture

```
┌─────────────┐     HTTP/SSE      ┌──────────────────┐    import/call    ┌──────────────┐
│  Agentfy    │ ───────────────►  │  Hermes Bridge   │ ───────────────►  │ Hermes Agent │
│  (RN app)   │   baseUrl A       │  :8765 /api/v1   │                   │  (~/.hermes) │
│             │                   └──────────────────┘                   └──────────────┘
│  backend    │
│  picker     │     HTTP/SSE      ┌──────────────────┐   spawn CLI       ┌──────────────┐
│             │ ───────────────►  │ OpenClaw Bridge  │ ───────────────►  │ openclaw     │
└─────────────┘   baseUrl B       │  :8766 /api/v1   │  --local --json   │ agent (main) │
                                  └──────────────────┘                   └──────────────┘
```

Both bridges speak the **same `/api/v1` contract**. The app flips `baseUrl`.

---

## 3. Integration path choice

| Path | Mechanism | Streaming | Effort | Verdict |
|---|---|---|---|---|
| **A. CLI shell-out** ✅ | spawn `openclaw agent --local --json` per turn | buffered now; line-mode later | Lowest | **MVP** |
| B. Gateway WebSocket | connect to `ws://127.0.0.1:18789` | native token stream | Medium | v2 (needs gateway service revived) |
| C. ACP bridge | `openclaw acp` Agent Control Protocol | native, standardized | Medium | if standardization matters |

Go with **A** for MVP. The gateway isn't running post-migration; `--local` sidesteps it entirely.

---

## 4. Work breakdown

### Phase 0 — Spike ✅ DONE
- [x] Prove `openclaw agent --json` returns a real turn → reply at `.payloads[].text`
- [x] Prove session persistence via `--session-id`
- [x] Identify env-key + model-override requirements
- [x] Capture as skill `openclaw-agent-programmatic-invoke`

### Phase 1 — OpenClaw Bridge MVP (~2–4 days)
- [ ] Fork `hermes-bridge` → `openclaw-bridge` (keep REST/SSE/session/TTS/pairing skeleton)
- [ ] Write `openclaw_proxy.py` replacing `agent_proxy.py`:
  - [ ] source provider keys (from `~/.hermes/.env` or own `.env`) into the subprocess env
  - [ ] map bridge session key → OpenClaw `--session-id`
  - [ ] spawn `openclaw agent --local --agent <id> --session-id <k> --model <m> --json -m <msg>`
  - [ ] parse JSON; check `.meta.stopReason`; extract `.payloads[*].text`
  - [ ] surface errors cleanly (model-access errors land in payload text too — gate on meta)
- [ ] Emit reply as SSE (single buffered event for MVP; token streaming in Phase 3)
- [ ] Run on **:8766** (Hermes bridge keeps :8765) so both can run side-by-side

### Phase 2 — Supporting endpoints (~1–2 days)
- [ ] `/agents` ← `openclaw agents list`
- [ ] `/models` ← OpenClaw config / provider list
- [ ] `/health` ← trivial
- [ ] `/skills` ← map to OpenClaw workspace skills if present (or stub)
- [ ] `/sessions` ← OpenClaw session store

### Phase 3 — Agentfy app (~½ day)
- [ ] Settings: backend picker (Hermes vs OpenClaw) → sets `baseUrl` (:8765 vs :8766, or tunnel)
- [ ] Persist choice; reconnect client on switch
- [ ] (Optional) show which backend is active in the chat header

### Phase 4 — Streaming polish (optional, v2)
- [ ] Switch Path A → line-buffered streaming, or move to Gateway (Path B) for native token stream

---

## 5. Known caveats / risks

- **`--local` needs provider keys in the subprocess env**, not just OpenClaw config. Bridge must inject them.
- **Default model may be inaccessible** (spike hit `gpt-5.5` access error). Bridge should pass a known-good `--model` or make it configurable per agent.
- **macOS has no `timeout` binary** — use OpenClaw's `--timeout` flag.
- **Gateway not running** post-migration → stick to `--local` for MVP.
- **Cold-start latency:** each `--local` turn spins the embedded runner (loads system prompt ~40K chars, tools). Measure; if too slow, move to Path B (persistent gateway) sooner.
- **Config location:** active `~/.openclaw/openclaw.json` (historical archive in `~/.openclaw.pre-migration/`).

---

## 5b. Unified control via ACP (Agent Client Protocol) — STRONGER OPTION

**Discovered 2026-07-13:** BOTH agents implement ACP (the Zed JSON-RPC-over-stdio agent standard).

- `hermes acp` = ACP **server**. VERIFIED: responds to raw `initialize` handshake → `agentInfo: hermes-agent v0.18.2`, capabilities incl. session fork/list/resume. Uses `agent-client-protocol==0.9.0` (protocol v1).
- `openclaw acp` = ACP **bridge + client**. `openclaw acp client --server <cmd>` is built to drive EXTERNAL ACP agents (docs list Claude Code, Cursor, Gemini CLI, Codex, OpenClaw, OpenCode). Since Hermes is a valid ACP server, OpenClaw can already treat Hermes as a controllable agent.

**Implication:** instead of two bespoke backend adapters, build ONE ACP↔HTTP/SSE bridge. It spawns `hermes acp` and an OpenClaw runtime as ACP subprocesses, merges their agent lists, and routes sessions by agent id. Any future ACP-compliant agent plugs in for free. Agentfy shows all agents in one list — no backend toggle.

**Caveats:**
- ACP is designed for editor/coding sessions (fs ops, permission prompts, tool approvals) — for phone chat you stub the fs + permission capabilities.
- Protocol-version skew: Hermes = agent-client-protocol 0.9.0 / v1; OpenClaw = separate Node impl. VERIFY OpenClaw's `acp client` completes `initialize → session/new → prompt` against Hermes before committing (NEXT SPIKE).
- OpenClaw's ACP bridge is "backed by the Gateway" (currently not running). For OpenClaw, `openclaw agent --local` (Section 1) may stay simpler than its ACP path.

**Recommendation:** Prototype the ACP route (Option 2 = thin unified ACP bridge). If OpenClaw's ACP proves fiddly, fall back to `openclaw agent --local` for the OpenClaw adapter while keeping Hermes on ACP.

## 6. Effort summary

| Scope | Estimate |
|---|---|
| Rough streaming spike to app | 1–2 days |
| **Full dual-backend MVP (chat works end-to-end, both backends)** | **~1 week** |
| Streaming/gateway polish | +2–4 days |

Bottom line: **not far off at all.** The hard unknown (can we invoke OpenClaw programmatically with sessions?) is already answered yes. Remaining work is mechanical bridge plumbing + a tiny app setting.
