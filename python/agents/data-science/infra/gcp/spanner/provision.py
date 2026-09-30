import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

PROTOCOL_VERSION = "1"
MIN_PROCESSING_UNITS = 100
DATABASE_ROLES = (
    "roles/spanner.databaseUser",
    # Workbook imports call update_ddl(), which requires databaseAdmin.
    "roles/spanner.databaseAdmin",
)

PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z]+(?:-[a-z0-9]+)+$")
INSTANCE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,29}$")
DATABASE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,79}$")
SERVICE_ACCOUNT_RE = re.compile(
    r"^[a-z][a-z0-9-]{2,29}@[a-z][a-z0-9-]{4,61}[a-z0-9]\.iam\.gserviceaccount\.com$"
)


@dataclass(frozen=True)
class ProvisionRequest:
    protocol_version: str
    desired_state: Literal["present", "absent"]
    run_id: str
    project_id: str
    region: str
    instance_id: str
    database_id: str
    processing_units: int
    runtime_service_account: str
    owned_resources: tuple[str, ...] = ()


class SpannerAdapter(Protocol):
    def ensure_gcloud_available(self) -> None: ...

    def enable_spanner_api(self, project_id: str) -> None: ...

    def instance_exists_for(self, project_id: str, instance_id: str) -> bool: ...

    def create_instance(
        self,
        project_id: str,
        region: str,
        instance_id: str,
        processing_units: int,
        run_id: str,
    ) -> None: ...

    def database_exists_for(
        self, project_id: str, instance_id: str, database_id: str
    ) -> bool: ...

    def create_database(
        self, project_id: str, instance_id: str, database_id: str
    ) -> None: ...

    def grant_database_role(
        self,
        project_id: str,
        instance_id: str,
        database_id: str,
        service_account: str,
        role: str,
    ) -> None: ...

    def list_databases(self, project_id: str, instance_id: str) -> tuple[str, ...]: ...

    def delete_database(
        self, project_id: str, instance_id: str, database_id: str
    ) -> None: ...

    def delete_instance(self, project_id: str, instance_id: str) -> None: ...


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class GcloudSpannerAdapter:
    def ensure_gcloud_available(self) -> None:
        if shutil.which("gcloud") is None:
            raise ProvisioningError(
                "missing_gcloud", "gcloud CLI is required but was not found"
            )

    def enable_spanner_api(self, project_id: str) -> None:
        self._run_gcloud(
            [
                "services",
                "enable",
                "spanner.googleapis.com",
                "--project",
                project_id,
                "--quiet",
            ]
        )

    def instance_exists_for(self, project_id: str, instance_id: str) -> bool:
        return self._describe_exists(
            ["spanner", "instances", "describe", instance_id, "--project", project_id]
        )

    def create_instance(
        self,
        project_id: str,
        region: str,
        instance_id: str,
        processing_units: int,
        run_id: str,
    ) -> None:
        self._run_gcloud(
            [
                "spanner",
                "instances",
                "create",
                instance_id,
                "--config",
                f"regional-{region}",
                "--description",
                "Sherpa-managed Data Science Spanner instance",
                "--processing-units",
                str(processing_units),
                "--labels",
                f"sherpa-managed=true,run-id={normalize_run_id(run_id)}",
                "--project",
                project_id,
                "--quiet",
            ]
        )

    def database_exists_for(
        self, project_id: str, instance_id: str, database_id: str
    ) -> bool:
        return self._describe_exists(
            [
                "spanner",
                "databases",
                "describe",
                database_id,
                "--instance",
                instance_id,
                "--project",
                project_id,
            ]
        )

    def create_database(
        self, project_id: str, instance_id: str, database_id: str
    ) -> None:
        self._run_gcloud(
            [
                "spanner",
                "databases",
                "create",
                database_id,
                "--instance",
                instance_id,
                "--project",
                project_id,
                "--quiet",
            ]
        )

    def grant_database_role(
        self,
        project_id: str,
        instance_id: str,
        database_id: str,
        service_account: str,
        role: str,
    ) -> None:
        self._run_gcloud(
            [
                "spanner",
                "databases",
                "add-iam-policy-binding",
                database_id,
                "--instance",
                instance_id,
                "--member",
                f"serviceAccount:{service_account}",
                "--role",
                role,
                "--project",
                project_id,
                "--quiet",
            ]
        )

    def list_databases(self, project_id: str, instance_id: str) -> tuple[str, ...]:
        result = self._run_gcloud(
            [
                "spanner",
                "databases",
                "list",
                "--instance",
                instance_id,
                "--project",
                project_id,
                "--format",
                "value(name)",
            ]
        )
        databases = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return tuple(databases)

    def delete_database(
        self, project_id: str, instance_id: str, database_id: str
    ) -> None:
        self._run_gcloud(
            [
                "spanner",
                "databases",
                "delete",
                database_id,
                "--instance",
                instance_id,
                "--project",
                project_id,
                "--quiet",
            ]
        )

    def delete_instance(self, project_id: str, instance_id: str) -> None:
        self._run_gcloud(
            [
                "spanner",
                "instances",
                "delete",
                instance_id,
                "--project",
                project_id,
                "--quiet",
            ]
        )

    def _describe_exists(self, args: list[str]) -> bool:
        try:
            self._run_gcloud(args)
        except ProvisioningError as exc:
            if exc.code == "not_found":
                return False
            raise
        return True

    def _run_gcloud(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["gcloud", *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result

        stderr = sanitize_error_message(result.stderr.strip())
        if "NOT_FOUND" in result.stderr or "was not found" in result.stderr:
            raise ProvisioningError("not_found", stderr or "resource not found")
        raise ProvisioningError(
            "gcloud_command_failed", stderr or "gcloud command failed"
        )


def parse_request(payload: Mapping[str, object]) -> ProvisionRequest:
    protocol_version = _require_string(payload, "protocolVersion")
    if protocol_version != PROTOCOL_VERSION:
        raise ValueError("protocolVersion must be '1'")

    desired_state = _require_string(payload, "desiredState")
    if desired_state not in {"present", "absent"}:
        raise ValueError("desiredState must be present or absent")

    run_id = _require_string(payload, "runId")
    project_id = _require_string(payload, "projectId")
    region = _require_string(payload, "region")
    instance_id = _require_string(payload, "instanceId")
    database_id = _require_string(payload, "databaseId")
    runtime_service_account = _require_string(payload, "runtimeServiceAccount")

    processing_units = payload.get("processingUnits")
    if not isinstance(processing_units, int):
        raise ValueError("processingUnits must be an integer")
    if processing_units < MIN_PROCESSING_UNITS:
        raise ValueError(
            f"processingUnits must be at least {MIN_PROCESSING_UNITS}"
        )

    owned_resources_raw = payload.get("ownedResources", [])
    if not isinstance(owned_resources_raw, list) or any(
        not isinstance(item, str) or not item for item in owned_resources_raw
    ):
        raise ValueError("ownedResources must be a list of non-empty strings")
    owned_resources = tuple(owned_resources_raw)

    _validate_identifier(project_id, PROJECT_ID_RE, "projectId")
    _validate_identifier(region, REGION_RE, "region")
    _validate_identifier(instance_id, INSTANCE_ID_RE, "instanceId")
    _validate_identifier(database_id, DATABASE_ID_RE, "databaseId")
    _validate_identifier(
        runtime_service_account, SERVICE_ACCOUNT_RE, "runtimeServiceAccount"
    )

    if desired_state == "absent":
        if not owned_resources:
            raise ValueError("ownedResources must include exact owned resource IDs")
        _validate_owned_resources(project_id, instance_id, database_id, owned_resources)

    return ProvisionRequest(
        protocol_version=protocol_version,
        desired_state=desired_state,
        run_id=run_id,
        project_id=project_id,
        region=region,
        instance_id=instance_id,
        database_id=database_id,
        processing_units=processing_units,
        runtime_service_account=runtime_service_account,
        owned_resources=owned_resources,
    )


def provision(request: ProvisionRequest, adapter: SpannerAdapter) -> dict[str, object]:
    resources: list[dict[str, str]] = []
    try:
        adapter.ensure_gcloud_available()
        if request.desired_state == "present":
            adapter.enable_spanner_api(request.project_id)
            _provision_present(request, adapter, resources)
            return _success_result(request, resources, _environment(request))

        _provision_absent(request, adapter, resources)
        return _success_result(request, resources, {})
    except Exception as exc:
        return _failure_result(request.protocol_version, resources, exc)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        request = parse_request(payload)
        result = provision(request, GcloudSpannerAdapter())
    except Exception as exc:
        result = _failure_result(PROTOCOL_VERSION, [], exc)

    sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["status"] == "succeeded" else 1


def _provision_present(
    request: ProvisionRequest,
    adapter: SpannerAdapter,
    resources: list[dict[str, str]],
) -> None:
    instance_id = _instance_resource_id(request)
    database_id = _database_resource_id(request)

    if adapter.instance_exists_for(request.project_id, request.instance_id):
        resources.append(_resource("spanner_instance", instance_id, "reused", "shared"))
    else:
        adapter.create_instance(
            request.project_id,
            request.region,
            request.instance_id,
            request.processing_units,
            request.run_id,
        )
        resources.append(
            _resource("spanner_instance", instance_id, "created", "run_owned")
        )

    if adapter.database_exists_for(
        request.project_id, request.instance_id, request.database_id
    ):
        resources.append(_resource("spanner_database", database_id, "reused", "shared"))
    else:
        adapter.create_database(
            request.project_id, request.instance_id, request.database_id
        )
        resources.append(
            _resource("spanner_database", database_id, "created", "run_owned")
        )

    for role in DATABASE_ROLES:
        adapter.grant_database_role(
            request.project_id,
            request.instance_id,
            request.database_id,
            request.runtime_service_account,
            role,
        )


def _provision_absent(
    request: ProvisionRequest,
    adapter: SpannerAdapter,
    resources: list[dict[str, str]],
) -> None:
    instance_id = _instance_resource_id(request)
    database_id = _database_resource_id(request)
    owned = set(request.owned_resources)

    if not adapter.instance_exists_for(request.project_id, request.instance_id):
        if database_id in owned:
            resources.append(
                _resource("spanner_database", database_id, "already_absent", "run_owned")
            )
        if instance_id in owned:
            resources.append(
                _resource("spanner_instance", instance_id, "already_absent", "run_owned")
            )
        return

    database_exists = adapter.database_exists_for(
        request.project_id, request.instance_id, request.database_id
    )
    if database_exists:
        if database_id in owned:
            adapter.delete_database(
                request.project_id, request.instance_id, request.database_id
            )
            resources.append(
                _resource("spanner_database", database_id, "deleted", "run_owned")
            )
        else:
            resources.append(
                _resource("spanner_database", database_id, "skipped_shared", "shared")
            )
    elif database_id in owned:
        resources.append(
            _resource("spanner_database", database_id, "already_absent", "run_owned")
        )

    if instance_id not in owned:
        resources.append(
            _resource("spanner_instance", instance_id, "skipped_shared", "shared")
        )
        return

    remaining_databases = adapter.list_databases(request.project_id, request.instance_id)
    non_owned_databases = [
        resource_id for resource_id in remaining_databases if resource_id not in owned
    ]
    if non_owned_databases:
        resources.append(
            _resource("spanner_instance", instance_id, "skipped_shared", "shared")
        )
        return

    adapter.delete_instance(request.project_id, request.instance_id)
    resources.append(_resource("spanner_instance", instance_id, "deleted", "run_owned"))


def _success_result(
    request: ProvisionRequest,
    resources: list[dict[str, str]],
    environment: dict[str, str],
) -> dict[str, object]:
    return {
        "protocolVersion": request.protocol_version,
        "status": "succeeded",
        "resources": resources,
        "environment": environment,
    }


def _failure_result(
    protocol_version: str,
    resources: list[dict[str, str]],
    exc: Exception,
) -> dict[str, object]:
    code = "provider_error"
    message = sanitize_error_message(str(exc)) or "provider operation failed"

    if isinstance(exc, ProvisioningError):
        code = exc.code
        message = sanitize_error_message(exc.message) or "provider operation failed"
    elif isinstance(exc, FileNotFoundError) and exc.filename == "gcloud":
        code = "missing_gcloud"
        message = "gcloud CLI is required but was not found"
    elif isinstance(exc, (ValueError, json.JSONDecodeError, TypeError)):
        code = "invalid_request"

    return {
        "protocolVersion": protocol_version,
        "status": "failed",
        "resources": resources,
        "environment": {},
        "error": {
            "code": code,
            "message": message,
        },
    }


def _resource(
    resource_type: str, resource_id: str, action: str, ownership: str
) -> dict[str, str]:
    return {
        "type": resource_type,
        "id": resource_id,
        "action": action,
        "ownership": ownership,
    }


def _environment(request: ProvisionRequest) -> dict[str, str]:
    return {
        "SPANNER_PROJECT_ID": request.project_id,
        "SPANNER_INSTANCE_ID": request.instance_id,
        "SPANNER_DATABASE_ID": request.database_id,
    }


def _instance_resource_id(request: ProvisionRequest) -> str:
    return f"projects/{request.project_id}/instances/{request.instance_id}"


def _database_resource_id(request: ProvisionRequest) -> str:
    return (
        f"{_instance_resource_id(request)}/databases/{request.database_id}"
    )


def _validate_owned_resources(
    project_id: str,
    instance_id: str,
    database_id: str,
    owned_resources: tuple[str, ...],
) -> None:
    allowed_resource_ids = {
        f"projects/{project_id}/instances/{instance_id}",
        f"projects/{project_id}/instances/{instance_id}/databases/{database_id}",
    }
    if len(set(owned_resources)) != len(owned_resources):
        raise ValueError("ownedResources must not contain duplicates")
    if any(resource_id not in allowed_resource_ids for resource_id in owned_resources):
        raise ValueError(
            "ownedResources must contain only this request's full instance or database ID"
        )


def _require_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _validate_identifier(value: str, pattern: re.Pattern[str], field: str) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"{field} is invalid")


def normalize_run_id(run_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    return (normalized or "run")[:63]


def sanitize_error_message(message: str) -> str:
    redactions = [
        (r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [redacted]"),
        (r"(?i)(authorization\s*:\s*)(.+)", r"\1[redacted]"),
        (r"(?i)(private[_-]?key\s*=?\s*)([^,\s]+)", r"\1[redacted]"),
        (r"(?i)(access[_-]?token\s*=?\s*)([^,\s]+)", r"\1[redacted]"),
        (r"(?i)(refresh[_-]?token\s*=?\s*)([^,\s]+)", r"\1[redacted]"),
        (r"(?i)(client[_-]?secret\s*=?\s*)([^,\s]+)", r"\1[redacted]"),
        (r"(?i)(password\s*=?\s*)([^,\s]+)", r"\1[redacted]"),
    ]
    sanitized = message
    for pattern, replacement in redactions:
        sanitized = re.sub(pattern, replacement, sanitized)
    return sanitized.strip()
