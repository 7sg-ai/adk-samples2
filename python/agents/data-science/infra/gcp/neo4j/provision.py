import json
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

PROTOCOL_VERSION = "1"
SECRET_ID = "neo4j-password"
CHART = "neo4j/neo4j"
BOLT_PORT = 7687
NEO4J_USER = "neo4j"


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

    def put_password(
        self, project_id: str, secret_id: str, password: str
    ) -> None: ...

    def uninstall(self, plan: dict[str, Any]) -> None: ...


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class GcloudHelmAdapter:
    """Live adapter driving gcloud, kubectl and helm. Implemented in Task 4."""

    def cluster_exists(self, project_id: str, zone: str, cluster_name: str) -> bool:
        raise NotImplementedError

    def install(self, plan: dict[str, Any]) -> None:
        raise NotImplementedError

    def bolt_ready(self, plan: dict[str, Any]) -> bool:
        raise NotImplementedError

    def put_password(self, project_id: str, secret_id: str, password: str) -> None:
        raise NotImplementedError

    def uninstall(self, plan: dict[str, Any]) -> None:
        raise NotImplementedError


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
    if storage_gb < 1:
        raise ValueError("storageGb must be positive")

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
    try:
        parsed = parse_request(request)
        protocol_version = parsed.protocol_version
        if parsed.desired_state != "present":
            raise ProvisioningError(
                "unsupported_desired_state",
                "only desiredState present is supported",
            )

        if not adapter.cluster_exists(
            parsed.project_id, parsed.zone, parsed.cluster_name
        ):
            raise ProvisioningError(
                "cluster_missing",
                f"GKE cluster {parsed.cluster_name} was not found",
            )

        password = secrets.token_urlsafe(24)
        plan: dict[str, Any] = {
            "project_id": parsed.project_id,
            "zone": parsed.zone,
            "cluster_name": parsed.cluster_name,
            "namespace": parsed.namespace,
            "release_name": parsed.release_name,
            "chart": CHART,
            "chart_version": parsed.chart_version,
            "values": build_values(parsed),
            "password": password,
        }
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
        return _failure_result(protocol_version, exc)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = provision(payload, GcloudHelmAdapter())
    except Exception as exc:
        result = _failure_result(PROTOCOL_VERSION, exc)

    sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["status"] == "succeeded" else 1


def _failure_result(protocol_version: str, exc: Exception) -> dict[str, object]:
    code = "provider_error"
    if isinstance(exc, ProvisioningError):
        code = exc.code
        message = exc.message
    else:
        message = str(exc) or "provider operation failed"
        if isinstance(exc, (ValueError, json.JSONDecodeError, TypeError)):
            code = "invalid_request"
    return {
        "protocolVersion": protocol_version,
        "status": "failed",
        "resources": [],
        "environment": {},
        "error": {"code": code, "message": message},
    }


def _resource(resource_type: str, resource_id: str) -> dict[str, str]:
    return {
        "type": resource_type,
        "id": resource_id,
        "action": "created",
        "ownership": "run_owned",
    }


def _require_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


if __name__ == "__main__":
    raise SystemExit(main())
