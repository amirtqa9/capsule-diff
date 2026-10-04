"""Validation, alignment, field comparison, and risk classification."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any, Mapping

TYPES = {"decision", "obligation", "assumption", "dependency", "evidence"}
REQUIRED = {"schema_version", "snapshot_id", "created_at", "source_revision", "records"}
RECORD_REQUIRED = {"id", "type", "status", "text"}
FIELDS_IGNORED = {"replaces_id"}


class SnapshotError(ValueError):
    """Invalid snapshot or explicit mapping."""


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    """Return a validated snapshot without modifying the caller's data."""
    if not isinstance(snapshot, dict):
        raise SnapshotError("snapshot must be a JSON object")
    missing = REQUIRED - snapshot.keys()
    if missing:
        raise SnapshotError(f"missing snapshot fields: {', '.join(sorted(missing))}")
    if snapshot["schema_version"] != 1 or isinstance(snapshot["schema_version"], bool):
        raise SnapshotError("unsupported schema_version (expected 1)")
    for field in ("snapshot_id", "created_at", "source_revision"):
        if not _nonempty(snapshot[field]):
            raise SnapshotError(f"{field} must be a nonempty string")
    if not isinstance(snapshot["records"], list):
        raise SnapshotError("records must be an array")
    seen: set[tuple[str, str]] = set()
    for n, record in enumerate(snapshot["records"]):
        loc = f"records[{n}]"
        if not isinstance(record, dict):
            raise SnapshotError(f"{loc} must be an object")
        missing = RECORD_REQUIRED - record.keys()
        if missing:
            raise SnapshotError(f"{loc} missing: {', '.join(sorted(missing))}")
        if not isinstance(record["type"], str) or record["type"] not in TYPES:
            raise SnapshotError(f"{loc}.type is unsupported")
        for field in ("id", "status", "text"):
            if not _nonempty(record[field]):
                raise SnapshotError(f"{loc}.{field} must be a nonempty string")
        key = (record["type"], record["id"])
        if key in seen:
            raise SnapshotError(f"duplicate record: {key[0]}:{key[1]}")
        seen.add(key)
        for field in ("topic_key", "replaces_id"):
            if field in record and not _nonempty(record[field]):
                raise SnapshotError(f"{loc}.{field} must be a nonempty string")
        if "evidence_ids" in record and (not isinstance(record["evidence_ids"], list) or
                any(not _nonempty(item) for item in record["evidence_ids"])):
            raise SnapshotError(f"{loc}.evidence_ids must be an array of nonempty strings")
        if "evidence_ids" in record and len(set(record["evidence_ids"])) != len(record["evidence_ids"]):
            raise SnapshotError(f"{loc}.evidence_ids contains duplicates")
    return snapshot


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")).hexdigest()


def _canonical(field: str, value: Any) -> Any:
    if field == "evidence_ids":
        return sorted(value or [])
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, dict):
        return {key: _canonical(key, item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(field, item) for item in value]
    return value


def _deltas(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    fields = sorted((set(before) | set(after)) - {"id", "type"} - FIELDS_IGNORED)
    return {field: {"before": before.get(field), "after": after.get(field)}
            for field in fields if _canonical(field, before.get(field)) != _canonical(field, after.get(field))}


def _has_completion_evidence(record: Mapping[str, Any]) -> bool:
    return bool(record.get("completion_evidence_ids") or record.get("completion_evidence"))


def _risk(kind: str, before: Mapping[str, Any] | None,
          after: Mapping[str, Any] | None, deltas: Mapping[str, Any]) -> tuple[str, str]:
    if before and not after:
        if before["type"] == "obligation" and before["status"] == "open":
            return "high", "lost-open-obligation"
        if before["type"] == "decision" and before["status"] == "active":
            return "high", "lost-active-decision"
        if before["type"] == "dependency" and (before["status"] == "required" or before.get("required") is True):
            return "high", "lost-required-dependency"
        return "review", "removed-record"
    if not before:
        return "info", "added-record"
    assert after is not None
    if (before["type"] == "obligation" and before["status"] == "open" and
            after["status"] == "done"):
        return ("review", "resolved-obligation") if _has_completion_evidence(after) else ("high", "unsupported-resolution")
    if before["type"] == "decision" and before["status"] == "active":
        if after["status"] == "superseded":
            return "review", "superseded-decision"
        if "text" in deltas:
            return "review", "changed-active-decision"
    if "evidence_ids" in deltas:
        removed = set(before.get("evidence_ids", [])) - set(after.get("evidence_ids", []))
        if removed:
            return "review", "removed-evidence"
    if "status" in deltas or "text" in deltas:
        return "review", "substantive-change"
    if kind == "renamed":
        return "review", "renamed-record"
    return "info", "metadata-change"


def _key(record: Mapping[str, Any]) -> tuple[str, str]:
    return record["type"], record["id"]


def compare(before: dict[str, Any], after: dict[str, Any],
            mapping: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Compare valid snapshots. Mapping keys use ``type:old_id`` and values ``new_id``."""
    validate_snapshot(before)
    validate_snapshot(after)
    old = {_key(r): r for r in before["records"]}
    new = {_key(r): r for r in after["records"]}
    pairs: list[tuple[tuple[str, str], tuple[str, str], str]] = []
    used_old: set[tuple[str, str]] = set()
    used_new: set[tuple[str, str]] = set()

    def pair(a: tuple[str, str], b: tuple[str, str], basis: str) -> None:
        if a in used_old or b in used_new:
            raise SnapshotError(f"ambiguous mapping: {a} -> {b}")
        if a not in old or b not in new or a[0] != b[0]:
            raise SnapshotError(f"invalid mapping: {a} -> {b}")
        pairs.append((a, b, basis))
        used_old.add(a)
        used_new.add(b)

    for key in sorted(old.keys() & new.keys()):
        pair(key, key, "id")

    for source, target in sorted((mapping or {}).items(), key=lambda item: str(item[0])):
        if not isinstance(source, str) or not _nonempty(target) or ":" not in source:
            raise SnapshotError("mapping entries must be type:old_id -> new_id strings")
        typ, old_id = source.split(":", 1)
        pair((typ, old_id), (typ, target), "mapping")

    for key in sorted(new):
        replacement = new[key].get("replaces_id")
        if replacement and key not in used_new:
            pair((key[0], replacement), key, "replaces_id")

    old_topics: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    new_topics: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for key in old.keys() - used_old:
        if old[key].get("topic_key"):
            old_topics[(key[0], old[key]["topic_key"])].append(key)
    for key in new.keys() - used_new:
        if new[key].get("topic_key"):
            new_topics[(key[0], new[key]["topic_key"])].append(key)
    warnings: list[str] = []
    for topic in sorted(old_topics.keys() & new_topics.keys()):
        a, b = old_topics[topic], new_topics[topic]
        if len(a) == len(b) == 1:
            pair(a[0], b[0], "topic_key")
        else:
            warnings.append(f"ambiguous topic_key {topic[0]}:{topic[1]}; reported as additions/removals")

    changes: list[dict[str, Any]] = []

    def emit(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None,
             basis: str, deltas: dict[str, Any] | None = None) -> None:
        deltas = deltas or {}
        kind = ("added" if a is None else "removed" if b is None else
                "renamed" if a["id"] != b["id"] else
                "evidence_changed" if set(deltas) == {"evidence_ids"} else
                "status_changed" if set(deltas) == {"status"} else "modified")
        risk, code = _risk(kind, a, b, deltas)
        typ = (a or b)["type"]
        aid, bid = a["id"] if a else None, b["id"] if b else None
        changes.append({"change_id": f"{typ}:{aid or '-'}->{bid or '-'}", "record_type": typ,
                        "before_id": aid, "after_id": bid, "kind": kind,
                        "field_deltas": deltas, "match_basis": basis, "risk": risk, "code": code})

    for akey, bkey, basis in pairs:
        deltas = _deltas(old[akey], new[bkey])
        if deltas or akey != bkey:
            emit(old[akey], new[bkey], basis, deltas)
    for key in sorted(old.keys() - used_old):
        emit(old[key], None, "unmatched")
    for key in sorted(new.keys() - used_new):
        emit(None, new[key], "unmatched")
    if before["source_revision"] != after["source_revision"]:
        warnings.append("source_revision changed; evidence may refer to different revisions")
    metadata_fields = sorted((set(before) | set(after)) - {"records", "schema_version"})
    metadata_deltas = {field: {"before": before.get(field), "after": after.get(field)}
                       for field in metadata_fields
                       if _canonical(field, before.get(field)) != _canonical(field, after.get(field))}
    changes.sort(key=lambda c: (c["record_type"], c["before_id"] or "", c["after_id"] or ""))
    return {"schema_version": 1, "before_hash": _hash(before), "after_hash": _hash(after),
            "before_snapshot_id": before["snapshot_id"], "after_snapshot_id": after["snapshot_id"],
            "metadata_deltas": metadata_deltas,
            "summary": {"total": len(changes), "by_risk": dict(sorted(Counter(c["risk"] for c in changes).items())),
                        "by_kind": dict(sorted(Counter(c["kind"] for c in changes).items()))},
            "warnings": warnings, "changes": changes}
