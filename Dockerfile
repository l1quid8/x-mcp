FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends git gosu \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.22
WORKDIR /opt/x-mcp
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY third_party ./third_party
RUN uv sync --locked --no-dev
RUN useradd --system --uid 10002 --home-dir /var/lib/x-mcp --shell /usr/sbin/nologin xmcp
COPY docker/app-entrypoint.sh /usr/local/bin/x-mcp-entrypoint
RUN chmod 0555 /usr/local/bin/x-mcp-entrypoint
ENV X_MCP_STATE_DIR=/var/lib/x-mcp/state \
    X_MCP_READER_STATE_DIR=/var/lib/x-mcp/reader \
    CREDENTIALS_DIRECTORY=/var/lib/x-mcp/keys \
    X_MCP_HOST=0.0.0.0
EXPOSE 8770
ENTRYPOINT ["/usr/local/bin/x-mcp-entrypoint"]
