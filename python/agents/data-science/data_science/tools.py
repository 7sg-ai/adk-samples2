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

"""Tools for the ADK Sampmles Data Science Agent."""

import logging

from google.adk.tools import ToolContext
from google.adk.tools.agent_tool import AgentTool

from .sub_agents import get_analytics_agent, get_database_agent

logger = logging.getLogger(__name__)


async def call_database_agent(
    question: str,
    tool_context: ToolContext,
):
    """Call the shared BigQuery and Spanner Graph database agent.

    Use this for listing sources, reading schema, running a read-only query,
    or loading an uploaded .xlsx workbook into a Spanner Graph schema.
    Natural-language-to-SQL and BQML are not available on this agent.
    """
    logger.debug("call_database_agent: %s", question)
    agent_tool = AgentTool(agent=get_database_agent())
    output = await agent_tool.run_async(
        args={"request": question}, tool_context=tool_context
    )
    tool_context.state["database_agent_output"] = output
    return output


async def call_analytics_agent(
    question: str,
    tool_context: ToolContext,
):
    """
    This tool can generate Python code to process and analyze a dataset.

    Some of the tasks it can do in Python include:
    * Creating graphics for data visualization;
    * Processing or filtering existing datasets;
    * Combining datasets to create a joined dataset for further analysis.

    The Python modules available to it are:
    * io
    * math
    * re
    * matplotlib.pyplot
    * numpy
    * pandas

    The tool DOES NOT have the ability to retrieve additional data from
    a database. Only the data already retrieved will be analyzed.

    Args:
        question (str): Natural language question or analytics request.
        tool_context (ToolContext): The tool context to use for generating the
            SQL query.

    Returns:
        Response from the analytics agent.

    """
    logger.debug("call_analytics_agent: %s", question)

    # if question == "N/A":
    #    return tool_context.state["db_agent_output"]

    query_data = tool_context.state.get("query_result", "")
    source = tool_context.state.get("query_result_source", "")

    question_with_data = f"""
  Question to answer: {question}

  Actual data to analyze this question is in query_result.
  Source: {source}

  <QUERY_RESULT>
  {query_data}
  </QUERY_RESULT>

  """

    agent_tool = AgentTool(agent=get_analytics_agent())

    analytics_agent_output = await agent_tool.run_async(
        args={"request": question_with_data}, tool_context=tool_context
    )
    tool_context.state["analytics_agent_output"] = analytics_agent_output
    return analytics_agent_output
