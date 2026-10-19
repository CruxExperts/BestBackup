"""Verify active documentation artwork and its embedded, portable provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
RIGHTS = "Private asset. Property of Crux Experts LLC. Use, reproduction, modification, distribution, or publication without prior written permission is prohibited."


def png_metadata(data: bytes) -> tuple[tuple[int, int], dict[str, str]]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Expected a PNG image")
    dimensions = (0, 0)
    metadata = {}
    offset = 8
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("Truncated PNG chunk")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        end = offset + 12 + length
        if end > len(data) or zlib.crc32(kind + payload) & 0xffffffff != struct.unpack(">I", data[end - 4:end])[0]:
            raise ValueError("Invalid PNG chunk checksum")
        if kind == b"IHDR":
            dimensions = struct.unpack(">II", payload[:8])
        elif kind == b"tEXt":
            key, value = payload.split(b"\0", 1)
            metadata[key.decode("latin-1")] = value.decode("latin-1")
        if kind == b"IEND":
            if end != len(data):
                raise ValueError("Unexpected content after PNG")
            return dimensions, metadata
        offset = end
    raise ValueError("Missing PNG end marker")


def jpeg_metadata(data: bytes) -> tuple[tuple[int, int], dict[str, str]]:
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("Expected a complete JPEG image")
    offset, dimensions, metadata = 2, (0, 0), {}
    while offset + 4 <= len(data):
        if data[offset] != 0xff:
            raise ValueError("Invalid JPEG marker")
        marker = data[offset + 1]
        if marker == 0xda:  # The compressed scan follows all header metadata.
            break
        size = int.from_bytes(data[offset + 2:offset + 4], "big")
        if size < 2 or offset + 2 + size > len(data):
            raise ValueError("Invalid JPEG segment")
        payload = data[offset + 4:offset + 2 + size]
        if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
            if len(payload) < 5:
                raise ValueError("Invalid JPEG dimensions")
            dimensions = (int.from_bytes(payload[3:5], "big"), int.from_bytes(payload[1:3], "big"))
        prefix = b"http://ns.adobe.com/xap/1.0/\0"
        if marker == 0xe1 and payload.startswith(prefix):
            document = ElementTree.fromstring(payload[len(prefix):])
            rights = document.find(".//{http://purl.org/dc/elements/1.1/}rights")
            if rights is not None:
                lines = "".join(rights.itertext()).strip().splitlines()
                lines = [line.strip() for line in lines if line.strip()]
                if len(lines) == 3:
                    metadata = dict(zip(("Copyright", "Website", "UsageTerms"), lines))
        offset += 2 + size
    if dimensions == (0, 0):
        raise ValueError("JPEG dimensions are missing")
    return dimensions, metadata


def check_assets(root: Path = ROOT) -> list[str]:
    errors = []
    manifest = json.loads((root / "docs/assets/provenance.json").read_text())
    for asset in manifest["assets"]:
        relative = Path(asset["path"])
        if relative.is_absolute() or ".." in relative.parts:
            errors.append("Asset paths must remain inside the repository")
            continue
        path = root / relative
        if path.is_symlink() or not path.is_file():
            errors.append(f"{relative}: missing regular asset")
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset["sha256"]:
            errors.append(f"{relative}: content differs from provenance record")
        try:
            dimensions, metadata = jpeg_metadata(data) if relative.suffix == ".jpg" else png_metadata(data)
            if asset.get("purpose") == "GitHub social upload" and len(data) >= 1_000_000:
                errors.append(f"{relative}: GitHub social preview must be under 1 MB")
            if list(dimensions) != asset["dimensions"]:
                errors.append(f"{relative}: dimensions differ from provenance record")
            for key, value in {
                "Copyright": "Copyright © 2026 Crux Experts LLC",
                "Website": "https://www.cruxexperts.com/",
                "UsageTerms": RIGHTS,
            }.items():
                if metadata.get(key) != value:
                    errors.append(f"{relative}: missing or incorrect {key} metadata")
        except (ValueError, UnicodeError, struct.error, ElementTree.ParseError) as error:
            errors.append(f"{relative}: {error}")
        if not asset.get("prompt") or not asset.get("generator"):
            errors.append(f"{relative}: missing generation provenance")
    return errors


if __name__ == "__main__":
    failures = check_assets()
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print("Asset provenance check passed.")
    raise SystemExit(bool(failures))
