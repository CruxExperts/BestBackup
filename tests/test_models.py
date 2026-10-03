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
    assert parsed.jobs[0].retention.cloud_monthly == 12
    assert parsed.jobs[0].overdue_hours == 26
    assert parsed.jobs[0].schedule == "daily"


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
