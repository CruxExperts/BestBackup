"""Source distributions retain portable build/test inputs and omit private state."""
import io
from pathlib import Path
import tarfile

import pytest

from scripts.smoke_installed_artifact import REPO_ROOT, SOURCE_INPUTS, verify_source_archive


def _archive(path: Path, names: set[str]) -> None:
    root = "bbackup-" + (REPO_ROOT / "VERSION").read_text().strip()
    with tarfile.open(path, "w:gz") as stream:
        for name in sorted(names):
            member = tarfile.TarInfo(f"{root}/{name}")
            member.size = 1
            stream.addfile(member, io.BytesIO(b"x"))


def test_source_archive_requires_complete_portable_inputs(tmp_path):
    archive = tmp_path / "source.tar.gz"
    _archive(archive, SOURCE_INPUTS)
    verify_source_archive(archive)
    _archive(archive, SOURCE_INPUTS - {"scripts/build_deb.py"})
    with pytest.raises(ValueError, match="missing portable inputs"):
        verify_source_archive(archive)


@pytest.mark.parametrize("private", [".agents/state/record.json", ".venv/bin/python", "private.key", "run.log"])
def test_source_archive_refuses_private_state(tmp_path, private):
    archive = tmp_path / "source.tar.gz"
    _archive(archive, SOURCE_INPUTS | {private})
    with pytest.raises(ValueError, match="private or generated"):
        verify_source_archive(archive)
