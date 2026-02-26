FROM python:3.12-slim

WORKDIR /app

# Install uv for fast, reproducible installs from uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Python dependencies (separate layer — only re-runs when
# pyproject.toml or uv.lock changes, not on every source edit)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# Copy source code
COPY curator/ ./curator/
COPY curator_query.py mcp_server.py ./

# Persistent state directories (both embedded and HTTP mode need curated/)
RUN mkdir -p /app/data/ov /app/data/curated
ENV CURATOR_DATA_PATH=/app/data
ENV OV_DATA_PATH=/app/data/ov
ENV CURATOR_CURATED_DIR=/app/data/curated
VOLUME /app/data

# Default: MCP server (stdio JSON-RPC).
# Override for CLI:  docker compose run --rm curator curator_query.py "question"
ENTRYPOINT ["uv", "run", "python"]
CMD ["mcp_server.py"]
