# syntax=docker/dockerfile:1
FROM python:3.11-slim

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Chromium system dependencies for Debian 12 (Bookworm) ────────────────────
# `playwright install --with-deps` hardcodes old package names like
# ttf-unifont / ttf-ubuntu-font-family that were removed in Debian 12.
# We install the correct packages manually and skip --with-deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core Chromium runtime libs
    libnss3 libnspr4 libdbus-1-3 libglib2.0-0 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libatspi2.0-0 libx11-6 libxcb1 libxext6 \
    libx11-xcb1 libxcursor1 libxi6 libxtst6 \
    # Audio (Debian 12 renamed libasound2 → libasound2t64)
    libasound2t64 \
    # Fonts (correct Debian 12 package names)
    fonts-liberation fonts-unifont \
    # Build tools
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies (layer-cached separately from source) ─────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Install Playwright Chromium (no --with-deps; deps installed above) ────────
RUN playwright install chromium

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# ── Start: run DB migrations then launch the server ─────────────────────────
# Use ; (not &&) so uvicorn always starts even if alembic has nothing to do.
# Railway injects $PORT; default to 8000 for local runs.
CMD ["sh", "-c", "echo 'Starting on port '${PORT:-8000} && alembic upgrade head; echo 'Alembic done, starting uvicorn' && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
