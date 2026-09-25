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

"""Load an .xlsx workbook into one Spanner Graph schema.

The model never emits DDL. This module profiles sheets, infers node keys and
edge relationships, then builds CREATE TABLE / CREATE PROPERTY GRAPH statements
and runs them through the Spanner Python client. A caller-supplied mapping
overrides inference.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from datetime import date, datetime
from typing import Any

import pandas as pd
from google.cloud import spanner
from google.cloud.spanner_v1 import param_types

from data_science.sub_agents.database import settings

CATALOG = "workbook_catalog"
MAX_STRING = 1024
MAX_ROWS = 20000
EDGE_OVERLAP = 0.80
HIGH_OVERLAP = 0.95
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ID_NAME = re.compile(r"^(id|.*_id|key|code|uuid)$", re.IGNORECASE)
_READ_ONLY = re.compile(r"^\s*(SELECT|WITH|GRAPH)\b", re.IGNORECASE | re.DOTALL)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|MERGE|CALL|GRANT|REVOKE|"
    r"TRUNCATE|EXPORT|LOAD)\b",
    re.IGNORECASE,
)
_EDGE_KEYS = (
    "sheet",
    "source_column",
    "target_column",
    "source_node",
    "target_node",
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


def _clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    cleaned = frame.dropna(how="all").dropna(axis=1, how="all")
    cleaned.columns = [str(column) for column in cleaned.columns]
    return cleaned


def _non_null(series: pd.Series) -> pd.Series:
    return series.dropna()


def _is_unique(series: pd.Series) -> bool:
    values = _non_null(series)
    return len(values) > 0 and values.nunique(dropna=True) == len(values)


def _samples(series: pd.Series) -> list[str]:
    values = _non_null(series).unique()[:3]
    return [str(value)[:80] for value in values]


def _profile_column(series: pd.Series) -> dict[str, Any]:
    values = _non_null(series)
    return {
        "sheet_column": str(series.name),
        "name": ident(series.name, "column"),
        "type": spanner_type(series),
        "null_rate": float(series.isna().mean()) if len(series) else 1.0,
        "distinct": int(values.nunique(dropna=True)),
        "unique": _is_unique(series),
        "samples": _samples(series),
    }


def _profile_sheet(sheet: str, frame: pd.DataFrame) -> dict[str, Any]:
    cleaned = _clean_frame(frame)
    if len(cleaned) > MAX_ROWS:
        raise ValueError(
            f"Sheet {sheet!r} has {len(cleaned)} rows; limit is {MAX_ROWS}."
        )
    return {
        "sheet": sheet,
        "frame": cleaned,
        "row_count": int(len(cleaned)),
        "columns": [_profile_column(cleaned[column]) for column in cleaned.columns],
    }


def read_sheets(workbook_bytes: bytes) -> dict[str, pd.DataFrame]:
    return pd.read_excel(
        io.BytesIO(workbook_bytes), sheet_name=None, engine="openpyxl"
    )


def _conventional_id(column: str) -> bool:
    return bool(_ID_NAME.match(ident(column, "column")))


def _pick_id(profile: dict) -> tuple[str | None, str]:
    """Return (id column or None for a synthetic key, confidence)."""
    unique = [col for col in profile["columns"] if col["unique"]]
    named = [col for col in unique if _conventional_id(col["sheet_column"])]
    if named:
        return named[0]["sheet_column"], "high"
    if unique:
        return unique[0]["sheet_column"], "medium"
    return None, "low"


def _name_match(column: str, node_sheet: str, id_column: str) -> bool:
    column_name = ident(column, "column").lower()
    tokens = {
        ident(node_sheet, "sheet").lower(),
        ident(id_column, "id").lower(),
    }
    generic = {"id", "key", "code", "uuid", "name"}
    return any(
        token and len(token) > 1 and token not in generic and token in column_name
        for token in tokens
    )


def _value_key(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _overlap(series: pd.Series, id_values: set) -> float:
    values = set(_non_null(series).map(_value_key))
    if not values or not id_values:
        return 0.0
    return len(values & id_values) / len(values)


def _id_values(frame: pd.DataFrame, id_column: str) -> set[str]:
    return set(_non_null(frame[id_column]).map(_value_key))


def _rank_targets(
    series: pd.Series, nodes: list[dict], *, skip_sheet: str
) -> list[dict]:
    ranked = []
    for node in nodes:
        if node["sheet"] == skip_sheet or not node["id_values"]:
            continue
        overlap = _overlap(series, node["id_values"])
        if overlap < EDGE_OVERLAP:
            continue
        ranked.append(
            {
                "sheet": node["sheet"],
                "id_column": node["id_column"],
                "overlap": overlap,
                "name_match": _name_match(
                    str(series.name), node["sheet"], node["id_column"]
                ),
            }
        )
    ranked.sort(key=lambda item: (item["name_match"], item["overlap"]), reverse=True)
    return ranked


def _attach_runner_up(ranked: list[dict]) -> dict:
    chosen = dict(ranked[0])
    if len(ranked) > 1 and not chosen["name_match"]:
        chosen["runner_up"] = ranked[1]["sheet"]
    return chosen


def _pair_confidence(left: dict, right: dict) -> str:
    close = left.get("runner_up") or right.get("runner_up")
    if close or min(left["overlap"], right["overlap"]) < HIGH_OVERLAP:
        return "low"
    return "high"


def _worst(levels: list[str]) -> str:
    order = {"high": 0, "medium": 1, "low": 2, "declared": -1}
    return max(levels, key=lambda level: order[level]) if levels else "low"


def _id_reason(node: dict) -> str:
    if node["id_column"] is None:
        return "No unique column; a synthetic row_id will be generated."
    if node["id_confidence"] == "high":
        return f"{node['id_column']} is unique and has an id-like name."
    return f"{node['id_column']} is unique, but its name is not id-like."


def _best_edge_pair(profile: dict, nodes: list[dict]) -> dict | None:
    candidates = []
    columns = list(profile["frame"].columns)
    for left_name in columns:
        left_ranked = _rank_targets(
            profile["frame"][left_name], nodes, skip_sheet=profile["sheet"]
        )
        if not left_ranked:
            continue
        for right_name in columns:
            if right_name == left_name:
                continue
            right_ranked = _rank_targets(
                profile["frame"][right_name], nodes, skip_sheet=profile["sheet"]
            )
            if not right_ranked:
                continue
            left = _attach_runner_up(left_ranked)
            right = _attach_runner_up(right_ranked)
            score = (
                int(left["name_match"]) + int(right["name_match"]),
                left["overlap"] + right["overlap"],
            )
            candidates.append((score, left_name, right_name, left, right))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, source_column, target_column, source, target = candidates[0]
    return {
        "sheet": profile["sheet"],
        "source_column": source_column,
        "target_column": target_column,
        "source_node": source["sheet"],
        "target_node": target["sheet"],
        "confidence": _pair_confidence(source, target),
        "reason": (
            f"{source_column} matches {source['sheet']}.{source['id_column']} "
            f"({source['overlap']:.0%}); {target_column} matches "
            f"{target['sheet']}.{target['id_column']} ({target['overlap']:.0%})."
        ),
        "source_overlap": round(source["overlap"], 3),
        "target_overlap": round(target["overlap"], 3),
    }


def analyze_workbook(workbook_bytes: bytes) -> dict[str, Any]:
    """Infer node sheets, id columns, and edge sheets from workbook contents.

    Always returns a loadable mapping. Confidence is high, medium, or low.
    A low score still describes a mapping that can be loaded.
    """
    sheets = read_sheets(workbook_bytes)
    if not sheets:
        raise ValueError("Workbook has no sheets.")
    profiles = [_profile_sheet(sheet, frame) for sheet, frame in sheets.items()]
    nodes = []
    for profile in profiles:
        id_column, confidence = _pick_id(profile)
        nodes.append(
            {
                "sheet": profile["sheet"],
                "id_column": id_column,
                "id_confidence": confidence,
                "frame": profile["frame"],
                "id_values": (
                    _id_values(profile["frame"], id_column) if id_column else set()
                ),
            }
        )
    edges = []
    edge_sheets = set()
    for profile in profiles:
        pair = _best_edge_pair(profile, nodes)
        if not pair:
            continue
        edges.append(pair)
        edge_sheets.add(profile["sheet"])
    node_specs = [
        {
            "sheet": node["sheet"],
            "id_column": node["id_column"],
            "confidence": node["id_confidence"],
            "reason": _id_reason(node),
        }
        for node in nodes
        if node["sheet"] not in edge_sheets
    ]
    if not node_specs:
        raise ValueError("Workbook has no sheet that can be a node table.")
    return {
        "node_sheets": node_specs,
        "edge_sheets": edges,
        "confidence": _worst(
            [spec["confidence"] for spec in node_specs]
            + [edge["confidence"] for edge in edges]
        ),
        "profiles": [
            {
                "sheet": profile["sheet"],
                "row_count": profile["row_count"],
                "columns": [
                    {
                        "name": column["sheet_column"],
                        "type": column["type"],
                        "unique": column["unique"],
                        "null_rate": round(column["null_rate"], 3),
                        "samples": column["samples"],
                    }
                    for column in profile["columns"]
                ],
            }
            for profile in profiles
        ],
    }


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
    id_column = spec.get("id_column")
    if sheet not in sheets:
        raise ValueError(f"Node sheet {sheet!r} is not in the workbook.")
    frame = _clean_frame(sheets[sheet])
    synthetic = not id_column
    if synthetic:
        id_column = "row_id"
        frame = frame.copy()
        frame.insert(0, id_column, [uuid.uuid4().hex for _ in range(len(frame))])
    elif id_column not in frame.columns:
        raise ValueError(f"Node sheet {sheet!r} has no id column {id_column!r}.")
    if len(frame) > MAX_ROWS:
        raise ValueError(f"Sheet {sheet!r} has {len(frame)} rows; limit is {MAX_ROWS}.")
    if not _is_unique(frame[id_column]):
        raise ValueError(f"Node sheet {sheet!r} id column {id_column!r} is not unique.")
    return {
        "sheet": sheet,
        "id_column": id_column,
        "id_name": ident(id_column, "id column"),
        "table": ident(f"{name}_{sheet}", "node table"),
        "label": ident(sheet, "label"),
        "columns": _column_plan(frame),
        "frame": frame,
        "synthetic_id": synthetic,
    }


def _edge_plan(sheets, edge_sheet, node_plans, name: str) -> dict:
    if not edge_sheet:
        raise ValueError("Edge mapping is missing.")
    sheet = edge_sheet["sheet"]
    if sheet not in sheets:
        raise ValueError(f"Edge sheet {sheet!r} is not in the workbook.")
    frame = _clean_frame(sheets[sheet])
    for key in _EDGE_KEYS:
        if key not in edge_sheet or not edge_sheet[key]:
            raise ValueError(f"Edge mapping is missing {key}.")
    by_sheet = {plan["sheet"]: plan for plan in node_plans}
    if edge_sheet["source_node"] not in by_sheet:
        raise ValueError("Edge source_node is not a node sheet.")
    if edge_sheet["target_node"] not in by_sheet:
        raise ValueError("Edge target_node is not a node sheet.")
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
        "confidence": edge_sheet.get("confidence", "declared"),
        "reason": edge_sheet.get("reason", "Declared by the caller."),
    }


def _edge_clause(edge_plans: list[dict]) -> str:
    if not edge_plans:
        return ""
    entries = []
    for plan in edge_plans:
        source = plan["source_node"]
        target = plan["target_node"]
        entries.append(
            f"{plan['table']}"
            f" SOURCE KEY ({plan['source_name']}) REFERENCES "
            f"{source['table']} ({source['id_name']})"
            f" DESTINATION KEY ({plan['target_name']}) REFERENCES "
            f"{target['table']} ({target['id_name']})"
            f" LABEL {plan['label']}"
        )
    return " EDGE TABLES ( " + ", ".join(entries) + " )"


def build_ddl(name: str, node_plans: list[dict], edge_plans: list[dict]) -> list[str]:
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
    for plan in edge_plans:
        prop_cols = ",\n  ".join(
            f"{col['name']} {col['type']}" for col in plan["columns"]
        )
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {plan['table']} (\n"
            f"  edge_id STRING(64) NOT NULL,\n  {prop_cols}\n) "
            f"PRIMARY KEY (edge_id)"
        )
    node_clause = ", ".join(
        f"{plan['table']} KEY ({plan['id_name']}) LABEL {plan['label']}"
        for plan in node_plans
    )
    statements.append(
        f"CREATE OR REPLACE PROPERTY GRAPH {name} "
        f"NODE TABLES ( {node_clause} ){_edge_clause(edge_plans)}"
    )
    return statements


def _rows(plan: dict, *, edge: bool = False) -> tuple[list[str], list[list]]:
    columns = [col["name"] for col in plan["columns"]]
    types = {col["name"]: col["type"] for col in plan["columns"]}
    values = [
        [cell(record[col["sheet_column"]], types[col["name"]]) for col in plan["columns"]]
        for _, record in plan["frame"].iterrows()
    ]
    if edge:
        columns = ["edge_id", *columns]
        values = [[uuid.uuid4().hex, *row] for row in values]
    return columns, values


def insert_rows(db, node_plans: list[dict], edge_plans: list[dict]) -> None:
    def _write(transaction):
        for plan in node_plans:
            columns, values = _rows(plan)
            if values:
                transaction.insert_or_update(plan["table"], columns=columns, values=values)
        for plan in edge_plans:
            columns, values = _rows(plan, edge=True)
            if values:
                transaction.insert(plan["table"], columns=columns, values=values)

    db.run_in_transaction(_write)


def drop_stale_tables(
    name: str, node_plans: list[dict], edge_plans: list[dict]
) -> list[str]:
    """Drop tables from a previous load of this graph that the new plan omits."""
    previous = graph_schema(name)
    if not previous:
        return []
    mapping = previous.get("mapping") or {}
    kept = {plan["table"] for plan in node_plans + edge_plans}
    stale = []
    previous_edges = list(mapping.get("edges") or [])
    if mapping.get("edge"):
        previous_edges.append(mapping["edge"])
    for item in list(mapping.get("nodes") or []) + previous_edges:
        table = item.get("table")
        if table and table not in kept:
            stale.append(f"DROP TABLE IF EXISTS {ident(table, 'table')}")
    return stale


def _catalog_mapping(
    node_plans: list[dict],
    edge_plans: list[dict],
    analysis: dict | None,
) -> dict:
    def _cols(plan: dict) -> list[dict[str, str]]:
        return [{"name": col["name"], "type": col["type"]} for col in plan["columns"]]

    by_sheet_node = {
        spec["sheet"]: spec for spec in (analysis or {}).get("node_sheets", [])
    }
    by_sheet_edge = {
        spec["sheet"]: spec for spec in (analysis or {}).get("edge_sheets", [])
    }
    return {
        "confidence": (analysis or {}).get("confidence", "declared"),
        "nodes": [
            {
                "sheet": plan["sheet"],
                "table": plan["table"],
                "label": plan["label"],
                "id_column": plan["id_name"],
                "synthetic_id": bool(plan.get("synthetic_id")),
                "confidence": by_sheet_node.get(plan["sheet"], {}).get(
                    "confidence", "declared"
                ),
                "reason": by_sheet_node.get(plan["sheet"], {}).get(
                    "reason", "Declared by the caller."
                ),
                "columns": _cols(plan),
            }
            for plan in node_plans
        ],
        "edges": [
            {
                "sheet": plan["sheet"],
                "table": plan["table"],
                "label": plan["label"],
                "source": plan["source_name"],
                "target": plan["target_name"],
                "source_node": plan["source_node"]["sheet"],
                "target_node": plan["target_node"]["sheet"],
                "confidence": by_sheet_edge.get(plan["sheet"], {}).get(
                    "confidence", plan.get("confidence", "declared")
                ),
                "reason": by_sheet_edge.get(plan["sheet"], {}).get(
                    "reason", plan.get("reason", "Declared by the caller.")
                ),
                "columns": _cols(plan),
            }
            for plan in edge_plans
        ],
    }


def _normalize_edges(edge_sheet, edge_sheets) -> list[dict]:
    if edge_sheets:
        return list(edge_sheets)
    if edge_sheet:
        return [edge_sheet]
    return []


def load_workbook(
    workbook_bytes: bytes,
    workbook_name: str,
    node_sheets: list[dict[str, str]] | None = None,
    edge_sheet: dict[str, str] | None = None,
    edge_sheets: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Persist one workbook as one property graph.

    When node_sheets is omitted, sheet roles, id columns, and edges are inferred.
    An explicit mapping overrides inference. Reloading the same filename replaces
    that graph and drops tables the new plan no longer uses.
    """
    sheets = read_sheets(workbook_bytes)
    name = graph_name_for(workbook_name)
    declared = bool(node_sheets)
    if not declared:
        analysis = analyze_workbook(workbook_bytes)
        node_sheets = [
            {"sheet": spec["sheet"], "id_column": spec["id_column"]}
            for spec in analysis["node_sheets"]
        ]
        edge_sheets = analysis["edge_sheets"]
    else:
        edge_sheets = _normalize_edges(edge_sheet, edge_sheets)
        analysis = {
            "confidence": "declared",
            "node_sheets": [
                {
                    "sheet": spec["sheet"],
                    "confidence": "declared",
                    "reason": "Declared by the caller.",
                }
                for spec in node_sheets
            ],
            "edge_sheets": edge_sheets,
        }
    if not node_sheets:
        raise ValueError("At least one node sheet mapping is required.")
    node_plans = [_node_plan(sheets, spec, name) for spec in node_sheets]
    edge_plans = [_edge_plan(sheets, spec, node_plans, name) for spec in edge_sheets]
    db = database()
    ensure_catalog(db)
    ddl = drop_stale_tables(name, node_plans, edge_plans)
    ddl.extend(build_ddl(name, node_plans, edge_plans))
    db.update_ddl(ddl).result(timeout=300)
    insert_rows(db, node_plans, edge_plans)
    mapping = _catalog_mapping(node_plans, edge_plans, analysis)

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
    for plan in edge_plans:
        counts[plan["table"]] = int(len(plan["frame"]))
    return {
        "status": "SUCCESS",
        "graph_name": name,
        "node_tables": [plan["table"] for plan in node_plans],
        "edge_tables": [plan["table"] for plan in edge_plans],
        "edge_table": edge_plans[0]["table"] if len(edge_plans) == 1 else None,
        "row_counts": counts,
        "mapping": mapping,
        "confidence": mapping["confidence"],
        "inferred": not declared,
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
    mapping = json.loads(rows[0][1])
    edges = list(mapping.get("edges") or [])
    if mapping.get("edge"):
        edges.append(mapping["edge"])
    return {
        "graph_name": name,
        "workbook_name": rows[0][0],
        "mapping": mapping,
        "edges": edges,
        "confidence": mapping.get("confidence"),
        "query_shape": f"GRAPH {name} MATCH (n) RETURN TO_JSON(n) AS node LIMIT 20",
    }

