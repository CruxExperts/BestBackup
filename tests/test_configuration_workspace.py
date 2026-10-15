import gzip
import json

import pytest

from bbackup.configuration_workspace import ConfigurationWorkspace
from bbackup.models import Configuration, ConfigurationError, HostBindings


@pytest.fixture
def workspace_documents(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    password = tmp_path / "repository.password"
    password.write_text("disposable-test-password\n")
    password.chmod(0o600)
    configuration = {"schema_version": 2, "repositories": [{"name": "local"}],
                     "sources": [{"name": "files", "kind": "files", "paths": [str(source)]}],
                     "jobs": [{"name": "daily", "repository": "local", "sources": ["files"]}]}
    bindings = {"schema_version": 2, "state_dir": str(tmp_path / "state"),
                "repositories": {"local": {"repository": str(tmp_path / "repository"),
                                             "password_file": str(password)}}}
    return ConfigurationWorkspace(tmp_path / "config.json", tmp_path / "bindings.json"), configuration, bindings


def test_setup_never_replaces_an_existing_host_declaration(workspace_documents):
    workspace, config, bindings = workspace_documents
    service = workspace.setup(json.dumps(config), json.dumps(bindings))
    assert service.config.jobs[0].name == "daily"
    assert workspace.config_path.stat().st_mode & 0o777 == 0o600
    assert workspace.bindings_path.stat().st_mode & 0o777 == 0o600
    original = workspace.bindings_path.read_bytes()
    changed = dict(bindings, state_dir=str(service.state / "different"))
    with pytest.raises(ConfigurationError):
        workspace.setup(json.dumps(config), json.dumps(changed))
    assert workspace.bindings_path.read_bytes() == original
    assert HostBindings.load(workspace.bindings_path).state_dir == service.state


def test_settings_preserve_recoverable_previous_bytes_and_reject_stale_writer(workspace_documents):
    workspace, config, bindings = workspace_documents
    workspace.setup(json.dumps(config), json.dumps(bindings))
    original = workspace.read()
    config["jobs"][0]["overdue_hours"] = 48
    saved = workspace.save(json.dumps(config), original["revision"])
    assert Configuration.load(workspace.config_path).jobs[0].overdue_hours == 48
    with gzip.open(saved["backup"], "rb") as backup:
        assert backup.read() == original["document"].encode()
    config["jobs"][0]["overdue_hours"] = 72
    with pytest.raises(ConfigurationError, match="changed"):
        workspace.save(json.dumps(config), original["revision"])
    assert Configuration.load(workspace.config_path).jobs[0].overdue_hours == 48


def test_settings_reject_unbound_repository_and_symlink_target(workspace_documents, tmp_path):
    workspace, config, bindings = workspace_documents
    workspace.setup(json.dumps(config), json.dumps(bindings))
    original = workspace.read()
    config["repositories"].append({"name": "unbound", "role": "replica"})
    from bbackup.service import ServiceError
    with pytest.raises(ServiceError):
        workspace.save(json.dumps(config), original["revision"])
    assert workspace.read() == original
    destination = tmp_path / "operator-config.json"
    workspace.config_path.rename(destination)
    workspace.config_path.symlink_to(destination)
    with pytest.raises(OSError):
        workspace.read()
    assert destination.read_text() == original["document"]
