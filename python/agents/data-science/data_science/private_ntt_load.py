"""Load the Acme workbook into the in-cluster Postgres and Neo4j."""

from __future__ import annotations

import json
import os
from pathlib import Path

from data_science.sub_agents.database.cluster_store import load_workbook

DEFAULT_WORKBOOK = Path(__file__).resolve().parents[1] / "sample_data" / "Acme Corp Financial Model.xlsx"


def main() -> None:
    path = Path(os.environ.get("WORKBOOK_PATH", str(DEFAULT_WORKBOOK)))
    if not path.is_file():
        raise SystemExit(f"workbook_missing: {path}")
    print(json.dumps(load_workbook(path)), flush=True)


if __name__ == "__main__":
    main()
