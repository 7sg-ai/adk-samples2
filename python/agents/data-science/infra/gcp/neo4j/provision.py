import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

PROTOCOL_VERSION = "1"
SECRET_ID = "neo4j-password"
HELM_REPO_NAME = "neo4j"
HELM_REPO_URL = "https://helm.neo4j.com/neo4j"
CHART = "neo4j/neo4j"
BOLT_PORT = 7687
NEO4J_USER = "neo4j"
STORAGE_GB = 20


@dataclass(frozen=True)
class ProvisionRequest:
    protocol_version: str
    desired_state: str
    run_id: str
    project_id: str
    runtime_service_account: str
    zone: str
    cluster_name: str
    namespace: str
    release_name: str
    load_balancer_ip: str
    storage_gb: int
    chart_version: str


class Neo4jAdapter(Protocol):
    def cluster_exists(
        self, project_id: str, zone: str, cluster_name: str
    ) -> bool: ...

    def install(self, plan: dict[str, Any]) -> None: ...

    def bolt_ready(self, plan: dict[str, Any]) -> bool: ...

    def get_password(self, project_id: str, secret_id: str) -> str | None: ...

    def put_password(
        self, project_id: str, secret_id: str, password: str
    ) -> None: ...

    def uninstall(self, plan: dict[str, Any]) -> None: ...

    def delete_data_volume(self, plan: dict[str, Any]) -> None: ...

    def delete_password(self, project_id: str, secret_id: str) -> None: ...


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


COMMAND_TIMEOUT_SECONDS = 900
BOLT_READY_TIMEOUT_SECONDS = 600
BOLT_READY_POLL_SECONDS = 10


class GcloudHelmAdapter:
    """Live adapter driving gcloud, kubectl and helm.

    Command failures are raised as RuntimeError whose text contains only the
    command's captured output, redacted with the plan password. Command lines
    are never put in exception text, so a ``--set neo4j.password=...`` argument
    cannot leak through a failure payload.
    """

    def cluster_exists(self, project_id: str, zone: str, cluster_name: str) -> bool:
        result = self._run(
            [
                "gcloud", "container", "clusters", "describe", cluster_name,
                "--zone", zone, "--project", project_id, "--format", "value(name)",
            ],
            check=False,
        )
        if result.returncode == 0:
            return True
        if _is_gcloud_not_found(result):
            return False
        raise RuntimeError(_command_error("gcloud clusters describe", result))

    def install(self, plan: dict[str, Any]) -> None:
        password = plan["password"]
        with self._kubeconfig(plan) as kubeconfig:
            with tempfile.TemporaryDirectory() as tmp:
                values_path = Path(tmp) / "values.yaml"
                # JSON is a subset of YAML, so helm reads this file directly.
                values_path.write_text(json.dumps(plan["values"]), encoding="utf-8")
                # Per-run Helm state so runs never share ~/.config/helm.
                helm_env = {
                    "HELM_REPOSITORY_CONFIG": str(Path(tmp) / "repositories.yaml"),
                    "HELM_REPOSITORY_CACHE": str(Path(tmp) / "helm-cache"),
                }
                (Path(tmp) / "helm-cache").mkdir()
                self._run(
                    ["helm", "repo", "add", HELM_REPO_NAME, HELM_REPO_URL,
                     "--force-update"],
                    env=helm_env,
                    label="helm repo add",
                )
                self._run(
                    ["helm", "repo", "update", HELM_REPO_NAME],
                    env=helm_env,
                    label="helm repo update",
                )
                self._run(
                    [
                        "helm", "upgrade", "--install", plan["release_name"],
                        plan["chart"], "--version", plan["chart_version"],
                        "--namespace", plan["namespace"], "--create-namespace",
                        "--values", str(values_path),
                        "--set", f"neo4j.password={password}",
                        "--kubeconfig", kubeconfig,
                    ],
                    secret=password,
                    label="helm upgrade",
                    env=helm_env,
                )

    def bolt_ready(self, plan: dict[str, Any]) -> bool:
        deadline = time.monotonic() + BOLT_READY_TIMEOUT_SECONDS
        with self._kubeconfig(plan) as kubeconfig:
            while True:
                result = self._run(
                    [
                        "kubectl", "get", "svc", "-n", plan["namespace"],
                        "-o", "json", "--kubeconfig", kubeconfig,
                    ],
                    check=False,
                )
                if result.returncode == 0 and _has_ingress_ip(
                    result.stdout, plan["load_balancer_ip"]
                ):
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(BOLT_READY_POLL_SECONDS)

    def get_password(self, project_id: str, secret_id: str) -> str | None:
        describe = self._run(
            ["gcloud", "secrets", "describe", secret_id, "--project", project_id],
            check=False,
        )
        if describe.returncode != 0:
            if _is_gcloud_not_found(describe):
                return None
            raise RuntimeError(_command_error("gcloud secrets describe", describe))
        versions = self._run(
            [
                "gcloud", "secrets", "versions", "list", secret_id,
                "--project", project_id, "--filter", "state=ENABLED",
                "--limit", "1", "--format", "value(name)",
            ],
            label="gcloud secrets versions list",
        )
        if not versions.stdout.strip():
            return None
        access = self._run(
            [
                "gcloud", "secrets", "versions", "access", "latest",
                "--secret", secret_id, "--project", project_id,
            ],
            label="gcloud secrets versions access",
        )
        return access.stdout.rstrip("\r\n") or None

    def put_password(self, project_id: str, secret_id: str, password: str) -> None:
        describe = self._run(
            ["gcloud", "secrets", "describe", secret_id, "--project", project_id],
            check=False,
        )
        if describe.returncode != 0:
            if not _is_gcloud_not_found(describe):
                raise RuntimeError(_command_error("gcloud secrets describe", describe))
            self._run(
                [
                    "gcloud", "secrets", "create", secret_id,
                    "--project", project_id, "--replication-policy", "automatic",
                ],
                secret=password,
                label="gcloud secrets create",
            )
        # The value goes over stdin so it never appears on a command line.
        self._run(
            [
                "gcloud", "secrets", "versions", "add", secret_id,
                "--project", project_id, "--data-file", "-",
            ],
            secret=password,
            label="gcloud secrets versions add",
            stdin=password,
        )

    def uninstall(self, plan: dict[str, Any]) -> None:
        with self._kubeconfig(plan, allow_missing=True) as kubeconfig:
            if kubeconfig is None:
                return  # cluster already gone, so is the release
            result = self._run(
                [
                    "helm", "uninstall", plan["release_name"],
                    "--namespace", plan["namespace"], "--kubeconfig", kubeconfig,
                ],
                check=False,
            )
        if result.returncode == 0 or _is_helm_release_not_found(result):
            return
        raise RuntimeError(_command_error("helm uninstall", result))

    def delete_data_volume(self, plan: dict[str, Any]) -> None:
        # helm uninstall leaves the StatefulSet PVC (and its disk) behind.
        with self._kubeconfig(plan, allow_missing=True) as kubeconfig:
            if kubeconfig is None:
                return  # cluster already gone, so is the volume
            result = self._run(
                [
                    "kubectl", "delete", "pvc", f"data-{plan['release_name']}-0",
                    "--namespace", plan["namespace"],
                    "--ignore-not-found", "--kubeconfig", kubeconfig,
                ],
                check=False,
            )
        if result.returncode == 0 or _is_pvc_not_found(
            result, f"data-{plan['release_name']}-0"
        ):
            return
        raise RuntimeError(_command_error("kubectl delete pvc", result))

    def delete_password(self, project_id: str, secret_id: str) -> None:
        result = self._run(
            [
                "gcloud", "secrets", "delete", secret_id,
                "--project", project_id, "--quiet",
            ],
            check=False,
        )
        if result.returncode == 0 or _is_gcloud_not_found(result):
            return
        raise RuntimeError(_command_error("gcloud secrets delete", result))

    @contextmanager
    def _kubeconfig(
        self, plan: Mapping[str, Any], *, allow_missing: bool = False
    ) -> Iterator[str | None]:
        """Yield a temp kubeconfig, or None when allow_missing and the cluster is gone.

        Auth and permission failures always raise.
        """
        with tempfile.TemporaryDirectory() as tmp:
            kubeconfig = str(Path(tmp) / "kubeconfig")
            Path(kubeconfig).touch()
            result = self._run(
                [
                    "gcloud", "container", "clusters", "get-credentials",
                    plan["cluster_name"], "--zone", plan["zone"],
                    "--project", plan["project_id"],
                ],
                env={"KUBECONFIG": kubeconfig},
                check=False,
            )
            if result.returncode != 0:
                if allow_missing and _is_gcloud_not_found(result):
                    yield None
                    return
                raise RuntimeError(_command_error("gcloud get-credentials", result))
            yield kubeconfig

    def _run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        secret: str | None = None,
        label: str | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        executable = shutil.which(args[0]) or args[0]
        result = subprocess.run(
            [executable, *args[1:]],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env={**os.environ, **(env or {})},
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                sanitize_error_message(
                    _command_error(label or args[0], result), secret
                )
            )
        return result


def _command_error(label: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    return f"{label} failed (exit {result.returncode}): {detail}"


def _output_text(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stderr or ''}\n{result.stdout or ''}"


def _is_gcloud_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    # gcloud reports a missing resource as NOT_FOUND or an HTTP 404 response.
    return bool(re.search(r"NOT_FOUND|\b404\b", _output_text(result)))


def _is_helm_release_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    return "release: not found" in _output_text(result).lower()


def _is_pvc_not_found(result: subprocess.CompletedProcess[str], pvc_name: str) -> bool:
    text = _output_text(result)
    return "(NotFound)" in text and f'"{pvc_name}"' in text


def _has_ingress_ip(services_json: str, load_balancer_ip: str) -> bool:
    try:
        items = json.loads(services_json).get("items", [])
    except (json.JSONDecodeError, AttributeError):
        return False
    for item in items:
        ingress = (item.get("status", {}).get("loadBalancer", {}) or {}).get(
            "ingress"
        ) or []
        if any(entry.get("ip") == load_balancer_ip for entry in ingress):
            return True
    return False


def parse_request(payload: Mapping[str, object]) -> ProvisionRequest:
    protocol_version = _require_string(payload, "protocolVersion")
    if protocol_version != PROTOCOL_VERSION:
        raise ValueError("protocolVersion must be '1'")

    desired_state = _require_string(payload, "desiredState")
    if desired_state not in {"present", "absent"}:
        raise ValueError("desiredState must be present or absent")

    storage_gb = payload.get("storageGb")
    if isinstance(storage_gb, bool) or not isinstance(storage_gb, int):
        raise ValueError("storageGb must be an integer")
    if storage_gb != STORAGE_GB:
        raise ValueError(f"storageGb must be {STORAGE_GB}")

    return ProvisionRequest(
        protocol_version=protocol_version,
        desired_state=desired_state,
        run_id=_require_string(payload, "runId"),
        project_id=_require_string(payload, "projectId"),
        runtime_service_account=_require_string(payload, "runtimeServiceAccount"),
        zone=_require_string(payload, "zone"),
        cluster_name=_require_string(payload, "clusterName"),
        namespace=_require_string(payload, "namespace"),
        release_name=_require_string(payload, "releaseName"),
        load_balancer_ip=_require_string(payload, "loadBalancerIp"),
        storage_gb=storage_gb,
        chart_version=_require_string(payload, "chartVersion"),
    )


def build_values(request: ProvisionRequest) -> dict[str, Any]:
    return {
        "neo4j": {"name": "data-science", "edition": "community"},
        "volumes": {
            "data": {
                "mode": "dynamic",
                "dynamic": {
                    "storageClassName": "standard-rwo",
                    "requests": {"storage": f"{request.storage_gb}Gi"},
                },
            }
        },
        "services": {
            "neo4j": {
                "enabled": True,
                "spec": {
                    "type": "LoadBalancer",
                    "loadBalancerIP": request.load_balancer_ip,
                },
                "annotations": {
                    "networking.gke.io/load-balancer-type": "Internal"
                },
            }
        },
    }


def provision(request: Mapping[str, object], adapter: Neo4jAdapter) -> dict[str, object]:
    protocol_version = PROTOCOL_VERSION
    password: str | None = None
    try:
        parsed = parse_request(request)
        protocol_version = parsed.protocol_version
        if parsed.desired_state == "absent":
            # The GKE cluster is shared and one-time; absent only removes the
            # Helm release and its password secret.
            teardown_plan = {
                "project_id": parsed.project_id,
                "zone": parsed.zone,
                "cluster_name": parsed.cluster_name,
                "namespace": parsed.namespace,
                "release_name": parsed.release_name,
            }
            # Uninstall deletes the LoadBalancer Service, which removes the
            # internal forwarding rule. The PVC (and its disk) must be deleted
            # explicitly afterwards.
            adapter.uninstall(teardown_plan)
            adapter.delete_data_volume(teardown_plan)
            adapter.delete_password(parsed.project_id, SECRET_ID)
            return {
                "protocolVersion": parsed.protocol_version,
                "status": "succeeded",
                "resources": [
                    _resource(
                        "neo4j_helm_release",
                        f"{parsed.namespace}/{parsed.release_name}",
                        "deleted",
                    ),
                    _resource(
                        "gce_persistent_disk",
                        f"data-{parsed.release_name}-0",
                        "deleted",
                    ),
                    _resource(
                        "internal_forwarding_rule",
                        f"{parsed.namespace}/{parsed.release_name}",
                        "deleted",
                    ),
                    _resource("secretmanager_secret", SECRET_ID, "deleted"),
                ],
                "environment": {},
            }

        if not adapter.cluster_exists(
            parsed.project_id, parsed.zone, parsed.cluster_name
        ):
            raise ProvisioningError(
                "cluster_missing",
                f"GKE cluster {parsed.cluster_name} was not found",
            )

        # Reuse the stored Bolt password once it exists: the data volume keeps
        # the first password, so rotating on a retry would lock the app out.
        password = adapter.get_password(parsed.project_id, SECRET_ID)
        stored_already = password is not None
        if password is None:
            password = secrets.token_urlsafe(24)
        plan: dict[str, Any] = {
            "project_id": parsed.project_id,
            "zone": parsed.zone,
            "cluster_name": parsed.cluster_name,
            "namespace": parsed.namespace,
            "release_name": parsed.release_name,
            "load_balancer_ip": parsed.load_balancer_ip,
            "chart": CHART,
            "chart_version": parsed.chart_version,
            "values": build_values(parsed),
            "password": password,
        }
        if not stored_already:
            adapter.put_password(parsed.project_id, SECRET_ID, password)
        adapter.install(plan)

        if not adapter.bolt_ready(plan):
            raise ProvisioningError(
                "bolt_not_ready", "Neo4j Bolt endpoint did not become ready"
            )

        resources = [
            _resource(
                "neo4j_helm_release",
                f"{parsed.namespace}/{parsed.release_name}",
            ),
            _resource("secretmanager_secret", SECRET_ID),
        ]
        return {
            "protocolVersion": parsed.protocol_version,
            "status": "succeeded",
            "resources": resources,
            "environment": {
                "NEO4J_URI": f"bolt://{parsed.load_balancer_ip}:{BOLT_PORT}",
                "NEO4J_USER": NEO4J_USER,
            },
        }
    except Exception as exc:
        return _failure_result(protocol_version, exc, password)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = provision(payload, GcloudHelmAdapter())
    except Exception as exc:
        result = _failure_result(PROTOCOL_VERSION, exc)

    sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["status"] == "succeeded" else 1


def _failure_result(
    protocol_version: str, exc: Exception, password: str | None = None
) -> dict[str, object]:
    code = "provider_error"
    if isinstance(exc, ProvisioningError):
        code = exc.code
        message = sanitize_error_message(exc.message, password)
    else:
        message = sanitize_error_message(str(exc), password)
        if isinstance(exc, (ValueError, json.JSONDecodeError, TypeError)):
            code = "invalid_request"
    return {
        "protocolVersion": protocol_version,
        "status": "failed",
        "resources": [],
        "environment": {},
        "error": {
            "code": code,
            "message": message or "provider operation failed",
        },
    }


def sanitize_error_message(message: str, password: str | None = None) -> str:
    sanitized = message
    if password:
        sanitized = sanitized.replace(password, "[redacted]")
    redactions = [
        (r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [redacted]"),
        (r"(?i)(authorization\s*:\s*)(.+)", r"\1[redacted]"),
        (r"(?i)(access[_-]?token\s*=?\s*)([^,\s]+)", r"\1[redacted]"),
        (r"(?i)(password\s*[=:]?\s*)([^,\s]+)", r"\1[redacted]"),
    ]
    for pattern, replacement in redactions:
        sanitized = re.sub(pattern, replacement, sanitized)
    return sanitized.strip()


def _resource(
    resource_type: str, resource_id: str, action: str = "created"
) -> dict[str, str]:
    return {
        "type": resource_type,
        "id": resource_id,
        "action": action,
        "ownership": "run_owned",
    }


def _require_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


if __name__ == "__main__":
    raise SystemExit(main())
