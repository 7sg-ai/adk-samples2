import pytest

from data_science.sub_agents.database import neo4j_store, tools


class FakeSession:
    def __init__(self):
        self.statements = []

    def run(self, statement):
        self.statements.append(statement)
        return [{"ok": 1}]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_run_read_rejects_a_write():
    with pytest.raises(ValueError, match="read-only"):
        neo4j_store.run_read(
            "CREATE (n:Airport {id: 'SFO'})", session_factory=lambda: FakeSession()
        )


@pytest.mark.asyncio
async def test_query_neo4j_stores_rows(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(
        neo4j_store,
        "run_read",
        lambda statement, session_factory=None: session.run(statement),
    )
    stored = {}

    class Ctx:
        state = {}

    monkeypatch.setattr(
        tools, "_store", lambda ctx, source, rows: stored.update(source=source, rows=rows)
    )
    result = await tools.query("neo4j", "RETURN 1 AS ok", Ctx())
    assert result["rows"] == [{"ok": 1}]
    assert stored["source"] == "neo4j"
