# syntax=docker/dockerfile:1

# Build stage: resolve and install dependencies with uv. Nothing from here ships except /app.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml or uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage: no build tools, no uv, a non-root user.
FROM python:3.12-slim AS runtime
RUN useradd --system --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "standingproof.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
