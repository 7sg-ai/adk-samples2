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

"""Shared list / schema / query / load surface for BigQuery and Spanner.

Built-in ADK toolsets stay read-only. The only mutation is load_xlsx, which
writes a Spanner Graph schema through the Spanner client, not model DDL.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.adk.tools import ToolContext
from google.adk.tools.bigquery import BigQueryToolset
from google.adk.tools.bigquery.config import BigQueryToolConfig, WriteMode
from google.adk.tools.spanner.settings import SpannerToolSettings
from google.adk.tools.spanner.spanner_credentials import SpannerCredentialsConfig
from google.adk.tools.spanner.spanner_toolset import SpannerToolset
from google.cloud import bigquery

from data_science.utils.utils import USER_AGENT

from data_science.sub_agents.database import settings, xlsx_loader

logger = logging.getLogger(__name__)

_bq_toolset = BigQueryToolset(
    tool_filter=[
        "list_dataset_ids",
        "list_table_ids",
        "get_table_info",
        "execute_sql",
    ],
    bigquery_tool_config=BigQueryToolConfig(
        write_mode=WriteMode.BLOCKED,
        application_name=USER_AGENT,
        compute_project_id=os.getenv("BQ_COMPUTE_PROJECT_ID"),
        max_query_result_rows=1000,
    ),
)
_spanner_toolset = SpannerToolset(
    tool_filter=["list_table_names", "get_table_schema", "execute_sql"],
    credentials_config=SpannerCredentialsConfig(),
    spanner_tool_settings=SpannerToolSettings(max_executed_query_result_rows=1000),
)


async def _tool(toolset, name: str):
    tools = await toolset.get_tools()
    for tool in tools:
        if tool.name == name:
            return tool
    raise RuntimeError(f"Tool {name} is not available.")


def _store(tool_context: ToolContext, source: str, payload: Any) -> None:
    tool_context.state["query_result"] = payload
    tool_context.state["query_result_source"] = source

async def list_sources(tool_context: ToolContext) -> dict:
    """List BigQuery datasets/tables and Spanner graphs loaded from workbooks.

    Returns:
        Dict with bigquery datasets and spanner graphs. Missing Spanner
        configuration is returned in-band instead of raising.
    """
    project = settings.bq_project_id()
    dataset = settings.bq_dataset_id()
    bigquery_sources: dict[str, Any] = {
        "project_id": project,
        "dataset_id": dataset,
    }
    try:
        list_datasets = await _tool(_bq_toolset, "list_dataset_ids")
        bigquery_sources["datasets"] = await list_datasets.run_async(
            args={"project_id": project}, tool_context=tool_context
        )
        if dataset:
            list_tables = await _tool(_bq_toolset, "list_table_ids")
            bigquery_sources["tables"] = await list_tables.run_async(
                args={"project_id": project, "dataset_id": dataset},
                tool_context=tool_context,
            )
    except Exception as exc:  # noqa: BLE001 - surface config errors to the model
        logger.exception("BigQuery list_sources failed")
        bigquery_sources["error"] = str(exc)

    spanner_sources: dict[str, Any] = {
        "project_id": settings.spanner_project_id(),
        "instance_id": settings.spanner_instance_id(),
        "database_id": settings.spanner_database_id(),
    }
    try:
        if settings.spanner_instance_id() and settings.spanner_database_id():
            spanner_sources["graphs"] = xlsx_loader.list_graphs()
        else:
            spanner_sources["graphs"] = []
            spanner_sources["error"] = (
                "SPANNER_INSTANCE_ID and SPANNER_DATABASE_ID are not set."
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Spanner list_sources failed")
        spanner_sources["error"] = str(exc)
    return {"bigquery": bigquery_sources, "spanner": spanner_sources}


async def get_schema(
    source: str,
    name: str,
    tool_context: ToolContext,
    dataset_id: str = "",
) -> dict:
    """Return the schema for a BigQuery table or a loaded Spanner graph.

    Args:
        source: "bigquery" or "spanner".
        name: BigQuery table id, or Spanner graph name from list_sources.
        dataset_id: BigQuery dataset. Defaults to BQ_DATASET_ID.

    Returns:
        Schema dict. Spanner schema includes inferred or declared nodes and edges.
    """
    source = source.strip().lower()
    if source == "bigquery":
        info = await _tool(_bq_toolset, "get_table_info")
        return await info.run_async(
            args={
                "project_id": settings.bq_project_id(),
                "dataset_id": dataset_id or settings.bq_dataset_id(),
                "table_id": name,
            },
            tool_context=tool_context,
        )
    if source == "spanner":
        schema = xlsx_loader.graph_schema(name)
        if schema is None:
            return {"status": "NOT_FOUND", "graph_name": name}
        return schema
    return {"status": "ERROR", "error_details": f"Unknown source {source!r}."}


async def query(source: str, sql: str, tool_context: ToolContext) -> dict:
    """Run a read-only query and store rows in query_result.

    Args:
        source: "bigquery" or "spanner".
        sql: GoogleSQL. Spanner graph reads start with GRAPH <graph_name>.

    Returns:
        The tool result. Rows are also written to session state query_result
        for the analytics agent. There is no separate result-copy callback.
    """
    source = source.strip().lower()
    statement = guard_sql(sql)
    if source == "bigquery":
        project = settings.bq_project_id()
        client = bigquery.Client(
            project=os.getenv("BQ_COMPUTE_PROJECT_ID") or project
        )
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        client.query(statement, job_config=job_config).result()
        execute = await _tool(_bq_toolset, "execute_sql")
        result = await execute.run_async(
            args={"project_id": project, "query": statement},
            tool_context=tool_context,
        )
    elif source == "spanner":
        execute = await _tool(_spanner_toolset, "execute_sql")
        result = await execute.run_async(
            args={
                "project_id": settings.spanner_project_id(),
                "instance_id": settings.spanner_instance_id(),
                "database_id": settings.spanner_database_id(),
                "query": statement,
            },
            tool_context=tool_context,
        )
    else:
        return {"status": "ERROR", "error_details": f"Unknown source {source!r}."}

    rows = result.get("rows") if isinstance(result, dict) else result
    _store(tool_context, source, rows)
    return result


async def load_xlsx(
    artifact_name: str,
    tool_context: ToolContext,
    node_sheets: list[dict] | None = None,
    edge_sheet: dict | None = None,
    edge_sheets: list[dict] | None = None,
) -> dict:
    """Load an uploaded .xlsx artifact into one Spanner Graph schema.

    This is the only write path. The workbook is not loaded into BigQuery.
    Sheet roles, id columns, and edges are inferred when no mapping is passed.
    Pass node_sheets and edge_sheets only to correct a previous inference.
    Reloading the same filename replaces that graph.

    Args:
        artifact_name: Session artifact filename of the uploaded workbook.
        node_sheets: Optional correction, [{"sheet": str, "id_column": str}].
            Omit id_column only when the sheet has no unique column.
        edge_sheet: Optional single correction with sheet, source_column,
            target_column, source_node, and target_node.
        edge_sheets: Optional list of those edge mappings. Overrides edge_sheet.

    Returns:
        graph_name, tables, row counts, mapping, and confidence.
    """
    part = await tool_context.load_artifact(artifact_name)
    if part is None or part.inline_data is None or not part.inline_data.data:
        return {
            "status": "ERROR",
            "error_details": (
                f"Artifact {artifact_name!r} was not found. Upload the .xlsx "
                "file in this session and pass its filename."
            ),
        }
    try:
        loaded = xlsx_loader.load_workbook(
            workbook_bytes=part.inline_data.data,
            workbook_name=artifact_name,
            node_sheets=node_sheets,
            edge_sheet=edge_sheet,
            edge_sheets=edge_sheets,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("load_xlsx failed")
        return {"status": "ERROR", "error_details": str(exc)}
    tool_context.state["loaded_graph"] = loaded
    return loaded


def guard_sql(sql: str) -> str:
    return xlsx_loader.guard_sql(sql)
