"""Release-packaging guarantees: closed allowlist and domain-neutral content."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import build_release_package as package


class ReleasePackageTests(unittest.TestCase):
    def test_release_is_allowlisted_and_current(self):
        folder, names = package.release_files()
        self.assertTrue(folder.startswith("Agent-Computer-Bridge-v"))
        self.assertIn("skills/agent-computer-bridge/SKILL.md", names)
        self.assertIn("agent_computer_bridge_mcp.py", names)
        self.assertIn("agent_cli.py", names)
        self.assertIn("agent_setup.py", names)
        self.assertIn("tests/test_dependency_bootstrap.py", names)
        self.assertIn("agent.cmd", names)
        self.assertIn("skills/agent-computer-bridge/references/shell-roundtrip.md", names)
        self.assertIn("tools/build_release_package.py", names)
        self.assertIn("portable/Agent-Computer-Bridge-Recorder-v2.42-r47-portable.exe", names)
        self.assertTrue(all(not name.startswith(("docs/", "evidence/", "history/",
                                                 "recordings/", "journals/", "inbox/"))
                            for name in names))
        self.assertFalse(any("agent_computer_bridge_v2_3" in name and "r47" not in name
                             for name in names))
        self.assertEqual(len(names), len(set(names)))

    def test_shipped_text_has_no_application_specific_identifiers(self):
        _, contents, digests = package.files_and_hashes()
        self.assertEqual(set(contents), set(digests))
        for name, body in contents.items():
            self.assertEqual(package.sha(body), digests[name])
            if Path(name).suffix.lower() not in package.BINARY_SUFFIXES:
                text = body.decode("utf-8").casefold()
                for term in package.FORBIDDEN_SHIPPED_TEXT:
                    self.assertNotIn(term, text, msg=name)

    def test_checksum_manifest_has_one_entry_per_source_file(self):
        _, contents, digests = package.files_and_hashes()
        rows = package.manifest_text(digests).decode("utf-8").splitlines()
        self.assertEqual(len(rows), len(contents))
        self.assertEqual(len(rows), len(set(rows)))
        self.assertTrue(all(len(row.split("  ", 1)[0]) == 64 for row in rows))


if __name__ == "__main__":
    unittest.main()
