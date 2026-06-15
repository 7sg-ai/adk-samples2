FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1
ENV UV_SYSTEM_PYTHON=1
ADD . /app
RUN uv pip install --system --no-cache \
    "google-adk>=1.18.0" \
    "google-genai>=1.9.0" \
    "google-cloud-aiplatform>=1.127.0" \
    "python-dotenv>=1.2.1" \
    "pyyaml>=6.0.3" \
    "tenacity>=9.1.2"
ENV PYTHONPATH="/app"
EXPOSE 8080
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8080", "/app"]