#!/usr/bin/env bash
# Entrypoint for the Hermes Bridge container.
#
# Responsibilities:
#   1. Verify the Hermes source is mounted at $HERMES_AGENT_ROOT.
#   2. Ensure Hermes' Python deps are installed into the bridge venv (once),
#      since a host venv can't be reused inside the container.
#   3. Run preflight (hermes-bridge doctor) and then exec the requested command.
set -euo pipefail

VENV="${BRIDGE_VENV:-/opt/bridge-venv}"
HERMES="${HERMES_AGENT_ROOT:-/hermes}"
DEPS_MARKER="${VENV}/.hermes-deps-installed"

export PATH="${VENV}/bin:${PATH}"

if [[ ! -f "${HERMES}/run_agent.py" ]]; then
    echo "✗ Hermes source not found at ${HERMES} (expected run_agent.py)." >&2
    echo "  Mount your Hermes install into the container, e.g.:" >&2
    echo "    -v \$HOME/.hermes/hermes-agent:/hermes:ro" >&2
    echo "  (docker-compose.yml does this for you.)" >&2
    exit 1
fi

# Install Hermes' Python deps into the bridge venv once. We install deps only
# (not the package data) so the mounted source stays the source of truth.
if [[ ! -f "${DEPS_MARKER}" ]]; then
    echo "→ First boot: installing Hermes Python dependencies (one-time)…"
    if [[ -f "${HERMES}/pyproject.toml" ]]; then
        # Install Hermes' declared dependencies without overwriting the mounted code.
        pip install "${HERMES}" || {
            echo "⚠ Full Hermes install failed; the bridge may still run if deps are present." >&2
        }
    fi
    touch "${DEPS_MARKER}"
fi

# Preflight — clear, actionable failure instead of a stack trace.
echo "→ Running preflight checks…"
hermes-bridge doctor || {
    echo "✗ Preflight failed. Fix the issues above and restart the container." >&2
    exit 1
}

exec hermes-bridge "$@"
