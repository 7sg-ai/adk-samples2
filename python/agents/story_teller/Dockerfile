FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY README.md ./
COPY pyproject.toml ./
COPY requirements.txt ./
COPY story_teller_agent/ ./story_teller_agent/
RUN pip install --no-cache-dir -r requirements.txt
ENV GOOGLE_GENAI_USE_VERTEXAI=true
EXPOSE 8080
CMD ["uvicorn", "story_teller_agent.agent:app", "--host", "0.0.0.0", "--port", "8080"]