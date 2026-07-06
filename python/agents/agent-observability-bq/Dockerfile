FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip
COPY . .
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; \
    elif [ -f pyproject.toml ]; then pip install --no-cache-dir .; fi
RUN python -c "import google.adk" 2>/dev/null || pip install --no-cache-dir google-adk

EXPOSE 8080
CMD ["sh", "-c", "adk web --host 0.0.0.0 --port ${PORT:-8080}"]
