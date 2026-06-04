# Hermes Bridge — derived image
#
# This builds ON TOP of the official Hermes Agent image rather than reinventing
# the runtime. Hermes already ships a Python venv (via uv) with fastapi,
# uvicorn, sse-starlette, pydantic and httpx — every dependency the bridge
# needs — so we just install the bridge package into that same venv and wire
# it in as an s6-supervised service alongside the gateway and dashboard.
#
# Why derive instead of reinstall:
#   • No second venv, no duplicate dependency tree, no first-boot install.
#   • Inherits Hermes' methodology wholesale: HERMES_HOME=/opt/data, UID/GID
#     remap, s6 supervision, secrets-stay-in-the-mounted-volume.
#   • The bridge shares config/keys/hermes.db with the gateway automatically.
#
# Pin the base tag to match your installed Hermes version for reproducibility.
ARG HERMES_IMAGE=nousresearch/hermes-agent:latest
FROM ${HERMES_IMAGE}

# Install the bridge into Hermes' existing venv with --no-deps: every runtime
# dependency is already present in the base image's [all] extra, so this is a
# fast egg-install with no resolution or downloads. Keeping --no-deps also
# guarantees we never accidentally upgrade a Hermes-pinned package.
USER root
COPY pyproject.toml README.md /opt/bridge/
COPY hermes_bridge /opt/bridge/hermes_bridge
RUN /opt/hermes/.venv/bin/pip install --no-cache-dir --no-deps /opt/bridge

# Register the bridge as an s6 service (mirrors the dashboard wiring). It's
# gated by HERMES_BRIDGE — unset means the slot stays down, exactly like the
# dashboard service. See docker/s6-rc.d/bridge/.
COPY docker/s6-rc.d/bridge /etc/s6-overlay/s6-rc.d/bridge
COPY docker/s6-rc.d/user/contents.d/bridge /etc/s6-overlay/s6-rc.d/user/contents.d/bridge

# Inherit the base image's ENTRYPOINT (/init + main-wrapper.sh). The gateway
# remains the container's main program; the bridge runs as a supervised
# side-service. No CMD/ENTRYPOINT override needed.
