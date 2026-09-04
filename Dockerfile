FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /uvx /bin/

# Install locked production dependencies before application source so Docker can
# reuse this layer when only the application changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini gunicorn_conf.py ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY app ./app

# Run as non-privileged service user
RUN useradd -u 10001 -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health/live || exit 1

CMD ["/app/.venv/bin/gunicorn", "-c", "gunicorn_conf.py", "app.main:app"]
