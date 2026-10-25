"""Regression coverage for lossless demonstrations and durable agent recovery.

Run: py -3 -m unittest discover -s tests -v
No native input is sent by these tests.
"""
import base64
import ctypes
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_computer_bridge as bridge


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.recorder = bridge.UserRecordingManager()
        self.actions = []
        self.recorder._event_targets_ignored_ui = lambda event: False
        self.recorder._emit_step = lambda action, **kwargs: self.actions.append(action)
        self.context = {"hwnd": 123, "pid": 456}

    def key(self, text=None, name=None, modifiers=(), shortcut=None):
        self.recorder._consume_event({
            "source": "keyboard", "phase": "down", "category": "text" if text else "navigation",
            "typed_text": text, "safe_key": name, "modifiers": modifiers, "shortcut": shortcut,
            "epoch": 100, "monotonic": 100, "event_context": self.context,
        })

    def flush(self):
        self.recorder._flush_due(102, force=True)

    def test_tab_splits_fields_without_reordering(self):
        self.key(text="A"); self.key(name="tab"); self.key(text="B"); self.flush()
        self.assertEqual([a["text"] for a in self.actions], ["A", "", "B"])
        self.assertEqual(self.actions[1]["input_sequence"], [{"kind": "key", "key": "tab", "modifiers": []}])

    def test_repeated_keys_and_edits_are_lossless(self):
        for key in ("left", "left", "left", "backspace", "backspace"):
            self.key(name=key)
        self.flush()
        self.assertEqual([a["safe_keys"][0] for a in self.actions], ["left", "left", "left", "backspace", "backspace"])

    def test_shortcut_stays_between_text_runs(self):
        self.key(text="old"); self.key(shortcut="ctrl+a", modifiers=("ctrl",)); self.key(text="new"); self.flush()
        self.assertEqual([a["text"] for a in self.actions], ["old", "", "new"])
        self.assertEqual(self.actions[1]["input_sequence"], [{"kind": "hotkey", "keys": ["ctrl", "a"]}])

    def test_pointer_cannot_overtake_pending_text(self):
        self.key(text="A")
        for phase in ("down", "up"):
            self.recorder._consume_event({"source": "mouse", "phase": phase, "button": "left",
                "x": 10, "y": 20, "epoch": 100.1, "monotonic": 100.1, "event_context": self.context})
        self.flush()
        self.assertEqual([a["type"] for a in self.actions], ["keyboard_input", "click"])

    def test_scroll_cannot_be_overtaken_by_text(self):
        self.recorder._consume_event({"source": "mouse", "phase": "wheel", "x": 10, "y": 20,
            "delta": -120, "epoch": 100, "monotonic": 100, "event_context": self.context})
        self.key(text="A"); self.flush()
        self.assertEqual([a["type"] for a in self.actions], ["scroll", "keyboard_input"])

    def test_exact_trace_does_not_merge_different_fields(self):
        r = self.recorder
        r._agent_step_projection = lambda step: step
        for n, name in enumerate(("First", "Second"), 1):
            r._append_agent_step({"step": n, "action": "type", "app": {"process": "test"},
                                  "target": {"name": name}, "text": name})
        self.assertEqual(len(r._agent_steps), 2)

    @staticmethod
    def target(name, identity="", role="EditControl", size=100):
        return {"name": name, "automation_id": identity, "class_name": "SharedClass", "role": role,
                "bounds": {"x": 0, "y": 0, "width": size, "height": 30 if size > 2 else 1}}

    def procedure(self, steps):
        self.recorder._agent_steps = steps
        with tempfile.TemporaryDirectory() as directory:
            return self.recorder._build_procedure(pathlib.Path(directory))

    def test_same_class_does_not_mean_same_target(self):
        self.assertFalse(self.recorder._procedure_same_target(self.target("First", "a"), self.target("Second", "b")))

    def test_scroll_delta_axis_and_units_survive_both_projections(self):
        for horizontal, delta in ((False, -840), (True, 60)):
            with self.subTest(horizontal=horizontal):
                compact = self.recorder._agent_step_projection({"step": 1, "action": {
                    "type": "scroll", "delta": delta, "horizontal": horizontal,
                    "direction": "right" if horizontal else "down", "events": 7,
                    "duration_ms": 230, "x": -50, "y": 120}})
                operation = self.procedure([compact])["operations"][0]
                self.assertEqual(operation["delta"], delta)
                self.assertEqual(operation["horizontal"], horizontal)
                self.assertEqual(operation["axis"], "horizontal" if horizontal else "vertical")
                self.assertEqual(operation["delta_unit"], "windows_wheel_delta")
                self.assertEqual(operation["duration_ms"], 230)

    def test_before_frame_is_never_a_post_action_result(self):
        compact = self.recorder._agent_step_projection({"step": 1, "action": {"type": "click", "x": 20, "y": 10},
                                                        "capture_timing": {"selection": "before_input_no_clean_after", "quality": "before_input"},
                                                        "screenshot": "before.jpg"})
        operation = self.procedure([compact])["operations"][0]
        self.assertEqual(operation["evidence_screenshot"], "before.jpg")
        self.assertNotIn("screenshot", operation["observed_after"])
        self.assertIn("no_clean_post_input_frame", operation["uncertainties"])

    @staticmethod
    def context_target(name, role="EditControl", width=100):
        return {"target": {"name": name, "control_type": role,
                           "bounding_rect": {"x": 0, "y": 0, "width": width, "height": 30}}}

    def test_click_never_inherits_later_focus_or_named_ancestor(self):
        step = {"step": 1, "action": {"type": "click", "x": 20, "y": 10},
                "uia": {"target": {"control_type": "ImageControl"},
                        "stack": [{"name": "Unrelated ancestor", "control_type": "ButtonControl"}]},
                "focus_after": self.context_target("I6"), "target_confidence": "verified"}
        compact = self.recorder._agent_step_projection(step)
        self.assertNotIn("target", compact)
        self.assertEqual(compact["observed_after"]["focus_target"]["name"], "I6")
        self.assertEqual(compact["confidence"], "unresolved")
        p = self.procedure([compact])
        self.assertGreater(p["procedure_quality"]["ambiguity_count"], 0)
        self.assertEqual(p["operations"][0]["operation"], "click")

    def test_keyboard_does_not_inherit_previous_cell(self):
        self.recorder._append_agent_step({"step": 1, "action": {"type": "click", "x": 20, "y": 10},
                                         "uia": self.context_target("I6")})
        self.recorder._append_agent_step({"step": 2, "action": {"type": "keyboard_input", "text": "="},
                                         "uia": self.context_target(""), "focus_after": self.context_target("I7")})
        second = self.recorder._agent_steps[1]
        self.assertNotIn("target", second)
        self.assertEqual(second["text"], "=")
        self.assertEqual(second["observed_after"]["focus_target"]["name"], "I7")

    def test_pointer_candidate_must_contain_click_and_not_be_tiny(self):
        for x, width in ((110, 100), (0, 1)):
            with self.subTest(x=x, width=width):
                compact = self.recorder._agent_step_projection({"action": {"type": "click", "x": x, "y": 10},
                                                                "uia": self.context_target("Candidate", width=width)})
                self.assertNotIn("target", compact)
                self.assertEqual(compact["sampled_target"]["name"], "Candidate")

    def test_sampled_button_is_not_verified_activation(self):
        compact = self.recorder._agent_step_projection({"step": 1, "action": {"type": "click", "x": 20, "y": 10},
                                                        "uia": self.context_target("Save", role="ButtonControl"),
                                                        "focus_after": self.context_target("Next")})
        p = self.procedure([compact])
        operation = p["operations"][0]
        self.assertEqual(operation["operation"], "click")
        self.assertEqual(operation["target"]["name"], "Save")
        self.assertEqual(operation["observed_after"]["focus_target"]["name"], "Next")
        self.assertEqual(operation["confidence"], "sampled_after_event")
        self.assertFalse(operation["observed_after"]["effect_verified"])

    def test_capture_overlap_survives_procedure_export(self):
        r = self.recorder
        for number, stamp in ((1, "2026-10-05T12:00:00+02:00"), (2, "2026-10-05T12:00:00.150+02:00")):
            r._append_agent_step({"step": number, "time": stamp, "action": {"type": "keyboard_input", "text": "A"},
                                  "capture_timing": {"capture_started_at": "2026-10-05T12:00:00.100+02:00", "capture_duration_ms": 200},
                                  "capture_binding": {"screen_mapping": {"x": -100, "y": 0}},
                                  "screenshot": f"screenshots/{number}.jpg"})
        p = self.procedure(r._agent_steps)
        first = p["operations"][0]
        self.assertIn("capture_overlaps_later_input", first["uncertainties"])
        self.assertEqual(first["observed_after"]["capture_timing"]["overlaps_later_steps"], [2])
        self.assertEqual(first["observed_after"]["capture_binding"]["screen_mapping"]["x"], -100)
        self.assertTrue(any(a["reason"] == "capture_overlaps_later_input" for a in p["ambiguities"]))

    def test_legacy_action_target_is_not_claimed_as_observed_focus(self):
        p = self.procedure([{"step": 1, "action": "click", "target": self.target("Old action target"), "screenshot": "old.jpg"}])
        state = p["operations"][0]["observed_after"]
        self.assertNotIn("focus_target", state)
        self.assertEqual(state["source"], "legacy_screenshot_only")
        self.assertIn("goal", p["procedure_quality"]["missing_intent"])

    def test_sampled_edit_is_typing_evidence_not_a_verified_fill(self):
        compact = self.recorder._agent_step_projection({"step": 1, "action": {"type": "keyboard_input", "text": "=8+2"},
                                                        "uia": self.context_target("I6")})
        p = self.procedure([compact])
        self.assertEqual(p["operations"][0]["operation"], "type")
        self.assertEqual(p["demonstrated_inputs"][0]["demonstrated_value"], "=8+2")
        self.assertIsNone(p["demonstrated_inputs"][0]["field"])

    def test_after_app_is_sampled_window_not_event_window(self):
        after = self.context_target("Next")
        after["window"] = {"process_name": "test.exe", "title": "New title"}
        compact = self.recorder._agent_step_projection({"step": 1, "action": {"type": "keyboard_input", "text": "A"},
                                                        "window": {"process_name": "test.exe", "title": "Old title"},
                                                        "focus_after": after})
        p = self.procedure([compact])
        self.assertEqual(p["operations"][0]["app"]["title"], "Old title")
        self.assertEqual(p["operations"][0]["observed_after"]["app"]["title"], "New title")

    def test_missing_focus_is_not_a_proved_focus_change(self):
        known = {"focus_target": {"name": "Same field", "role": "EditControl"}}
        missing = {"focus_target": None}
        with tempfile.TemporaryDirectory() as directory:
            for before, after in ((known, missing), (missing, known)):
                self.assertIsNone(self.recorder._procedure_observed_effect(pathlib.Path(directory), before, after))
            effect = self.recorder._procedure_observed_effect(pathlib.Path(directory), known, {"focus_target": {"name": "Changed readout"}})
            self.assertEqual(effect["focus_label_after"], "Changed readout")
            self.assertNotIn("focus_target_changed", effect)

    def test_fill_preserves_source_steps(self):
        target = self.target("Title", "title")
        p = self.procedure([{"step": 1, "action": "click", "app": {"process": "test"}, "target": target},
                            {"step": 2, "action": "type", "app": {"process": "test"}, "target": target, "text": "Example"}])
        self.assertEqual(p["operations"][0]["operation"], "fill")
        self.assertEqual(p["operations"][0]["evidence_steps"], [1, 2])

    def test_custom_grid_remains_explicit(self):
        target = self.target("Synthetic grid focus", "readout", size=1)
        p = self.procedure([{"step": 1, "action": "click", "target": target},
                            {"step": 2, "action": "type", "target": target, "text": "="}])
        self.assertEqual([o["operation"] for o in p["operations"]], ["click", "type"])

    def test_selection_never_merges_different_apps(self):
        p = self.procedure([{"step": 1, "action": "click", "app": {"process": "a"}, "target": self.target("Category", role="ComboBoxControl")},
                            {"step": 2, "action": "click", "app": {"process": "b"}, "target": self.target("Other", role="ListItemControl")}])
        self.assertEqual(len(p["operations"]), 2)
        self.assertNotIn("select", [o["operation"] for o in p["operations"]])

    def test_right_click_is_not_activation(self):
        p = self.procedure([{"step": 1, "action": "click", "button": "right", "target": self.target("Save", role="ButtonControl")}])
        self.assertEqual(p["operations"][0]["operation"], "click")

    def test_intent_is_explicit_and_example_values_are_not_rules(self):
        self.recorder._session = {"intent": bridge.recording_intent("Create an example", "Blank form", "Example appears")}
        p = self.procedure([{"step": 1, "action": "type", "target": self.target("Price"), "text": "19.99"}])
        self.assertTrue(p["procedure_quality"]["intent_complete"])
        self.assertEqual(p["intent"]["source"], "user_supplied")
        self.assertEqual(p["operations"][0]["value_semantics"], "demonstrated_value_not_inferred_rule")

    def test_unknown_or_password_focus_redacts_every_text_projection(self):
        for context in ({"target": None, "stack": []},
                        {"target": {"control_type": "EditControl"}, "stack": []},
                        {"target": {"is_password": True}, "stack": []}):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as directory:
                r = bridge.UserRecordingManager()
                r._session_dir = pathlib.Path(directory); r._session = {"status": "recording"}
                r._capture_screenshots = False; r._settle_seconds = 0
                window = {"hwnd": 123, "pid": 456, "title": "Synthetic", "process_name": "test"}
                r._event_window_for_step = lambda c: window
                with patch.object(bridge, "focused_window", return_value=window), patch.object(bridge, "uia_context_at_focus", return_value=context):
                    r._enrich_step({"action": {"type": "keyboard_input", "text": "synthetic-secret", "input_sequence": [{"kind": "text", "text": "synthetic-secret"}]},
                                    "epoch": bridge.time.time(), "focus": True, "event_context": window})
                r._write_report(pathlib.Path(directory))
                for filename in ("steps.json", "agent_steps.json", "report.html"):
                    self.assertNotIn("synthetic-secret", (pathlib.Path(directory) / filename).read_text(encoding="utf-8"))

    def test_throwing_password_property_stays_unknown(self):
        class UnknownControl:
            Name = "Synthetic"
            @property
            def IsPassword(self): raise RuntimeError("provider failed")
        target = bridge._uia_node_summary(UnknownControl())
        self.assertIsNone(target["is_password"])
        self.assertFalse(bridge.uia_text_capture_safe({"target": target, "stack": []}))

    def test_remote_host_uia_cannot_allow_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            r = bridge.UserRecordingManager(); r._session_dir = pathlib.Path(directory)
            r._session = {"status": "recording"}; r._capture_screenshots = False; r._settle_seconds = 0
            window = {"hwnd": 123, "pid": 456, "process_name": "mstsc.exe", "class_name": "TscShellContainerClass"}
            r._event_window_for_step = lambda c: window
            host_uia = {"target": {"control_type": "PaneControl", "is_password": False}, "stack": []}
            with patch.object(bridge, "focused_window", return_value=window), patch.object(bridge, "uia_context_at_focus", return_value=host_uia):
                r._enrich_step({"action": {"type": "keyboard_input", "text": "remote-secret"}, "epoch": bridge.time.time(), "focus": True, "event_context": window})
            self.assertNotIn("remote-secret", (pathlib.Path(directory) / "steps.json").read_text(encoding="utf-8"))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = bridge.AgentJournalManager()
        self.directory = self.journal.start("owner", output_dir=self.temp.name, goal="Example", capture_screenshots=False)["session_dir"]

    def read(self):
        return bridge.AgentJournalManager.read(self.directory)

    def test_fresh_process_can_read_checkpoint_and_uncertain_action(self):
        self.journal.checkpoint("owner", completed="Opened form", next_step="Inspect result")
        self.journal.begin_action("type", {"text": "secret", "controller_id": "owner"}, "owner")
        result = self.read()
        self.assertEqual(result["checkpoint"]["next_step"], "Inspect result")
        self.assertEqual(len(result["unfinished_actions"]), 1)
        self.assertTrue(result["requires_fresh_observation"])
        raw = (pathlib.Path(self.directory) / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret", raw); self.assertNotIn('"controller_id"', raw)

    def test_dispatch_receipt_does_not_verify_effect(self):
        event = self.journal.begin_action("click", {"state_id": "old-state"}, "owner")
        self.journal.end_action(event, result={"dispatch_status": "sent", "effect_verified": False})
        result = self.read()
        self.assertFalse(result["unfinished_actions"])
        self.assertFalse(result["last_action"]["receipt"]["result"]["effect_verified"])

    def test_different_controller_cannot_checkpoint_or_stop(self):
        with self.assertRaises(bridge.ControllerLeaseError): self.journal.checkpoint("other")
        with self.assertRaises(bridge.ControllerLeaseError): self.journal.stop("other")
        self.assertIsNone(self.journal.begin_action("click", {}, "other"))

    def test_truncated_tail_is_recoverable_but_middle_corruption_is_not(self):
        path = pathlib.Path(self.directory) / "events.jsonl"
        with path.open("a", encoding="utf-8") as stream: stream.write('{"event":')
        self.assertTrue(self.read()["incomplete_tail"])
        with path.open("a", encoding="utf-8") as stream: stream.write('\n{}\n')
        with self.assertRaises(ValueError): self.read()

    def test_stop_and_checkpoint_are_durable(self):
        self.journal.checkpoint("owner", next_step="Observe")
        self.journal.stop("owner")
        self.assertEqual(self.read()["session"]["status"], "stopped")

    def test_expired_controller_can_start_new_journal_without_stealing_live_one(self):
        lease = bridge.ControllerLeaseManager()
        first = lease.acquire(owner="first", lease_seconds=30)
        self.journal.stop("owner")
        self.journal.start(first["controller_id"], output_dir=self.temp.name)
        old_directory = self.journal.status()["session_dir"]
        with self.assertRaises(bridge.ControllerLeaseError): lease.acquire(owner="second")
        self.assertTrue(self.journal.status()["active"])
        with patch.object(bridge.time, "monotonic", return_value=bridge.time.monotonic() + 31):
            second = lease.acquire(owner="second")
            self.journal.reconcile_controller(second["controller_id"])
        self.assertFalse(self.journal.status()["active"])
        self.assertEqual(bridge.AgentJournalManager.read(old_directory)["session"]["status"], "interrupted")
        self.journal.start(second["controller_id"], output_dir=self.temp.name)
        self.assertIsNotNone(self.journal.begin_action("click", {}, second["controller_id"]))

    def test_opt_in_text_still_redacts_unknown_password_metadata(self):
        self.journal._capture_text = True
        with patch.object(bridge, "uia_context_at_focus", return_value={"target": {"control_type": "EditControl"}, "stack": []}), \
             patch.object(bridge, "focused_window", return_value=None):
            self.journal.begin_action("type", {"text": "synthetic-secret"}, "owner")
        self.assertNotIn("synthetic-secret", (pathlib.Path(self.directory) / "events.jsonl").read_text(encoding="utf-8"))

    def test_failed_pre_dispatch_write_prevents_native_input(self):
        with patch.object(bridge, "AGENT_JOURNAL", self.journal), \
             patch.object(bridge, "resolve_state_bound_action", return_value={"controller_id": "owner", "text": "x"}), \
             patch.object(bridge, "_require_remote_surface_state_binding"), \
             patch.object(bridge.CONTROLLER_LEASE, "validate_action", return_value={}), \
             patch.object(self.journal, "_append", side_effect=OSError("disk unavailable")), \
             patch.object(bridge, "_perform_action_unlocked") as dispatch:
            with self.assertRaises(OSError): bridge.perform_action("type", {"controller_id": "owner", "text": "x"})
            dispatch.assert_not_called()

    def test_observation_image_is_saved_without_live_controller(self):
        image = bridge.Image.new("RGB", (2, 2)); stream = io.BytesIO(); image.save(stream, format="PNG")
        self.journal._capture_screenshots = True
        self.journal.observe({"state_id": "historical", "controller_binding": {"controller_id": "owner"},
                              "raster": {"base64": base64.b64encode(stream.getvalue()).decode()}}, "owner")
        result = self.read(); state = result["last_observation"]["state"]
        self.assertNotIn("controller_binding", state)
        self.assertNotIn("base64", state["raster"])
        self.assertTrue((pathlib.Path(self.directory) / state["screenshot"]).is_file())


class EvidenceCoverageTests(unittest.TestCase):
    def test_counts_distinguish_images_targets_and_event_loss(self):
        recorder = bridge.UserRecordingManager()
        recorder._session = {"capture": {"screenshots": True}, "processing_errors": {}}
        recorder._processing_errors = {"correlation": 1, "enrichment": 2}
        recorder._dropped_events = 3
        recorder._dropped_semantic_steps = 4
        recorder._agent_steps = [
            {"screenshot": "a.jpg", "screenshot_relation": "after_input", "sampled_target": {"name": "candidate"},
             "capture_timing": {"selection": "early_after_input_before_next"}},
            {"screenshot": "b.jpg", "screenshot_relation": "before_input", "observed_after": {"focus_target": {"name": "later"}}},
            {"screenshot": "c.jpg", "screenshot_relation": "after_input", "capture_quality": "late",
             "capture_timing": {"source": "live_fallback", "overlaps_later_steps": [4]}},
            {"screenshot": "missing.jpg", "screenshot_relation": "after_input"},
            {"screenshot_error": "synthetic failure"},
        ]
        recorder._steps = [{}] * 5
        with tempfile.TemporaryDirectory() as folder:
            directory = pathlib.Path(folder)
            for name in ("a.jpg", "b.jpg", "c.jpg"): (directory / name).touch()
            result = recorder._recording_evidence_coverage(directory)
        images = result["screenshots"]
        self.assertEqual((images["present_files"], images["after_input"], images["before_input"]), (3, 2, 1))
        for key in ("missing_files", "no_reference", "early_after_input", "live_fallback", "overlaps_later_input", "late_or_window_changed", "capture_errors"):
            self.assertEqual(images[key], 1, key)
        self.assertEqual(result["uia"], {"with_target_candidate": 1, "without_target_candidate": 4, "with_sampled_focus": 1})
        self.assertEqual(result["processing_errors"], {"correlation": 1, "enrichment": 2})
        self.assertEqual((result["dropped_raw_events"], result["dropped_semantic_steps"]), (3, 4))

    def test_disabled_screenshots_are_explicit_not_an_acquisition_error(self):
        recorder = bridge.UserRecordingManager()
        recorder._session = {"capture": {"screenshots": False}}
        recorder._agent_steps = [{}]
        with tempfile.TemporaryDirectory() as folder:
            result = recorder._recording_evidence_coverage(pathlib.Path(folder))
        self.assertIs(result["screenshots_requested"], False)
        self.assertEqual(result["screenshots"]["no_reference"], 1)
        self.assertEqual(result["screenshots"]["capture_errors"], 0)
        self.assertIsNone(result["processing_errors"])

    def test_legacy_image_without_relation_does_not_become_after_input(self):
        recorder = bridge.UserRecordingManager()
        recorder._agent_steps = [{"screenshot": "legacy.jpg"}]
        with tempfile.TemporaryDirectory() as folder:
            directory = pathlib.Path(folder); (directory / "legacy.jpg").touch()
            result = recorder._recording_evidence_coverage(directory)
        self.assertEqual(result["screenshots"]["relation_unknown"], 1)
        self.assertEqual(result["screenshots"]["after_input"], 0)
        self.assertIsNone(result["screenshots_requested"])

    def test_failure_after_last_step_persists_in_both_zip_summaries(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(bridge, "WindowsUserInputHook") as constructor:
            constructor.return_value.start.return_value = True
            recorder = bridge.UserRecordingManager()
            recorder.start(output_dir=folder, capture_screenshots=False, capture_text=False)
            recorder._log_recording_error("enrichment", OSError("synthetic failure after last step"))
            result = recorder.stop()
            with bridge.zipfile.ZipFile(result["archive"]) as archive:
                session = json.loads(archive.read(next(n for n in archive.namelist() if n.endswith("/session.json"))))
                procedure = json.loads(archive.read(next(n for n in archive.namelist() if n.endswith("/procedure.json"))))
        self.assertEqual(session["processing_errors"]["enrichment"], 1)
        self.assertEqual(session["processing_errors"], procedure["evidence_coverage"]["processing_errors"])
        self.assertEqual(session["evidence_coverage"], procedure["evidence_coverage"])

    def test_processing_failure_is_counted_without_log_file(self):
        recorder = bridge.UserRecordingManager()
        recorder._log_recording_error("enrichment", OSError("synthetic"))
        self.assertEqual(recorder._processing_errors, {"correlation": 0, "enrichment": 1})


class FrameBufferTests(unittest.TestCase):
    @staticmethod
    def frame(start, end, pixels=b"frame"):
        return {"started_epoch": start, "completed_epoch": end, "jpeg": pixels}

    def test_settled_frame_precedes_later_click(self):
        buffer = bridge.RecordingFrameBuffer()
        for start in (10.05, 10.20, 10.40): buffer._store(self.frame(start, start + .03))
        buffer.note_input({"source": "mouse", "phase": "down", "epoch": 10.30})
        selected = buffer.select(10, .16)
        self.assertEqual(selected["started_epoch"], 10.20)
        self.assertEqual(selected["selection"], "settled_after_input")

    def test_fast_next_input_uses_early_frame_without_waiting_for_settle(self):
        buffer = bridge.RecordingFrameBuffer()
        buffer._store(self.frame(10.01, 10.03))
        buffer._store(self.frame(10.20, 10.22))
        buffer.note_input({"source": "keyboard", "phase": "down", "epoch": 10.05, "category": "text"})
        self.assertEqual(buffer.select(10, .16)["selection"], "early_after_input_before_next")

    def test_capture_crossing_next_input_is_rejected(self):
        buffer = bridge.RecordingFrameBuffer()
        buffer._store(self.frame(9.90, 9.95))
        buffer._store(self.frame(10.01, 10.10))
        buffer.note_input({"source": "mouse", "phase": "down", "epoch": 10.05})
        self.assertEqual(buffer.select(10, .16)["selection"], "before_input_no_clean_after")
        empty = bridge.RecordingFrameBuffer()
        self.assertIsNone(empty.select(10, .16))

    def test_mouse_move_and_modifier_do_not_invalidate_frame(self):
        buffer = bridge.RecordingFrameBuffer()
        buffer._store(self.frame(10.20, 10.23))
        buffer.note_input({"source": "mouse", "phase": "move", "epoch": 10.10})
        buffer.note_input({"source": "keyboard", "phase": "down", "category": "modifier", "epoch": 10.10})
        self.assertEqual(buffer.select(10, .16)["selection"], "settled_after_input")

    def test_memory_and_history_are_bounded_and_cleared(self):
        buffer = bridge.RecordingFrameBuffer()
        payload = b"x" * (1024 * 1024)
        for i in range(50): buffer._store(self.frame(10 + i * .01, 10 + i * .01, payload))
        self.assertLessEqual(buffer._bytes, 32 * 1024 * 1024)
        buffer._store(self.frame(20, 20))
        self.assertEqual(len(buffer._frames), 1)
        buffer.stop()
        self.assertFalse(buffer._frames)
        self.assertEqual(buffer._bytes, 0)

    def test_sampler_stops_even_when_shutdown_fails(self):
        for failure in ("queue_full", "hook_error", "worker_timeout", "missing_directory"):
            with self.subTest(failure=failure):
                recorder = bridge.UserRecordingManager()
                recorder._active = True
                recorder._session = {"session_id": "synthetic"}
                recorder._session_dir = pathlib.Path("synthetic")
                recorder._hook = Mock()
                recorder._queue = Mock()
                recorder._worker = Mock()
                recorder._worker.is_alive.return_value = False
                recorder._frame_buffer = Mock()
                if failure == "queue_full":
                    recorder._queue.put_nowait.side_effect = bridge.queue.Full
                    recorder._queue.put.side_effect = bridge.queue.Full
                elif failure == "hook_error":
                    recorder._hook.stop.side_effect = OSError("synthetic hook failure")
                elif failure == "worker_timeout":
                    recorder._worker.is_alive.return_value = True
                else:
                    recorder._session_dir = None
                with self.assertRaises((OSError, bridge.queue.Full)):
                    recorder.stop()
                recorder._frame_buffer.stop.assert_called_once()

    def test_hook_input_does_not_wait_for_frame_history_lock(self):
        import threading
        buffer = bridge.RecordingFrameBuffer()
        done = threading.Event()
        with buffer._lock:
            thread = threading.Thread(target=lambda: (buffer.note_input({"source": "mouse", "phase": "down", "epoch": 10}), done.set()))
            thread.start()
            completed = done.wait(.5)
        thread.join(timeout=1)
        self.assertTrue(completed)
        buffer._store(self.frame(10.01, 10.02))
        buffer.select(9.9, 0)
        self.assertEqual(list(buffer._inputs), [10.0])

    def test_stalled_sampler_is_terminated_before_receiver_cleanup(self):
        buffer = bridge.RecordingFrameBuffer()
        buffer._process = Mock(pid=123)
        buffer._process.is_alive.side_effect = [True, False, False]
        buffer._process_stop = Mock()
        buffer._thread = Mock()
        buffer._thread.is_alive.return_value = False
        output = buffer._output = Mock()
        buffer.stop()
        self.assertIs(buffer._process_stop.value, True)
        buffer._process.terminate.assert_called_once()
        buffer._process.kill.assert_not_called()
        output.close.assert_called_once()
        self.assertIsNone(buffer._output)

    def test_sampler_retries_transient_capture_failure(self):
        output = Mock()
        stop = Mock()
        stop.value = False
        image = bridge.Image.new("RGB", (2, 2), "red")
        with patch.object(bridge.multiprocessing, "parent_process", return_value=None), \
             patch.object(bridge, "capture_screenshot_with_info", side_effect=[OSError("transient capture failure"), (image, {})]) as capture, \
             patch.object(bridge, "list_monitors", return_value=[]), \
             patch.object(bridge.time, "sleep", side_effect=lambda _: setattr(stop, "value", capture.call_count == 2)) as wait:
            bridge._recording_frame_sampler(output, stop)
        self.assertEqual(capture.call_count, 2)
        output.send.assert_called_once()
        self.assertEqual(wait.call_count, 2)

    def test_frame_receiver_does_not_capture_pixels(self):
        buffer = bridge.RecordingFrameBuffer()
        buffer._output = Mock()
        def receive():
            buffer._stop.set()
            return self.frame(10, 10.01)
        buffer._output.poll.return_value = True
        buffer._output.recv.side_effect = receive
        with patch.object(bridge, "capture_screenshot_with_info") as capture:
            buffer._run()
            capture.assert_not_called()
        self.assertEqual(len(buffer._frames), 1)

    def test_process_start_failure_cleans_up_transport(self):
        buffer = bridge.RecordingFrameBuffer()
        context = Mock()
        context.Pipe.return_value = (Mock(), Mock())
        context.Process.return_value.pid = None
        context.Process.return_value.start.side_effect = OSError("synthetic spawn failure")
        with patch.object(bridge.multiprocessing, "get_context", return_value=context):
            with self.assertRaises(OSError): buffer.start()
        context.Pipe.return_value[0].close.assert_called_once()
        context.Pipe.return_value[1].close.assert_called_once()
        self.assertIsNone(buffer._output)

    def test_saved_frame_uses_historical_mapping_without_live_recapture(self):
        stream = io.BytesIO(); bridge.Image.new("RGB", (20, 10), "red").save(stream, format="JPEG")
        frame = dict(self.frame(10, 10.01, stream.getvalue()), selection="settled_after_input",
                     binding={"screen_mapping": {"x": -20, "y": 0, "width": 20, "height": 10}},
                     monitors=[{"x": -20, "y": 0, "width": 20, "height": 10}])
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "capture_screenshot_with_info") as capture:
            path = pathlib.Path(directory) / "selected.jpg"
            binding = bridge._capture_recording_frame(path, point={"x": -10, "y": 5}, target_rect=None, buffered_frame=frame)
            capture.assert_not_called()
            self.assertEqual(binding["screen_mapping"]["x"], -20)
            self.assertEqual(binding["frame_timing"]["started_epoch"], 10)
            self.assertTrue(path.is_file())


@unittest.skipUnless(bridge.os.name == "nt", "Windows physical hook backend")
class MouseHookTests(unittest.TestCase):
    def test_mouse_move_bypasses_window_queries_and_event_emission(self):
        hook = bridge.WindowsUserInputHook(Mock())
        user32 = ctypes.windll.user32
        with patch.object(user32, "SetWindowsHookExW", return_value=1), \
             patch.object(user32, "UnhookWindowsHookEx"), \
             patch.object(user32, "GetMessageW", return_value=0), \
             patch.object(user32, "CallNextHookEx", return_value=7) as next_hook, \
             patch.object(bridge, "_event_window_snapshot") as snapshot:
            hook._run()
            # Invalid l_param would fail if ignored movement were decoded.
            self.assertEqual(hook._mouse_proc(0, 0x0200, 0), 7)
            snapshot.assert_not_called()
            hook.on_event.assert_not_called()
            next_hook.assert_called_once()


@unittest.skipUnless(bridge.os.name == "nt", "Windows Unicode input backend")
class UnicodeTests(unittest.TestCase):
    def test_unicode_units_surrogates_and_newline(self):
        sent = []
        def capture(count, inputs, size):
            self.assertEqual(size, 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
            sent.extend((inputs[i].data.ki.wVk, inputs[i].data.ki.wScan, inputs[i].data.ki.dwFlags) for i in range(count))
            return count
        with patch.object(ctypes.windll.user32, "SendInput", Mock(side_effect=capture)), patch.object(bridge.pyautogui, "failSafeCheck"):
            bridge._send_unicode_text_windows("ä€😀\r\n", 0)
        self.assertEqual([scan for vk, scan, flags in sent if flags == 4], [0xE4, 0x20AC, 0xD83D, 0xDE00])
        self.assertEqual([vk for vk, scan, flags in sent if flags == 0], [13])

    def test_partial_send_requires_reobservation(self):
        with patch.object(ctypes.windll.user32, "SendInput", Mock(return_value=1)), patch.object(bridge.pyautogui, "failSafeCheck"):
            with self.assertRaises(bridge.ActionDispatchError) as error:
                bridge._send_unicode_text_windows("ä", 0)
        self.assertEqual(error.exception.retry, "reobserve")

    def test_failsafe_after_first_character_is_delivery_uncertain(self):
        with patch.object(ctypes.windll.user32, "SendInput", Mock(return_value=2)), \
             patch.object(bridge.pyautogui, "failSafeCheck", side_effect=[None, bridge.pyautogui.FailSafeException()]):
            with self.assertRaises(bridge.ActionDispatchError) as error:
                bridge._send_unicode_text_windows("ab", 0)
        self.assertEqual(error.exception.dispatch_status, "possibly_sent")
        self.assertEqual(error.exception.retry, "reobserve")


if __name__ == "__main__":
    unittest.main()
