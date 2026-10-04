import json
import importlib.util
from importlib import resources
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule_diff import SnapshotError, compare, validate_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def snapshot(name, records=(), revision="abc"):
    return {"schema_version": 1, "snapshot_id": name, "created_at": "2026-01-01T00:00:00Z",
            "source_revision": revision, "records": list(records)}


def record(typ, ident, status="active", text="Some text", **extra):
    return {"type": typ, "id": ident, "status": status, "text": text, **extra}


class CompareTests(unittest.TestCase):
    def test_reorder_and_whitespace_are_ignored(self):
        a = record("decision", "d", text="Use  local   files", evidence_ids=["e2", "e1"])
        b = record("obligation", "o", status="open")
        changed = record("decision", "d", text="Use local files", evidence_ids=["e1", "e2"])
        result = compare(snapshot("a", [a, b]), snapshot("b", [b, changed]))
        self.assertEqual(result["changes"], [])

    def test_removed_open_obligation_is_high(self):
        result = compare(snapshot("a", [record("obligation", "o", "open")]), snapshot("b"))
        self.assertEqual((result["changes"][0]["risk"], result["changes"][0]["code"]),
                         ("high", "lost-open-obligation"))

    def test_done_with_completion_evidence_is_review(self):
        before = record("obligation", "o", "open")
        after = record("obligation", "o", "done", completion_evidence_ids=["test-1"])
        self.assertEqual(compare(snapshot("a", [before]), snapshot("b", [after]))["changes"][0]["risk"], "review")
        after.pop("completion_evidence_ids")
        self.assertEqual(compare(snapshot("a", [before]), snapshot("b", [after]))["changes"][0]["risk"], "high")
        after["completed_at"] = "2026-01-02T00:00:00Z"
        self.assertEqual(compare(snapshot("a", [before]), snapshot("b", [after]))["changes"][0]["risk"], "high")

    def test_removed_active_decision_and_required_dependency_are_high(self):
        for item in (record("decision", "d", "active"),
                     record("dependency", "dep", "required")):
            result = compare(snapshot("a", [item]), snapshot("b"))
            self.assertEqual(result["changes"][0]["risk"], "high")

    def test_only_evidence_removed_has_specific_kind(self):
        old = record("assumption", "a", evidence_ids=["e1", "e2"])
        new = record("assumption", "a", evidence_ids=["e2"])
        change = compare(snapshot("a", [old]), snapshot("b", [new]))["changes"][0]
        self.assertEqual((change["kind"], change["code"]), ("evidence_changed", "removed-evidence"))

    def test_renames_by_replaces_mapping_and_unique_topic(self):
        old = record("decision", "old", topic_key="auth")
        for extra, mapping, basis in [({"replaces_id": "old"}, None, "replaces_id"),
                                      ({}, {"decision:old": "new"}, "mapping"),
                                      ({"topic_key": "auth"}, None, "topic_key")]:
            new = record("decision", "new", **extra)
            result = compare(snapshot("a", [old]), snapshot("b", [new]), mapping)
            self.assertEqual(len(result["changes"]), 1)
            self.assertEqual((result["changes"][0]["kind"], result["changes"][0]["match_basis"]),
                             ("renamed", basis))

    def test_ambiguous_topic_does_not_guess(self):
        old = [record("decision", "a", topic_key="x"), record("decision", "b", topic_key="x")]
        new = [record("decision", "c", topic_key="x")]
        result = compare(snapshot("a", old), snapshot("b", new))
        self.assertEqual(len(result["changes"]), 3)
        self.assertIn("ambiguous", result["warnings"][0])

    def test_decision_text_evidence_and_revision(self):
        old = record("decision", "d", text="Use A", evidence_ids=["e1", "e2"])
        new = record("decision", "d", text="Use B", evidence_ids=["e2"])
        result = compare(snapshot("a", [old]), snapshot("b", [new], "def"))
        change = result["changes"][0]
        self.assertEqual(change["risk"], "review")
        self.assertEqual(set(change["field_deltas"]), {"text", "evidence_ids"})
        self.assertIn("source_revision", result["warnings"][0])
        self.assertEqual(result["metadata_deltas"]["source_revision"]["after"], "def")

    def test_empty_and_unicode_are_stable(self):
        self.assertEqual(compare(snapshot("a"), snapshot("b"))["summary"]["total"], 0)
        r = record("decision", "שלום", text="בדיקה")
        self.assertEqual(compare(snapshot("a", [r]), snapshot("b", [r]))["changes"], [])

    def test_invalid_schema_and_duplicate_ids(self):
        for bad in [snapshot("a", [record("decision", "x"), record("decision", "x")]),
                    {**snapshot("a"), "schema_version": 2},
                    snapshot("a", [{"type": [], "id": "x", "status": "active", "text": "x"}])]:
            with self.assertRaises(SnapshotError):
                validate_snapshot(bad)

    def test_invalid_explicit_mapping(self):
        with self.assertRaises(SnapshotError):
            compare(snapshot("a", [record("decision", "x")]), snapshot("b"),
                    {"decision:x": "missing"})


class CliTests(unittest.TestCase):
    def test_sarif_reports_all_risks_and_record_locations_under_gate(self):
        before = FIXTURES / "compaction-before.json"
        after = FIXTURES / "compaction-after.json"
        base = [sys.executable, "-m", "capsule_diff", "compare", str(before), str(after),
                "--format", "sarif"]
        gated = subprocess.run(base + ["--fail-on", "high"], capture_output=True, text=True)
        self.assertEqual(gated.returncode, 1, gated.stderr)
        clear = subprocess.run(base, capture_output=True, text=True)
        self.assertEqual(clear.returncode, 0, clear.stderr)
        self.assertEqual(gated.stdout, clear.stdout)
        sarif = json.loads(gated.stdout)
        self.assertEqual(sarif["version"], "2.1.0")
        run = sarif["runs"][0]
        self.assertEqual(run["tool"]["driver"]["name"], "CapsuleDiff")
        self.assertEqual([r["id"] for r in run["tool"]["driver"]["rules"]],
                         sorted(r["id"] for r in run["tool"]["driver"]["rules"]))
        self.assertEqual(len(run["results"]), 4)
        by_change = {r["properties"]["changeId"]: r for r in run["results"]}
        removed = by_change["obligation:auth-tests->-"]
        self.assertEqual((removed["level"], removed["ruleId"]), ("error", "lost-open-obligation"))
        self.assertEqual(removed["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], str(before))
        self.assertEqual(removed["locations"][0]["properties"]["jsonPointer"], "/records/1")
        changed = by_change["decision:auth-scheme->auth-scheme"]
        self.assertEqual(changed["level"], "warning")
        self.assertEqual(changed["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], str(after))
        self.assertEqual(changed["locations"][0]["properties"]["jsonPointer"], "/records/3")
        added = by_change["assumption:-->clock-sync"]
        self.assertEqual(added["level"], "note")
        self.assertEqual(added["locations"][0]["properties"]["jsonPointer"], "/records/4")

    def test_sarif_location_uri_escapes_spaces(self):
        with tempfile.TemporaryDirectory(prefix="capsule diff ") as temp:
            before = Path(temp) / "before snapshot.json"
            after = Path(temp) / "after snapshot.json"
            before.write_text(json.dumps(snapshot("a", [record("obligation", "o", "open")])))
            after.write_text(json.dumps(snapshot("b")))
            run = subprocess.run([sys.executable, "-m", "capsule_diff", "compare", str(before), str(after),
                                  "--format", "sarif"], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            uri = json.loads(run.stdout)["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
            self.assertIn("%20", uri)
            self.assertNotIn(" ", uri)

    def test_schema_command_matches_packaged_schema(self):
        run = subprocess.run([sys.executable, "-m", "capsule_diff", "schema"],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        schema_text = resources.files("capsule_diff").joinpath("snapshot.schema.json").read_text(encoding="utf-8")
        self.assertEqual(run.stdout, schema_text)
        schema = json.loads(run.stdout)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"], {"const": 1})
        self.assertEqual(set(schema["required"]),
                         {"schema_version", "snapshot_id", "created_at", "source_revision", "records"})

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is optional for tests")
    def test_schema_acceptance_matches_runtime_validation(self):
        from jsonschema import Draft202012Validator

        schema = json.loads(resources.files("capsule_diff").joinpath("snapshot.schema.json").read_text())
        validator = Draft202012Validator(schema)
        valid = [snapshot("empty"), snapshot("with-record", [record("decision", "a",
                                                               evidence_ids=["e1"], topic_key="auth")])]
        invalid = [
            {**snapshot("bad"), "schema_version": True},
            {**snapshot("bad"), "snapshot_id": "  "},
            {**snapshot("bad"), "records": {}},
            snapshot("bad", [record("decision", "a", evidence_ids=["e1", "e1"])]),
            snapshot("bad", [record("decision", "a", topic_key="  ")]),
        ]
        for item in valid:
            with self.subTest(item=item):
                self.assertFalse(list(validator.iter_errors(item)))
                validate_snapshot(item)
        for item in invalid:
            with self.subTest(item=item):
                self.assertTrue(list(validator.iter_errors(item)))
                with self.assertRaises(SnapshotError):
                    validate_snapshot(item)
        # Composite key uniqueness is intentionally enforced by the CLI validator.
        duplicate = snapshot("duplicate", [record("decision", "a"), record("decision", "a", text="other")])
        self.assertFalse(list(validator.iter_errors(duplicate)))
        with self.assertRaises(SnapshotError):
            validate_snapshot(duplicate)

    def test_realistic_compaction_matches_golden_reports(self):
        before = FIXTURES / "compaction-before.json"
        after = FIXTURES / "compaction-after.json"
        for format in ("json", "md"):
            run = subprocess.run([sys.executable, "-m", "capsule_diff", "compare", str(before), str(after),
                                  "--format", format, "--fail-on", "lost-open-obligation"],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 1, run.stderr)
            if format == "json":
                self.assertEqual(run.stdout, (FIXTURES / "compaction-report.json").read_text(encoding="utf-8"))
            else:
                self.assertIn("# CapsuleDiff report\n", run.stdout)
                self.assertIn("- HIGH removed obligation:auth-tests->-", run.stdout)
                self.assertIn("- REVIEW modified decision:auth-scheme->auth-scheme", run.stdout)
        report = json.loads((FIXTURES / "compaction-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["by_risk"], {"high": 1, "info": 1, "review": 2})
        self.assertEqual([c["change_id"] for c in report["changes"]],
                         ["assumption:-->clock-sync", "decision:auth-scheme->auth-scheme",
                          "obligation:auth-tests->-", "obligation:handoff-note->handoff-note"])
        self.assertEqual(report["metadata_deltas"]["summary"]["after"],
                         "Auth implementation and handoff note are complete.")

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is optional")
    def test_malformed_yaml_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.yaml"
            bad.write_text("snapshot: [unterminated\n", encoding="utf-8")
            run = subprocess.run([sys.executable, "-m", "capsule_diff", "validate", str(bad)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertIn("invalid YAML", run.stderr)
            before = snapshot("a")
            after = {**snapshot("b"), "summary": "  Revised   context "}
            result = compare(before, after)
            self.assertEqual(result["metadata_deltas"]["summary"],
                             {"before": None, "after": "  Revised   context "})

    def test_compaction_gate_and_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "before.json"
            new = root / "after.json"
            report = root / "report.json"
            old.write_text(json.dumps(snapshot("a", [record("obligation", "finish-tests", "open")])))
            new.write_text(json.dumps(snapshot("b")))
            args = [sys.executable, "-m", "capsule_diff", "compare", str(old), str(new),
                    "--format", "json", "--out", str(report), "--fail-on", "lost-open-obligation"]
            run = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(run.returncode, 1, run.stderr)
            data = json.loads(report.read_text())
            self.assertEqual(data["changes"][0]["code"], "lost-open-obligation")
            self.assertEqual(subprocess.run([sys.executable, "-m", "capsule_diff", "validate", str(old)],
                                            capture_output=True).returncode, 0)
            md = subprocess.run(args[:-6] + ["--format", "md"], capture_output=True, text=True)
            self.assertEqual(md.returncode, 0, md.stderr)
            self.assertIn("## Changes", md.stdout)


if __name__ == "__main__":
    unittest.main()
