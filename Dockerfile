# Hermes Bridge API — container image
#
# IMPORTANT: the bridge cannot run standalone. It imports a Hermes Agent install
# at runtime. This image therefore uses a "bring your own Hermes" model:
#
#   • The Hermes source is MOUNTED at runtime (not baked in) → see compose file.
#   • Your API keys live in ~/.hermes/.env on the host, also mounted at runtime,
#     so NO secrets are ever written into the image layers.
#
# A macOS/Windows virtualenv can't be reused inside this Linux container (native
# wheels differ), so the entrypoint installs Hermes' Python deps against the
# mounted source on first boot into a container-local venv that persists in a
# named volume.

FROM python:3.11-slim

# System deps: git (some Hermes deps pull from git), build tools for native wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HERMES_AGENT_ROOT=/hermes \
    BRIDGE_VENV=/opt/bridge-venv

WORKDIR /app

# Install the bridge itself (cached unless bridge source changes).
COPY pyproject.toml README.md ./
COPY hermes_bridge ./hermes_bridge
RUN python -m venv "$BRIDGE_VENV" \
    && "$BRIDGE_VENV/bin/pip" install --upgrade pip \
    && "$BRIDGE_VENV/bin/pip" install ".[qr]"

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8765

# Healthcheck hits the bridge's own health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8765/api/v1/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["start", "--host", "0.0.0.0", "--port", "8765", "--foreground"]
