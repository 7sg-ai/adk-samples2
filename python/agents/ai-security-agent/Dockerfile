FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1
RUN touch README.md
ADD . /app
RUN uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev
RUN uv sync --frozen --no-dev || uv sync --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8080", "/app"]