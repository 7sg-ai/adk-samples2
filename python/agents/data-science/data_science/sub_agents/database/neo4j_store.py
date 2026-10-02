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

"""Read-only Cypher access to the Neo4j graph source."""

from __future__ import annotations

import os
import re
from typing import Any, Callable

from data_science.sub_agents.database import settings

_WRITE_KEYWORDS = frozenset(
    {"CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP"}
)


def _default_session():
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4j_uri(),
        auth=(settings.neo4j_user(), os.environ["NEO4J_PASSWORD"]),
    )
    return _DriverSession(driver)


class _DriverSession:
    """Session context that also closes the driver it was opened from."""

    def __init__(self, driver) -> None:
        self._driver = driver
        self._session = driver.session()

    def run(self, statement: str):
        return self._session.run(statement)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        try:
            self._session.close()
        finally:
            self._driver.close()
        return False


def run_read(
    statement: str, session_factory: Callable[[], Any] | None = None
) -> list[dict]:
    """Run one read-only Cypher statement and return its rows as dicts."""
    text = statement.strip()
    first = text.split(None, 1)[0].upper() if text else ""
    if first in _WRITE_KEYWORDS:
        raise ValueError("Only read-only Cypher statements are allowed.")
    factory = session_factory or _default_session
    with factory() as session:
        return [
            record if isinstance(record, dict) else record.data()
            for record in session.run(text)
        ]
