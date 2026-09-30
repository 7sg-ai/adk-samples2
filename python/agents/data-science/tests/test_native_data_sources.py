import json
from pathlib import Path

import yaml

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]


def load_wdf():
    return yaml.safe_load((REPO_ROOT / "data-science.wdf.yaml").read_text())


def scenario_definition(wdf, scenario_name):
    if scenario_name == "native":
        return wdf["scenarios"]["native"]
    return next(
        item
        for item in wdf["scenarios"].get("alternates", [])
        if item["name"] == scenario_name
    )


def scenario_component(wdf, scenario_name):
    return scenario_definition(wdf, scenario_name)["topology"]["components"][0]


def runtime_env(component):
    return {item["name"]: item["value"] for item in component["runtime"]["env"]}


def test_dataset_config_matches_supported_bigquery_spanner_runtime():
    config = json.loads((APP_ROOT / "flights_dataset_config.json").read_text())

    assert [item["type"] for item in config["datasets"]] == ["bigquery", "spanner"]


def test_native_scenario_preserves_existing_spanner_coordinates():
    wdf = load_wdf()
    env = runtime_env(scenario_component(wdf, "native"))

    assert wdf["metadata"]["version"] == "0.3.0"
    assert {
        "bigquery-flights",
        "spanner-graph-gcp",
    }.issubset({item["dsdfRef"] for item in wdf["dataSources"]["inputs"]})
    assert wdf["scenarios"]["native"]["deployments"][0]["dataSources"] == [
        "bigquery-flights",
        "spanner-graph-gcp",
    ]
    assert env["BQ_DATASET_ID"] == "cymbal_flights_dataset"
    assert env["SPANNER_PROJECT_ID"] == "gen-lang-client-0373235205"
    assert env["SPANNER_INSTANCE_ID"] == "data-science"
    assert env["SPANNER_DATABASE_ID"] == "workbook_graph"
    assert env["DATASET_CONFIG_FILE"] == "/app/flights_dataset_config.json"


def test_native_managed_uses_same_cloud_run_shape_without_hard_coded_spanner_coordinates():
    wdf = load_wdf()
    native_component = scenario_component(wdf, "native")
    managed_component = scenario_component(wdf, "native-managed")
    native_env = runtime_env(native_component)
    managed_env = runtime_env(managed_component)

    assert native_component["name"] == managed_component["name"] == "data-science"
    assert native_component["role"] == managed_component["role"] == "api"
    assert native_component["runtime"]["platform"] == managed_component["runtime"]["platform"] == "serverless"
    assert native_component["runtime"]["regions"] == managed_component["runtime"]["regions"] == [
        "us-central1"
    ]
    assert native_component["runtime"]["image"] == managed_component["runtime"]["image"]
    assert native_component["runtime"]["resources"] == managed_component["runtime"]["resources"]
    assert native_component["runtime"]["autoscaling"] == managed_component["runtime"]["autoscaling"]
    assert native_component["endpoints"] == managed_component["endpoints"]
    assert managed_component["runtime"]["envFromSecretRefs"] == native_component["runtime"]["envFromSecretRefs"]

    assert managed_env["GOOGLE_GENAI_USE_VERTEXAI"] == native_env["GOOGLE_GENAI_USE_VERTEXAI"] == "true"
    assert managed_env["ROOT_AGENT_MODEL"] == native_env["ROOT_AGENT_MODEL"] == "gemini-2.5-flash"
    assert managed_env["ANALYTICS_AGENT_MODEL"] == native_env["ANALYTICS_AGENT_MODEL"] == "gemini-2.5-flash"
    assert managed_env["DATABASE_AGENT_MODEL"] == native_env["DATABASE_AGENT_MODEL"] == "gemini-2.5-flash"
    assert managed_env["SERVE_WEB_INTERFACE"] == native_env["SERVE_WEB_INTERFACE"] == "true"
    assert managed_env["BQ_DATASET_ID"] == native_env["BQ_DATASET_ID"] == "cymbal_flights_dataset"
    assert managed_env["DATASET_CONFIG_FILE"] == native_env["DATASET_CONFIG_FILE"] == "/app/flights_dataset_config.json"
    assert "SPANNER_PROJECT_ID" not in managed_env
    assert "SPANNER_INSTANCE_ID" not in managed_env
    assert "SPANNER_DATABASE_ID" not in managed_env


def test_native_managed_selects_managed_spanner_and_existing_bigquery_without_spanner_secrets():
    wdf = load_wdf()
    managed = scenario_definition(wdf, "native-managed")
    deployment = managed["deployments"][0]

    assert deployment["target"] == {
        "provider": "gcp",
        "region": "us-central1",
        "tdfRef": "gcp-native",
    }
    assert deployment["components"] == ["data-science"]
    assert deployment["dataSources"] == [
        {
            "name": "bigquery-flights",
            "dsdfRef": "bigquery-flights",
            "managementPolicy": "existing",
        },
        {
            "name": "spanner-graph-gcp",
            "dsdfRef": "spanner-graph-gcp",
            "managementPolicy": "managed",
        },
    ]
    data_source_refs = {
        item["name"]: item["dsdfRef"] for item in wdf["dataSources"]["inputs"]
    }
    assert data_source_refs["bigquery-flights"] == "bigquery-flights"
    assert data_source_refs["spanner-graph"] == "spanner-graph-gcp"

    secret_names = {item["name"] for item in wdf["secrets"]["secretRefs"]}
    injected_env_vars = {item.get("envVar") for item in wdf["secrets"]["secretRefs"]}
    assert all("spanner" not in name.lower() for name in secret_names)
    assert "SPANNER_PASSWORD" not in injected_env_vars
    assert "SPANNER_PROJECT_ID" not in injected_env_vars
    assert "SPANNER_INSTANCE_ID" not in injected_env_vars
    assert "SPANNER_DATABASE_ID" not in injected_env_vars


def test_managed_spanner_provisioner_script_exists_in_workload_source():
    assert (APP_ROOT / "infra" / "gcp" / "spanner" / "provision.py").is_file()
