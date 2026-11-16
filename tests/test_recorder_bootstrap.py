"""Recorder one-time dependency bootstrap; no pip or GUI activity in tests."""
import json
import os
import subprocess
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_setup


class RecorderBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.module = self.root / "module.py"
        self.dist = self.root / "METADATA"
        self.module.write_text("fixture", encoding="utf-8")
        self.dist.write_text("fixture", encoding="utf-8")
        self.sig = {"schema": 1, "interpreter": "test-python",
                    "requirements_sha256": "abc", "runtime_sha256": "def"}
        self.patches = [
            patch.object(agent_setup, "recorder_cache_dir", return_value=self.root),
            patch.object(agent_setup, "recorder_fingerprint", return_value=self.sig),
            patch.object(agent_setup, "recorder_dependency_witnesses",
                         side_effect=self.witnesses),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def witnesses(self):
        return [
            {"path": str(path), "size": path.stat().st_size,
             "modified_ns": path.stat().st_mtime_ns}
            for path in (self.module, self.dist) * 3
        ]

    def test_first_start_verifies_subsequent_start_skips_full_check(self):
        with patch.object(agent_setup, "status", return_value={"ok": True}) as check, \
             patch.object(agent_setup, "install") as install:
            first = agent_setup.recorder_ready()
            second = agent_setup.recorder_ready()
        self.assertTrue(first["ok"])
        self.assertTrue(first["checked"])
        self.assertFalse(first["cache_hit"])
        self.assertFalse(second["checked"])
        self.assertTrue(second["cache_hit"])
        self.assertTrue(second["ok"])
        check.assert_called_once()
        install.assert_not_called()
        saved = json.loads((self.root / "recorder-dependencies.json").read_text())
        self.assertEqual(saved["fingerprint"], self.sig)

    def test_requirements_or_runtime_change_invalidates_cache(self):
        with patch.object(agent_setup, "status", return_value={"ok": True}) as check:
            self.assertFalse(agent_setup.recorder_ready()["cache_hit"])
            self.sig["requirements_sha256"] = "new-file"
            self.assertFalse(agent_setup.recorder_ready()["cache_hit"])
            self.sig["runtime_sha256"] = "new-release"
            self.assertFalse(agent_setup.recorder_ready()["cache_hit"])
        self.assertEqual(check.call_count, 3)

    def test_missing_or_replaced_installed_package_rechecks(self):
        with patch.object(agent_setup, "status", return_value={"ok": True}) as check:
            agent_setup.recorder_ready()
            self.module.write_text("updated", encoding="utf-8")
            self.assertFalse(agent_setup.recorder_ready()["cache_hit"])
            self.assertTrue(agent_setup.recorder_ready()["cache_hit"])
            self.dist.unlink()
            # Fake verifier cannot legitimately issue a new stamp if missing.
            with self.assertRaises(FileNotFoundError):
                agent_setup.recorder_ready()
        self.assertEqual(check.call_count, 3)

    def test_explicit_repair_bypasses_valid_cache(self):
        with patch.object(agent_setup, "status", return_value={"ok": True}) as check:
            agent_setup.recorder_ready()
            repaired = agent_setup.recorder_ready(repair=True)
        self.assertTrue(repaired["checked"])
        self.assertFalse(repaired["cache_hit"])
        self.assertEqual(check.call_count, 2)

    def test_missing_libraries_auto_installed_then_cached(self):
        with patch.object(agent_setup, "status", return_value={
                "ok": False, "python": {"supported": True}, "windows": True}) as check, \
             patch.object(agent_setup, "install",
                          return_value={"ok": True, "install_invoked": True}) as install:
            first = agent_setup.recorder_ready()
            second = agent_setup.recorder_ready()
        self.assertTrue(first["ok"])
        self.assertTrue(first["installed"])
        self.assertTrue(second["cache_hit"])
        check.assert_called_once()
        install.assert_called_once_with()

    def test_failed_install_does_not_cache_and_is_retried(self):
        bad = {"ok": False, "install_invoked": True, "installer_tail": "network unavailable"}
        with patch.object(agent_setup, "status", return_value={"ok": False}), \
             patch.object(agent_setup, "install", return_value=bad) as installer:
            first = agent_setup.recorder_ready()
            second = agent_setup.recorder_ready()
        self.assertFalse(first["ok"])
        self.assertIn("network unavailable", first["error"])
        self.assertFalse(second["ok"])
        self.assertFalse((self.root / "recorder-dependencies.json").exists())
        self.assertEqual(installer.call_count, 2)

    def test_missing_pip_recorder_preserves_interpreter_repair_guidance_without_cache(self):
        guidance = 'Run "C:\\\\temp\\\\venv\\\\Scripts\\\\python.exe" -m ensurepip --upgrade, then retry.'
        bad = {"ok": False, "install_invoked": True, "installer_tail": "No module named pip",
               "next": guidance}
        with patch.object(agent_setup, "status", return_value={"ok": False}), \
             patch.object(agent_setup, "install", return_value=bad) as installer:
            result = agent_setup.recorder_ready()
        self.assertFalse(result["ok"])
        self.assertEqual(result["next"], guidance)
        self.assertIn("No module named pip", result["error"])
        self.assertTrue(result["installed"])
        self.assertFalse((self.root / "recorder-dependencies.json").exists())
        installer.assert_called_once_with()

    def test_stale_or_invalid_cache_is_rechecked(self):
        (self.root / "recorder-dependencies.json").write_text("{bad", encoding="utf-8")
        with patch.object(agent_setup, "status", return_value={"ok": True}) as check:
            ready = agent_setup.recorder_ready()
        self.assertTrue(ready["checked"])
        check.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows byte-range locks only")
    def test_two_first_launches_serialize_and_reuse_the_receipt(self):
        environment = dict(os.environ, LOCALAPPDATA=self.temp.name)
        script = pathlib.Path(__file__).resolve().parents[1] / "agent_setup.py"
        command = [sys.executable, str(script), "recorder-ready"]
        first = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=environment)
        second = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=environment)
        results = []
        for process in (first, second):
            out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(sum(int(result["checked"]) for result in results), 1)
        self.assertEqual(sum(int(result["cache_hit"]) for result in results), 1)

    def test_cmd_and_bat_dispatch_same_bootstrap(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        batch = (root / "record.cmd").read_text(encoding="utf-8").lower()
        alias = (root / "record.bat").read_text(encoding="utf-8").lower()
        self.assertIn("agent_setup.py", batch)
        self.assertIn("recorder-ready", batch)
        self.assertIn("record-ui", batch)
        self.assertIn("recorder-ready --repair", batch)
        self.assertIn("--check", batch)
        self.assertIn("record.cmd", alias)


if __name__ == "__main__":
    unittest.main()
