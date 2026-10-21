"""Local fixture checks for the private Linux coverage lifecycle."""
import json
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from bbackup.enrollment import policy_lease, write_bytes
from bbackup.linux_coverage import (CoverageError, InventoryPaths, check_coverage,
                                    discover_coverage, evaluate_recovery_evidence,
                                    restic_literal_exclusion, review_coverage,
                                    validate_scope_policy)


def _private_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _recipe(tmp_path, name="fixture_recipe"):
    procedure = tmp_path / (name + ".procedure")
    procedure.write_text("reinstall from fixture package inventory\n")
    procedure.chmod(0o600)
    procedure_digest = hashlib.sha256(procedure.read_bytes()).hexdigest()
    evidence = _private_json(tmp_path / (name + ".evidence.json"), {
        "schema_version": 1, "recipe_id": name, "recipe_sha256": procedure_digest,
        "environment": "isolated-local-fixture",
        "checks": {"procedure_executed": True, "result_verified": True},
    })
    recipe = {"reason": "reinstallable", "consequence": "rebuild on restore",
              "recipe_id": name, "recipe_sha256": procedure_digest}
    reference = {"recipe_id": name, "recipe_sha256": procedure_digest,
                 "procedure_path": str(procedure), "evidence_path": str(evidence),
                 "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
    return recipe, reference


@pytest.fixture
def coverage_fixture(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / ".credential").write_text("do not report this password")
    (root / "database.sqlite").write_bytes(b"sqlite fixture")
    (root / "alias").symlink_to(".credential")
    (root / "original").write_text("hardlink")
    (root / "linked").hardlink_to(root / "original")
    try:
        (root / "original").setxattr("user.fixture", b"value")
    except (AttributeError, OSError):
        pass
    inventory_dir = tmp_path / "inventory"
    inventory_dir.mkdir()
    passwd = inventory_dir / "passwd"
    passwd.write_text(f"fixture:x:1000:1000::{root}:/bin/sh\n")
    group = inventory_dir / "group"
    group.write_text("fixture:x:1000:\n")
    mountinfo = inventory_dir / "mountinfo"
    mountinfo.write_text("1 1 0:1 / / rw - ext4 /dev/fixture rw\n")
    packages = inventory_dir / "packages"
    packages.write_text("Package: fixture\n")
    units = inventory_dir / "units"
    units.mkdir()
    inventory = InventoryPaths(passwd, group, mountinfo, packages, units)
    policy = {"schema_version": 1, "approved_roots": ["home"],
              "require_privileged": False, "filesystem_boundaries": "explicit",
              "metadata_expectations": {name: True for name in
                                        ("numeric_owner", "mode", "acl", "xattr", "links")}}
    package_recipe, package_evidence = _recipe(tmp_path)
    declaration = _private_json(tmp_path / "declaration.json", {
        "schema_version": 1,
        "roots": [{"id": "home", "path": str(root), "source_id": "files", "adapter": "files"}],
        "exclusions": [], "mount_decisions": [], "identity_decisions": [],
        "applications": [], "package_reconstruction": package_recipe,
        "reconstruction_evidence": [package_evidence],
    })
    state = tmp_path / "state"
    policy_bytes, binding_bytes = b'{"policy":1}', b'{"binding":1}'
    write_bytes(state, policy_bytes, binding_bytes)
    with policy_lease(state, create=True):
        pass
    return {"root": root, "inventory": inventory, "policy": policy,
            "declaration": declaration, "state": state,
            "policy_bytes": policy_bytes, "binding_bytes": binding_bytes}


def _call(function, fixture, *args):
    return function(fixture["declaration"], fixture["policy"], *args,
                    state=fixture["state"], policy_bytes=fixture["policy_bytes"],
                    binding_bytes=fixture["binding_bytes"], inventory=fixture["inventory"])


def _review(fixture):
    candidate = _call(discover_coverage, fixture)
    assert candidate["coverage_status"] == "complete"
    reviewed = _call(review_coverage, fixture, candidate["candidate_digest"])
    return candidate, reviewed


def _sqlite_root(coverage_fixture):
    root = coverage_fixture["root"]
    (root / "database.sqlite").unlink(missing_ok=True)
    database_path = root / "database [review]*?.sqlite"
    writer = sqlite3.connect(database_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE committed (value TEXT)")
    writer.execute("INSERT INTO committed VALUES ('first')")
    writer.commit()
    try:
        os.setxattr(database_path, "user.coverage", b"original")
    except (AttributeError, OSError):
        writer.close()
        pytest.skip("Fixture filesystem cannot store SQLite xattrs")
    (coverage_fixture["inventory"].application_units / "fixture.service").write_text(
        "[Service]\n")
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    recipe, evidence = _recipe(database_path.parent, "sqlite_rebuild")
    declaration["roots"].append({"id": "database", "path": str(database_path),
        "source_id": "database", "adapter": "sqlite"})
    declaration["applications"] = [{"id": "fixture", "unit": "fixture",
        "source_id": "database", "state_roots": [str(database_path)],
        "startup_recipe": recipe}]
    declaration["reconstruction_evidence"].append(evidence)
    _private_json(coverage_fixture["declaration"], declaration)
    coverage_fixture["policy"] = {**coverage_fixture["policy"],
                                   "approved_roots": ["home", "database"]}
    return database_path, writer


def test_exact_review_capture_readiness_and_new_files(coverage_fixture):
    candidate, reviewed = _review(coverage_fixture)
    checked = _call(check_coverage, coverage_fixture)
    assert checked.capture_ready
    assert checked.baseline_digest == reviewed["baseline_digest"]
    assert checked.approved_roots == (("home", coverage_fixture["root"], "files", "files"),)
    assert "do not report" not in json.dumps(checked.public_result())
    assert str(coverage_fixture["root"]) not in json.dumps(candidate)
    assert str(coverage_fixture["root"]) not in json.dumps(checked.public_result())
    (coverage_fixture["root"] / ".new-hidden-secret").write_text("new secret")
    assert _call(check_coverage, coverage_fixture).capture_ready


def test_reviewed_sqlite_root_ignores_wal_churn_and_excludes_raw_files(coverage_fixture):
    database_path, writer = _sqlite_root(coverage_fixture)
    try:
        _, reviewed = _review(coverage_fixture)
        checked = _call(check_coverage, coverage_fixture)
        assert checked.capture_ready
        assert checked.baseline_digest == reviewed["baseline_digest"]
        assert len(checked.approved_sqlite) == 1
        accepted = checked.approved_sqlite[0]
        assert accepted["root_id"] == "database"
        assert accepted["path"] == str(database_path)
        assert accepted["identity"]["inode"] == database_path.stat().st_ino
        assert accepted["identity"]["xattrs"] == [
            {"name": "user.coverage", "value_base64": "b3JpZ2luYWw="}]
        assert accepted["recipe_digest"] == json.loads(
            coverage_fixture["declaration"].read_text())["applications"][0][
                "startup_recipe"]["recipe_sha256"]
        assert accepted["baseline_digest"] == reviewed["baseline_digest"]
        assert set(checked.sqlite_capture_exclusions) == {
            database_path, Path(str(database_path) + "-wal"),
            Path(str(database_path) + "-shm"), Path(str(database_path) + "-journal")}
        literal = restic_literal_exclusion(database_path)
        assert "\\[" in literal and "\\]" in literal and "\\*" in literal and "\\?" in literal

        writer.execute("INSERT INTO committed VALUES ('after review')")
        writer.commit()
        assert Path(str(database_path) + "-wal").exists()
        churned = _call(check_coverage, coverage_fixture)
        assert churned.capture_ready
        assert churned.candidate_digest == checked.candidate_digest
    finally:
        writer.close()


@pytest.mark.parametrize("change", ["inode", "mode"])
def test_reviewed_sqlite_identity_and_metadata_drift(coverage_fixture, change):
    database_path, writer = _sqlite_root(coverage_fixture)
    writer.close()
    _review(coverage_fixture)
    if change == "inode":
        replacement = coverage_fixture["root"].parent / "replacement.sqlite"
        with sqlite3.connect(replacement) as database:
            database.execute("CREATE TABLE replacement(value TEXT)")
        os.replace(replacement, database_path)
    else:
        database_path.chmod(database_path.stat().st_mode & 0o777 ^ 0o100)
    checked = _call(check_coverage, coverage_fixture)
    assert checked.coverage_status == "drift"
    assert "structural_inventory_changed" in checked.reasons


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_missing_or_corrupt_sqlite_root_is_incomplete_before_review(coverage_fixture, failure):
    database_path, writer = _sqlite_root(coverage_fixture)
    writer.close()
    if failure == "missing":
        database_path.unlink()
    else:
        database_path.write_bytes(b"not a SQLite database")
    candidate = _call(discover_coverage, coverage_fixture)
    assert candidate["coverage_status"] == "incomplete"
    expected = "sqlite_database_missing" if failure == "missing" else "sqlite_database_invalid"
    assert expected in candidate["reasons"]


@pytest.mark.parametrize("unsafe", ["symlink", "directory", "hardlink"])
def test_unsafe_existing_sqlite_sidecar_is_incomplete(coverage_fixture, unsafe):
    database_path, writer = _sqlite_root(coverage_fixture)
    writer.close()
    sidecar = Path(str(database_path) + "-wal")
    outside = coverage_fixture["root"].parent / "outside-sidecar"
    outside.write_bytes(b"sidecar")
    if unsafe == "symlink":
        sidecar.symlink_to(outside)
    elif unsafe == "directory":
        sidecar.mkdir()
    else:
        sidecar.hardlink_to(outside)
    candidate = _call(discover_coverage, coverage_fixture)
    assert candidate["coverage_status"] == "incomplete"
    assert "sqlite_sidecar_unsafe" in candidate["reasons"]


def test_sqlite_requires_exact_application_state_and_verified_recipe(coverage_fixture):
    database_path, writer = _sqlite_root(coverage_fixture)
    writer.close()
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    declaration["applications"][0]["state_roots"] = [str(database_path), str(coverage_fixture["root"])]
    _private_json(coverage_fixture["declaration"], declaration)
    candidate = _call(discover_coverage, coverage_fixture)
    assert "sqlite_application_state_unaccounted" in candidate["reasons"]

    declaration["applications"][0]["state_roots"] = [str(database_path)]
    declaration["reconstruction_evidence"] = [item for item in declaration["reconstruction_evidence"]
        if item["recipe_id"] != declaration["applications"][0]["startup_recipe"]["recipe_id"]]
    _private_json(coverage_fixture["declaration"], declaration)
    candidate = _call(discover_coverage, coverage_fixture)
    assert "reconstruction_evidence_missing" in candidate["reasons"]


def test_user_exclusion_cannot_cover_reviewed_sqlite_state(coverage_fixture, tmp_path):
    database_path, writer = _sqlite_root(coverage_fixture)
    writer.close()
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    recipe, evidence = _recipe(tmp_path, "exclude_sqlite")
    declaration["exclusions"] = [{"root_id": "home", "path": str(database_path),
                                  "recipe": recipe}]
    declaration["reconstruction_evidence"].append(evidence)
    _private_json(coverage_fixture["declaration"], declaration)
    candidate = _call(discover_coverage, coverage_fixture)
    assert "sqlite_state_excluded" in candidate["reasons"]


def test_sqlite_sidecar_cannot_be_an_explicit_files_root(coverage_fixture):
    database_path, writer = _sqlite_root(coverage_fixture)
    try:
        wal_path = Path(str(database_path) + "-wal")
        assert wal_path.is_file()
        declaration = json.loads(coverage_fixture["declaration"].read_text())
        declaration["roots"].append({"id": "wal", "path": str(wal_path),
            "source_id": "files", "adapter": "files"})
        _private_json(coverage_fixture["declaration"], declaration)
        coverage_fixture["policy"] = {**coverage_fixture["policy"],
                                       "approved_roots": ["home", "database", "wal"]}
        candidate = _call(discover_coverage, coverage_fixture)
        assert candidate["coverage_status"] == "incomplete"
        assert "sqlite_artifact_explicitly_mapped" in candidate["reasons"]
    finally:
        writer.close()


@pytest.mark.parametrize("change", ["user", "mount", "app"])
def test_structural_inventory_drift(coverage_fixture, change):
    _review(coverage_fixture)
    inventory = coverage_fixture["inventory"]
    if change == "user":
        inventory.passwd.write_text(inventory.passwd.read_text() + "new:x:1001:1001::/new:/bin/sh\n")
    elif change == "mount":
        inventory.mountinfo.write_text(inventory.mountinfo.read_text() +
                                       f"2 1 0:2 / {coverage_fixture['root']} rw - ext4 /dev/other rw\n")
    else:
        (inventory.application_units / "new.service").write_text("[Service]\n")
    checked = _call(check_coverage, coverage_fixture)
    assert checked.coverage_status == "drift"
    assert not checked.capture_ready
    assert "structural_inventory_changed" in checked.reasons


def test_instantiated_systemd_services_remain_reviewable(coverage_fixture, tmp_path):
    unit_stems = ["serial-getty@ttyS0", "worker@", r"worker@tty\x2dS0", "a" * 247]
    inventory = coverage_fixture["inventory"].application_units
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    applications = []
    for index, stem in enumerate(unit_stems):
        (inventory / f"{stem}.service").write_text("[Service]\n")
        recipe, evidence = _recipe(tmp_path, f"startup_{index}")
        declaration["reconstruction_evidence"].append(evidence)
        applications.append({"id": f"unit_{index}", "unit": stem,
            "source_id": "files", "state_roots": [str(coverage_fixture["root"])],
            "startup_recipe": recipe})
    declaration["applications"] = applications
    _private_json(coverage_fixture["declaration"], declaration)

    candidate, reviewed = _review(coverage_fixture)
    checked = _call(check_coverage, coverage_fixture)

    assert candidate["counts"]["applications"] == len(unit_stems)
    assert checked.capture_ready
    assert checked.baseline_digest == reviewed["baseline_digest"]


@pytest.mark.parametrize("unit_stem", [
    "", "@ttyS0", "worker/ttyS0", "worker@ttyS0/extra", "../escape", "worker\nservice",
    "worker@ttyS0@ttyS1", r"worker@tty\x2", r"worker@tty\xG0",
    r"worker@tty\x00", "a" * 248,
])
def test_invalid_systemd_service_stems_are_rejected(coverage_fixture, unit_stem):
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    recipe, evidence = _recipe(coverage_fixture["declaration"].parent, "invalid_unit")
    declaration["applications"] = [{"id": "invalid_unit", "unit": unit_stem,
        "source_id": "files", "state_roots": [str(coverage_fixture["root"])],
        "startup_recipe": recipe}]
    declaration["reconstruction_evidence"].append(evidence)
    _private_json(coverage_fixture["declaration"], declaration)

    with pytest.raises(CoverageError, match="application unit"):
        _call(discover_coverage, coverage_fixture)


def test_private_tmp_namespace_mount_ids_do_not_invalidate_reviewed_scope(coverage_fixture):
    root = coverage_fixture["root"]
    ancestor = root.parent
    mountinfo = coverage_fixture["inventory"].mountinfo
    def lines(start_id, *, private=False, bind=False):
        text = (f"{start_id} {start_id} 0:1 / / rw - ext4 /dev/fixture rw\n"
                f"{start_id + 1} {start_id} 0:2 / {ancestor} rw - ext4 /dev/data rw\n"
                f"{start_id + 2} {start_id + 1} 0:3 / {root} rw - ext4 /dev/scope rw\n")
        if private:
            text += (f"{start_id + 3} {start_id + 1} 0:4 / {ancestor / 'systemd-private-tmp'} "
                     "rw - tmpfs tmpfs rw\n")
        if bind:
            text += (f"{start_id + 4} {start_id + 2} 0:5 / {root / 'bind'} "
                     "rw - ext4 /dev/bind rw\n")
        return text
    mountinfo.write_text(lines(10))
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    declaration["mount_decisions"] = [{"mount_id": 12, "mountpoint": str(root),
                                       "decision": "include"}]
    _private_json(coverage_fixture["declaration"], declaration)
    _review(coverage_fixture)
    baseline_check = _call(check_coverage, coverage_fixture)
    mountinfo.write_text(lines(110, private=True))
    namespace_check = _call(check_coverage, coverage_fixture)
    assert namespace_check.capture_ready
    assert namespace_check.candidate_digest == baseline_check.candidate_digest
    mountinfo.write_text(lines(110, private=True).replace("/dev/scope", "/dev/replacement"))
    assert _call(check_coverage, coverage_fixture).coverage_status == "drift"
    (root / "bind").mkdir()
    mountinfo.write_text(lines(110, private=True, bind=True))
    changed = _call(check_coverage, coverage_fixture)
    assert changed.coverage_status == "drift"
    assert "mount_boundary_undecided" in changed.reasons


def test_stale_candidate_and_baseline_tamper_fail_closed(coverage_fixture):
    candidate = _call(discover_coverage, coverage_fixture)
    coverage_fixture["inventory"].passwd.write_text("other:x:1000:1000::/missing:/bin/sh\n")
    with pytest.raises(CoverageError, match="changed"):
        _call(review_coverage, coverage_fixture, candidate["candidate_digest"])
    coverage_fixture["inventory"].passwd.write_text(
        f"fixture:x:1000:1000::{coverage_fixture['root']}:/bin/sh\n")
    _, reviewed = _review(coverage_fixture)
    baseline = coverage_fixture["state"] / "coverage" / "baselines" / (reviewed["baseline_digest"] + ".json")
    baseline.write_text("{}")
    assert _call(check_coverage, coverage_fixture).coverage_status == "unenrolled"


def test_missing_root_and_changed_declaration_fail(coverage_fixture):
    _review(coverage_fixture)
    root = coverage_fixture["root"]
    os.mkfifo(root / "unsupported")
    assert _call(check_coverage, coverage_fixture).coverage_status == "incomplete"
    (root / "unsupported").unlink()
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    declaration["roots"][0]["path"] = str(root / "elsewhere")
    _private_json(coverage_fixture["declaration"], declaration)
    assert _call(check_coverage, coverage_fixture).coverage_status == "drift"


def test_policy_rejects_incomplete_metadata_and_evidence_is_only_emulated(tmp_path):
    with pytest.raises(CoverageError):
        validate_scope_policy({"schema_version": 1, "approved_roots": ["x"]})
    evidence = _private_json(tmp_path / "evidence.json", {
        "schema_version": 1, "environment": "isolated-local-fixture",
        "baseline_digest": "a" * 64, "snapshot_id": "b" * 64,
        "replacement_machine_digest": "c" * 64, "independent_escrow": True,
        "checks": {name: True for name in
                   ("metadata", "secrets", "users", "reinstall", "app_data", "startup")},
    })
    result = evaluate_recovery_evidence(evidence, baseline_digest="a" * 64,
                                        snapshot_id="b" * 64, source_machine_digest="d" * 64)
    assert result["recovery_status"] == "emulated"
    with pytest.raises(CoverageError, match="exact baseline"):
        evaluate_recovery_evidence(evidence, baseline_digest="e" * 64,
                                   snapshot_id="b" * 64, source_machine_digest="d" * 64)


def test_profiles_do_not_replace_each_others_active_review(coverage_fixture, tmp_path):
    _review(coverage_fixture)
    first = coverage_fixture["declaration"]
    second = _private_json(tmp_path / "other-declaration.json", json.loads(first.read_text()))
    coverage_fixture["declaration"] = second
    _review(coverage_fixture)
    assert _call(check_coverage, coverage_fixture).capture_ready
    coverage_fixture["declaration"] = first
    assert _call(check_coverage, coverage_fixture).capture_ready


def test_linux_account_shell_group_members_and_linked_home(coverage_fixture, tmp_path):
    inventory = coverage_fixture["inventory"]
    inventory.passwd.write_text(f"_apt:x:1000:1000::{coverage_fixture['root']}:/bin/sh\n")
    inventory.group.write_text("_apt:x:1000:fixture\n")
    _review(coverage_fixture)
    inventory.group.write_text("_apt:x:1000:other\n")
    assert _call(check_coverage, coverage_fixture).coverage_status == "drift"
    inventory.group.write_text("_apt:x:1000:fixture\n")
    inventory.passwd.write_text(f"_apt:x:1000:1000::{coverage_fixture['root']}:/bin/false\n")
    assert _call(check_coverage, coverage_fixture).coverage_status == "drift"
    outside = tmp_path / "outside"
    outside.mkdir()
    home_link = coverage_fixture["root"] / "home-link"
    home_link.symlink_to(outside)
    inventory.passwd.write_text(f"_apt:x:1000:1000::{home_link}:/bin/sh\n")
    discovered = _call(discover_coverage, coverage_fixture)
    assert discovered["coverage_status"] == "incomplete"
    assert "user_home_unaccounted" in discovered["reasons"]


def test_nested_symlink_unit_and_unreadable_file_are_detected(coverage_fixture):
    _review(coverage_fixture)
    wants = coverage_fixture["inventory"].application_units / "multi-user.target.wants"
    wants.mkdir()
    (wants / "new.service").symlink_to("/usr/lib/systemd/system/new.service")
    assert _call(check_coverage, coverage_fixture).coverage_status == "drift"
    (wants / "new.service").unlink()
    wants.rmdir()
    if os.geteuid() == 0:
        pytest.skip("Root can read mode-zero files")
    secret = coverage_fixture["root"] / ".credential"
    secret.chmod(0)
    try:
        discovered = _call(discover_coverage, coverage_fixture)
        assert "unreadable_file" in discovered["reasons"]
    finally:
        secret.chmod(0o600)


def test_reconstruction_reference_must_be_verified(coverage_fixture):
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    declaration["reconstruction_evidence"] = []
    _private_json(coverage_fixture["declaration"], declaration)
    discovered = _call(discover_coverage, coverage_fixture)
    assert "reconstruction_evidence_missing" in discovered["reasons"]
    declaration["reconstruction_evidence"] = [{"recipe_id": "fixture_recipe",
        "recipe_sha256": declaration["package_reconstruction"]["recipe_sha256"],
        "procedure_path": str(coverage_fixture["declaration"]),
        "evidence_path": str(coverage_fixture["declaration"]),
        "evidence_sha256": "a" * 64}]
    _private_json(coverage_fixture["declaration"], declaration)
    discovered = _call(discover_coverage, coverage_fixture)
    assert "reconstruction_evidence_unverified" in discovered["reasons"]


def test_reviewed_exact_exclusion_is_carried_to_capture_gate(coverage_fixture, tmp_path):
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    recipe, evidence = _recipe(tmp_path, "credential_rebuild")
    omitted = coverage_fixture["root"] / ".credential"
    declaration["exclusions"] = [{"root_id": "home", "path": str(omitted), "recipe": recipe}]
    declaration["reconstruction_evidence"].append(evidence)
    _private_json(coverage_fixture["declaration"], declaration)
    _review(coverage_fixture)
    checked = _call(check_coverage, coverage_fixture)
    assert checked.capture_ready
    assert checked.approved_exclusions == (omitted,)
    assert str(omitted) not in json.dumps(checked.public_result())
    omitted.unlink()
    assert _call(check_coverage, coverage_fixture).coverage_status == "incomplete"


def test_application_state_link_outside_reviewed_roots_is_incomplete(coverage_fixture, tmp_path):
    outside = tmp_path / "app-data"
    outside.mkdir()
    state_link = coverage_fixture["root"] / "app-data"
    state_link.symlink_to(outside)
    (coverage_fixture["inventory"].application_units / "fixture.service").write_text("[Service]\n")
    declaration = json.loads(coverage_fixture["declaration"].read_text())
    recipe, evidence = _recipe(tmp_path, "startup")
    declaration["applications"] = [{"id": "fixture", "unit": "fixture",
        "source_id": "files", "state_roots": [str(state_link)], "startup_recipe": recipe}]
    declaration["reconstruction_evidence"].append(evidence)
    _private_json(coverage_fixture["declaration"], declaration)
    discovered = _call(discover_coverage, coverage_fixture)
    assert discovered["coverage_status"] == "incomplete"
    assert "application_state_unaccounted" in discovered["reasons"]
