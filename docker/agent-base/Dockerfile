# The default computer for `container` agents: Claude Code behind its ACP adapter and DeepSeek Harness, a non-root
# user, /work.
# Build: docker build -t valet/agent-base docker/agent-base
FROM node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates ripgrep curl \
 && rm -rf /var/lib/apt/lists/*

# Pinned adapter. Optional dependencies stay on: the Agent SDK ships Claude Code itself as a per-platform
# optional package.
RUN npm install -g @agentclientprotocol/claude-agent-acp@0.99.1 \
 && npm cache clean --force

# DeepSeek Harness, pinned: a developer preview whose extension contracts may still change. It speaks ACP itself
# (`dsh --profile acp`). Its own layer, so bumping it doesn't rebuild the Claude one.
RUN npm install -g @deepseek-ai/dsh@0.2.1-rc.5 \
 && npm cache clean --force

# The image's `node` user (uid 1000) becomes `agent`. Named volumes mounted on /home/agent and /work copy this
# ownership the first time they're used.
RUN usermod --login agent --home /home/agent --move-home node \
 && groupmod --new-name agent node \
 && mkdir -p /work \
 && chown agent:agent /home/agent /work

ENV HOME=/home/agent \
    CLAUDE_AGENT_ACP_PROTOCOL=v1
WORKDIR /work
USER agent
