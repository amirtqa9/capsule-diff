# CapsuleDiff

CapsuleDiff compares two structured context snapshots and highlights changed decisions, evidence, dependencies, and open obligations. Matching uses stable record IDs, declared renames, or unique topic keys; reports are deterministic and suitable for review or CI.

## Install

Requires Python 3.11 or newer. From this repository:

```sh
python3 -m pip install -e .
```

JSON snapshots work without additional dependencies. Install PyYAML separately to read YAML snapshots.

## Use

```sh
capsule-diff schema > snapshot.schema.json
capsule-diff validate before.json
capsule-diff compare before.json after.json --format sarif --fail-on high --out changes.sarif
```

Each snapshot has `schema_version: 1`, an ID, creation time, source revision, and a `records` array. The `compare` command also supports `text`, `md`, and `json` reports. A configured `--fail-on` gate returns exit code `1` while still writing the complete report. The tool compares recorded claims; it does not decide whether their prose is true.

Run the tests with `python3 -m unittest discover -s tests -q`.
