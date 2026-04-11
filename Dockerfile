# syntax=docker/dockerfile:1
FROM python:3.11-slim

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── System packages needed by Playwright / asyncpg build ─────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies (layer-cached separately from source) ─────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Install Playwright Chromium + all required Linux system libraries ─────────
# --with-deps installs libnss3, libgtk-3-0, libgbm1, etc. automatically
RUN playwright install chromium --with-deps

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# ── Port (Railway injects $PORT at runtime) ───────────────────────────────────
EXPOSE 8000

# ── Start: run DB migrations then launch the server ──────────────────────────
# Shell form so ${PORT:-8000} expansion works
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
