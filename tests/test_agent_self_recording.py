"""Agent self-recording from canonical action/observation owners.

No native input; all dispatch and screenshot callbacks are isolated.
"""
import base64
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_computer_bridge as bridge


def fake_state(color: str = "red", surface: str = "remote_desktop"):
    picture = bridge.Image.new("RGB", (12, 8), color)
    raw = io.BytesIO()
    picture.save(raw, format="PNG")
    return {
        "state_id": "historical-fake",
        "target_window": {"title": "synthetic safe fixture"},
        "surface": {"kind": surface, "remote": surface == "remote_desktop"},
        "accessibility": {"available": surface != "remote_desktop", "node_count": 0},
        "raster": {
            "width": 12, "height": 8, "capture_provider": "synthetic",
            "base64": base64.b64encode(raw.getvalue()).decode("ascii"),
        },
        "controller_binding": {"controller_id": "owner"},
    }


class AgentSelfRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = bridge.AgentJournalManager()
        self.old = bridge.AGENT_JOURNAL
        bridge.AGENT_JOURNAL = self.journal
        self.addCleanup(lambda: setattr(bridge, "AGENT_JOURNAL", self.old))

    def run_action(self, *, capture_raises=False):
        def observe(**kwargs):
            self.assertEqual(kwargs["observation_source"], "automatic_after_action")
            self.assertEqual(kwargs["controller_id"], "owner")
            if capture_raises:
                raise RuntimeError("synthetic capture unavailable")
            self.journal.observe(fake_state("blue"), "owner",
                                 source=kwargs["observation_source"])
        with patch.object(bridge, "resolve_state_bound_action",
                          return_value={"controller_id": "owner", "text": "private sample"}), \
             patch.object(bridge, "_require_remote_surface_state_binding"), \
             patch.object(bridge.CONTROLLER_LEASE, "validate_action", return_value=None), \
             patch.object(bridge, "_perform_action_unlocked",
                          return_value={"ok": True, "backend_fixture": True}), \
             patch.object(bridge, "_app_state_snapshot_unlocked", side_effect=observe):
            return bridge.perform_action(
                "type", {"controller_id": "owner", "text": "private sample"})

    def test_agent_trace_pairs_before_after_and_masks_text(self):
        directory = pathlib.Path(self.journal.start(
            "owner", output_dir=self.temp.name, goal="Demonstrate a safe task",
            capture_text=False, auto_observe_after_action=True)["session_dir"])
        self.journal.observe(fake_state("red"), "owner", source="explicit")
        result = self.run_action()
        self.assertTrue(result["action_sent"])
        self.assertEqual(result["journal_post_observation"], "recorded_not_verified")
        before_stop = self.journal.read(str(directory))["procedure"]
        self.assertEqual(before_stop["evidence_coverage"]["paired_images"], 1)
        self.assertEqual(before_stop["operations"][0]["visual_comparison"], "image_bytes_differ")
        self.assertFalse(before_stop["operations"][0]["effect_verified"])
        self.assertIn("remote_control_focus_not_accessible_to_local_uia",
                      before_stop["operations"][0]["uncertainties"])
        result = self.journal.stop("owner")
        self.assertTrue(result["procedure_path"])
        project = json.loads((directory / "procedure.json").read_text(encoding="utf-8"))
        self.assertEqual(project["kind"], "agent_execution_procedure")
        self.assertEqual(project["input_source"], "agent_dispatch_not_human")
        operation = project["operations"][0]
        self.assertEqual(operation["before"]["source"], "explicit")
        self.assertEqual(operation["after"]["source"], "automatic_after_action")
        self.assertEqual(operation["dispatch_status"], "sent")
        self.assertFalse(operation["effect_verified"])
        self.assertTrue((directory / operation["after"]["screenshot"]).is_file())
        self.assertNotIn("private sample", (directory / "events.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("private sample", (directory / "procedure.json").read_text(encoding="utf-8"))

    def test_capture_failure_does_not_rewrite_delivery_receipt(self):
        directory = self.journal.start(
            "owner", output_dir=self.temp.name,
            auto_observe_after_action=True)["session_dir"]
        receipt = self.run_action(capture_raises=True)
        self.assertEqual(receipt["dispatch_status"], "sent")
        self.assertEqual(receipt["journal_post_observation"], "unavailable_not_verified")
        procedure = self.journal.read(directory)["procedure"]
        self.assertEqual(procedure["evidence_coverage"]["observation_failures"], 1)
        self.assertIn("post_action_observation_failed", procedure["operations"][0]["uncertainties"])
        self.assertIn("missing_post_action_observation", procedure["operations"][0]["uncertainties"])

    def test_untrusted_journal_image_path_does_not_escape_session(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = pathlib.Path(folder)
            session = parent / "session"
            session.mkdir()
            (parent / "private.png").write_bytes(b"sensitive-example")
            events = [
                {"kind": "observation", "event": 1,
                 "state": {"screenshot": "../private.png"}},
                {"kind": "action_started", "event": 2, "action": "click",
                 "before_observation": 1, "request": {}},
                {"kind": "action_finished", "event": 3, "action_event": 2,
                 "result": {"dispatch_status": "sent"}},
                {"kind": "observation", "event": 4,
                 "source": "automatic_after_action",
                 "state": {"screenshot": "../private.png"}},
            ]
            projection = bridge.AgentJournalManager._project_trace({}, events, session)
            self.assertEqual(projection["evidence_coverage"]["paired_images"], 0)
            self.assertEqual(projection["evidence_coverage"]["image_pairs_unavailable"], 1)
            self.assertEqual(projection["operations"][0]["visual_comparison"],
                             "referenced_image_missing_or_not_safe_to_read")

    def test_later_manual_observation_is_not_event_bound(self):
        events = [
            {"kind": "action_started", "event": 1, "action": "click", "request": {}},
            {"kind": "action_finished", "event": 2, "action_event": 1,
             "result": {"dispatch_status": "sent"}},
            {"kind": "observation", "event": 3, "source": "explicit",
             "state": {"target_window": {"title": "an unrelated later window"}}},
        ]
        with tempfile.TemporaryDirectory() as folder:
            projection = bridge.AgentJournalManager._project_trace(
                {}, events, pathlib.Path(folder))
        self.assertIn("later_observation_not_event_time_bound",
                      projection["operations"][0]["uncertainties"])

    def test_auto_capture_is_opt_in(self):
        directory = self.journal.start(
            "owner", output_dir=self.temp.name,
            auto_observe_after_action=False)["session_dir"]
        self.assertEqual(self.journal.post_action_capture_options("owner"), None)
        self.assertEqual(self.journal.read(directory)["procedure"]["operations"], [])

    def test_evidence_availability_is_comparable_not_equated(self):
        agent = [{
            "operation": "click", "before": {"screenshot": "before.png"},
            "after": {"screenshot": "after.png"},
            "request": {"image_x": 8, "image_y": 9},
            "effect_verified": False,
            "uncertainties": ["remote_control_focus_not_accessible_to_local_uia"],
        }]
        human = [{
            "operation": "click", "prior_step_context": {
                "screenshot": "prior.jpg"},
            "observed_after": {"screenshot": "step.jpg"},
            "target": {"role": "ButtonControl"},
            "confidence": "sampled_after_event",
        }]
        a = bridge.operational_evidence_availability(agent, source="agent")
        h = bridge.operational_evidence_availability(human, source="human")
        for metric in ("with_prior_visual_context", "with_post_action_image_reference",
                       "with_visual_pair_reference"):
            self.assertEqual(a["signals"][metric], h["signals"][metric])
        self.assertEqual(a["signals"]["with_semantic_target_candidate"], 0)
        self.assertEqual(h["signals"]["with_semantic_target_candidate"], 1)
        self.assertEqual(a["signals"]["with_uncertain_remote_focus"], 1)
        self.assertEqual(h["signals"]["with_uncertain_remote_focus"], 0)
        self.assertEqual(a["signals"]["independently_verified_effects"], 0)
        self.assertIn("previous_observed_step", h["prior_visual_context"])
        self.assertIn("not_remote_focus_proof", a["prior_visual_context"])

    def test_human_procedure_references_prior_step_only_as_context(self):
        recorder = bridge.UserRecordingManager()
        recorder._session = {"capture": {"screenshots": True},
                             "intent": bridge.recording_intent("Demonstration", "", "Done")}
        recorder._agent_steps = [
            {"step": 1, "action": "click", "button": "left",
             "app": {"process": "sample.exe"}, "confidence": "unresolved",
             "observed_after": {"screenshot": "first.jpg", "effect_verified": False}},
            {"step": 2, "action": "click", "button": "left",
             "app": {"process": "sample.exe"}, "confidence": "unresolved",
             "observed_after": {"screenshot": "second.jpg", "effect_verified": False}},
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "first.jpg").write_bytes(b"fixture")
            (root / "second.jpg").write_bytes(b"fixture")
            procedure = recorder._build_procedure(root)
        self.assertNotIn("prior_step_context", procedure["operations"][0])
        prior = procedure["operations"][1]["prior_step_context"]
        self.assertEqual(prior["screenshot"], "first.jpg")
        self.assertFalse(prior["time_bound_to_current_action"])
        available = procedure["evidence_availability"]
        self.assertEqual(available["signals"]["with_prior_visual_context"], 1)
        self.assertEqual(available["signals"]["with_post_action_image_reference"], 2)
        self.assertEqual(available["signals"]["independently_verified_effects"], 0)

    def test_read_limits_procedure_but_final_file_contains_all(self):
        directory = self.journal.start("owner", output_dir=self.temp.name)["session_dir"]
        for index in range(55):
            event = self.journal.begin_action("key", {"key": "down"}, "owner")
            self.journal.end_action(event, result={"dispatch_status": "sent"})
        live = self.journal.read(directory)["procedure"]
        self.assertEqual(len(live["operations"]), 50)
        self.assertEqual(live["truncated_operations"], 5)
        self.journal.stop("owner")
        full = json.loads((pathlib.Path(directory) / "procedure.json").read_text(encoding="utf-8"))
        self.assertEqual(len(full["operations"]), 55)
        self.assertEqual(full["evidence_coverage"]["actions"], 55)


if __name__ == "__main__":
    unittest.main()
