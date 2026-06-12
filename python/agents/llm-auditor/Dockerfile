# Use a Python image with uv pre-installed
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Install the project into `/app`
WORKDIR /app

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1

ADD . /app

# Install the project's dependencies using the lockfile and settings
RUN uv sync --locked --no-install-project --no-dev

# Then, add the rest of the project source code and install it
# Installing separately from its dependencies allows optimal layer caching
RUN uv sync --locked --no-dev

# Install OpenTelemetry auto-instrumentation packages
# opentelemetry-instrument CLI needs pip to create the entry point in PATH
RUN pip install --no-cache-dir opentelemetry-distro opentelemetry-exporter-otlp-proto-http openinference-instrumentation-google-adk

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Expose the port
EXPOSE 8080

# Run adk web with OTEL auto-instrumentation for automatic trace capture
CMD ["uv", "run", "adk", "web", "--host", "0.0.0.0", "--port", "8080"]
