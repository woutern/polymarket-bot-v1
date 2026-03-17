FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml ./

# Install dependencies
RUN uv pip install --system -e .
ENV PYTHONPATH=/app/src

# Copy source
COPY src/ src/
COPY scripts/ scripts/

# Run the bot
CMD ["python", "scripts/run.py"]
