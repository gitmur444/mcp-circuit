# MCP Circuit agent container: Claude Code with an empty HOME, workspace mounted read-only at /work.
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 git jq ca-certificates curl procps less ripgrep file findutils \
    && rm -rf /var/lib/apt/lists/*
ARG CLAUDE_CODE_VERSION=2.1.295
RUN npm i -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force
USER node
ENV HOME=/home/agent
WORKDIR /work
