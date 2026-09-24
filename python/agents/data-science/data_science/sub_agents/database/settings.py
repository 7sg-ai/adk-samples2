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

"""Environment-backed settings for the shared database agent."""

import os
import re

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip("'\"")


def bq_project_id() -> str:
    return env("BQ_DATA_PROJECT_ID") or env("GOOGLE_CLOUD_PROJECT")


def bq_dataset_id() -> str:
    return env("BQ_DATASET_ID")


def spanner_project_id() -> str:
    return env("SPANNER_PROJECT_ID") or env("GOOGLE_CLOUD_PROJECT")


def spanner_instance_id() -> str:
    return env("SPANNER_INSTANCE_ID")


def spanner_database_id() -> str:
    return env("SPANNER_DATABASE_ID")


def require_ident(value: str, label: str) -> str:
    """Reject anything that is not a bare SQL identifier."""
    if not value or not _IDENT.match(value):
        raise ValueError(
            f"{label} must be a bare identifier (letters, digits, underscore)."
        )
    return value
