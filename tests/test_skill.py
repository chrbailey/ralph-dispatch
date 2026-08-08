"""Dependency-free structural checks for the bundled operating skill."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "ralph-dispatch" / "SKILL.md"
OPENAI_YAML = ROOT / "skill" / "ralph-dispatch" / "agents" / "openai.yaml"


class TestBundledSkill(unittest.TestCase):
    def test_frontmatter_has_only_required_trigger_fields(self):
        text = SKILL.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(match)
        fields = {
            line.split(":", 1)[0]
            for line in match.group(1).splitlines()
            if line and not line.startswith(" ")
        }
        self.assertEqual(fields, {"name", "description"})
        self.assertIn("name: ralph-dispatch", match.group(1))

    def test_interface_metadata_and_safety_rules_match_skill(self):
        skill = SKILL.read_text(encoding="utf-8")
        interface = OPENAI_YAML.read_text(encoding="utf-8")
        self.assertIn("$ralph-dispatch", interface)
        self.assertIn("needs_human", skill)
        self.assertIn("do not auto-resolve", skill)
        self.assertIn("only `committed`", skill)


if __name__ == "__main__":
    unittest.main(verbosity=2)
