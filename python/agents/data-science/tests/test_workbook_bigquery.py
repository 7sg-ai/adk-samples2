"""workbook_data table names are qualified, and the WDF Gemini model is selected."""

import os

from data_science.sub_agents.database.prompts import return_instructions_database
from data_science.sub_agents.database.settings import configured_model, qualify_bigquery_sql


def test_bare_acme_table_is_qualified_with_workbook_data():
    sql = qualify_bigquery_sql(
        "SELECT line_item FROM acme_income_statement LIMIT 5",
        "workbook_data",
        "gen-lang-client-0373235205",
    )
    assert "`gen-lang-client-0373235205`.workbook_data.acme_income_statement" in sql
    assert "FROM acme_income_statement" not in sql


def test_already_qualified_table_and_cte_stay_put():
    sql = qualify_bigquery_sql(
        "WITH src AS (SELECT 1 AS n FROM workbook_data.acme_summary) "
        "SELECT n FROM src LIMIT 1",
        "workbook_data",
        "gen-lang-client-0373235205",
    )
    assert "workbook_data.workbook_data" not in sql
    assert "FROM src" in sql


def test_model_comes_from_the_wdf_env(monkeypatch):
    monkeypatch.setenv("DATABASE_AGENT_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("ROOT_AGENT_MODEL", "gemini-2.0-flash")
    assert configured_model("DATABASE_AGENT_MODEL") == "gemini-2.5-flash"
    monkeypatch.delenv("DATABASE_AGENT_MODEL")
    assert configured_model("DATABASE_AGENT_MODEL") == "gemini-2.0-flash"
    monkeypatch.delenv("ROOT_AGENT_MODEL")
    assert configured_model("DATABASE_AGENT_MODEL") == "gemini-2.5-flash"


def test_cloud_instructions_name_workbook_data(monkeypatch):
    monkeypatch.setenv("BQ_DATASET_ID", "workbook_data")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gen-lang-client-0373235205")
    monkeypatch.setenv("DATABASE_AGENT_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("DATASET_CONFIG_FILE", raising=False)
    text = return_instructions_database()
    assert "workbook_data.table_id" in text
    assert "acme_income_statement" in text
    assert "gemini-2.5-flash" in text
    assert "sheet_fields" not in text
