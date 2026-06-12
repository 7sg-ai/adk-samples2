FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1
COPY . /app
RUN uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev
RUN uv sync --frozen --no-dev || uv sync --no-dev
ENV PATH="/app/.venv/bin:$PATH"
ENV GOOGLE_GENAI_USE_VERTEXAI=true
EXPOSE 8080
CMD ["uvicorn", "story_teller_agent.agent:app", "--host", "0.0.0.0", "--port", "8080"]