# Multi-stage Dockerfile for RdioCallsAPI
ARG PYTHON_IMAGE=python:3.11.16-alpine3.24@sha256:6857d2dae63e052057f2db389a7061188ac9a92a3fa8d402bde68f36df6fada1

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

# Apply published base-image fixes, then remove packaging tools that are not
# needed after the wheel is installed in the builder. Keep application code
# root-owned and grant the service account access only to mutable state.
RUN apk upgrade --no-cache && \
    rm -rf \
        /usr/local/lib/python3.11/ensurepip \
        /usr/local/lib/python3.11/site-packages/_distutils_hack \
        /usr/local/lib/python3.11/site-packages/pip* \
        /usr/local/lib/python3.11/site-packages/pkg_resources \
        /usr/local/lib/python3.11/site-packages/setuptools* \
        /usr/local/lib/python3.11/site-packages/wheel* \
        /usr/local/bin/pip* \
        /usr/local/bin/wheel && \
    addgroup -S -g 1000 rdio && \
    adduser -S -D -H -u 1000 -G rdio -s /sbin/nologin rdio && \
    mkdir -p /app/data /app/logs && \
    chown rdio:rdio /app/data /app/logs && \
    chmod 0755 /app && \
    chmod 0700 /app/data /app/logs

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
RUN mkdir -p data/audio data/temp logs && \
    chmod 0700 data/audio data/temp logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-m", "src.healthcheck", "--config", "/app/config/config.yaml"]

# Expose port
EXPOSE 8080

# Run application
CMD ["sdrtrunk-rdio-api", "serve", "--host", "0.0.0.0"]
