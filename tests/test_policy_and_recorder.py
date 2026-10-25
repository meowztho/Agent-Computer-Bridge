"""Regression checks for controller admission, cancellation and recorder throughput.

All desktop input and clipboard effects are mocked; loopback HTTP exercises
the real Handler/Lease path, never a native Windows mutation.
"""
import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_computer_bridge as bridge


class ControllerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.old_lease = bridge.CONTROLLER_LEASE
        bridge.CONTROLLER_LEASE = bridge.ControllerLeaseManager()
        # The safety tests mock OS input. An unrelated RDP client may be
        # foreground on the test machine; it must not alter this fixture's
        # admission/cancellation assertions. RDP guards have dedicated tests.
        self.remote_detector = patch.object(
            bridge, "_remote_surface_windows_for_unbound_action",
            return_value=[])
        self.remote_detector.start()
        self.addCleanup(self.remote_detector.stop)
        self.lease_id = bridge.CONTROLLER_LEASE.acquire(owner="policy-test")["controller_id"]
        self.server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.AgentBridgeHandler)
        self.server.auth_token = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        bridge.CONTROLLER_LEASE = self.old_lease

    def post(self, route, data):
        raw = json.dumps(data).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}/v1/{route}",
            data=raw, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_clipboard_rejected_with_stopped_lease_without_mutation(self):
        bridge.CONTROLLER_LEASE.stop(self.lease_id, reason="test")
        with patch.object(bridge, "clipboard_set_text") as action:
            status, body = self.post("clipboard-set", {"text": "not actually copied"})
            self.assertEqual(status, 409)
            self.assertEqual(body["error_code"], "CONTROLLER_BUSY")
            action.assert_not_called()

    def test_clipboard_allowed_to_live_controller_only(self):
        with patch.object(bridge, "clipboard_set_text", return_value={"ok": True}) as action:
            status, _ = self.post("clipboard-set", {"text": "blocked"})
            self.assertEqual(status, 409)
            action.assert_not_called()
            status, _ = self.post("clipboard-set", {
                "text": "simulated", "controller_id": self.lease_id})
            self.assertEqual(status, 200)
            action.assert_called_once_with("simulated")

    def test_scope_and_viewport_reject_stopped_controller(self):
        bridge.CONTROLLER_LEASE.stop(self.lease_id, reason="test")
        cases = [
            ("scope", {"title": "no window selected"}),
            ("scope-clear", {}),
            ("viewport", {"x": 0, "y": 0, "width": 10, "height": 10}),
            ("viewport-clear", {}),
        ]
        with patch.object(bridge, "set_window_scope") as scope, \
             patch.object(bridge, "clear_window_scope") as scope_clear, \
             patch.object(bridge, "save_viewport") as viewport, \
             patch.object(bridge, "clear_viewport") as viewport_clear:
            for route, data in cases:
                with self.subTest(route=route):
                    status, body = self.post(route, data)
                    self.assertEqual(status, 409)
                    self.assertEqual(body["error_code"], "CONTROLLER_BUSY")
            for action in (scope, scope_clear, viewport, viewport_clear):
                action.assert_not_called()

    def test_failsafe_after_partial_native_dispatch_is_uncertain(self):
        def fail():
            raise bridge.pyautogui.FailSafeException("synthetic failsafe")
        def simulate(action, payload):
            bridge._dispatch_backend("first", lambda: None)
            bridge._dispatch_backend("second", fail)
            return {"ok": True}
        with patch.object(bridge, "_perform_action_unlocked", side_effect=simulate):
            with self.assertRaises(bridge.ActionDispatchError) as captured:
                bridge.perform_action("type", {
                    "text": "fixture", "controller_id": self.lease_id})
        self.assertIsNone(captured.exception.action_sent)
        self.assertEqual(captured.exception.dispatch_status, "possibly_sent")
        self.assertEqual(captured.exception.retry, "reobserve")

    def test_stop_between_backend_steps_prevents_second_input(self):
        entered = threading.Event()
        resume = threading.Event()
        dispatched = []
        outcome = {}

        def first_backend():
            dispatched.append("first")
            entered.set()
            self.assertTrue(resume.wait(3))

        def second_backend():
            dispatched.append("second")

        def fake_action(action, data):
            bridge._dispatch_backend("first", first_backend)
            bridge._dispatch_backend("second", second_backend)
            return {"ok": True}

        def run():
            try:
                outcome["result"] = bridge.perform_action(
                    "type", {"text": "fixture", "controller_id": self.lease_id})
            except Exception as exc:
                outcome["error"] = exc

        with patch.object(bridge, "_perform_action_unlocked", side_effect=fake_action):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                bridge.CONTROLLER_LEASE.stop_active_from_user()
            finally:
                resume.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(dispatched, ["first"])
        self.assertIsInstance(outcome.get("error"), bridge.ActionDispatchError)
        self.assertEqual(outcome["error"].dispatch_status, "possibly_sent")
        self.assertEqual(outcome["error"].retry, "reobserve")


class RecorderLatencyTests(unittest.TestCase):
    def test_emission_pins_post_input_frame_before_slow_enrichment(self):
        recorder = bridge.UserRecordingManager()
        recorder._capture_screenshots = True
        recorder._settle_seconds = 0.1
        recorder._semantic_queue = queue.Queue()
        frame = {"selection": "early_after_input_before_next", "jpeg": b"fixture",
                 "completed_epoch": 10.2}
        class Buffer:
            def select(self, epoch, settle):
                return frame
        recorder._frame_buffer = Buffer()
        recorder._emit_step({"type": "scroll", "delta": -120}, epoch=10.0)
        item = recorder._semantic_queue.get_nowait()
        self.assertIs(item["buffered_frame_at_emit"], frame)

    def test_incremental_recording_log_avoids_full_json_per_step(self):
        recorder = bridge.UserRecordingManager()
        recorder._capture_screenshots = False
        recorder._settle_seconds = 0.0
        recorder._session = {"capture": {"screenshots": False}, "intent": {}}
        recorder._last_progress_snapshot = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            recorder._session_dir = Path(directory)
            with patch.object(bridge, "focused_window", return_value=None), \
                 patch.object(bridge, "_atomic_write_json") as snapshots:
                for index in range(60):
                    recorder._enrich_step({
                        "action": {"type": "scroll", "delta": -120, "x": 10, "y": 10},
                        "epoch": time.time(), "point": None,
                        "focus": False, "event_context": None})
                lines = (Path(directory) / "steps.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), 60)
                self.assertEqual(len(recorder._steps), 60)
                # Initial + every 24 steps; no quadratic 3-array save per event.
                self.assertLessEqual(snapshots.call_count, 12)


class IntegrationExportTests(unittest.TestCase):
    def test_fast_synthetic_human_stream_finalizes_machine_trace(self):
        import zipfile

        class StubHook:
            def __init__(self, *args, **kwargs):
                pass
            def start(self):
                return True
            def stop(self):
                pass

        with tempfile.TemporaryDirectory() as root:
            recorder = bridge.UserRecordingManager()
            with patch.object(bridge, "WindowsUserInputHook", StubHook), \
                 patch.object(bridge, "window_at", return_value=None), \
                 patch.object(bridge, "focused_window", return_value=None):
                recorder.start(output_dir=root, capture_screenshots=False,
                               capture_text=False, goal="Synthetic teach flow",
                               success_criteria="All events exported")
                for i in range(120):
                    recorder._enqueue_event({
                        "source": "mouse", "phase": "wheel",
                        "delta": 120 if i % 2 else -120,
                        "horizontal": False, "x": 100, "y": 100,
                        "epoch": time.time(), "monotonic": time.monotonic(),
                        "event_context": None})
                result = recorder.stop()
            directory = Path(result["session_dir"])
            procedure = json.loads((directory / "procedure.json").read_text(encoding="utf-8"))
            self.assertEqual(result["step_count"], 120)
            self.assertEqual(len(procedure["operations"]), 120)
            self.assertFalse((directory / "steps.jsonl").exists())
            self.assertEqual(result["trace_integrity"]["assessment"], "no_reported_processing_loss")
            with zipfile.ZipFile(result["archive"]) as archive:
                names = set(Path(name).name for name in archive.namelist())
                self.assertTrue({"agent_handoff.json", "procedure.json",
                                 "agent_steps.json", "steps.json",
                                 "session.json"}.issubset(names))
                handoff = json.loads(next(
                    archive.read(name) for name in archive.namelist()
                    if name.endswith("/agent_handoff.json")))
                self.assertEqual(handoff["context_source"], "user_conversation")
                self.assertEqual(len(handoff["steps"]), 120)

    def test_pinned_image_survives_late_enrichment_selection(self):
        from PIL import Image
        import io
        raw = io.BytesIO()
        Image.new("RGB", (240, 160), (40, 42, 48)).save(raw, format="JPEG")
        event_epoch = time.time() - 1.0
        frame = {"selection": "early_after_input_before_next",
                 "jpeg": raw.getvalue(),
                 "started_epoch": event_epoch + 0.1,
                 "completed_epoch": event_epoch + 0.2,
                 "binding": {"screen_mapping": {"x": 0, "y": 0, "width": 240, "height": 160}},
                 "monitors": []}
        class Cache:
            def __init__(self):
                self.calls = 0
            def select(self, epoch, settle):
                self.calls += 1
                return frame if self.calls == 1 else None
        recorder = bridge.UserRecordingManager()
        recorder._capture_screenshots = True
        recorder._capture_scope = "virtual_desktop"
        recorder._settle_seconds = 0
        recorder._frame_buffer = Cache()
        recorder._semantic_queue = queue.Queue()
        recorder._session = {"capture": {"screenshots": True}, "intent": {}}
        with tempfile.TemporaryDirectory() as folder:
            recorder._session_dir = Path(folder)
            with patch.object(bridge, "focused_window", return_value=None), \
                 patch.object(bridge, "window_at", return_value=None):
                recorder._emit_step({"type": "scroll", "x": 50, "y": 50, "delta": -120},
                                    epoch=event_epoch, point={"x": 50, "y": 50})
                recorder._enrich_step(recorder._semantic_queue.get_nowait())
            step = recorder._steps[0]
            self.assertEqual(step["capture_timing"]["source"], "buffered")
            self.assertEqual(step["capture_timing"]["selection"], "early_after_input_before_next")
            self.assertEqual(step["capture_timing"]["capture_lag_ms"], 100)
            self.assertTrue((Path(folder) / step["screenshot"]).is_file())


if __name__ == "__main__":
    unittest.main()
