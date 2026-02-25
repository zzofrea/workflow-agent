FROM node:20-slim

# System deps: Python, psql client, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        postgresql-client \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code@latest

# Create agent directories and auth staging area owned by node user
RUN mkdir -p /agent/input /agent/output /agent/auth && chown -R node:node /agent

# Copy the entrypoint
COPY --chown=node:node entrypoint.py /agent/entrypoint.py

# Run as non-root node user
USER node
WORKDIR /agent
ENTRYPOINT ["python3", "/agent/entrypoint.py"]
