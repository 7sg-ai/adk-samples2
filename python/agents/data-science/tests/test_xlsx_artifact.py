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

"""Artifact lookup for uploaded workbooks. No live services."""

import asyncio
from types import SimpleNamespace

from data_science.sub_agents.database.xlsx_loader import resolve_xlsx_artifact


class _Part:
    def __init__(self, data: bytes):
        self.inline_data = SimpleNamespace(data=data, mime_type="application/octet-stream")


class _Artifacts:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.loaded: list[str] = []

    async def load_artifact(self, name: str):
        self.loaded.append(name)
        data = self.files.get(name)
        if data is None:
            return None
        return _Part(data)

    async def list_artifacts(self):
        return list(self.files)


def test_resolves_display_name_when_key_uses_underscores():
    tool_context = _Artifacts({"MLB_PbP_Sample_Analytics.xlsx": b"book"})

    name, part, error = asyncio.run(
        resolve_xlsx_artifact(tool_context, "MLB PbP Sample Analytics.xlsx")
    )

    assert error is None
    assert name == "MLB_PbP_Sample_Analytics.xlsx"
    assert part.inline_data.data == b"book"


def test_uses_the_only_workbook_when_the_model_passes_a_missing_name():
    tool_context = _Artifacts({"artifact_inv_0": b"not-xlsx", "report.xlsx": b"book"})

    name, part, error = asyncio.run(
        resolve_xlsx_artifact(tool_context, "missing.xlsx")
    )

    assert error is None
    assert name == "report.xlsx"
    assert part.inline_data.data == b"book"


def test_generated_key_is_used_only_when_no_name_was_given():
    tool_context = _Artifacts({"artifact_e-abc_0": b"book"})

    name, _, error = asyncio.run(resolve_xlsx_artifact(tool_context, ""))

    assert error is None
    assert name == "artifact_e-abc_0"


def test_a_guessed_name_does_not_load_an_unrelated_binary():
    tool_context = _Artifacts({"artifact_e-abc_0": b"book"})

    _, part, error = asyncio.run(
        resolve_xlsx_artifact(tool_context, "MLB PbP Sample Analytics.xlsx")
    )

    assert part is None
    assert "artifact_e-abc_0" in error
    assert "none is an .xlsx workbook" in error


def test_lists_real_names_instead_of_asking_for_a_reupload():
    tool_context = _Artifacts({"one.xlsx": b"a", "two.xlsx": b"b"})

    _, part, error = asyncio.run(resolve_xlsx_artifact(tool_context, "other.xlsx"))

    assert part is None
    assert "one.xlsx" in error
    assert "two.xlsx" in error
    assert "re-upload" not in error.casefold()
    assert "upload the" not in error.casefold()


def test_user_namespace_is_tried_when_the_plain_name_misses():
    tool_context = _Artifacts({"user:friends.xlsx": b"book"})

    name, part, error = asyncio.run(
        resolve_xlsx_artifact(tool_context, "friends.xlsx")
    )

    assert error is None
    assert name == "user:friends.xlsx"
    assert part.inline_data.data == b"book"

