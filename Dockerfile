# Multi-stage build for Stock Watcher
# Stage 1: Build frontend (Node.js + Vite)
FROM node:22-bookworm-slim AS frontend

WORKDIR /src/frontend

# Copy package files first for better layer caching
COPY frontend/package.json frontend/package*.json* ./

# Install dependencies
RUN npm ci

# Copy rest of frontend source
COPY frontend/ .

# Build the SPA
RUN npm run build


# Stage 2: Runtime (Python + FastAPI + Playwright)
FROM python:3.12-slim-bookworm

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data \
    STATIC_DIR=/app/static

WORKDIR /app

# Copy backend requirements
COPY backend/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

# Copy backend source code
COPY backend/ .

# Copy built frontend (SPA) to serve from FastAPI
COPY --from=frontend /src/frontend/dist ./static

# Build arguments and labels
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

LABEL org.opencontainers.image.title="Stock Watcher" \
      org.opencontainers.image.description="Self-hosted restock alert watcher with ntfy notifications" \
      org.opencontainers.image.source="https://github.com/loswastaken/stock-watcher"

# Volume for persistent data (SQLite + images)
VOLUME /data

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health').read()" || exit 1

# Start FastAPI with uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--workers", "1"]
