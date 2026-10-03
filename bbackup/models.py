"""Strict version-two portable configuration. Secrets remain in host bindings."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any


class ConfigurationError(ValueError):
    """Configuration does not satisfy the version-two contract."""


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
        if role not in ("local", "replica"):
            raise ConfigurationError("Repository role must be local or replica")
        return cls(_name(raw["name"]), role)


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    paths: tuple[str, ...]
    database: str | None = None
    connection: str | None = None
    quiesce: str | None = None

    @classmethod
    def parse(cls, raw):
        raw = _object(
            raw,
            {"name", "kind", "paths", "database", "connection", "quiesce"},
            {"name", "kind"},
        )
        kind = raw["kind"]
        if kind not in ("files", "sqlite", "postgresql", "mysql", "mariadb"):
            raise ConfigurationError(
                "Source kind is not implemented; capture is refused"
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
                raise ConfigurationError(
                    "MySQL and MariaDB sources require quiesce='read-lock'"
                )
        if set(raw) != expected_database_fields:
            raise ConfigurationError(
                "Database sources require a database name and private connection reference"
            )
        return cls(
            _name(raw["name"]),
            kind,
            (),
            _name(raw["database"]),
            _name(raw["connection"]),
            raw.get("quiesce"),
        )


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
    retention: Retention = field(default_factory=Retention)

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
        return cls(
            _name(raw["name"]),
            _name(raw["repository"]),
            sources,
            replicas,
            schedule,
            _positive(raw.get("overdue_hours", 26)),
            Retention.parse(raw.get("retention", {})),
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
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
            raise ConfigurationError("Expected schema_version 2")
        groups = []
        for key, model in (
            ("repositories", Repository),
            ("sources", Source),
            ("jobs", Job),
        ):
            if not isinstance(raw[key], list):
                raise ConfigurationError("Configuration collections must be arrays")
            entries = tuple(model.parse(item) for item in raw[key])
            if len({item.name for item in entries}) != len(entries):
                raise ConfigurationError("Duplicate names in configuration collection")
            groups.append(entries)
        config = cls(*groups)
        repositories = {repo.name: repo for repo in config.repositories}
        sources = {source.name for source in config.sources}
        for job in config.jobs:
            if (
                job.repository not in repositories
                or repositories[job.repository].role != "local"
            ):
                raise ConfigurationError("Jobs require a declared local repository")
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


def _absolute_file(value: Any, description: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute() or "\0" in value:
        raise ConfigurationError(f"{description} must be an absolute path")
    return Path(value)


@dataclass(frozen=True)
class HostBindings:
    state_dir: Path
    repositories: dict[str, RepositoryBinding]
    database_bindings: dict[str, DatabaseBinding] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path):
        if path.stat().st_mode & 0o077:
            raise ConfigurationError(
                "Host bindings must not be accessible to group or other users"
            )
        if path.stat().st_size > 1024 * 1024:
            raise ConfigurationError("Host bindings exceed 1 MiB")
        raw = _object(
            strict_json(path.read_text(encoding="utf-8")),
            {"schema_version", "state_dir", "repositories", "database_bindings"},
            {"schema_version", "state_dir", "repositories"},
        )
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
            raise ConfigurationError("Expected host binding schema_version 2")
        state = raw["state_dir"]
        if not isinstance(state, str) or not Path(state).is_absolute() or "\0" in state:
            raise ConfigurationError("State directory must be an absolute path")
        if not isinstance(raw["repositories"], dict):
            raise ConfigurationError("Repository bindings must be an object")
        repositories = {
            _name(key): RepositoryBinding.parse(value)
            for key, value in raw["repositories"].items()
        }
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
        return cls(Path(state), repositories, database_bindings)

    @property
    def sensitive_paths(self) -> tuple[Path, ...]:
        """Private files a service must exclude from captured source trees."""

        paths = [binding.password_file for binding in self.repositories.values()]
        for binding in self.database_bindings.values():
            paths.extend(binding.sensitive_paths)
        return tuple(path for path in paths if path is not None)
