FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1
ADD . /app
RUN uv sync --no-dev || uv sync
ENV PATH="/app/.venv/bin:$PATH"
RUN uv pip install opentelemetry-distro opentelemetry-exporter-otlp-proto-http opentelemetry-instrumentation-google-genai openinference-instrumentation-google-adk
EXPOSE 8080
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8080", "/app"]
