"""Acme workbook tables, and the private-ntt config stays off the Cloud Run path."""

import json
import os
from pathlib import Path

from data_science.sub_agents.database.dataset_config import uses_cluster_stores
from data_science.sub_agents.database.prompts import return_instructions_database
from data_science.sub_agents.database.workbook_model import guard_cypher, load_workbook_tables

APP_ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = APP_ROOT / "sample_data" / "Acme Corp Financial Model.xlsx"


def _field(tables, sheet, label):
    return next(
        item["value"]
        for item in tables["fields"]
        if item["sheet"] == sheet and item["label"] == label
    )


def _line(tables, sheet, line_item, period):
    return next(
        item["amount"]
        for item in tables["lines"]
        if item["sheet"] == sheet and item["line_item"] == line_item and item["period"] == period
    )


def test_workbook_tables_hold_debt_income_and_sensitivity():
    tables = load_workbook_tables(WORKBOOK)

    assert _field(tables, "Debt Schedule", "Principal ($mm)") == "150"
    assert _field(tables, "Debt Schedule", "Annual interest rate") == "0.07"
    assert _field(tables, "Debt Schedule", "Annual debt service").startswith("27.1668")
    assert _field(tables, "Debt Schedule", "Level periodic payment (PMT)").startswith("2.2639")
    assert _line(tables, "Income Statement", "Revenue", "2026E") == "540"
    assert _line(tables, "Debt Schedule", "Total debt service", "2026E").startswith("27.1668")
    price = next(
        item["share_price"]
        for item in tables["sensitivity"]
        if item["wacc"] == "0.1031" and item["axis_value"] == "0.025"
    )
    assert price.startswith("14.669")
    assert tables["payments"][0]["payment"].startswith("2.2639")
    assert tables["payments"][0]["payment_date"] == "2026-01-31"


def test_cypher_guard_rejects_writes():
    assert guard_cypher("MATCH (n:Field) RETURN n.label LIMIT 5").startswith("MATCH")
    try:
        guard_cypher("MATCH (n) DETACH DELETE n")
    except ValueError:
        return
    raise AssertionError("write cypher was accepted")


def test_flights_config_keeps_cloud_instructions(monkeypatch):
    monkeypatch.setenv("DATASET_CONFIG_FILE", str(APP_ROOT / "flights_dataset_config.json"))
    assert uses_cluster_stores() is False
    text = return_instructions_database()
    assert 'source is "bigquery", "spanner", or "neo4j"' in text
    assert "sheet_fields" not in text


def test_private_ntt_config_uses_cluster_instructions(monkeypatch):
    monkeypatch.setenv("DATASET_CONFIG_FILE", str(APP_ROOT / "private_ntt_dataset_config.json"))
    config = json.loads((APP_ROOT / "private_ntt_dataset_config.json").read_text())
    assert [item["type"] for item in config["datasets"]] == ["postgres", "neo4j"]
    assert uses_cluster_stores() is True
    text = return_instructions_database()
    assert "sheet_fields" in text
    assert "Do not query BigQuery or Spanner." in text
    assert os.environ["DATASET_CONFIG_FILE"].endswith("private_ntt_dataset_config.json")
