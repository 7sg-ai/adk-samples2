# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain the copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM python:3.11-slim

RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock* ./

COPY ./blogger_agent ./blogger_agent

RUN uv add openinference-instrumentation-google-adk 2>/dev/null || true
RUN uv sync

# Install OpenTelemetry auto-instrumentation dependencies
RUN pip install --no-cache-dir opentelemetry-distro opentelemetry-exporter-otlp-proto-http

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

EXPOSE 8080

CMD ["opentelemetry-instrument", "uv", "run", "adk", "web", "--host", "0.0.0.0", "--port", "8080"]
