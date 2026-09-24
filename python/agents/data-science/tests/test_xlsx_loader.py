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

"""Unit tests for workbook-to-Spanner-Graph planning. No live Spanner."""

import io

import pandas as pd
import pytest

from data_science.sub_agents.database import xlsx_loader


def _workbook() -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(
            {"person_id": [1, 2], "name": ["Ada", "Gus"]}
        ).to_excel(writer, sheet_name="People", index=False)
        pd.DataFrame(
            {"source": [1], "target": [2], "since": ["2020"]}
        ).to_excel(writer, sheet_name="Knows", index=False)
        pd.DataFrame({"note": ["ignore me"]}).to_excel(
            writer, sheet_name="Notes", index=False
        )
    return buffer.getvalue()


def test_declared_mapping_builds_one_graph_and_ignores_undeclared_sheets(monkeypatch):
    captured = {}

    class FakeDb:
        def update_ddl(self, statements):
            captured["ddl"] = statements

            class Op:
                def result(self, timeout=0):
                    return None

            return Op()

        def run_in_transaction(self, fn):
            captured.setdefault("writes", []).append(fn)

        def snapshot(self):
            raise AssertionError("schema lookup is not part of this test")

    monkeypatch.setattr(xlsx_loader, "database", lambda: FakeDb())
    monkeypatch.setattr(xlsx_loader, "ensure_catalog", lambda db: None)
    monkeypatch.setattr(xlsx_loader, "insert_rows", lambda *args: None)

    result = xlsx_loader.load_workbook(
        _workbook(),
        "friends.xlsx",
        node_sheets=[{"sheet": "People", "id_column": "person_id"}],
        edge_sheet={
            "sheet": "Knows",
            "source_column": "source",
            "target_column": "target",
            "source_node": "People",
            "target_node": "People",
        },
    )

    assert result["status"] == "SUCCESS"
    assert result["graph_name"].startswith("g_friends_")
    ddl = "\n".join(captured["ddl"])
    assert "CREATE OR REPLACE PROPERTY GRAPH" in ddl
    assert "SOURCE KEY (source) REFERENCES" in ddl
    assert "DESTINATION KEY (target) REFERENCES" in ddl
    assert "Notes" not in ddl
    assert "CREATE TABLE IF NOT EXISTS workbook_catalog" not in ddl


def test_missing_edge_declaration_does_not_infer_edges(monkeypatch):
    class FakeDb:
        def update_ddl(self, statements):
            self.statements = statements

            class Op:
                def result(self, timeout=0):
                    return None

            return Op()

        def run_in_transaction(self, fn):
            return None

    fake = FakeDb()
    monkeypatch.setattr(xlsx_loader, "database", lambda: fake)
    monkeypatch.setattr(xlsx_loader, "ensure_catalog", lambda db: None)
    monkeypatch.setattr(xlsx_loader, "insert_rows", lambda *args: None)
    xlsx_loader.load_workbook(
        _workbook(),
        "friends.xlsx",
        node_sheets=[{"sheet": "People", "id_column": "person_id"}],
    )
    ddl = "\n".join(fake.statements)
    assert "EDGE TABLES" not in ddl
    assert "Knows" not in ddl


def test_missing_id_column_is_rejected():
    with pytest.raises(ValueError, match="id column"):
        xlsx_loader._node_plan(
            {"People": pd.DataFrame({"name": ["Ada"]})},
            {"sheet": "People", "id_column": "person_id"},
            "g_test",
        )


def test_guard_sql_allows_graph_and_blocks_dml():
    assert xlsx_loader.guard_sql(
        "GRAPH g MATCH (n) RETURN n LIMIT 5"
    ).startswith("GRAPH")
    with pytest.raises(ValueError):
        xlsx_loader.guard_sql("DROP PROPERTY GRAPH g")
    with pytest.raises(ValueError):
        xlsx_loader.guard_sql("SELECT 1; SELECT 2")
