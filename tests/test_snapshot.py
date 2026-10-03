import json
import hashlib
import textwrap
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bbackup.cli import cli
from bbackup.config import Config
from bbackup.snapshot import (
    ResticRunner,
    SnapshotError,
    check_snapshot_profile,
    discover_git_repos,
    load_state,
    path_id,
    purge_plan,
    reconcile_repos,
    retire_repo,
    save_state,
    snapshot_check,
    snapshot_plan,
    snapshot_run,
    state_file,
)


def write_snapshot_config(tmp_path, extra=""):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent(f"""
        snapshot_profiles:
          essentials-daily:
            engine: restic
            host_id: test-host
            repository: rclone:example-drive:backups/WORKSTATION01/restic/essentials-daily
            cache_dir: {tmp_path}/cache
            state_dir: {tmp_path}/state
            password_file: {tmp_path}/password
            retry_lock: 7m
            repo_homes:
              - {tmp_path}/repos
            explicit_repos:
              - {tmp_path}/myrig
            include_paths:
              - {tmp_path}/Documents
            exclude_paths:
              - {tmp_path}/repos/archives
            tags:
              - workstation01
            {extra}
    """))
    (tmp_path / "password").write_text("secret\n")
    (tmp_path / "password").chmod(0o600)
    (tmp_path / "cache").mkdir()
    (tmp_path / "state").mkdir()
    return cfg_file


def init_git_repo(path):
    path.mkdir(parents=True)
    (path / "README.md").write_text("test\n")
    import subprocess

    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


def test_snapshot_profile_config_parsed(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    assert profile.engine == "restic"
    assert profile.repository.startswith("rclone:")
    assert profile.repo_homes == [f"{tmp_path}/repos"]
    assert profile.explicit_repos == [f"{tmp_path}/myrig"]
    assert profile.exclude_paths == [f"{tmp_path}/repos/archives"]


def test_snapshot_profile_default_drive_client_opt_in_requires_boolean_true(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path, 'allow_default_rclone_drive_client: "false"')))
    profile = cfg.snapshot_profiles["essentials-daily"]
    assert profile.allow_default_rclone_drive_client is False


def test_restic_backup_args_include_required_flags(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    args = ResticRunner(profile).backup_args("/tmp/source", ["bbackup", "profile=essentials-daily"])
    assert args[:3] == ["restic", "-r", profile.repository]
    assert "--password-file" in args
    assert "--cache-dir" in args
    assert "--json" in args
    assert "--retry-lock" in args
    assert "7m" in args
    assert "--host" in args
    assert "test-host" in args
    assert "--tag" in args
    assert "--exclude" in args



def test_restic_retention_args_use_one_conjunctive_scope_selector(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    args = ResticRunner(profile).retention_args(
        ["bbackup", "profile=essentials-daily", "scope=repo", "repo_id=abc123"],
        {"daily": 14, "weekly": 8, "monthly": 12},
        host="test-host",
        dry_run=True,
    )

    tag_index = args.index("--tag")
    assert args[tag_index + 1] == "bbackup,profile=essentials-daily,scope=repo,repo_id=abc123"
    assert args.count("--tag") == 1
    assert args[args.index("--host") + 1] == "test-host"
    assert args[args.index("--group-by") + 1] == "host"
    assert "--keep-daily" in args
    assert "--keep-weekly" in args
    assert "--keep-monthly" in args
    assert "--dry-run" in args


def test_retention_selector_ignores_mutable_profile_tags(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.retention = {"active_repo_daily": 14}
    repo_path = tmp_path / "repos" / "repo-a"
    init_git_repo(repo_path)

    profile.tags = ["owner=before"]
    before = snapshot_plan(profile)["retention"][0]
    profile.tags = ["owner=after"]
    after = snapshot_plan(profile)["retention"][0]

    assert before["tags"] == after["tags"]
    assert "owner=before" not in before["args"]
    assert "owner=after" not in after["args"]
    assert before["args"][before["args"].index("--host") + 1] == "test-host"


def test_snapshot_retention_rejects_fractional_counts(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.retention = {"active_repo_daily": 1.9}
    init_git_repo(tmp_path / "repos" / "repo-a")

    try:
        snapshot_plan(profile)
    except SnapshotError as exc:
        assert "non-negative integer" in str(exc)
    else:
        raise AssertionError("fractional retention count should be rejected")

def test_snapshot_retention_ignores_unscoped_counts(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.retention = {"daily": 7, "weekly": 4, "monthly": 12}
    init_git_repo(tmp_path / "repos" / "repo-a")

    plan = snapshot_plan(profile)

    assert plan["retention"] == []


def test_snapshot_plan_retention_only_targets_active_repos_and_paths(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.retention = {
        "active_repo_daily": 14,
        "path_daily": 14,
    }
    include_path = tmp_path / "Documents"
    include_path.mkdir()
    profile.include_paths = [str(include_path)]
    repo_path = tmp_path / "repos" / "repo-a"
    init_git_repo(repo_path)
    active_repo_id = reconcile_repos(profile, discover_git_repos(profile))["active_repo_ids"][0]
    state = load_state(profile)
    state["repos"]["retired-id"] = {
        "repo_id": "retired-id",
        "path": str(tmp_path / "repos" / "retired"),
        "status": "retired",
        "successful_snapshot_ids": ["retired-snapshot"],
    }
    save_state(profile, state)

    plan = snapshot_plan(profile)

    identifiers = {item["identifier"] for item in plan["retention"]}
    assert identifiers == {active_repo_id, path_id(str(include_path))}
    assert "retired-id" not in identifiers
    for item in plan["retention"]:
        assert item["args"].count("--tag") == 1
        assert "--dry-run" in item["args"]
        assert item["args"][item["args"].index("--tag") + 1].count(",") >= 3


def test_snapshot_run_applies_scoped_retention_after_successful_backup(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.retention = {"active_repo_daily": 14}
    profile.include_paths = []
    init_git_repo(tmp_path / "repos" / "repo-a")
    backup_stdout = json.dumps({"message_type": "summary", "snapshot_id": "b" * 64})
    successful_result = {
        "args": [],
        "returncode": 0,
        "stdout": backup_stdout,
        "stderr": "",
        "ok": True,
    }
    retention_result = {**successful_result, "stdout": ""}

    with patch("bbackup.snapshot._require_snapshot_operation_preflight"), patch.object(
        ResticRunner,
        "run",
        side_effect=[successful_result, retention_result],
    ) as run:
        result = snapshot_run(profile)

    assert result["success"] is True
    assert len(result["retention_results"]) == 1
    retention_args = run.call_args_list[1].args[0]
    retention_tag = retention_args[retention_args.index("--tag") + 1]
    assert retention_tag.startswith("bbackup,profile=")
    assert "scope=repo" in retention_tag
    assert retention_tag.endswith(f"repo_id={result['results'][0]['repo_id']}")



def test_restic_backup_args_expand_path_based_excludes(tmp_path, monkeypatch):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    monkeypatch.setenv("BBACKUP_EXCLUDE_ROOT", str(tmp_path / "cache-root"))
    profile.exclude_paths = ["$BBACKUP_EXCLUDE_ROOT/cache", "~/CloudDrive", "node_modules"]

    args = ResticRunner(profile).backup_args("/tmp/source", ["bbackup"])
    exclude_values = [
        args[index + 1]
        for index, value in enumerate(args)
        if value == "--exclude"
    ]

    assert str((tmp_path / "cache-root" / "cache").resolve()) in exclude_values
    assert "node_modules" in exclude_values
    assert not any(value.startswith("$BBACKUP_EXCLUDE_ROOT") for value in exclude_values)


def test_discover_git_repos_uses_immediate_children_and_explicit_roots(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    init_git_repo(tmp_path / "repos" / "repo-a")
    (tmp_path / "repos" / "not-git").mkdir(parents=True)
    init_git_repo(tmp_path / "myrig")

    discovered = discover_git_repos(profile)
    paths = {repo.path for repo in discovered}
    assert str((tmp_path / "repos" / "repo-a").resolve()) in paths
    assert str((tmp_path / "myrig").resolve()) in paths
    assert str((tmp_path / "repos" / "not-git").resolve()) not in paths


def test_discover_git_repos_skips_excluded_repo_home_children(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.exclude_paths.append(f"{tmp_path}/repos/starred-repos")
    init_git_repo(tmp_path / "repos" / "repo-a")
    init_git_repo(tmp_path / "repos" / "starred-repos")

    discovered = discover_git_repos(profile)
    paths = {repo.path for repo in discovered}
    assert str((tmp_path / "repos" / "repo-a").resolve()) in paths
    assert str((tmp_path / "repos" / "starred-repos").resolve()) not in paths


def test_reconcile_marks_deleted_repo_retired_only_after_snapshot(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    init_git_repo(tmp_path / "repos" / "repo-a")
    discovered = discover_git_repos(profile)
    first = reconcile_repos(profile, discovered)
    repo_id = first["active_repo_ids"][0]
    state = load_state(profile)
    state["repos"][repo_id]["successful_snapshot_ids"] = ["abc123"]
    from bbackup.snapshot import save_state

    save_state(profile, state)

    import shutil

    shutil.rmtree(tmp_path / "repos" / "repo-a")
    result = reconcile_repos(profile, [])
    assert repo_id in result["retired_repo_ids"]
    assert result["alerts"] == []


def test_reconcile_alerts_when_deleted_repo_has_no_successful_snapshot(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    init_git_repo(tmp_path / "repos" / "repo-a")
    discovered = discover_git_repos(profile)
    reconcile_repos(profile, discovered)

    import shutil

    shutil.rmtree(tmp_path / "repos" / "repo-a")
    result = reconcile_repos(profile, [])
    assert result["alerts"][0]["code"] == "missing_without_snapshot"


def test_path_reuse_with_different_fingerprint_alerts(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    init_git_repo(tmp_path / "repos" / "repo-a")
    discovered = discover_git_repos(profile)
    first = reconcile_repos(profile, discovered)
    repo_id = first["active_repo_ids"][0]
    state = load_state(profile)
    state["repos"][repo_id]["status"] = "retired"
    state["repos"][repo_id]["successful_snapshot_ids"] = ["abc123"]
    state["repos"][repo_id]["fingerprint"] = "different"
    from bbackup.snapshot import save_state

    save_state(profile, state)

    result = reconcile_repos(profile, discovered)
    assert result["alerts"][0]["code"] == "path_reuse_conflict"



def test_reconcile_keeps_discovered_retired_repo_out_of_active_retention(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.include_paths = []
    profile.retention = {"active_repo_daily": 14}
    repo_path = tmp_path / "repos" / "repo-a"
    init_git_repo(repo_path)
    discovered = discover_git_repos(profile)
    first = reconcile_repos(profile, discovered)
    repo_id = first["active_repo_ids"][0]
    state = load_state(profile)
    state["repos"][repo_id]["successful_snapshot_ids"] = ["snapshot-1"]
    save_state(profile, state)
    retire_repo(profile, repo_id)

    reconciled = reconcile_repos(profile, discovered)
    plan = snapshot_plan(profile)

    assert repo_id not in reconciled["active_repo_ids"]
    assert repo_id in reconciled["retired_repo_ids"]
    assert load_state(profile)["repos"][repo_id]["status"] == "retired"
    assert plan["repos"] == []
    assert plan["retention"] == []

def test_no_origin_retired_path_reuse_alerts_after_reinit(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    repo_path = tmp_path / "repos" / "repo-a"
    init_git_repo(repo_path)
    first = reconcile_repos(profile, discover_git_repos(profile))
    repo_id = first["active_repo_ids"][0]
    state = load_state(profile)
    state["repos"][repo_id]["status"] = "retired"
    state["repos"][repo_id]["successful_snapshot_ids"] = ["abc123"]
    save_state(profile, state)

    import shutil
    import subprocess

    shutil.rmtree(repo_path)
    repo_path.mkdir(parents=True)
    (repo_path / "README.md").write_text("different repo\n")
    subprocess.run(["git", "-C", str(repo_path), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "different"], check=True, capture_output=True)
    result = reconcile_repos(profile, discover_git_repos(profile))

    assert result["alerts"][0]["code"] == "path_reuse_conflict"


def test_reconcile_updates_active_repo_after_new_commit(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    repo_path = tmp_path / "repos" / "repo-a"
    init_git_repo(repo_path)
    first = reconcile_repos(profile, discover_git_repos(profile))
    repo_id = first["active_repo_ids"][0]

    (repo_path / "README.md").write_text("changed\n")
    import subprocess

    subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "change"], check=True, capture_output=True)
    second = reconcile_repos(profile, discover_git_repos(profile))

    assert second["alerts"] == []
    assert second["active_repo_ids"] == [repo_id]
    state = load_state(profile)
    assert state["repos"][repo_id]["head"]


def test_reconcile_migrates_legacy_head_derived_repo_id(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    repo_path = tmp_path / "repos" / "repo-a"
    init_git_repo(repo_path)
    discovered = discover_git_repos(profile)
    repo = discovered[0]
    legacy_fingerprint = repo.legacy_fingerprint
    legacy_repo_id = repo.legacy_repo_id
    assert legacy_repo_id != repo.repo_id

    save_state(profile, {
        "repos": {
            legacy_repo_id: {
                "repo_id": legacy_repo_id,
                "path": repo.path,
                "fingerprint": legacy_fingerprint,
                "origin": repo.origin,
                "head": repo.head,
                "status": "active",
                "successful_snapshot_ids": ["old-snapshot"],
            }
        }
    })

    plan = snapshot_plan(profile)
    assert plan["repos"][0]["repo_id"] == repo.repo_id
    assert f"repo_id={repo.repo_id}" in plan["repos"][0]["args"]

    result = reconcile_repos(profile, discovered)
    state = load_state(profile)
    assert result["active_repo_ids"] == [repo.repo_id]
    assert legacy_repo_id not in state["repos"]
    assert state["repos"][repo.repo_id]["successful_snapshot_ids"] == ["old-snapshot"]


def test_reconcile_blocks_unknown_active_same_path_identity_change(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    repo_path = tmp_path / "repos" / "repo-a"
    init_git_repo(repo_path)
    repo = discover_git_repos(profile)[0]
    conflicting_fingerprint = hashlib.sha256(b"not-this-repo").hexdigest()
    conflicting_repo_id = hashlib.sha256(f"{repo.origin or repo.path}:{conflicting_fingerprint}".encode("utf-8")).hexdigest()[:16]

    save_state(profile, {
        "repos": {
            conflicting_repo_id: {
                "repo_id": conflicting_repo_id,
                "path": repo.path,
                "fingerprint": conflicting_fingerprint,
                "origin": repo.origin,
                "head": repo.head,
                "status": "active",
                "successful_snapshot_ids": ["old-snapshot"],
            }
        }
    })

    plan = snapshot_plan(profile)

    assert plan["repos"] == []
    assert plan["alerts"][0]["code"] == "active_path_identity_conflict"


def test_purge_plan_refuses_active_repo(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    init_git_repo(tmp_path / "repos" / "repo-a")
    result = reconcile_repos(profile, discover_git_repos(profile))
    repo_id = result["active_repo_ids"][0]
    try:
        purge_plan(profile, repo_id)
    except SnapshotError as exc:
        assert "active" in str(exc)
    else:
        raise AssertionError("purge_plan should refuse active repos")


def test_purge_plan_for_retired_repo_is_dry_run_first(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    init_git_repo(tmp_path / "repos" / "repo-a")
    result = reconcile_repos(profile, discover_git_repos(profile))
    repo_id = result["active_repo_ids"][0]
    state = load_state(profile)
    snapshot_id = "a" * 64
    state["repos"][repo_id]["successful_snapshot_ids"] = [snapshot_id]
    save_state(profile, state)
    retire_repo(profile, repo_id)
    plan = purge_plan(profile, repo_id)
    assert plan["dry_run_required"] is True
    assert plan["selection"] == "exact_snapshot_ids"
    assert plan["snapshot_ids"] == [snapshot_id]
    assert "--dry-run" in plan["forget_args"]
    assert "--dry-run" not in plan["destructive_args_after_confirmation"]
    assert "--tag" not in plan["forget_args"]
    assert plan["forget_args"][-2:] == ["--", snapshot_id]
    assert plan["destructive_args_after_confirmation"][-2:] == ["--", snapshot_id]


def test_snapshot_plan_cli_json(tmp_path):
    cfg_file = write_snapshot_config(tmp_path)
    init_git_repo(tmp_path / "repos" / "repo-a")
    result = CliRunner().invoke(
        cli,
        ["--config", str(cfg_file), "snapshot", "plan", "--profile", "essentials-daily", "--output", "json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["data"]["repos"][0]["repo_id"]


def test_snapshot_plan_does_not_persist_state(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    init_git_repo(tmp_path / "repos" / "repo-a")

    plan = snapshot_plan(profile)

    assert plan["repos"]
    assert not state_file(profile).exists()


def test_snapshot_init_dry_run_cli_json(tmp_path):
    cfg_file = write_snapshot_config(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "--config", str(cfg_file),
            "snapshot", "init",
            "--profile", "essentials-daily",
            "--dry-run",
            "--output", "json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"]["dry_run"] is True
    assert data["data"]["args"][-1] == "init"


def test_snapshot_commands_accept_required_options_from_input_json(tmp_path):
    cfg_file = write_snapshot_config(tmp_path)
    init_git_repo(tmp_path / "repos" / "repo-a")
    cfg = Config(config_path=str(cfg_file))
    profile = cfg.snapshot_profiles["essentials-daily"]
    repo_id = reconcile_repos(profile, discover_git_repos(profile))["active_repo_ids"][0]
    state = load_state(profile)
    state["repos"][repo_id]["successful_snapshot_ids"] = ["abc123"]
    save_state(profile, state)

    runner = CliRunner()
    commands = [
        ("plan", {"profile": "essentials-daily", "output": "json"}),
        ("init", {"profile": "essentials-daily", "dry_run": True, "output": "json"}),
        ("run", {"profile": "essentials-daily", "dry_run": True, "output": "json"}),
        (
            "check",
            {
                "profile": "essentials-daily",
                "read_data_subset": "5%",
                "dry_run": True,
                "output": "json",
            },
        ),
        (
            "restore",
            {
                "profile": "essentials-daily",
                "snapshot_id": "latest",
                "target": str(tmp_path / "restore-target"),
                "dry_run": True,
                "output": "json",
            },
        ),
        ("retire", {"profile": "essentials-daily", "repo_id": repo_id, "output": "json"}),
        ("purge-plan", {"profile": "essentials-daily", "repo_id": repo_id, "output": "json"}),
        ("schedule", {"profile": "essentials-daily", "output": "json"}),
    ]

    for command, payload in commands:
        result = runner.invoke(
            cli,
            [
                "--config", str(cfg_file),
                "snapshot", command,
                "--input-json", json.dumps(payload),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["success"] is True


def test_snapshot_command_missing_json_required_param_returns_json_error(tmp_path):
    cfg_file = write_snapshot_config(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "--config", str(cfg_file),
            "snapshot", "restore",
            "--input-json", json.dumps({"profile": "essentials-daily", "output": "json"}),
        ],
    )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["success"] is False
    assert "Missing required option: --snapshot-id" in data["errors"][0]


def test_snapshot_restore_env_target_guard_uses_expanded_path(tmp_path, monkeypatch):
    cfg_file = write_snapshot_config(tmp_path)
    target = tmp_path / "restore-target"
    target.mkdir()
    (target / "existing.txt").write_text("existing\n")
    monkeypatch.setenv("RESTORE_TARGET", str(target))

    result = CliRunner().invoke(
        cli,
        [
            "--config", str(cfg_file),
            "snapshot", "restore",
            "--profile", "essentials-daily",
            "--snapshot-id", "latest",
            "--target", "$RESTORE_TARGET",
            "--output", "json",
        ],
    )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert "Restore target is not empty" in data["errors"][0]


def test_snapshot_run_dry_run_reports_alerts_as_unsuccessful(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    init_git_repo(tmp_path / "repos" / "repo-a")
    reconcile_repos(profile, discover_git_repos(profile))

    import shutil

    shutil.rmtree(tmp_path / "repos" / "repo-a")
    result = snapshot_run(profile, dry_run=True)

    assert result["success"] is False
    assert result["alerts"][0]["code"] == "missing_without_snapshot"


def test_snapshot_run_dry_run_reports_no_targets_as_unsuccessful(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.repo_homes = []
    profile.explicit_repos = []
    profile.include_paths = []

    result = snapshot_run(profile, dry_run=True)

    assert result["success"] is False
    assert result["alerts"] == []


def test_snapshot_health_reports_profile_dependencies(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    with patch("bbackup.snapshot.shutil.which", return_value="/usr/bin/tool"), \
         patch("bbackup.snapshot.socket.gethostname", return_value="test-host"), \
         patch("bbackup.snapshot.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="restic 0.19.0\n", stderr="")
        result = check_snapshot_profile(profile)
    assert result["ok"] is True
    assert result["checks"]["restic"]["ok"] is True
    assert result["checks"]["rclone"]["ok"] is True


def test_snapshot_health_rejects_google_drive_remote_without_client_id(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]

    def run_side_effect(args, **kwargs):
        if args[:3] == ["rclone", "config", "show"]:
            return MagicMock(returncode=0, stdout="[example-drive]\ntype = drive\nscope = drive\n", stderr="")
        return MagicMock(returncode=0, stdout="restic 0.19.0\n", stderr="")

    with patch("bbackup.snapshot.shutil.which", return_value="/usr/bin/tool"), \
         patch("bbackup.snapshot.socket.gethostname", return_value="test-host"), \
         patch("bbackup.snapshot.subprocess.run", side_effect=run_side_effect):
        result = check_snapshot_profile(profile)

    assert result["ok"] is False
    assert result["checks"]["rclone_drive_client_id"]["ok"] is False
    assert "no client_id" in result["checks"]["rclone_drive_client_id"]["message"]


def test_snapshot_health_accepts_google_drive_remote_with_client_id(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]

    def run_side_effect(args, **kwargs):
        if args[:3] == ["rclone", "config", "show"]:
            return MagicMock(
                returncode=0,
                stdout="[example-drive]\ntype = drive\nclient_id = example.apps.googleusercontent.com\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="restic 0.19.0\n", stderr="")

    with patch("bbackup.snapshot.shutil.which", return_value="/usr/bin/tool"), \
         patch("bbackup.snapshot.socket.gethostname", return_value="test-host"), \
         patch("bbackup.snapshot.subprocess.run", side_effect=run_side_effect):
        result = check_snapshot_profile(profile)

    assert result["ok"] is True
    assert result["checks"]["rclone_drive_client_id"]["ok"] is True


def test_snapshot_health_accepts_default_drive_client_when_profile_allows_it(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path, "allow_default_rclone_drive_client: true")))
    profile = cfg.snapshot_profiles["essentials-daily"]

    def run_side_effect(args, **kwargs):
        if args[:3] == ["rclone", "config", "show"]:
            return MagicMock(returncode=0, stdout="[example-drive]\ntype = drive\nscope = drive\n", stderr="")
        return MagicMock(returncode=0, stdout="restic 0.19.0\n", stderr="")

    with patch("bbackup.snapshot.shutil.which", return_value="/usr/bin/tool"), \
         patch("bbackup.snapshot.socket.gethostname", return_value="test-host"), \
         patch("bbackup.snapshot.subprocess.run", side_effect=run_side_effect):
        result = check_snapshot_profile(profile)

    assert result["ok"] is True
    assert result["checks"]["rclone_drive_client_id"]["ok"] is True
    assert "explicit profile configuration" in result["checks"]["rclone_drive_client_id"]["message"]


def test_snapshot_health_reports_unsupported_engine_without_crashing(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    profile.engine = "unknown"

    with patch("bbackup.snapshot.shutil.which", return_value="/usr/bin/tool"), \
         patch("bbackup.snapshot.socket.gethostname", return_value="test-host"), \
         patch("bbackup.snapshot.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="restic 0.19.0\n", stderr="")
        result = check_snapshot_profile(profile)

    assert result["ok"] is False
    assert result["checks"]["engine"]["ok"] is False
    assert result["checks"]["repository_initialized"]["message"] == "unsupported snapshot engine"


def test_snapshot_check_enforces_google_drive_client_id_or_opt_in(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]

    def run_side_effect(args, **kwargs):
        if args[:3] == ["rclone", "config", "show"]:
            return MagicMock(returncode=0, stdout="[example-drive]\ntype = drive\nscope = drive\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("bbackup.snapshot.shutil.which", return_value="/usr/bin/tool"), \
         patch("bbackup.snapshot.subprocess.run", side_effect=run_side_effect):
        try:
            snapshot_check(profile)
        except SnapshotError as exc:
            assert "no client_id" in str(exc)
        else:
            raise AssertionError("snapshot_check should enforce Google Drive client_id")


def test_snapshot_check_preflight_rejects_wrong_hostname(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]

    def run_side_effect(args, **kwargs):
        if args[:3] == ["rclone", "config", "show"]:
            return MagicMock(
                returncode=0,
                stdout="[example-drive]\ntype = drive\nclient_id = example.apps.googleusercontent.com\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="restic 0.19.0\n", stderr="")

    with patch("bbackup.snapshot.shutil.which", return_value="/usr/bin/tool"), \
         patch("bbackup.snapshot.socket.gethostname", return_value="other-host"), \
         patch("bbackup.snapshot.subprocess.run", side_effect=run_side_effect):
        try:
            snapshot_check(profile)
        except SnapshotError as exc:
            assert "hostname" in str(exc)
        else:
            raise AssertionError("snapshot_check should reject profiles for a different host")


def test_snapshot_check_preflight_rejects_permissive_password_file(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    (tmp_path / "password").chmod(0o644)

    def run_side_effect(args, **kwargs):
        if args[:3] == ["rclone", "config", "show"]:
            return MagicMock(
                returncode=0,
                stdout="[example-drive]\ntype = drive\nclient_id = example.apps.googleusercontent.com\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="restic 0.19.0\n", stderr="")

    with patch("bbackup.snapshot.shutil.which", return_value="/usr/bin/tool"), \
         patch("bbackup.snapshot.socket.gethostname", return_value="test-host"), \
         patch("bbackup.snapshot.subprocess.run", side_effect=run_side_effect):
        try:
            snapshot_check(profile)
        except SnapshotError as exc:
            assert "password file must not be group/world accessible" in str(exc)
        else:
            raise AssertionError("snapshot_check should reject permissive password files")


def test_schedule_units_include_matching_services_for_all_timers(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(
        tmp_path,
        (
            'schedule:\n'
            '              daily_time: "02:15"\n'
            '              maintenance_time: "Sun 04:30"\n'
            '              verification_time: "monthly"\n'
            '              verification_read_data_subset: "10%"'
        ),
    )))
    profile = cfg.snapshot_profiles["essentials-daily"]
    from bbackup.snapshot import schedule_units

    units = schedule_units(profile, config_path=str(tmp_path / "custom-config.yaml"))

    assert "bbackup-test-host-essentials-daily.service" in units
    assert "bbackup-test-host-essentials-daily.timer" in units
    assert "bbackup-test-host-essentials-daily-maintenance.service" in units
    assert "bbackup-test-host-essentials-daily-maintenance.timer" in units
    assert "bbackup-test-host-essentials-daily-verification.service" in units
    assert "bbackup-test-host-essentials-daily-verification.timer" in units
    assert "--config" in units["bbackup-test-host-essentials-daily.service"]
    assert "custom-config.yaml snapshot run --profile essentials-daily" in units[
        "bbackup-test-host-essentials-daily.service"
    ]
    assert "snapshot check --profile essentials-daily\n" in units[
        "bbackup-test-host-essentials-daily-maintenance.service"
    ]
    assert "--read-data-subset 10%%" in units["bbackup-test-host-essentials-daily-verification.service"]
    expected_lock = "flock --exclusive %t/bbackup-test-host-essentials-daily.lock"
    for service_name in (
        "bbackup-test-host-essentials-daily.service",
        "bbackup-test-host-essentials-daily-maintenance.service",
        "bbackup-test-host-essentials-daily-verification.service",
    ):
        assert expected_lock in units[service_name]


def test_schedule_units_escape_default_percent_in_verification_subset(tmp_path):
    cfg = Config(config_path=str(write_snapshot_config(tmp_path)))
    profile = cfg.snapshot_profiles["essentials-daily"]
    from bbackup.snapshot import schedule_units

    units = schedule_units(profile)

    assert "--read-data-subset 5%%" in units["bbackup-test-host-essentials-daily-verification.service"]
