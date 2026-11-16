"""State-bound element identity re-resolution; no native input or live desktop.

The admission path must re-match an observation-scoped element_index against a
live UIA read. Two distinct HWND-backed controls can share name, control type,
class name and automation id, so the control's own HWND is the only observed
field that separates them. These tests fail if that field is dropped from the
observation or ignored during admission.
"""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_computer_bridge as bridge


TARGET = {"hwnd": 44, "x": 0, "y": 0, "width": 800, "height": 600,
          "pid": 100, "process_name": "app.exe", "class_name": "AppWindow"}


def expected_element(*, native_hwnd=1001, name="Add"):
    return {
        "index": 1, "depth": 1, "name": name, "control_type": "ButtonControl",
        "automation_id": "", "class_name": "Button",
        "bounding_rect_windows_pixels": {"x": 10, "y": 20, "width": 80, "height": 30},
        "native_window_handle": native_hwnd, "enabled": True, "offscreen": False,
        "keyboard_focus": False, "keyboard_focusable": True, "is_password": False,
    }


def live_element(*, native_hwnd=1001, name="Add"):
    return {
        "name": name, "control_type": "ButtonControl", "automation_id": "",
        "class_name": "Button", "framework_id": "Win32",
        "bounding_rect": {"x": 10, "y": 20, "width": 80, "height": 30},
        "enabled": True, "offscreen": False, "keyboard_focus": False,
        "is_password": False, "native_window_handle": native_hwnd,
    }


class UiaIdentityTests(unittest.TestCase):
    def test_identical_semantics_with_distinct_control_hwnd_is_a_different_element(self):
        # Before the control-HWND comparison this returned True: the wrong
        # target passed admission.
        self.assertFalse(bridge._same_uia_identity(
            expected_element(native_hwnd=1001), live_element(native_hwnd=1002)))

    def test_same_control_hwnd_still_matches(self):
        self.assertTrue(bridge._same_uia_identity(
            expected_element(native_hwnd=1001), live_element(native_hwnd=1001)))

    def test_unavailable_control_hwnd_falls_back_to_semantic_fields(self):
        # Non-HWND UIA providers report 0. Never invent a mismatch there.
        self.assertTrue(bridge._same_uia_identity(
            expected_element(native_hwnd=0), live_element(native_hwnd=0)))
        self.assertTrue(bridge._same_uia_identity(
            expected_element(native_hwnd=1001), live_element(native_hwnd=0)))
        self.assertTrue(bridge._same_uia_identity(
            expected_element(native_hwnd=0), live_element(native_hwnd=1001)))

    def test_semantic_difference_still_fails(self):
        self.assertFalse(bridge._same_uia_identity(
            expected_element(), live_element(name="Cancel")))
        self.assertFalse(bridge._same_uia_identity(expected_element(), None))


class StateBoundElementAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.old_states = bridge.OBSERVATION_STATES
        bridge.OBSERVATION_STATES = bridge.ObservationStateRegistry()
        self.addCleanup(lambda: setattr(bridge, "OBSERVATION_STATES", self.old_states))
        # Use the real flattening owner so a dropped field is caught here too.
        tree = {
            "index": 0, "depth": 0, "name": "App", "control_type": "WindowControl",
            "automation_id": "", "class_name": "AppWindow",
            "bounding_rect": {"x": 0, "y": 0, "width": 800, "height": 600},
            "enabled": True, "offscreen": False, "keyboard_focus": False,
            "keyboard_focusable": False, "is_password": False,
            "native_window_handle": 44,
            "children": [{
                "index": 1, "depth": 1, "name": "Add", "control_type": "ButtonControl",
                "automation_id": "", "class_name": "Button",
                "bounding_rect": {"x": 10, "y": 20, "width": 80, "height": 30},
                "enabled": True, "offscreen": False, "keyboard_focus": False,
                "keyboard_focusable": True, "is_password": False,
                "native_window_handle": 1001, "children": [],
            }],
        }
        self.button = next(row for row in bridge._flatten_accessibility_tree(tree)
                           if row["name"] == "Add")
        self.sid = bridge.OBSERVATION_STATES.store(private={
            "target_window": TARGET,
            "surface": {"kind": "local_window", "remote": False},
            "elements_by_index": {self.button["index"]: self.button},
            "raster_width": None, "raster_height": None,
            "image_region_windows_pixels": None, "controller_id": None,
        })

    def resolve(self, live):
        with patch.object(bridge, "_state_current_window_or_raise", return_value=TARGET), \
             patch.object(bridge, "window_at", return_value=TARGET), \
             patch.object(bridge, "uia_context_at_point", return_value={"target": live}):
            return bridge.resolve_state_bound_action(
                {"state_id": self.sid, "element_index": self.button["index"]},
                action="click")

    def test_flattened_element_retains_native_window_handle(self):
        self.assertEqual(self.button["native_window_handle"], 1001)

    def test_replacement_control_with_same_label_is_rejected_at_admission(self):
        with self.assertRaises(bridge.ObservationStateError) as error:
            self.resolve(live_element(native_hwnd=1002))
        self.assertIn("identity changed", str(error.exception))
        self.assertFalse(error.exception.action_sent)

    def test_same_control_resolves_to_its_center(self):
        resolved = self.resolve(live_element(native_hwnd=1001))
        self.assertEqual((resolved["x"], resolved["y"]), (50, 35))
        self.assertEqual(resolved["coordinate_space"], "virtual_desktop")

    def test_non_hwnd_control_still_resolves_by_semantic_identity(self):
        self.button["native_window_handle"] = 0
        resolved = self.resolve(live_element(native_hwnd=0))
        self.assertEqual((resolved["x"], resolved["y"]), (50, 35))


if __name__ == "__main__":
    unittest.main()
