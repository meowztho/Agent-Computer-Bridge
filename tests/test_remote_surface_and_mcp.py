"""RDP surface contracts and thin MCP tool adapters; no native desktop input."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_computer_bridge as bridge
import agent_computer_bridge_mcp as mcp


class RemoteSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.old_states = bridge.OBSERVATION_STATES
        bridge.OBSERVATION_STATES = bridge.ObservationStateRegistry()
        self.target = {
            "hwnd": 44, "x": 2560, "y": 0, "width": 1920, "height": 1080,
            "pid": 100, "process_name": "mstsc.exe",
        }
        self.client = {"x": 2560, "y": 0, "width": 1920, "height": 1080}
        self.surface = {
            "kind": "remote_desktop", "remote": True,
            "process_name": "mstsc.exe", "class_name": "TscShellContainerClass",
            "client_rect_windows_pixels": self.client,
            "monitor": {"device": "DISPLAY3"},
        }
        self.sid = bridge.OBSERVATION_STATES.store(private={
            "target_window": self.target, "surface": self.surface,
            "raster_width": 1920, "raster_height": 1080,
            "image_region_windows_pixels": self.client,
            "elements_by_index": {}, "controller_id": None,
        })

    def tearDown(self):
        bridge.OBSERVATION_STATES = self.old_states

    def resolve(self, payload, *, owner=None):
        with patch.object(bridge, "_state_current_window_or_raise", return_value=self.target), \
             patch.object(bridge, "window_at", return_value=self.target if owner is None else owner):
            return bridge.resolve_state_bound_action(
                {"state_id": self.sid, **payload}, action="click")

    def test_rdp_second_monitor_raster_coordinates_are_absolute(self):
        result = self.resolve({"image_x": 580, "image_y": 450})
        self.assertEqual((result["x"], result["y"]), (3140, 450))
        self.assertEqual(result["coordinate_space"], "virtual_desktop")
        self.assertEqual(result["resolved_target"]["type"], "raster")

    def test_rdp_local_uia_index_fails_closed(self):
        with self.assertRaises(bridge.ObservationStateError) as error:
            self.resolve({"element_index": 1})
        self.assertEqual(error.exception.code, "ACTION_UNAVAILABLE")

    def test_rdp_occluded_point_fails_closed(self):
        with self.assertRaises(bridge.ObservationStateError):
            self.resolve({"image_x": 500, "image_y": 300},
                         owner={**self.target, "hwnd": 999})

    def test_remote_state_is_single_use_but_local_state_is_reusable(self):
        self.assertTrue(bridge.OBSERVATION_STATES.consume_remote_action(self.sid))
        with self.assertRaises(bridge.ObservationStateError):
            bridge.OBSERVATION_STATES.get(self.sid)
        local_sid = bridge.OBSERVATION_STATES.store(
            private={"surface": {"remote": False}})
        self.assertFalse(bridge.OBSERVATION_STATES.consume_remote_action(local_sid))
        self.assertTrue(bridge.OBSERVATION_STATES.get(local_sid))

    def test_actual_dispatch_consumes_remote_state_before_native_input(self):
        payload = {"state_id": self.sid, "image_x": 100, "image_y": 120}
        with patch.object(bridge, "_state_current_window_or_raise",
                          return_value=self.target), \
             patch.object(bridge, "window_at", return_value=self.target), \
             patch.object(bridge, "_perform_action_unlocked",
                          return_value={"ok": True, "test_only": True}) as backend, \
             patch.object(bridge, "preview_action",
                          return_value={"ok": True, "dry_run": True}) as preview, \
             patch.object(bridge, "current_action_state", return_value={}):
            dry = bridge.perform_action("click", {**payload, "dry_run": True})
            self.assertFalse(dry["action_sent"])
            self.assertTrue(bridge.OBSERVATION_STATES.get(self.sid))
            sent = bridge.perform_action("click", payload)
            self.assertTrue(sent["action_sent"])
            backend.assert_called_once()
            with self.assertRaises(bridge.ObservationStateError) as stale:
                bridge.perform_action("click", payload)
            self.assertEqual(stale.exception.code, "STALE_STATE")
            self.assertEqual(preview.call_count, 2)

    def test_rdp_client_resize_invalidates_observation(self):
        changed = {**self.surface, "client_rect_windows_pixels": {
            **self.client, "width": 1800}}
        with patch.object(bridge, "window_scope_status", return_value={"active": False}), \
             patch.object(bridge, "focused_window", return_value=self.target), \
             patch.object(bridge, "_surface_context", return_value=changed):
            with self.assertRaises(bridge.ObservationStateError) as error:
                bridge._state_current_window_or_raise(
                    bridge.OBSERVATION_STATES.get(self.sid))
        self.assertEqual(error.exception.code, "STALE_STATE")


class McpMissingToolsTests(unittest.TestCase):
    def test_named_key_uses_existing_canonical_endpoint(self):
        with patch.object(mcp.SESSION, "mutate", return_value={"ok": True}) as mutate:
            mcp.computer_key("enter", presses=2)
            mutate.assert_called_once_with(
                "key", {"key": "enter", "presses": 2, "interval": 0.05})

    def test_drag_uses_state_bound_raster_target(self):
        with patch.object(mcp.SESSION, "mutate", return_value={"ok": True}) as mutate:
            mcp.computer_drag(42, 57, duration=0.4)
            mutate.assert_called_once_with("drag", {
                "image_x": 42, "image_y": 57, "duration": 0.4,
                "button": "left", "dry_run": False})


if __name__ == "__main__":
    unittest.main()
