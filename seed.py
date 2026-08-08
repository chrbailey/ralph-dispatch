#!/usr/bin/env python3
"""Validate and idempotently seed Ralph Dispatch from a normalized manifest."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import dispatch

MAX_MANIFEST_BYTES = 10_000_000


def load_manifest(path: str | Path) -> list[dict[str, Any]]:
    manifest_path = Path(path)
    raw = manifest_path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise dispatch.ValidationError("manifest exceeds 10 MB")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise dispatch.ValidationError(f"manifest is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("jobs"), list):
        raise dispatch.ValidationError("manifest must be an object containing jobs[]")
    jobs = decoded["jobs"]
    refs: set[str] = set()
    for index, item in enumerate(jobs):
        if not isinstance(item, dict):
            raise dispatch.ValidationError(f"jobs[{index}] must be an object")
        allowed = {"ref", "kind", "payload", "depends_on", "idempotency_key"}
        extra = set(item) - allowed
        if extra:
            raise dispatch.ValidationError(f"jobs[{index}] has unknown fields: {sorted(extra)}")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref.strip() or ref in refs:
            raise dispatch.ValidationError(f"jobs[{index}].ref must be unique and non-empty")
        refs.add(ref)
        kind = item.get("kind")
        if kind not in dispatch.KIND_START_TIER or kind == "critic_review":
            raise dispatch.ValidationError(f"jobs[{index}].kind is invalid")
        payload = item.get("payload")
        dispatch.validate_payload(kind, payload)
        depends_on = item.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(
            isinstance(dependency, str) and dependency for dependency in depends_on
        ):
            raise dispatch.ValidationError(f"jobs[{index}].depends_on must contain refs")
        if len(depends_on) != len(set(depends_on)):
            raise dispatch.ValidationError(f"jobs[{index}].depends_on contains duplicates")
        key = item.get("idempotency_key", f"manifest:{ref}")
        if key is None:
            raise dispatch.ValidationError(f"jobs[{index}].idempotency_key cannot be null")
        dispatch._validate_idempotency_key(key)
    unknown = {
        dependency
        for item in jobs
        for dependency in item.get("depends_on", [])
        if dependency not in refs
    }
    if unknown:
        raise dispatch.ValidationError(f"unknown dependency refs: {sorted(unknown)}")
    return jobs


def topological_order(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending = {item["ref"]: item for item in jobs}
    ordered: list[dict[str, Any]] = []
    resolved: set[str] = set()
    while pending:
        ready = [item for item in pending.values() if set(item.get("depends_on", [])) <= resolved]
        if not ready:
            raise dispatch.ValidationError("manifest dependency graph contains a cycle")
        for item in ready:
            ordered.append(item)
            resolved.add(item["ref"])
            del pending[item["ref"]]
    return ordered


def seed_manifest(
    conn,
    jobs: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, int | None]:
    ordered = topological_order(jobs)
    if dry_run:
        return {item["ref"]: None for item in ordered}
    ids: dict[str, int] = {}
    for item in ordered:
        ids[item["ref"]] = dispatch.enqueue(
            conn,
            item["kind"],
            item["payload"],
            idempotency_key=item.get("idempotency_key", f"manifest:{item['ref']}"),
            depends_on=[ids[dependency] for dependency in item.get("depends_on", [])],
        )
    return ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="normalized UTF-8 JSON manifest")
    parser.add_argument("--db", default=str(dispatch.DB_PATH), help="SQLite database path")
    parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        jobs = load_manifest(args.manifest)
        if args.dry_run:
            ids = seed_manifest(None, jobs, dry_run=True)
        else:
            conn = dispatch.db(args.db)
            try:
                ids = seed_manifest(conn, jobs)
            finally:
                conn.close()
        print(json.dumps({"jobs": ids}, indent=2, sort_keys=True))
        return 0
    except (dispatch.DispatchError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
