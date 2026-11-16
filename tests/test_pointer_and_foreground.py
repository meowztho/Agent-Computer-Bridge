"""Pointer visibility and foreground verification; no live input or desktop capture.

The observation owner must expose the pointer in every coordinate frame of the
state it returns, and must not claim activation succeeded when another window
re-took the foreground. These tests patch the Win32/UIA reads so no real window
or pointer is involved.
"""
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_cli as cli
import agent_computer_bridge as bridge


TARGET = {"hwnd": 44, "pid": 100, "process_name": "app.exe", "class_name": "AppWindow",
          "title": "App", "x": 0, "y": 0, "width": 800, "height": 600, "minimized": False}
SECOND_MONITOR = {"hwnd": 44, "pid": 100, "process_name": "app.exe", "class_name": "AppWindow",
                  "title": "App", "x": -1920, "y": 0, "width": 1920, "height": 1080,
                  "minimized": False}
FOREIGN = {"hwnd": 999, "pid": 5, "process_name": "foreign.exe", "title": "Foreign",
           "x": 0, "y": 0, "width": 400, "height": 300, "minimized": False}


def raster_info(region, width, height, *, occlusion_independent=True):
    return {
        "mime": "image/png", "width": width, "height": height, "bytes": 1, "base64": "",
        "capture_provider": "windows_graphics_capture",
        "capture_binding": {
            "provider": "windows_graphics_capture",
            "occlusion_independent": occlusion_independent,
        },
        "image_region_windows_pixels": dict(region),
    }


def occluder(index, *, title="Cover"):
    return {"hwnd": 700 + index, "title": f"{title} {index}", "pid": 70 + index,
            "process_name": "cover.exe",
            "overlap": {"x": 0, "y": 0, "width": 100, "height": 100}}


class ObservationPointerTests(unittest.TestCase):
    def tearDown(self):
        bridge.clear_window_scope()

    def observe(self, *, target=None, mouse=(10, 20), image=None, hover=None,
                foreground=None, occluders=None, include_image=True,
                surface=None):
        with patch.object(bridge, "_uia_target_window", return_value=dict(target or TARGET)), \
             patch.object(bridge, "_surface_context",
                          return_value=surface or {"kind": "local_window", "remote": False}), \
             patch.object(bridge, "accessibility_tree",
                          return_value={"available": True, "elements": [], "tree": None,
                                        "truncated": False}), \
             patch.object(bridge, "_capture_target_window_state_image",
                          return_value=image), \
             patch.object(bridge, "current_mouse", return_value=mouse), \
             patch.object(bridge, "uia_context_at_point",
                          return_value=hover or {"available": False}), \
             patch.object(bridge, "focused_window", return_value=foreground), \
             patch.object(bridge, "_scope_occluders", return_value=occluders or []):
            return bridge._app_state_snapshot_unlocked(include_image=include_image)

    def test_pointer_image_mapping_reuses_stored_state_region_and_raster(self):
        region = {"x": -1920, "y": 0, "width": 1920, "height": 1080}
        payload = self.observe(
            target=SECOND_MONITOR, mouse=(-960, 270),
            image=raster_info(region, 960, 540))
        pointer = payload["pointer"]
        self.assertEqual(pointer["screen"], {"x": -960, "y": 270})
        self.assertTrue(pointer["inside_target"])
        self.assertEqual(pointer["target_relative"], {"x": 960, "y": 270})
        self.assertEqual(pointer["image"], {"x": 480, "y": 135})
        self.assertIn(payload["state_id"], pointer["meaning"])

        # The admission path must resolve that exact pixel back to the same
        # virtual-desktop point: one convention, not two.
        entry = bridge.OBSERVATION_STATES.get(payload["state_id"])["private"]
        self.assertEqual(entry["raster_width"], 960)
        self.assertEqual(entry["raster_height"], 540)
        self.assertEqual(entry["image_region_windows_pixels"], region)
        self.assertEqual(
            bridge._screen_point_from_raster(480, 135, entry["image_region_windows_pixels"],
                                             entry["raster_width"], entry["raster_height"]),
            {"x": -960, "y": 270})

    def test_pointer_outside_target_has_no_relative_or_image_point(self):
        region = {"x": 0, "y": 0, "width": 800, "height": 600}
        payload = self.observe(mouse=(2000, 1500), image=raster_info(region, 800, 600))
        pointer = payload["pointer"]
        self.assertFalse(pointer["inside_target"])
        self.assertIsNone(pointer["target_relative"])
        self.assertIsNone(pointer["image"])

    def test_no_image_observation_keeps_pointer_and_input_readiness(self):
        payload = self.observe(include_image=False, mouse=(10, 20))
        self.assertIsNone(payload["raster"])
        self.assertIsNone(payload["pointer"]["image"])
        self.assertEqual(payload["pointer"]["screen"], {"x": 10, "y": 20})
        self.assertIn("input_readiness", payload)
        self.assertIsNone(payload["input_readiness"]["capture_occlusion_independent"])

    def test_password_hover_never_leaks_text(self):
        hover = {"available": True, "target": {
            "name": "hunter2", "control_type": "EditControl",
            "automation_id": "pwd", "is_password": True}}
        payload = self.observe(hover=hover)
        self.assertEqual(payload["pointer"]["hover"]["name"], "<password-control>")
        self.assertNotIn("hunter2", json.dumps(payload["pointer"]))

    def test_unknown_privacy_semantics_also_fail_closed(self):
        hover = {"available": True, "target": {
            "name": "secret", "control_type": "EditControl",
            "automation_id": "pwd", "is_password": None}}
        payload = self.observe(hover=hover)
        self.assertEqual(payload["pointer"]["hover"]["name"], "<password-control>")
        self.assertNotIn("secret", json.dumps(payload["pointer"]))

    def test_remote_surface_hover_is_null_with_reason(self):
        payload = self.observe(
            surface={"kind": "remote_desktop", "remote": True},
            hover={"available": True, "target": {"name": "Remote button",
                                                 "control_type": "ButtonControl",
                                                 "is_password": False}})
        self.assertIsNone(payload["pointer"]["hover"])
        self.assertEqual(payload["pointer"]["hover_reason"],
                         "remote_surface_has_no_local_uia")

    def test_input_readiness_reports_foreign_foreground_and_bounded_occluders(self):
        region = {"x": 0, "y": 0, "width": 800, "height": 600}
        payload = self.observe(
            foreground=FOREIGN, image=raster_info(region, 800, 600),
            occluders=[occluder(i) for i in range(5)])
        readiness = payload["input_readiness"]
        self.assertFalse(readiness["target_is_foreground"])
        self.assertFalse(readiness["keyboard_ready"])
        self.assertFalse(readiness["minimized"])
        self.assertEqual(readiness["foreground"],
                         {"hwnd": 999, "pid": 5, "process_name": "foreign.exe",
                          "title": "Foreign"})
        self.assertEqual(len(readiness["occluded_by"]), 3)
        self.assertEqual(readiness["occluded_by"][0]["title"], "Cover 0")
        self.assertTrue(readiness["capture_occlusion_independent"])
        self.assertIn("does not imply input is possible", readiness["pointer_note"])

        # machine_view stays compact but carries the same conclusion.
        compact = payload["machine_view"]["input_readiness"]
        self.assertFalse(compact["keyboard_ready"])
        self.assertEqual(compact["foreground"]["process_name"], "foreign.exe")
        self.assertEqual(compact["occluded_by_count"], 3)
        self.assertEqual(compact["occluded_by_first_title"], "Cover 0")

    def test_keyboard_ready_when_target_is_foreground(self):
        payload = self.observe(foreground=dict(TARGET))
        self.assertTrue(payload["input_readiness"]["target_is_foreground"])
        self.assertTrue(payload["input_readiness"]["keyboard_ready"])
        self.assertTrue(payload["machine_view"]["input_readiness"]["keyboard_ready"])


class ScopeActivationVerificationTests(unittest.TestCase):
    def tearDown(self):
        bridge.clear_window_scope()

    def window(self):
        return {"hwnd": 44, "pid": 100, "process_name": "app.exe", "title": "App",
                "x": 0, "y": 0, "width": 800, "height": 600}

    def test_foreground_retaken_after_activation_is_reported_as_lost(self):
        with patch.object(bridge, "list_windows", return_value=[self.window()]), \
             patch.object(bridge, "activate_window", return_value=True), \
             patch.object(bridge, "focused_window", return_value=dict(FOREIGN)), \
             patch.object(bridge, "active_scope_window", return_value=self.window()), \
             patch.object(bridge.time, "sleep"):
            result = bridge.set_window_scope({"hwnd": 44, "activate": True})
        self.assertTrue(result["ok"])
        self.assertFalse(result["activated"])
        self.assertTrue(result["foreground_lost"])
        self.assertEqual(result["retry"], "reobserve")
        self.assertEqual(result["foreground"]["hwnd"], 999)
        self.assertTrue(result["message"])
        self.assertTrue(result["scope"]["active"])

    def test_verified_foreground_is_activated(self):
        with patch.object(bridge, "list_windows", return_value=[self.window()]), \
             patch.object(bridge, "activate_window", return_value=True), \
             patch.object(bridge, "focused_window", return_value=dict(TARGET)), \
             patch.object(bridge, "active_scope_window", return_value=self.window()), \
             patch.object(bridge.time, "sleep"):
            result = bridge.set_window_scope({"hwnd": 44, "activate": True})
        self.assertTrue(result["activated"])
        self.assertFalse(result["foreground_lost"])
        self.assertEqual(result["retry"], "none")
        self.assertEqual(result["foreground"]["hwnd"], 44)


class ShellFocusAndObservationTests(unittest.TestCase):
    def parse(self, *args):
        return cli.parser().parse_args(list(args))

    def test_focus_builds_scope_request_and_fails_closed_when_unverified(self):
        reply = {"ok": True, "activated": False, "foreground": dict(FOREIGN),
                 "foreground_lost": True, "retry": "reobserve",
                 "message": "another window owns the foreground",
                 "scope": {"active": True, "window": {"hwnd": 44}}}
        with patch.object(cli, "preflight", return_value=None), \
             patch.object(cli, "api", return_value=reply) as http:
            result = cli.execute(self.parse(
                "focus", "--controller-id", "c1", "--hwnd", "44"))
        http.assert_called_once()
        self.assertEqual(http.call_args.args[1], "scope")
        self.assertEqual(http.call_args.kwargs["data"],
                         {"controller_id": "c1", "hwnd": 44, "activate": True})
        self.assertFalse(result["ok"])
        self.assertFalse(result["activated"])
        self.assertTrue(result["foreground_lost"])
        self.assertEqual(result["retry"], "reobserve")
        self.assertEqual(result["scope"]["window"]["hwnd"], 44)

    def test_focus_verified_returns_ok(self):
        reply = {"ok": True, "activated": True, "foreground": dict(TARGET),
                 "foreground_lost": False, "retry": "none", "message": "verified",
                 "scope": {"active": True}}
        with patch.object(cli, "preflight", return_value=None), \
             patch.object(cli, "api", return_value=reply):
            result = cli.execute(self.parse(
                "focus", "--controller-id", "c1", "--hwnd", "44"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["activated"])
        self.assertEqual(result["retry"], "none")

    def test_observation_passes_pointer_readiness_and_contract(self):
        pointer = {"screen": {"x": 5, "y": 6}, "inside_target": True,
                   "image": {"x": 5, "y": 6}, "hover": None, "hover_reason": None,
                   "meaning": "Image coordinates are valid only for state_id s1."}
        readiness = {"target_is_foreground": False, "foreground": None,
                     "minimized": False, "occluded_by": [],
                     "capture_occlusion_independent": None,
                     "keyboard_ready": False, "pointer_note": "note"}
        contract = {"element_target": "state_id + element_index"}
        payload = {"ok": True, "state_id": "s1", "expires_in_seconds": 30,
                   "surface": {}, "target_window": {}, "raster": {},
                   "machine_view": {}, "accessibility": {},
                   "pointer": pointer, "input_readiness": readiness,
                   "contract": contract}
        with patch.object(cli, "preflight", return_value=None), \
             patch.object(cli, "api", return_value=payload) as http:
            result = cli.execute(self.parse(
                "observe", "--controller-id", "c1", "--no-image"))
        self.assertEqual(http.call_args.kwargs["query"]["include_image"], 0)
        self.assertEqual(result["pointer"], pointer)
        self.assertEqual(result["input_readiness"], readiness)
        self.assertEqual(result["contract"], contract)
        self.assertIsNone(result["screenshot_path"])


if __name__ == "__main__":
    unittest.main()
