
# syntax=docker/dockerfile:1.7

# ==============================================================================
# Stage 1: Builder - Install locked production dependencies using uv
# ==============================================================================

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Copy dependency metadata first for better layer caching
COPY pyproject.toml uv.lock .python-version ./

# Install production dependencies only
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project


# ==============================================================================
# Stage 2: Test - Includes development dependencies and test suite
# Build: docker build --target test -t ric-backend:test .
# ==============================================================================

FROM builder AS test

# Install development dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Copy application and test files
COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
COPY seed ./seed
COPY tests ./tests

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["pytest", "-v"]


# ==============================================================================
# Stage 3: Runtime - Minimal hardened production image
# Default build target
# ==============================================================================

FROM python:3.15-slim-bookworm AS runtime

# Runtime environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    ENVIRONMENT=production \
    LOG_FORMAT=json \
    DEBUG=false \
    DOCS_ENABLED=false

WORKDIR /app

# Create dedicated non-root user and group
RUN groupadd --gid 10001 appgroup \
    && useradd \
        --uid 10001 \
        --gid appgroup \
        --shell /usr/sbin/nologin \
        --no-create-home \
        appuser

# Copy production virtual environment
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv

# Copy runtime application files only
COPY --chown=appuser:appgroup pyproject.toml alembic.ini ./
COPY --chown=appuser:appgroup app ./app
COPY --chown=appuser:appgroup migrations ./migrations
COPY --chown=appuser:appgroup scripts ./scripts
COPY --chown=appuser:appgroup seed ./seed

# Run as non-root user
USER appuser

# FastAPI application port
EXPOSE 8000

# Health check using Python standard library
HEALTHCHECK --interval=30s \
    --timeout=5s \
    --start-period=15s \
    --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# Default FastAPI application entrypoint
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
