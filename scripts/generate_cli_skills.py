"""
scripts/generate_cli_skills.py
Purpose: Generate the CLI skills catalog in docs/cli-skills.md (and a compact
         index JSON) from the unified bbackup/bbman CLI metadata layer.
Created: 2026-03-04
Last Updated: 2026-03-04
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bbackup import __version__ as BBACKUP_VERSION  # noqa: E402
from bbackup.cli_metadata import CliCommand, get_command_registry, PRODUCTION_COMMANDS, production_schema  # noqa: E402

DOC_PATH = REPO_ROOT / "docs" / "cli-skills.md"
INDEX_PATH = REPO_ROOT / "docs" / "cli-skills-index.json"
PACKAGE_DOC_PATH = REPO_ROOT / "bbackup" / "data" / "cli-skills.md"
PACKAGE_INDEX_PATH = REPO_ROOT / "bbackup" / "data" / "cli-skills-index.json"


def _render_header(lines: List[str]) -> None:
    lines.append("# CLI skills catalog")
    lines.append("")
    lines.append(
        f"> Generated from the bbackup/bbman CLI metadata. "
        f"Version: {BBACKUP_VERSION}. This catalog is authoritative for this version."
    )
    lines.append(
        "> Source: `bbackup/cli_metadata.py` rendered by `scripts/generate_cli_skills.py`; "
        "run the generator's `--check` mode before publishing."
    )
    lines.append("")


def _render_command_section(lines: List[str], cmd: CliCommand) -> None:
    # Heading pattern is stable and indexable
    lines.append(f"### {cmd.cli} {cmd.name}")
    lines.append("")
    lines.append(f"Summary: {cmd.summary}")
    lines.append("")
    if cmd.description:
        lines.append(cmd.description)
        lines.append("")

    cli_params = [p for p in cmd.parameters if p.kind in ("flag", "positional")]
    json_params = [p for p in cmd.parameters if p.kind == "json_field"]
    env_params = [p for p in cmd.parameters if p.kind == "env_var"]

    if cli_params:
        lines.append(f"#### CLI parameters for `{cmd.cli} {cmd.name}`")
        lines.append("")
        lines.append("| Name | Type | Required | Default | Description |")
        lines.append("|:--|:--|:--:|:--|:--|")
        for p in cli_params:
            name = p.cli_flag or p.name
            default = "" if p.default is None else repr(p.default)
            required = "yes" if p.required else "no"
            desc = p.description or ""
            lines.append(f"| `{name}` | `{p.type}` | {required} | `{default}` | {desc} |")
        lines.append("")

    if json_params or env_params:
        lines.append(f"#### JSON and environment parameters for `{cmd.cli} {cmd.name}`")
        lines.append("")
        lines.append("| Name | Kind | Type | Required | Default | Description |")
        lines.append("|:--|:--|:--|:--:|:--|:--|")
        for p in json_params:
            name = p.json_key or p.name
            default = "" if p.default is None else repr(p.default)
            required = "yes" if p.required else "no"
            desc = p.description or ""
            lines.append(f"| `{name}` | json | `{p.type}` | {required} | `{default}` | {desc} |")
        for p in env_params:
            name = p.env_var or p.name
            default = "" if p.default is None else repr(p.default)
            required = "yes" if p.required else "no"
            desc = p.description or ""
            lines.append(f"| `{name}` | env | `{p.type}` | {required} | `{default}` | {desc} |")
        lines.append("")

    if cmd.examples:
        lines.append(f"#### Examples for `{cmd.cli} {cmd.name}`")
        lines.append("")
        for ex in cmd.examples:
            lines.append(f"- {ex.description}")
            if ex.cli:
                lines.append("")
                lines.append("  ```bash")
                lines.append(f"  {ex.cli}")
                lines.append("  ```")
            if ex.input_json is not None:
                lines.append("")
                lines.append("  ```bash")
                cli_name = "bbackup" if cmd.cli == "bbackup" else "bbman"
                json_str = json.dumps(ex.input_json, separators=(",", ":"))
                lines.append(
                    f"  {cli_name} {cmd.name} --input-json '{json_str}' --output json"
                )
                lines.append("  ```")
            lines.append("")


def generate_markdown_and_index() -> Dict[str, Dict[str, int]]:
    lines: List[str] = []
    index: Dict[str, Dict[str, int]] = {}

    _render_header(lines)

    for cli in ("bbackup", "bbman"):
        registry = get_command_registry(cli)  # type: ignore[arg-type]
        if not registry:
            continue
        lines.append(f"## {cli}")
        lines.append("")

        # Sort commands by name for stable ordering
        for cmd in sorted(registry.values(), key=lambda c: c.name):
            start_line = len(lines) + 1  # 1-based
            _render_command_section(lines, cmd)
            index[cmd.id] = {"start": start_line, "end": 0}

    # Compute end line for each section
    # Sort by start line, then assign end as next start - 1
    sorted_items = sorted(index.items(), key=lambda kv: kv[1]["start"])
    for i, (cmd_id, meta) in enumerate(sorted_items):
        if i + 1 < len(sorted_items):
            next_start = sorted_items[i + 1][1]["start"]
        else:
            next_start = len(lines) + 1
        meta["end"] = next_start - 1
        index[cmd_id] = meta

    content = "\n".join(lines).rstrip() + "\n"
    index_content = json.dumps(index, indent=2, sort_keys=True) + "\n"
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    PACKAGE_DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(content, encoding="utf-8")
    INDEX_PATH.write_text(index_content, encoding="utf-8")
    PACKAGE_DOC_PATH.write_text(content, encoding="utf-8")
    PACKAGE_INDEX_PATH.write_text(index_content, encoding="utf-8")

    return index


def check_up_to_date() -> bool:
    if not DOC_PATH.exists() or not INDEX_PATH.exists():
        return False
    if not PACKAGE_DOC_PATH.exists() or not PACKAGE_INDEX_PATH.exists():
        return False

    # Generate into memory and compare (normalize newlines and trailing blanks)
    text_before = DOC_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    package_text_before = PACKAGE_DOC_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        index_before = json.load(f)
    with open(PACKAGE_INDEX_PATH, "r", encoding="utf-8") as f:
        package_index_before = json.load(f)

    # Temporarily generate to a temp structure
    lines: List[str] = []
    index: Dict[str, Dict[str, int]] = {}
    _render_header(lines)
    for cli in ("bbackup", "bbman"):
        registry = get_command_registry(cli)  # type: ignore[arg-type]
        if not registry:
            continue
        lines.append(f"## {cli}")
        lines.append("")
        for cmd in sorted(registry.values(), key=lambda c: c.name):
            start_line = len(lines) + 1
            _render_command_section(lines, cmd)
            index[cmd.id] = {"start": start_line, "end": 0}
    sorted_items = sorted(index.items(), key=lambda kv: kv[1]["start"])
    for i, (cmd_id, meta) in enumerate(sorted_items):
        if i + 1 < len(sorted_items):
            next_start = sorted_items[i + 1][1]["start"]
        else:
            next_start = len(lines) + 1
        meta["end"] = next_start - 1
        index[cmd_id] = meta

    text_after = "\n".join(lines).replace("\r\n", "\n").rstrip("\n")
    if text_after != text_before:
        return False
    if text_after != package_text_before:
        return False
    if index != index_before:
        return False
    if index != package_index_before:
        return False
    return True


def production_artifacts() -> dict[Path, str]:
    """Render the preview command contract from the same definitions as Click."""
    schema = {name: production_schema(name) for name in PRODUCTION_COMMANDS}
    lines = ["# Production preview command reference", "",
             "> Generated by `scripts/generate_cli_skills.py` from `bbackup/cli_metadata.py`.", "",
             "These commands are a development preview, not a qualified 2.0 release.", "",
             "Prefix commands with `bbackup production --config CONFIG --bindings BINDINGS`.", "",
             "All operation commands emit one schema-version-2 JSON result. Use `--schema` for the input contract.", ""]
    for name, definition in PRODUCTION_COMMANDS.items():
        lines.extend([f"## `{name}`", "", definition["summary"], ""])
        for field, kind in definition["fields"].items():
            lines.append(f"- `--{field.replace('_', '-')}`: {kind}.")
        lines.append("")
    return {REPO_ROOT / "docs" / "production-cli.md": "\n".join(lines).rstrip() + "\n",
            REPO_ROOT / "bbackup" / "data" / "production-schemas.json": json.dumps(schema, indent=2, sort_keys=True) + "\n"}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/cli-skills.md and docs/cli-skills-index.json from CLI metadata."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write files; exit non-zero if generation would change anything.",
    )
    args = parser.parse_args(argv)

    if args.check:
        ok = check_up_to_date()
        ok = ok and all(path.exists() and path.read_text(encoding="utf-8") == content
                        for path, content in production_artifacts().items())
        if not ok:
            sys.stderr.write("cli-skills docs are out of date. Run without --check to regenerate.\n")
            return 1
        return 0

    generate_markdown_and_index()
    for path, content in production_artifacts().items():
        path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
