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
from typing import Literal

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip("'\"")


def bq_project_id() -> str:
    return env("BQ_DATA_PROJECT_ID") or env("GOOGLE_CLOUD_PROJECT")


def bq_dataset_id() -> str:
    return env("BQ_DATASET_ID")


def configured_model(*names: str) -> str:
    """Model id from the WDF env, then Vertex Gemini 2.5 Flash."""
    for name in (*names, "ROOT_AGENT_MODEL"):
        value = env(name)
        if value:
            return value
    return "gemini-2.5-flash"


def qualify_bigquery_sql(sql: str, dataset: str, project: str = "") -> str:
    """Prefix bare table names with the configured BigQuery dataset."""
    dataset = (dataset or "").strip()
    project = (project or "").strip()
    if not dataset or not (sql or "").strip():
        return sql
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return sql
    cte_names = {
        cte.alias_or_name
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }
    for table in tree.find_all(exp.Table):
        if not table.name or table.name in cte_names:
            continue
        if table.args.get("db") or table.args.get("catalog"):
            continue
        table.set("db", exp.to_identifier(dataset))
        if project:
            table.set("catalog", exp.to_identifier(project))
    return tree.sql(dialect="bigquery")


def spanner_project_id() -> str:
    return env("SPANNER_PROJECT_ID") or env("GOOGLE_CLOUD_PROJECT")


def spanner_instance_id() -> str:
    return env("SPANNER_INSTANCE_ID")


def spanner_database_id() -> str:
    return env("SPANNER_DATABASE_ID")


def graph_source() -> Literal["spanner", "neo4j"]:
    value = env("GRAPH_SOURCE", "spanner").lower()
    if value not in {"spanner", "neo4j"}:
        raise ValueError("GRAPH_SOURCE must be spanner or neo4j.")
    return value


def neo4j_uri() -> str:
    return env("NEO4J_URI")


def neo4j_user() -> str:
    return env("NEO4J_USER", "neo4j")


def require_ident(value: str, label: str) -> str:
    """Reject anything that is not a bare SQL identifier."""
    if not value or not _IDENT.match(value):
        raise ValueError(
            f"{label} must be a bare identifier (letters, digits, underscore)."
        )
    return value
