"""Manifest-boundary tests for seed.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dispatch  # noqa: E402
import seed  # noqa: E402


class TestSeedManifest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, value):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def payload(self, task):
        return {
            "task": task,
            "evidence": [
                {
                    "source_id": "one",
                    "url": "https://evidence.example/1",
                    "excerpt": "Evidence",
                }
            ],
        }

    def test_manifest_topologically_seeds_and_is_rerunnable(self):
        jobs = [
            {
                "ref": "assemble",
                "kind": "dossier_assemble",
                "payload": self.payload("Assemble"),
                "depends_on": ["section"],
            },
            {
                "ref": "section",
                "kind": "dossier_section",
                "payload": self.payload("Extract"),
            },
        ]
        loaded = seed.load_manifest(self.write({"jobs": jobs}))
        conn = dispatch.db(self.root / "dispatch.db")
        first = seed.seed_manifest(conn, loaded)
        second = seed.seed_manifest(conn, loaded)
        self.assertEqual(first, second)
        self.assertLess(first["section"], first["assemble"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        conn.close()

    def test_cycle_is_rejected_without_writes(self):
        jobs = [
            {
                "ref": "a",
                "kind": "dossier_section",
                "payload": self.payload("A"),
                "depends_on": ["b"],
            },
            {
                "ref": "b",
                "kind": "dossier_section",
                "payload": self.payload("B"),
                "depends_on": ["a"],
            },
        ]
        loaded = seed.load_manifest(self.write({"jobs": jobs}))
        with self.assertRaises(dispatch.ValidationError):
            seed.topological_order(loaded)

    def test_unknown_manifest_fields_are_rejected(self):
        jobs = [
            {
                "ref": "a",
                "kind": "dossier_section",
                "payload": self.payload("A"),
                "shell_command": "curl secrets.example",
            }
        ]
        with self.assertRaises(dispatch.ValidationError):
            seed.load_manifest(self.write({"jobs": jobs}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
