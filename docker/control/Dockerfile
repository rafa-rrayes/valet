# The control plane (valet-control): the API, the web UI, the MCP gateway, and the browser profiles' Chrome, headed on
# a virtual display (sites trust it more than a headless one). Build from the repository's root:
#
#   docker build -f docker/control/Dockerfile -t valet/control .
#
# amd64 and arm64 alike: Chrome is Playwright's Chromium (one install for both), at the version the
# pinned Playwright drives. Agents don't run here: a runner does (docker/valet-runner.service), and its agent image is
# docker/agent-base.
FROM node:24-bookworm-slim AS node

FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git tini tzdata xvfb xauth \
        fonts-liberation fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# Node 24 (node, npm, npx): stdio MCP servers in mcp.yaml run here, often as `npx -y <server>`.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# Chromium and the system libraries it needs (its own layer: it changes only with Playwright).
RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /opt/playwright
COPY valet ./valet
RUN uv sync --frozen --no-dev && rm /usr/local/bin/uv

# A non-root user (uid 1000, the usual first user, so a bind-mounted /data owned by it just works). Xvfb
# needs /tmp/.X11-unix made by root. A new named volume on /data takes this ownership the first time it's used.
COPY docker/control/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh \
 && useradd --create-home --uid 1000 --shell /bin/sh valet \
 && mkdir -p /data /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix && chown valet:valet /data

# Headed Chrome on Xvfb :99 (the entrypoint starts it). Without its own sandbox: Docker blocks the user namespaces it
# needs, so the container is the boundary.
ENV VALET_DATA=/data VALET_HOST=0.0.0.0 VALET_PORT=8780 DISPLAY=:99 VALET_HEADLESS=0 VALET_CHROME_NO_SANDBOX=1
USER valet
EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/me' % os.environ.get('VALET_PORT', '8780'), timeout=5)"
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["valet-control", "serve"]
