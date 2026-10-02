"""Turn the Acme workbook into queryable tables. No database client."""

from __future__ import annotations

import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
YEAR = re.compile(r"^\d{4}[AE]$")
_CYPHER_READ = re.compile(
    r"^\s*(MATCH|OPTIONAL\s+MATCH|WITH|RETURN|UNWIND)\b",
    re.IGNORECASE | re.DOTALL,
)
_CYPHER_FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|CALL|FOREACH)\b",
    re.IGNORECASE,
)

PAYMENT_COLUMNS = {
    "period": "period",
    "payment date": "payment_date",
    "year": "year",
    "beginning balance": "beginning_balance",
    "payment": "payment",
    "interest": "interest",
    "principal": "principal",
    "ending balance": "ending_balance",
}


def guard_cypher(statement: str) -> str:
    text = statement.strip().rstrip(";")
    if not _CYPHER_READ.match(text) or _CYPHER_FORBIDDEN.search(text) or ";" in text:
        raise ValueError("Only a single read-only Cypher query is allowed.")
    return text


def load_workbook_tables(path: str | Path) -> dict:
    sheets = _read_sheets(Path(path))
    cells: list[dict] = []
    fields: list[dict] = []
    lines: list[dict] = []
    payments: list[dict] = []
    sensitivity: list[dict] = []
    for sheet, grid in sheets.items():
        for row_number, columns in grid.items():
            for col_number, value in columns.items():
                cells.append({
                    "sheet": sheet,
                    "row_number": row_number,
                    "col_number": col_number,
                    "value": value,
                })
        consumed: set[int] = set()
        lines.extend(_statement_lines(sheet, grid, consumed))
        payments.extend(_payments(sheet, grid, consumed))
        sensitivity.extend(_sensitivity(sheet, grid, consumed))
        fields.extend(_fields(sheet, grid, consumed))
    return {
        "cells": cells,
        "fields": fields,
        "lines": lines,
        "payments": payments,
        "sensitivity": sensitivity,
    }


def _statement_lines(sheet: str, grid: dict[int, dict[int, str]], consumed: set[int]) -> list[dict]:
    rows = sorted(grid)
    headers = [(row, _period_map(grid[row])) for row in rows]
    headers = [(row, periods) for row, periods in headers if periods]
    found: list[dict] = []
    header_rows = {row for row, _ in headers}
    for header_row, periods in headers:
        consumed.add(header_row)
        for row in rows:
            if row <= header_row:
                continue
            if row in header_rows:
                break
            label = grid[row].get(2, "").strip()
            amounts = {
                period: grid[row].get(col, "").strip()
                for col, period in periods.items()
                if grid[row].get(col, "").strip()
            }
            if not amounts:
                if label:
                    break
                continue
            consumed.add(row)
            if not label:
                continue
            for period, amount in amounts.items():
                found.append({
                    "sheet": sheet,
                    "line_item": label,
                    "period": period,
                    "amount": amount,
                })
    return found


def _period_map(row: dict[int, str]) -> dict[int, str]:
    years = sorted(col for col, value in row.items() if YEAR.match(value.strip()))
    if len(years) < 2:
        return {}
    return {
        col: value.strip()
        for col, value in row.items()
        if col >= years[0] and value.strip()
    }


def _payments(sheet: str, grid: dict[int, dict[int, str]], consumed: set[int]) -> list[dict]:
    if sheet != "Debt Schedule":
        return []
    header_row = 0
    columns: dict[str, int] = {}
    for row, values in grid.items():
        mapped = {}
        for col, value in values.items():
            key = PAYMENT_COLUMNS.get(value.strip().lower())
            if key:
                mapped[key] = col
        if "beginning_balance" in mapped and "principal" in mapped and "period" in mapped:
            header_row = row
            columns = mapped
            break
    if not header_row:
        return []
    consumed.add(header_row)
    found: list[dict] = []
    for row in sorted(row for row in grid if row > header_row):
        values = grid[row]
        period_raw = values.get(columns["period"], "").strip()
        if not period_raw.isdigit():
            break
        payment = values.get(columns["payment"], "").strip()
        beginning = values.get(columns["beginning_balance"], "").strip()
        if payment in {"", "0"} and beginning in {"", "0"}:
            break
        consumed.add(row)
        item = {"period": int(period_raw)}
        for key, col in columns.items():
            if key == "period":
                continue
            raw = values.get(col, "").strip()
            item[key] = _excel_date(raw) if key == "payment_date" else raw
        found.append(item)
    return found


def _sensitivity(sheet: str, grid: dict[int, dict[int, str]], consumed: set[int]) -> list[dict]:
    if sheet != "DCF":
        return []
    found: list[dict] = []
    rows = sorted(grid)
    for index, row in enumerate(rows):
        title = grid[row].get(2, "")
        if "sensitivity" not in title.lower():
            continue
        if index + 1 >= len(rows):
            continue
        header = rows[index + 1]
        axis = {
            col: grid[header].get(col, "").strip()
            for col in sorted(grid[header])
            if col >= 4 and grid[header].get(col, "").strip()
        }
        if len(axis) < 2:
            continue
        consumed.add(row)
        consumed.add(header)
        for data_row in rows[index + 2 :]:
            wacc = grid[data_row].get(3, "").strip()
            if not wacc:
                break
            prices = {
                col: grid[data_row].get(col, "").strip()
                for col in axis
                if grid[data_row].get(col, "").strip()
            }
            if not prices:
                break
            consumed.add(data_row)
            for col, price in prices.items():
                found.append({
                    "table_name": title.strip(),
                    "wacc": wacc,
                    "axis_value": axis[col],
                    "share_price": price,
                })
    return found


def _fields(sheet: str, grid: dict[int, dict[int, str]], consumed: set[int]) -> list[dict]:
    found: list[dict] = []
    for row, values in grid.items():
        if row in consumed:
            continue
        label = values.get(2, "").strip()
        value = values.get(3, "").strip()
        if not label or not value or YEAR.match(label):
            continue
        found.append({"sheet": sheet, "label": label, "value": value})
    return found


def _excel_date(value: str) -> str:
    try:
        serial = float(value)
    except ValueError:
        return value
    if not 20000 <= serial <= 80000:
        return value
    return (date(1899, 12, 30) + timedelta(days=int(serial))).isoformat()


def _read_sheets(path: Path) -> dict[str, dict[int, dict[int, str]]]:
    with zipfile.ZipFile(path) as workbook:
        strings = _shared_strings(workbook)
        workbook_xml = ET.fromstring(workbook.read("xl/workbook.xml"))
        rels = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in rels}
        sheets: dict[str, dict[int, dict[int, str]]] = {}
        for sheet in workbook_xml.findall("m:sheets/m:sheet", NS):
            relation = sheet.attrib[
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            ]
            target = targets[relation]
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            sheets[sheet.attrib["name"]] = _sheet_grid(workbook.read(target), strings)
    return sheets


def _shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.findall(".//m:t", NS))
        for item in root.findall("m:si", NS)
    ]


def _sheet_grid(payload: bytes, strings: list[str]) -> dict[int, dict[int, str]]:
    root = ET.fromstring(payload)
    grid: dict[int, dict[int, str]] = {}
    for cell in root.findall("m:sheetData/m:row/m:c", NS):
        row, col = _cell_ref(cell.attrib.get("r", "A1"))
        value = _cell_value(cell, strings)
        if value == "":
            continue
        grid.setdefault(row, {})[col] = value
    return grid


def _cell_ref(ref: str) -> tuple[int, int]:
    letters = "".join(char for char in ref if char.isalpha())
    row = int("".join(char for char in ref if char.isdigit()) or "0")
    col = 0
    for char in letters:
        col = col * 26 + ord(char.upper()) - 64
    return row, col


def _cell_value(cell: ET.Element, strings: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", NS)).strip()
    node = cell.find("m:v", NS)
    if node is None or node.text is None:
        return ""
    if kind == "s":
        return strings[int(node.text)].strip()
    return node.text.strip()
