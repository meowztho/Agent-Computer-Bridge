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
