#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HEADER_RE = re.compile(r"(?mi)^\s*(?::cl:|🆑)\s*$")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HEADING_RE = re.compile(r"^#{1,6}\s")

SECTION_LABELS = {
    "добавлено": "Add",
    "удалено": "Remove",
    "изменено": "Tweak",
    "исправлено": "Fix",
    "add": "Add",
    "remove": "Remove",
    "removed": "Remove",
    "tweak": "Tweak",
    "changed": "Tweak",
    "change": "Tweak",
    "fix": "Fix",
    "fixed": "Fix",
}

SECTION_TITLES = {
    "Add": "🆕 Добавлено",
    "Remove": "❌ Удалено",
    "Tweak": "🛠️ Изменено",
    "Fix": "🐛 Исправлено",
}

SECTION_ORDER = ("Add", "Remove", "Tweak", "Fix")
DISCORD_FIELD_LIMIT = 1024


def load_body(args: argparse.Namespace) -> str:
    if args.body_base64 is not None:
        return base64.b64decode(args.body_base64).decode("utf-8", errors="replace")

    if args.body_file is not None:
        raw = Path(args.body_file).read_bytes()
        for encoding in ("utf-8", "utf-8-sig", "cp1251"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue

        return raw.decode("utf-8", errors="replace")

    raise ValueError("Either --body-base64 or --body-file must be provided.")


def clean_body(body: str) -> str:
    return COMMENT_RE.sub("", body).replace("\r\n", "\n").replace("\r", "\n")


def normalize_section(label: str) -> str | None:
    key = re.sub(r"\s+", " ", label.strip().lower())
    return SECTION_LABELS.get(key)


def parse_changes(body: str) -> list[dict[str, str]]:
    clean = clean_body(body)
    lines = clean.split("\n")

    marker_index = next(
        (index for index, line in enumerate(lines) if HEADER_RE.match(line)),
        None,
    )

    if marker_index is None:
        return []

    changes: list[dict[str, str]] = []
    current_type: str | None = None

    for raw_line in lines[marker_index + 1 :]:
        if HEADING_RE.match(raw_line):
            break

        stripped = raw_line.strip()
        if not stripped:
            current_type = None
            continue

        line = re.sub(r"^\s*[-*]\s*", "", raw_line).strip()
        match = re.match(r"^(?P<label>[^:]+):\s*(?P<message>.*)$", line)
        if match:
            current_type = normalize_section(match.group("label"))
            if current_type is None:
                continue

            message = re.sub(r"\s+", " ", match.group("message")).strip()
            if message:
                changes.append({"type": current_type, "message": message})
            continue

        if current_type is None:
            continue

        message = re.sub(r"^\s*[-*]\s*", "", raw_line).strip()
        message = re.sub(r"\s+", " ", message)
        if message:
            changes.append({"type": current_type, "message": message})

    return changes


def group_changes(changes: list[dict[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for change in changes:
        grouped[change["type"]].append(change["message"])
    return grouped


def update_yaml(args: argparse.Namespace) -> int:
    import yaml

    changes = parse_changes(load_body(args))
    if not changes:
        print("No changelog entries found in PR body.")
        return 0

    changelog_path = Path(args.changelog_file)
    if changelog_path.exists() and changelog_path.stat().st_size > 0:
        data = yaml.safe_load(changelog_path.read_text(encoding="utf-8")) or {}
    else:
        data = {}

    entries = data.get("Entries", [])
    last_id = max((entry.get("id", 0) for entry in entries), default=0)
    entries.append(
        {
            "id": last_id + 1,
            "author": args.author,
            "time": args.time,
            "pr": args.pr,
            "changes": changes,
        }
    )
    data["Entries"] = entries

    changelog_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, indent=2),
        encoding="utf-8",
    )
    print(f"Updated changelog: {changelog_path}")
    return 0


def _needs_quotes(value: str) -> bool:
    """Mirror the quoting style of Resources/Changelog/Changelog.yml."""
    if value == "" or value != value.strip():
        return True

    if any(char in value for char in "\n\r\t"):
        return True

    if value[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True

    if ": " in value or value.endswith(":"):
        return True

    if " #" in value:
        return True

    if value.lower() in {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}:
        return True

    try:
        float(value)
        return True
    except ValueError:
        return False


def yaml_scalar(value: object) -> str:
    """Render a value as a YAML scalar, plain unless it would change meaning."""
    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, datetime):
        return f"'{value.isoformat()}'"

    text = str(value)
    if not _needs_quotes(text):
        return text

    return "'" + text.replace("'", "''") + "'"


def _emit_change(change: dict[str, str]) -> list[str]:
    return [
        f"  - message: {yaml_scalar(change['message'])}",
        f"    type: {yaml_scalar(change['type'])}",
    ]


def _emit_entry(entry: dict[str, object]) -> list[str]:
    lines = [f"- author: {yaml_scalar(entry['author'])}", "  changes:"]

    for change in entry.get("changes", []):
        lines.extend(_emit_change(change))

    lines.append(f"  id: {yaml_scalar(entry['id'])}")

    if entry.get("time") is not None:
        lines.append(f"  time: '{entry['time']}'")

    if entry.get("url") is not None:
        lines.append(f"  url: {yaml_scalar(entry['url'])}")

    return lines


def render_changelog_file(data: dict[str, object]) -> str:
    """Serialize the changelog document in the Resources/Changelog house style."""
    entries = data.get("Entries") or []
    lines: list[str] = []

    for key, value in data.items():
        if key == "Entries":
            continue

        lines.append(f"{key}: {yaml_scalar(value)}")

    lines.append("Entries:")
    for entry in entries:
        lines.extend(_emit_entry(entry))

    return "\n".join(lines) + "\n"


def _load_changelog(changelog_path: Path) -> dict[str, object]:
    import yaml

    if not changelog_path.exists() or changelog_path.stat().st_size == 0:
        return {}

    return yaml.safe_load(changelog_path.read_text(encoding="utf-8")) or {}


def write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return

    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def apply_yaml(args: argparse.Namespace) -> int:
    """Idempotently upsert (or drop) this PR's entry in the changelog file.

    Keyed on the entry `url`, so re-running after a PR body edit rewrites the
    existing entry instead of appending a duplicate.
    """
    changes = parse_changes(load_body(args))
    changelog_path = Path(args.changelog_file)
    data = _load_changelog(changelog_path)

    time = args.time or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000+00:00")

    entries = list(data.get("Entries") or [])
    url = f"https://github.com/{args.repo}/pull/{args.pr}"
    existing_index = next(
        (index for index, entry in enumerate(entries) if entry.get("url") == url),
        None,
    )

    if not changes:
        if existing_index is None:
            print("No changelog entries in PR body and none stored; nothing to do.")
            write_github_output("changed", "false")
            return 0

        entries.pop(existing_index)
        print(f"Removed changelog entry for PR #{args.pr}.")
    elif existing_index is None:
        next_id = max((entry.get("id", 0) or 0 for entry in entries), default=0) + 1
        entries.append(
            {
                "author": args.author,
                "changes": changes,
                "id": next_id,
                "time": time,
                "url": url,
            }
        )
        print(f"Added changelog entry {next_id} for PR #{args.pr}.")
    else:
        entry = entries[existing_index]
        if entry.get("changes") == changes and entry.get("author") == args.author:
            print(f"Changelog entry for PR #{args.pr} is already up to date.")
            write_github_output("changed", "false")
            return 0

        entry["changes"] = changes
        entry["author"] = args.author
        entry["url"] = url
        print(f"Updated changelog entry {entry.get('id')} for PR #{args.pr}.")

    data["Entries"] = entries
    if "AdminOnly" not in data:
        data["AdminOnly"] = False

    rendered = render_changelog_file(data)
    previous = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""

    if rendered == previous:
        print("Changelog file unchanged.")
        write_github_output("changed", "false")
        return 0

    changelog_path.write_text(rendered, encoding="utf-8")
    print(f"Wrote {changelog_path}.")
    write_github_output("changed", "true")
    return 0


def build_embed_fields(changes: list[dict[str, str]]) -> list[dict[str, object]]:
    grouped = group_changes(changes)
    fields: list[dict[str, object]] = []

    for change_type in SECTION_ORDER:
        messages = grouped.get(change_type)
        if not messages:
            continue

        chunk_lines: list[str] = []
        current_length = 0

        for message in messages:
            line = f"• {message}"
            additional = len(line) + (1 if chunk_lines else 0)

            if chunk_lines and current_length + additional > DISCORD_FIELD_LIMIT:
                fields.append(
                    {
                        "name": SECTION_TITLES[change_type],
                        "value": "\n".join(chunk_lines),
                        "inline": False,
                    }
                )
                chunk_lines = [line]
                current_length = len(line)
                continue

            chunk_lines.append(line)
            current_length += additional

        if chunk_lines:
            fields.append(
                {
                    "name": SECTION_TITLES[change_type],
                    "value": "\n".join(chunk_lines),
                    "inline": False,
                }
            )

    return fields


def render_discord(args: argparse.Namespace) -> int:
    changes = parse_changes(load_body(args))
    output_path = Path(args.output_file)

    if not changes:
        print("No changelog entries found in PR body.")
        if output_path.exists():
            output_path.unlink()
        return 0

    title = args.pr_title.strip() if args.pr_title else f"Изменение (PR #{args.pr})"
    description = f"Список изменений из [PR #{args.pr}](https://github.com/{args.repo}/pull/{args.pr})"

    payload = {
        "username": args.username,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": title,
                "url": f"https://github.com/{args.repo}/pull/{args.pr}",
                "description": description,
                "fields": build_embed_fields(changes),
                "color": 15792383,
                "footer": {
                    "text": f"Автор: {args.author} • PR #{args.pr}",
                },
                "timestamp": args.time,
            }
        ],
    }

    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Rendered Discord payload: {output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--body-base64")
    common.add_argument("--body-file")

    update_parser = subparsers.add_parser("update-yaml", parents=[common])
    update_parser.add_argument("--author", required=True)
    update_parser.add_argument("--pr", required=True, type=int)
    update_parser.add_argument("--time", required=True)
    update_parser.add_argument("--changelog-file", required=True)
    update_parser.set_defaults(func=update_yaml)

    apply_parser = subparsers.add_parser(
        "apply-yaml",
        parents=[common],
        help="Idempotently upsert this PR's :cl: block into a changelog file.",
    )
    apply_parser.add_argument("--author", required=True)
    apply_parser.add_argument("--pr", required=True, type=int)
    apply_parser.add_argument("--repo", required=True, help="owner/name, used to build the entry url")
    apply_parser.add_argument("--changelog-file", required=True)
    apply_parser.add_argument(
        "--time",
        default=None,
        help="Timestamp for new entries (defaults to now, UTC).",
    )
    apply_parser.set_defaults(func=apply_yaml)

    discord_parser = subparsers.add_parser("render-discord", parents=[common])
    discord_parser.add_argument("--author", required=True)
    discord_parser.add_argument("--pr", required=True, type=int)
    discord_parser.add_argument("--repo", required=True)
    discord_parser.add_argument("--pr-title")
    discord_parser.add_argument("--time", required=True)
    discord_parser.add_argument("--username", default="Бойжирсерр")
    discord_parser.add_argument("--output-file", required=True)
    discord_parser.set_defaults(func=render_discord)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())