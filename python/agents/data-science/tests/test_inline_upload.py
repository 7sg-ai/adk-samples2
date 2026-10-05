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

"""Uploads named inline-file must be saved before load_xlsx can see them."""

import pytest
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.genai import types

from data_science import agent as agent_mod
from data_science.sub_agents.database import tools as db_tools
from data_science.sub_agents.database import xlsx_loader


class _Session:
    id = "session-1"
    state: dict = {}


class _Invocation:
    def __init__(self, artifact_service):
        self.artifact_service = artifact_service
        self.app_name = "data_science"
        self.user_id = "user-1"
        self.session = _Session()
        self.invocation_id = "inv-1"


class _ToolContext:
    def __init__(self, artifact_service):
        self._artifact_service = artifact_service
        self.state = {}

    async def load_artifact(self, filename):
        return await self._artifact_service.load_artifact(
            app_name="data_science",
            user_id="user-1",
            session_id="session-1",
            filename=filename,
        )


def _upload(display_name: str) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(text="load this workbook"),
            types.Part(
                inline_data=types.Blob(
                    mime_type=(
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet"
                    ),
                    display_name=display_name,
                    data=b"workbook-bytes",
                )
            ),
        ],
    )


@pytest.mark.asyncio
async def test_inline_file_upload_is_visible_to_load_xlsx(monkeypatch):
    service = InMemoryArtifactService()
    invocation = _Invocation(service)
    message = _upload("inline-file")
    plugins = getattr(getattr(agent_mod, "app", None), "plugins", [])
    for plugin in plugins:
        rewritten = await plugin.on_user_message_callback(
            invocation_context=invocation,
            user_message=message,
        )
        if rewritten is not None:
            message = rewritten

    monkeypatch.setattr(
        xlsx_loader,
        "load_workbook",
        lambda **kwargs: {"graph_name": "g_test", "workbook_name": kwargs["workbook_name"]},
    )
    loaded = await db_tools.load_xlsx(_ToolContext(service), "inline-file")

    assert loaded.get("status") != "ERROR"
    assert loaded["workbook_name"] == "inline-file"
    assert "inline-file" in message.parts[1].text
