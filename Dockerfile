ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# A venv copies cleanly into the runtime stage without dragging pip's build
# leftovers along.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /src

# Dependencies first so source edits do not invalidate this layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

FROM python:${PYTHON_VERSION}-slim

# Filled in by the CI action (inpacken-un-af-dor-mit).
ARG VERSION=dev
ARG COMMIT=unknown

LABEL org.opencontainers.image.title="comfyui-mcp-server" \
      org.opencontainers.image.description="MCP server and REST API that serve ComfyUI workflows as tools" \
      org.opencontainers.image.source="https://github.com/hauke-cloud/comfyui-mcp-server" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.licenses="MIT"

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
COPY workflows /app/workflows

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    WORKFLOWS_DIR=/app/workflows

WORKDIR /app
EXPOSE 8000
USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

# uvicorn handles SIGTERM itself, so no init process is needed as PID 1.
CMD ["comfyui-mcp-server"]
