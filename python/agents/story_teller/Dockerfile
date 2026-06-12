# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM python:3.13-slim

# Prevents Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1

# Install uv for dependency management
RUN pip install --no-cache-dir uv==0.6.12

WORKDIR /app

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Copy only the dependency files to leverage Docker layer caching
COPY pyproject.toml uv.lock* ./

# Install project dependencies
RUN uv sync --frozen

# Install OpenTelemetry auto-instrumentation packages
RUN pip install --no-cache-dir opentelemetry-distro opentelemetry-exporter-otlp-proto-http openinference-instrumentation-google-adk

# Expose the port for ADK web UI
EXPOSE 8080

# Run with OpenTelemetry auto-instrumentation
CMD ["opentelemetry-instrument", "adk", "web", "--host", "0.0.0.0", "--port", "8080", "story_teller_agent"]
