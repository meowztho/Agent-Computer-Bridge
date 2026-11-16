"""Release-packaging guarantees: closed allowlist and domain-neutral content."""
from __future__ import annotations

import re
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
        self.assertIn("record.cmd", names)
        self.assertIn("record.bat", names)
        self.assertIn("tests/test_recorder_bootstrap.py", names)
        self.assertIn("tests/test_machine_guidance.py", names)
        self.assertIn("skills/agent-computer-bridge/references/shell-roundtrip.md", names)
        self.assertIn("tools/build_release_package.py", names)
        version, build = (line.strip() for line in
                          (ROOT / "VERSION.txt").read_text(encoding="utf-8").splitlines())
        revision = build.rsplit("-", 1)[-1]
        self.assertIn(
            f"portable/Agent-Computer-Bridge-Recorder-v{version}-{revision}-portable.exe",
            names)
        self.assertTrue(all(not name.startswith(("docs/", "evidence/", "history/",
                                                 "recordings/", "journals/", "inbox/"))
                            for name in names))
        self.assertFalse(any("agent_computer_bridge_v2_3" in name and "r49" not in name
                             for name in names))
        self.assertEqual(len(names), len(set(names)))

    def test_every_test_module_is_released(self):
        _, names = package.release_files()
        on_disk = {f"tests/{path.name}" for path in (ROOT / "tests").glob("test_*.py")}
        shipped = {name for name in names if name.startswith("tests/")}
        self.assertEqual(
            shipped, on_disk,
            "every test module must be in RELEASE_FILES; otherwise the shipped "
            "suite cannot cover the release it is shipped with")

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

    def test_checksum_list_paths_and_hashes_are_valid_utf8(self):
        import hashlib

        entries = (ROOT / "SHA256SUMS.txt").read_bytes().decode("utf-8").splitlines()
        is_dev_repository = (ROOT / "PROJECT_INDEX.yaml").is_file()
        has_historical_workspace = (ROOT / "history").is_dir()
        seen = set()
        for row in entries:
            digest, delimiter, name = row.partition("  ")
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertEqual(delimiter, "  ")
            self.assertTrue(name)
            self.assertNotIn(name, seen)
            seen.add(name)
            item = ROOT / name
            if not item.is_file():
                if name.startswith("portable/") and name.endswith(".exe"):
                    # Documented exception: the portable recorder EXE is a gitignored
                    # local build artifact, so a clean clone legitimately lacks it.
                    continue
                if is_dev_repository and not has_historical_workspace:
                    # Some archived originals are intentionally untracked, not in clean clones.
                    continue
                self.fail(f"missing checksum path: {name}")
            if name.startswith("portable/") and name.endswith(".exe"):
                # Immutable build artifact: verify its digest when present.
                self.assertEqual(hashlib.sha256(item.read_bytes()).hexdigest(), digest, name)
            else:
                # Every shipped source byte must match the manifest. The manifest
                # is regenerated by the release process, so a stale entry is a
                # release defect, not an accepted snapshot.
                self.assertEqual(hashlib.sha256(item.read_bytes()).hexdigest(), digest, name)
        self.assertGreaterEqual(len(entries), 150 if is_dev_repository else 40)
        if is_dev_repository:
            handovers = [name for name in seen if name.startswith("docs/Agent Computer Bridge ")]
            self.assertEqual(len(handovers), 20)
            for name in handovers:
                self.assertTrue((ROOT / name).is_file(), f"broken historical name: {name}")

    def test_root_checksum_manifest_matches_packager_file_set(self):
        _, _, digests = package.files_and_hashes()
        rows = (ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        listed = {row.partition("  ")[2] for row in rows}
        self.assertEqual(listed, set(digests))

    def test_zip_time_matches_build_id(self):
        source = (ROOT / "agent_computer_bridge.py").read_text(encoding="utf-8")
        build_id = re.search(r'^BUILD_ID = "([^"]+)"$', source, re.MULTILINE).group(1)
        self.assertEqual(package.ZIP_TIME, package.zip_time_from_build_id(build_id))
        self.assertEqual(package.zip_time_from_build_id("2026-11-12-r54"),
                         (2026, 11, 12, 0, 0, 0))


class ShippedTextGuardTests(unittest.TestCase):
    def test_replacement_character_is_rejected(self):
        with self.assertRaises(ValueError):
            package.validate_shipped_text("notes.md", "broken \ufffd punctuation")

    def test_literal_backslash_n_in_markdown_is_rejected(self):
        with self.assertRaises(ValueError):
            package.validate_shipped_text("notes.md", "row one\\nrow two")

    def test_fenced_code_block_may_contain_backslash_n(self):
        package.validate_shipped_text(
            "notes.md", "prose\n```text\nexample\\nsequence\n```\nmore prose")

    def test_non_markdown_text_may_contain_backslash_n(self):
        package.validate_shipped_text("example.json", '{"line": "a\\nb"}')


if __name__ == "__main__":
    unittest.main()
