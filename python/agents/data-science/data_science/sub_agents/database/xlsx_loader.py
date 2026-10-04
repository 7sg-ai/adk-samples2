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

"""Load an .xlsx workbook into BigQuery tables and/or one Spanner Graph schema.

The model never emits DDL. Sheets are classified first: large raw sheets become
BigQuery tables; smaller sheets with formulas or cross-sheet key relationships
become one Spanner property graph. A caller-supplied node/edge mapping forces
those sheets into the graph. There is no row-count limit.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import uuid
from datetime import date, datetime
from typing import Any

import pandas as pd
from google.cloud import bigquery, spanner
from google.cloud.spanner_v1 import param_types
from openpyxl import load_workbook as open_workbook

from data_science.sub_agents.database import settings

logger = logging.getLogger(__name__)

CATALOG = "workbook_catalog"
MAX_STRING = 1024
# Routing hint only. Sheets at or above this size are tables unless they
# contain formulas or foreign-key overlap. Never a hard reject.
GRAPH_ROW_HINT = 20000
BATCH_ROWS = 500
EDGE_OVERLAP = 0.80
HIGH_OVERLAP = 0.95
MIN_EDGE_DISTINCT = 8
_HEADER_SCAN_ROWS = 8
_AGGREGATE_FN = re.compile(
    r"\b(SUMPRODUCT|COUNTIFS?|SUMIFS?|AVERAGEIFS?|INDEX|MATCH)\s*\(",
    re.IGNORECASE,
)
_SENTINEL = re.compile(
    r"^(all(\s+\w+)?|total|none|n/?a|nan|--)$",
    re.IGNORECASE,
)
_RANGE_REF = re.compile(
    r"(?:'(?P<quoted>(?:[^']|'')+)'|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))!"
    r"\$?(?P<col>[A-Z]{1,3})\$?(?P<row>\d+)"
)
_FORMULA_REF = re.compile(
    r"(?:'((?:[^']|'')+)'|([A-Za-z_][A-Za-z0-9_]*))!",
)
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


def _flatten_label(value: Any) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return index


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _is_label(value: Any) -> bool:
    if _is_blank(value) or isinstance(value, (int, float, datetime, date, bool)):
        return False
    text = _flatten_label(value)
    return bool(text) and not text.startswith("=") and not _SENTINEL.match(text)


def _non_null(series: pd.Series) -> pd.Series:
    return series.dropna()


def _sentinel_share(series: pd.Series) -> float:
    values = _non_null(series).map(_flatten_label)
    if values.empty:
        return 0.0
    flagged = values.map(lambda text: bool(_SENTINEL.match(text)))
    return float(flagged.mean())


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
    return {
        "sheet": sheet,
        "frame": cleaned,
        "row_count": int(len(cleaned)),
        "columns": [_profile_column(cleaned[column]) for column in cleaned.columns],
    }


def read_sheets(workbook_bytes: bytes) -> dict[str, pd.DataFrame]:
    layout = parse_layout(workbook_bytes)
    return {
        sheet: info["frame"]
        for sheet, info in layout["sheets"].items()
    }


def _sheet_grid(worksheet) -> list[list[Any]]:
    return [list(row) for row in worksheet.iter_rows(values_only=True)]


def _header_row(grid: list[list[Any]]) -> int | None:
    """Return the 0-based row whose labels name the rows beneath it."""
    best: tuple[float, int] | None = None
    limit = min(len(grid), _HEADER_SCAN_ROWS)
    for index in range(limit):
        row = grid[index]
        labels = [column for column, value in enumerate(row) if _is_label(value)]
        if len(labels) == 1 and index + 1 < limit:
            nxt = grid[index + 1]
            if sum(1 for value in nxt if _is_label(value)) >= 2:
                continue
        if len(labels) < 2:
            continue
        followers = grid[index + 1 : index + 4]
        if not followers:
            continue
        next_row = grid[index + 1]
        next_label_count = sum(1 for value in next_row if _is_label(value))
        if next_label_count > len(labels):
            continue
        filled = 0
        text_followers = 0
        checked = 0
        for column in labels:
            for follower in followers:
                if column >= len(follower):
                    continue
                checked += 1
                if not _is_blank(follower[column]):
                    filled += 1
                if _is_label(follower[column] if column < len(follower) else None):
                    text_followers += 1
        if not checked or not filled:
            continue
        data_share = 1 - (text_followers / filled)
        score = len(labels) * (filled / checked) * (0.25 + data_share)
        score = max(score, len(labels) * (filled / checked) * 0.8)
        if best is None or score > best[0]:
            best = (score, index)
    if best is None or best[0] < 1.5:
        return None
    return best[1]


def _region_frame(grid: list[list[Any]], header_index: int) -> pd.DataFrame:
    """Build a frame from one header row, skipping blank and title columns."""
    header = grid[header_index]
    width = max((len(row) for row in grid), default=0)
    columns: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    for column in range(width):
        label = _flatten_label(header[column]) if column < len(header) else ""
        if not _is_label(label):
            continue
        count = seen.get(label, 0) + 1
        seen[label] = count
        columns.append((column, label if count == 1 else f"{label}_{count}"))
    records = []
    for row in grid[header_index + 1 :]:
        if not any(
            column < len(row) and not _is_blank(row[column]) for column, _ in columns
        ):
            continue
        records.append(
            {
                name: row[column] if column < len(row) else None
                for column, name in columns
            }
        )
    names = [name for _, name in columns]
    if not records:
        return _clean_frame(pd.DataFrame(columns=names))
    return _clean_frame(pd.DataFrame.from_records(records, columns=names))


def _open_book(workbook_bytes: bytes):
    return open_workbook(io.BytesIO(workbook_bytes), data_only=False, read_only=False)


def _defined_name_text(book) -> dict[str, str]:
    return {
        name: (getattr(defined, "attr_text", "") or "")
        for name, defined in book.defined_names.items()
    }


def parse_layout(workbook_bytes: bytes) -> dict[str, Any]:
    """Detect header rows, named-range columns, and fact versus lookup sheets.

    Dashboard sheets full of COUNTIFS/SUMIFS stay out of the entity graph.
    A fact sheet referenced by many column ranges, and the lookup sheet those
    formulas join to, are returned as tables with their real headers.
    """
    book = _open_book(workbook_bytes)
    try:
        grids = {sheet.title: _sheet_grid(sheet) for sheet in book.worksheets}
        names = _defined_name_text(book)
        catalog: list[dict[str, Any]] = []
        for name, text in names.items():
            match = _RANGE_REF.search(text)
            if not match:
                continue
            catalog.append(
                {
                    "name": name,
                    "sheet": (match.group("quoted") or match.group("plain")).replace(
                        "''", "'"
                    ),
                    "column": _column_index(match.group("col")),
                    "row": int(match.group("row")),
                }
            )
        by_sheet: dict[str, list[dict[str, Any]]] = {}
        for item in catalog:
            by_sheet.setdefault(item["sheet"], []).append(item)
        sheets: dict[str, dict[str, Any]] = {}
        for title, grid in grids.items():
            header_index = _header_row(grid)
            if header_index is None:
                frame = _clean_frame(
                    pd.read_excel(
                        io.BytesIO(workbook_bytes),
                        sheet_name=title,
                        engine="openpyxl",
                    )
                )
            else:
                frame = _region_frame(grid, header_index)
            refs = by_sheet.get(title, [])
            labels: dict[str, str] = {}
            if header_index is not None:
                header = grid[header_index]
                for item in refs:
                    column = item["column"] - 1
                    if 0 <= column < len(header) and _is_label(header[column]):
                        labels[item["name"]] = _flatten_label(header[column])
            sheets[title] = {
                "sheet": title,
                "header_row": None if header_index is None else header_index + 1,
                "frame": frame,
                "named_ranges": len(refs),
                "range_labels": labels,
            }
        return {"sheets": sheets, "named_ranges": catalog, "defined_names": names}
    finally:
        book.close()


def _aggregate_sheet(formulas: dict[str, Any]) -> bool:
    samples = formulas.get("formula_samples") or []
    count = int(formulas.get("formula_count") or 0)
    if count < 3 or not samples:
        return False
    return any(_AGGREGATE_FN.search(sample) for sample in samples)


def _formula_text(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("="):
        return value
    text = getattr(value, "text", None)
    if isinstance(text, str) and text.startswith("="):
        return text
    return None


def formula_report(workbook_bytes: bytes) -> dict[str, dict[str, Any]]:
    """Count formulas and cross-sheet references without evaluating them."""
    book = open_workbook(io.BytesIO(workbook_bytes), data_only=False, read_only=True)
    report: dict[str, dict[str, Any]] = {}
    try:
        defined = []
        for named in book.defined_names.values():
            defined.append(getattr(named, "attr_text", "") or "")
        for sheet in book.worksheets:
            formulas = 0
            cross_sheet = 0
            samples: list[str] = []
            references: set[str] = set()
            title = sheet.title
            for row in sheet.iter_rows():
                for cell in row:
                    formula = _formula_text(cell.value)
                    if not formula:
                        continue
                    formulas += 1
                    if len(samples) < 12:
                        samples.append(formula[:160])
                    if _FORMULA_REF.search(formula):
                        cross_sheet += 1
                        for match in _FORMULA_REF.finditer(formula):
                            references.add((match.group(1) or match.group(2)).replace("''", "'"))
            for text in defined:
                quoted = f"'{title}'!"
                plain = f"{title}!"
                if quoted in text or plain in text:
                    cross_sheet += 1
            report[title] = {
                "formula_count": formulas,
                "cross_sheet_refs": cross_sheet,
                "formula_samples": samples,
                "references": sorted(references),
            }
    finally:
        book.close()
    return report


def _sheet_role(
    profile: dict,
    formulas: dict[str, Any],
    nodes: list[dict],
    *,
    layout_sheet: dict[str, Any] | None = None,
    derived: bool = False,
    settings: bool = False,
) -> tuple[str, str]:
    """Return (table|graph|skip, reason) for one sheet."""
    layout_sheet = layout_sheet or {}
    formula_count = int(formulas.get("formula_count") or 0)
    named_ranges = int(layout_sheet.get("named_ranges") or 0)
    if derived or (formula_count and _aggregate_sheet(formulas)):
        return (
            "skip",
            f"{formula_count} aggregate formulas; derived view, not an entity.",
        )
    if named_ranges >= 3 and not formula_count and profile["row_count"] >= 1:
        return (
            "table",
            f"Fact sheet referenced by {named_ranges} named column ranges.",
        )
    if formula_count:
        cross_sheet = int(formulas.get("cross_sheet_refs") or 0)
        if cross_sheet:
            return (
                "graph",
                f"{formula_count} formulas, {cross_sheet} cross-sheet references.",
            )
        return "graph", f"{formula_count} formulas relate cells in this sheet."
    pair = _best_edge_pair(profile, nodes)
    if pair:
        return "graph", pair["reason"]
    if profile["row_count"] >= GRAPH_ROW_HINT:
        return (
            "table",
            f"{profile['row_count']} raw rows and no formulas or key overlap.",
        )
    if profile["row_count"] == 0:
        return "table", "Empty sheet; stored as a table."
    if (
        not formula_count
        and profile["row_count"] <= 30
        and len(profile["frame"].columns) <= 1
        and settings
    ):
        return "skip", "Settings or filter list, not an entity table."
    return (
        "table",
        "No formulas and no foreign-key overlap with another sheet.",
    )


def _fact_sheet(layout: dict[str, Any]) -> str | None:
    counts = {
        sheet: int(info.get("named_ranges") or 0)
        for sheet, info in layout["sheets"].items()
    }
    if not counts:
        return None
    sheet, count = max(counts.items(), key=lambda item: item[1])
    return sheet if count >= 3 else None


def _lookup_sheets(layout: dict[str, Any], formulas: dict[str, dict[str, Any]]) -> set[str]:
    """Sheets joined by named ranges from aggregate formulas, not edge tables."""
    fact = _fact_sheet(layout)
    labels = {
        sheet: set(info.get("range_labels") or {})
        for sheet, info in layout["sheets"].items()
        if sheet != fact
    }
    found = set()
    for info in formulas.values():
        if not _aggregate_sheet(info):
            continue
        for sample in info.get("formula_samples") or []:
            for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", sample):
                token = match.group(1)
                for sheet, names in labels.items():
                    if token in names:
                        found.add(sheet)
    return found


def classify_sheets(workbook_bytes: bytes) -> dict[str, Any]:
    """Choose BigQuery, Spanner Graph, or skip for each sheet. Does not load data."""
    layout = parse_layout(workbook_bytes)
    sheets = {sheet: info["frame"] for sheet, info in layout["sheets"].items()}
    if not sheets:
        raise ValueError("Workbook has no sheets.")
    profiles = [_profile_sheet(sheet, frame) for sheet, frame in sheets.items()]
    formulas = formula_report(workbook_bytes)
    lookups = _lookup_sheets(layout, formulas)
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
    decisions = []
    referenced = {
        name
        for info in formulas.values()
        if not _aggregate_sheet(info)
        for name in info.get("references") or []
    }
    endpoints = set()
    for profile in profiles:
        if profile["sheet"] in lookups or _aggregate_sheet(
            formulas.get(profile["sheet"], {})
        ):
            continue
        pair = _best_edge_pair(profile, nodes)
        if pair and pair["source_node"] not in lookups and pair["target_node"] not in lookups:
            endpoints.add(pair["source_node"])
            endpoints.add(pair["target_node"])
    for profile in profiles:
        formula_info = formulas.get(profile["sheet"], {})
        destination, reason = _sheet_role(
            profile,
            formula_info,
            nodes,
            layout_sheet=layout["sheets"].get(profile["sheet"], {}),
            derived=profile["sheet"] not in lookups and _aggregate_sheet(formula_info),
            settings=bool(layout.get("named_ranges")),
        )
        if profile["sheet"] in lookups and destination != "skip":
            destination = "table"
            labels = sorted(
                set(layout["sheets"][profile["sheet"]].get("range_labels", {}).values())
            )
            shown = ", ".join(labels[:6]) or "named ranges"
            reason = f"Lookup dimension joined by named ranges ({shown})."
        elif (
            destination == "table"
            and profile["sheet"] in referenced
            and profile["sheet"] != _fact_sheet(layout)
        ):
            destination = "graph"
            reason = "Referenced by a formula on another sheet."
        elif destination == "table" and profile["sheet"] in endpoints:
            destination = "graph"
            reason = "Key column is the endpoint of another sheet."
        decisions.append(
            {
                "sheet": profile["sheet"],
                "destination": destination,
                "reason": reason,
                "row_count": profile["row_count"],
                "formula_count": int(formula_info.get("formula_count") or 0),
            }
        )
    return {
        "sheets": decisions,
        "profiles": profiles,
        "formulas": formulas,
        "layout": layout,
    }


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


def _edge_key_ok(series: pd.Series, node: dict) -> bool:
    """Reject shared labels, sentinels, and synthetic header names as edges."""
    name = str(series.name)
    id_column = str(node.get("id_column") or "")
    if name.startswith("Unnamed") or id_column.startswith("Unnamed"):
        return False
    if _sentinel_share(series) >= 0.5:
        return False
    values = set(_non_null(series).map(_value_key))
    if len(values) < MIN_EDGE_DISTINCT and not (
        _conventional_id(name) or _conventional_id(id_column)
    ):
        return False
    if not _conventional_id(name) and not _conventional_id(id_column):
        return False
    return True


def _rank_targets(
    series: pd.Series, nodes: list[dict], *, skip_sheet: str
) -> list[dict]:
    ranked = []
    for node in nodes:
        if node["sheet"] == skip_sheet or not node["id_values"]:
            continue
        overlap = _overlap(series, node["id_values"])
        if overlap < EDGE_OVERLAP or not _edge_key_ok(series, node):
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
            for start in range(0, len(values), BATCH_ROWS):
                transaction.insert_or_update(
                    plan["table"],
                    columns=columns,
                    values=values[start : start + BATCH_ROWS],
                )
        for plan in edge_plans:
            columns, values = _rows(plan, edge=True)
            for start in range(0, len(values), BATCH_ROWS):
                transaction.insert(
                    plan["table"],
                    columns=columns,
                    values=values[start : start + BATCH_ROWS],
                )

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


def table_id_for(workbook_name: str, sheet: str) -> str:
    stem = ident(workbook_name.rsplit(".", 1)[0], "workbook")
    return ident(f"{stem[:40]}_{sheet}", "table")[:1024]


def bq_table_name(workbook_name: str, sheet: str) -> str:
    dataset = settings.bq_dataset_id()
    project = settings.bq_project_id()
    if not dataset or not project:
        raise ValueError(
            "BQ_DATASET_ID and BQ_DATA_PROJECT_ID (or GOOGLE_CLOUD_PROJECT) "
            "must be set to load a tabular sheet."
        )
    settings.require_ident(dataset, "BQ_DATASET_ID")
    return f"{project}.{dataset}.{table_id_for(workbook_name, sheet)}"


def bigquery_client():
    project = settings.bq_project_id()
    compute = settings.env("BQ_COMPUTE_PROJECT_ID") or project
    if not compute:
        raise ValueError(
            "BQ_DATA_PROJECT_ID or GOOGLE_CLOUD_PROJECT must be set to load a table."
        )
    return bigquery.Client(project=compute)


def load_table(workbook_name: str, sheet: str, frame: pd.DataFrame) -> dict[str, Any]:
    """Replace one BigQuery table with a sheet of raw rows. No row cap."""
    table = bq_table_name(workbook_name, sheet)
    client = bigquery_client()
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        autodetect=True,
    )
    job = client.load_table_from_dataframe(frame, table, job_config=job_config)
    job.result(timeout=600)
    return {
        "sheet": sheet,
        "table": table,
        "table_id": table.rsplit(".", 1)[-1],
        "row_count": int(len(frame)),
    }


def drop_bq_tables(tables: list[str]) -> None:
    if not tables:
        return
    client = bigquery_client()
    for table in tables:
        client.delete_table(table, not_found_ok=True)


def _normalize_edges(edge_sheet, edge_sheets) -> list[dict]:
    if edge_sheets:
        return list(edge_sheets)
    if edge_sheet:
        return [edge_sheet]
    return []


def _load_graph(
    sheets: dict[str, pd.DataFrame],
    workbook_name: str,
    node_sheets: list[dict],
    edge_sheets: list[dict],
    *,
    declared: bool,
    analysis: dict | None,
) -> dict[str, Any]:
    name = graph_name_for(workbook_name)
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
    mapping["tables"] = []

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
        "graph_name": name,
        "node_tables": [plan["table"] for plan in node_plans],
        "edge_tables": [plan["table"] for plan in edge_plans],
        "edge_table": edge_plans[0]["table"] if len(edge_plans) == 1 else None,
        "row_counts": counts,
        "mapping": mapping,
        "confidence": mapping["confidence"],
        "inferred": not declared,
    }


def _drop_previous_graph(workbook_name: str) -> None:
    name = graph_name_for(workbook_name)
    previous = graph_schema(name)
    if not previous:
        return
    db = database()
    ddl = drop_stale_tables(name, [], [])
    ddl.append(f"DROP PROPERTY GRAPH IF EXISTS {name}")
    db.update_ddl(ddl).result(timeout=300)

    def _delete(transaction):
        transaction.delete(CATALOG, keyset=spanner.KeySet(keys=[[name]]))

    db.run_in_transaction(_delete)


def _previous_tables(workbook_name: str) -> list[str]:
    previous = graph_schema(graph_name_for(workbook_name))
    if not previous:
        return []
    return list((previous.get("mapping") or {}).get("tables") or [])


def load_workbook(
    workbook_bytes: bytes,
    workbook_name: str,
    node_sheets: list[dict[str, str]] | None = None,
    edge_sheet: dict[str, str] | None = None,
    edge_sheets: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Load each sheet as a BigQuery table or into one Spanner property graph.

    Large raw sheets and named-range fact tables become BigQuery tables.
    Aggregate dashboards are skipped. Smaller sheets with formulas or key
    overlap become the graph. An explicit node/edge mapping forces those
    sheets into the graph and leaves the other sheets as tables. Reloading
    the same filename replaces both sides and drops objects the new plan
    no longer uses.
    """
    sheets = read_sheets(workbook_bytes)
    if not sheets:
        raise ValueError("Workbook has no sheets.")
    classification = classify_sheets(workbook_bytes)
    by_sheet = {item["sheet"]: item for item in classification["sheets"]}
    declared = bool(node_sheets)
    if declared:
        declared_edges = _normalize_edges(edge_sheet, edge_sheets)
        declared_nodes = list(node_sheets or [])
        for sheet in {spec["sheet"] for spec in declared_nodes + declared_edges}:
            if sheet not in by_sheet:
                raise ValueError(f"Sheet {sheet!r} is not in the workbook.")
            by_sheet[sheet]["destination"] = "graph"
            by_sheet[sheet]["reason"] = "Declared by the caller."
    else:
        declared_nodes = []
        declared_edges = []

    graph_sheets = {
        sheet for sheet, item in by_sheet.items() if item["destination"] == "graph"
    }
    table_sheets = [
        sheet for sheet, item in by_sheet.items() if item["destination"] == "table"
    ]
    graph: dict[str, Any] | None = None
    node_specs: list[dict] = []
    edge_specs: list[dict] = []
    analysis = None
    if graph_sheets:
        if declared:
            analysis = {
                "confidence": "declared",
                "node_sheets": [
                    {
                        "sheet": spec["sheet"],
                        "confidence": "declared",
                        "reason": "Declared by the caller.",
                    }
                    for spec in declared_nodes
                ],
                "edge_sheets": declared_edges,
            }
            node_specs = declared_nodes
            edge_specs = declared_edges
        else:
            analysis = analyze_workbook(workbook_bytes)
            node_specs = [
                {"sheet": spec["sheet"], "id_column": spec["id_column"]}
                for spec in analysis["node_sheets"]
                if spec["sheet"] in graph_sheets
            ]
            edge_specs = [
                spec
                for spec in analysis["edge_sheets"]
                if spec["sheet"] in graph_sheets
                and spec.get("source_node") in graph_sheets
                and spec.get("target_node") in graph_sheets
            ]
            graph_sheets = {spec["sheet"] for spec in node_specs + edge_specs}
            table_sheets = [
                sheet
                for sheet, item in by_sheet.items()
                if sheet not in graph_sheets and item["destination"] != "skip"
            ]
            for item in by_sheet.values():
                if item["sheet"] in graph_sheets:
                    continue
                if item["destination"] == "graph":
                    item["destination"] = "table"
                    item["reason"] = (
                        "No node or edge role remained after graph planning."
                    )

    previous_tables = _previous_tables(workbook_name)
    tables = [
        load_table(workbook_name, sheet, _clean_frame(sheets[sheet]))
        for sheet in table_sheets
    ]
    loaded_ids = {item["table"] for item in tables}
    drop_bq_tables([table for table in previous_tables if table not in loaded_ids])

    if graph_sheets:
        graph = _load_graph(
            sheets,
            workbook_name,
            node_specs,
            edge_specs,
            declared=declared,
            analysis=analysis,
        )
        graph["mapping"]["tables"] = [item["table"] for item in tables]

        def _write(transaction):
            transaction.insert_or_update(
                CATALOG,
                columns=["graph_name", "workbook_name", "mapping_json", "loaded_at"],
                values=[
                    [
                        graph["graph_name"],
                        workbook_name[:256],
                        json.dumps(graph["mapping"]),
                        spanner.COMMIT_TIMESTAMP,
                    ]
                ],
            )

        database().run_in_transaction(_write)
    else:
        _drop_previous_graph(workbook_name)

    return {
        "status": "SUCCESS",
        "storage": "split" if tables and graph else ("graph" if graph else "table"),
        "tables": tables,
        "graph_name": graph["graph_name"] if graph else None,
        "node_tables": graph["node_tables"] if graph else [],
        "edge_tables": graph["edge_tables"] if graph else [],
        "edge_table": graph["edge_table"] if graph else None,
        "row_counts": {
            **{item["table"]: item["row_count"] for item in tables},
            **(graph["row_counts"] if graph else {}),
        },
        "mapping": (
            graph["mapping"]
            if graph
            else {"tables": [item["table"] for item in tables]}
        ),
        "confidence": graph["confidence"] if graph else None,
        "inferred": graph["inferred"] if graph else not declared,
        "classification": [
            {
                "sheet": item["sheet"],
                "destination": item["destination"],
                "reason": item["reason"],
                "row_count": item["row_count"],
                "formula_count": item["formula_count"],
            }
            for item in classification["sheets"]
        ],
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


def artifact_key(name: str) -> str:
    """Normalize a filename so a display name and a stored key can match."""
    stem = name.removeprefix("user:").strip().strip("\"'")
    stem = re.sub(r"\s+", " ", stem).casefold()
    return stem.replace(" ", "_")


def workbook_artifact_names(names: list[str]) -> list[str]:
    return [
        name
        for name in names
        if name.removeprefix("user:").casefold().endswith((".xlsx", ".xlsm"))
    ]


async def _load_named(tool_context: Any, name: str):
    for candidate in (name, f"user:{name.removeprefix('user:')}"):
        part = await tool_context.load_artifact(candidate)
        if part is not None and part.inline_data is not None and part.inline_data.data:
            return candidate, part
    return None, None


async def resolve_xlsx_artifact(
    tool_context: Any, artifact_name: str
) -> tuple[str | None, Any, str | None]:
    """Find an uploaded workbook even when the model repeats a display name.

    ADK Web stores uploads under the blob display name, or under a generated
    ``artifact_<invocation>_<index>`` key when that name is missing. The model
    often asks the user for the original filename and then looks that up. Match
    the given name, a space/underscore-normalized form, and the only workbook
    in the session before reporting a miss.

    Returns:
        (resolved filename, part, error). Exactly one of part or error is set.
    """
    requested = (artifact_name or "").strip().strip("\"'")
    if requested:
        found_name, part = await _load_named(tool_context, requested)
        if part is not None:
            return found_name, part, None

    try:
        names = list(await tool_context.list_artifacts())
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_artifacts failed while resolving %r: %s", requested, exc)
        names = []

    workbooks = workbook_artifact_names(names)
    if requested:
        wanted = artifact_key(requested)
        matches = [name for name in workbooks if artifact_key(name) == wanted]
        if len(matches) == 1:
            found_name, part = await _load_named(tool_context, matches[0])
            if part is not None:
                return found_name, part, None
        if len(matches) > 1:
            return None, None, (
                f"Multiple artifacts match {requested!r}: {matches}. "
                "Pass one of those filenames."
            )

    if len(workbooks) == 1:
        found_name, part = await _load_named(tool_context, workbooks[0])
        if part is not None:
            return found_name, part, None

    # A nameless upload is stored as artifact_<invocation>_<index>. Use that
    # only when the caller did not name a file and it is the sole artifact.
    if not requested and len(names) == 1 and not workbooks:
        found_name, part = await _load_named(tool_context, names[0])
        if part is not None:
            return found_name, part, None

    if workbooks:
        return None, None, (
            f"Artifact {requested!r} was not found. Uploaded workbooks in this "
            f"session: {workbooks}. Call load_xlsx again with one of those names."
        )
    if names:
        return None, None, (
            f"Artifact {requested!r} was not found. Session artifacts are "
            f"{names}, and none is an .xlsx workbook."
        )
    return None, None, (
        f"Artifact {requested!r} was not found, and this session has no "
        "artifacts. The upload may not have been saved."
    )


