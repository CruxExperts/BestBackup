"""Strict portable policy and private host bindings."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any


class ConfigurationError(ValueError):
    """Configuration does not satisfy the declared contract."""


def _object(value: Any, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ConfigurationError("Expected an object")
    if set(value) - allowed:
        raise ConfigurationError("Unknown configuration fields")
    if required - set(value):
        raise ConfigurationError("Missing required configuration fields")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", value
    ):
        raise ConfigurationError(
            "Names must contain 1–64 letters, digits, dots, underscores or hyphens"
        )
    return value


def _positive(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ConfigurationError("Expected a positive integer")
    return value


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v or "\0" in v for v in value
    ):
        raise ConfigurationError("Expected an array of nonempty strings")
    return tuple(value)


def strict_json(text: str) -> Any:
    """Reject duplicate keys and non-JSON numeric constants at every level."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ConfigurationError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid(_):
        raise ConfigurationError("Non-finite JSON numbers are forbidden")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, RecursionError) as exc:
        raise ConfigurationError("Invalid strict JSON") from exc


@dataclass(frozen=True)
class Repository:
    name: str
    role: str = "local"

    @classmethod
    def parse(cls, raw):
        raw = _object(raw, {"name", "role"}, {"name"})
        role = raw.get("role", "local")
        if role not in ("local", "replica", "remote-primary"):
            raise ConfigurationError("Repository role must be local, remote-primary, or replica")
        return cls(_name(raw["name"]), role)


@dataclass(frozen=True)
class ResourceBudget:
    """Portable remote-primary staging ceiling and observed free-space reserve."""

    staging_max_bytes: int
    minimum_free_bytes: int
    cache_max_bytes: int = 0
    schema_version: int = 1

    @classmethod
    def parse(cls, raw):
        raw = _object(raw, set(cls.__dataclass_fields__),
                      {"schema_version", "staging_max_bytes", "minimum_free_bytes", "cache_max_bytes"})
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ConfigurationError("Unsupported resource budget version")
        ceiling = _positive(raw["staging_max_bytes"])
        reserve = _positive(raw["minimum_free_bytes"])
        if type(raw["cache_max_bytes"]) is not int or raw["cache_max_bytes"] != 0:
            raise ConfigurationError("Remote-primary cache must be disabled")
        if reserve >= ceiling:
            raise ConfigurationError("Resource reserve must be below staging capacity")
        return cls(ceiling, reserve)


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    paths: tuple[str, ...] = ()
    database: str | None = None
    connection: str | None = None
    quiesce: str | None = None
    container: str | None = None
    stop_policy: str | None = None

    @classmethod
    def parse(cls, raw):
        raw = _object(
            raw,
            {"name", "kind", "paths", "database", "connection", "quiesce", "container", "stop_policy"},
            {"name", "kind"},
        )
        kind = raw["kind"]
        if kind not in ("files", "sqlite", "postgresql", "mysql", "mariadb", "docker"):
            raise ConfigurationError("Source kind is not implemented; capture is refused")
        if kind == "docker":
            if set(raw) != {"name", "kind", "container", "connection", "stop_policy"}:
                raise ConfigurationError(
                    "Docker sources require one container, private connection, and explicit stop policy"
                )
            container = raw["container"]
            if not isinstance(container, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container
            ):
                raise ConfigurationError("Docker sources require one exact container name")
            if raw["stop_policy"] != "stop-restart":
                raise ConfigurationError("Docker sources require stop_policy='stop-restart'")
            return cls(
                _name(raw["name"]), kind, (), connection=_name(raw["connection"]),
                container=container, stop_policy="stop-restart",
            )
        if kind in ("files", "sqlite"):
            if set(raw) != {"name", "kind", "paths"}:
                raise ConfigurationError("File and SQLite sources require only paths")
            paths = _strings(raw["paths"])
            if not paths or any(not Path(path).is_absolute() for path in paths):
                raise ConfigurationError("Source paths must be nonempty absolute paths")
            if kind == "sqlite" and len(paths) != 1:
                raise ConfigurationError("SQLite sources require exactly one database")
            return cls(_name(raw["name"]), kind, paths)
        expected_database_fields = {"name", "kind", "database", "connection"}
        if kind in ("mysql", "mariadb"):
            expected_database_fields.add("quiesce")
            if raw.get("quiesce") != "read-lock":
                raise ConfigurationError("MySQL and MariaDB sources require quiesce='read-lock'")
        if set(raw) != expected_database_fields:
            raise ConfigurationError("Database sources require a database name and private connection reference")
        return cls(
            _name(raw["name"]), kind, (), _name(raw["database"]),
            _name(raw["connection"]), raw.get("quiesce"),
        )

    @classmethod
    def parse_policy(cls, raw):
        """Parse a portable source without accepting any host selector."""
        raw = _object(raw, {"name", "kind", "quiesce", "stop_policy"}, {"name", "kind"})
        kind = raw["kind"]
        if kind not in ("files", "sqlite", "postgresql", "mysql", "mariadb", "docker"):
            raise ConfigurationError("Source kind is not implemented; capture is refused")
        expected = {"name", "kind"}
        if kind in ("mysql", "mariadb"):
            expected.add("quiesce")
            if raw.get("quiesce") != "read-lock":
                raise ConfigurationError("MySQL and MariaDB sources require quiesce='read-lock'")
        if kind == "docker":
            expected.add("stop_policy")
            if raw.get("stop_policy") != "stop-restart":
                raise ConfigurationError("Docker sources require stop_policy='stop-restart'")
        if set(raw) != expected:
            raise ConfigurationError("Portable sources may contain policy, not host selectors")
        return cls(_name(raw["name"]), kind, quiesce=raw.get("quiesce"),
                   stop_policy=raw.get("stop_policy"))


@dataclass(frozen=True)
class Retention:
    local_daily: int = 7
    cloud_daily: int = 30
    cloud_weekly: int = 8
    cloud_monthly: int = 12
    protection_days: int = 30

    @classmethod
    def parse(cls, raw):
        fields = set(cls.__dataclass_fields__)
        raw = _object(raw, fields, set())
        values = {key: _positive(value) for key, value in raw.items()}
        if values.get("protection_days", 30) < 30:
            raise ConfigurationError(
                "Cloud recovery protection must be at least thirty days"
            )
        return cls(**values)


@dataclass(frozen=True)
class Job:
    name: str
    repository: str
    sources: tuple[str, ...]
    replicas: tuple[str, ...] = ()
    schedule: str = "daily"
    overdue_hours: int = 26
    timeout_seconds: int = 3600
    retention: Retention = field(default_factory=Retention)
    resource_budget: ResourceBudget | None = None
    coverage_policy: dict | None = None

    @classmethod
    def parse(cls, raw):
        raw = _object(
            raw, set(cls.__dataclass_fields__), {"name", "repository", "sources"}
        )
        sources = tuple(_name(v) for v in _strings(raw["sources"]))
        replicas = tuple(_name(v) for v in _strings(raw.get("replicas", [])))
        if (
            not sources
            or len(set(sources)) != len(sources)
            or len(set(replicas)) != len(replicas)
        ):
            raise ConfigurationError("Job references must be nonempty and unique")
        schedule = raw.get("schedule", "daily")
        if (
            not isinstance(schedule, str)
            or not schedule
            or any(c in schedule for c in "\n\r\0")
        ):
            raise ConfigurationError(
                "Schedule must be a single nonempty systemd calendar expression"
            )
        budget = ResourceBudget.parse(raw["resource_budget"]) if "resource_budget" in raw else None
        coverage_policy = None
        if "coverage_policy" in raw:
            from .linux_coverage import validate_scope_policy
            try:
                coverage_policy = validate_scope_policy(raw["coverage_policy"])
            except (ValueError, RuntimeError) as error:
                raise ConfigurationError(str(error)) from None
        return cls(
            _name(raw["name"]),
            _name(raw["repository"]),
            sources,
            replicas,
            schedule,
            _positive(raw.get("overdue_hours", 26)),
            _positive(raw.get("timeout_seconds", 3600)),
            Retention.parse(raw.get("retention", {})),
            budget,
            coverage_policy,
        )


@dataclass(frozen=True)
class Configuration:
    repositories: tuple[Repository, ...]
    sources: tuple[Source, ...]
    jobs: tuple[Job, ...]
    schema_version: int = 2

    @classmethod
    def parse(cls, raw):
        raw = _object(
            raw,
            {"schema_version", "repositories", "sources", "jobs"},
            {"schema_version", "repositories", "sources", "jobs"},
        )
        version = raw["schema_version"]
        if type(version) is not int or version not in (2, 3):
            raise ConfigurationError("Expected schema_version 2 or 3")
        groups = []
        for key, model in (
            ("repositories", Repository),
            ("sources", Source),
            ("jobs", Job),
        ):
            if not isinstance(raw[key], list):
                raise ConfigurationError("Configuration collections must be arrays")
            parser = Source.parse_policy if key == "sources" and version == 3 else model.parse
            entries = tuple(parser(item) for item in raw[key])
            if len({item.name for item in entries}) != len(entries):
                raise ConfigurationError("Duplicate names in configuration collection")
            groups.append(entries)
        config = cls(*groups, schema_version=version)
        repositories = {repo.name: repo for repo in config.repositories}
        sources = {source.name for source in config.sources}
        for job in config.jobs:
            if job.coverage_policy is not None and version != 3:
                raise ConfigurationError("Coverage scope requires portable policy version 3")
            if (job.repository not in repositories
                    or repositories[job.repository].role not in ("local", "remote-primary")):
                raise ConfigurationError("Jobs require a declared primary repository")
            if (repositories[job.repository].role == "remote-primary") != (job.resource_budget is not None):
                raise ConfigurationError("Remote-primary jobs require a resource budget; local jobs cannot use one")
            if repositories[job.repository].role == "remote-primary" and job.replicas:
                raise ConfigurationError("Remote-primary jobs cannot dispatch unbudgeted replica copies")
            if not set(job.sources) <= sources:
                raise ConfigurationError("Job references an undeclared source")
            if any(
                name not in repositories or repositories[name].role != "replica"
                for name in job.replicas
            ):
                raise ConfigurationError(
                    "Job replicas require declared replica repositories"
                )
        return config

    @classmethod
    def load(cls, path: Path):
        if path.stat().st_size > 1024 * 1024:
            raise ConfigurationError("Configuration exceeds 1 MiB")
        return cls.parse(strict_json(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class RepositoryBinding:
    """Host-private locations; credentials are references, never inline values."""

    repository: str
    password_file: Path

    @classmethod
    def parse(cls, raw):
        raw = _object(
            raw, {"repository", "password_file"}, {"repository", "password_file"}
        )
        location = raw["repository"]
        password = raw["password_file"]
        if (
            not isinstance(location, str)
            or not location
            or any(c in location for c in "\0\r\n")
        ):
            raise ConfigurationError("Invalid repository binding")
        if (
            not isinstance(password, str)
            or not Path(password).is_absolute()
            or "\0" in password
        ):
            raise ConfigurationError("Password file must be an absolute path")
        # URL credentials belong in the service environment, never in repository identifiers.
        if "://" in location and "@" in location.split("://", 1)[1].split("/", 1)[0]:
            raise ConfigurationError("Inline repository credentials are forbidden")
        return cls(location, Path(password))


@dataclass(frozen=True)
class DatabaseBinding:
    """Private native-client connection and credential file references."""

    kind: str
    service_file: Path | None = None
    service_name: str | None = None
    password_file: Path | None = None
    options_file: Path | None = None

    @classmethod
    def parse(cls, raw):
        raw = _object(
            raw,
            {"kind", "service_file", "service_name", "password_file", "options_file"},
            {"kind"},
        )
        kind = raw["kind"]
        if kind == "postgresql":
            if set(raw) not in (
                {"kind", "service_file", "service_name"},
                {"kind", "service_file", "service_name", "password_file"},
            ):
                raise ConfigurationError(
                    "PostgreSQL bindings require a service file and service name"
                )
            service_file = _absolute_file(
                raw["service_file"], "PostgreSQL service file"
            )
            service_name = _name(raw["service_name"])
            password_file = (
                _absolute_file(raw["password_file"], "PostgreSQL password file")
                if "password_file" in raw
                else None
            )
            return cls("postgresql", service_file, service_name, password_file)
        if kind in ("mysql", "mariadb"):
            if set(raw) != {"kind", "options_file"}:
                raise ConfigurationError(
                    "MySQL and MariaDB bindings require one private options file"
                )
            options_file = _absolute_file(raw["options_file"], "Database options file")
            return cls(kind, options_file=options_file)
        raise ConfigurationError("Unsupported database binding kind")
    @property
    def sensitive_paths(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in (self.service_file, self.password_file, self.options_file)
            if path is not None
        )


@dataclass(frozen=True)
class DockerBinding:
    """Private local Docker Unix-socket reference."""

    kind: str
    socket: Path

    @classmethod
    def parse(cls, raw):
        raw = _object(raw, {"kind", "socket"}, {"kind", "socket"})
        if raw["kind"] != "docker":
            raise ConfigurationError("Docker bindings require kind='docker'")
        return cls("docker", _absolute_file(raw["socket"], "Docker socket"))

    @property
    def sensitive_paths(self) -> tuple[Path, ...]:
        return (self.socket,)



def _absolute_file(value: Any, description: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute() or "\0" in value:
        raise ConfigurationError(f"{description} must be an absolute path")
    return Path(value)


@dataclass(frozen=True)
class SourceBinding:
    """Concrete capture selector owned by one private host."""

    paths: tuple[str, ...] = ()
    database: str | None = None
    connection: str | None = None
    container: str | None = None

    @classmethod
    def parse(cls, raw, policy: Source):
        kind = policy.kind
        if kind in ("files", "sqlite"):
            raw = _object(raw, {"paths"}, {"paths"})
            paths = _strings(raw["paths"])
            if not paths or any(not Path(path).is_absolute() for path in paths):
                raise ConfigurationError("Source paths must be nonempty absolute paths")
            if kind == "sqlite" and len(paths) != 1:
                raise ConfigurationError("SQLite sources require exactly one database")
            return cls(paths=paths)
        if kind == "docker":
            raw = _object(raw, {"container", "connection"}, {"container", "connection"})
            container = raw["container"]
            if not isinstance(container, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container
            ):
                raise ConfigurationError("Docker sources require one exact container name")
            return cls(connection=_name(raw["connection"]), container=container)
        raw = _object(raw, {"database", "connection"}, {"database", "connection"})
        return cls(database=_name(raw["database"]), connection=_name(raw["connection"]))

    def resolve(self, policy: Source) -> Source:
        return Source(policy.name, policy.kind, self.paths, self.database,
                      self.connection, policy.quiesce, self.container,
                      policy.stop_policy)


@dataclass(frozen=True)
class HostBindings:
    state_dir: Path
    repositories: dict[str, RepositoryBinding]
    database_bindings: dict[str, DatabaseBinding] = field(default_factory=dict)
    docker_bindings: dict[str, DockerBinding] = field(default_factory=dict)
    source_bindings: dict[str, SourceBinding] = field(default_factory=dict)
    schema_version: int = 2
    staging_dir: Path | None = None
    coverage_declarations: dict[str, Path] = field(default_factory=dict)
    @classmethod
    def load(cls, path: Path, configuration: Configuration | None = None):
        if path.stat().st_mode & 0o077:
            raise ConfigurationError(
                "Host bindings must not be accessible to group or other users"
            )
        if path.stat().st_size > 1024 * 1024:
            raise ConfigurationError("Host bindings exceed 1 MiB")
        return cls.parse(strict_json(path.read_text(encoding="utf-8")), configuration)

    @classmethod
    def parse(cls, raw, configuration: Configuration | None = None):
        raw = _object(
            raw,
            {"schema_version", "state_dir", "repositories", "database_bindings", "docker_bindings", "source_bindings", "staging_dir", "coverage_declarations"},
            {"schema_version", "state_dir", "repositories"},
        )
        version = raw["schema_version"]
        if type(version) is not int or version not in (2, 3):
            raise ConfigurationError("Expected host binding schema_version 2 or 3")
        if configuration is not None and version != configuration.schema_version:
            raise ConfigurationError("Policy and bindings schema versions must match")
        if version == 2 and "source_bindings" in raw:
            raise ConfigurationError("Version 2 bindings cannot contain source_bindings")
        if version == 3 and "source_bindings" not in raw:
            raise ConfigurationError("Version 3 bindings require source_bindings")
        state = raw["state_dir"]
        if not isinstance(state, str) or not Path(state).is_absolute() or "\0" in state:
            raise ConfigurationError("State directory must be an absolute path")
        if not isinstance(raw["repositories"], dict):
            raise ConfigurationError("Repository bindings must be an object")
        repositories = {
            _name(key): RepositoryBinding.parse(value)
            for key, value in raw["repositories"].items()
        }
        staging_dir = None
        if "staging_dir" in raw:
            location = raw["staging_dir"]
            if not isinstance(location, str) or not Path(location).is_absolute() or "\0" in location:
                raise ConfigurationError("Staging directory must be an absolute private path")
            staging_dir = Path(location)
        if configuration is not None:
            needs_staging = any(r.role == "remote-primary" for r in configuration.repositories)
            if needs_staging != (staging_dir is not None):
                raise ConfigurationError("Remote-primary bindings require a private staging directory")
        raw_coverage = raw.get("coverage_declarations", {})
        if version != 3 and raw_coverage:
            raise ConfigurationError("Coverage declarations require version 3 bindings")
        if not isinstance(raw_coverage, dict):
            raise ConfigurationError("Coverage declarations must be a private job mapping")
        coverage_declarations = {}
        for name, location in raw_coverage.items():
            name = _name(name)
            if (not isinstance(location, str) or not Path(location).is_absolute()
                    or ".." in Path(location).parts or any(c in location for c in "\0\r\n")
                    or str(Path(location)) != location):
                raise ConfigurationError("Coverage declaration must be an absolute private path")
            coverage_declarations[name] = Path(location)
        if configuration is not None:
            covered = {job.name for job in configuration.jobs if job.coverage_policy is not None}
            if set(coverage_declarations) != covered:
                raise ConfigurationError("Coverage declarations must match opt-in jobs")
        locations = [value.repository for value in repositories.values()]
        if len(set(locations)) != len(locations):
            raise ConfigurationError("Multiple names for one repository are forbidden")
        raw_database_bindings = raw.get("database_bindings", {})
        if not isinstance(raw_database_bindings, dict):
            raise ConfigurationError("Database bindings must be an object")
        database_bindings = {
            _name(key): DatabaseBinding.parse(value)
            for key, value in raw_database_bindings.items()
        }
        raw_docker_bindings = raw.get("docker_bindings", {})
        if not isinstance(raw_docker_bindings, dict):
            raise ConfigurationError("Docker bindings must be an object")
        docker_bindings = {
            _name(key): DockerBinding.parse(value)
            for key, value in raw_docker_bindings.items()
        }
        source_bindings = {}
        if version == 3:
            if configuration is None:
                raise ConfigurationError("Version 3 bindings require the matching portable policy")
            raw_sources = raw["source_bindings"]
            if not isinstance(raw_sources, dict) or set(raw_sources) != {s.name for s in configuration.sources}:
                raise ConfigurationError("Private source bindings must match declared sources")
            source_bindings = {
                source.name: SourceBinding.parse(raw_sources[source.name], source)
                for source in configuration.sources
            }
        return cls(Path(state), repositories, database_bindings, docker_bindings,
                   source_bindings, version, staging_dir, coverage_declarations)

    @property
    def sensitive_paths(self) -> tuple[Path, ...]:
        """Private files a service must exclude from captured source trees."""

        paths = [binding.password_file for binding in self.repositories.values()]
        for binding in self.database_bindings.values():
            paths.extend(binding.sensitive_paths)
        for binding in self.docker_bindings.values():
            paths.extend(binding.sensitive_paths)
        return tuple(path for path in paths if path is not None)
