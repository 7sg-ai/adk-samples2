import pytest
from data_science.sub_agents.database import settings


def test_graph_source_defaults_to_spanner(monkeypatch):
    monkeypatch.delenv("GRAPH_SOURCE", raising=False)
    assert settings.graph_source() == "spanner"


def test_graph_source_accepts_neo4j(monkeypatch):
    monkeypatch.setenv("GRAPH_SOURCE", "neo4j")
    assert settings.graph_source() == "neo4j"


def test_graph_source_rejects_unknown(monkeypatch):
    monkeypatch.setenv("GRAPH_SOURCE", "yugabyte")
    with pytest.raises(ValueError, match="GRAPH_SOURCE"):
        settings.graph_source()
