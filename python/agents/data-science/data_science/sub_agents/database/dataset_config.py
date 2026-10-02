"""Which dataset types the current process was configured to use."""

from __future__ import annotations

import json
import os
from pathlib import Path

_CLOUD_TYPES = {"bigquery", "spanner"}


def configured_types() -> set[str]:
    path = os.getenv("DATASET_CONFIG_FILE", "")
    if not path:
        return set(_CLOUD_TYPES)
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(_CLOUD_TYPES)
    found = {
        str(item.get("type") or "")
        for item in payload.get("datasets") or []
        if isinstance(item, dict) and item.get("type")
    }
    return found or set(_CLOUD_TYPES)


def uses_cluster_stores() -> bool:
    """True only when this process was pointed at the in-cluster dataset file."""

    types = configured_types()
    return "postgres" in types or "neo4j" in types
