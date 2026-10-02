"""Postgres and Neo4j access for the private-ntt dataset config.

Cloud Run keeps using BigQuery and Spanner. This module is called only when
the dataset file declares postgres or neo4j.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from data_science.sub_agents.database.workbook_model import guard_cypher, load_workbook_tables
from data_science.sub_agents.database.xlsx_loader import guard_sql

TABLES = (
    "sheet_fields",
    "statement_lines",
    "debt_payments",
    "dcf_sensitivity",
    "workbook_cells",
)
_COLUMNS = {
    "sheet_fields": ("sheet", "label", "value"),
    "statement_lines": ("sheet", "line_item", "period", "amount"),
    "debt_payments": (
        "period",
        "payment_date",
        "year",
        "beginning_balance",
        "payment",
        "interest",
        "principal",
        "ending_balance",
    ),
    "dcf_sensitivity": ("table_name", "wacc", "axis_value", "share_price"),
    "workbook_cells": ("sheet", "row_number", "col_number", "value"),
}
_LABEL_TABLE = {
    "Field": "sheet_fields",
    "StatementLine": "statement_lines",
    "DebtPayment": "debt_payments",
    "DcfSensitivity": "dcf_sensitivity",
    "Sheet": "sheet_fields",
}


def list_sources() -> dict:
    tables = _postgres(_table_names)
    labels = _neo4j("MATCH (n) RETURN DISTINCT labels(n) AS labels LIMIT 20", {})
    return {"postgres": {"tables": tables}, "neo4j": {"labels": labels}}


def get_schema(source: str, name: str) -> dict:
    if source == "postgres":
        if name not in _COLUMNS:
            return {"status": "NOT_FOUND", "table": name}
        return {"table": name, "columns": [{"name": column, "type": "text"} for column in _COLUMNS[name]]}
    if name == "Sheet":
        return {"label": name, "properties": ["name"]}
    if name not in _LABEL_TABLE:
        return {"status": "NOT_FOUND", "label": name}
    return {"label": name, "properties": list(_COLUMNS[_LABEL_TABLE[name]])}


def query_source(source: str, statement: str) -> dict:
    if source == "postgres":
        sql = guard_sql(statement)
        rows = _postgres(lambda conn: _sql_rows(conn, sql))
        return {"rows": rows, "row_count": len(rows)}
    cypher = guard_cypher(statement)
    rows = _neo4j(cypher, {})
    return {"rows": rows, "row_count": len(rows)}


def load_workbook(path: str | Path) -> dict:
    tables = load_workbook_tables(path)
    _postgres(lambda conn: _replace_postgres(conn, tables), retry=True)
    _replace_neo4j(tables)
    return {
        "status": "loaded",
        "workbook": str(path),
        "sheet_fields": len(tables["fields"]),
        "statement_lines": len(tables["lines"]),
        "debt_payments": len(tables["payments"]),
        "dcf_sensitivity": len(tables["sensitivity"]),
        "workbook_cells": len(tables["cells"]),
    }


def _postgres(operation, *, retry: bool = False):
    last: Exception | None = None
    attempts = 40 if retry else 1
    for _ in range(attempts):
        try:
            import pg8000.native

            conn = pg8000.native.Connection(
                user=os.environ["POSTGRES_USER"],
                password=os.environ["POSTGRES_PASSWORD"],
                host=os.environ["POSTGRES_HOST"],
                port=int(os.environ.get("POSTGRES_PORT", "5432")),
                database=os.environ["POSTGRES_DB"],
            )
            try:
                return operation(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - startup retry is only for the load
            last = exc
            if not retry:
                raise
            time.sleep(3)
    raise RuntimeError(f"postgres_unavailable: {last}")


def _table_names(conn) -> list[str]:
    rows = conn.run(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    )
    return [str(row[0]) for row in rows]


def _sql_rows(conn, sql: str) -> list[dict]:
    rows = conn.run(sql)
    names = [str(column["name"]) for column in (conn.columns or [])]
    return [dict(zip(names, row, strict=False)) for row in rows]


def _replace_postgres(conn, tables: dict) -> None:
    conn.run(
        "DROP TABLE IF EXISTS sheet_fields, statement_lines, debt_payments, "
        "dcf_sensitivity, workbook_cells"
    )
    conn.run("CREATE TABLE sheet_fields (sheet text, label text, value text)")
    conn.run(
        "CREATE TABLE statement_lines (sheet text, line_item text, period text, amount text)"
    )
    conn.run(
        "CREATE TABLE debt_payments ("
        "period integer, payment_date text, year text, beginning_balance text, "
        "payment text, interest text, principal text, ending_balance text)"
    )
    conn.run(
        "CREATE TABLE dcf_sensitivity ("
        "table_name text, wacc text, axis_value text, share_price text)"
    )
    conn.run(
        "CREATE TABLE workbook_cells ("
        "sheet text, row_number integer, col_number integer, value text)"
    )
    _insert(conn, "sheet_fields", tables["fields"])
    _insert(conn, "statement_lines", tables["lines"])
    _insert(conn, "debt_payments", tables["payments"])
    _insert(conn, "dcf_sensitivity", tables["sensitivity"])
    _insert(conn, "workbook_cells", tables["cells"])


def _insert(conn, table: str, rows: list[dict]) -> None:
    columns = _COLUMNS[table]
    names = ", ".join(columns)
    holders = ", ".join(f":{column}" for column in columns)
    statement = f"INSERT INTO {table} ({names}) VALUES ({holders})"
    for row in rows:
        conn.run(statement, **{column: row.get(column) for column in columns})


def _replace_neo4j(tables: dict) -> None:
    _neo4j("MATCH (n) DETACH DELETE n", {}, write=True, retry=True)
    _neo4j_rows(
        "UNWIND $rows AS row MERGE (s:Sheet {name: row.sheet}) "
        "CREATE (n:Field {sheet: row.sheet, label: row.label, value: row.value}) "
        "CREATE (s)-[:HAS_FIELD]->(n)",
        tables["fields"],
    )
    _neo4j_rows(
        "UNWIND $rows AS row MERGE (s:Sheet {name: row.sheet}) "
        "CREATE (n:StatementLine {sheet: row.sheet, line_item: row.line_item, "
        "period: row.period, amount: row.amount}) "
        "CREATE (s)-[:HAS_LINE]->(n)",
        tables["lines"],
    )
    _neo4j_rows(
        "UNWIND $rows AS row MERGE (s:Sheet {name: 'Debt Schedule'}) "
        "CREATE (n:DebtPayment {period: row.period, payment_date: row.payment_date, "
        "year: row.year, beginning_balance: row.beginning_balance, payment: row.payment, "
        "interest: row.interest, principal: row.principal, ending_balance: row.ending_balance}) "
        "CREATE (s)-[:HAS_PAYMENT]->(n)",
        tables["payments"],
    )
    _neo4j_rows(
        "UNWIND $rows AS row MERGE (s:Sheet {name: 'DCF'}) "
        "CREATE (n:DcfSensitivity {table_name: row.table_name, wacc: row.wacc, "
        "axis_value: row.axis_value, share_price: row.share_price}) "
        "CREATE (s)-[:HAS_SENSITIVITY]->(n)",
        tables["sensitivity"],
    )


def _neo4j_rows(statement: str, rows: list[dict]) -> None:
    for start in range(0, len(rows), 200):
        _neo4j(statement, {"rows": rows[start : start + 200]}, write=True, retry=True)


def _neo4j(statement: str, parameters: dict, *, write: bool = False, retry: bool = False) -> list[dict]:
    if not write:
        statement = guard_cypher(statement)
    last: Exception | None = None
    attempts = 40 if retry else 1
    for _ in range(attempts):
        try:
            return _neo4j_once(statement, parameters)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if not retry:
                raise
            time.sleep(3)
        except RuntimeError as exc:
            last = exc
            if not retry or not str(exc).startswith("neo4j_http_5"):
                raise
            time.sleep(3)
    raise RuntimeError(f"neo4j_unavailable: {last}")


def _neo4j_once(statement: str, parameters: dict) -> list[dict]:
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ["NEO4J_PASSWORD"]
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    body = json.dumps({
        "statements": [{"statement": statement, "parameters": parameters}]
    }).encode("utf-8")
    request = urllib.request.Request(_neo4j_url(), data=body, method="POST")
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"neo4j_http_{exc.code}: {detail}") from exc
    errors = payload.get("errors") or []
    if errors:
        raise RuntimeError(str(errors[0].get("message") or "neo4j_error")[:240])
    rows: list[dict] = []
    for result in payload.get("results") or []:
        columns = result.get("columns") or []
        for item in result.get("data") or []:
            raw = item.get("row") or []
            rows.append(dict(zip(columns, [_plain(value) for value in raw], strict=False)))
    return rows


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return json.dumps(value)


def _neo4j_url() -> str:
    explicit = os.environ.get("NEO4J_HTTP", "")
    if explicit:
        return explicit
    uri = os.environ.get("NEO4J_URI", "bolt://neo4j.ntt-tenant-a.svc:7687")
    host = uri.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
    return f"http://{host}:7474/db/neo4j/tx/commit"
