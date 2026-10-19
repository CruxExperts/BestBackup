import pytest

from scripts.verify_release_source import select_ci_run


def trusted_run(**changes):
    return dict({"id": 123, "head_sha": "a" * 40, "head_branch": "main", "event": "push",
                 "status": "completed", "conclusion": "success", "workflow_id": 456,
                 "path": ".github/workflows/ci.yml", "head_repository": {"full_name": "example/project"}}, **changes)


@pytest.mark.parametrize("changes", [
    {"head_sha": "b" * 40}, {"head_branch": "feature"}, {"event": "pull_request"},
    {"conclusion": "failure"}, {"status": "in_progress"}, {"workflow_id": 789},
    {"path": ".github/workflows/other.yml"}, {"head_repository": {"full_name": "fork/project"}},
])
def test_release_rejects_untrusted_or_different_commit_evidence(changes):
    with pytest.raises(ValueError, match="exact release commit"):
        select_ci_run([trusted_run(**changes)], sha="a" * 40, branch="main",
                      workflow_id=456, repository="example/project")


def test_release_selects_exact_successful_push_evidence():
    assert select_ci_run([trusted_run(event="pull_request"), trusted_run()],
                         sha="a" * 40, branch="main", workflow_id=456,
                         repository="example/project") == 123
