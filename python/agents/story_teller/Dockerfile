FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml .
COPY uv.lock .
RUN uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev
RUN pip install opentelemetry-distro opentelemetry-exporter-otlp-proto-http openinference-instrumentation-google-adk
COPY story_teller_agent/ ./story_teller_agent/
ENV HOST=0.0.0.0
ENV PORT=8080
EXPOSE 8080
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8080", "/app"]
