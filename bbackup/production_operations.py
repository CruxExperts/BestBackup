"""One validated operation boundary for the machine CLI and interactive dashboard."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urlsplit

from .b2 import B2VersionLockReader, inspect_b2
from .cli_metadata import PRODUCTION_COMMANDS, production_schema
from .cloud import S3CloudAdapter, _normalize_endpoint_url
from .configuration_workspace import ConfigurationWorkspace, read_document
from .enrollment import (_read_regular, machine_identity, parse_pair, policy_lease,
                         read_pair, verify_bytes)
from .linux_coverage import (check_coverage, discover_coverage,
                             evaluate_recovery_evidence, review_coverage)
from .postgresql_coverage import PostgreSQLCoverageContext
from .models import ConfigurationError, strict_json
from .operations import OperationRunner, ReconciliationOutcome, repository_key
from .recovery import (accept_recovery_checkpoint, create_checked_kit, open_kit, verify_checkpoint,
                       verify_reconstructed_repository)
from .scheduling import install_user_units, job_units, system_job_units
from .service import BackupService, ServiceError
from .service_status import operational_status


def validate_operation_input(command: str, values: dict[str, Any]) -> dict[str, Any]:
    """Apply the registry's strict types, defaults, and unknown-field boundary."""
    if command not in PRODUCTION_COMMANDS or not isinstance(values, dict):
        raise ConfigurationError("Unknown operation or non-object input")
    fields = PRODUCTION_COMMANDS[command]["fields"]
    if set(values) - set(fields):
        raise ConfigurationError("Unknown operation fields")
    result = {}
    for name, kind in fields.items():
        default = False if kind == "boolean" else 100 if kind == "limit" else 0 if kind == "offset" else None
        value = values.get(name, default)
        if value is None:
            if kind == "optional-string":
                continue
            raise ConfigurationError("Missing required input")
        if kind == "boolean":
            if type(value) is not bool:
                raise ConfigurationError("Expected boolean")
        elif kind in ("limit", "offset"):
            if type(value) is not int or value < (1 if kind == "limit" else 0) or (kind == "limit" and value > 1000):
                raise ConfigurationError("Invalid pagination")
        elif not isinstance(value, str) or not value or "\0" in value:
            raise ConfigurationError("Expected a nonempty string")
        result[name] = value
    return result


@contextmanager
def _operation_lease(config_path, bindings_path):
    if config_path is None or bindings_path is None:
        raise ConfigurationError("This operation requires configuration and bindings")
    preliminary_policy, preliminary_bindings = read_pair(config_path, bindings_path)
    configuration, bindings = parse_pair(preliminary_policy, preliminary_bindings)
    if configuration.schema_version != 3:
        raise ConfigurationError("Version 2 is inspection and migration only; run config migrate")
    with policy_lease(bindings.state_dir):
        policy_bytes, binding_bytes = read_pair(config_path, bindings_path)
        current_config, current_bindings = parse_pair(policy_bytes, binding_bytes)
        if current_config.schema_version != 3:
            raise ConfigurationError("Version 2 is inspection and migration only; run config migrate")
        if current_bindings.state_dir.resolve() != bindings.state_dir.resolve():
            raise ConfigurationError("Private state location changed during operation; retry")
        verify_bytes(current_bindings.state_dir, policy_bytes, binding_bytes)
        if read_pair(config_path, bindings_path) != (policy_bytes, binding_bytes):
            raise ConfigurationError("Configuration changed during enrollment validation; review and retry")
        yield BackupService(current_config, current_bindings,
                            policy_bytes=policy_bytes, binding_bytes=binding_bytes)


def execute_operation(command: str, values: dict[str, Any], *, service: BackupService | None = None,
                      config_path: Path | None = None, bindings_path: Path | None = None,
                      cancel=None):
    """Execute exactly one requested operation; never retry or replay mutations."""
    values = validate_operation_input(command, values)
    if command == "skills":
        name = values.get("name")
        if name is not None and name not in PRODUCTION_COMMANDS:
            raise ConfigurationError("Unknown command schema")
        return {key: {"summary": item["summary"], "input_schema": production_schema(key),
                      "ui": item.get("ui", {})}
                for key, item in PRODUCTION_COMMANDS.items() if name is None or key == name}
    if command == "setup" or command.startswith("config ") or command == "service enroll":
        if config_path is None or bindings_path is None:
            raise ConfigurationError("This operation requires configuration and bindings paths")
        workspace = ConfigurationWorkspace(config_path, bindings_path)
        if command == "setup":
            if values["migrate"] and values["enroll"]:
                raise ConfigurationError("Choose migration or enrollment")
            if values["migrate"]:
                return workspace.migrate()
            if values["enroll"]:
                return workspace.enroll()
            if "configuration_file" not in values or "bindings_file" not in values:
                raise ConfigurationError("Setup requires policy and binding candidates")
            workspace.setup(read_document(Path(values["configuration_file"])),
                            read_document(Path(values["bindings_file"])))
            return {"configured": True, "repositories_initialized": False, "services_enabled": False}
        if command == "config show":
            return workspace.read()
        if command == "config migrate":
            return workspace.migrate()
        if command == "service enroll":
            return workspace.enroll()
        return workspace.save(read_document(Path(values["document_file"])), values["revision"])
    if command.startswith("recovery ") and command != "recovery checkpoint":
        return _recovery_dispatch(service, command, values, cancel)
    if command == "coverage review":
        # Review owns an exclusive policy lease; do not nest it inside the
        # ordinary shared operation lease and deadlock the same process.
        if config_path is None or bindings_path is None:
            raise ConfigurationError("Coverage review requires configuration and bindings paths")
        policy_bytes, binding_bytes = read_pair(config_path, bindings_path)
        configuration, bindings = parse_pair(policy_bytes, binding_bytes)
        if configuration.schema_version != 3:
            raise ConfigurationError("Coverage review requires version-three enrollment")
        return _coverage_operation(configuration, bindings, policy_bytes, binding_bytes,
                                   command, values, cancel=cancel)
    if command == "doctor" and config_path is not None and bindings_path is not None:
        policy_bytes, binding_bytes = read_pair(config_path, bindings_path)
        configuration, bindings = parse_pair(policy_bytes, binding_bytes)
        if configuration.schema_version != 3:
            return {"restic_available": shutil.which("restic") is not None,
                    "jobs": len(configuration.jobs), "enrollment_valid": False,
                    "next_action": "Run config migrate, or open the dashboard to review migration",
                    "production_qualified": False}
        try:
            with policy_lease(bindings.state_dir):
                verify_bytes(bindings.state_dir, policy_bytes, binding_bytes)
        except (ConfigurationError, OSError):
            # Inspection stays available without creating a ledger or approving new authority.
            return {"restic_available": shutil.which("restic") is not None,
                    "jobs": len(configuration.jobs), "enrollment_valid": False,
                    "next_action": "Review the exact local mappings, then run service enroll",
                    "production_qualified": False}
    with _operation_lease(config_path, bindings_path) as service:
        if command == "recovery checkpoint":
            return _recovery_dispatch(service, command, values, cancel)
        return _service_operation(service, command, values, config_path, bindings_path, cancel)


def _service_operation(service, command, values, config_path, bindings_path, cancel):
    if command.startswith("coverage "):
        return _coverage_operation(service.config, service.bindings,
                                   service.policy_bytes, service.binding_bytes, command, values,
                                   cancel=cancel)
    if command.startswith("cloud-retention "):
        return _cloud_retention_operation(service, command, values, cancel)
    if command.startswith("cloud-maintenance "):
        return _cloud_maintenance_operation(service, command, values, cancel)
    if command == "doctor":
        return {"restic_available": shutil.which("restic") is not None,
                "jobs": len(service.config.jobs), "production_qualified": False,
                "status": operational_status(service)}
    if command == "jobs list":
        return [asdict(job) for job in service.config.jobs]
    if command == "jobs run":
        return service.run_job(values["name"], cancel=cancel,
            quiet_administrative_window_confirmed=values["quiet_administrative_window_confirmed"])
    if command == "repositories list":
        return [asdict(repo) for repo in service.config.repositories]
    if command == "repositories init":
        return service.initialize(values["name"], source=values.get("source"), cancel=cancel)
    if command == "repositories check":
        return service.check(values["name"], cancel=cancel)
    if command == "snapshots list":
        return service.snapshots(**values)
    if command == "retention plan":
        return service.retention_plan(values["job"], cancel=cancel)
    if command == "retention apply":
        return service.retention_apply(
            values["job"], values["plan_token"], values["approved"], cancel=cancel
        )
    if command == "snapshots copy":
        return service.copy(**values, cancel=cancel)
    if command == "restore":
        return service.restore(values["repository"], values["snapshot"], Path(values["target"]), cancel=cancel)
    if command == "databases restore":
        return service.restore_database(**values, cancel=cancel)
    if command == "docker restore":
        return service.docker_restore(
            values["repository"], values["snapshot"], values["source"],
            values["connection"], Path(values["target"]), values["container"],
            values["trusted_archive"], cancel=cancel,
        )
    if command == "docker recover":
        return service.docker_recover(
            values["connection"], values["reviewed"], cancel=cancel
        )
    if command == "docker reconcile":
        if values["docker_reviewed"] is not True:
            raise ConfigurationError("Docker inspection must be explicitly acknowledged")
        try:
            outcome = ReconciliationOutcome(values["outcome"])
        except ValueError:
            raise ConfigurationError("Invalid Docker reconciliation outcome") from None
        return service.docker_reconcile(
            values["connection"], values["container"], values["operation_id"],
            outcome, values["docker_reviewed"],
        )
    if command == "databases reconcile":
        if values["database_reviewed"] is not True:
            raise ConfigurationError("Database inspection must be explicitly acknowledged")
        try:
            outcome = ReconciliationOutcome(values["outcome"])
        except ValueError:
            raise ConfigurationError("Invalid reconciliation outcome") from None
        service.ledger.reconcile(repository_key(service.database_identity(values["connection"], values["target_database"])), values["operation_id"], outcome)
        return {"operation_id": values["operation_id"], "reviewed_outcome": outcome.value, "replayed": False}
    if command == "runs reconcile":
        if values["repository_reviewed"] is not True:
            raise ConfigurationError("Repository inspection must be explicitly acknowledged")
        try:
            outcome = ReconciliationOutcome(values["outcome"])
        except ValueError:
            raise ConfigurationError("Invalid reconciliation outcome") from None
        return service.reconcile_run(values["repository"], values["operation_id"], outcome,
                                     quiescent_window_confirmed=values["quiescent_window_confirmed"],
                                     cancel=cancel)
    if command == "runs retained-exports":
        return service.retained_exports(values["repository"], limit=values["limit"],
                                        offset=values["offset"])
    if command == "runs discard-exports":
        return service.discard_exports(values["repository"], values["operation_id"],
                                       values["artifact_record_sha256"])
    if command in ("runs list", "runs stream"):
        events = service.ledger.events(**values)
        return [asdict(event) for event in events]
    if command in ("service render", "service render-system"):
        if config_path is None or bindings_path is None:
            raise ConfigurationError("Rendering services requires configuration paths")
        render = system_job_units if command == "service render-system" else job_units
        return render(service._job(values["name"]), executable=Path(values["executable"]),
                      config=config_path.absolute(), bindings=bindings_path.absolute())
    if command == "service install":
        if config_path is None or bindings_path is None:
            raise ConfigurationError("Installing services requires configuration paths")
        units = job_units(service._job(values["name"]), executable=Path(values["executable"]),
                          config=config_path.absolute(), bindings=bindings_path.absolute())
        return install_user_units(units, activate=values["activate"])
    raise ServiceError("Unsupported command")


def _coverage_operation(configuration, bindings, policy_bytes, binding_bytes, command, values,
                        *, cancel=None):
    job = next((item for item in configuration.jobs if item.name == values["name"]), None)
    if job is None or job.coverage_policy is None or job.name not in bindings.coverage_declarations:
        raise ConfigurationError("Job requires portable coverage policy and private declaration")
    arguments = {"state": bindings.state_dir, "policy_bytes": policy_bytes,
                 "binding_bytes": binding_bytes}
    pg_sources = {source.name: bindings.source_bindings[source.name].resolve(source)
                  for source in configuration.sources if source.name in job.sources
                  and source.kind == "postgresql"}
    if pg_sources:
        arguments["postgresql_context"] = PostgreSQLCoverageContext(
            pg_sources, bindings.database_bindings, OperationRunner(), cancel=cancel,
            quiet_administrative_window=values.get("quiet_administrative_window_confirmed", False))
    declaration = bindings.coverage_declarations[job.name]
    if command == "coverage discover":
        return discover_coverage(declaration, job.coverage_policy, **arguments)
    if command == "coverage review":
        return review_coverage(declaration, job.coverage_policy, values["candidate_digest"], **arguments)
    coverage = check_coverage(declaration, job.coverage_policy, **arguments)
    if command == "coverage check":
        return coverage.public_result()
    if not coverage.capture_ready or coverage.baseline_digest is None:
        raise ConfigurationError("Recovery evidence requires the exact current reviewed coverage baseline")
    return evaluate_recovery_evidence(
        Path(values["evidence_file"]) if "evidence_file" in values else None,
        baseline_digest=coverage.baseline_digest, snapshot_id=values["snapshot"],
        source_machine_digest=machine_identity()[0])


def _private_json(path):
    try:
        return strict_json(_read_regular(Path(path), private=True).decode("utf-8"))
    except UnicodeError:
        raise ConfigurationError("Invalid private JSON input") from None


def _cloud_retention_operation(service, command, values, cancel):
    """Bind central cloud authority to the enrolled repository and its ledger."""
    adapter, lease_id = _central_cloud_adapter(service, values)
    if command == "cloud-retention catalog-enroll":
        return adapter.initialize_central_retention_catalog(values["policy_file"], values["grant_file"])
    if command == "cloud-retention reconcile":
        return adapter.reconcile_central_retention(
            values["plan_file"], ledger=service.ledger, expected_repository_lease_id=lease_id)
    directories = _central_record_directories(values["records_file"])
    if command == "cloud-retention plan":
        return adapter.plan_central_retention(
            values["policy_file"], values["grant_file"], directories,
            quiescent_window_confirmed=values["quiescent_window_confirmed"],
            repository_lease_id=lease_id)
    return adapter.apply_central_retention(
        values["plan_id"], values["plan_sha256"], values["policy_file"], values["grant_file"],
        directories, quiescent_window_confirmed=values["quiescent_window_confirmed"],
        ledger=service.ledger, cancel_event=cancel, expected_repository_lease_id=lease_id)


def _central_cloud_adapter(service, values, *, maintenance=False):
    """Freeze one inspection/deletion pair for native and S3 authority."""
    policy = _private_json(values["policy_file"])
    if not isinstance(policy, dict):
        raise ConfigurationError("Invalid central retention policy")
    endpoint = _normalize_endpoint_url(policy.get("endpoint_url"))
    bucket, prefix = policy.get("bucket"), policy.get("prefix")
    if (not isinstance(bucket, str) or not bucket or "/" in bucket
            or not isinstance(prefix, str) or not prefix or prefix.startswith("/")):
        raise ConfigurationError("Invalid central retention target")
    identity = service._identity(values["repository"])
    expected = f"s3:{endpoint}/{bucket}/{prefix.rstrip('/')}"
    if identity.rstrip("/") != expected:
        raise ConfigurationError("Central retention target differs from enrolled repository")
    native = policy.get("lock_metadata") is not None
    freeze_pair = native or policy.get("schema_version") == 2 or (policy.get("schema_version") == 3 and maintenance)
    if freeze_pair:
        parsed = urlsplit(endpoint)
        match = re.fullmatch(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", parsed.netloc)
        if parsed.scheme != "https" or match is None or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ConfigurationError("Native B2 retention requires a canonical B2 S3 endpoint")
        lock_metadata = policy.get("lock_metadata")
        if native and (
            not isinstance(lock_metadata, dict) or set(lock_metadata) != {"provider", "bucket_id"}
            or lock_metadata.get("provider") != "b2-native"
            or not isinstance(lock_metadata.get("bucket_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", lock_metadata["bucket_id"])
        ):
            raise ConfigurationError("Native B2 lock target is invalid")
        try:
            import boto3
            from botocore.config import Config

            credentials = boto3.Session().get_credentials()
            frozen = credentials.get_frozen_credentials() if credentials is not None else None
            if frozen is None or not frozen.access_key or not frozen.secret_key or frozen.token:
                raise ValueError()
            pair = (frozen.access_key, frozen.secret_key)
            reader = (B2VersionLockReader(
                bucket, lock_metadata["bucket_id"], prefix, endpoint,
                access_key_id=pair[0], secret_access_key=pair[1],
            ) if native else None)
            client = boto3.client(
                "s3", endpoint_url=endpoint, region_name=match.group(1),
                aws_access_key_id=pair[0], aws_secret_access_key=pair[1],
                config=Config(connect_timeout=10, read_timeout=30,
                              retries={"total_max_attempts": 5, "mode": "standard"},
                              signature_version="s3v4"),
            )
        except Exception:
            raise ConfigurationError("Could not bind native B2 retention authority") from None
        adapter = S3CloudAdapter(
            bucket, client=client, endpoint_url=endpoint, region_name=match.group(1),
            prefix=prefix, native_lock_reader=reader, credential_pair=pair,
        )
    else:
        adapter = S3CloudAdapter(bucket, endpoint_url=endpoint, prefix=prefix)
    return adapter, repository_key(identity)


def _central_record_directories(path):
    directories = _private_json(path)
    if (not isinstance(directories, list) or not directories or len(directories) > 10000
            or any(not isinstance(path, str) or not Path(path).is_absolute()
                   or any(char in path for char in "\0\r\n") for path in directories)
            or len(set(directories)) != len(directories)):
        raise ConfigurationError("Expected unique absolute central acceptance directories")
    return directories


def _cloud_maintenance_operation(service, command, values, cancel):
    adapter, lease_id = _central_cloud_adapter(service, values, maintenance=True)
    if command == "cloud-maintenance catalog-enroll":
        return adapter.initialize_central_retention_catalog(
            values["policy_file"], values["grant_file"],
            expected_action="maintain_current_repository",
        )
    directories = _central_record_directories(values["records_file"])
    if command == "cloud-maintenance records-enroll":
        return adapter.enroll_central_maintenance_records(
            values["policy_file"], values["grant_file"], directories,
        )
    return service.cloud_maintenance(
        command, adapter, values, directories, lease_id=lease_id, cancel=cancel,
    )


def _recovery_dispatch(service, command, values, cancel):
    if command == "recovery b2-inspect":
        return inspect_b2(**values, cancel=cancel)
    if command == "recovery open":
        return open_kit(Path(values["kit"]), Path(values["target"]), signer=values["signer"], cancel=cancel)
    adapter = S3CloudAdapter(values["bucket"], endpoint_url=values["endpoint"],
                             prefix=values.get("prefix", ""), region_name=values.get("region"))
    if command == "recovery accept":
        return accept_recovery_checkpoint(
            adapter, Path(values["kit"]), Path(values["target"]),
            source_signer=values["source_signer"], trusted_verifier=values["verifier"],
            source_repository=values["repository"], repository_id=values["repository_id"],
            snapshot=values["snapshot"], cancel=cancel,
        )
    if command == "recovery inspect":
        return asdict(adapter.inspect_lifecycle())
    if command == "recovery checkpoint":
        return create_checked_kit(service, adapter, values["repository"], values["snapshot"], Path(values["target"]),
                                  signer=values["signer"], recipient=values["recipient"],
                                  quiescent_window_confirmed=values["quiescent_window_confirmed"], cancel=cancel)
    digest = verify_checkpoint(Path(values["manifest"]), Path(values["signature"]), values["signer"], cancel=cancel)
    result = asdict(adapter.restore_checkpoint(values["manifest"], values["target"],
                     expected_manifest_sha256=digest, expected_source_repository=values["repository"],
                     expected_repository_id=values["repository_id"], expected_source_snapshot_id=values["snapshot"], cancel_event=cancel))
    result["destination"] = str(result["destination"])
    verify_reconstructed_repository(Path(values["target"]), Path(values["password_file"]),
                                    values["repository_id"], values["snapshot"], cancel=cancel)
    result["repository_verified"] = True
    result["recovery_demonstrated"] = False
    return result
