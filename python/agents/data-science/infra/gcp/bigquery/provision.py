import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

PROTOCOL_VERSION = "1"
REPO_ROOT = Path(__file__).resolve().parents[3]

PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
LOCATION_RE = re.compile(r"^[a-z]+(?:-[a-z0-9]+)*$")
DATASET_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class SeedTable:
    table: str
    file: str
    allow_quoted_newlines: bool = False


@dataclass(frozen=True)
class ProvisionRequest:
    protocol_version: str
    desired_state: Literal["present", "absent"]
    run_id: str
    project_id: str
    location: str
    dataset_id: str
    seed_tables: tuple[SeedTable, ...] = ()
    owned_resources: tuple[str, ...] = ()


class BigqueryAdapter(Protocol):
    def ensure_gcloud_available(self) -> None: ...

    def enable_bigquery_api(self, project_id: str) -> None: ...

    def dataset_exists_for(self, project_id: str, dataset_id: str) -> bool: ...

    def create_dataset(
        self,
        project_id: str,
        location: str,
        dataset_id: str,
        run_id: str,
    ) -> None: ...

    def table_exists_for(
        self, project_id: str, dataset_id: str, table: str
    ) -> bool: ...

    def load_seed_table(
        self,
        project_id: str,
        location: str,
        dataset_id: str,
        table: str,
        csv_file: str,
        allow_quoted_newlines: bool,
    ) -> None: ...

    def delete_dataset(self, project_id: str, dataset_id: str) -> None: ...


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class GcloudBigqueryAdapter:
    def ensure_gcloud_available(self) -> None:
        if shutil.which("gcloud") is None or shutil.which("bq") is None:
            raise ProvisioningError(
                "missing_gcloud", "gcloud and bq CLIs are required but were not found"
            )

    def enable_bigquery_api(self, project_id: str) -> None:
        self._run_gcloud(
            [
                "services",
                "enable",
                "bigquery.googleapis.com",
                "--project",
                project_id,
                "--quiet",
            ]
        )

    def dataset_exists_for(self, project_id: str, dataset_id: str) -> bool:
        return self._describe_exists(
            ["--project_id", project_id, "--format=none", "show", "--dataset", dataset_id]
        )

    def create_dataset(
        self,
        project_id: str,
        location: str,
        dataset_id: str,
        run_id: str,
    ) -> None:
        self._run_bq(
            [
                "--project_id",
                project_id,
                "--location",
                location,
                "mk",
                "--dataset",
                "--label",
                "sherpa-managed:true",
                "--label",
                f"run-id:{normalize_run_id(run_id)}",
                dataset_id,
            ]
        )

    def table_exists_for(
        self, project_id: str, dataset_id: str, table: str
    ) -> bool:
        return self._describe_exists(
            [
                "--project_id",
                project_id,
                "--format=none",
                "show",
                f"{dataset_id}.{table}",
            ]
        )

    def load_seed_table(
        self,
        project_id: str,
        location: str,
        dataset_id: str,
        table: str,
        csv_file: str,
        allow_quoted_newlines: bool,
    ) -> None:
        source_path = REPO_ROOT / csv_file
        if not source_path.is_file():
            raise ProvisioningError(
                "seed_file_missing",
                f"seed CSV '{csv_file}' was not found in the workload source",
            )
        args = [
            "--project_id",
            project_id,
            "--location",
            location,
            "load",
            "--source_format=CSV",
            "--autodetect",
            "--skip_leading_rows=1",
        ]
        if allow_quoted_newlines:
            args.append("--allow_quoted_newlines")
        args.extend([f"{dataset_id}.{table}", str(source_path)])
        self._run_bq(args)

    def delete_dataset(self, project_id: str, dataset_id: str) -> None:
        self._run_bq(
            [
                "--project_id",
                project_id,
                "rm",
                "--recursive",
                "--force",
                "--dataset",
                dataset_id,
            ]
        )

    def _describe_exists(self, args: list[str]) -> bool:
        try:
            self._run_bq(args)
        except ProvisioningError as exc:
            if exc.code == "not_found":
                return False
            raise
        return True

    def _run_bq(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run_cli("bq", args)

    def _run_gcloud(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run_cli("gcloud", args)

    def _run_cli(
        self, cli: str, args: list[str]
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [cli, *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result

        stderr = sanitize_error_message(result.stderr.strip())
        if "not found" in result.stderr.lower() or "NOT_FOUND" in result.stderr:
            raise ProvisioningError("not_found", stderr or "resource not found")
        raise ProvisioningError(
            "bigquery_command_failed", stderr or f"{cli} command failed"
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
    location = _require_string(payload, "location")
    dataset_id = _require_string(payload, "datasetId")

    _validate_identifier(project_id, PROJECT_ID_RE, "projectId")
    _validate_identifier(location, LOCATION_RE, "location")
    _validate_identifier(dataset_id, DATASET_ID_RE, "datasetId")

    seed_tables = _parse_seed_tables(payload.get("seedTables", []))

    owned_resources_raw = payload.get("ownedResources", [])
    if not isinstance(owned_resources_raw, list) or any(
        not isinstance(item, str) or not item for item in owned_resources_raw
    ):
        raise ValueError("ownedResources must be a list of non-empty strings")
    owned_resources = tuple(owned_resources_raw)

    if desired_state == "absent":
        if not owned_resources:
            raise ValueError("ownedResources must include exact owned resource IDs")
        _validate_owned_resources(project_id, dataset_id, owned_resources)

    return ProvisionRequest(
        protocol_version=protocol_version,
        desired_state=desired_state,
        run_id=run_id,
        project_id=project_id,
        location=location,
        dataset_id=dataset_id,
        seed_tables=seed_tables,
        owned_resources=owned_resources,
    )


def _parse_seed_tables(raw: object) -> tuple[SeedTable, ...]:
    if not isinstance(raw, list):
        raise ValueError("seedTables must be a list of objects")
    tables: list[SeedTable] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValueError("seedTables must be a list of objects")
        table = _require_string(entry, "table")
        file = _require_string(entry, "file")
        allow_quoted = entry.get("allowQuotedNewlines", False)
        if not isinstance(allow_quoted, bool):
            raise ValueError("seedTables allowQuotedNewlines must be a boolean")
        _validate_identifier(table, DATASET_ID_RE, "seedTables table")
        if ".." in file or file.startswith("/"):
            raise ValueError("seedTables file must be a relative path inside the source")
        tables.append(
            SeedTable(table=table, file=file, allow_quoted_newlines=allow_quoted)
        )
    return tuple(tables)


def provision(request: ProvisionRequest, adapter: BigqueryAdapter) -> dict[str, object]:
    resources: list[dict[str, str]] = []
    try:
        adapter.ensure_gcloud_available()
        if request.desired_state == "present":
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
        result = provision(request, GcloudBigqueryAdapter())
    except Exception as exc:
        result = _failure_result(PROTOCOL_VERSION, [], exc)

    sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["status"] == "succeeded" else 1


def _provision_present(
    request: ProvisionRequest,
    adapter: BigqueryAdapter,
    resources: list[dict[str, str]],
) -> None:
    dataset_id = _dataset_resource_id(request)

    if adapter.dataset_exists_for(request.project_id, request.dataset_id):
        resources.append(_resource("bigquery_dataset", dataset_id, "reused", "shared"))
    else:
        adapter.create_dataset(
            request.project_id,
            request.location,
            request.dataset_id,
            request.run_id,
        )
        resources.append(
            _resource("bigquery_dataset", dataset_id, "created", "run_owned")
        )

    for seed in request.seed_tables:
        table_id = _table_resource_id(request, seed.table)
        if adapter.table_exists_for(
            request.project_id, request.dataset_id, seed.table
        ):
            resources.append(_resource("bigquery_table", table_id, "reused", "shared"))
            continue
        adapter.load_seed_table(
            request.project_id,
            request.location,
            request.dataset_id,
            seed.table,
            seed.file,
            seed.allow_quoted_newlines,
        )
        resources.append(
            _resource("bigquery_table", table_id, "created", "run_owned")
        )


def _provision_absent(
    request: ProvisionRequest,
    adapter: BigqueryAdapter,
    resources: list[dict[str, str]],
) -> None:
    dataset_id = _dataset_resource_id(request)
    owned = set(request.owned_resources)

    if not adapter.dataset_exists_for(request.project_id, request.dataset_id):
        if dataset_id in owned:
            resources.append(
                _resource("bigquery_dataset", dataset_id, "already_absent", "run_owned")
            )
        return

    if dataset_id not in owned:
        resources.append(
            _resource("bigquery_dataset", dataset_id, "skipped_shared", "shared")
        )
        return

    adapter.delete_dataset(request.project_id, request.dataset_id)
    resources.append(_resource("bigquery_dataset", dataset_id, "deleted", "run_owned"))


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
    elif isinstance(exc, (FileNotFoundError, shutil.Error)):
        code = "missing_gcloud"
        message = "gcloud and bq CLIs are required but were not found"
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
        "BQ_DATASET_ID": request.dataset_id,
    }


def _dataset_resource_id(request: ProvisionRequest) -> str:
    return f"projects/{request.project_id}/datasets/{request.dataset_id}"


def _table_resource_id(request: ProvisionRequest, table: str) -> str:
    return f"{_dataset_resource_id(request)}/tables/{table}"


def _validate_owned_resources(
    project_id: str,
    dataset_id: str,
    owned_resources: tuple[str, ...],
) -> None:
    allowed_resource_ids = {
        f"projects/{project_id}/datasets/{dataset_id}",
    }
    if len(set(owned_resources)) != len(owned_resources):
        raise ValueError("ownedResources must not contain duplicates")
    if any(resource_id not in allowed_resource_ids for resource_id in owned_resources):
        raise ValueError(
            "ownedResources must contain only this request's full dataset ID"
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


if __name__ == "__main__":
    raise SystemExit(main())