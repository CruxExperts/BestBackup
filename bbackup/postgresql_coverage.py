"""Bounded, machine-local PostgreSQL coverage inspection.

The module inspects one PostgreSQL cluster through the same native operation
runner used by database capture. It records structural digests only; server
settings, credentials, and command output stay in memory.
"""
from __future__ import annotations

from dataclasses import dataclass
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from .models import DatabaseBinding, Source
from .operations import OperationPlan, OperationRunner, OperationStatus


_MAX_OUTPUT = 1024 * 1024
_MAX_CONNECTION_FILE = 1024 * 1024
_MAX_RECOVERY_MATERIAL = 64 * 1024 * 1024
_MAX_DATABASES = 4096
_MAX_ROLES = 65536
_MAX_CONFIG_ITEMS = 100000
_SYSTEM_ID = re.compile(r"^Database system identifier:\s*([0-9]+)\s*$", re.MULTILINE)
_PG_VERSION = re.compile(r"PostgreSQL\)\s+([0-9]+)(?:\.([0-9]+))?")
_DATA_VERSION = re.compile(r"([0-9]+)(?:\.([0-9]+))?\Z")
_INCLUDE = re.compile(r"^\s*(include|include_if_exists|include_dir)\s*(?:=\s*)?(['\"])(.*?)\2\s*(?:#.*)?$", re.I)


class PostgreSQLCoverageError(RuntimeError):
    """Sanitized PostgreSQL coverage inspection failure."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PostgreSQLCoverageContext:
    """Resolved native PostgreSQL inputs for a coverage scan.

    ``sources`` is keyed by source name and ``database_bindings`` by the exact
    private connection selector. The supplied runner is the normal bounded
    ``OperationRunner``; command output is additionally capped here.
    """

    sources: Mapping[str, Source]
    database_bindings: Mapping[str, DatabaseBinding]
    runner: OperationRunner
    cancel: Any = None
    timeout_seconds: float = 15.0
    quiet_administrative_window: bool = False

    def __post_init__(self):
        if (not isinstance(self.sources, Mapping)
                or not isinstance(self.database_bindings, Mapping)
                or not hasattr(self.runner, "run")
                or isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not 0 < self.timeout_seconds <= 60):
            raise ValueError("Invalid PostgreSQL inspection context")
        if type(self.quiet_administrative_window) is not bool:
            raise ValueError("Invalid PostgreSQL consistency window")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _no_symlink_ancestors(path: Path) -> bool:
    current = path
    while True:
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
        except OSError:
            return False
        if current.parent == current:
            return True
        current = current.parent


def _private_bytes(path: Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077):
                raise PostgreSQLCoverageError("postgresql_private_material_unsafe")
            data = stream.read(limit + 1)
    except PostgreSQLCoverageError:
        raise
    except OSError:
        raise PostgreSQLCoverageError("postgresql_private_material_unavailable") from None
    if len(data) > limit:
        raise PostgreSQLCoverageError("postgresql_private_material_too_large")
    return data


def _private_file_digest(path: Path, limit: int) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077):
                raise PostgreSQLCoverageError("postgresql_private_material_unsafe")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = stream.read(min(65536, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise PostgreSQLCoverageError("postgresql_private_material_too_large")
                digest.update(chunk)
    except PostgreSQLCoverageError:
        raise
    except OSError:
        raise PostgreSQLCoverageError("postgresql_private_material_unavailable") from None
    return digest.hexdigest()


def _read_cluster_config(data_directory: Path, config_files: list[str],
                         auth_files: set[Path]) -> str:
    """Hash the active config tree while rejecting external files and links."""
    visited: set[Path] = set()
    file_digests: dict[Path, str] = {}
    auto_config = data_directory / "postgresql.auto.conf"
    pending = [Path(item) for item in config_files]
    if auto_config.exists() or auto_config.is_symlink():
        pending.append(auto_config)
    while pending:
        path = pending.pop()
        if not path.is_absolute():
            raise PostgreSQLCoverageError("postgresql_external_configuration")
        path = Path(os.path.normpath(path))
        if not _within(path, data_directory) or not _no_symlink_ancestors(path):
            raise PostgreSQLCoverageError("postgresql_external_configuration")
        if path in visited:
            continue
        visited.add(path)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise PostgreSQLCoverageError("postgresql_configuration_unsupported")
            content = _private_or_server_file(path, 4 * 1024 * 1024)
        except PostgreSQLCoverageError:
            raise
        except OSError:
            raise PostgreSQLCoverageError("postgresql_configuration_unavailable") from None
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeError:
            raise PostgreSQLCoverageError("postgresql_configuration_malformed") from None
        file_digests[path] = hashlib.sha256(content).hexdigest()
        for line in lines:
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            if path in auth_files and (
                    re.match(r"^(?:include|include_if_exists|include_dir)\s", stripped, re.I)
                    or "@" in stripped):
                raise PostgreSQLCoverageError("postgresql_hba_dependencies_unsupported")
            match = _INCLUDE.fullmatch(line)
            if match:
                kind, _, value = match.groups()
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = path.parent / candidate
                candidate = Path(os.path.normpath(candidate))
                if not _within(candidate, data_directory):
                    raise PostgreSQLCoverageError("postgresql_external_configuration")
                if kind.lower() == "include_dir":
                    if not _no_symlink_ancestors(candidate):
                        raise PostgreSQLCoverageError("postgresql_external_configuration")
                    try:
                        children = sorted(candidate.glob("*.conf"))
                    except OSError:
                        raise PostgreSQLCoverageError("postgresql_configuration_unavailable") from None
                    if len(children) > 4096:
                        raise PostgreSQLCoverageError("postgresql_configuration_limit")
                    pending.extend(children)
                else:
                    if not candidate.exists() and kind.lower() == "include_if_exists":
                        continue
                    pending.append(candidate)
                continue
            if re.match(r"^\s*include(?:_if_exists|_dir)?(?:\s|=)", line, re.I):
                raise PostgreSQLCoverageError("postgresql_configuration_malformed")
        if len(visited) > 4096:
            raise PostgreSQLCoverageError("postgresql_configuration_limit")
    records = []
    for path in sorted(visited, key=str):
        records.append([str(path.relative_to(data_directory)),
                        file_digests[path]])
    return _digest(records)


def _private_or_server_file(path: Path, limit: int) -> bytes:
    """Read a server-owned configuration file without persisting its contents."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PostgreSQLCoverageError("postgresql_configuration_unsupported")
            content = stream.read(limit + 1)
    except PostgreSQLCoverageError:
        raise
    except OSError:
        raise PostgreSQLCoverageError("postgresql_configuration_unavailable") from None
    if len(content) > limit:
        raise PostgreSQLCoverageError("postgresql_configuration_limit")
    return content


def _validate_connection_file(path: Path, service_name: str) -> tuple[str, dict[str, str]]:
    content = _private_bytes(path, _MAX_CONNECTION_FILE)
    parser = configparser.ConfigParser(interpolation=None, strict=True, delimiters=("="),
                                       comment_prefixes=("#", ";"), inline_comment_prefixes=None)
    try:
        parser.read_string(content.decode("utf-8"))
        if service_name not in parser:
            raise ValueError()
        service = {key.lower(): value.strip() for key, value in parser[service_name].items()}
    except (UnicodeError, configparser.Error, ValueError):
        raise PostgreSQLCoverageError("postgresql_connection_file_malformed") from None
    if "hostaddr" in service or "include" in service or "include_dir" in service:
        raise PostgreSQLCoverageError("postgresql_remote_endpoint_unsupported")
    host = service.get("host")
    if host and (not host.startswith("/") or "," in host or "\0" in host or "\n" in host):
        raise PostgreSQLCoverageError("postgresql_remote_endpoint_unsupported")
    if host and not _no_symlink_ancestors(Path(host)):
        raise PostgreSQLCoverageError("postgresql_connection_socket_unsafe")
    return hashlib.sha256(content).hexdigest(), service


def _environment(binding: DatabaseBinding) -> dict[str, str]:
    if binding.service_file is None or binding.service_name is None:
        raise PostgreSQLCoverageError("postgresql_binding_missing")
    env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
           "PGSERVICEFILE": str(binding.service_file),
           "PGPASSFILE": str(binding.password_file) if binding.password_file else "/dev/null"}
    return env


def _run_bounded(context: PostgreSQLCoverageContext, repository: str,
                 argv: list[str], *, env: dict[str, str]) -> bytes:
    output = bytearray()

    def collect(chunk: bytes) -> None:
        if not isinstance(chunk, bytes) or len(output) + len(chunk) > _MAX_OUTPUT:
            raise PostgreSQLCoverageError("postgresql_inspection_output_limit")
        output.extend(chunk)

    plan = OperationPlan.create(repository, "pg-cov", argv, env=env,
                                timeout_seconds=float(context.timeout_seconds))
    try:
        result = context.runner.run(plan, on_stdout_chunk=collect,
                                    cancel_event=context.cancel)
    except PostgreSQLCoverageError:
        raise
    except Exception:
        raise PostgreSQLCoverageError("postgresql_inspection_failed") from None
    if (result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0
            or result.stdout_bytes != len(output) or len(output) > _MAX_OUTPUT):
        raise PostgreSQLCoverageError("postgresql_inspection_incomplete")
    return bytes(output)


def _parse_local_control(context: PostgreSQLCoverageContext, cluster: dict[str, Any],
                         env: dict[str, str]) -> tuple[str, str, str]:
    data_directory = Path(cluster["data_directory"])
    try:
        data_version_text = _private_or_server_file(data_directory / "PG_VERSION", 32).decode("ascii").strip()
    except (PostgreSQLCoverageError, UnicodeError):
        raise PostgreSQLCoverageError("postgresql_local_identity_malformed") from None
    data_version = _DATA_VERSION.fullmatch(data_version_text)
    if data_version is None:
        raise PostgreSQLCoverageError("postgresql_local_identity_malformed")
    data_major = data_version.group(1)
    if data_major == "9" and data_version.group(2) is not None:
        data_major += "." + data_version.group(2)
    version_output = _run_bounded(context, cluster["connection"],
                                  ["pg_controldata", "--version"], env=env)
    try:
        version_text = version_output.decode("utf-8").strip()
    except UnicodeError:
        raise PostgreSQLCoverageError("postgresql_local_identity_malformed") from None
    version_match = _PG_VERSION.search(version_text)
    if version_match is None:
        raise PostgreSQLCoverageError("postgresql_local_identity_malformed")
    local_major = version_match.group(1)
    control_output = _run_bounded(context, cluster["connection"],
                                  ["pg_controldata", str(data_directory)], env=env)
    try:
        control_text = control_output.decode("utf-8")
    except UnicodeError:
        raise PostgreSQLCoverageError("postgresql_local_identity_malformed") from None
    system_id = _SYSTEM_ID.search(control_text)
    if system_id is None:
        raise PostgreSQLCoverageError("postgresql_local_identity_malformed")
    return system_id.group(1), local_major, data_major


def _parse_sql_snapshot(context: PostgreSQLCoverageContext, database: str,
                        connection_selector: str,
                        binding: DatabaseBinding, env: dict[str, str]) -> dict[str, Any]:
    escaped_database = database.replace("\\", "\\\\").replace("'", "\\'")
    connection = f"service={binding.service_name} dbname='{escaped_database}'"
    query = """SELECT json_build_object(
      'system_identifier', (pg_control_system()).system_identifier::text,
      'current_database', current_database(),
      'server_version_num', current_setting('server_version_num')::integer,
      'data_directory', current_setting('data_directory'),
      'config_file', current_setting('config_file'),
      'hba_file', current_setting('hba_file'),
      'ident_file', current_setting('ident_file'),
      'in_recovery', pg_is_in_recovery(),
      'databases', COALESCE((SELECT json_agg(json_build_object(
        'name', d.datname, 'oid', d.oid::bigint, 'allow_connections', d.datallowconn,
        'encoding', pg_encoding_to_char(d.encoding), 'collate', d.datcollate,
        'ctype', d.datctype, 'locale_provider', COALESCE(to_jsonb(d)->>'datlocprovider','c'),
        'owner', owner.rolname, 'acl', d.datacl, 'connection_limit', d.datconnlimit,
        'is_template', d.datistemplate
      ) ORDER BY d.datname) FROM pg_database AS d
        JOIN pg_roles AS owner ON owner.oid=d.datdba), '[]'::json),
      'roles', COALESCE((SELECT json_agg(json_build_object(
        'name', r.rolname, 'superuser', r.rolsuper, 'inherit', r.rolinherit,
        'create_role', r.rolcreaterole, 'create_database', r.rolcreatedb,
        'can_login', r.rolcanlogin, 'replication', r.rolreplication,
        'bypass_rls', r.rolbypassrls, 'connection_limit', r.rolconnlimit,
        'valid_until', r.rolvaliduntil::text, 'password', r.rolpassword
      ) ORDER BY r.rolname) FROM pg_authid AS r), '[]'::json),
      'memberships', COALESCE((SELECT json_agg(json_build_object(
        'role', role_name.rolname, 'member', member_name.rolname,
        'admin', m.admin_option,
        'inherit', COALESCE((to_jsonb(m)->>'inherit_option')::boolean,true),
        'set', COALESCE((to_jsonb(m)->>'set_option')::boolean,true)
      ) ORDER BY role_name.rolname, member_name.rolname)
        FROM pg_auth_members AS m
        JOIN pg_roles AS role_name ON role_name.oid=m.roleid
        JOIN pg_roles AS member_name ON member_name.oid=m.member), '[]'::json),
      'extensions', COALESCE((SELECT json_agg(json_build_object(
        'name', e.extname, 'version', e.extversion) ORDER BY e.extname)
        FROM pg_extension AS e), '[]'::json),
      'languages', COALESCE((SELECT json_agg(lanname ORDER BY lanname)
        FROM pg_language WHERE lanname NOT IN ('internal','c','sql','plpgsql')), '[]'::json),
      'foreign_tables', (SELECT count(*) FROM pg_foreign_table),
      'foreign_servers', (SELECT count(*) FROM pg_foreign_server),
      'subscriptions', (SELECT count(*) FROM pg_subscription),
      'replication_slots', (SELECT count(*) FROM pg_replication_slots),
      'tablespaces', COALESCE((SELECT json_agg(json_build_object(
        'name', t.spcname, 'location', pg_tablespace_location(t.oid)) ORDER BY t.spcname)
        FROM pg_tablespace AS t), '[]'::json),
      'settings', COALESCE((SELECT json_agg(json_build_array(name,setting,source,context)
        ORDER BY name) FROM pg_settings), '[]'::json),
      'role_settings', COALESCE((SELECT json_agg(json_build_object(
        'database', COALESCE(d.datname,''), 'role', COALESCE(r.rolname,''),
        'settings', s.setconfig) ORDER BY d.datname NULLS FIRST, r.rolname NULLS FIRST)
        FROM pg_db_role_setting AS s
        LEFT JOIN pg_database AS d ON d.oid=s.setdatabase
        LEFT JOIN pg_roles AS r ON r.oid=s.setrole), '[]'::json)
    )::text;"""
    output = _run_bounded(context, connection_selector,
                          ["psql", "--no-psqlrc", "--no-password", "--quiet",
                           "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1",
                           f"--dbname={connection}", "--command=" + query], env=env)
    try:
        if not output or len(output) > _MAX_OUTPUT:
            raise ValueError()
        value = json.loads(output.decode("utf-8").strip())
    except (UnicodeError, ValueError, TypeError):
        raise PostgreSQLCoverageError("postgresql_sql_inventory_malformed") from None
    required = {"system_identifier", "current_database", "server_version_num", "data_directory", "config_file",
                "hba_file", "ident_file", "in_recovery", "databases", "roles", "memberships",
                "extensions", "languages", "foreign_tables", "foreign_servers", "subscriptions",
                "replication_slots", "tablespaces", "settings", "role_settings"}
    if not isinstance(value, dict) or set(value) != required:
        raise PostgreSQLCoverageError("postgresql_sql_inventory_malformed")
    if (not isinstance(value["system_identifier"], str)
            or not re.fullmatch(r"[0-9]{1,32}", value["system_identifier"])
            or not isinstance(value["current_database"], str)
            or not value["current_database"]
            or type(value["server_version_num"]) is not int
            or value["server_version_num"] < 100000
            or type(value["in_recovery"]) is not bool):
        raise PostgreSQLCoverageError("postgresql_sql_inventory_malformed")
    if any(not isinstance(value[name], str) or not value[name] for name in
           ("data_directory", "config_file", "hba_file", "ident_file")):
        raise PostgreSQLCoverageError("postgresql_sql_inventory_malformed")
    for name in ("databases", "roles", "memberships", "extensions", "languages",
                 "tablespaces", "settings", "role_settings"):
        if not isinstance(value[name], list):
            raise PostgreSQLCoverageError("postgresql_sql_inventory_malformed")
    if (len(value["databases"]) > _MAX_DATABASES or len(value["roles"]) > _MAX_ROLES
            or any(len(value[name]) > _MAX_CONFIG_ITEMS for name in
                   ("memberships", "extensions", "languages", "tablespaces", "settings", "role_settings"))):
        raise PostgreSQLCoverageError("postgresql_sql_inventory_limit")
    try:
        for role in value["roles"]:
            expected = {"name", "superuser", "inherit", "create_role", "create_database",
                        "can_login", "replication", "bypass_rls", "connection_limit", "valid_until", "password"}
            if (not isinstance(role, dict) or set(role) != expected
                    or not isinstance(role["name"], str) or not role["name"] or len(role["name"]) > 63
                    or any(type(role[key]) is not bool for key in
                           ("superuser", "inherit", "create_role", "create_database", "can_login",
                            "replication", "bypass_rls"))
                    or type(role["connection_limit"]) is not int
                    or (role["valid_until"] is not None and not isinstance(role["valid_until"], str))
                    or (role["password"] is not None and not isinstance(role["password"], str))):
                raise ValueError()
            # The native command output remains in memory. Only a digest of
            # each verifier reaches structural evidence or capture metadata.
            role["password"] = (hashlib.sha256(role["password"].encode()).hexdigest()
                                if role["password"] is not None else None)
        for membership in value["memberships"]:
            if (not isinstance(membership, dict)
                    or set(membership) != {"role", "member", "admin", "inherit", "set"}
                    or any(not isinstance(membership[key], str) or not membership[key]
                           for key in ("role", "member"))
                    or any(type(membership[key]) is not bool for key in ("admin", "inherit", "set"))):
                raise ValueError()
        for extension in value["extensions"]:
            if (not isinstance(extension, dict) or set(extension) != {"name", "version"}
                    or any(not isinstance(extension[key], str) or not extension[key]
                           for key in ("name", "version"))):
                raise ValueError()
        if any(not isinstance(language, str) or not language for language in value["languages"]):
            raise ValueError()
        for tablespace in value["tablespaces"]:
            if (not isinstance(tablespace, dict) or set(tablespace) != {"name", "location"}
                    or not isinstance(tablespace["name"], str) or not tablespace["name"]
                    or tablespace["location"] is not None and not isinstance(tablespace["location"], str)):
                raise ValueError()
        for setting in value["settings"]:
            if (not isinstance(setting, list) or len(setting) != 4
                    or not isinstance(setting[0], str) or not setting[0]
                    or not isinstance(setting[1], str)
                    or setting[2] is not None and not isinstance(setting[2], str)
                    or setting[3] is not None and not isinstance(setting[3], str)):
                raise ValueError()
        for setting in value["role_settings"]:
            if (not isinstance(setting, dict) or set(setting) != {"database", "role", "settings"}
                    or any(not isinstance(setting[key], str) for key in ("database", "role"))
                    or not isinstance(setting["settings"], list)
                    or any(not isinstance(item, str) for item in setting["settings"])):
                raise ValueError()
        for key in ("foreign_tables", "foreign_servers", "subscriptions", "replication_slots"):
            if type(value[key]) is not int or value[key] < 0:
                raise ValueError()
    except (TypeError, ValueError):
        raise PostgreSQLCoverageError("postgresql_sql_inventory_malformed") from None
    return value


def _database_inventory(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    databases = []
    try:
        for item in snapshot["databases"]:
            if (not isinstance(item, dict)
                    or set(item) != {"name", "oid", "allow_connections", "encoding", "collate",
                                     "ctype", "locale_provider", "owner", "acl", "connection_limit",
                                     "is_template"}
                    or not isinstance(item["name"], str) or not item["name"] or len(item["name"]) > 128
                    or type(item["oid"]) is not int or item["oid"] <= 0
                    or type(item["allow_connections"]) is not bool
                    or type(item["is_template"]) is not bool
                    or type(item["connection_limit"]) is not int
                    or not isinstance(item["owner"], str) or not item["owner"]
                    or (item["acl"] is not None and (not isinstance(item["acl"], list)
                        or any(not isinstance(entry, str) for entry in item["acl"])))
                    or any(not isinstance(item[key], str) or not item[key] for key in
                           ("encoding", "collate", "ctype", "locale_provider"))):
                raise ValueError()
            databases.append({key: item[key] for key in
                              ("name", "oid", "allow_connections", "encoding", "collate", "ctype",
                               "locale_provider", "owner", "acl", "connection_limit", "is_template")})
    except (TypeError, ValueError):
        raise PostgreSQLCoverageError("postgresql_sql_inventory_malformed") from None
    if len({item["name"] for item in databases}) != len(databases) or len({item["oid"] for item in databases}) != len(databases):
        raise PostgreSQLCoverageError("postgresql_sql_inventory_ambiguous")
    return sorted(databases, key=lambda item: item["name"])


def _verify_database_decisions(cluster: dict[str, Any], database_roots: list[dict[str, Any]],
                               databases: list[dict[str, Any]], context: PostgreSQLCoverageContext,
                               reasons: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    declarations = {item["name"]: item for item in cluster["databases"]}
    observed = {item["name"]: item for item in databases}
    if set(declarations) != set(observed):
        reasons.add("postgresql_database_accounting_changed")
    for name, decision in declarations.items():
        db = observed.get(name)
        if db is None or db["oid"] != decision["oid"]:
            reasons.add("postgresql_database_identity_changed")
    roots_by_id = {item["id"]: item for item in database_roots}
    selected: list[dict[str, Any]] = []
    for name, decision in declarations.items():
        db = observed.get(name)
        if decision["decision"] == "reconstruct":
            continue
        root = roots_by_id.get(decision["root_id"])
        if root is None or root["database"] != name:
            reasons.add("postgresql_database_selector_mismatch")
            continue
        source = context.sources.get(root["source_id"])
        binding = context.database_bindings.get(root["connection"])
        if (source is None or source.name != root["source_id"] or source.kind != "postgresql"
                or source.connection != root["connection"] or source.database != root["database"]
                or binding is None or binding.kind != "postgresql"
                or binding.service_name is None or binding.service_file is None):
            reasons.add("postgresql_database_selector_mismatch")
            continue
        if db is not None and not db["allow_connections"]:
            reasons.add("postgresql_database_not_connectable")
        selected.append({"root_id": root["id"], "source_id": root["source_id"],
                         "connection": root["connection"], "database": root["database"],
                         "cluster_id": root["cluster_id"], "database_oid": decision["oid"]})
    declared_capture_ids = {item["root_id"] for item in cluster["databases"]
                            if item["decision"] == "capture"}
    if declared_capture_ids != {item["id"] for item in database_roots}:
        reasons.add("postgresql_database_accounting_changed")
    return sorted(selected, key=lambda item: item["root_id"]), [
        {"path": str(Path(item["path"])), "sha256": item["sha256"]}
        for item in cluster["reconstruction"]["recovery_material"]]


def _verify_reconstruction(cluster: dict[str, Any], *, cluster_digest: str,
                            global_digest: str, role_digest: str, config_digest: str,
                            reasons: set[str], data_directory: Path,
                            physical_roots: list[dict[str, Any]],
                            exclusions: list[dict[str, Any]],
                            nonconnectable: list[dict[str, Any]]) -> list[dict[str, str]]:
    reconstruction = cluster["reconstruction"]
    recipe = reconstruction["recipe"]
    procedure_path = Path(reconstruction["procedure_path"])
    evidence_path = Path(reconstruction["evidence_path"])
    try:
        procedure_digest = _private_file_digest(procedure_path, 4 * 1024 * 1024)
        evidence_bytes = _private_bytes(evidence_path, 1024 * 1024)
        evidence = json.loads(evidence_bytes.decode("utf-8"))
    except (PostgreSQLCoverageError, UnicodeError, ValueError, TypeError):
        reasons.add("postgresql_reconstruction_evidence_unverified")
        evidence = None
        procedure_digest = ""
        evidence_bytes = b""
    material = []
    required_artifacts = [
        {"path": str(procedure_path), "sha256": procedure_digest},
        {"path": str(evidence_path), "sha256": hashlib.sha256(evidence_bytes).hexdigest()},
    ]
    for item in reconstruction["recovery_material"]:
        path = Path(item["path"])
        try:
            digest = _private_file_digest(path, _MAX_RECOVERY_MATERIAL)
        except PostgreSQLCoverageError:
            reasons.add("postgresql_recovery_material_unavailable")
            digest = ""
        if digest != item["sha256"]:
            reasons.add("postgresql_recovery_material_changed")
        material.append({"path": str(path), "sha256": digest})
    material.sort(key=lambda item: item["path"])
    required_artifacts.extend(material)
    excluded_paths = [Path(item["path"]) for item in exclusions]
    for item in required_artifacts:
        path = Path(item["path"])
        if (_within(path, data_directory) or not path.is_absolute()
                or not _no_symlink_ancestors(path)):
            reasons.add("postgresql_recovery_artifact_route_unsafe")
        matching_roots = [root for root in physical_roots
                          if root["adapter"] == "files"
                          and _within(path, Path(root["path"]))]
        if (len(matching_roots) != 1
                or any(_within(path, excluded) for excluded in excluded_paths)):
            reasons.add("postgresql_recovery_artifact_route_uncovered")
        else:
            item["root_id"] = matching_roots[0]["id"]
    if hashlib.sha256(evidence_bytes).hexdigest() != reconstruction["evidence_sha256"]:
        reasons.add("postgresql_reconstruction_evidence_unverified")
    checks = {"procedure_executed": True, "result_verified": True,
              "quiet_administrative_window": True, "schema_changes_frozen": True}
    expected_keys = {"schema_version", "recipe_id", "recipe_sha256", "environment",
                     "cluster_inventory_sha256", "global_inventory_sha256", "role_inventory_sha256",
                     "configuration_sha256", "recovery_material", "checks",
                     "nonconnectable_databases"}
    expected_nonconnectable = sorted(({
        "name": item["name"], "oid": item["oid"], "metadata_sha256": _digest(item),
        "checks": {"procedure_executed": True, "result_verified": True}}
        for item in nonconnectable), key=lambda item: item["name"])
    if (procedure_digest != recipe["recipe_sha256"] or not isinstance(evidence, dict)
            or set(evidence) != expected_keys or evidence.get("schema_version") != 1
            or evidence.get("recipe_id") != recipe["recipe_id"]
            or evidence.get("recipe_sha256") != recipe["recipe_sha256"]
            or evidence.get("environment") not in ("isolated-local-fixture", "reviewed-local-procedure")
            or evidence.get("cluster_inventory_sha256") != cluster_digest
            or evidence.get("global_inventory_sha256") != global_digest
            or evidence.get("role_inventory_sha256") != role_digest
            or evidence.get("configuration_sha256") != config_digest
            or evidence.get("nonconnectable_databases") != expected_nonconnectable
            or evidence.get("recovery_material") != [
                {"path": item["path"], "sha256": item["sha256"]} for item in material]
            or evidence.get("checks") != checks):
        reasons.add("postgresql_reconstruction_evidence_stale")
    return required_artifacts


def inspect_postgresql_coverage(database_roots: list[dict[str, Any]],
                                clusters: list[dict[str, Any]],
                                context: PostgreSQLCoverageContext | None,
                                physical_roots: list[dict[str, Any]] | None = None,
                                exclusions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Inspect the selected local primary cluster and return digest-only facts."""
    if not database_roots and not clusters:
        return {"reasons": [], "approved_postgresql": [],
                "capture_exclusions": [], "clusters": []}
    if context is None:
        return {"reasons": ["postgresql_inspection_context_missing"],
                "approved_postgresql": [], "capture_exclusions": [], "clusters": []}
    reasons: set[str] = set()
    if not context.quiet_administrative_window:
        reasons.add("postgresql_quiet_administrative_window_required")
    physical_roots = physical_roots or []
    exclusions = exclusions or []
    if len(clusters) != 1:
        reasons.add("postgresql_single_cluster_required")
    declared_sources = {root["source_id"] for root in database_roots}
    resolved_sources = {source.name for source in context.sources.values()
                        if getattr(source, "kind", None) == "postgresql"}
    if (declared_sources != resolved_sources
            or any(name != source.name for name, source in context.sources.items())):
        reasons.add("postgresql_database_selector_mismatch")
    approved: list[dict[str, Any]] = []
    capture_exclusions: list[str] = []
    observed_clusters: list[dict[str, Any]] = []
    roots_by_cluster: dict[str, list[dict[str, Any]]] = {}
    for root in database_roots:
        roots_by_cluster.setdefault(root["cluster_id"], []).append(root)
    for cluster in clusters:
        cluster_reasons_before = set(reasons)
        directory = Path(cluster["data_directory"])
        try:
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or not _no_symlink_ancestors(directory)
                    or info.st_uid == 0 or info.st_mode & 0o027):
                raise PostgreSQLCoverageError("postgresql_data_directory_unsafe")
        except PostgreSQLCoverageError as exc:
            reasons.add(exc.reason)
            continue
        except OSError:
            reasons.add("postgresql_data_directory_unavailable")
            continue
        pg_wal = directory / "pg_wal"
        pg_tblspc = directory / "pg_tblspc"
        try:
            wal_info = pg_wal.lstat()
            if (not stat.S_ISDIR(wal_info.st_mode) or stat.S_ISLNK(wal_info.st_mode)
                    or wal_info.st_dev != info.st_dev):
                reasons.add("postgresql_external_wal_unsupported")
            with os.scandir(pg_tblspc) as entries:
                if any(True for _ in entries):
                    reasons.add("postgresql_external_tablespace_unsupported")
        except OSError:
            reasons.add("postgresql_storage_layout_incomplete")
            continue
        cluster_roots = roots_by_cluster.get(cluster["id"], [])
        selected_sources = [context.sources.get(root["source_id"]) for root in cluster_roots]
        matching_sources = [source for source in selected_sources
                            if source is not None and source.connection == cluster["connection"]
                            and source.kind == "postgresql"]
        if not matching_sources:
            reasons.add("postgresql_database_selector_mismatch")
            continue
        # All selected databases in one cluster share a private connection file.
        source = matching_sources[0]
        binding = context.database_bindings.get(cluster["connection"])
        if binding is None or binding.kind != "postgresql" or binding.service_file is None:
            reasons.add("postgresql_binding_missing")
            continue
        if any(candidate is None or candidate.connection != cluster["connection"]
               or candidate.kind != "postgresql" for candidate in selected_sources):
            reasons.add("postgresql_database_selector_mismatch")
        try:
            connection_digest, _service = _validate_connection_file(
                binding.service_file, binding.service_name or "")
            password_digest = (_private_file_digest(binding.password_file, _MAX_CONNECTION_FILE)
                               if binding.password_file is not None else hashlib.sha256(b"no-password-file").hexdigest())
            env = _environment(binding)
            snapshot = _parse_sql_snapshot(context, source.database,
                                           cluster["connection"], binding, env)
            system_id, local_major, data_major = _parse_local_control(context, cluster, env)
        except PostgreSQLCoverageError as exc:
            reasons.add(exc.reason)
            continue
        try:
            databases = _database_inventory(snapshot)
        except PostgreSQLCoverageError as exc:
            reasons.add(exc.reason)
            continue
        snapshots = {source.database: snapshot}
        try:
            for db in databases:
                if db["allow_connections"] and db["name"] not in snapshots:
                    snapshots[db["name"]] = _parse_sql_snapshot(
                        context, db["name"], cluster["connection"], binding, env)
        except PostgreSQLCoverageError as exc:
            reasons.add(exc.reason)
            continue
        nonconnectable = [db for db in databases if not db["allow_connections"]]
        declared_reconstruct = {item["name"] for item in cluster["databases"]
                                if item["decision"] == "reconstruct"}
        if any(db["name"] not in declared_reconstruct for db in nonconnectable):
            reasons.add("postgresql_database_not_connectable")
        for database_name, database_snapshot in snapshots.items():
            if database_snapshot["current_database"] != database_name:
                reasons.add("postgresql_database_selector_mismatch")
            if any(database_snapshot[key] != snapshot[key] for key in
                   ("system_identifier", "server_version_num", "data_directory", "config_file",
                    "hba_file", "ident_file", "in_recovery", "databases", "roles", "memberships",
                    "tablespaces", "settings", "role_settings", "replication_slots")):
                reasons.add("postgresql_cluster_inventory_inconsistent")
            if database_snapshot["extensions"]:
                names = {item["name"] for item in database_snapshot["extensions"]}
                if names - {"plpgsql"}:
                    reasons.add("postgresql_extensions_unsupported")
            if database_snapshot["languages"]:
                reasons.add("postgresql_languages_unsupported")
            for key, reason in (("foreign_tables", "postgresql_foreign_tables_unsupported"),
                                ("foreign_servers", "postgresql_foreign_servers_unsupported"),
                                ("subscriptions", "postgresql_subscriptions_unsupported")):
                if database_snapshot[key]:
                    reasons.add(reason)
        sql_system_id = snapshot["system_identifier"]
        sql_major = str(snapshot["server_version_num"] // 10000)
        if system_id != sql_system_id:
            reasons.add("postgresql_sql_local_identity_mismatch")
        if local_major != sql_major or data_major != sql_major:
            reasons.add("postgresql_major_version_mismatch")
        if snapshot["in_recovery"]:
            reasons.add("postgresql_standby_unsupported")
        if str(Path(os.path.normpath(snapshot["data_directory"]))) != str(directory):
            reasons.add("postgresql_data_directory_namespace_mismatch")
        if any(item["locale_provider"] != "c" for item in databases):
            reasons.add("postgresql_non_libc_locale_unsupported")
        for name in ("config_file", "hba_file", "ident_file"):
            value = snapshot[name]
            if not isinstance(value, str) or not value:
                reasons.add("postgresql_external_configuration")
                continue
            path = Path(os.path.normpath(value))
            if not path.is_absolute() or not _within(path, directory):
                reasons.add("postgresql_external_configuration")
        try:
            config_digest = _read_cluster_config(directory, [snapshot["config_file"],
                snapshot["hba_file"], snapshot["ident_file"]],
                {Path(os.path.normpath(snapshot["hba_file"])),
                 Path(os.path.normpath(snapshot["ident_file"]))})
        except PostgreSQLCoverageError as exc:
            reasons.add(exc.reason)
            continue
        settings = {item[0]: item[1] for item in snapshot["settings"]}
        for name, allowed in {"ssl": {"off"}, "archive_mode": {"off"},
                              "shared_preload_libraries": {""},
                              "session_preload_libraries": {""},
                              "local_preload_libraries": {""},
                              "restore_command": {""},
                              # PostgreSQL reports this inactive default while
                              # archive_mode is off, including in version 17.
                              "archive_command": ({"", "(disabled)"}
                                  if settings.get("archive_mode") == "off" else {""}),
                              "primary_conninfo": {""},
                              "primary_slot_name": {""}}.items():
            if name not in settings or settings[name] not in allowed:
                reasons.add("postgresql_configuration_dependencies_unsupported")
        for key in ("replication_slots",):
            if type(snapshot[key]) is not int or snapshot[key] < 0:
                reasons.add("postgresql_sql_inventory_malformed")
            elif snapshot[key]:
                reasons.add("postgresql_replication_slots_unsupported")
        external_tablespaces = [item for item in snapshot["tablespaces"]
                                if not isinstance(item, dict) or item.get("name") not in ("pg_default", "pg_global")
                                or item.get("location") not in ("", None)]
        if external_tablespaces:
            reasons.add("postgresql_external_tablespace_unsupported")
        role_data = {"roles": snapshot["roles"], "memberships": snapshot["memberships"]}
        role_digest = _digest(role_data)
        config_runtime_digest = _digest({"settings": snapshot["settings"],
                                         "role_settings": snapshot["role_settings"],
                                         "files": config_digest})
        database_digests = {name: _digest({"extensions": item["extensions"],
                             "languages": item["languages"],
                             "foreign_tables": item["foreign_tables"],
                             "foreign_servers": item["foreign_servers"],
                             "subscriptions": item["subscriptions"]})
                            for name, item in snapshots.items()}
        cluster_digest = _digest({"databases": databases,
                                  "database_inventories": database_digests,
                                  "tablespaces": snapshot["tablespaces"],
                                  "replication_slots": snapshot["replication_slots"]})
        global_digest = _digest({"roles": role_digest, "configuration": config_runtime_digest})
        selected, _ = _verify_database_decisions(cluster,
            roots_by_cluster.get(cluster["id"], []), databases, context, reasons)
        artifacts = _verify_reconstruction(cluster, cluster_digest=cluster_digest,
            global_digest=global_digest, role_digest=role_digest,
            config_digest=config_runtime_digest, reasons=reasons,
            data_directory=directory, physical_roots=physical_roots, exclusions=exclusions,
            nonconnectable=nonconnectable)
        for item in artifacts:
            try:
                if _private_file_digest(Path(item["path"]), _MAX_RECOVERY_MATERIAL) != item["sha256"]:
                    reasons.add("postgresql_recovery_material_changed_during_inspection")
            except PostgreSQLCoverageError:
                reasons.add("postgresql_recovery_material_unavailable")
        try:
            connection_digest_after, _ = _validate_connection_file(
                binding.service_file, binding.service_name or "")
            password_digest_after = (_private_file_digest(binding.password_file, _MAX_CONNECTION_FILE)
                                     if binding.password_file is not None
                                     else hashlib.sha256(b"no-password-file").hexdigest())
            final_info = directory.lstat()
            original_identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
                                 stat.S_IMODE(info.st_mode))
            final_identity = (final_info.st_dev, final_info.st_ino, final_info.st_uid,
                              final_info.st_gid, stat.S_IMODE(final_info.st_mode))
            if original_identity != final_identity:
                reasons.add("postgresql_data_directory_changed_during_inspection")
            if (connection_digest != connection_digest_after
                    or password_digest != password_digest_after):
                reasons.add("postgresql_connection_material_changed_during_inspection")
        except PostgreSQLCoverageError as exc:
            reasons.add(exc.reason)
        if (len(clusters) == 1 and system_id == sql_system_id and local_major == sql_major
                and data_major == sql_major
                and reasons == cluster_reasons_before):
            capture_exclusions.append(str(directory))
        observed_clusters.append({"id": cluster["id"], "connection": cluster["connection"],
            "data_directory": str(directory),
            "data_directory_identity": {"device": info.st_dev, "inode": info.st_ino,
                                        "uid": info.st_uid, "gid": info.st_gid,
                                        "mode": stat.S_IMODE(info.st_mode)},
            "system_identifier_sha256": hashlib.sha256(system_id.encode("ascii")).hexdigest(),
            "major_version": sql_major, "cluster_inventory_sha256": cluster_digest,
            "global_inventory_sha256": global_digest,
            "role_inventory_sha256": role_digest,
            "configuration_sha256": config_runtime_digest,
            "connection_material_sha256": _digest({"service_file": connection_digest,
                                                     "password_file": password_digest}),
            "recipe_sha256": cluster["reconstruction"]["recipe"]["recipe_sha256"],
            "recovery_artifacts": artifacts, "databases": databases,
            "database_inventory_sha256": database_digests})
        approved.extend({**item, "system_identifier_sha256": hashlib.sha256(system_id.encode("ascii")).hexdigest(),
                         "data_directory": str(directory), "major_version": sql_major,
                         "data_directory_identity": {"device": info.st_dev, "inode": info.st_ino,
                            "uid": info.st_uid, "gid": info.st_gid,
                            "mode": stat.S_IMODE(info.st_mode)},
                         "cluster_inventory_sha256": cluster_digest,
                         "global_inventory_sha256": global_digest,
                         "role_inventory_sha256": role_digest,
                         "configuration_sha256": config_runtime_digest,
                         "connection_material_sha256": _digest({"service_file": connection_digest,
                                                                 "password_file": password_digest}),
                         "database_inventory_sha256": database_digests[item["database"]],
                         "recipe_sha256": cluster["reconstruction"]["recipe"]["recipe_sha256"],
                         "recovery_artifacts": artifacts}
                        for item in selected)
    reasons.update({"postgresql_context_cancelled"} if context.cancel is not None
                   and getattr(context.cancel, "is_set", lambda: False)() else set())
    return {"reasons": sorted(reasons), "approved_postgresql": approved,
            "capture_exclusions": sorted(set(capture_exclusions)),
            "clusters": observed_clusters}


__all__ = ["PostgreSQLCoverageContext", "PostgreSQLCoverageError",
           "inspect_postgresql_coverage"]
