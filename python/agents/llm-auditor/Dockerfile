FROM python:3.12-slim
WORKDIR /app
RUN pip install uv
COPY pyproject.toml .
COPY . .
RUN uv pip install --system --no-cache -e .
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8080", "/app"]