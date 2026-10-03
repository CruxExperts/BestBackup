"""Validate bbackup's public GitHub Markdown profile.

The checker is intentionally dependency-free so it can run before the project
environment is installed. It validates the structures that are easiest to
break during documentation edits: fenced blocks, visible headings, local
links and images, alerts, approved HTML boundaries, and public-surface
provenance metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_DOCUMENTS = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "INSTALL.md",
    "QUICKSTART.md",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
}
PUBLIC_DIRECTORIES = (".github", "docs", "scripts")
GENERATED_DOCUMENTS = ("bbackup/data/cli-skills.md",)
HEADING_REPEAT_EXCEPTIONS = {
    # Keep a Changelog repeats only these category headings under each version.
    "CHANGELOG.md": {"added", "changed", "deprecated", "removed", "fixed", "security"},
}
TEMPLATE_DOCUMENTS = {
    ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
    ".github/pull_request_template.md",
}
STANDARD_DOCUMENTS = {
    "docs/standards/github-markdown/github-markdown-capabilities-reference.md",
    "docs/standards/github-markdown/github-markdown-writing-standard.md",
    "docs/standards/github-markdown/github-markdown-review-checklist.md",
    "docs/standards/github-markdown/github-markdown-source-provenance.md",
}
PROVENANCE_MANIFEST = "docs/standards/github-markdown/github-markdown-provenance-manifest.json"
DISALLOWED_HTML = re.compile(r"<\s*(?:script|style|iframe)\b", re.IGNORECASE)
LIQUID_SYNTAX = re.compile(r"\{\{|\{%|\{%[-+]?|\{%[-+]?\s*end", re.IGNORECASE)
ALERT = re.compile(r"^> \[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]$")
FENCE = re.compile(r"^ {0,3}(`{3,})(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CHANGELOG_VERSION_HEADING = re.compile(
    r"^\[((?:Unreleased|\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?))\](?:\s+-\s+.+)?$"
)
INLINE_LINK = re.compile(r"!?\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")
MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(")
HTML_IMAGE = re.compile(
    r"<img\b[^>]*\balt\s*=\s*([\"'])(.*?)\1[^>]*\bsrc\s*=\s*([\"'])(.*?)\3"
    r"|<img\b[^>]*\bsrc\s*=\s*([\"'])(.*?)\5[^>]*\balt\s*=\s*([\"'])(.*?)\7",
    re.IGNORECASE,
)
CUSTOM_ANCHOR = re.compile(
    r"<a\b[^>]*\b(?:name|id)\s*=\s*([\"'])([^\"']+)\1[^>]*>\s*</a>",
    re.IGNORECASE,
)


def github_anchor(title: str) -> str:
    """Return the repository-file anchor form used by GitHub headings."""

    title = re.sub(r"<[^>]*>", "", title).strip().lower()
    title = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE)
    return re.sub(r"\s+", "-", title)


def heading_anchors(path: Path) -> set[str]:
    """Collect visible GitHub heading anchors from a Markdown file."""

    headings: list[str] = []
    custom_anchors: set[str] = set()
    fence_length: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        fence_match = FENCE.match(line)
        if fence_match:
            ticks = len(fence_match.group(1))
            info = fence_match.group(2).strip()
            if fence_length is None:
                fence_length = ticks
            elif ticks >= fence_length and not info:
                fence_length = None
            continue
        if fence_length is None:
            custom_anchor_match = CUSTOM_ANCHOR.search(line)
            if custom_anchor_match:
                custom_anchors.add(custom_anchor_match.group(2))
            heading_match = HEADING.match(line)
            if heading_match:
                headings.append(heading_match.group(2))

    anchors: set[str] = set(custom_anchors)
    counts: dict[str, int] = {}
    for title in headings:
        slug = github_anchor(title)
        if not slug:
            continue
        suffix = counts.get(slug, 0)
        anchor = slug if suffix == 0 else f"{slug}-{suffix}"
        anchors.add(anchor)
        counts[slug] = suffix + 1
    return anchors


def public_documents() -> list[Path]:
    paths = [REPO_ROOT / relative for relative in sorted(ROOT_DOCUMENTS)]
    for directory in PUBLIC_DIRECTORIES:
        root = REPO_ROOT / directory
        if root.exists():
            paths.extend(sorted(root.rglob("*.md")))
    paths.extend(REPO_ROOT / relative for relative in GENERATED_DOCUMENTS)
    unique = {path.resolve(): path for path in paths if path.exists()}
    return [unique[key] for key in sorted(unique)]


def relative_name(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def add_error(errors: list[str], path: Path, line: int, message: str) -> None:
    errors.append(f"{relative_name(path)}:{line}: {message}")


def is_template(path: Path) -> bool:
    return relative_name(path) in TEMPLATE_DOCUMENTS


def scan_file(path: Path) -> list[str]:
    errors: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    headings: list[tuple[int, int, str]] = []
    custom_anchor_lines: dict[str, int] = {}
    local_links: list[tuple[int, str, Path, str]] = []
    fence_length: int | None = None
    fence_start: int | None = None
    alert_lines: list[int] = []
    previous_alert = False
    details_open = 0

    if lines and lines[0].strip() == "---":
        closing = next((index for index, line in enumerate(lines[1:], 2) if line.strip() == "---"), None)
        if closing is None:
            add_error(errors, path, 1, "front matter is not closed")
        else:
            for number, line in enumerate(lines[1 : closing - 1], 2):
                if line.strip() and not line[0].isspace() and not re.match(r"^[A-Za-z_][\w-]*\s*:", line):
                    add_error(errors, path, number, "front matter line is not a top-level key")

    for number, line in enumerate(lines, 1):
        fence_match = FENCE.match(line)
        if fence_match:
            ticks = len(fence_match.group(1))
            info = fence_match.group(2).strip()
            if fence_length is None:
                fence_length = ticks
                fence_start = number
                if not info:
                    add_error(errors, path, number, "fenced code block needs a lower-case language identifier")
                elif info.split()[0] != info.split()[0].lower():
                    add_error(errors, path, number, "fenced code language identifier must be lower-case")
            elif ticks >= fence_length and not info:
                fence_length = None
                fence_start = None
            continue

        if fence_length is not None:
            continue
        custom_anchor_match = CUSTOM_ANCHOR.search(line)
        if custom_anchor_match:
            anchor = custom_anchor_match.group(2)
            if anchor in custom_anchor_lines:
                add_error(errors, path, number, f"custom anchor is duplicated: {anchor}")
            elif not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", anchor):
                add_error(errors, path, number, "custom anchor must use lowercase ASCII letters, digits, and hyphens")
            custom_anchor_lines.setdefault(anchor, number)

        heading_match = HEADING.match(line)
        if heading_match:
            headings.append((number, len(heading_match.group(1)), heading_match.group(2)))

        alert_match = ALERT.match(line)
        if alert_match:
            alert_lines.append(number)
            if previous_alert:
                add_error(errors, path, number, "alerts must not be consecutive")
            previous_alert = True
        elif previous_alert and not re.match(r"^\s*>", line):
            previous_alert = False

        if re.match(r"^\s*<details\b", line, re.IGNORECASE):
            details_open += 1
        if re.match(r"^\s*</details>\s*$", line, re.IGNORECASE):
            details_open -= 1
            if details_open < 0:
                add_error(errors, path, number, "closing details tag has no matching opener")

        prose_line = re.sub(r"`+[^`]*`+", "", line)
        if DISALLOWED_HTML.search(prose_line):
            add_error(errors, path, number, "disallowed raw HTML tag")
        if LIQUID_SYNTAX.search(prose_line):
            add_error(errors, path, number, "GitHub Docs Liquid syntax is not allowed")
        for image_match in MARKDOWN_IMAGE.finditer(line):
            if not image_match.group(1).strip():
                add_error(errors, path, number, "image is missing useful alt text")

        for link_match in INLINE_LINK.finditer(line):
            target = link_match.group(1).strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "data:")):
                continue
            parsed = urlsplit(target)
            target_path = unquote(parsed.path)
            if target_path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", target_path):
                add_error(errors, path, number, "local link target must be relative")
                continue
            resolved = (path.parent / target_path).resolve() if target_path else path.resolve()
            if target_path and not resolved.exists():
                add_error(errors, path, number, f"relative link target does not exist: {target}")
            elif parsed.fragment:
                local_links.append((number, target, resolved, unquote(parsed.fragment)))

        for image_match in HTML_IMAGE.finditer(line):
            groups = image_match.groups()
            if groups[1] is not None:
                alt, target = groups[1], groups[3]
            else:
                target, alt = groups[5], groups[7]
            if not alt.strip():
                add_error(errors, path, number, f"image is missing useful alt text: {target}")
            if target.startswith(("http://", "https://", "data:")):
                continue
            if not (path.parent / unquote(urlsplit(target).path)).resolve().exists():
                add_error(errors, path, number, f"image target does not exist: {target}")
    for number, target, resolved, fragment in local_links:
        if resolved.suffix.lower() != ".md" or not resolved.exists():
            continue
        if fragment not in heading_anchors(resolved):
            add_error(errors, path, number, f"heading fragment does not exist: {target}")


    if fence_length is not None:
        add_error(errors, path, fence_start or len(lines), "fenced code block is not closed")
    if details_open:
        add_error(errors, path, len(lines), "details tag is not closed")
    if len(alert_lines) > 2:
        add_error(errors, path, alert_lines[2], "a document may contain no more than two alerts")
    relative = relative_name(path)
    repeatable_headings = HEADING_REPEAT_EXCEPTIONS.get(relative, set())
    seen_heading_anchors: dict[str, set[str | None]] = {}
    version_context: str | None = None
    for number, level, title in headings:
        if relative == "CHANGELOG.md" and level == 2:
            version_match = CHANGELOG_VERSION_HEADING.fullmatch(title)
            version_context = version_match.group(1) if version_match else None
        anchor = github_anchor(title)
        if not anchor:
            continue
        prior_contexts = seen_heading_anchors.setdefault(anchor, set())
        if anchor in repeatable_headings:
            if version_context is None:
                add_error(errors, path, number, f"release category heading must be under a version heading: {anchor}")
            elif version_context in prior_contexts:
                add_error(errors, path, number, f"heading anchor is duplicated in the same changelog version: {anchor}")
            elif None in prior_contexts:
                add_error(errors, path, number, f"release category heading follows an unversioned duplicate: {anchor}")
        elif prior_contexts:
            add_error(errors, path, number, f"heading anchor is duplicated: {anchor}")
        prior_contexts.add(version_context)

    h1 = [item for item in headings if item[1] == 1]
    if not is_template(path) and len(h1) != 1:
        add_error(errors, path, h1[0][0] if h1 else 1, "document must contain exactly one visible H1")
    previous_level = 0
    for number, level, title in headings:
        if previous_level and level > previous_level + 1:
            add_error(errors, path, number, "heading level skips a nesting level")
        previous_level = level

    return errors


def check_standard_metadata(errors: list[str]) -> None:
    for relative in STANDARD_DOCUMENTS:
        path = REPO_ROOT / relative
        text = path.read_text(encoding="utf-8")
        for field, value in (("status", '"active"'), ("sensitivity", '"public"'), ("license", '"not specified"')):
            if f"{field}: {value}" not in text:
                errors.append(f"{relative}: missing adopted metadata {field}: {value}")
    provenance = REPO_ROOT / "docs/standards/github-markdown/github-markdown-source-provenance.md"
    provenance_text = provenance.read_text(encoding="utf-8")
    hashes = re.findall(r"`([0-9a-f]{64})`", provenance_text)
    if len(hashes) < 4:
        errors.append(f"{relative_name(provenance)}: source input SHA-256 register is incomplete")

def check_provenance_manifest(errors: list[str]) -> None:
    manifest_path = REPO_ROOT / PROVENANCE_MANIFEST
    if not manifest_path.exists():
        errors.append(f"{PROVENANCE_MANIFEST}: provenance manifest is missing")
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{PROVENANCE_MANIFEST}: invalid JSON ({exc})")
        return
    if len(manifest.get("source_inputs", [])) != 4:
        errors.append(f"{PROVENANCE_MANIFEST}: expected four source input records")
    outputs = manifest.get("adapted_outputs", [])
    if len(outputs) != 4:
        errors.append(f"{PROVENANCE_MANIFEST}: expected four adapted output records")
    for output in outputs:
        relative = output.get("path")
        expected = output.get("sha256_after_adaptation")
        if not relative:
            errors.append(f"{PROVENANCE_MANIFEST}: adapted output is missing a path")
            continue
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            errors.append(f"{PROVENANCE_MANIFEST}: missing or invalid checksum for {relative}")
            continue
        path = REPO_ROOT / relative
        if not path.exists():
            errors.append(f"{PROVENANCE_MANIFEST}: output does not exist: {relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"{PROVENANCE_MANIFEST}: checksum mismatch for {relative}")


def main() -> int:
    errors: list[str] = []
    for path in public_documents():
        errors.extend(scan_file(path))
    check_standard_metadata(errors)
    check_provenance_manifest(errors)

    home = str(Path.home())
    repo = str(REPO_ROOT)
    for path in public_documents():
        text = path.read_text(encoding="utf-8")
        for forbidden in (home, repo):
            if forbidden and forbidden in text:
                errors.append(f"{relative_name(path)}: contains machine-specific path {forbidden}")

    if errors:
        print("Markdown standards check failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    print(f"Markdown standards check passed for {len(public_documents())} public files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
