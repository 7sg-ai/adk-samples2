import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "infra" / "gcp" / "bigquery" / "provision.py"
)
MODULE_NAME = "test_bigquery_provision_module"


def load_module():
    sys.modules.pop(MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def valid_payload():
    return {
        "protocolVersion": "1",
        "desiredState": "present",
        "runId": "run-123",
        "projectId": "demo-project",
        "location": "us-central1",
        "datasetId": "cymbal_flights_dataset",
        "seedTables": [
            {"table": "flight_history", "file": "flights_dataset/flight_history_table.csv"},
            {
                "table": "cymbalair_policies",
                "file": "flights_dataset/cymbalair_policies_table.csv",
                "allowQuotedNewlines": True,
            },
        ],
        "ownedResources": [],
    }


def test_script_entrypoint_emits_protocol_json_for_invalid_request():
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        input=json.dumps({"protocolVersion": "1"}),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "protocolVersion": "1",
        "status": "failed",
        "resources": [],
        "environment": {},
        "error": {
            "code": "invalid_request",
            "message": "desiredState must be a non-empty string",
        },
    }


def dataset_resource_id(payload):
    return f"projects/{payload['projectId']}/datasets/{payload['datasetId']}"


def table_resource_id(payload, table):
    return f"{dataset_resource_id(payload)}/tables/{table}"


class FakeAdapter:
    def __init__(
        self,
        *,
        gcloud_present=True,
        dataset_exists=False,
        existing_tables=None,
        fail_on=None,
        failure_message="provider failed",
    ):
        self.gcloud_present = gcloud_present
        self.dataset_exists = dataset_exists
        self.existing_tables = set(existing_tables or [])
        self.fail_on = fail_on
        self.failure_message = failure_message
        self.calls = []

    def _maybe_fail(self, operation):
        if self.fail_on == operation:
            raise RuntimeError(self.failure_message)

    def ensure_gcloud_available(self):
        self.calls.append(("ensure_gcloud_available",))
        if not self.gcloud_present:
            raise FileNotFoundError("gcloud")

    def enable_bigquery_api(self, project_id):
        self.calls.append(("enable_bigquery_api", project_id))
        self._maybe_fail("enable_bigquery_api")

    def dataset_exists_for(self, project_id, dataset_id):
        self.calls.append(("dataset_exists", project_id, dataset_id))
        self._maybe_fail("dataset_exists")
        return self.dataset_exists

    def create_dataset(self, project_id, location, dataset_id, run_id):
        self.calls.append(
            ("create_dataset", project_id, location, dataset_id, run_id)
        )
        self._maybe_fail("create_dataset")
        self.dataset_exists = True

    def table_exists_for(self, project_id, dataset_id, table):
        self.calls.append(("table_exists", project_id, dataset_id, table))
        self._maybe_fail("table_exists")
        return table in self.existing_tables

    def load_seed_table(
        self,
        project_id,
        location,
        dataset_id,
        table,
        csv_file,
        allow_quoted_newlines,
    ):
        self.calls.append(
            (
                "load_seed_table",
                project_id,
                location,
                dataset_id,
                table,
                csv_file,
                allow_quoted_newlines,
            )
        )
        self._maybe_fail("load_seed_table")
        self.existing_tables.add(table)

    def delete_dataset(self, project_id, dataset_id):
        self.calls.append(("delete_dataset", project_id, dataset_id))
        self._maybe_fail("delete_dataset")
        self.dataset_exists = False


def test_request_validation_rejects_invalid_payloads():
    module = load_module()
    payload = valid_payload()

    invalid_cases = [
        ("protocolVersion", "2", "protocolVersion"),
        ("desiredState", "unknown", "desiredState"),
        ("projectId", "", "projectId"),
        ("location", "BAD_LOCATION", "location"),
        ("datasetId", "bad id", "datasetId"),
        ("seedTables", "not-a-list", "seedTables"),
    ]
    for key, value, expected in invalid_cases:
        candidate = dict(payload)
        candidate[key] = value
        with pytest.raises(ValueError, match=expected):
            module.parse_request(candidate)

    missing_file = dict(payload)
    missing_file["seedTables"] = [{"table": "flight_history"}]
    with pytest.raises(ValueError, match="file must be a non-empty string"):
        module.parse_request(missing_file)

    bad_table = dict(payload)
    bad_table["seedTables"] = [
        {"table": "bad table", "file": "flights_dataset/flight_history_table.csv"}
    ]
    with pytest.raises(ValueError, match="seedTables"):
        module.parse_request(bad_table)

    traversal = dict(payload)
    traversal["seedTables"] = [
        {"table": "flight_history", "file": "../outside.csv"}
    ]
    with pytest.raises(ValueError, match="seedTables"):
        module.parse_request(traversal)

    absent_payload = dict(payload)
    absent_payload["desiredState"] = "absent"
    absent_payload["ownedResources"] = []
    with pytest.raises(ValueError, match="ownedResources"):
        module.parse_request(absent_payload)

    invalid_owned_cases = [
        ([dataset_resource_id(payload), dataset_resource_id(payload)], "duplicates"),
        (["projects/demo-project/datasets/other"], "ownedResources"),
        (["projects/other-project/datasets/cymbal_flights_dataset"], "ownedResources"),
        (["not-a-resource-id"], "ownedResources"),
    ]
    for owned_resources, expected in invalid_owned_cases:
        absent_payload = dict(payload)
        absent_payload["desiredState"] = "absent"
        absent_payload["ownedResources"] = owned_resources
        with pytest.raises(ValueError, match=expected):
            module.parse_request(absent_payload)


def test_missing_gcloud_fails_before_create():
    module = load_module()
    request = module.parse_request(valid_payload())
    adapter = FakeAdapter(gcloud_present=False)

    result = module.provision(request, adapter)

    assert result["status"] == "failed"
    assert result["resources"] == []
    assert "create_dataset" not in [call[0] for call in adapter.calls]


def test_present_creates_dataset_and_loads_seed_tables():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter()

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "bigquery_dataset",
            "id": dataset_resource_id(payload),
            "action": "created",
            "ownership": "run_owned",
        },
        {
            "type": "bigquery_table",
            "id": table_resource_id(payload, "flight_history"),
            "action": "created",
            "ownership": "run_owned",
        },
        {
            "type": "bigquery_table",
            "id": table_resource_id(payload, "cymbalair_policies"),
            "action": "created",
            "ownership": "run_owned",
        },
    ]
    assert result["environment"] == {"BQ_DATASET_ID": payload["datasetId"]}


def test_present_reuses_existing_dataset_and_skips_existing_tables():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter(
        dataset_exists=True,
        existing_tables={"flight_history"},
    )

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "bigquery_dataset",
            "id": dataset_resource_id(payload),
            "action": "reused",
            "ownership": "shared",
        },
        {
            "type": "bigquery_table",
            "id": table_resource_id(payload, "flight_history"),
            "action": "reused",
            "ownership": "shared",
        },
        {
            "type": "bigquery_table",
            "id": table_resource_id(payload, "cymbalair_policies"),
            "action": "created",
            "ownership": "run_owned",
        },
    ]
    loaded = [call for call in adapter.calls if call[0] == "load_seed_table"]
    assert [call[4] for call in loaded] == ["cymbalair_policies"]


def test_load_failure_returns_created_dataset_evidence():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter(fail_on="load_seed_table")

    result = module.provision(request, adapter)

    assert result["status"] == "failed"
    assert result["resources"] == [
        {
            "type": "bigquery_dataset",
            "id": dataset_resource_id(payload),
            "action": "created",
            "ownership": "run_owned",
        },
    ]


def test_seed_load_passes_allow_quoted_newlines_flag():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter()

    module.provision(request, adapter)

    loads = [call for call in adapter.calls if call[0] == "load_seed_table"]
    flags = {call[4]: call[6] for call in loads}
    assert flags["cymbalair_policies"] is True
    assert flags["flight_history"] is False


def test_absent_deletes_owned_dataset():
    module = load_module()
    payload = valid_payload()
    payload["desiredState"] = "absent"
    payload["ownedResources"] = [dataset_resource_id(payload)]
    request = module.parse_request(payload)
    adapter = FakeAdapter(dataset_exists=True)

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert [call[0] for call in adapter.calls if call[0].startswith("delete_")] == [
        "delete_dataset",
    ]
    assert result["resources"] == [
        {
            "type": "bigquery_dataset",
            "id": dataset_resource_id(payload),
            "action": "deleted",
            "ownership": "run_owned",
        },
    ]
    assert result["environment"] == {}


def test_absent_requires_dataset_to_be_owned():
    module = load_module()
    payload = valid_payload()
    payload["desiredState"] = "absent"
    # Validation only allows this request's own dataset ID in
    # ownedResources, so every valid absent request is run-owned.
    payload["ownedResources"] = [dataset_resource_id(payload)]
    request = module.parse_request(payload)
    assert request.owned_resources == (dataset_resource_id(payload),)


def test_absent_reports_already_absent_dataset_when_owned():
    module = load_module()
    payload = valid_payload()
    payload["desiredState"] = "absent"
    payload["ownedResources"] = [dataset_resource_id(payload)]
    request = module.parse_request(payload)
    adapter = FakeAdapter(dataset_exists=False)

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "bigquery_dataset",
            "id": dataset_resource_id(payload),
            "action": "already_absent",
            "ownership": "run_owned",
        },
    ]


def test_provider_exception_output_contains_no_credential_material():
    module = load_module()
    request = module.parse_request(valid_payload())
    adapter = FakeAdapter(
        fail_on="create_dataset",
        failure_message="Authorization: Bearer secret-token private_key=super-secret",
    )

    result = module.provision(request, adapter)

    message = result["error"]["message"]
    assert result["status"] == "failed"
    assert "secret-token" not in message
    assert "super-secret" not in message
    assert "Bearer" not in message


@pytest.mark.parametrize(
    ("adapter", "expected_exit"),
    [
        (FakeAdapter(), 0),
        (FakeAdapter(gcloud_present=False), 1),
    ],
)
def test_main_emits_one_json_document_and_correct_exit_code(
    monkeypatch, adapter, expected_exit
):
    module = load_module()
    monkeypatch.setattr(module, "GcloudBigqueryAdapter", lambda: adapter)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(valid_payload())))
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    exit_code = module.main()

    assert exit_code == expected_exit
    assert stdout.getvalue().count("\n") == 1
    assert json.loads(stdout.getvalue())["protocolVersion"] == "1"


def test_create_dataset_labels_include_run_id(monkeypatch):
    module = load_module()
    adapter = module.GcloudBigqueryAdapter()
    captured = {}

    def fake_run_bq(args):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(adapter, "_run_bq", fake_run_bq)

    adapter.create_dataset("demo-project", "us-central1", "cymbal_flights_dataset", "run-123")

    args = captured["args"]
    labels = [arg for arg in args if arg.startswith("sherpa-managed:") or arg.startswith("run-id:")]
    assert "sherpa-managed:true" in labels
    assert "run-id:run-123" in labels


def test_seed_file_missing_raises_provisioning_error():
    module = load_module()
    adapter = module.GcloudBigqueryAdapter()

    with pytest.raises(module.ProvisioningError, match="workload source"):
        adapter.load_seed_table(
            "demo-project",
            "us-central1",
            "cymbal_flights_dataset",
            "flight_history",
            "flights_dataset/does_not_exist.csv",
            False,
        )