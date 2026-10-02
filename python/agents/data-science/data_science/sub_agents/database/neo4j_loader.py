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

"""Load planned workbook nodes and edges into Neo4j with MERGE statements.

Labels and property names are validated with settings.require_ident and are the
only text interpolated into Cypher. Every value is a Cypher parameter.
"""

from __future__ import annotations

from typing import Any

from data_science.sub_agents.database import settings, xlsx_loader


def _props(plan: dict, record: Any, skip: set[str] | None = None) -> dict:
    skip = skip or set()
    props = {}
    for col in plan["columns"]:
        name = settings.require_ident(col["name"], "property")
        if name in skip:
            continue
        props[name] = xlsx_loader.cell(record[col["sheet_column"]], col["type"])
    return props


def _id_value(plan: dict, record: Any) -> Any:
    column = next(c for c in plan["columns"] if c["name"] == plan["id_name"])
    return xlsx_loader.cell(record[column["sheet_column"]], column["type"])


def _load_nodes(plan: dict, session) -> int:
    label = settings.require_ident(plan["label"], "label")
    id_name = settings.require_ident(plan["id_name"], "id property")
    statement = f"MERGE (n:{label} {{{id_name}: $id}}) SET n += $props"
    count = 0
    for _, record in plan["frame"].iterrows():
        session.run(statement, id=_id_value(plan, record), props=_props(plan, record))
        count += 1
    return count


def _load_edges(plan: dict, session) -> int:
    label = settings.require_ident(plan["label"], "label")
    source, target = plan["source_node"], plan["target_node"]
    source_label = settings.require_ident(source["label"], "label")
    target_label = settings.require_ident(target["label"], "label")
    source_id = settings.require_ident(source["id_name"], "id property")
    target_id = settings.require_ident(target["id_name"], "id property")
    source_name = settings.require_ident(plan["source_name"], "source property")
    target_name = settings.require_ident(plan["target_name"], "target property")
    statement = (
        f"MATCH (a:{source_label} {{{source_id}: $source}}), "
        f"(b:{target_label} {{{target_id}: $target}}) "
        f"MERGE (a)-[r:{label}]->(b) SET r += $props"
    )
    columns = {c["name"]: c for c in plan["columns"]}
    count = 0
    for _, record in plan["frame"].iterrows():
        src = columns[source_name]
        dst = columns[target_name]
        session.run(
            statement,
            source=xlsx_loader.cell(record[src["sheet_column"]], src["type"]),
            target=xlsx_loader.cell(record[dst["sheet_column"]], dst["type"]),
            props=_props(plan, record, skip={source_name, target_name}),
        )
        count += 1
    return count


def load_graph(node_plans: list[dict], edge_plans: list[dict], session) -> dict:
    """MERGE every node row, then every edge row, and return the row counts."""
    nodes = sum(_load_nodes(plan, session) for plan in node_plans)
    edges = sum(_load_edges(plan, session) for plan in edge_plans)
    return {"nodes": nodes, "edges": edges}
