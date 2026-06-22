FROM python:3.12-slim

# Prevents Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

WORKDIR /app

# Upgrade pip and install uv
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "uv>=0.7.19"

# Copy only the dependency files first to leverage Docker layer caching
COPY pyproject.toml uv.lock ./

# Install project dependencies, synced to uv's lockfile
RUN uv sync --frozen --no-install-project

# Copy the rest of the source
COPY . .

# Install the project itself
RUN uv sync --frozen

# Expose the ADK web UI port
EXPOSE 8080

CMD ["uv", "run", "adk", "web", "--host", "0.0.0.0", "--port", "8080"]
