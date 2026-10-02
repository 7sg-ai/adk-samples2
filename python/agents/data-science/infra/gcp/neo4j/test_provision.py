import json
import subprocess
from pathlib import Path

import pytest

from infra.gcp.neo4j.provision import GcloudHelmAdapter, provision


class FakeNeo4jAdapter:
    def __init__(self, existing_password=None):
        self.existing_password = existing_password
        self.installed = None
        self.stored = None
        self.uninstalled = None
        self.deleted_secret = None
        self.deleted_volume = None

    def cluster_exists(self, project_id, zone, cluster_name):
        return True

    def install(self, plan):
        self.installed = plan

    def bolt_ready(self, plan):
        return True

    def get_password(self, project_id, secret_id):
        return self.existing_password

    def put_password(self, project_id, secret_id, password):
        self.stored = (project_id, secret_id, password)

    def uninstall(self, plan):
        self.uninstalled = plan

    def delete_data_volume(self, plan):
        self.deleted_volume = (plan["namespace"], f"data-{plan['release_name']}-0")

    def delete_password(self, project_id, secret_id):
        self.deleted_secret = secret_id


def _request(**overrides):
    payload = {
        "protocolVersion": "1",
        "desiredState": "present",
        "runId": "run-1",
        "projectId": "gen-lang-client-0373235205",
        "runtimeServiceAccount": "deploy@gen-lang-client-0373235205.iam.gserviceaccount.com",
        "zone": "us-central1-a",
        "clusterName": "data-science-neo4j",
        "namespace": "neo4j",
        "releaseName": "neo4j",
        "loadBalancerIp": "10.128.0.20",
        "storageGb": 20,
        "chartVersion": "5.26.1",
    }
    payload.update(overrides)
    return payload


def test_present_installs_internal_neo4j_and_hides_password():
    adapter = FakeNeo4jAdapter()
    payload = provision(_request(), adapter)
    assert payload["status"] == "succeeded"
    assert payload["environment"] == {
        "NEO4J_URI": "bolt://10.128.0.20:7687",
        "NEO4J_USER": "neo4j",
    }
    assert adapter.stored[1] == "neo4j-password"
    assert adapter.stored[2]
    rendered = str(payload)
    assert adapter.stored[2] not in rendered
    plan = adapter.installed
    assert plan["chart"] == "neo4j/neo4j"
    assert plan["chart_version"] == "5.26.1"
    assert plan["values"]["services"]["neo4j"]["spec"]["type"] == "LoadBalancer"
    assert plan["values"]["services"]["neo4j"]["spec"]["loadBalancerIP"] == "10.128.0.20"
    assert plan["values"]["services"]["neo4j"]["annotations"]["networking.gke.io/load-balancer-type"] == "Internal"
    assert plan["values"]["volumes"]["data"]["dynamic"]["requests"]["storage"] == "20Gi"
    assert adapter.stored[0] == "gen-lang-client-0373235205"
    assert plan["values"]["neo4j"]["edition"] == "community"
    assert "password" not in str(plan["values"])


def test_storage_gb_other_than_20_fails_without_install():
    adapter = FakeNeo4jAdapter()
    payload = provision(_request(storageGb=10), adapter)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "invalid_request"
    assert adapter.installed is None
    assert adapter.stored is None


def test_install_failure_does_not_leak_password():
    adapter = FakeNeo4jAdapter()

    def failing_install(plan):
        raise RuntimeError(
            f"helm failed: --set neo4j.password={plan['password']} bad release"
        )

    adapter.install = failing_install
    payload = provision(_request(), adapter)
    assert payload["status"] == "failed"
    assert adapter.stored[2]
    assert adapter.stored[2] not in str(payload)
    assert adapter.stored[2] not in json.dumps(payload)


def test_absent_uninstalls_release_and_keeps_cluster():
    adapter = FakeNeo4jAdapter()
    payload = provision(_request(desiredState="absent"), adapter)
    assert payload["status"] == "succeeded"
    assert payload["environment"] == {}
    assert adapter.uninstalled["release_name"] == "neo4j"
    assert adapter.uninstalled["namespace"] == "neo4j"
    assert adapter.deleted_secret == "neo4j-password"
    assert all(item["type"] != "container.googleapis.com/cluster" for item in payload["resources"])


def test_absent_deletes_disk_and_reports_forwarding_rule_without_cluster():
    adapter = FakeNeo4jAdapter()
    payload = provision(_request(desiredState="absent"), adapter)
    assert payload["status"] == "succeeded"
    assert adapter.deleted_volume == ("neo4j", "data-neo4j-0")
    resources = {(item["type"], item["id"]): item for item in payload["resources"]}
    assert resources[("gce_persistent_disk", "data-neo4j-0")]["action"] == "deleted"
    assert resources[("internal_forwarding_rule", "neo4j/neo4j")]["ownership"] == "run_owned"
    assert all(item["type"] != "container.googleapis.com/cluster" for item in payload["resources"])
    assert all(item["action"] == "deleted" and item["ownership"] == "run_owned" for item in payload["resources"])


def test_present_reuses_existing_password_and_adds_no_version():
    adapter = FakeNeo4jAdapter(existing_password="already-stored-pw")
    payload = provision(_request(), adapter)
    assert payload["status"] == "succeeded"
    assert adapter.stored is None
    assert adapter.installed["password"] == "already-stored-pw"
    assert "already-stored-pw" not in json.dumps(payload)


def test_present_generates_and_stores_password_when_secret_has_no_version():
    adapter = FakeNeo4jAdapter(existing_password=None)
    provision(_request(), adapter)
    assert adapter.stored[2]
    assert adapter.installed["password"] == adapter.stored[2]


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


class _Recorder:
    """Patch target for subprocess.run that records calls and scripts replies."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or (lambda args: _completed(args))

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        return self.replies(list(args))

    def commands(self):
        return [" ".join(call[0][1:]) for call in self.calls]


_PLAN = {
    "project_id": "proj",
    "zone": "us-central1-a",
    "cluster_name": "data-science-neo4j",
    "namespace": "neo4j",
    "release_name": "neo4j",
    "load_balancer_ip": "10.128.0.20",
    "chart": "neo4j/neo4j",
    "chart_version": "5.26.1",
    "values": {"neo4j": {"edition": "community"}},
    "password": "s3cret-pw",
}


def test_install_registers_helm_repo_before_upgrade(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    GcloudHelmAdapter().install(dict(_PLAN))

    commands = recorder.commands()
    add = next(i for i, c in enumerate(commands) if c.startswith("repo add neo4j "))
    update = next(i for i, c in enumerate(commands) if c == "repo update neo4j")
    upgrade = next(i for i, c in enumerate(commands) if c.startswith("upgrade --install"))
    assert add < update < upgrade
    assert "https://helm.neo4j.com/neo4j --force-update" in commands[add]

    # Helm state is per-run, not the shared ~/.config/helm.
    for call_args, kwargs in recorder.calls:
        if call_args[1] in {"repo", "upgrade"}:
            env = kwargs["env"]
            assert env["HELM_REPOSITORY_CONFIG"].endswith("repositories.yaml")
            assert "helm-cache" in env["HELM_REPOSITORY_CACHE"]
    configs = {
        kwargs["env"]["HELM_REPOSITORY_CONFIG"]
        for call_args, kwargs in recorder.calls
        if call_args[1] in {"repo", "upgrade"}
    }
    assert len(configs) == 1


def test_install_passes_password_only_via_set_not_values_file(monkeypatch):
    seen = {}

    def replies(args):
        if args[1] == "upgrade":
            values = Path(args[args.index("--values") + 1])
            seen["values"] = values.read_text(encoding="utf-8")
            seen["args"] = args
        return _completed(args)

    monkeypatch.setattr(subprocess, "run", _Recorder(replies))
    GcloudHelmAdapter().install(dict(_PLAN))
    assert "s3cret-pw" not in seen["values"]
    assert "neo4j.password=s3cret-pw" in seen["args"]


def test_absent_missing_cluster_still_deletes_secret(monkeypatch):
    def replies(args):
        if "get-credentials" in args:
            return _completed(
                args, 1, stderr="ERROR: ResponseError: code=404, message=Not found: cluster."
            )
        return _completed(args)

    recorder = _Recorder(replies)
    monkeypatch.setattr(subprocess, "run", recorder)
    payload = provision(_request(desiredState="absent"), GcloudHelmAdapter())

    assert payload["status"] == "succeeded"
    commands = recorder.commands()
    assert not any(c.startswith(("uninstall", "delete pvc")) for c in commands)
    assert any(c.startswith("secrets delete neo4j-password") for c in commands)


def test_absent_auth_failure_on_get_credentials_fails_run(monkeypatch):
    def replies(args):
        if "get-credentials" in args:
            return _completed(
                args, 1, stderr="ERROR: code=403, message=Permission denied on cluster"
            )
        return _completed(args)

    recorder = _Recorder(replies)
    monkeypatch.setattr(subprocess, "run", recorder)
    payload = provision(_request(desiredState="absent"), GcloudHelmAdapter())

    assert payload["status"] == "failed"
    assert not any(c.startswith("secrets delete") for c in recorder.commands())


def test_helm_release_not_found_is_already_gone(monkeypatch):
    stderr = "Error: uninstall: Release not loaded: neo4j: release: not found"
    monkeypatch.setattr(
        subprocess,
        "run",
        _Recorder(
            lambda args: _completed(args, 1, stderr=stderr)
            if args[1] == "uninstall"
            else _completed(args)
        ),
    )
    GcloudHelmAdapter().uninstall(dict(_PLAN))


def test_loose_not_found_is_not_treated_as_gone(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        _Recorder(
            lambda args: _completed(args, 1, stderr="credentials file not found")
            if args[1] in {"uninstall", "delete", "secrets", "container"}
            else _completed(args)
        ),
    )
    adapter = GcloudHelmAdapter()
    with pytest.raises(RuntimeError):
        adapter.uninstall(dict(_PLAN))
    with pytest.raises(RuntimeError):
        adapter.delete_data_volume(dict(_PLAN))
    with pytest.raises(RuntimeError):
        adapter.delete_password("proj", "neo4j-password")
    with pytest.raises(RuntimeError):
        adapter.cluster_exists("proj", "us-central1-a", "c")


def test_gcloud_not_found_and_pvc_notfound_are_already_gone(monkeypatch):
    def replies(args):
        if args[1:3] == ["secrets", "delete"]:
            return _completed(args, 1, stderr="NOT_FOUND: Secret [x] not found")
        if args[1:3] == ["delete", "pvc"]:
            return _completed(
                args,
                1,
                stderr='Error from server (NotFound): persistentvolumeclaims "data-neo4j-0" not found',
            )
        return _completed(args)

    monkeypatch.setattr(subprocess, "run", _Recorder(replies))
    adapter = GcloudHelmAdapter()
    adapter.delete_password("proj", "neo4j-password")
    adapter.delete_data_volume(dict(_PLAN))


def test_pvc_notfound_for_other_object_is_not_already_gone(monkeypatch):
    def replies(args):
        if args[1:3] == ["delete", "pvc"]:
            return _completed(
                args, 1, stderr='Error from server (NotFound): namespaces "other" not found'
            )
        return _completed(args)

    monkeypatch.setattr(subprocess, "run", _Recorder(replies))
    with pytest.raises(RuntimeError):
        GcloudHelmAdapter().delete_data_volume(dict(_PLAN))


def test_get_password_returns_none_without_versions_and_reuses_latest(monkeypatch):
    def replies(args):
        if args[1:3] == ["secrets", "versions"] and args[3] == "list":
            return _completed(args, stdout=listing["out"])
        if args[1:3] == ["secrets", "versions"] and args[3] == "access":
            return _completed(args, stdout="stored-pw\n")
        return _completed(args)

    listing = {"out": ""}
    monkeypatch.setattr(subprocess, "run", _Recorder(replies))
    adapter = GcloudHelmAdapter()
    assert adapter.get_password("proj", "neo4j-password") is None
    listing["out"] = "projects/1/secrets/neo4j-password/versions/1\n"
    assert adapter.get_password("proj", "neo4j-password") == "stored-pw"