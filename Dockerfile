FROM python:3.12-slim AS base

# Secrets are injected at deploy time, never baked into the image
# (constraint C3).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY engine ./engine
COPY tools ./tools
COPY alembic.ini ./

RUN uv venv --python 3.12 && uv pip install -e .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 engine && chown -R engine:engine /app
USER engine

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD [".venv/bin/uvicorn", "engine.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
