"""Contextual human demonstrations: remote pixels without invented UIA semantics."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_computer_bridge as bridge


class ContextualDemonstrationTests(unittest.TestCase):
    def setUp(self):
        self.recorder = bridge.UserRecordingManager()
        self.recorder._session = {
            "capture": {"screenshots": True},
            "intent": bridge.recording_intent(),
        }
        self.remote_window = {
            "hwnd": 523, "process_name": "mstsc.exe",
            "class_name": "TscShellContainerClass",
            "title": "Remote desktop window", "x": 2560, "y": 0,
            "width": 1920, "height": 1080,
        }
        local_capture_node = {
            "name": "Input Capture Window", "control_type": "PaneControl",
            "class_name": "IHWindowClass",
            "bounding_rect": {"x": 2560, "y": 0, "width": 1920, "height": 1080},
        }
        self.step = {
            "step": 1, "time": "2026-10-17T05:00:01+02:00",
            "action": {"type": "click", "button": "right",
                       "x": 3494, "y": 640},
            "window": self.remote_window,
            "uia": {"target": local_capture_node},
            "focus_after": {"target": local_capture_node},
            "target_confidence": "sampled_after_event",
            "screenshot": "screenshots/step_0001.jpg",
            "capture_timing": {
                "selection": "settled_after_input", "quality": "ok",
            },
            "capture_binding": {
                "screen_mapping": {
                    "x": 2560, "y": 0, "width": 1920, "height": 1080,
                },
                "frame_size": {"width": 1920, "height": 1080},
            },
        }

    def test_remote_client_shell_is_not_a_remote_target(self):
        projected = self.recorder._agent_step_projection(self.step)
        self.assertEqual(projected["surface"]["kind"], "remote_desktop")
        self.assertNotIn("target", projected)
        self.assertNotIn("sampled_target", projected)
        self.assertIsNone(projected["observed_after"]["focus_target"])
        self.assertEqual(projected["confidence"], "remote_raster_only")
        self.assertEqual(projected["image_at"], {"x": 934, "y": 640})
        self.assertIn("remote_controls_not_exposed_by_local_uia",
                      projected["uncertainties"])
        self.assertEqual(projected["screenshot_relation"], "after_input")

    def test_raster_mapping_respects_actual_capture_size_and_bounds(self):
        step = dict(self.step)
        step["capture_binding"] = {
            "screen_mapping": {"x": -300, "y": 100, "width": 1200, "height": 600},
            "frame_size": {"width": 600, "height": 300},
        }
        self.assertEqual(self.recorder._recorded_image_point(
            step, {"x": 300, "y": 400}), {"x": 300, "y": 150})
        self.assertIsNone(self.recorder._recorded_image_point(
            step, {"x": 1400, "y": 400}))
        self.assertIsNone(self.recorder._recorded_image_point(
            {"capture_binding": {}}, {"x": 1, "y": 2}))

    def test_conversation_driven_handoff_is_short_and_source_linked(self):
        early_step = {**self.step, "capture_timing": {
            "selection": "early_after_input_before_next", "quality": "ok",
        }}
        first = self.recorder._agent_step_projection(early_step)
        second = dict(first, step=2, screenshot="screenshots/step_0002.jpg",
                      screenshot_relation="before_input", capture_timing={
                          "selection": "before_input_no_clean_after", "quality": "before_input",
                      })
        third = {
            "step": 3, "action": "click", "button": "left",
            "surface": {"kind": "local_window"}, "screenshot": "step_0003.jpg",
            "screenshot_relation": "after_input",
            "target": {"name": "Settings", "role": "ButtonControl"},
            "app": {"process": "demo.exe"},
        }
        self.recorder._agent_steps = [first, second, third]
        handoff = self.recorder._build_agent_handoff(
            {"trace_integrity": {"assessment": "no_reported_processing_loss"}})
        self.assertEqual(handoff["context_source"], "user_conversation")
        self.assertEqual(handoff["source_of_truth"]["procedure"], "procedure.json")
        self.assertEqual(len(handoff["segments"]), 2)
        self.assertEqual((handoff["segments"][0]["first_step"],
                          handoff["segments"][0]["last_step"]), (1, 2))
        self.assertEqual(handoff["steps"][0]["image_at"], {"x": 934, "y": 640})
        self.assertEqual(handoff["steps"][0]["image_warning"], "after_input_but_not_settled")
        self.assertEqual(handoff["steps"][1]["capture_selection"], "before_input_no_clean_after")
        self.assertEqual(handoff["steps"][0]["target_evidence"],
                         "screenshot_only_not_local_uia")
        self.assertNotIn("local_target_candidate", handoff["steps"][0])
        self.assertEqual(handoff["steps"][1]["image_warning"],
                         "context_before_action_not_result")
        self.assertEqual(handoff["steps"][2]["local_target_candidate"]["name"],
                         "Settings")
        self.assertNotIn("missing_intent", json.dumps(handoff))
        self.assertLess(len(json.dumps(handoff)), 4500)

    def test_remote_enrichment_does_not_query_local_uia(self):
        import time
        self.recorder._capture_screenshots = False
        self.recorder._settle_seconds = 0.0
        when = time.time() - 1
        event = {
            "action": {"type": "click", "button": "right",
                       "x": 3494, "y": 640},
            "epoch": when,
            "point": {"x": 3494, "y": 640},
            "focus": False,
            "event_context": dict(self.remote_window),
        }
        with tempfile.TemporaryDirectory() as folder:
            self.recorder._session_dir = pathlib.Path(folder)
            with patch.object(self.recorder, "_event_window_for_step",
                              return_value=self.remote_window), \
                 patch.object(bridge, "window_at",
                              return_value=self.remote_window), \
                 patch.object(bridge, "focused_window",
                              return_value=self.remote_window), \
                 patch.object(bridge, "uia_context_at_point",
                              side_effect=AssertionError("local UIA must be skipped")) as at_point, \
                 patch.object(bridge, "uia_context_at_focus",
                              side_effect=AssertionError("local UIA must be skipped")) as at_focus:
                self.recorder._enrich_step(event)
                at_point.assert_not_called()
                at_focus.assert_not_called()
        self.assertEqual(len(self.recorder._agent_steps), 1)
        self.assertEqual(self.recorder._agent_steps[0]["confidence"],
                         "remote_raster_only")
        self.assertNotIn("sampled_target", self.recorder._agent_steps[0])

    def test_context_selection_preserves_actual_pre_click_menu_frame(self):
        import queue
        import time

        recorder = self.recorder
        recorder._capture_screenshots = True
        recorder._settle_seconds = 0
        recorder._semantic_queue = queue.Queue()
        image_bytes = b"fake-jpeg-for-testing"
        event_epoch = time.time() - 1
        before = {"jpeg": image_bytes, "started_epoch": event_epoch + 2.55,
                  "completed_epoch": event_epoch + 2.60,
                  "selection": "immediately_before_input"}
        post = {"jpeg": image_bytes, "started_epoch": event_epoch + 2.8,
                "completed_epoch": event_epoch + 2.9,
                "selection": "settled_after_input"}
        class Buffer:
            def select(self, epoch, settle): return post
            def select_before(self, epoch): return before
        recorder._frame_buffer = Buffer()
        context = dict(self.remote_window)
        with tempfile.TemporaryDirectory() as folder:
            recorder._session_dir = pathlib.Path(folder)
            recorder._emit_step(
                {"type": "click", "button": "right", "x": 3500, "y": 620},
                epoch=event_epoch, point={"x": 3500, "y": 620},
                event_context=context)
            recorder._emit_step(
                {"type": "click", "button": "left", "x": 3697, "y": 341},
                epoch=event_epoch+2.7, point={"x": 3697, "y": 341},
                event_context=context)
            self.assertNotIn("buffered_before_context_selection",
                             recorder._semantic_queue.queue[0])
            self.assertIs(recorder._semantic_queue.queue[1][
                "buffered_before_context_selection"], before)
            def render(path, *, buffered_frame=None, **kwargs):
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(buffered_frame["jpeg"])
                return {"frame_timing": {
                    "started_epoch": buffered_frame["started_epoch"],
                    "completed_epoch": buffered_frame["completed_epoch"],
                    "source": "buffered",
                    "selection": buffered_frame["selection"]},
                    "screen_mapping": {"x": 2560, "y": 0,
                                       "width": 1920, "height": 1080},
                    "frame_size": {"width": 1920, "height": 1080}}
            with patch.object(recorder, "_event_window_for_step",
                              return_value=context), \
                 patch.object(bridge, "window_at", return_value=context), \
                 patch.object(bridge, "focused_window", return_value=context), \
                 patch.object(bridge, "_capture_recording_frame",
                              side_effect=render):
                recorder._enrich_step(recorder._semantic_queue.get_nowait())
                recorder._enrich_step(recorder._semantic_queue.get_nowait())
            second = recorder._agent_steps[1]
            self.assertIsNone(recorder._agent_steps[0].get("pre_input_screenshot"))
            self.assertEqual(second["pre_input_screenshot"],
                             "screenshots/step_0002_before.jpg")
            self.assertEqual(second["pre_input_relation"], "before_input_not_result")
            self.assertTrue((pathlib.Path(folder) /
                             second["pre_input_screenshot"]).is_file())
            summary = recorder._build_agent_handoff({})
            self.assertEqual(summary["steps"][1]["before_image"],
                             second["pre_input_screenshot"])
            self.assertEqual(summary["steps"][1]["before_image_relation"],
                             "before_input_not_result")
            coverage = recorder._recording_evidence_coverage(pathlib.Path(folder))
            self.assertEqual(coverage["screenshots"]["pre_input_context_present"], 1)
            self.assertEqual(coverage["screenshots"]["pre_input_context_missing"], 0)

    def test_pre_input_selection_never_uses_future_or_stale_frame(self):
        buffer = bridge.RecordingFrameBuffer()
        base = 1000.0
        for started, ended in ((997, 997.2), (999.0, 999.1),
                               (999.95, 1000.07)):
            buffer._store({"jpeg": b"x", "started_epoch": started,
                           "completed_epoch": ended})
        previous = buffer.select_before(base)
        self.assertEqual(previous["completed_epoch"], 999.1)
        self.assertIsNone(buffer.select_before(base, max_age=0.3))

    def test_human_procedure_has_no_false_remote_semantic_target(self):
        projection = self.recorder._agent_step_projection(self.step)
        self.recorder._agent_steps = [projection]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "screenshots").mkdir()
            (root / "screenshots" / "step_0001.jpg").write_bytes(b"fake pixels")
            procedure = self.recorder._build_procedure(root)
        operation = procedure["operations"][0]
        self.assertEqual(operation["surface"]["kind"], "remote_desktop")
        self.assertNotIn("target", operation)
        self.assertEqual(operation["image_at"], {"x": 934, "y": 640})
        self.assertEqual(
            procedure["evidence_availability"]["signals"]["with_semantic_target_candidate"], 0)
        self.assertEqual(procedure["evidence_coverage"]["uia"]["with_target_candidate"], 0)


if __name__ == "__main__":
    unittest.main()
