import io

import pandas as pd
import pytest

from data_science.sub_agents.database import (
    neo4j_loader,
    neo4j_store,
    settings,
    tools as db_tools,
    xlsx_loader,
)


def _workbook() -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"person_id": [1, 2], "name": ["Ada", "Gus"]}).to_excel(
            writer, sheet_name="People", index=False
        )
        pd.DataFrame({"source": [1], "target": [2], "since": ["2020"]}).to_excel(
            writer, sheet_name="Knows", index=False
        )
        pd.DataFrame({"note": ["ignore me"]}).to_excel(
            writer, sheet_name="Notes", index=False
        )
    return buffer.getvalue()


class RecordingSession:
    def __init__(self):
        self.statements = []

    def run(self, statement, **params):
        self.statements.append((statement, params))


class _ToolContext:
    def __init__(self):
        self.state = {}

    async def load_artifact(self, filename):
        class _Blob:
            data = _workbook()

        class _Part:
            inline_data = _Blob()

        return _Part()


def test_load_graph_merges_inferred_nodes_and_edge():
    session = RecordingSession()
    node_plans, edge_plans = xlsx_loader.plan_workbook(_workbook(), "friends.xlsx")
    counts = neo4j_loader.load_graph(node_plans, edge_plans, session)
    assert counts == {"nodes": 3, "edges": 1}
    rendered = "\n".join(statement for statement, _ in session.statements)
    assert "MERGE (n:People" in rendered
    assert "MERGE (a)-[r:Knows]->(b)" in rendered


def test_load_graph_passes_values_as_parameters():
    session = RecordingSession()
    node_plans, edge_plans = xlsx_loader.plan_workbook(_workbook(), "friends.xlsx")
    neo4j_loader.load_graph(node_plans, edge_plans, session)
    rendered = "\n".join(statement for statement, _ in session.statements)
    assert "Ada" not in rendered
    assert any(params.get("props", {}).get("name") == "Ada" for _, params in session.statements)


@pytest.mark.asyncio
async def test_load_xlsx_uses_spanner_loader_by_default(monkeypatch):
    monkeypatch.delenv("GRAPH_SOURCE", raising=False)
    calls = []
    monkeypatch.setattr(
        xlsx_loader,
        "load_workbook",
        lambda **kwargs: calls.append(kwargs) or {"graph_name": "g"},
    )
    monkeypatch.setattr(
        neo4j_loader,
        "load_graph",
        lambda *args, **kwargs: pytest.fail("neo4j load_graph must not run"),
    )
    loaded = await db_tools.load_xlsx(_ToolContext(), "friends.xlsx")
    assert loaded == {"graph_name": "g"}
    assert calls and calls[0]["workbook_name"] == "friends.xlsx"


@pytest.mark.asyncio
async def test_load_xlsx_spanner_when_graph_source_patched(monkeypatch):
    monkeypatch.setattr(settings, "graph_source", lambda: "spanner")
    calls = []
    monkeypatch.setattr(
        xlsx_loader, "load_workbook", lambda **kwargs: calls.append(kwargs) or {"ok": 1}
    )
    monkeypatch.setattr(
        neo4j_loader,
        "load_graph",
        lambda *args, **kwargs: pytest.fail("neo4j load_graph must not run"),
    )
    await db_tools.load_xlsx(_ToolContext(), "friends.xlsx")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_load_xlsx_uses_neo4j_loader_when_selected(monkeypatch):
    monkeypatch.setattr(settings, "graph_source", lambda: "neo4j")
    session = RecordingSession()

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(neo4j_store, "write_session", lambda: _Ctx())
    monkeypatch.setattr(
        xlsx_loader,
        "load_workbook",
        lambda **kwargs: pytest.fail("spanner load_workbook must not run"),
    )
    context = _ToolContext()
    loaded = await db_tools.load_xlsx(context, "friends.xlsx")
    assert loaded["status"] == "SUCCESS"
    assert loaded["nodes"] == 3 and loaded["edges"] == 1
    assert context.state["loaded_graph"] == loaded
    assert session.statements


@pytest.mark.asyncio
async def test_neo4j_load_returns_inferred_mapping(monkeypatch):
    monkeypatch.setenv("GRAPH_SOURCE", "neo4j")
    session = RecordingSession()

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(neo4j_store, "write_session", lambda: _Ctx())
    loaded = await db_tools.load_xlsx(_ToolContext(), "friends.xlsx")
    assert loaded["status"] == "SUCCESS"
    assert loaded["source"] == "neo4j"
    assert loaded["nodes"] == 3 and loaded["edges"] == 1
    assert loaded["confidence"] in {"high", "medium", "low"}
    mapping = loaded["mapping"]
    assert {"sheet": "People", "id_column": "person_id"} in mapping["nodes"]
    assert {node["sheet"] for node in mapping["nodes"]} == {"People", "Notes"}
    assert mapping["edges"] == [
        {
            "sheet": "Knows",
            "source_column": "source",
            "target_column": "target",
            "source_node": "People",
            "target_node": "People",
        }
    ]
