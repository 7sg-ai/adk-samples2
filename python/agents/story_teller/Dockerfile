FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[server]" google-adk
COPY story_teller_agent/ ./story_teller_agent/
ENV HOST=0.0.0.0
ENV PORT=8080
EXPOSE 8080
CMD ["uvicorn", "story_teller_agent.app:app", "--host", "0.0.0.0", "--port", "8080"]