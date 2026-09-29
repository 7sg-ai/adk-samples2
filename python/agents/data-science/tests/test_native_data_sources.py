import json
from pathlib import Path

import yaml


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]


def test_dataset_config_matches_supported_bigquery_spanner_runtime():
    config = json.loads((APP_ROOT / "flights_dataset_config.json").read_text())

    assert [item["type"] for item in config["datasets"]] == ["bigquery", "spanner"]


def test_native_wdf_selects_bigquery_and_spanner_resources():
    wdf = yaml.safe_load((REPO_ROOT / "data-science.wdf.yaml").read_text())
    native = wdf["scenarios"]["native"]
    runtime = native["topology"]["components"][0]["runtime"]
    env = {item["name"]: item["value"] for item in runtime["env"]}

    assert wdf["metadata"]["version"] == "0.2.0"
    assert [item["dsdfRef"] for item in wdf["dataSources"]["inputs"]] == [
        "bigquery-flights",
        "spanner-graph-gcp",
    ]
    assert native["deployments"][0]["dataSources"] == [
        "bigquery-flights",
        "spanner-graph-gcp",
    ]
    assert env["BQ_DATASET_ID"] == "cymbal_flights_dataset"
    assert env["SPANNER_PROJECT_ID"] == "gen-lang-client-0373235205"
    assert env["SPANNER_INSTANCE_ID"] == "data-science"
    assert env["SPANNER_DATABASE_ID"] == "workbook_graph"
    assert env["DATASET_CONFIG_FILE"] == "/app/flights_dataset_config.json"
