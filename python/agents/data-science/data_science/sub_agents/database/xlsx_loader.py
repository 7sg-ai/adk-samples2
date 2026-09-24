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

"""Load a declared .xlsx workbook into one Spanner Graph schema.

The model never emits DDL. This module builds CREATE TABLE / CREATE PROPERTY
GRAPH statements and runs them through the Spanner Python client. Edges are
created only from a mapping the caller declared; they are not inferred.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import date, datetime
from typing import Any

import pandas as pd
from google.cloud import spanner
from google.cloud.spanner_v1 import param_types

from data_science.sub_agents.database import settings

CATALOG = "workbook_catalog"
MAX_STRING = 1024
MAX_ROWS = 20000
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_READ_ONLY = re.compile(r"^\s*(SELECT|WITH|GRAPH)\b", re.IGNORECASE | re.DOTALL)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|MERGE|CALL|GRANT|REVOKE|"
    r"TRUNCATE|EXPORT|LOAD)\b",
    re.IGNORECASE,
)


def guard_sql(sql: str) -> str:
    """Allow a single read-only SELECT, WITH, or GRAPH statement."""
    text = sql.strip().rstrip(";")
    if not _READ_ONLY.match(text) or _FORBIDDEN.search(text) or ";" in text:
        raise ValueError(
            "Only a single read-only SELECT, WITH, or GRAPH statement is allowed."
        )
    return text


def ident(value: str, label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", str(value).strip())
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"c_{cleaned}"
    if not _IDENT.match(cleaned):
        raise ValueError(f"{label} {value!r} is not a usable identifier.")
    return cleaned


def graph_name_for(workbook_name: str) -> str:
    digest = hashlib.sha1(workbook_name.encode("utf-8")).hexdigest()[:8]
    stem = ident(workbook_name.rsplit(".", 1)[0], "workbook")
    return f"g_{stem[:40]}_{digest}"


def spanner_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "BOOL"
    if pd.api.types.is_integer_dtype(series):
        return "INT64"
    if pd.api.types.is_float_dtype(series):
        return "FLOAT64"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMP"
    return f"STRING({MAX_STRING})"


def cell(value: Any, column_type: str) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if column_type == "BOOL":
        return bool(value)
    if column_type == "INT64":
        return int(value)
    if column_type == "FLOAT64":
        return float(value)
    if column_type == "TIMESTAMP":
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        return pd.to_datetime(value).to_pydatetime()
    text = value if isinstance(value, str) else str(value)
    return text[:MAX_STRING]


def database():
    project = settings.spanner_project_id()
    instance_id = settings.spanner_instance_id()
    database_id = settings.spanner_database_id()
    if not project or not instance_id or not database_id:
        raise ValueError(
            "SPANNER_PROJECT_ID, SPANNER_INSTANCE_ID, and "
            "SPANNER_DATABASE_ID must be set. The instance and database "
            "must already exist; this agent does not provision them."
        )
    client = spanner.Client(project=project)
    return client.instance(instance_id).database(database_id)
def ensure_catalog(db) -> None:
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {CATALOG} (
      graph_name STRING(128) NOT NULL,
      workbook_name STRING(256) NOT NULL,
      mapping_json STRING(MAX) NOT NULL,
      loaded_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
    ) PRIMARY KEY (graph_name)
    """
    db.update_ddl([ddl]).result(timeout=120)


def _column_plan(frame: pd.DataFrame) -> list[dict[str, str]]:
    return [
        {
            "sheet_column": str(column),
            "name": ident(column, "column"),
            "type": spanner_type(frame[column]),
        }
        for column in frame.columns
    ]


def _node_plan(sheets: dict[str, pd.DataFrame], spec: dict, name: str) -> dict:
    sheet = spec["sheet"]
    id_column = spec["id_column"]
    if sheet not in sheets:
        raise ValueError(f"Node sheet {sheet!r} is not in the workbook.")
    frame = sheets[sheet].dropna(how="all")
    if id_column not in frame.columns:
        raise ValueError(f"Node sheet {sheet!r} has no id column {id_column!r}.")
    if len(frame) > MAX_ROWS:
        raise ValueError(f"Sheet {sheet!r} has {len(frame)} rows; limit is {MAX_ROWS}.")
    return {
        "sheet": sheet,
        "id_column": id_column,
        "id_name": ident(id_column, "id column"),
        "table": ident(f"{name}_{sheet}", "node table"),
        "label": ident(sheet, "label"),
        "columns": _column_plan(frame),
        "frame": frame,
    }


def _edge_plan(sheets, edge_sheet, node_plans, name: str) -> dict | None:
    if not edge_sheet:
        return None
    sheet = edge_sheet["sheet"]
    if sheet not in sheets:
        raise ValueError(f"Edge sheet {sheet!r} is not in the workbook.")
    frame = sheets[sheet].dropna(how="all")
    for key in ("source_column", "target_column", "source_node", "target_node"):
        if key not in edge_sheet:
            raise ValueError(f"Edge mapping is missing {key}.")
    by_sheet = {plan["sheet"]: plan for plan in node_plans}
    if edge_sheet["source_node"] not in by_sheet:
        raise ValueError("Edge source_node is not a declared node sheet.")
    if edge_sheet["target_node"] not in by_sheet:
        raise ValueError("Edge target_node is not a declared node sheet.")
    for column in (edge_sheet["source_column"], edge_sheet["target_column"]):
        if column not in frame.columns:
            raise ValueError(f"Edge sheet {sheet!r} has no column {column!r}.")
    if len(frame) > MAX_ROWS:
        raise ValueError(f"Sheet {sheet!r} has {len(frame)} rows; limit is {MAX_ROWS}.")
    return {
        "sheet": sheet,
        "table": ident(f"{name}_{sheet}", "edge table"),
        "label": ident(sheet, "label"),
        "source_name": ident(edge_sheet["source_column"], "source"),
        "target_name": ident(edge_sheet["target_column"], "target"),
        "source_node": by_sheet[edge_sheet["source_node"]],
        "target_node": by_sheet[edge_sheet["target_node"]],
        "columns": _column_plan(frame),
        "frame": frame,
    }


def build_ddl(name: str, node_plans: list[dict], edge_plan: dict | None) -> list[str]:
    """Build table and property-graph DDL. Identifiers are already sanitized."""
    statements = []
    for plan in node_plans:
        cols = ",\n  ".join(
            f"{col['name']} {col['type']}"
            + (" NOT NULL" if col["name"] == plan["id_name"] else "")
            for col in plan["columns"]
        )
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {plan['table']} (\n  {cols}\n) "
            f"PRIMARY KEY ({plan['id_name']})"
        )
    edge_clause = ""
    if edge_plan:
        prop_cols = ",\n  ".join(
            f"{col['name']} {col['type']}" for col in edge_plan["columns"]
        )
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {edge_plan['table']} (\n"
            f"  edge_id STRING(64) NOT NULL,\n  {prop_cols}\n) "
            f"PRIMARY KEY (edge_id)"
        )
        source = edge_plan["source_node"]
        target = edge_plan["target_node"]
        edge_clause = (
            f" EDGE TABLES ( {edge_plan['table']}"
            f" SOURCE KEY ({edge_plan['source_name']}) REFERENCES "
            f"{source['table']} ({source['id_name']})"
            f" DESTINATION KEY ({edge_plan['target_name']}) REFERENCES "
            f"{target['table']} ({target['id_name']})"
            f" LABEL {edge_plan['label']} )"
        )
    node_clause = ", ".join(
        f"{plan['table']} KEY ({plan['id_name']}) LABEL {plan['label']}"
        for plan in node_plans
    )
    statements.append(
        f"CREATE OR REPLACE PROPERTY GRAPH {name} "
        f"NODE TABLES ( {node_clause} ){edge_clause}"
    )
    return statements


def _rows(plan: dict) -> tuple[list[str], list[list]]:
    columns = [col["name"] for col in plan["columns"]]
    types = {col["name"]: col["type"] for col in plan["columns"]}
    values = [
        [cell(record[col["sheet_column"]], types[col["name"]]) for col in plan["columns"]]
        for _, record in plan["frame"].iterrows()
    ]
    return columns, values


def insert_rows(db, node_plans: list[dict], edge_plan: dict | None) -> None:
    def _write(transaction):
        for plan in node_plans:
            columns, values = _rows(plan)
            if values:
                transaction.insert_or_update(plan["table"], columns=columns, values=values)
        if not edge_plan:
            return
        columns, values = _rows(edge_plan)
        edged = []
def _catalog_mapping(node_plans: list[dict], edge_plan: dict | None) -> dict:
    def _cols(plan: dict) -> list[dict[str, str]]:
        return [{"name": col["name"], "type": col["type"]} for col in plan["columns"]]

    edge = None
    if edge_plan:
        edge = {
            "sheet": edge_plan["sheet"],
            "table": edge_plan["table"],
            "label": edge_plan["label"],
            "source": edge_plan["source_name"],
            "target": edge_plan["target_name"],
            "columns": _cols(edge_plan),
        }
    return {
        "nodes": [
            {
                "sheet": plan["sheet"],
                "table": plan["table"],
                "label": plan["label"],
                "id_column": plan["id_name"],
                "columns": _cols(plan),
            }
            for plan in node_plans
        ],
        "edge": edge,
    }


def load_workbook(
    workbook_bytes: bytes,
    workbook_name: str,
    node_sheets: list[dict[str, str]],
    edge_sheet: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Persist one workbook as one property graph.

    node_sheets items are {"sheet", "id_column"}. edge_sheet, when set, is
    {"sheet", "source_column", "target_column", "source_node", "target_node"}.
    source_node and target_node are declared node sheet names.
    """
    if not node_sheets:
        raise ValueError("At least one node sheet mapping is required.")
    sheets = pd.read_excel(io.BytesIO(workbook_bytes), sheet_name=None, engine="openpyxl")
    name = graph_name_for(workbook_name)
    node_plans = [_node_plan(sheets, spec, name) for spec in node_sheets]
    edge_plan = _edge_plan(sheets, edge_sheet, node_plans, name)
    db = database()
    ensure_catalog(db)
    db.update_ddl(build_ddl(name, node_plans, edge_plan)).result(timeout=300)
    insert_rows(db, node_plans, edge_plan)
    mapping = _catalog_mapping(node_plans, edge_plan)

    def _write(transaction):
        transaction.insert_or_update(
            CATALOG,
            columns=["graph_name", "workbook_name", "mapping_json", "loaded_at"],
            values=[
                [
                    name,
                    workbook_name[:256],
                    json.dumps(mapping),
                    spanner.COMMIT_TIMESTAMP,
                ]
            ],
        )

    db.run_in_transaction(_write)
    counts = {plan["table"]: int(len(plan["frame"])) for plan in node_plans}
    if edge_plan:
        counts[edge_plan["table"]] = int(len(edge_plan["frame"]))
    return {
        "status": "SUCCESS",
        "graph_name": name,
        "node_tables": [plan["table"] for plan in node_plans],
        "edge_table": edge_plan["table"] if edge_plan else None,
        "row_counts": counts,
    }


def list_graphs() -> list[dict[str, Any]]:
    db = database()
    with db.snapshot() as snapshot:
        rows = snapshot.execute_sql(
            f"SELECT graph_name, workbook_name, mapping_json FROM {CATALOG} "
            "ORDER BY graph_name"
        )
        return [
            {
                "graph_name": row[0],
                "workbook_name": row[1],
                "mapping": json.loads(row[2]),
            }
            for row in rows
        ]


def graph_schema(name: str) -> dict[str, Any] | None:
    settings.require_ident(name, "graph_name")
    db = database()
    with db.snapshot() as snapshot:
        rows = list(
            snapshot.execute_sql(
                f"SELECT workbook_name, mapping_json FROM {CATALOG} "
                "WHERE graph_name = @name",
                params={"name": name},
                param_types={"name": param_types.STRING},
            )
        )
    if not rows:
        return None
    return {
        "graph_name": name,
        "workbook_name": rows[0][0],
        "mapping": json.loads(rows[0][1]),
        "query_shape": f"GRAPH {name} MATCH (n) RETURN TO_JSON(n) AS node LIMIT 20",
    }

