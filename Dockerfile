# Multi-stage Dockerfile for RdioCallsAPI
ARG PYTHON_IMAGE=python:3.11.16-slim-trixie@sha256:1042b61448fef4ba92d16a8c7eb4996d027568ce64792a7877fd88511e0af7c6

# Keep the dependency installer out of the runtime image. Both this digest and
# PYTHON_IMAGE are multi-platform image-index digests.
FROM ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 AS uv

# Stage 1: Builder
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

# Defense in depth for every project-aware uv command in this stage. Individual
# sync commands also pass --locked explicitly below.
ENV UV_LOCKED=1 \
    UV_NO_BUILD_ISOLATION=1

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install runtime and locked build dependencies. The build group is removed
# again before the environment is copied into the runtime stage.
RUN uv sync --locked --no-build --no-dev --group build --no-install-project

# Copy source code
COPY src/ ./src/
COPY cli.py ./
COPY README.md ./
COPY LICENSE ./

# Build with the audited backend already installed, prune build-only tools, and
# install the resulting wheel without consulting a package index.
RUN --network=none \
    uv build --no-build-isolation --no-create-gitignore --python /app/.venv/bin/python --wheel --out-dir /app/dist && \
    uv sync --locked --no-build --no-dev --no-install-project && \
    uv pip install --python /app/.venv/bin/python --no-deps --no-index /app/dist/*.whl

# Stage 2: Runtime
FROM ${PYTHON_IMAGE} AS runtime

# Keep application code root-owned and grant the service account access only to
# the state directories it needs to modify.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin rdio && \
    install -d -o root -g root -m 0755 /app && \
    install -d -o rdio -g rdio -m 0700 /app/data /app/logs

# Set working directory
WORKDIR /app

# Copy only the installed runtime environment. Application code comes from the
# locally built wheel; uv and all build/development dependencies stay behind.
COPY --from=builder /app/.venv /app/.venv

# Copy configuration
COPY --chown=rdio:rdio --chmod=0600 config/config.example.yaml /app/config/config.yaml

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH" \
    TMPDIR="/app/data/temp" \
    SQLITE_TMPDIR="/tmp" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER rdio

# Create data directories
RUN install -d -m 0700 data/audio data/temp logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-m", "src.healthcheck", "--config", "/app/config/config.yaml"]

# Expose port
EXPOSE 8080

# Run application
CMD ["sdrtrunk-rdio-api", "serve", "--host", "0.0.0.0"]
