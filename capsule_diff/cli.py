"""Command-line interface for capsule-diff."""

from __future__ import annotations

import argparse
from importlib import resources
import json
import sys
from pathlib import Path
from typing import Any

from .core import SnapshotError, compare, validate_snapshot
from .sarif import render_sarif


def _load(path: str) -> Any:
    raw = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SnapshotError("YAML input requires PyYAML (pip install PyYAML)") from exc
        try:
            return yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise SnapshotError(f"invalid YAML in {path}: {exc}") from exc
    return json.loads(raw)


def _render(report: dict[str, Any], format: str) -> str:
    if format == "json":
        return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    lines = ["# CapsuleDiff report" if format == "md" else "CapsuleDiff report",
             f"Before: {report['before_snapshot_id']}", f"After: {report['after_snapshot_id']}",
             f"Changes: {report['summary']['total']}"]
    if report["metadata_deltas"]:
        lines.extend(["", "## Snapshot metadata" if format == "md" else "Snapshot metadata"])
        for field, values in report["metadata_deltas"].items():
            old = json.dumps(values["before"], ensure_ascii=False, sort_keys=True)
            new = json.dumps(values["after"], ensure_ascii=False, sort_keys=True)
            lines.append(f"- {field}: {old} → {new}")
    if report["warnings"]:
        lines.extend(["", "## Warnings" if format == "md" else "Warnings"])
        lines.extend(f"- {w}" for w in report["warnings"])
    if report["changes"]:
        lines.extend(["", "## Changes" if format == "md" else "Changes"])
        for change in report["changes"]:
            label = (f"{change['risk'].upper()} {change['kind']} {change['change_id']} "
                     f"({change['code']}; match: {change['match_basis']})")
            lines.append(f"- {label}")
            for field, values in change["field_deltas"].items():
                old = json.dumps(values["before"], ensure_ascii=False, sort_keys=True)
                new = json.dumps(values["after"], ensure_ascii=False, sort_keys=True)
                lines.append(f"  - {field}: {old} → {new}")
    return "\n".join(lines) + "\n"


def _gated(report: dict[str, Any], threshold: str) -> bool:
    if threshold == "none":
        return False
    if threshold == "any":
        return bool(report["changes"])
    if threshold == "high":
        return any(change["risk"] == "high" for change in report["changes"])
    return any(change["code"] == "lost-open-obligation" for change in report["changes"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capsule-diff")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema", help="print the snapshot v1 JSON Schema")
    check = sub.add_parser("validate", help="validate a snapshot")
    check.add_argument("snapshot")
    diff = sub.add_parser("compare", help="compare snapshots")
    diff.add_argument("before")
    diff.add_argument("after")
    diff.add_argument("--format", choices=["text", "md", "json", "sarif"], default="text")
    diff.add_argument("--out")
    diff.add_argument("--fail-on", choices=["none", "high", "lost-open-obligation", "any"], default="none")
    diff.add_argument("--mapping", help="JSON object mapping type:old_id to new_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            schema = resources.files("capsule_diff").joinpath("snapshot.schema.json")
            sys.stdout.write(schema.read_text(encoding="utf-8"))
            return 0
        if args.command == "validate":
            validate_snapshot(_load(args.snapshot))
            print(f"valid: {args.snapshot}")
            return 0
        mapping = _load(args.mapping) if args.mapping else None
        if mapping is not None and not isinstance(mapping, dict):
            raise SnapshotError("mapping must be a JSON object")
        before, after = _load(args.before), _load(args.after)
        report = compare(before, after, mapping)
        output = (render_sarif(report, args.before, args.after, before, after)
                  if args.format == "sarif" else _render(report, args.format))
        if args.out:
            Path(args.out).write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 1 if _gated(report, args.fail_on) else 0
    except (OSError, ValueError, SnapshotError) as exc:
        print(f"capsule-diff: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
