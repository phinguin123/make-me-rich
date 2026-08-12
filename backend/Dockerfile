# ─────────────────────────────────────────────────────────────────────────────
# Backend Dockerfile — Python 3.12 slim, two build targets:
#   development  → uvicorn --reload, source mounted via docker-compose volume
#   production   → frozen copy of source, no dev tools
# ─────────────────────────────────────────────────────────────────────────────

# ── Shared base ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

# System deps needed by some pip packages (e.g. uvloop's C extension).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps in a separate layer so Docker caches them between
# code-only rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Development target ────────────────────────────────────────────────────────
FROM base AS development
# Source is bind-mounted at runtime; nothing to COPY here.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ── Production target ─────────────────────────────────────────────────────────
FROM base AS production
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
# Workers=1 keeps the shared in-memory client set consistent.
# Use Gunicorn + UvicornWorker for multi-process production deployments.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
