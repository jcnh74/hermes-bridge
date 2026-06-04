#!/usr/bin/env bash
#
# Hermes Bridge — one-command installer.
#
# Installs the bridge into the SAME Python environment that runs your Hermes
# Agent, so it can import Hermes at runtime. Safe to re-run (idempotent).
#
# Usage:
#   ./install.sh                       # auto-detect Hermes venv
#   HERMES_AGENT_ROOT=/path ./install.sh
#
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
say()  { echo -e "${BLUE}▶${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. Locate the Hermes install ─────────────────────────────────────────
say "Locating Hermes Agent..."
HERMES_ROOT="${HERMES_AGENT_ROOT:-}"
if [[ -z "$HERMES_ROOT" ]]; then
  for c in "$HOME/.hermes/hermes-agent"; do
    if [[ -f "$c/run_agent.py" ]]; then HERMES_ROOT="$c"; break; fi
  done
fi
[[ -n "$HERMES_ROOT" && -f "$HERMES_ROOT/run_agent.py" ]] \
  || die "Hermes Agent not found. Install it first, or set HERMES_AGENT_ROOT=/path/to/hermes-agent
   See https://hermes-agent.nousresearch.com"
ok "Hermes Agent: $HERMES_ROOT"

# ── 2. Find the Python that runs Hermes ──────────────────────────────────
say "Locating Hermes Python environment..."
PYBIN=""
# Prefer a venv whose Python can actually import Hermes' deps (e.g. yaml).
for v in "$HERMES_ROOT/venv/bin/python" "$HERMES_ROOT/.venv/bin/python" \
         "$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/hermes-agent/.venv/bin/python"; do
  if [[ -x "$v" ]] && "$v" -c 'import yaml' >/dev/null 2>&1; then PYBIN="$v"; break; fi
done
# Next, any venv Python even if we couldn't verify deps.
if [[ -z "$PYBIN" ]]; then
  for v in "$HERMES_ROOT/venv/bin/python" "$HERMES_ROOT/.venv/bin/python"; do
    if [[ -x "$v" ]]; then PYBIN="$v"; warn "Using $v (could not verify Hermes deps)"; break; fi
  done
fi
# Fall back to whatever python3 is on PATH.
if [[ -z "$PYBIN" ]]; then
  PYBIN="$(command -v python3 || true)"
  [[ -n "$PYBIN" ]] && warn "No Hermes venv found; using $PYBIN on PATH"
fi
[[ -n "$PYBIN" ]] || die "No Python 3 found. Install Python 3.11+ and retry."

PYVER="$("$PYBIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
ok "Python: $PYBIN (v$PYVER)"
"$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
  || die "Python 3.11+ required (found $PYVER). Use the Hermes venv or upgrade Python."

# ── 3. Install the bridge into that environment ──────────────────────────
say "Installing hermes-bridge (with QR support)..."
"$PYBIN" -m pip install --upgrade --quiet "$SCRIPT_DIR[qr]" \
  || die "pip install failed. Try: $PYBIN -m pip install -e \"$SCRIPT_DIR[qr]\""
ok "Installed into $PYBIN"

# ── 4. Preflight ─────────────────────────────────────────────────────────
say "Running environment check..."
if HERMES_AGENT_ROOT="$HERMES_ROOT" "$PYBIN" -m hermes_bridge.cli doctor; then
  echo
  ok "Hermes Bridge is ready."
  echo
  echo "  Start it:   $PYBIN -m hermes_bridge.cli start"
  echo "  Or:         hermes-bridge start    (if $(dirname "$PYBIN") is on your PATH)"
  echo "  Pair app:   hermes-bridge pair"
  echo
else
  die "Preflight failed — see messages above."
fi
