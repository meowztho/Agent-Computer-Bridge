"""No-MCP shell adapter contract tests; never send real Windows input."""
from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_cli as cli
import agent_computer_bridge as core


class ShellEntrypointTests(TestCase):
    def parse(self, *args):
        return cli.parser().parse_args(list(args))

    def test_json_output_supports_unicode_windows_codepage(self):
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with patch.object(cli, "execute", return_value={
            "ok": True, "title": "Window ?\u200b"}), \
             patch.object(cli.sys, "argv", ["agent_cli.py", "help"]), \
             redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        encoded = output.getvalue()
        encoded.encode("cp1252")
        self.assertEqual(json.loads(encoded)["title"], "Window ?\u200b")

    def test_help_is_self_describing_without_mcp(self):
        output = cli.execute(self.parse("help"))
        self.assertTrue(output["ok"])
        self.assertEqual(output["transport"], "shell_without_mcp")
        self.assertIn("agent.cmd observe", " ".join(output["commands"]))

    def test_doctor_does_not_capture_but_diagnose_does(self):
        with patch.object(core, "doctor_report", return_value={"ok": True}) as doctor:
            self.assertTrue(cli.execute(self.parse("doctor"))["ok"])
            doctor.assert_called_once_with(capture_probe=False)
        with patch.object(core, "doctor_report", return_value={"ok": True}) as diagnose, \
             patch.object(cli, "compare_server_diagnostics",
                          return_value={"status": "unavailable"}) as server:
            output = cli.execute(self.parse("diagnose"))
            self.assertTrue(output["ok"])
            self.assertEqual(output["server_diagnostics"]["status"], "unavailable")
            diagnose.assert_called_once_with(capture_probe=True)
            server.assert_called_once()

    def test_standalone_esc_not_misreported_as_server_failure(self):
        with patch.object(core, "PHYSICAL_ESCAPE_STOP_ACTIVE", False), \
             patch.object(core, "PHYSICAL_ESCAPE_STOP_SOURCE", "unavailable"):
            local = core.doctor_report()
            server = core.doctor_report(server_process=True)
        self.assertEqual(local["diagnostic_context"], "standalone_process")
        self.assertEqual(server["diagnostic_context"], "bridge_server")
        local_esc = next(x for x in local["checks"] if x["name"] == "physical_escape_stop")
        server_esc = next(x for x in server["checks"] if x["name"] == "physical_escape_stop")
        self.assertEqual(local_esc["status"], "not_applicable")
        self.assertIsNone(local_esc["active"])
        self.assertEqual(server_esc["status"], "warn")
        self.assertFalse(server_esc["active"])

    def test_diagnose_live_server_comparison_checks_exact_identity(self):
        live = {"diagnostic_context": "bridge_server", "version": core.VERSION,
                "build_id": core.BUILD_ID, "pid": 123,
                "readiness": "ready",
                "checks": [{"name": "physical_escape_stop", "status": "pass",
                            "active": True, "source": "overlay-poll"}]}
        status = {"ok": True, "matches_current_file": True, "reported": {"pid": 123}}
        with patch.object(core, "query_status", return_value=status), \
             patch.object(cli, "api", return_value=live) as http:
            result = cli.compare_server_diagnostics(self.parse("diagnose"))
        self.assertEqual(result["status"], "matching")
        self.assertEqual(result["readiness"], "ready")
        self.assertEqual(result["physical_escape_stop"]["source"], "overlay-poll")
        http.assert_called_once()

        with patch.object(core, "query_status", return_value={
                "ok": True, "matches_current_file": False}), \
             patch.object(cli, "api") as http:
            self.assertEqual(cli.compare_server_diagnostics(
                self.parse("diagnose"))["status"], "build_mismatch")
            http.assert_not_called()
        with patch.object(core, "query_status", return_value={"ok": False}), \
             patch.object(cli, "api") as http:
            self.assertEqual(cli.compare_server_diagnostics(
                self.parse("diagnose"))["status"], "unavailable")
            http.assert_not_called()
        with patch.object(core, "query_status", return_value=status), \
             patch.object(cli, "api", return_value={**live, "pid": 456}):
            self.assertEqual(cli.compare_server_diagnostics(
                self.parse("diagnose"))["status"], "unverified")

    def test_agent_quickstart_does_not_require_mcp(self):
        guidance = core.agent_quickstart()
        steps = " ".join(guidance["first_steps"])
        self.assertIn("already available", steps)
        self.assertIn("agent.cmd start", steps)
        self.assertIn("agent.cmd finish", steps)
        self.assertNotIn("Configure agent_computer_bridge_mcp.py", str(guidance))

    def test_public_discovery_and_agent_help_share_conditional_onboarding(self):
        from types import SimpleNamespace

        fixtures = {
            "screen": {"width": 1920, "height": 1080},
            "virtual_desktop": {"x": 0, "y": 0, "width": 1920, "height": 1080},
            "dpi_awareness": {"active": True},
        }
        responses = []
        handler = object.__new__(core.AgentBridgeHandler)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 7802))
        with patch.object(core.AgentBridgeHandler, "require_auth", return_value=True), \
             patch.object(core.AgentBridgeHandler, "send_json", side_effect=responses.append), \
             patch.object(core, "runtime_status", return_value=fixtures), \
             patch.object(core, "focused_window", return_value=None), \
             patch.object(core, "window_scope_status", return_value={}), \
             patch.object(core, "uia_backend_status", return_value={"available": False}), \
             patch.object(core, "capture_provider_status", return_value={}), \
             patch.object(core, "windows_graphics_capture_backend_status", return_value={"available": False}), \
             patch.object(core, "load_viewport", return_value=None), \
             patch.object(core, "endpoint_catalog", return_value=[]), \
             patch.object(core, "visual_guide_contract", return_value={} ), \
             patch.object(core.CONTROLLER_LEASE, "status", return_value={}), \
             patch.object(core.USER_RECORDING, "status", return_value={}):
            handler.path = "/v1/agent-help"
            handler.do_GET()
            handler.path = "/v1/discovery"
            handler.do_GET()
        quickstart, discovery = responses
        self.assertEqual(discovery["preferred_agent_entry"], quickstart["preferred_agent_entry"])
        self.assertEqual(discovery["recommended_loop"][:3], quickstart["first_steps"][:3])
        steps = " ".join(discovery["recommended_loop"])
        self.assertIn("MCP is already available", steps)
        self.assertIn("agent.cmd doctor", steps)
        self.assertIn("agent.cmd start", steps)
        self.assertIn("agent.cmd finish", steps)
        self.assertIn("state", steps)
        self.assertNotIn("python agent_computer_bridge.py agent-session", str(discovery))
        self.assertNotIn("low-level GET /v1/app-state if MCP is unavailable", str(discovery))

    def test_windows_launcher_propagates_actual_child_codes(self):
        import os
        import shutil
        import subprocess

        if os.name != "nt":
            self.skipTest("Windows CMD launcher contract")
        root = Path(__file__).resolve().parents[1]
        fixture_source = ('import json,os,sys\n'
                          'command=sys.argv[1]\n'
                          'code=int(os.environ.get("ACB_CHILD_CODE","0"))\n'
                          'print(json.dumps({"fixture":command,"ok":code==0}))\n'
                          'sys.exit(code)\n')
        with tempfile.TemporaryDirectory(prefix="acb-launcher-exits-") as tmp:
            temp = Path(tmp)
            shutil.copyfile(root / "agent.cmd", temp / "agent.cmd")
            (temp / "agent_setup.py").write_text(fixture_source, encoding="utf-8")
            (temp / "agent_cli.py").write_text(fixture_source, encoding="utf-8")
            for command in ("doctor", "check", "setup", "help", "windows"):
                for code in (0, 2, 7):
                    with self.subTest(command=command, code=code):
                        env = {**os.environ, "ACB_CHILD_CODE": str(code)}
                        result = subprocess.run(
                            ["cmd", "/d", "/c", str(temp / "agent.cmd"), command],
                            cwd=temp, env=env, capture_output=True, text=True,
                            timeout=15, check=False)
                        self.assertEqual(result.returncode, code, result.stderr)
                        self.assertEqual(json.loads(result.stdout)["fixture"], command)
            # The start preflight deliberately normalizes failed checks to 2.
            env = {**os.environ, "ACB_CHILD_CODE": "7"}
            result = subprocess.run(
                ["cmd", "/d", "/c", str(temp / "agent.cmd"), "start"],
                cwd=temp, env=env, capture_output=True, text=True,
                timeout=15, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["fixture"], "check")

    def test_explicit_failed_capture_probe_blocks_readiness(self):
        with patch.object(core, "_interactive_desktop_status", return_value={
                "available": True, "platform_supported": True}), \
             patch.object(core, "list_monitors", return_value=[{"id": 1}]), \
             patch.object(core, "virtual_desktop_bounds", return_value={}), \
             patch.object(core, "uia_backend_status", return_value={
                 "available": True, "platform_supported": True}), \
             patch.object(core, "capture_provider_status", return_value={
                 "providers": [], "default": None}), \
             patch.object(core, "capture_raw_desktop", side_effect=RuntimeError("capture unavailable")):
            output = core.doctor_report(capture_probe=True)
        probe = next(item for item in output["checks"] if item["name"] == "desktop_capture_probe")
        self.assertEqual(probe["status"], "fail")
        self.assertEqual(output["readiness"], "blocked")
        self.assertFalse(output["ok"])

    def test_start_controller_roundtrip(self):
        reply = {"ok": True, "controller_id": "lease-1",
                 "server": {"ok": True, "already_running": False},
                 "lease": {"lease_seconds": 240}}
        with patch.object(core, "agent_session_bootstrap", return_value=reply) as startup:
            output = cli.execute(self.parse("start"))
        self.assertTrue(output["ok"])
        self.assertEqual(output["controller_id"], "lease-1")
        self.assertTrue(output["server_started"])
        self.assertIn("agent.cmd finish", output["finish"])
        startup.assert_called_once()

    def test_start_propagates_failure(self):
        with patch.object(core, "agent_session_bootstrap",
                          return_value={"ok": False, "stage": "controller-acquire"}):
            self.assertFalse(cli.execute(self.parse("start"))["ok"])

    def test_bootstrap_refuses_wrong_running_version_before_lease(self):
        with patch.object(core, "start_detached",
                          return_value={"ok": False, "error": "wrong build"}), \
             patch.object(core, "bridge_http_json") as http:
            output = core.agent_session_bootstrap("127.0.0.1", 7802, None)
        self.assertFalse(output["ok"])
        self.assertEqual(output["stage"], "server-start")
        http.assert_not_called()

    def test_current_build_required_to_observe_or_act(self):
        args = self.parse("click", "--controller-id", "c", "--state-id", "s",
                          "--image-x", "50", "--image-y", "60")
        with patch.object(core, "query_status",
                          return_value={"ok": True, "matches_current_file": False}), \
             patch.object(cli, "api") as http:
            result = cli.execute(args)
        self.assertEqual(result["error_code"], "BRIDGE_VERSION_OR_CONNECTION_MISMATCH")
        http.assert_not_called()

    def test_observation_compacts_raster_and_exports_image_path(self):
        frame = b"\x89PNG\r\n\x1a\nfixture"
        state = {
            "ok": True, "state_id": "test-unique-state-id",
            "expires_in_seconds": 60,
            "surface": {"kind": "remote_desktop"}, "target_window": {"hwnd": 100},
            "raster": {"base64": base64.b64encode(frame).decode("ascii"),
                       "width": 20, "height": 10, "mime": "image/png"},
            "accessibility": {"available": False, "elements": []},
        }
        calls = []
        def fake_api(args, endpoint, *, data=None, query=None):
            calls.append((endpoint, data, query))
            return {"ok": True} if endpoint == "scope" else state
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli.tempfile, "gettempdir", return_value=directory), \
             patch.object(cli, "api", side_effect=fake_api), \
             patch.object(core, "query_status",
                          return_value={"ok": True, "matches_current_file": True}):
            result = cli.execute(self.parse(
                "observe", "--controller-id", "lease-1", "--hwnd", "100"))
            self.assertEqual(Path(result["screenshot_path"]).read_bytes(), frame)
            self.assertNotIn("base64", json.dumps(result))
            self.assertEqual(result["surface"]["kind"], "remote_desktop")
        self.assertEqual(calls[0][0], "scope")
        self.assertEqual(calls[1][0], "app-state")
        self.assertEqual(calls[1][2]["controller_id"], "lease-1")

    def test_click_passes_fresh_state_via_canonical_endpoint(self):
        args = self.parse("click", "--controller-id", "lease-1", "--state-id", "fresh",
                          "--image-x", "111", "--image-y", "212")
        with patch.object(core, "query_status",
                          return_value={"ok": True, "matches_current_file": True}), \
             patch.object(cli, "api", return_value={
                 "ok": True, "action_sent": False, "dry_run": True}) as api:
            output = cli.execute(args)
        self.assertTrue(output["ok"])
        endpoint = api.call_args.args[1]
        sent = api.call_args.kwargs["data"]
        self.assertEqual(endpoint, "click")
        self.assertEqual(sent["controller_id"], "lease-1")
        self.assertEqual(sent["state_id"], "fresh")
        self.assertEqual((sent["image_x"], sent["image_y"]), (111, 212))

    def test_failed_release_must_not_shutdown_other_owner(self):
        with patch.object(core, "bridge_http_json",
                          return_value={"ok": False, "error_code": "CONTROLLER_BUSY"}) as http:
            output = core.agent_session_finish("127.0.0.1", 7802, None,
                                               "foreign-lease", keep_server=False)
        self.assertFalse(output["ok"])
        self.assertTrue(output["kept_running"])
        http.assert_called_once()

    def test_finish_keeps_existing_server_and_releases_owner(self):
        with patch.object(core, "query_status",
                          return_value={"ok": True, "matches_current_file": True}), \
             patch.object(core, "agent_session_finish", return_value={"ok": True}) as finish:
            self.assertTrue(cli.execute(self.parse(
                "finish", "--controller-id", "lease-1"))["ok"])
        self.assertTrue(finish.call_args.kwargs["keep_server"])

    def test_action_refuses_missing_or_mixed_target(self):
        with self.assertRaises(ValueError):
            cli.action_payload(self.parse(
                "click", "--controller-id", "x", "--state-id", "s"))
        with self.assertRaises(ValueError):
            cli.action_payload(self.parse(
                "click", "--controller-id", "x", "--state-id", "s",
                "--element-index", "2", "--image-x", "2", "--image-y", "3"))


if __name__ == "__main__":
    main()
