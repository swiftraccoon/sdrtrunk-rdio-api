# Multi-stage Dockerfile for RdioCallsAPI
# Stage 1: Builder
FROM python:3.13-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-install-project

# Copy source code
COPY src/ ./src/
COPY cli.py ./
COPY README.md ./

# Install project
RUN uv sync --frozen

# Stage 2: Runtime
FROM python:3.13-slim

# Create non-root user
RUN useradd -m -u 1000 rdio && \
    mkdir -p /app/data /app/logs && \
    chown -R rdio:rdio /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy uv and virtual environment from builder
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
COPY --from=builder --chown=rdio:rdio /app/.venv /app/.venv
COPY --from=builder --chown=rdio:rdio /app/src /app/src
COPY --from=builder --chown=rdio:rdio /app/cli.py /app/cli.py

# Copy configuration
COPY --chown=rdio:rdio config/config.example.yaml /app/config/config.yaml

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER rdio

# Create data directories
RUN mkdir -p data/audio data/temp logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; exit(0 if requests.get('http://localhost:8000/health').status_code == 200 else 1)"

# Expose port
EXPOSE 8000

# Run application
CMD ["uv", "run", "python", "cli.py", "serve"]