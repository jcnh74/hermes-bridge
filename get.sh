#!/usr/bin/env bash
#
# Hermes Bridge — remote bootstrap installer.
#
# The one-liner behind https://get.agentfy.app :
#
#     curl -fsSL https://get.agentfy.app | bash
#
# Clones (or updates) the Hermes Bridge repo, then runs install.sh, which
# installs the bridge into the same Python environment that runs Hermes Agent.
# Safe to re-run (idempotent).
#
# Overrides (env vars):
#   BRIDGE_REPO   git URL to clone           (default: jcnh74/hermes-bridge)
#   BRIDGE_REF    branch/tag/commit          (default: main)
#   BRIDGE_DIR    where to clone             (default: ~/.hermes/hermes-bridge-src)
#
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
say()  { echo -e "${BLUE}▶${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

REPO="${BRIDGE_REPO:-https://github.com/jcnh74/hermes-bridge.git}"
REF="${BRIDGE_REF:-main}"
DIR="${BRIDGE_DIR:-$HOME/.hermes/hermes-bridge-src}"

echo
echo "  Hermes Bridge installer"
echo "  ───────────────────────"
echo

# ── 0. Prerequisites ─────────────────────────────────────────────────────
command -v git >/dev/null 2>&1 || die "git is required. Install it and retry."

# ── 1. Clone or update the repo ──────────────────────────────────────────
if [[ -d "$DIR/.git" ]]; then
  say "Updating existing checkout at $DIR ..."
  git -C "$DIR" fetch --quiet origin "$REF" || die "git fetch failed"
  git -C "$DIR" checkout --quiet "$REF"     || die "git checkout $REF failed"
  git -C "$DIR" reset --hard --quiet "origin/$REF" 2>/dev/null \
    || git -C "$DIR" reset --hard --quiet "$REF"
  ok "Updated to latest $REF"
else
  say "Cloning $REPO ($REF) ..."
  mkdir -p "$(dirname "$DIR")"
  git clone --quiet --branch "$REF" --depth 1 "$REPO" "$DIR" \
    || git clone --quiet "$REPO" "$DIR" \
    || die "git clone failed"
  ok "Cloned to $DIR"
fi

# ── 2. Hand off to the repo's installer ──────────────────────────────────
[[ -f "$DIR/install.sh" ]] || die "install.sh not found in $DIR"
say "Running install.sh ..."
echo
bash "$DIR/install.sh"
