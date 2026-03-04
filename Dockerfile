# =============================================================================
# Full-Self-Crawl-Agent Alpha — Dockerfile (multi-stage)
#
# Uses local crawl-agent:latest as base (has Python 3.12 + Playwright + Chromium).
# If unavailable, change to: python:3.12-bookworm and uncomment system deps.
# =============================================================================

# ---- Stage 1: base (dependency layer) ----
FROM crawl-agent:latest AS base

USER root

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DOCKER_CONTAINER=1

WORKDIR /app

# Alpha project dependencies (some already in base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true

# Workspace for agent file I/O
RUN mkdir -p /workspace/artifacts/data /workspace/artifacts/files /workspace/tmp

# ---- Stage 2: dev (source mounted via volume) ----
FROM base AS dev

RUN pip install --no-cache-dir pytest pytest-asyncio pytest-cov black ruff

RUN useradd -m -s /bin/bash crawler \
    && chown -R crawler:crawler /app /workspace

ENV BROWSER_HEADLESS=true

CMD ["bash"]

# ---- Stage 3: production (code baked into image) ----
FROM base AS production

COPY src/ /app/src/
COPY config/ /app/config/
COPY pyproject.toml /app/

RUN useradd -m -s /bin/bash crawler \
    && chown -R crawler:crawler /app /workspace /home/crawler

# Copy Playwright browser cache to crawler user
RUN cp -r /root/.cache /home/crawler/.cache 2>/dev/null || true \
    && chown -R crawler:crawler /home/crawler/.cache 2>/dev/null || true

USER crawler

ENV BROWSER_HEADLESS=true

ENTRYPOINT ["python", "-m", "src.main"]
