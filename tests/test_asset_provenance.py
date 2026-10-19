import json
from pathlib import Path
import shutil

from scripts.check_assets import check_assets, jpeg_metadata, png_metadata


def test_active_artwork_has_complete_embedded_provenance():
    assert check_assets() == []


def test_changed_or_missing_artwork_is_detected(tmp_path):
    source = Path(__file__).resolve().parents[1] / "docs/assets"
    target = tmp_path / "docs/assets"
    target.mkdir(parents=True)
    manifest = json.loads((source / "provenance.json").read_text())
    for asset in manifest["assets"]:
        shutil.copyfile(source / Path(asset["path"]).name, tmp_path / asset["path"])
    (target / "provenance.json").write_text(json.dumps(manifest))
    assert check_assets(tmp_path) == []
    path = tmp_path / manifest["assets"][0]["path"]
    path.write_bytes(path.read_bytes()[:-12])
    errors = check_assets(tmp_path)
    assert any("content differs" in error for error in errors)
    assert any("Missing PNG end marker" in error for error in errors)


def test_png_rejects_checksum_damage():
    import pytest
    path = Path(__file__).resolve().parents[1] / "docs/assets/bbackup-banner-v2.png"
    data = bytearray(path.read_bytes())
    data[20] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        png_metadata(bytes(data))


def test_social_upload_meets_dimensions_size_and_embedded_rights():
    import pytest
    path = Path(__file__).resolve().parents[1] / "docs/assets/bbackup-social-v2.jpg"
    data = path.read_bytes()
    dimensions, metadata = jpeg_metadata(data)
    assert dimensions == (1774, 887)
    assert len(data) < 1_000_000
    assert metadata["Copyright"] == "Copyright © 2026 Crux Experts LLC"
    assert metadata["Website"] == "https://www.cruxexperts.com/"
    assert metadata["UsageTerms"].startswith("Private asset.")
    with pytest.raises(ValueError, match="complete JPEG"):
        jpeg_metadata(data[:-2])
