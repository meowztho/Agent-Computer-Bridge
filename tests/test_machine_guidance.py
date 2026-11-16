"""Semantic evidence for text-only agents, no new input or image interpretation owner."""
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_computer_bridge as core
import agent_cli


def control(index, name, role="ButtonControl", *,
            password=False, enabled=True, offscreen=False):
    return {"index": index, "name": name, "automation_id": "", "control_type": role,
            "enabled": enabled, "offscreen": offscreen, "is_password": password,
            "keyboard_focus": False}


class MachineViewTests(unittest.TestCase):
    def test_live_local_controls_have_current_indices_and_no_credentials(self):
        state = {"available": True, "truncated": False, "elements": [
            control(0, "Save"),
            control(1, "Password", "EditControl", password=True),
            control(2, "Inactive", enabled=False),
            control(3, "Offscreen", offscreen=True),
            control(4, "UnknownType", role="PaneControl"),
            control(5, "Search", "EditControl"),
        ]}
        view = core.machine_readable_live_view(
            {"kind": "local_window"}, state)
        self.assertEqual([x["element_index"] for x in view["control_candidates"]], [0, 5])
        self.assertFalse(view["vision"]["recommended"])
        self.assertEqual(view["candidate_total"], 2)
        self.assertNotIn("Password", json.dumps(view))
        self.assertTrue(all(x["candidate_only"] for x in view["control_candidates"]))

    def test_symbol_only_buttons_do_not_satisfy_blind_agent(self):
        view = core.machine_readable_live_view(
            {"kind": "local_window"},
            {"available": True, "elements": [
                control(0, "?"), control(1, "?"),
                control(2, "?"), control(3, "X"),
            ]})
        self.assertEqual(view["candidate_total"], 0)
        self.assertEqual(view["symbolic_or_uninformative_labels"], 4)
        self.assertTrue(view["vision"]["recommended"])
        self.assertEqual(view["vision"]["reason"],
                         "no_trustworthy_local_control_candidate")

    def test_remote_uia_never_promotes_local_shell_controls(self):
        view = core.machine_readable_live_view(
            {"kind": "remote_desktop", "remote": True},
            {"available": True, "elements": [control(1, "Remote save")]})
        self.assertEqual(view["control_candidates"], [])
        self.assertTrue(view["vision"]["recommended"])
        self.assertEqual(view["vision"]["reason"], "remote_surface_has_no_remote_uia")
        self.assertFalse(view["uia_available_for_target"])

    def test_no_uia_or_truncated_metadata_does_not_invent_targets(self):
        view = core.machine_readable_live_view(
            {"kind": "local_window"},
            {"available": False, "truncated": True, "elements": [
                control(2, "potential but unsupported")]})
        self.assertFalse(view["control_candidates"])
        self.assertTrue(view["uia_truncated"])
        self.assertTrue(view["vision"]["recommended"])

    def test_handoff_text_first_remote_and_prioritized_visual_tasks(self):
        recorder = core.UserRecordingManager()
        recorder._agent_steps = [
            {"step": 1, "action": "click",
             "button": "left",
             "surface": {"kind": "local_window"}, "app": {"process": "editor.exe"},
             "target": {"name": "Settings", "role": "ButtonControl"},
             "screenshot": "screenshots/1.jpg", "screenshot_relation": "after_input"},
            {"step": 2, "action": "click", "button": "right",
             "surface": {"kind": "remote_desktop"}, "app": {"process": "mstsc.exe"},
             "screenshot": "screenshots/2.jpg",
             "image_at": {"x": 420, "y": 210}, "screenshot_relation": "after_input"},
            {"step": 3, "action": "click", "button": "left",
             "surface": {"kind": "remote_desktop"}, "app": {"process": "mstsc.exe"},
             "screenshot": "screenshots/3.jpg",
             "pre_input_screenshot": "screenshots/3_before.jpg",
             "pre_input_age_ms": 20, "screenshot_relation": "after_input"},
        ]
        handoff = recorder._build_agent_handoff({"trace_integrity": {}})
        guidance = handoff["machine_view"]
        self.assertEqual(guidance["named_local_candidates"], 1)
        self.assertEqual(guidance["unresolved_pointer_steps"], 2)
        self.assertEqual(handoff["steps"][0]["machine_instruction"]["target_candidate"],
                         "Settings")
        self.assertEqual(handoff["steps"][0]["machine_instruction"]["confidence"],
                         "asynchronous_uia_candidate_not_event_bound")
        self.assertIsNone(handoff["steps"][1]["machine_instruction"]["target_candidate"])
        tasks = guidance["vision_review"]["requests"]
        self.assertEqual(tasks[0]["step"], 3)  # menu pre-image prioritized
        self.assertEqual(tasks[0]["before_image"], "screenshots/3_before.jpg")
        self.assertTrue(tasks[0]["read_only"])
        self.assertIn("historical", guidance["vision_review"]["note"])
        self.assertNotIn("remote_target", json.dumps(handoff))

    def test_recorded_glyph_only_uia_is_not_meaningful_for_text_agent(self):
        recorder = core.UserRecordingManager()
        recorder._agent_steps = [{
            "step": 1, "action": "click",
            "surface": {"kind": "local_window"},
            "app": {"process": "demo.exe"},
            "target": {"name": "?", "role": "ButtonControl"},
            "screenshot": "screenshots/1.jpg",
        }]
        view = recorder._build_agent_handoff({})
        self.assertEqual(view["machine_view"]["named_local_candidates"], 0)
        self.assertTrue(view["machine_view"]["vision_review"]["recommended"])
        self.assertIsNone(view["steps"][0]["machine_instruction"]["target_candidate"])

    def test_no_unjustified_vision_escalation_for_named_local_steps(self):
        recorder = core.UserRecordingManager()
        recorder._agent_steps = [
            {"step": 1, "action": "click", "surface": {"kind": "local_window"},
             "app": {"process": "a.exe"},
             "target": {"name": "OK"}, "screenshot": "1.jpg"},
        ]
        h = recorder._build_agent_handoff({})
        self.assertFalse(h["machine_view"]["vision_review"]["recommended"])
        self.assertEqual(h["machine_view"]["vision_review"]["requests"], [])

    def test_shell_passes_shared_machine_view_with_screenshot_reference(self):
        response = {"ok": True, "state_id": "id", "surface": {"kind": "remote_desktop"},
                    "target_window": {"hwnd": 123},
                    "raster": {"base64": None},
                    "accessibility": {"available": False, "elements": []},
                    "machine_view": {"kind": "live_semantic_orientation",
                                     "vision": {"recommended": True}}}
        with patch.object(agent_cli, "api", return_value=response):
            a = agent_cli.parser().parse_args(["observe", "--controller-id", "controller"])
            result = agent_cli.observation(a)
        self.assertTrue(result["machine_view"]["vision"]["recommended"])
        self.assertTrue(result["machine_view"]["vision"]["requires_external_image_reader"])
        self.assertIsNone(result["machine_view"]["vision"]["screenshot_path"])


if __name__ == "__main__":
    unittest.main()
