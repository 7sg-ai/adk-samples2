import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "infra" / "gcp" / "spanner" / "provision.py"
)
MODULE_NAME = "test_spanner_provision_module"


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
        "region": "us-central1",
        "instanceId": "data-science",
        "databaseId": "workbook_graph",
        "processingUnits": 100,
        "runtimeServiceAccount": "ds-agent@demo-project.iam.gserviceaccount.com",
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


def instance_resource_id(payload):
    return f"projects/{payload['projectId']}/instances/{payload['instanceId']}"


def database_resource_id(payload):
    return (
        f"{instance_resource_id(payload)}/databases/{payload['databaseId']}"
    )


class FakeAdapter:
    def __init__(
        self,
        *,
        gcloud_present=True,
        instance_exists=False,
        database_exists=False,
        databases_in_instance=None,
        fail_on=None,
        failure_message="provider failed",
    ):
        self.gcloud_present = gcloud_present
        self.instance_exists = instance_exists
        self.database_exists = database_exists
        self.databases_in_instance = list(databases_in_instance or [])
        self.fail_on = fail_on
        self.failure_message = failure_message
        self.calls = []
        self.granted_roles = []

    def _maybe_fail(self, operation):
        if self.fail_on == operation:
            raise RuntimeError(self.failure_message)

    def ensure_gcloud_available(self):
        self.calls.append(("ensure_gcloud_available",))
        if not self.gcloud_present:
            raise FileNotFoundError("gcloud")

    def enable_spanner_api(self, project_id):
        self.calls.append(("enable_spanner_api", project_id))
        self._maybe_fail("enable_spanner_api")

    def instance_exists_for(self, project_id, instance_id):
        self.calls.append(("instance_exists", project_id, instance_id))
        self._maybe_fail("instance_exists")
        return self.instance_exists

    def create_instance(self, project_id, region, instance_id, processing_units, run_id):
        self.calls.append(
            (
                "create_instance",
                project_id,
                region,
                instance_id,
                processing_units,
                run_id,
            )
        )
        self._maybe_fail("create_instance")
        self.instance_exists = True

    def database_exists_for(self, project_id, instance_id, database_id):
        self.calls.append(("database_exists", project_id, instance_id, database_id))
        self._maybe_fail("database_exists")
        return self.database_exists

    def create_database(self, project_id, instance_id, database_id):
        self.calls.append(("create_database", project_id, instance_id, database_id))
        self._maybe_fail("create_database")
        self.database_exists = True
        self.databases_in_instance.append(database_resource_id(valid_payload()))

    def grant_database_role(
        self, project_id, instance_id, database_id, service_account, role
    ):
        self.calls.append(
            (
                "grant_database_role",
                project_id,
                instance_id,
                database_id,
                service_account,
                role,
            )
        )
        self._maybe_fail("grant_database_role")
        self.granted_roles.append(role)

    def list_databases(self, project_id, instance_id):
        self.calls.append(("list_databases", project_id, instance_id))
        self._maybe_fail("list_databases")
        return tuple(self.databases_in_instance)

    def delete_database(self, project_id, instance_id, database_id):
        self.calls.append(("delete_database", project_id, instance_id, database_id))
        self._maybe_fail("delete_database")
        target = database_resource_id(valid_payload())
        self.database_exists = False
        self.databases_in_instance = [
            resource for resource in self.databases_in_instance if resource != target
        ]

    def delete_instance(self, project_id, instance_id):
        self.calls.append(("delete_instance", project_id, instance_id))
        self._maybe_fail("delete_instance")
        self.instance_exists = False


def test_request_validation_rejects_invalid_payloads():
    module = load_module()
    payload = valid_payload()

    invalid_cases = [
        ("protocolVersion", "2", "protocolVersion"),
        ("desiredState", "unknown", "desiredState"),
        ("projectId", "", "projectId"),
        ("instanceId", "BAD_ID", "instanceId"),
        ("databaseId", "bad id", "databaseId"),
        ("processingUnits", 99, "processingUnits"),
        ("runtimeServiceAccount", "not-an-email", "runtimeServiceAccount"),
    ]

    for key, value, expected in invalid_cases:
        candidate = dict(payload)
        candidate[key] = value
        with pytest.raises(ValueError, match=expected):
            module.parse_request(candidate)

    absent_payload = dict(payload)
    absent_payload["desiredState"] = "absent"
    absent_payload["ownedResources"] = []
    with pytest.raises(ValueError, match="ownedResources"):
        module.parse_request(absent_payload)

    invalid_owned_resources_cases = [
        ([database_resource_id(payload), database_resource_id(payload)], "duplicates"),
        (["projects/demo-project/instances/data-science/databases/other"], "ownedResources"),
        (["projects/other-project/instances/data-science"], "ownedResources"),
        (["not-a-resource-id"], "ownedResources"),
    ]
    for owned_resources, expected in invalid_owned_resources_cases:
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
    assert "create_instance" not in [call[0] for call in adapter.calls]


def test_present_creates_instance_database_and_iam():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter()

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "spanner_instance",
            "id": instance_resource_id(payload),
            "action": "created",
            "ownership": "run_owned",
        },
        {
            "type": "spanner_database",
            "id": database_resource_id(payload),
            "action": "created",
            "ownership": "run_owned",
        },
    ]
    assert result["environment"] == {
        "SPANNER_PROJECT_ID": payload["projectId"],
        "SPANNER_INSTANCE_ID": payload["instanceId"],
        "SPANNER_DATABASE_ID": payload["databaseId"],
    }
    assert adapter.granted_roles == [
        "roles/spanner.databaseUser",
        "roles/spanner.databaseAdmin",
    ]


def test_present_reuses_both_with_shared_ownership():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter(
        instance_exists=True,
        database_exists=True,
        databases_in_instance=[database_resource_id(payload)],
    )

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "spanner_instance",
            "id": instance_resource_id(payload),
            "action": "reused",
            "ownership": "shared",
        },
        {
            "type": "spanner_database",
            "id": database_resource_id(payload),
            "action": "reused",
            "ownership": "shared",
        },
    ]


def test_present_reuses_instance_and_creates_database():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter(instance_exists=True, database_exists=False)

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "spanner_instance",
            "id": instance_resource_id(payload),
            "action": "reused",
            "ownership": "shared",
        },
        {
            "type": "spanner_database",
            "id": database_resource_id(payload),
            "action": "created",
            "ownership": "run_owned",
        },
    ]


def test_database_create_failure_returns_created_instance_evidence():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter(fail_on="create_database")

    result = module.provision(request, adapter)

    assert result["status"] == "failed"
    assert result["resources"] == [
        {
            "type": "spanner_instance",
            "id": instance_resource_id(payload),
            "action": "created",
            "ownership": "run_owned",
        }
    ]


def test_retry_after_partial_failure_reuses_instance_and_creates_database():
    module = load_module()
    payload = valid_payload()
    request = module.parse_request(payload)
    adapter = FakeAdapter(instance_exists=True, database_exists=False)

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"][0]["action"] == "reused"
    assert result["resources"][1]["action"] == "created"


def test_absent_deletes_owned_database_then_owned_instance():
    module = load_module()
    payload = valid_payload()
    payload["desiredState"] = "absent"
    payload["ownedResources"] = [
        database_resource_id(payload),
        instance_resource_id(payload),
    ]
    request = module.parse_request(payload)
    adapter = FakeAdapter(
        instance_exists=True,
        database_exists=True,
        databases_in_instance=[database_resource_id(payload)],
    )

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert [call[0] for call in adapter.calls if call[0].startswith("delete_")] == [
        "delete_database",
        "delete_instance",
    ]
    assert result["resources"] == [
        {
            "type": "spanner_database",
            "id": database_resource_id(payload),
            "action": "deleted",
            "ownership": "run_owned",
        },
        {
            "type": "spanner_instance",
            "id": instance_resource_id(payload),
            "action": "deleted",
            "ownership": "run_owned",
        },
    ]


def test_absent_skips_shared_database_and_preserves_non_owned_instance():
    module = load_module()
    payload = valid_payload()
    payload["desiredState"] = "absent"
    payload["ownedResources"] = [instance_resource_id(payload)]
    request = module.parse_request(payload)
    adapter = FakeAdapter(
        instance_exists=True,
        database_exists=True,
        databases_in_instance=[database_resource_id(payload)],
    )

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "spanner_database",
            "id": database_resource_id(payload),
            "action": "skipped_shared",
            "ownership": "shared",
        },
        {
            "type": "spanner_instance",
            "id": instance_resource_id(payload),
            "action": "skipped_shared",
            "ownership": "shared",
        },
    ]
    assert [call[0] for call in adapter.calls if call[0].startswith("delete_")] == []


def test_absent_reports_already_absent_database_when_owned():
    module = load_module()
    payload = valid_payload()
    payload["desiredState"] = "absent"
    payload["ownedResources"] = [database_resource_id(payload)]
    request = module.parse_request(payload)
    adapter = FakeAdapter(instance_exists=True, database_exists=False, databases_in_instance=[])

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "spanner_database",
            "id": database_resource_id(payload),
            "action": "already_absent",
            "ownership": "run_owned",
        },
        {
            "type": "spanner_instance",
            "id": instance_resource_id(payload),
            "action": "skipped_shared",
            "ownership": "shared",
        },
    ]


def test_absent_reports_already_absent_database_and_instance_when_owned():
    module = load_module()
    payload = valid_payload()
    payload["desiredState"] = "absent"
    payload["ownedResources"] = [
        database_resource_id(payload),
        instance_resource_id(payload),
    ]
    request = module.parse_request(payload)
    adapter = FakeAdapter(instance_exists=False, database_exists=False, databases_in_instance=[])

    result = module.provision(request, adapter)

    assert result["status"] == "succeeded"
    assert result["resources"] == [
        {
            "type": "spanner_database",
            "id": database_resource_id(payload),
            "action": "already_absent",
            "ownership": "run_owned",
        },
        {
            "type": "spanner_instance",
            "id": instance_resource_id(payload),
            "action": "already_absent",
            "ownership": "run_owned",
        },
    ]


def test_create_instance_display_name_within_gcp_limit(monkeypatch):
    module = load_module()
    adapter = module.GcloudSpannerAdapter()
    captured = {}

    def fake_run_gcloud(args):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(adapter, "_run_gcloud", fake_run_gcloud)

    adapter.create_instance(
        "demo-project", "us-central1", "data-science", 100, "run-123"
    )

    description = captured["args"][captured["args"].index("--description") + 1]
    assert 4 <= len(description) <= 30, (
        "Spanner display name must be 4-30 characters per GCP limits"
    )


def test_provider_exception_output_contains_no_credential_material():
    module = load_module()
    request = module.parse_request(valid_payload())
    adapter = FakeAdapter(
        fail_on="create_database",
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
    monkeypatch.setattr(module, "GcloudSpannerAdapter", lambda: adapter)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(valid_payload())))
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    exit_code = module.main()

    assert exit_code == expected_exit
    assert stdout.getvalue().count("\n") == 1
    assert json.loads(stdout.getvalue())["protocolVersion"] == "1"
