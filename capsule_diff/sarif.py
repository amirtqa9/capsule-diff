"""SARIF 2.1.0 rendering for snapshot comparisons."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote


_LEVEL = {"high": "error", "review": "warning", "info": "note"}


def _location(path: str, snapshot: dict[str, Any], typ: str, ident: str) -> dict[str, Any] | None:
    for index, record in enumerate(snapshot["records"]):
        if record["type"] == typ and record["id"] == ident:
            return {
                "physicalLocation": {"artifactLocation": {"uri": quote(path, safe="/:")}},
                "logicalLocations": [{"fullyQualifiedName": f"{typ}:{ident}"}],
                "properties": {"jsonPointer": f"/records/{index}"},
            }
    return None


def render_sarif(report: dict[str, Any], before_path: str, after_path: str,
                 before: dict[str, Any], after: dict[str, Any]) -> str:
    """Render every change, including changes below a configured CLI gate.

    The artifact URI and JSON Pointer identify the record. Source line numbers
    are omitted because the JSON/YAML loader does not retain source positions.
    """
    rules = sorted({change["code"] for change in report["changes"]})
    results = []
    for change in report["changes"]:
        typ = change["record_type"]
        after_id, before_id = change["after_id"], change["before_id"]
        primary = (_location(after_path, after, typ, after_id) if after_id else
                   _location(before_path, before, typ, before_id))
        result: dict[str, Any] = {
            "ruleId": change["code"],
            "level": _LEVEL[change["risk"]],
            "message": {"text": f"{change['kind']} {change['change_id']} ({change['code']})"},
            "properties": {
                "changeId": change["change_id"],
                "recordType": typ,
                "risk": change["risk"],
                "matchBasis": change["match_basis"],
                "fieldDeltas": change["field_deltas"],
            },
        }
        if primary:
            result["locations"] = [primary]
        results.append(result)
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "CapsuleDiff",
                "informationUri": "https://github.com/amirtqa9/capsule-diff",
                "rules": [{"id": code, "shortDescription": {"text": code.replace("-", " ")}}
                          for code in rules],
            }},
            "results": results,
            "properties": {
                "beforeHash": report["before_hash"],
                "afterHash": report["after_hash"],
                "metadataDeltas": report["metadata_deltas"],
                "warnings": report["warnings"],
            },
        }],
    }
    return json.dumps(sarif, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
