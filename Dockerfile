FROM python:3.12-slim

WORKDIR /app

# LightGBM requires libgomp, Node.js for auto-claim script
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for cache efficiency during development builds
COPY pyproject.toml uv.lock ./

# Create a minimal src stub so uv can resolve the editable install,
# then install all production dependencies without dev extras and no cache.
RUN mkdir -p src/polybot && touch src/polybot/__init__.py && \
    uv sync --no-dev --no-cache

# Copy source and entrypoint (overwrites the stub)
COPY src/ src/
COPY scripts/ scripts/

# Install Node.js dependencies for auto-claim
COPY package.json package-lock.json ./
RUN npm ci --production 2>/dev/null || npm install --production

ENV PYTHONPATH=/app/src

EXPOSE 8888

# Watchdog: if heartbeat file older than 5 min → unhealthy → ECS restarts
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python3 -c "import os,time; f='/tmp/heartbeat'; exit(0 if os.path.exists(f) and time.time()-float(open(f).read())<300 else 1)"

CMD ["/bin/sh", "scripts/start.sh"]
