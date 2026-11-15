# syntax=docker/dockerfile:1

# Build stage: resolve and install dependencies with uv. Nothing from here ships except /app.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml or uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Fetch the local embedding model now, so the running container never downloads anything.
# Before the source copy would cache better, but the model name lives in the source.
RUN /app/.venv/bin/python -c "from fastembed import TextEmbedding; from standingproof.library.embedding import DEFAULT_MODEL; TextEmbedding(DEFAULT_MODEL, cache_dir='/app/models')"

# Runtime stage: no build tools, no uv, a non-root user.
FROM python:3.13-slim AS runtime
RUN useradd --system --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --from=build --chown=app:app /app/models /app/models
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./
USER app
# HF_HUB_OFFLINE: use the model file baked in above and never contact the model hub.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SP_EMBEDDING_CACHE_DIR=/app/models HF_HUB_OFFLINE=1
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "standingproof.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
