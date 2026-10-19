import gzip
import json

import pytest

from bbackup.configuration_workspace import ConfigurationWorkspace
from bbackup.enrollment import policy_lease, verify as verify_enrollment
from bbackup.models import Configuration, ConfigurationError, HostBindings


@pytest.fixture
def workspace_documents(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    password = tmp_path / "repository.password"
    password.write_text("disposable-test-password\n")
    password.chmod(0o600)
    configuration = {"schema_version": 3, "repositories": [{"name": "local"}],
                     "sources": [{"name": "files", "kind": "files"}],
                     "jobs": [{"name": "daily", "repository": "local", "sources": ["files"]}]}
    bindings = {"schema_version": 3, "state_dir": str(tmp_path / "state"),
                "repositories": {"local": {"repository": str(tmp_path / "repository"),
                                             "password_file": str(password)}},
                "source_bindings": {"files": {"paths": [str(source)]}}}
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
    assert HostBindings.load(workspace.bindings_path, service.config).state_dir == service.state


def test_setup_requires_explicit_migration_for_version_two(workspace_documents):
    workspace, config, bindings = workspace_documents
    legacy = dict(config, schema_version=2, sources=[
        {"name": "files", "kind": "files", "paths": bindings["source_bindings"]["files"]["paths"]}])
    with pytest.raises(ConfigurationError, match="migrate"):
        workspace.setup(json.dumps(legacy), json.dumps(bindings))
    assert not workspace.config_path.exists()


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


def test_migration_preserves_version_two_and_enrolls_private_sources(workspace_documents):
    workspace, config, bindings = workspace_documents
    legacy_config = dict(config, schema_version=2, sources=[
        {"name": "files", "kind": "files", "paths": bindings["source_bindings"]["files"]["paths"]}])
    legacy_bindings = {key: value for key, value in bindings.items() if key != "source_bindings"}
    legacy_bindings["schema_version"] = 2
    workspace.config_path.write_text(json.dumps(legacy_config))
    workspace.bindings_path.write_text(json.dumps(legacy_bindings))
    workspace.bindings_path.chmod(0o600)
    original_policy = workspace.config_path.read_bytes()
    original_bindings = workspace.bindings_path.read_bytes()
    migrated = workspace.migrate()
    policy_path = workspace.config_path.with_name("config.v3.json")
    bindings_path = workspace.bindings_path.with_name("bindings.v3.json")
    assert migrated["configuration_path"] == str(policy_path)
    assert workspace.config_path.read_bytes() == original_policy
    assert workspace.bindings_path.read_bytes() == original_bindings
    policy = json.loads(policy_path.read_text())
    private = json.loads(bindings_path.read_text())
    assert policy["sources"] == [{"name": "files", "kind": "files"}]
    assert private["source_bindings"]["files"] == bindings["source_bindings"]["files"]
    verify_enrollment(workspace.config_path.parent / "state", policy_path, bindings_path)


def test_save_reports_busy_while_shared_operation_lease_is_held(workspace_documents):
    workspace, config, bindings = workspace_documents
    workspace.setup(json.dumps(config), json.dumps(bindings))
    current = workspace.read()
    config["jobs"][0]["overdue_hours"] = 48
    with policy_lease(workspace.config_path.parent / "state"):
        with pytest.raises(ConfigurationError, match="busy"):
            workspace.save(json.dumps(config), current["revision"])
        assert workspace.read()["revision"] == current["revision"]
    workspace.save(json.dumps(config), current["revision"])
    assert Configuration.load(workspace.config_path).jobs[0].overdue_hours == 48


def test_save_refuses_binding_edit_after_policy_replacement(workspace_documents, monkeypatch):
    from bbackup import configuration_workspace as module
    workspace, config, bindings = workspace_documents
    workspace.setup(json.dumps(config), json.dumps(bindings))
    original = workspace.read()
    config["jobs"][0]["overdue_hours"] = 48
    sync = module._sync_directory

    def edit_after_replace(directory):
        sync(directory)
        if directory == workspace.config_path.parent:
            private = json.loads(workspace.bindings_path.read_text())
            private["repositories"]["local"]["repository"] += "-raw-edit"
            workspace.bindings_path.write_text(json.dumps(private))

    monkeypatch.setattr(module, "_sync_directory", edit_after_replace)
    with pytest.raises(ConfigurationError, match="enrollment was not refreshed"):
        workspace.save(json.dumps(config), original["revision"])
    with pytest.raises(ConfigurationError, match="enrollment differs"):
        verify_enrollment(workspace.config_path.parent / "state", workspace.config_path,
                          workspace.bindings_path)


def test_save_receipt_never_adopts_binding_edit_at_receipt_write(workspace_documents, monkeypatch):
    from bbackup import configuration_workspace as module
    workspace, config, bindings = workspace_documents
    workspace.setup(json.dumps(config), json.dumps(bindings))
    original = workspace.read()
    config["jobs"][0]["overdue_hours"] = 48
    write = module.write_bytes

    def edit_before_receipt(state, policy_bytes, binding_bytes):
        private = json.loads(workspace.bindings_path.read_text())
        private["repositories"]["local"]["repository"] += "-raw-edit"
        workspace.bindings_path.write_text(json.dumps(private))
        write(state, policy_bytes, binding_bytes)

    monkeypatch.setattr(module, "write_bytes", edit_before_receipt)
    workspace.save(json.dumps(config), original["revision"])
    with pytest.raises(ConfigurationError, match="enrollment differs"):
        verify_enrollment(workspace.config_path.parent / "state", workspace.config_path,
                          workspace.bindings_path)


def test_enrollment_refuses_files_changed_since_review(workspace_documents):
    workspace, config, bindings = workspace_documents
    service = workspace.setup(json.dumps(config), json.dumps(bindings))
    revision = workspace.candidates()["review_revision"]
    receipt = (service.state / "enrollment.json").read_bytes()
    bindings["source_bindings"]["files"]["paths"] = [str(workspace.config_path.parent / "other-source")]
    workspace.bindings_path.write_text(json.dumps(bindings))
    with pytest.raises(ConfigurationError, match="changed since review"):
        workspace.enroll(review_revision=revision)
    assert (service.state / "enrollment.json").read_bytes() == receipt
    fresh = workspace.candidates()
    assert fresh["sources"][0]["selector"] == bindings["source_bindings"]["files"]["paths"]
    workspace.enroll(review_revision=fresh["review_revision"])
    verify_enrollment(service.state, workspace.config_path, workspace.bindings_path)
