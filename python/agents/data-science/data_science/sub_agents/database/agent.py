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

"""Database agent: one tool chain for BigQuery and Spanner Graph."""

import os

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from .prompts import return_instructions_database
from .tools import get_schema, list_sources, load_xlsx, query

database_agent = Agent(
    model=os.getenv("DATABASE_AGENT_MODEL", os.getenv("BIGQUERY_AGENT_MODEL", "")),
    name="database_agent",
    instruction=return_instructions_database(),
    tools=[
        FunctionTool(list_sources),
        FunctionTool(get_schema),
        FunctionTool(query),
        FunctionTool(load_xlsx),
    ],
)
