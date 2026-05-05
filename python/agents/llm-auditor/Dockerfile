FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1

ADD . /app

RUN uv sync --locked --no-install-project --no-dev
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

ENTRYPOINT ["uv", "run", "adk", "web", "--host", "0.0.0.0", "--port", "8080"]
