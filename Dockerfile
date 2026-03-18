FROM python:3.12-slim

WORKDIR /app

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

ENV PYTHONPATH=/app/src

EXPOSE 8888

CMD ["/bin/sh", "scripts/start.sh"]
