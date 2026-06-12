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

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1

ADD . /app

RUN uv sync --locked --no-install-project --no-dev
RUN uv sync --locked --no-dev

RUN uv add openinference-instrumentation-google-adk 2>/dev/null || true
RUN pip install opentelemetry-distro opentelemetry-exporter-otlp-proto-http

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

CMD ["opentelemetry-instrument", "adk", "web", "--host", "0.0.0.0", "--port", "8080"]
