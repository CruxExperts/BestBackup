"""Require signed release ancestry and exact-commit trusted CI evidence."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

FINGERPRINT = "AF7968466B5B39C5E928FEFE716342D3EFBA5522"


def api(path: str):
    result = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def select_ci_run(runs, *, sha: str, branch: str, workflow_id: int, repository: str):
    for run in runs:
        if (run.get("head_sha") == sha and run.get("head_branch") == branch
                and run.get("event") == "push" and run.get("status") == "completed"
                and run.get("conclusion") == "success" and run.get("workflow_id") == workflow_id
                and run.get("path") == ".github/workflows/ci.yml"
                and run.get("head_repository", {}).get("full_name") == repository):
            return run["id"]
    raise ValueError("No successful trusted default-branch CI run for the exact release commit")


def verify(repository: str, tag: str, checkout: Path) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+_-]+)?", tag):
        raise ValueError("Invalid release tag")
    metadata = api(f"repos/{repository}")
    branch = metadata["default_branch"]
    sha = api(f"repos/{repository}/commits/{tag}")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid release commit")
    def git(*args, env=None):
        return subprocess.run(["git", "-C", str(checkout), *args], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()
    git("fetch", "--no-tags", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}")
    git("fetch", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    if git("rev-parse", f"refs/tags/{tag}^{{commit}}") != sha:
        raise ValueError("Tag moved during verification")
    git("merge-base", "--is-ancestor", sha, f"refs/remotes/origin/{branch}")
    if git("cat-file", "-t", f"refs/tags/{tag}") != "tag":
        raise ValueError("Release requires an annotated OpenPGP-signed tag")
    with tempfile.TemporaryDirectory(prefix="bbackup-tag-verification-") as directory:
        environment = dict(os.environ, GNUPGHOME=directory)
        subprocess.run(["gpg", "--batch", "--homedir", directory, "--import",
                        str(checkout / ".github/release-signing-key.asc")],
                       env=environment, capture_output=True, check=True)
        result = subprocess.run(["git", "-C", str(checkout), "-c", "gpg.format=openpgp",
                                 "-c", "gpg.program=gpg", "verify-tag", "--raw", tag],
                                env=environment, text=True, capture_output=True, check=True)
        valid = [line.split() for line in result.stderr.splitlines()
                 if line.startswith("[GNUPG:] VALIDSIG ")]
        if not any(len(fields) >= 11 and FINGERPRINT in (fields[2], fields[-1])
                   and fields[9] in ("8", "9", "10") for fields in valid):
            raise ValueError("Tag signature does not match the pinned release key")
    workflow = api(f"repos/{repository}/actions/workflows/ci.yml")
    runs = api(f"repos/{repository}/actions/workflows/{workflow['id']}/runs?head_sha={sha}&event=push&status=success&per_page=100")
    run_id = select_ci_run(runs["workflow_runs"], sha=sha, branch=branch,
                           workflow_id=workflow["id"], repository=repository)
    jobs = api(f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    required = [job for job in jobs["jobs"] if job["name"] == "CI required"]
    if jobs["total_count"] > 100 or len(required) != 1 or required[0]["conclusion"] != "success":
        raise ValueError("Exact-commit aggregate CI evidence is missing or unsuccessful")
    return {"tag": tag, "commit": sha, "ci_run_id": run_id, "verified": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--checkout", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.repository, args.tag, args.checkout), sort_keys=True))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit("Release source verification failed: require a signed annotated tag on default-branch history and successful exact-commit CI.") from None
