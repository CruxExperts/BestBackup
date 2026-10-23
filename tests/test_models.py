"""Version-two configuration rejects ambiguous and unsupported capture policy."""

import copy

import pytest

from bbackup.models import Configuration, ConfigurationError, strict_json


@pytest.fixture
def config():
    return {
        "schema_version": 2,
        "repositories": [{"name": "local"}, {"name": "cloud", "role": "replica"}],
        "sources": [{"name": "files", "kind": "files", "paths": ["/srv/data"]}],
        "jobs": [
            {
                "name": "daily",
                "repository": "local",
                "sources": ["files"],
                "replicas": ["cloud"],
            }
        ],
    }


def test_defaults(config):
    parsed = Configuration.parse(config)
    assert parsed.jobs[0].retention.local_daily == 7
    assert parsed.jobs[0].retention.local_last == 0
    assert parsed.jobs[0].retention.local_weekly == 0
    assert parsed.jobs[0].retention.cloud_monthly == 12
    assert parsed.jobs[0].overdue_hours == 26
    assert parsed.jobs[0].schedule == "daily"


@pytest.mark.parametrize("field", ["local_last", "local_weekly"])
def test_local_retention_last_and_weekly_are_nonnegative_integers(config, field):
    config["jobs"][0]["retention"] = {field: 3}
    parsed = Configuration.parse(config)
    assert getattr(parsed.jobs[0].retention, field) == 3
    for invalid in (-1, True, 1.5, "2"):
        config["jobs"][0]["retention"] = {field: invalid}
        with pytest.raises(ConfigurationError):
            Configuration.parse(config)


def test_coverage_opt_in_requires_portable_scope_and_exact_private_job_mapping(config, tmp_path):
    from bbackup.models import HostBindings
    scope = {"schema_version": 1, "approved_roots": ["data"],
             "require_privileged": False, "filesystem_boundaries": "explicit",
             "metadata_expectations": {name: True for name in
                                       ("numeric_owner", "mode", "acl", "xattr", "links")}}
    config["jobs"][0]["coverage_policy"] = scope
    with pytest.raises(ConfigurationError, match="version 3"):
        Configuration.parse(config)
    config["schema_version"] = 3
    config["sources"] = [{"name": "files", "kind": "files"}]
    parsed = Configuration.parse(config)
    assert parsed.jobs[0].coverage_policy["approved_roots"] == ["data"]
    raw = {"schema_version": 3, "state_dir": str(tmp_path / "state"),
           "repositories": {name: {"repository": str(tmp_path / name),
                                    "password_file": str(tmp_path / "password")}
                            for name in ("local", "cloud")},
           "source_bindings": {"files": {"paths": [str(tmp_path / "data")]}},
           "coverage_declarations": {"daily": str(tmp_path / "declaration.json")}}
    bindings = HostBindings.parse(raw, parsed)
    assert bindings.coverage_declarations["daily"] == tmp_path / "declaration.json"
    del raw["coverage_declarations"]
    with pytest.raises(ConfigurationError, match="opt-in jobs"):
        HostBindings.parse(raw, parsed)


def test_remote_primary_requires_versioned_zero_cache_resource_budget(config):
    config["repositories"][0]["role"] = "remote-primary"
    with pytest.raises(ConfigurationError, match="resource budget"):
        Configuration.parse(config)
    config["jobs"][0]["resource_budget"] = {
        "schema_version": 1, "staging_max_bytes": 1024**3,
        "minimum_free_bytes": 64 * 1024**2, "cache_max_bytes": 0,
    }
    config["jobs"][0]["replicas"] = []
    parsed = Configuration.parse(config)
    assert parsed.repositories[0].role == "remote-primary"
    assert parsed.jobs[0].resource_budget.cache_max_bytes == 0
    config["jobs"][0]["replicas"] = ["cloud"]
    with pytest.raises(ConfigurationError, match="replica copies"):
        Configuration.parse(config)
    config["jobs"][0]["replicas"] = []
    config["jobs"][0]["resource_budget"]["cache_max_bytes"] = 1
    with pytest.raises(ConfigurationError, match="cache"):
        Configuration.parse(config)
    config["jobs"][0]["resource_budget"]["cache_max_bytes"] = 0
    config["jobs"][0]["resource_budget"]["schema_version"] = 2
    with pytest.raises(ConfigurationError, match="version"):
        Configuration.parse(config)


@pytest.mark.parametrize(
    "text", ['{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '{"a":NaN}', '{"a":Infinity}']
)
def test_reject_ambiguous_json(text):
    with pytest.raises(ConfigurationError):
        strict_json(text)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(secret="password"),
        lambda c: c.update(schema_version=True),
        lambda c: c["sources"][0].update(kind="mysql"),
        lambda c: c["sources"][0].update(paths=["relative"]),
        lambda c: c["jobs"][0].update(sources=["missing"]),
        lambda c: c["jobs"][0].update(replicas=["local"]),
        lambda c: c["jobs"][0].update(retention={"protection_days": 29}),
        lambda c: c["jobs"][0].update(overdue_hours=True),
        lambda c: c["repositories"].append(copy.deepcopy(c["repositories"][0])),
        lambda c: c["jobs"][0].update(schedule="daily\nExecStart=unsafe"),
    ],
)
def test_reject_unsafe_configuration(config, mutation):
    mutation(config)
    with pytest.raises(ConfigurationError):
        Configuration.parse(config)


def test_private_host_bindings(tmp_path):
    import json
    from bbackup.models import HostBindings

    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "state_dir": str(tmp_path / "state"),
                "repositories": {
                    "local": {
                        "repository": str(tmp_path / "repo"),
                        "password_file": str(tmp_path / "password"),
                    }
                },
            }
        )
    )
    path.chmod(0o644)
    with pytest.raises(ConfigurationError):
        HostBindings.load(path)
    path.chmod(0o600)
    assert (
        HostBindings.load(path).repositories["local"].password_file
        == tmp_path / "password"
    )


def test_database_sources_reference_named_private_connections(config):
    config["sources"] = [
        {
            "name": "pg",
            "kind": "postgresql",
            "database": "app_db",
            "connection": "prod_pg",
        },
        {
            "name": "mysql",
            "kind": "mysql",
            "database": "legacy",
            "connection": "prod_mysql",
            "quiesce": "read-lock",
        },
    ]
    config["jobs"][0]["sources"] = ["pg", "mysql"]

    parsed = Configuration.parse(config)

    assert parsed.sources[0].paths == ()
    assert parsed.sources[0].database == "app_db"
    assert parsed.sources[0].connection == "prod_pg"
    assert parsed.sources[1].kind == "mysql"


def test_docker_source_requires_one_named_container_and_explicit_stop_policy(config):
    config["sources"] = [{
        "name": "app",
        "kind": "docker",
        "container": "web-01",
        "connection": "local_docker",
        "stop_policy": "stop-restart",
    }]
    config["jobs"][0]["sources"] = ["app"]

    source = Configuration.parse(config).sources[0]

    assert source.paths == ()
    assert source.container == "web-01"
    assert source.connection == "local_docker"
    assert source.stop_policy == "stop-restart"


@pytest.mark.parametrize(
    "source",
    [
        {"name": "app", "kind": "docker", "container": "web", "connection": "local"},
        {"name": "app", "kind": "docker", "container": "web", "connection": "local",
         "stop_policy": "none"},
        {"name": "app", "kind": "docker", "containers": ["web"], "connection": "local",
         "stop_policy": "stop-restart"},
        {"name": "app", "kind": "docker", "container": "web", "connection": "tcp://remote",
         "stop_policy": "stop-restart"},
    ],
)
def test_docker_source_rejects_ambiguous_or_unsafe_schema(config, source):
    config["sources"] = [source]
    config["jobs"][0]["sources"] = [source["name"]]

    with pytest.raises(ConfigurationError):
        Configuration.parse(config)


@pytest.mark.parametrize(
    "source",
    [
        {
            "name": "pg",
            "kind": "postgresql",
            "database": "db",
            "connection": "host=other",
        },
        {
            "name": "mysql",
            "kind": "mysql",
            "database": "db@host",
            "connection": "prod",
            "quiesce": "read-lock",
        },
        {
            "name": "mysql",
            "kind": "mysql",
            "database": "db",
            "connection": "prod",
        },
        {
            "name": "mysql",
            "kind": "mysql",
            "database": "db",
            "connection": "prod",
            "quiesce": "none",
        },
        {
            "name": "maria",
            "kind": "mariadb",
            "database": "db",
            "connection": "prod",
            "paths": [],
        },
    ],
)
def test_database_sources_reject_connection_strings_and_ambiguous_fields(
    config, source
):
    config["sources"] = [source]
    config["jobs"][0]["sources"] = [source["name"]]

    with pytest.raises(ConfigurationError):
        Configuration.parse(config)


def test_database_host_bindings_expose_only_private_file_references(tmp_path):
    import json

    from bbackup.models import HostBindings

    path = tmp_path / "bindings.json"
    raw = {
        "schema_version": 2,
        "state_dir": str(tmp_path / "state"),
        "repositories": {
            "local": {
                "repository": str(tmp_path / "repo"),
                "password_file": str(tmp_path / "restic-password"),
            }
        },
        "database_bindings": {
            "prod_pg": {
                "kind": "postgresql",
                "service_file": str(tmp_path / "pg_service.conf"),
                "service_name": "prod",
                "password_file": str(tmp_path / "pgpass"),
            },
            "prod_mysql": {
                "kind": "mysql",
                "options_file": str(tmp_path / "mysql.cnf"),
            },
        },
        "docker_bindings": {
            "local_docker": {"kind": "docker", "socket": str(tmp_path / "docker.sock")}
        },
    }
    path.write_text(json.dumps(raw))
    path.chmod(0o600)

    bindings = HostBindings.load(path)

    assert bindings.database_bindings["prod_pg"].service_name == "prod"
    assert (
        bindings.database_bindings["prod_mysql"].options_file == tmp_path / "mysql.cnf"
    )
    assert set(bindings.sensitive_paths) == {
        tmp_path / "restic-password",
        tmp_path / "pg_service.conf",
        tmp_path / "pgpass",
        tmp_path / "mysql.cnf",
        tmp_path / "docker.sock",
    }


@pytest.mark.parametrize(
    "binding",
    [
        {"kind": "postgresql", "service_file": "relative", "service_name": "prod"},
        {
            "kind": "postgresql",
            "service_file": "/tmp/service",
            "service_name": "host=attacker",
        },
        {"kind": "mysql", "options_file": "relative"},
        {
            "kind": "mariadb",
            "options_file": "/tmp/options",
            "password_file": "/tmp/password",
        },
    ],
)
def test_database_binding_rejects_secret_values_and_unknown_fields(tmp_path, binding):
    import json

    from bbackup.models import HostBindings

    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "state_dir": str(tmp_path / "state"),
                "repositories": {},
                "database_bindings": {"prod": binding},
            }
        )
    )
    path.chmod(0o600)

    with pytest.raises(ConfigurationError):
        HostBindings.load(path)
