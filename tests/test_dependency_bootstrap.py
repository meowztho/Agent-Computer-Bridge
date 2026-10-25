"""No-dependency bootstrap tests. All installation subprocesses are mocked."""
from __future__ import annotations

import importlib.metadata
import json
import sys
import unittest
from unittest.mock import patch

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_setup


class BootstrapDependenciesTests(unittest.TestCase):
    def test_core_exact_requirements_are_manifest_driven(self):
        self.assertEqual(agent_setup.requirements("core"), {
            "PyAutoGUI": "==0.9.54", "Pillow": "==12.3.0",
            "uiautomation": "==2.0.29",
        })
        self.assertEqual(agent_setup.requirements("mcp"), {"mcp": ">=2,<3"})
        self.assertEqual(agent_setup.requirements("wgc"), {"windows-capture": "==2.0.1"})

    def test_optional_dependency_version_ranges_are_enforced(self):
        self.assertTrue(agent_setup.version_matches("2.16.0", ">=2,<3"))
        self.assertFalse(agent_setup.version_matches("3.0.0", ">=2,<3"))
        self.assertTrue(agent_setup.version_matches("6.22.3", ">=6.22,<7"))
        self.assertFalse(agent_setup.version_matches("7.0.0", ">=6.22,<7"))
        self.assertTrue(agent_setup.version_matches("12.3", "==12.3.0"))
        self.assertFalse(agent_setup.version_matches("12.2.0", "==12.3.0"))
        self.assertFalse(agent_setup.version_matches("6.22.0rc1", ">=6.22,<7"))

    def test_missing_dependency_is_machine_readable_before_core_import(self):
        def unavailable(name):
            if name == "PyAutoGUI":
                raise importlib.metadata.PackageNotFoundError(name)
            return {"Pillow": "12.3.0", "uiautomation": "2.0.29"}[name]
        with patch.object(agent_setup.importlib.metadata, "version",
                          side_effect=unavailable), \
             patch.object(agent_setup.importlib.util, "find_spec",
                          return_value=object()):
            data = agent_setup.status()
        self.assertFalse(data["ok"])
        self.assertEqual(data["groups"]["core"]["missing"][0]["package"], "PyAutoGUI")
        self.assertFalse(data["mcp_configuration_required"])
        self.assertIn("agent.cmd setup", data["next"])

    def test_incompatible_version_cannot_pass_preflight(self):
        def version(name):
            return "0.1.0" if name == "PyAutoGUI" else {
                "Pillow": "12.3.0", "uiautomation": "2.0.29"}[name]
        with patch.object(agent_setup.importlib.metadata, "version",
                          side_effect=version), \
             patch.object(agent_setup.importlib.util, "find_spec",
                          return_value=object()):
            data = agent_setup.status()
        self.assertFalse(data["ok"])
        self.assertEqual(data["groups"]["core"]["incompatible"][0]["package"],
                         "PyAutoGUI")

    def test_broken_transitive_import_fails_even_when_versions_match(self):
        completed = type("Probe", (), {"returncode": 1,
                                       "stderr": "Missing a transitive runtime module"})()
        def version(name):
            return {"PyAutoGUI": "0.9.54", "Pillow": "12.3.0",
                    "uiautomation": "2.0.29"}[name]
        with patch.object(agent_setup.importlib.metadata, "version",
                          side_effect=version), \
             patch.object(agent_setup.importlib.util, "find_spec",
                          return_value=object()), \
             patch.object(agent_setup.subprocess, "run",
                          return_value=completed) as run:
            result = agent_setup.status()
        self.assertFalse(result["ok"])
        self.assertTrue(result["groups"]["core"]["ready"])
        self.assertFalse(result["core_runtime_import"]["ok"])
        self.assertIn("transitive", result["core_runtime_import"]["error_tail"])
        run.assert_called_once()

    def test_setup_no_changes_when_dependencies_satisfied(self):
        with patch.object(agent_setup, "status",
                          return_value={"ok": True, "python": {"supported": True},
                                        "windows": True, "groups": {"core": {"ready": True}}}), \
             patch.object(agent_setup.subprocess, "run") as run:
            data = agent_setup.install()
        self.assertTrue(data["ok"])
        self.assertFalse(data["install_invoked"])
        run.assert_not_called()

    def test_explicit_setup_only_installs_selected_requirements(self):
        before = {"ok": False, "python": {"supported": True}, "windows": True,
                  "groups": {"core": {"ready": False}, "wgc": {"ready": False}}}
        after = {"ok": True, "python": {"supported": True}, "windows": True,
                 "groups": {"core": {"ready": True}, "wgc": {"ready": True}}}
        completed = type("Result", (), {"returncode": 0, "stdout": "Installed",
                                          "stderr": ""})()
        with patch.object(agent_setup, "status", side_effect=[before, after]), \
             patch.object(agent_setup.subprocess, "run",
                          return_value=completed) as run:
            result = agent_setup.install(with_wgc=True)
        args = run.call_args.args[0]
        self.assertTrue(result["ok"])
        self.assertTrue(result["install_invoked"])
        self.assertIn("requirements.txt", " ".join(args))
        self.assertIn("requirements-wgc.txt", " ".join(args))
        self.assertNotIn("requirements-mcp.txt", " ".join(args))
        self.assertNotIn("requirements-portable.txt", " ".join(args))

    def test_pip_missing_returns_actionable_error_without_hidden_retry(self):
        before = {"ok": False, "python": {"supported": True}, "windows": True,
                  "groups": {"core": {"ready": False}}}
        with patch.object(agent_setup, "status", return_value=before), \
             patch.object(agent_setup.subprocess, "run",
                          side_effect=OSError("pip missing")):
            result = agent_setup.install()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "OSError")
        self.assertIn("ensurepip", result["next"])

    def test_help_is_stdlib_only_and_explains_optional_groups(self):
        self.assertEqual(agent_setup.main(["help"]), 0)
        p = agent_setup.status()
        self.assertIn("core", p["groups"])
        self.assertNotIn("mcp", p["groups"])
        self.assertNotIn("wgc", p["groups"])


if __name__ == "__main__":
    unittest.main()
