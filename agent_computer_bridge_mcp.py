#!/usr/bin/env python3
"""Thin stdio MCP adapter for Agent Computer Bridge.

This file intentionally owns no desktop state and performs no native input.
It starts/uses the existing HTTP bridge and routes every mutation through that
canonical owner.  The adapter only simplifies agent discovery, lease lifecycle,
state binding, and cleanup.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal

try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ImageContent, TextContent
except ImportError as exc:  # pragma: no cover - exercised on hosts before optional install
    raise SystemExit(
        "Agent Computer Bridge MCP support is optional. Run install-mcp.cmd or "
        "`py -3 -m pip install -r requirements-mcp.txt`."
    ) from exc


ROOT = Path(__file__).resolve().parent
BRIDGE_SCRIPT = ROOT / "agent_computer_bridge.py"
HOST = os.environ.get("AGENT_COMPUTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_COMPUTER_PORT", "8765"))
TOKEN = os.environ.get("AGENT_COMPUTER_TOKEN") or None
IDLE_SHUTDOWN_SECONDS = max(60.0, float(os.environ.get("AGENT_COMPUTER_MCP_IDLE_SHUTDOWN", "300")))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


class BridgeToolError(ToolError):
    pass


class BridgeSession:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.controller_id: str | None = None
        self.started_by_mcp = False
        self.latest_state_id: str | None = None
        self.latest_state: dict[str, Any] | None = None
        self.last_activity = time.monotonic()
        self.closed = False
        self._idle_thread: threading.Thread | None = None

    @property
    def base(self) -> str:
        return f"http://{HOST}:{PORT}/v1"

    def _touch(self) -> None:
        self.last_activity = time.monotonic()

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        timeout: float = 30.0,
        query: dict[str, Any] | None = None,
        allow_error: bool = False,
    ) -> dict[str, Any]:
        self._touch()
        url = f"{self.base}/{path.lstrip('/')}"
        if query:
            encoded = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
            url += ("?" + encoded)
        headers = {"Accept": "application/json", "Connection": "close"}
        if TOKEN:
            headers["Authorization"] = f"Bearer {TOKEN}"
        payload = None
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                raise BridgeToolError(f"Bridge HTTP {exc.code}: {raw[:500]}") from exc
            result.setdefault("http_status", int(exc.code))
            if not allow_error:
                raise BridgeToolError(_json_text(result))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BridgeToolError(f"Agent Computer Bridge is unreachable at {url}: {exc}") from exc
        if not allow_error and result.get("ok") is False:
            raise BridgeToolError(_json_text(result))
        return result

    def _probe(self) -> dict[str, Any] | None:
        try:
            return self._request("status", timeout=0.7, allow_error=True)
        except BridgeToolError:
            return None

    def _ensure_idle_thread(self) -> None:
        if self._idle_thread is not None:
            return
        self._idle_thread = threading.Thread(target=self._idle_watch, name="acb-mcp-idle", daemon=True)
        self._idle_thread.start()

    def _idle_watch(self) -> None:
        while not self.closed:
            time.sleep(2.0)
            with self.lock:
                if self.closed or not self.started_by_mcp:
                    continue
                if time.monotonic() - self.last_activity < IDLE_SHUTDOWN_SECONDS:
                    continue
                self._cleanup_locked(shutdown="auto")

    def ensure_server(self) -> dict[str, Any]:
        with self.lock:
            status = self._probe()
            if status and status.get("ok"):
                self._ensure_idle_thread()
                return {"already_running": True, "status": status}
            cmd = [sys.executable, str(BRIDGE_SCRIPT), "start", "--host", HOST, "--port", str(PORT)]
            if TOKEN:
                cmd += ["--token", TOKEN]
            completed = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=15)
            if completed.returncode != 0:
                raise BridgeToolError(
                    "Bridge startup failed: " + (completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}")
                )
            try:
                started = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise BridgeToolError(f"Bridge startup returned invalid JSON: {completed.stdout[:500]}") from exc
            self.started_by_mcp = bool(started.get("started"))
            self._ensure_idle_thread()
            return started

    def ensure_controller(self) -> str:
        with self.lock:
            self.ensure_server()
            if self.controller_id:
                heartbeat = self._request(
                    "controller-heartbeat", method="POST", data={"controller_id": self.controller_id}, allow_error=True
                )
                if heartbeat.get("ok"):
                    return self.controller_id
                # A physical/user stop must never be silently bypassed by auto-reacquiring.
                raise BridgeToolError(_json_text(heartbeat))
            acquired = self._request(
                "controller-acquire",
                method="POST",
                data={"owner": f"mcp:{os.getpid()}", "lease_seconds": 300},
            )
            self.controller_id = str(acquired["controller_id"])
            return self.controller_id

    def compact_state(self, state: dict[str, Any]) -> dict[str, Any]:
        elements = []
        for element in (state.get("accessibility") or {}).get("elements", []):
            elements.append(
                {
                    "index": element.get("index"),
                    "control_type": element.get("control_type"),
                    "name": element.get("name"),
                    "automation_id": element.get("automation_id"),
                    "enabled": element.get("enabled"),
                    "offscreen": element.get("offscreen"),
                    "keyboard_focus": element.get("keyboard_focus"),
                    "rect": element.get("bounding_rect_windows_pixels") or element.get("bounding_rect"),
                }
            )
        raster = state.get("raster") or {}
        surface = state.get("surface") or {}
        return {
            "ok": state.get("ok", True),
            "state_id": state.get("state_id"),
            "expires_in_seconds": state.get("expires_in_seconds"),
            "target_window": state.get("target_window"),
            "surface": surface,
            "targeting": (
                "Use image_x/image_y from this raster only; local UIA is not actionable remote content."
                if surface.get("kind") == "remote_desktop"
                else "Prefer element_index; use image_x/image_y only when accessibility does not expose the target."
            ),
            "raster": {
                "present": bool(raster.get("base64")),
                "width": raster.get("width"),
                "height": raster.get("height"),
                "capture_provider": raster.get("capture_provider"),
                "screen_mapping": raster.get("image_region_windows_pixels"),
                "capture_binding": raster.get("capture_binding"),
            },
            "elements": elements,
        }

    def observe(
        self,
        *,
        process: str | None = None,
        title: str | None = None,
        pid: int | None = None,
        hwnd: int | None = None,
        include_image: bool = True,
        depth: int = 4,
        max_nodes: int = 200,
    ) -> dict[str, Any]:
        controller_id = self.ensure_controller()
        selector = {"process": process, "title": title, "pid": pid, "hwnd": hwnd}
        selector = {k: v for k, v in selector.items() if v not in (None, "")}
        scope_result = None
        if selector:
            scope_result = self._request("scope", method="POST", data={**selector, "activate": True, "controller_id": controller_id})
            if scope_result.get("activated") is False:
                raise BridgeToolError(
                    "Target window was selected but Windows did not grant foreground activation. "
                    "Do not raw-click around this gate. Re-run computer_observe once; if activation still fails, "
                    "ask the user to foreground the target. Scope result: " + _json_text(scope_result)
                )
        state = self._request(
            "app-state",
            query={
                "controller_id": controller_id,
                "include_image": 1 if include_image else 0,
                "depth": max(1, min(int(depth), 8)),
                "max_nodes": max(10, min(int(max_nodes), 500)),
            },
            timeout=45,
        )
        self.latest_state_id = str(state.get("state_id") or "") or None
        self.latest_state = state
        return {"state": state, "scope": scope_result}

    def state_id(self) -> str:
        if not self.latest_state_id:
            raise BridgeToolError("No current observation. Call computer_observe first.")
        return self.latest_state_id

    def mutate(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        controller_id = self.ensure_controller()
        remote_action = bool((self.latest_state or {}).get("surface", {}).get("remote")) and not bool(payload.get("dry_run"))
        payload = {**payload, "controller_id": controller_id, "state_id": self.state_id()}
        result = self._request(endpoint, method="POST", data=payload, allow_error=True, timeout=45)
        # The canonical Core consumes one-use remote states. Mirror the state
        # locally so the next call asks for observation instead of retrying a
        # stale RDP screenshot.
        if remote_action:
            self.latest_state_id = None
            self.latest_state = None
        if not result.get("ok"):
            retry = result.get("retry")
            sent = result.get("action_sent")
            advice = ""
            if retry == "reobserve" or sent is None:
                advice = " Re-observe before any retry."
            raise BridgeToolError(_json_text(result) + advice)
        if remote_action:
            result["next"] = "RDP content may have changed. Call computer_observe and verify the remote app/control before another action."
        return result

    def finish(self, shutdown: Literal["auto", "always", "never"] = "auto") -> dict[str, Any]:
        with self.lock:
            return self._cleanup_locked(shutdown=shutdown)

    def _cleanup_locked(self, shutdown: Literal["auto", "always", "never"] = "auto") -> dict[str, Any]:
        released = None
        stopped = None
        if self.controller_id:
            try:
                released = self._request(
                    "controller-release", method="POST", data={"controller_id": self.controller_id}, allow_error=True
                )
            except Exception:
                released = {"ok": False, "error": "release failed during cleanup"}
            self.controller_id = None
        self.latest_state_id = None
        self.latest_state = None
        should_stop = shutdown == "always" or (shutdown == "auto" and self.started_by_mcp)
        if should_stop:
            try:
                stopped = self._request("stop", method="POST", data={}, allow_error=True, timeout=3)
            except Exception:
                stopped = {"ok": False, "error": "server stop failed during cleanup"}
            self.started_by_mcp = False
        self._touch()
        return {"ok": True, "released": released, "server_stop": stopped, "shutdown": shutdown}

    def close_process(self) -> None:
        with self.lock:
            if self.closed:
                return
            try:
                self._cleanup_locked(shutdown="auto")
            except Exception:
                pass
            self.closed = True


SESSION = BridgeSession()
atexit.register(SESSION.close_process)

mcp = MCPServer(
    "Agent Computer Bridge",
    instructions=(
        "Native Windows computer use through the user's Agent Computer Bridge. "
        "Call computer_start once, computer_observe before actions, use element indices for local UI and raster pixels only for remote_desktop, "
        "re-observe after meaningful mutations, and call computer_finish when done. Never invent raw desktop coordinates."
    ),
)


@mcp.tool()
def computer_start() -> dict[str, Any]:
    """Start/attach to the bridge and acquire one exclusive controller lease. Use this before desktop actions."""
    controller = SESSION.ensure_controller()
    status = SESSION._request("status")
    monitors = SESSION._request("monitors")
    focused = SESSION._request("focused-window")
    return {
        "ready": True,
        "controller_id": controller,
        "version": status.get("version"),
        "build_id": status.get("build_id"),
        "monitors": monitors.get("monitors", []),
        "focused_window": focused.get("window"),
        "next": "Call computer_observe. Do not use raw screen coordinates from memory or another observation.",
    }


@mcp.tool()
def computer_windows(query: str | None = None) -> dict[str, Any]:
    """List visible top-level windows, optionally filtered by process/title text. Use this only when choosing a target for computer_observe."""
    SESSION.ensure_server()
    result = SESSION._request("windows")
    windows = list(result.get("windows", []))
    if query:
        needle = str(query).casefold()
        windows = [
            window for window in windows
            if needle in str(window.get("process_name") or "").casefold()
            or needle in str(window.get("title") or "").casefold()
        ]
    return {"windows": windows, "focused": SESSION._request("focused-window").get("window")}


@mcp.tool()
def computer_doctor(capture_probe: bool = False) -> dict[str, Any]:
    """Run non-mutating bridge diagnostics when computer_start/observe reports an environment problem. Screenshot probing is opt-in."""
    SESSION.ensure_server()
    return SESSION._request("doctor", query={"capture_probe": 1 if capture_probe else 0}, timeout=45)


@mcp.tool(structured_output=False)
def computer_observe(
    process: str | None = None,
    title: str | None = None,
    pid: int | None = None,
    hwnd: int | None = None,
    include_image: bool = True,
    depth: int = 4,
    max_nodes: int = 200,
) -> list[TextContent | ImageContent]:
    """Observe the focused window or select one target and observe it. Returns a fresh state plus optional image; always use this before acting."""
    observed = SESSION.observe(
        process=process,
        title=title,
        pid=pid,
        hwnd=hwnd,
        include_image=include_image,
        depth=depth,
        max_nodes=max_nodes,
    )
    state = observed["state"]
    compact = SESSION.compact_state(state)
    content: list[TextContent | ImageContent] = [TextContent(type="text", text=_json_text(compact))]
    raster = state.get("raster") or {}
    if include_image and raster.get("base64"):
        content.append(
            ImageContent(
                type="image",
                data=str(raster["base64"]),
                mime_type=str(raster.get("mime") or "image/png"),
            )
        )
    return content


@mcp.tool()
def computer_click(
    element_index: int | None = None,
    image_x: int | None = None,
    image_y: int | None = None,
    button: Literal["left", "middle", "right"] = "left",
    clicks: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Click only within the latest observation. Local UI: prefer element_index. Remote desktop: use image_x/image_y from that observation's raster."""
    has_element = element_index is not None
    has_raster = image_x is not None or image_y is not None
    if has_element == has_raster:
        raise BridgeToolError("Provide exactly one target: element_index OR both image_x and image_y.")
    payload: dict[str, Any] = {"button": button, "clicks": clicks, "dry_run": dry_run}
    if has_element:
        payload["element_index"] = int(element_index)
    else:
        if image_x is None or image_y is None:
            raise BridgeToolError("Both image_x and image_y are required for a raster click.")
        payload.update({"image_x": int(image_x), "image_y": int(image_y)})
    return SESSION.mutate("click", payload)


@mcp.tool()
def computer_move(image_x: int, image_y: int, dry_run: bool = False) -> dict[str, Any]:
    """Move the pointer to a pixel in the latest observation raster. This is state-bound; no raw virtual-desktop coordinates are exposed."""
    return SESSION.mutate("move", {"image_x": int(image_x), "image_y": int(image_y), "dry_run": dry_run})


@mcp.tool()
def computer_drag(
    image_x: int, image_y: int,
    duration: float = 0.5, button: Literal["left", "middle", "right"] = "left",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Drag from the current pointer to a pixel in the latest observation raster.
    Move the pointer to the observed start target first, verify the source,
    then call this tool. It uses the canonical scope and input dispatcher.
    """
    return SESSION.mutate("drag", {
        "image_x": int(image_x), "image_y": int(image_y),
        "duration": max(0.0, min(float(duration), 10.0)),
        "button": button, "dry_run": dry_run,
    })


@mcp.tool()
def computer_key(key: str, presses: int = 1, interval: float = 0.05) -> dict[str, Any]:
    """Press a named key (Enter, Tab, arrows, Backspace, etc.) in the latest
    observed window. Supports repetition with cancellation checkpoints.
    """
    if not isinstance(key, str) or not key.strip():
        raise BridgeToolError("key must be a non-empty key name")
    if isinstance(presses, bool) or not 1 <= presses <= 100:
        raise BridgeToolError("presses must be between 1 and 100")
    return SESSION.mutate("key", {
        "key": key, "presses": int(presses),
        "interval": max(0.0, min(float(interval), 10.0)),
    })


@mcp.tool()
def computer_scroll(
    amount: int,
    element_index: int | None = None,
    image_x: int | None = None,
    image_y: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scroll within the latest observation. Target an element when available or a raster pixel pair; then re-observe to verify effect."""
    payload: dict[str, Any] = {"amount": int(amount), "dry_run": dry_run}
    if element_index is not None:
        if image_x is not None or image_y is not None:
            raise BridgeToolError("Use element_index OR image_x/image_y, not both.")
        payload["element_index"] = int(element_index)
    elif image_x is not None or image_y is not None:
        if image_x is None or image_y is None:
            raise BridgeToolError("Both image_x and image_y are required.")
        payload.update({"image_x": int(image_x), "image_y": int(image_y)})
    return SESSION.mutate("scroll", payload)


@mcp.tool()
def computer_type(text: str, interval: float = 0.02) -> dict[str, Any]:
    """Type text into the window from the latest observation. The controller lease and state binding are applied automatically."""
    return SESSION.mutate("type", {"text": text, "interval": max(0.0, float(interval))})


@mcp.tool()
def computer_hotkey(keys: list[str]) -> dict[str, Any]:
    """Send a key chord to the window from the latest observation. A sent receipt is not proof of UI effect; re-observe afterward."""
    if not keys or not all(isinstance(key, str) and key for key in keys):
        raise BridgeToolError("keys must be a non-empty list of key names")
    return SESSION.mutate("hotkey", {"keys": list(keys)})


@mcp.tool()
def computer_journal_start(
    goal: str = "", prerequisites: str = "", success_criteria: str = "",
    output_dir: str | None = None, capture_text: bool = False, capture_screenshots: bool = True,
    auto_observe_after_action: bool = False,
) -> dict[str, Any]:
    """Optionally self-record agent actions using the canonical journal. With auto_observe_after_action=true, capture an observation after every completed dispatch. No business effect is assumed; text is masked by default."""
    controller = SESSION.ensure_controller()
    return SESSION._request("journal-start", method="POST", data={
        "controller_id": controller, "goal": goal, "prerequisites": prerequisites,
        "success_criteria": success_criteria, "output_dir": output_dir,
        "capture_text": capture_text, "capture_screenshots": capture_screenshots,
        "auto_observe_after_action": auto_observe_after_action,
    })


@mcp.tool()
def computer_checkpoint(completed: str = "", next_step: str = "", notes: str = "") -> dict[str, Any]:
    """Persist progress and the next step in the active journal. These are agent-supplied notes; observe the actual UI before claiming success."""
    controller = SESSION.ensure_controller()
    return SESSION._request("journal-checkpoint", method="POST", data={
        "controller_id": controller, "completed": completed, "next_step": next_step, "notes": notes,
    })


@mcp.tool()
def computer_journal_read(session_dir: str) -> dict[str, Any]:
    """Read persisted goal, checkpoint, latest observation and unfinished actions after context loss. Saved state IDs are expired evidence: acquire a controller and re-observe; never blindly replay actions."""
    SESSION.ensure_server()
    return SESSION._request("journal-read", method="POST", data={"session_dir": session_dir})


@mcp.tool()
def computer_journal_stop() -> dict[str, Any]:
    """Stop this agent's journal without releasing its controller. computer_finish also closes the journal."""
    if not SESSION.controller_id:
        raise BridgeToolError("No current controller session.")
    return SESSION._request("journal-stop", method="POST", data={"controller_id": SESSION.controller_id})


@mcp.tool()
def computer_journal_status() -> dict[str, Any]:
    """Find the active journal directory and event count."""
    SESSION.ensure_server()
    return SESSION._request("journal")


@mcp.tool()
def computer_finish(shutdown: Literal["auto", "always", "never"] = "auto") -> dict[str, Any]:
    """Release the controller lease. 'auto' also stops the bridge if this MCP process started it; use 'always' when the user wants no server left running."""
    return SESSION.finish(shutdown=shutdown)


if __name__ == "__main__":
    try:
        mcp.run()
    finally:
        SESSION.close_process()
