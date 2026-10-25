#!/usr/bin/env python3
"""
agent_computer_bridge.py

Single-file local computer-use bridge for AI coding agents.

Design:
    Core actions (single owner)
        -> CLI adapter
        -> local HTTP adapter

The script is intentionally dependency-light and harness-independent.  An agent
only needs shell access, local HTTP access, or both.

Install:
    pip install pyautogui pillow

Optional structured Windows UI observation:
    pip install uiautomation

Optional HWND-bound Windows.Graphics.Capture screenshots:
    pip install windows-capture

Best AI-agent bootstrap:
    # Preferred: connect agent_computer_bridge_mcp.py as a local stdio MCP server.
    # Fallback without MCP:
    python agent_computer_bridge.py agent-session
    # This starts the bridge if needed, acquires a controller lease, and returns a compact session contract.
    # Raw HTTP /v1/discovery remains available for low-level clients.

Quick CLI examples:
    python agent_computer_bridge.py screen
    python agent_computer_bridge.py screenshot
    python agent_computer_bridge.py screenshot --output agent_screen.png
    python agent_computer_bridge.py viewport set 0 0 1920 1080
    python agent_computer_bridge.py viewport show
    python agent_computer_bridge.py mouse
    python agent_computer_bridge.py move 1200 500
    python agent_computer_bridge.py click 1200 500
    python agent_computer_bridge.py click 0.5 0.5 --normalized
    python agent_computer_bridge.py double-click 1200 500
    python agent_computer_bridge.py drag 1200 500 --duration 0.5
    python agent_computer_bridge.py scroll -5
    python agent_computer_bridge.py type "Hello world"
    python agent_computer_bridge.py key enter
    python agent_computer_bridge.py hotkey ctrl l
    python agent_computer_bridge.py record

Start HTTP server:
    python agent_computer_bridge.py serve
    python agent_computer_bridge.py serve --port 8765
    python agent_computer_bridge.py serve --token my-secret-token

For backwards compatibility, running without a command starts the HTTP server.

HTTP examples:
    GET  /v1/discovery
    GET  /v1/windows
    GET  /v1/processes?query=mstsc
    GET  /v1/focused-window
    GET  /v1/scope
    GET  /v1/recording
    GET  /v1/app-state

    POST /v1/scope
         {"process": "mstsc.exe", "activate": true}

    POST /v1/scope-clear
         {}

    POST /v1/recording-start
         {"capture_screenshots": true, "settle_ms": 160, "capture_text": true}

    POST /v1/recording-stop
         {}
    GET  /screen
    GET  /mouse
    GET  /viewport
    GET  /screenshot
    GET  /screenshot?cursor=0

    POST /move
         {"x": 1000, "y": 500, "duration": 0.2}

    POST /click
         {"x": 1000, "y": 500, "button": "left", "clicks": 1}

    POST /click
         {"state_id": "<from /v1/app-state>", "element_index": 17}

    POST /click
         {"state_id": "<from /v1/app-state>", "image_x": 640, "image_y": 360}

    POST /click
         {"x": 0.5, "y": 0.5, "normalized": true}

    POST /drag
         {"x": 1400, "y": 700, "duration": 0.5, "button": "left"}

    POST /mouse-down
         {"button": "left"}

    POST /mouse-up
         {"button": "left"}

    POST /scroll
         {"amount": -5}

    POST /type
         {"text": "Hello world", "interval": 0.03}

    POST /key
         {"key": "enter", "presses": 1}

    POST /hotkey
         {"keys": ["ctrl", "l"]}

    POST /viewport
         {"x": 0, "y": 0, "width": 1920, "height": 1080}

    POST /viewport-clear
         {}

If --token is used for the server, send:
    Authorization: Bearer <token>

Screenshot cursor marker:
    PyAutoGUI screenshots normally do not include the OS mouse cursor.  This
    bridge overlays a deliberately larger high-contrast cursor marker with a
    soft glow on screenshots so vision models can locate the current pointer
    reliably.  It does NOT change the Windows system cursor itself.

Safety:
    - HTTP binds to 127.0.0.1 by default.
    - On Windows, an active server shows a click-through glowing border and live cursor halo.
    - A physically pressed Esc stops the active controller session first; with no controller lease it stops the server. Injected/agent-generated Esc is ignored.
    - PyAutoGUI FAILSAFE is enabled: moving the pointer to the top-left corner
      aborts PyAutoGUI operations.
    - Out-of-range pointer coordinates are rejected instead of silently clamped.
    - If a visible viewport is configured, pointer targets outside it are rejected.
"""

from __future__ import annotations

import argparse
import base64
import html
import hmac
import io
import importlib.util
import json
import os
import multiprocessing
import queue
import sys
import threading
import time
import subprocess
import tempfile
import traceback
import urllib.error
import zipfile
import urllib.request
import uuid
from collections import deque, OrderedDict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def _configure_windows_dpi_awareness_early() -> dict[str, Any]:
    """Request the strongest available Windows DPI mode before GUI/input libs load.

    Per-monitor-v2 awareness keeps Win32 window/monitor/cursor coordinates and
    screenshot pixels in the same physical-pixel model on mixed-DPI desktops.
    The call must happen before PyAutoGUI/Tk/Pillow create DPI-sensitive state.
    """
    state: dict[str, Any] = {
        "platform": "windows" if os.name == "nt" else os.name,
        "requested": "per_monitor_v2",
        "active": False,
        "mode": "not_applicable" if os.name != "nt" else "unknown",
        "set_method": None,
    }
    if os.name != "nt":
        return state
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        bits = ctypes.sizeof(ctypes.c_void_p) * 8
        pmv2_value = (-4) & ((1 << bits) - 1)
        pmv2 = ctypes.c_void_p(pmv2_value)

        setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if setter is not None:
            setter.argtypes = [ctypes.c_void_p]
            setter.restype = wintypes.BOOL
            try:
                if setter(pmv2):
                    state["set_method"] = "SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)"
            except Exception:
                pass

        # If PMv2 is unavailable (older Windows), request per-monitor awareness.
        if state["set_method"] is None:
            try:
                shcore = ctypes.windll.shcore
                shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
                shcore.SetProcessDpiAwareness.restype = ctypes.c_long
                if int(shcore.SetProcessDpiAwareness(2)) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
                    state["set_method"] = "SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)"
            except Exception:
                pass

        if state["set_method"] is None:
            try:
                user32.SetProcessDPIAware.restype = wintypes.BOOL
                if user32.SetProcessDPIAware():
                    state["set_method"] = "SetProcessDPIAware(system)"
            except Exception:
                pass

        # Query the effective thread context. This also handles ERROR_ACCESS_DENIED
        # from setters when a parent/host already established DPI awareness.
        try:
            get_ctx = getattr(user32, "GetThreadDpiAwarenessContext")
            get_ctx.restype = ctypes.c_void_p
            are_equal = getattr(user32, "AreDpiAwarenessContextsEqual")
            are_equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            are_equal.restype = wintypes.BOOL
            ctx = ctypes.c_void_p(get_ctx())
            if ctx.value and are_equal(ctx, pmv2):
                state["mode"] = "per_monitor_v2"
                state["active"] = True
            else:
                get_awareness = getattr(user32, "GetAwarenessFromDpiAwarenessContext")
                get_awareness.argtypes = [ctypes.c_void_p]
                get_awareness.restype = ctypes.c_int
                awareness = int(get_awareness(ctx)) if ctx.value else -1
                state["mode"] = {0: "unaware", 1: "system", 2: "per_monitor"}.get(awareness, "unknown")
                state["active"] = awareness == 2
        except Exception:
            # Setting PMv2/per-monitor successfully is still useful even if the
            # query APIs are missing on an older Windows build.
            if state["set_method"]:
                state["mode"] = "per_monitor_or_better_unqueried"
                state["active"] = "PER_MONITOR" in state["set_method"]
    except Exception as exc:
        state["error"] = str(exc)
    return state


WINDOWS_DPI_STATE = _configure_windows_dpi_awareness_early()

try:
    import pyautogui
    from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageFont, ImageChops
except (ImportError, OSError) as exc:
    print(
        "Computer bridge dependencies are unavailable.\n"
        "Install with: pip install pyautogui pillow\n"
        f"Details: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1)


VERSION = "2.42"
BUILD_ID = "2026-10-17-r47"
API_VERSION = "v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_SCREENSHOT = "agent_screen.png"
DEFAULT_OBSERVATION_SCREENSHOT = "agent_observation.png"
DEFAULT_VIEWPORT_FILE = ".agent_computer_viewport.json"
VALID_BUTTONS = {"left", "middle", "right"}
VALID_CAPTURE_PROVIDERS = {"auto", "desktop_composite", "windows_graphics_capture"}
DEFAULT_CAPTURE_PROVIDER = "auto"
OBSERVATION_STATE_TTL_SECONDS = 60.0
OBSERVATION_STATE_MAX = 12
REMOTE_DESKTOP_PROCESS_NAMES = {
    "mstsc.exe",          # classic Remote Desktop Connection
    "msrdc.exe",          # Remote Desktop / AVD desktop client
    "msrdcw.exe",         # Remote Desktop worker/client variant
    "rdclient.windows.exe",  # legacy Microsoft Store Remote Desktop app
}
REMOTE_DESKTOP_CLASS_HINTS = {
    "tscshellcontainerclass",
    "rail_window",
}
CONTROLLER_LEASE_DEFAULT_SECONDS = 120.0
CONTROLLER_LEASE_MIN_SECONDS = 15.0
CONTROLLER_LEASE_MAX_SECONDS = 900.0
FAILSAFE_MARGIN = 2
MAX_BODY_BYTES = 1 * 1024 * 1024
MAX_REJECT_DRAIN_BYTES = 8 * 1024 * 1024
COMPUTER_ACTION_LOCK = threading.RLock()
ACTION_DISPATCH_CONTEXT = threading.local()  # Cancellation state; not an input owner
BRIDGE_INJECTING_KEYBOARD = threading.Event()
RUNTIME_VISUALS_ACTIVE = False
PHYSICAL_ESCAPE_STOP_ACTIVE = False
PHYSICAL_ESCAPE_STOP_SOURCE = "unavailable"
ACTION_HISTORY: deque[dict[str, Any]] = deque(maxlen=200)
ACTION_HISTORY_LOCK = threading.Lock()
ACTION_SEQUENCE = 0
WINDOW_SCOPE_LOCK = threading.RLock()
ACTIVE_WINDOW_SCOPE: dict[str, Any] | None = None


class WindowScopeAmbiguous(ValueError):
    def __init__(self, message: str, candidates: list[dict[str, Any]]):
        super().__init__(message)
        self.candidates = candidates


class PointerSafetyConflict(RuntimeError):
    """A valid request could not be executed without violating a safety boundary."""


class CaptureProviderUnavailable(RuntimeError):
    """An explicitly requested screenshot provider is unavailable or failed."""


class ObservationStateError(RuntimeError):
    """A state-bound action cannot safely reuse the referenced observation."""

    def __init__(self, message: str, *, code: str = "STALE_STATE", retry: str = "reobserve"):
        super().__init__(message)
        self.code = str(code)
        self.retry = str(retry)
        self.action_sent = False


class ControllerLeaseError(RuntimeError):
    """A mutating action is blocked by the controller-session owner."""

    def __init__(self, message: str, *, code: str = "CONTROLLER_BUSY", retry: str = "never", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.retry = str(retry)
        self.action_sent = False
        self.dispatch_status = "not_started"
        self.details = dict(details or {})


class ActionDispatchError(RuntimeError):
    """The backend raised after dispatch began and delivery is not fully knowable."""

    def __init__(self, message: str, *, code: str = "DISPATCH_UNCERTAIN", action_sent: bool | None = None, dispatch_status: str = "possibly_sent", retry: str = "reobserve", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.action_sent = action_sent
        self.dispatch_status = str(dispatch_status)
        self.retry = str(retry)
        self.details = dict(details or {})


def keyboard_injection_sentinel_path(pid: int | None = None) -> Path:
    return Path(tempfile.gettempdir()) / f"agent_computer_bridge_inject_{int(pid or os.getpid())}.flag"


def _begin_keyboard_injection() -> None:
    BRIDGE_INJECTING_KEYBOARD.set()
    try:
        keyboard_injection_sentinel_path().write_text("1", encoding="ascii")
    except OSError:
        pass


def _end_keyboard_injection() -> None:
    try:
        keyboard_injection_sentinel_path().unlink(missing_ok=True)
    except OSError:
        pass
    BRIDGE_INJECTING_KEYBOARD.clear()

# Important safety behavior: moving the mouse to the top-left corner aborts
# PyAutoGUI operations.
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


# ---------------------------------------------------------------------------
# Core: canonical computer-use behavior.
# CLI and HTTP adapters below must call these functions rather than reimplement
# coordinate conversion, validation, or action behavior.
# ---------------------------------------------------------------------------


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def screen_size() -> tuple[int, int]:
    """Primary monitor size, retained for backwards-compatible /screen fields."""
    with COMPUTER_ACTION_LOCK:
        size = pyautogui.size()
    return int(size.width), int(size.height)


def virtual_desktop_bounds() -> dict[str, int]:
    """Canonical Windows virtual-desktop rectangle, including negative origins.

    Example with a 1920x1080 monitor to the left of a 2560x1440 primary:
        {"x": -1920, "y": 0, "width": 4480, "height": 1440}

    All Windows cursor observation and multi-monitor screenshot mapping uses
    this geometry.  On non-Windows platforms it falls back to the primary
    screen rectangle.
    """
    if os.name == "nt":
        import ctypes
        user32 = ctypes.windll.user32
        SM_XVIRTUALSCREEN = 76
        SM_YVIRTUALSCREEN = 77
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
        y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if width > 0 and height > 0:
            return {"x": x, "y": y, "width": width, "height": height}
    width, height = screen_size()
    return {"x": 0, "y": 0, "width": width, "height": height}


def _point_in_rect(x: int, y: int, rect: dict[str, int]) -> bool:
    return (
        int(rect["x"]) <= x < int(rect["x"]) + int(rect["width"])
        and int(rect["y"]) <= y < int(rect["y"]) + int(rect["height"])
    )


def validate_button(button: Any) -> str:
    value = str(button or "left").lower()
    if value not in VALID_BUTTONS:
        raise ValueError("button must be left, middle, or right")
    return value


def _known_failsafe_points(width: int, height: int) -> set[tuple[int, int]]:
    """Return PyAutoGUI failsafe points, with four-corner fallback."""
    fallback = {
        (0, 0),
        (0, max(0, height - 1)),
        (max(0, width - 1), 0),
        (max(0, width - 1), max(0, height - 1)),
    }
    raw_points = getattr(pyautogui, "FAILSAFE_POINTS", None)
    if not raw_points:
        return fallback

    points: set[tuple[int, int]] = set()
    for point in raw_points:
        try:
            px, py = int(point[0]), int(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= px < width and 0 <= py < height:
            points.add((px, py))
    return points or fallback


def _nudge_from_failsafe_corner(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """Move only targets near a configured failsafe point slightly toward screen center."""
    for fx, fy in _known_failsafe_points(width, height):
        if abs(x - fx) <= FAILSAFE_MARGIN and abs(y - fy) <= FAILSAFE_MARGIN:
            dx = FAILSAFE_MARGIN if fx <= (width - 1) / 2 else -FAILSAFE_MARGIN
            dy = FAILSAFE_MARGIN if fy <= (height - 1) / 2 else -FAILSAFE_MARGIN
            return (
                clamp(fx + dx, 0, max(0, width - 1)),
                clamp(fy + dy, 0, max(0, height - 1)),
            )
    return x, y


def _nudge_from_monitor_corner(x: int, y: int) -> tuple[int, int]:
    """Avoid exact/near monitor corners while staying inside that monitor.

    PyAutoGUI's documented failsafe points are historically primary-monitor
    based, but real Windows/RDP setups can still behave badly at secondary
    monitor corners. Treat every physical monitor corner as a pointer safety
    corner and nudge inward *within that same monitor*.
    """
    try:
        monitors = list_monitors()
    except Exception:
        monitors = []

    for monitor in monitors:
        # Only the monitor that actually contains the target may influence its
        # safety nudge. Adjacent monitor corners can be within the same small
        # margin (e.g. primary right edge 2559 next to secondary x=2560).
        if not _point_in_rect(int(x), int(y), monitor):
            continue
        left = int(monitor["x"])
        top = int(monitor["y"])
        right = left + int(monitor["width"]) - 1
        bottom = top + int(monitor["height"]) - 1
        corners = (
            (left, top, +FAILSAFE_MARGIN, +FAILSAFE_MARGIN),
            (right, top, -FAILSAFE_MARGIN, +FAILSAFE_MARGIN),
            (left, bottom, +FAILSAFE_MARGIN, -FAILSAFE_MARGIN),
            (right, bottom, -FAILSAFE_MARGIN, -FAILSAFE_MARGIN),
        )
        for fx, fy, dx, dy in corners:
            if abs(x - fx) <= FAILSAFE_MARGIN and abs(y - fy) <= FAILSAFE_MARGIN:
                return (
                    clamp(fx + dx, left, right),
                    clamp(fy + dy, top, bottom),
                )

    # Preserve the explicit PyAutoGUI primary-monitor fallback if monitor
    # enumeration is unavailable or did not match the point.
    primary_w, primary_h = screen_size()
    if 0 <= x < primary_w and 0 <= y < primary_h:
        return _nudge_from_failsafe_corner(x, y, primary_w, primary_h)
    return x, y


def _point_on_real_monitor(x: int, y: int) -> bool:
    """Return True only when the point maps to an actual monitor rectangle.

    The Windows virtual desktop is a bounding rectangle and can contain empty
    gaps for staggered monitor layouts. Sending a cursor into such a gap lets
    Windows clamp it unpredictably, so pointer input rejects those coordinates.
    """
    try:
        monitors = list_monitors()
    except Exception:
        monitors = []
    if not monitors:
        return _point_in_rect(int(x), int(y), virtual_desktop_bounds())
    return any(_point_in_rect(int(x), int(y), monitor) for monitor in monitors)


def _resolve_pointer_coordinate_space(value: Any, *, scope_active: bool) -> str:
    """Resolve pointer coordinate-space selection without hidden scope conversion.

    Safety contract:
      - without a window scope, omitted/auto means virtual_desktop;
      - with an active window scope, coordinate_space is REQUIRED for every
        pointer request that supplies x/y. The caller must explicitly choose
        either window_scope (window-relative) or virtual_desktop (absolute
        Windows coordinates).

    This avoids silently reinterpreting absolute coordinates as window-local
    coordinates merely because a scope became active.
    """
    if value in (None, "", "auto"):
        if scope_active:
            raise ValueError(
                "coordinate_space is required while a process/window scope is active; "
                "use 'window_scope' for window-relative x/y or 'virtual_desktop' for absolute Windows x/y"
            )
        return "virtual_desktop"
    space = str(value).strip().lower()
    # Accept only canonical API values.  Older internal aliases such as
    # "screen", "desktop", "absolute" or "scope" are intentionally rejected:
    # silently rewriting an unknown coordinate-space token makes pointer safety
    # diagnostics ambiguous and can hide an agent-side contract bug.
    if space not in {"window_scope", "virtual_desktop"}:
        raise ValueError(
            f"unknown coordinate_space: {space!r}; expected 'window_scope' or 'virtual_desktop'"
        )
    if space == "window_scope" and not scope_active:
        raise ValueError("coordinate_space='window_scope' requires an active process/window scope")
    return space


def resolve_xy(x: Any, y: Any, *, normalized: bool = False, coordinate_space: Any = None) -> tuple[int, int]:
    """Resolve a pointer target in the active coordinate space.

    No scope:
      - absolute x/y are Windows virtual-desktop coordinates and may be negative
        when monitors are placed left/above the primary display.
      - normalized x/y map across the complete virtual desktop.

    Active window/process scope:
      - coordinate_space is mandatory for every request that supplies x/y.
      - coordinate_space="window_scope" interprets x/y relative to the scoped
        window's outer rectangle.
      - coordinate_space="virtual_desktop" accepts absolute Windows coordinates,
        but the resolved target must still lie inside the active scope.
      - normalized coordinates follow the explicitly selected coordinate space.

    The final target is validated against virtual desktop, optional RDP
    viewport, and optional window scope before input is sent.
    """
    desktop = virtual_desktop_bounds()
    primary_w, primary_h = screen_size()
    scope = active_scope_window()
    requested_space = _resolve_pointer_coordinate_space(coordinate_space, scope_active=scope is not None)

    try:
        x_value = float(x)
        y_value = float(y)
    except (TypeError, ValueError) as exc:
        raise ValueError("x and y must be numeric") from exc

    if requested_space == "virtual_desktop":
        coordinate_space = "virtual_desktop"
        if normalized:
            if not 0.0 <= x_value <= 1.0 or not 0.0 <= y_value <= 1.0:
                raise ValueError("normalized x and y must be between 0.0 and 1.0")
            screen_x = int(round(desktop["x"] + x_value * max(0, desktop["width"] - 1)))
            screen_y = int(round(desktop["y"] + y_value * max(0, desktop["height"] - 1)))
        else:
            screen_x = int(round(x_value))
            screen_y = int(round(y_value))
        if not _point_in_rect(screen_x, screen_y, desktop):
            x2 = desktop["x"] + desktop["width"] - 1
            y2 = desktop["y"] + desktop["height"] - 1
            raise ValueError(
                f"pointer target outside {coordinate_space} bounds: "
                f"x must be {desktop['x']}..{x2}, y must be {desktop['y']}..{y2}"
            )
        if scope is not None and not _point_in_rect(screen_x, screen_y, scope):
            raise ValueError(
                f"virtual_desktop target ({screen_x}, {screen_y}) is outside active window scope; "
                f"scope bounds are x={int(scope['x'])}..{int(scope['x']) + int(scope['width']) - 1}, "
                f"y={int(scope['y'])}..{int(scope['y']) + int(scope['height']) - 1}"
            )
    else:
        if scope.get("minimized"):
            raise ValueError("active window scope is minimized; activate/restore it before pointer input")
        width, height = int(scope["width"]), int(scope["height"])
        if width <= 0 or height <= 0:
            raise ValueError("active window scope has no usable geometry")
        if normalized:
            if not 0.0 <= x_value <= 1.0 or not 0.0 <= y_value <= 1.0:
                raise ValueError("normalized x and y must be between 0.0 and 1.0")
            x_value *= max(0, width - 1)
            y_value *= max(0, height - 1)
        local_x = int(round(x_value))
        local_y = int(round(y_value))
        if not (0 <= local_x < width and 0 <= local_y < height):
            raise ValueError(
                f"pointer target outside window_scope bounds: x must be 0..{max(0, width - 1)}, "
                f"y must be 0..{max(0, height - 1)}"
            )
        screen_x = int(scope["x"]) + local_x
        screen_y = int(scope["y"]) + local_y
        if not _point_in_rect(screen_x, screen_y, desktop):
            raise ValueError(
                f"resolved pointer target ({screen_x}, {screen_y}) is outside the virtual desktop; "
                "move/resize the scoped window into a visible monitor area"
            )

    if not _point_on_real_monitor(screen_x, screen_y):
        raise ValueError(
            f"pointer target ({screen_x}, {screen_y}) lies in an empty gap of the virtual desktop, not on a real monitor"
        )

    # Avoid risky corners on *any* monitor, not only the primary. The nudge is
    # always inward inside the monitor that owns the corner.
    screen_x, screen_y = _nudge_from_monitor_corner(screen_x, screen_y)
    # Safety is re-validated AFTER every automatic correction. A safety nudge
    # is never allowed to move the pointer outside viewport/scope boundaries.
    require_pointer_allowed(screen_x, screen_y)
    return screen_x, screen_y


def resolve_screen_xy(x: Any, y: Any, *, normalized: bool = False) -> tuple[int, int]:
    """Resolve a read-only pixel in virtual-desktop coordinates."""
    desktop = virtual_desktop_bounds()
    try:
        x_value = float(x)
        y_value = float(y)
    except (TypeError, ValueError) as exc:
        raise ValueError("x and y must be numeric") from exc
    if normalized:
        if not 0.0 <= x_value <= 1.0 or not 0.0 <= y_value <= 1.0:
            raise ValueError("normalized x and y must be between 0.0 and 1.0")
        px = int(round(desktop["x"] + x_value * max(0, desktop["width"] - 1)))
        py = int(round(desktop["y"] + y_value * max(0, desktop["height"] - 1)))
    else:
        px = int(round(x_value))
        py = int(round(y_value))
    if not _point_in_rect(px, py, desktop):
        raise ValueError("screen coordinate is outside the virtual desktop")
    return px, py


def current_mouse() -> tuple[int, int]:
    """Canonical pointer observation.

    On Windows use GetCursorPos directly so the bridge sees the same cursor
    position as the runtime overlay, including RDP-hosted windows and monitors
    with negative virtual-desktop coordinates. PyAutoGUI remains the fallback.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            point = wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception:
            pass
    with COMPUTER_ACTION_LOCK:
        pos = pyautogui.position()
    return int(pos.x), int(pos.y)

def capture_raw_desktop() -> Image.Image:
    """Capture the complete virtual desktop when the platform supports it."""
    with COMPUTER_ACTION_LOCK:
        if os.name == "nt":
            # Pillow's Windows backend supports the complete virtual desktop,
            # including monitors with negative X/Y origins.
            return ImageGrab.grab(all_screens=True).convert("RGB")
        return pyautogui.screenshot().convert("RGB")



def normalize_capture_provider(value: Any = None) -> str:
    raw = DEFAULT_CAPTURE_PROVIDER if value is None else str(value).strip().lower()
    if raw not in VALID_CAPTURE_PROVIDERS:
        expected = ", ".join(sorted(VALID_CAPTURE_PROVIDERS))
        raise ValueError(f"unknown capture_provider: {raw!r}; expected one of: {expected}")
    return raw


def windows_graphics_capture_backend_status() -> dict[str, Any]:
    """Describe the optional HWND-bound Windows.Graphics.Capture provider."""
    status: dict[str, Any] = {
        "provider": "windows_graphics_capture",
        "platform": "windows" if os.name == "nt" else os.name,
        "available": False,
        "package": "windows-capture",
        "import_name": "windows_capture",
        "install_hint": "pip install windows-capture",
        "target_binding": "HWND",
        "occlusion_independent": True,
    }
    if os.name != "nt":
        status["reason"] = "Windows.Graphics.Capture is only available on Windows"
        return status
    try:
        spec = importlib.util.find_spec("windows_capture")
    except Exception as exc:
        status["reason"] = f"backend probe failed: {exc}"
        return status
    if spec is None:
        status["reason"] = "optional windows_capture package is not installed"
        return status
    status["available"] = True
    status["reason"] = None
    return status


def capture_provider_status() -> dict[str, Any]:
    wgc = windows_graphics_capture_backend_status()
    return {
        "default": DEFAULT_CAPTURE_PROVIDER,
        "providers": {
            "desktop_composite": {
                "available": True,
                "scope_binding": "desktop crop",
                "occlusion_independent": False,
            },
            "windows_graphics_capture": wgc,
        },
        "auto_policy": (
            "With an active window scope, prefer windows_graphics_capture when available; "
            "otherwise fall back to desktop_composite. Without a scope, use desktop_composite."
        ),
    }


def _capture_window_windows_graphics(hwnd: int, *, timeout: float = 2.0) -> Image.Image:
    """Capture one HWND-bound frame using the optional windows-capture package.

    The provider is observation-only. It never sends input and therefore does not
    become a second pointer/keyboard authority.
    """
    backend = windows_graphics_capture_backend_status()
    if not backend.get("available"):
        raise CaptureProviderUnavailable(str(backend.get("reason") or "Windows.Graphics.Capture backend unavailable"))
    if timeout <= 0 or timeout > 10:
        raise ValueError("capture timeout must be between 0 and 10 seconds")

    try:
        from windows_capture import WindowsCapture  # type: ignore
    except Exception as exc:
        raise CaptureProviderUnavailable(f"cannot import windows_capture: {exc}") from exc

    done = threading.Event()
    result: dict[str, Any] = {}
    errors: list[str] = []

    try:
        capture = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            secondary_window=False,
            window_hwnd=int(hwnd),
        )
    except Exception as exc:
        raise CaptureProviderUnavailable(f"cannot create HWND capture for {hwnd}: {exc}") from exc

    @capture.event
    def on_frame_arrived(frame: Any, capture_control: Any) -> None:
        try:
            width = int(frame.width)
            height = int(frame.height)
            if width <= 0 or height <= 0:
                raise RuntimeError(f"invalid WGC frame size {width}x{height}")
            # windows-capture exposes tightly packed BGRA numpy frame data.
            raw = frame.frame_buffer.tobytes(order="C")
            expected = width * height * 4
            if len(raw) != expected:
                raise RuntimeError(f"unexpected WGC frame buffer length {len(raw)}; expected {expected}")
            result["image"] = Image.frombytes("RGBA", (width, height), raw, "raw", "BGRA").convert("RGB")
        except Exception as exc:
            errors.append(str(exc))
        finally:
            done.set()
            try:
                capture_control.stop()
            except Exception:
                pass

    @capture.event
    def on_closed() -> None:
        if not done.is_set():
            errors.append("target window capture closed before a frame arrived")
            done.set()

    control = None
    try:
        control = capture.start_free_threaded()
        if not done.wait(timeout):
            raise CaptureProviderUnavailable(f"timed out after {timeout:.1f}s waiting for HWND {hwnd} capture frame")
    except CaptureProviderUnavailable:
        raise
    except Exception as exc:
        raise CaptureProviderUnavailable(f"Windows.Graphics.Capture failed for HWND {hwnd}: {exc}") from exc
    finally:
        if control is not None:
            try:
                if not control.is_finished():
                    control.stop()
            except Exception:
                pass

    if errors:
        raise CaptureProviderUnavailable(errors[0])
    image = result.get("image")
    if not isinstance(image, Image.Image):
        raise CaptureProviderUnavailable("Windows.Graphics.Capture produced no image")
    return image


def _capture_scoped_raw(
    scope: dict[str, Any],
    *,
    capture_provider: Any = DEFAULT_CAPTURE_PROVIDER,
) -> tuple[Image.Image, dict[str, Any]]:
    """Capture a scoped window through the selected provider.

    `auto` prefers HWND-bound WGC and falls back to the legacy desktop-composite
    crop. Explicit WGC is fail-closed and never silently substitutes desktop
    pixels.
    """
    requested = normalize_capture_provider(capture_provider)
    wgc_status = windows_graphics_capture_backend_status()
    prefer_wgc = requested == "windows_graphics_capture" or (
        requested == "auto" and bool(wgc_status.get("available"))
    )
    fallback_reason: str | None = None

    if prefer_wgc:
        try:
            image = _capture_window_windows_graphics(int(scope["hwnd"]))
            return image, {
                "requested_provider": requested,
                "provider": "windows_graphics_capture",
                "capture_mode": "hwnd_windows_graphics_capture",
                "target_hwnd": int(scope["hwnd"]),
                "target_title": scope.get("title"),
                "target_process": scope.get("process_name"),
                "occlusion_independent": True,
                "fallback_used": False,
                "frame_size": {"width": image.size[0], "height": image.size[1]},
                "screen_mapping": {
                    "x": int(scope["x"]), "y": int(scope["y"]),
                    "width": int(scope["width"]), "height": int(scope["height"]),
                    "method": "proportional_to_scoped_window_rect",
                },
            }
        except Exception as exc:
            if requested == "windows_graphics_capture":
                if isinstance(exc, CaptureProviderUnavailable):
                    raise
                raise CaptureProviderUnavailable(str(exc)) from exc
            fallback_reason = str(exc)

    # Canonical legacy fallback: visible desktop composition cropped to scope.
    desktop = capture_raw_desktop()
    image = crop_to_screen_region(
        desktop, scope["x"], scope["y"], scope["width"], scope["height"]
    )
    return image, {
        "requested_provider": requested,
        "provider": "desktop_composite",
        "capture_mode": "desktop_composite_crop",
        "target_hwnd": int(scope["hwnd"]),
        "target_title": scope.get("title"),
        "target_process": scope.get("process_name"),
        "occlusion_independent": False,
        "fallback_used": bool(fallback_reason),
        "fallback_reason": fallback_reason,
        "frame_size": {"width": image.size[0], "height": image.size[1]},
        "screen_mapping": {
            "x": int(scope["x"]), "y": int(scope["y"]),
            "width": int(scope["width"]), "height": int(scope["height"]),
            "method": "exact_desktop_crop",
        },
        "occluders_detected": _scope_occluders(scope),
    }


def pixel_color(x: Any, y: Any, *, normalized: bool = False) -> dict[str, Any]:
    """Return RGB at a virtual-desktop coordinate without visual overlays."""
    px, py = resolve_screen_xy(x, y, normalized=normalized)
    if os.name == "nt":
        # Use the same full-virtual-desktop capture/mapping as screenshots so
        # negative monitor coordinates follow one canonical path.
        image = capture_raw_desktop()
        ix, iy = map_screen_xy_to_image(px, py, image_width=image.size[0], image_height=image.size[1])
        value = image.getpixel((ix, iy))
        return {"ok": True, "x": px, "y": py, "rgb": list(value)}
    pixel_fn = getattr(pyautogui, "pixel", None)
    if callable(pixel_fn):
        with COMPUTER_ACTION_LOCK:
            value = pixel_fn(px, py)
        return {"ok": True, "x": px, "y": py, "rgb": [int(value[0]), int(value[1]), int(value[2])]}
    image = capture_raw_desktop()
    ix, iy = map_screen_xy_to_image(px, py, image_width=image.size[0], image_height=image.size[1])
    value = image.getpixel((ix, iy))
    return {"ok": True, "x": px, "y": py, "rgb": list(value)}


def viewport_config_path() -> Path:
    return Path.home() / DEFAULT_VIEWPORT_FILE


def load_viewport() -> dict[str, int] | None:
    path = viewport_config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        x = int(data["x"])
        y = int(data["y"])
        width = int(data["width"])
        height = int(data["height"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if width <= 0 or height <= 0:
        return None
    desktop = virtual_desktop_bounds()
    left, top = desktop["x"], desktop["y"]
    right = left + desktop["width"]
    bottom = top + desktop["height"]
    x = clamp(x, left, right - 1)
    y = clamp(y, top, bottom - 1)
    width = clamp(width, 1, max(1, right - x))
    height = clamp(height, 1, max(1, bottom - y))
    return {"x": x, "y": y, "width": width, "height": height}


def save_viewport(x: Any, y: Any, width: Any, height: Any) -> dict[str, int]:
    desktop = virtual_desktop_bounds()
    left, top = desktop["x"], desktop["y"]
    right = left + desktop["width"]
    bottom = top + desktop["height"]
    try:
        x = int(round(float(x)))
        y = int(round(float(y)))
        width = int(round(float(width)))
        height = int(round(float(height)))
    except (TypeError, ValueError) as exc:
        raise ValueError("viewport x/y/width/height must be numeric") from exc
    if width <= 0 or height <= 0:
        raise ValueError("viewport width and height must be positive")
    x = clamp(x, left, right - 1)
    y = clamp(y, top, bottom - 1)
    width = clamp(width, 1, max(1, right - x))
    height = clamp(height, 1, max(1, bottom - y))
    data = {"x": x, "y": y, "width": width, "height": height}
    path = viewport_config_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def clear_viewport() -> bool:
    path = viewport_config_path()
    if path.exists():
        path.unlink()
        return True
    return False



# ---------------------------------------------------------------------------
# Agent observability helpers: window awareness, monitors, clipboard, history.
# These are read-oriented capabilities around the canonical input core above.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _history_request_view(data: dict[str, Any]) -> dict[str, Any]:
    """Keep history useful without retaining typed/clipboard secrets verbatim."""
    safe = dict(data)
    if "text" in safe:
        value = safe.get("text")
        safe["text"] = f"<{len(value) if isinstance(value, str) else '?'} chars>"
    return safe


def _record_history(action: str, request: dict[str, Any], *, result: dict[str, Any] | None = None, error: str | None = None) -> int:
    global ACTION_SEQUENCE
    with ACTION_HISTORY_LOCK:
        ACTION_SEQUENCE += 1
        item = {
            "id": ACTION_SEQUENCE,
            "time": _now_iso(),
            "action": action,
            "request": _history_request_view(request),
            "ok": error is None,
        }
        if result is not None:
            item["result"] = result
        if error is not None:
            item["error"] = error
        ACTION_HISTORY.append(item)
        return ACTION_SEQUENCE


def get_action_history(limit: Any = 20) -> list[dict[str, Any]]:
    try:
        count = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("history limit must be an integer") from exc
    if not 1 <= count <= 100:
        raise ValueError("history limit must be between 1 and 100")
    with ACTION_HISTORY_LOCK:
        return list(ACTION_HISTORY)[-count:]


def current_action_state() -> dict[str, Any]:
    x, y = current_mouse()
    scope_status = window_scope_status()
    mouse: dict[str, Any] = {"x": x, "y": y}
    if scope_status.get("active") and not scope_status.get("stale"):
        window = scope_status["window"]
        local_x = x - int(window["x"])
        local_y = y - int(window["y"])
        inside = 0 <= local_x < int(window["width"]) and 0 <= local_y < int(window["height"])
        mouse["scope_relative"] = {"x": local_x, "y": local_y, "inside": inside}
    mouse["source"] = "win32.GetCursorPos" if os.name == "nt" else "pyautogui.position"
    return {
        "mouse": mouse,
        "virtual_desktop": virtual_desktop_bounds(),
        "viewport": load_viewport(),
        "window_scope": scope_status,
        "failsafe_margin_px": FAILSAFE_MARGIN,
    }


def pointer_target_preview(x: Any, y: Any, *, normalized: bool = False, coordinate_space: Any = None) -> dict[str, Any]:
    desktop = virtual_desktop_bounds()
    primary_w, primary_h = screen_size()
    scope = active_scope_window()
    requested_space = _resolve_pointer_coordinate_space(coordinate_space, scope_active=scope is not None)
    try:
        raw_x = float(x)
        raw_y = float(y)
    except (TypeError, ValueError) as exc:
        raise ValueError("x and y must be numeric") from exc

    requested = {"x": raw_x, "y": raw_y, "normalized": bool(normalized), "coordinate_space": requested_space}
    if requested_space == "virtual_desktop":
        coordinate_space = "virtual_desktop"
        if normalized:
            if not 0.0 <= raw_x <= 1.0 or not 0.0 <= raw_y <= 1.0:
                raise ValueError("normalized x and y must be between 0.0 and 1.0")
            screen_x = int(round(desktop["x"] + raw_x * max(0, desktop["width"] - 1)))
            screen_y = int(round(desktop["y"] + raw_y * max(0, desktop["height"] - 1)))
        else:
            screen_x = int(round(raw_x))
            screen_y = int(round(raw_y))
        if not _point_in_rect(screen_x, screen_y, desktop):
            raise ValueError("pointer target is outside the virtual desktop")
        if scope is not None and not _point_in_rect(screen_x, screen_y, scope):
            raise ValueError(
                f"virtual_desktop target ({screen_x}, {screen_y}) is outside active window scope; "
                f"scope bounds are x={int(scope['x'])}..{int(scope['x']) + int(scope['width']) - 1}, "
                f"y={int(scope['y'])}..{int(scope['y']) + int(scope['height']) - 1}"
            )
        if scope is not None:
            local_x = screen_x - int(scope["x"])
            local_y = screen_y - int(scope["y"])
        else:
            local_x = screen_x - desktop["x"]
            local_y = screen_y - desktop["y"]
    else:
        if scope.get("minimized"):
            raise ValueError("active window scope is minimized; activate/restore it before pointer input")
        width, height = int(scope["width"]), int(scope["height"])
        coordinate_space = "window_scope"
        if normalized:
            if not 0.0 <= raw_x <= 1.0 or not 0.0 <= raw_y <= 1.0:
                raise ValueError("normalized x and y must be between 0.0 and 1.0")
            local_x = int(round(raw_x * max(0, width - 1)))
            local_y = int(round(raw_y * max(0, height - 1)))
        else:
            local_x = int(round(raw_x))
            local_y = int(round(raw_y))
        if not (0 <= local_x < width and 0 <= local_y < height):
            raise ValueError(
                f"pointer target outside window_scope bounds: x must be 0..{max(0, width - 1)}, "
                f"y must be 0..{max(0, height - 1)}"
            )
        screen_x = int(scope["x"]) + local_x
        screen_y = int(scope["y"]) + local_y
        if not _point_in_rect(screen_x, screen_y, desktop):
            raise ValueError("resolved pointer target is outside the virtual desktop")

    if not _point_on_real_monitor(screen_x, screen_y):
        raise ValueError(
            f"pointer target ({screen_x}, {screen_y}) lies in an empty gap of the virtual desktop, not on a real monitor"
        )
    resolved_x, resolved_y = _nudge_from_monitor_corner(screen_x, screen_y)
    require_pointer_allowed(resolved_x, resolved_y)
    return {
        "coordinate_space": coordinate_space,
        "requested": requested,
        "local_before_safety": {"x": local_x, "y": local_y},
        "screen_before_safety": {"x": screen_x, "y": screen_y},
        "resolved": {"x": resolved_x, "y": resolved_y},
        "failsafe_nudged": (resolved_x, resolved_y) != (screen_x, screen_y),
        "viewport_allowed": True,
        "scope_allowed": True,
        "virtual_desktop": desktop,
    }

def preview_action(action: str, data: dict[str, Any]) -> dict[str, Any]:
    normalized = bool(data.get("normalized", False))
    coordinate_space = data.get("coordinate_space")
    preview: dict[str, Any] = {"ok": True, "dry_run": True, "action": action}
    if action in {"move", "drag"}:
        preview["target"] = pointer_target_preview(data.get("x"), data.get("y"), normalized=normalized, coordinate_space=coordinate_space)
        if action == "drag":
            cx, cy = current_mouse()
            require_pointer_allowed(cx, cy)
            preview["start"] = {"x": cx, "y": cy}
            preview["button"] = validate_button(data.get("button", "left"))
    elif action == "click":
        button = validate_button(data.get("button", "left"))
        clicks = int(data.get("clicks", 1))
        if not 1 <= clicks <= 5:
            raise ValueError("clicks must be between 1 and 5")
        x, y = data.get("x"), data.get("y")
        if x is None and y is None:
            cx, cy = current_mouse()
            require_pointer_allowed(cx, cy)
            preview["target"] = {"requested": "current_mouse", "resolved": {"x": cx, "y": cy}, "failsafe_nudged": False, "viewport_allowed": True}
        elif x is None or y is None:
            raise ValueError("provide both x and y, or neither")
        else:
            preview["target"] = pointer_target_preview(x, y, normalized=normalized, coordinate_space=coordinate_space)
        if active_scope_window() is not None:
            resolved = preview["target"]["resolved"]
            preview["target_binding"] = validate_scoped_click_target(
                int(resolved["x"]), int(resolved["y"]), target_area=data.get("target_area", "client")
            )
        preview.update({"button": button, "clicks": clicks, "target_area": data.get("target_area", "client")})
    elif action == "scroll":
        amount = int(data.get("amount", 0))
        if amount == 0:
            raise ValueError("amount must not be 0")
        x, y = data.get("x"), data.get("y")
        if x is None and y is None:
            cx, cy = current_mouse()
            require_pointer_allowed(cx, cy)
            preview["target"] = {"requested": "current_mouse", "resolved": {"x": cx, "y": cy}, "failsafe_nudged": False, "viewport_allowed": True}
        elif x is None or y is None:
            raise ValueError("provide both x and y, or neither")
        else:
            preview["target"] = pointer_target_preview(x, y, normalized=normalized, coordinate_space=coordinate_space)
        preview["amount"] = amount
    else:
        raise ValueError("dry_run is supported for move, click, drag, and scroll")
    preview["state"] = current_action_state()
    return preview


def _process_image_path(pid: int) -> str | None:
    """Best-effort Win32 process image path without adding psutil."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value[: int(size.value)]
    finally:
        kernel32.CloseHandle(handle)


def _nullable_hwnd_int(value: Any) -> int:
    """Normalize Win32 HWND return values where NULL is exposed as None."""
    return int(value) if value else 0


def _window_class_name(hwnd: int) -> str:
    """Best-effort Win32 class name for a top-level window."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(512)
        length = int(user32.GetClassNameW(int(hwnd), buf, len(buf)))
        return buf.value[:length] if length > 0 else ""
    except Exception:
        return ""


def _is_remote_desktop_window(window: dict[str, Any] | None) -> bool:
    """Classify known Windows RDP client surfaces without claiming remote UIA access."""
    if not window:
        return False
    process_name = str(window.get("process_name") or "").strip().casefold()
    class_name = str(window.get("class_name") or "").strip().casefold()
    if process_name in REMOTE_DESKTOP_PROCESS_NAMES:
        return True
    return any(hint and hint in class_name for hint in REMOTE_DESKTOP_CLASS_HINTS)


def _surface_context(window: dict[str, Any] | None) -> dict[str, Any]:
    """Describe the local surface represented by a top-level window.

    Remote Desktop is a nested desktop rendered inside a local client window.
    Local UIA therefore describes the client/chrome, not controls inside the
    remote session.  This context is observation metadata only; input still
    flows through the canonical action dispatcher.
    """
    if not window:
        return {"kind": "unknown", "remote": False}
    remote = _is_remote_desktop_window(window)
    client_rect = None
    try:
        if window.get("hwnd"):
            client_rect = _client_rect_screen(int(window["hwnd"]))
    except Exception:
        client_rect = None
    cx = int(window.get("x") or 0) + max(0, int(window.get("width") or 0) // 2)
    cy = int(window.get("y") or 0) + max(0, int(window.get("height") or 0) // 2)
    monitor = None
    try:
        monitor = monitor_for_point(cx, cy)
    except Exception:
        monitor = None
    monitor_view = None
    if monitor:
        monitor_view = {k: monitor.get(k) for k in ("index", "device", "x", "y", "width", "height", "primary")}
    return {
        "kind": "remote_desktop" if remote else "local_window",
        "remote": remote,
        "nested_coordinate_space": remote,
        "process_name": window.get("process_name"),
        "class_name": window.get("class_name"),
        "hwnd": window.get("hwnd"),
        "client_rect_windows_pixels": client_rect,
        "monitor": monitor_view,
        "semantic_targeting": "local_uia_not_remote_content" if remote else "uia_available_when_exposed",
        "recommended_targeting": "state_id + image_x/image_y" if remote else "state_id + element_index; raster fallback",
    }


def _window_info_from_hwnd(hwnd: int) -> dict[str, Any] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    if not user32.IsWindow(hwnd):
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(hwnd, title_buf, len(title_buf))
    title = title_buf.value
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process_path = _process_image_path(int(pid.value))
    foreground_raw = user32.GetForegroundWindow()
    foreground_hwnd = _nullable_hwnd_int(foreground_raw)
    return {
        "hwnd": int(hwnd),
        "title": title,
        "pid": int(pid.value),
        "process_name": Path(process_path).name if process_path else None,
        "process_path": process_path,
        "class_name": _window_class_name(int(hwnd)),
        "x": int(rect.left),
        "y": int(rect.top),
        "width": max(0, int(rect.right - rect.left)),
        "height": max(0, int(rect.bottom - rect.top)),
        # GetForegroundWindow may transiently return NULL while focus changes
        # between top-level windows. Treat that as "no focused window" instead
        # of crashing the recorder with int(None).
        "focused": bool(foreground_hwnd) and int(hwnd) == foreground_hwnd,
        "minimized": bool(user32.IsIconic(hwnd)),
        "visible": bool(user32.IsWindowVisible(hwnd)),
    }


def _client_rect_screen(hwnd: int) -> dict[str, int] | None:
    """Return a top-level window's client rectangle in Windows virtual-desktop pixels."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    if not user32.IsWindow(int(hwnd)):
        return None
    rect = wintypes.RECT()
    if not user32.GetClientRect(int(hwnd), ctypes.byref(rect)):
        return None
    top_left = wintypes.POINT(int(rect.left), int(rect.top))
    bottom_right = wintypes.POINT(int(rect.right), int(rect.bottom))
    if not user32.ClientToScreen(int(hwnd), ctypes.byref(top_left)):
        return None
    if not user32.ClientToScreen(int(hwnd), ctypes.byref(bottom_right)):
        return None
    width = max(0, int(bottom_right.x - top_left.x))
    height = max(0, int(bottom_right.y - top_left.y))
    if width <= 0 or height <= 0:
        return None
    return {"x": int(top_left.x), "y": int(top_left.y), "width": width, "height": height}


def _rect_intersection(a: dict[str, Any], b: dict[str, Any]) -> dict[str, int] | None:
    left = max(int(a["x"]), int(b["x"]))
    top = max(int(a["y"]), int(b["y"]))
    right = min(int(a["x"]) + int(a["width"]), int(b["x"]) + int(b["width"]))
    bottom = min(int(a["y"]) + int(a["height"]), int(b["y"]) + int(b["height"]))
    if right <= left or bottom <= top:
        return None
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def _scope_occluders(scope: dict[str, Any], region: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Best-effort list of visible top-level windows above the scoped window that overlap it.

    EnumWindows/list_windows are Z-order ordered. This is diagnostic evidence for
    desktop-composite screenshots; the point hit-test remains the authoritative
    click gate.
    """
    if os.name != "nt":
        return []
    target = region or scope
    result: list[dict[str, Any]] = []
    for candidate in list_windows():
        if int(candidate.get("hwnd", -1)) == int(scope["hwnd"]):
            break
        overlap = _rect_intersection(candidate, target)
        if overlap is None:
            continue
        result.append({
            "hwnd": int(candidate["hwnd"]),
            "title": candidate.get("title"),
            "pid": candidate.get("pid"),
            "process_name": candidate.get("process_name"),
            "overlap": overlap,
        })
    return result


def scope_capture_binding(scope: dict[str, Any] | None = None, *, requested_provider: Any = DEFAULT_CAPTURE_PROVIDER) -> dict[str, Any]:
    """Describe what a scoped screenshot actually represents.

    This function describes target geometry and provider policy before a capture.
    The actual provider used is returned by screenshot_as_json/capture metadata.
    """
    if scope is None:
        try:
            scope = active_scope_window()
        except Exception as exc:
            return {"active": True, "available": False, "error": str(exc)}
    if scope is None:
        return {"active": False}
    client = _client_rect_screen(int(scope["hwnd"]))
    focused = focused_window()
    focused_match = bool(focused and int(focused.get("hwnd", -1)) == int(scope["hwnd"]))
    occlusion_region = client or {k: int(scope[k]) for k in ("x", "y", "width", "height")}
    occluders = _scope_occluders(scope, occlusion_region)
    requested = normalize_capture_provider(requested_provider)
    wgc = windows_graphics_capture_backend_status()
    preferred = (
        "windows_graphics_capture"
        if requested == "auto" and wgc.get("available")
        else ("desktop_composite" if requested == "auto" else requested)
    )
    return {
        "active": True,
        "target_hwnd": int(scope["hwnd"]),
        "target_title": scope.get("title"),
        "target_process": scope.get("process_name"),
        "requested_provider": requested,
        "preferred_provider": preferred,
        "capture_mode": "not_captured_yet",
        "occlusion_independent": None,
        "scope_focused": focused_match,
        "client_rect": client,
        "desktop_composite_occluders_detected": occluders,
        "provider_status": capture_provider_status(),
        "warning": (
            "Provider is selected at capture time. auto prefers HWND-bound Windows.Graphics.Capture when available; "
            "desktop_composite remains the transparent fallback and can include occluding windows."
        ),
    }


def validate_scoped_click_target(x: int, y: int, *, target_area: Any = "client") -> dict[str, Any]:
    """Fail closed unless a scoped click is visibly bound to the selected window.

    Coordinate scope alone is not target binding. A valid scoped click must also:
      - target the selected top-level window at the instant of the click,
      - target the client area by default (titlebar/window chrome requires target_area=window),
      - and have that scoped window as the effective foreground application.
    """
    scope = active_scope_window()
    if scope is None:
        return {"active": False, "allowed": True}

    area = str(target_area or "client").strip().lower()
    if area not in {"client", "window"}:
        raise ValueError("unknown target_area: %r; expected 'client' or 'window'" % area)

    require_pointer_allowed(int(x), int(y))
    front = focused_window()
    if front is None or int(front.get("hwnd", -1)) != int(scope["hwnd"]):
        raise ValueError(
            "scoped click blocked because the scoped window is not the effective foreground window; "
            "activate the scope first or re-select it with activate=true"
        )

    under = window_at(int(x), int(y))
    if under is None or int(under.get("hwnd", -1)) != int(scope["hwnd"]):
        if under is None:
            detail = "no visible top-level window owns the target point"
        else:
            detail = (
                f"the visible window at the target is hwnd={under.get('hwnd')} "
                f"title={under.get('title')!r} process={under.get('process_name')!r}"
            )
        raise ValueError(
            "scoped click blocked because the target point is not visibly owned by the scoped window; " + detail
        )

    client = _client_rect_screen(int(scope["hwnd"]))
    if area == "client":
        if client is None:
            raise ValueError("scoped click blocked because the target window client rectangle could not be resolved")
        if not _point_in_rect(int(x), int(y), client):
            raise ValueError(
                f"scoped click target ({x}, {y}) is outside the visible client area; "
                f"client bounds are x={client['x']}..{client['x'] + client['width'] - 1}, "
                f"y={client['y']}..{client['y'] + client['height'] - 1}. "
                "Use target_area=window only for an intentional local window-chrome click."
            )

    return {
        "active": True,
        "allowed": True,
        "target_area": area,
        "scope_hwnd": int(scope["hwnd"]),
        "scope_title": scope.get("title"),
        "scope_process": scope.get("process_name"),
        "scope_focused": True,
        "visible_window_at_target": {
            "hwnd": int(under["hwnd"]),
            "title": under.get("title"),
            "pid": under.get("pid"),
            "process_name": under.get("process_name"),
        },
        "client_rect": client,
        "point": {"x": int(x), "y": int(y)},
    }


def list_windows() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    windows: list[dict[str, Any]] = []
    CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        info = _window_info_from_hwnd(int(hwnd))
        if (
            info
            and info["title"].strip()
            and not _is_runtime_overlay_window(info)
            and info["width"] > 0
            and info["height"] > 0
        ):
            windows.append(info)
        return True
    proc = CALLBACK(callback)
    user32.EnumWindows(proc, 0)
    return windows


def list_processes(query: str | None = None) -> list[dict[str, Any]]:
    """Group visible top-level windows by PID for agent-friendly discovery."""
    query_norm = str(query or "").strip().casefold()
    grouped: dict[int, dict[str, Any]] = {}
    for window in list_windows():
        haystack = " ".join(str(window.get(key) or "") for key in ("process_name", "process_path", "title", "pid")).casefold()
        if query_norm and query_norm not in haystack:
            continue
        pid = int(window["pid"])
        item = grouped.setdefault(pid, {"pid": pid, "process_name": window.get("process_name"), "process_path": window.get("process_path"), "windows": []})
        item["windows"].append({k: window.get(k) for k in ("hwnd", "title", "x", "y", "width", "height", "focused", "minimized")})
    result = list(grouped.values())
    for item in result:
        item["window_count"] = len(item["windows"])
    result.sort(key=lambda item: ((item.get("process_name") or "").casefold(), item["pid"]))
    return result




def uia_backend_status() -> dict[str, Any]:
    """Report optional read-only Windows UI Automation availability.

    UIA is intentionally optional so the bridge keeps its small required
    dependency surface.  The existing screenshot/Win32/PyAutoGUI core remains
    fully functional when the package is absent.
    """
    if os.name != "nt":
        return {
            "available": False,
            "platform_supported": False,
            "backend": "uiautomation",
            "reason": "Windows UI Automation is only available on Windows.",
        }
    try:
        installed = importlib.util.find_spec("uiautomation") is not None
    except Exception:
        installed = False
    return {
        "available": bool(installed),
        "platform_supported": True,
        "backend": "uiautomation",
        "mode": "read_only",
        "install_hint": None if installed else "pip install uiautomation",
        "reason": None if installed else "Optional Python package 'uiautomation' is not installed.",
    }


def _load_uia_backend():
    status = uia_backend_status()
    if not status.get("available"):
        return None, status
    try:
        import uiautomation as auto  # type: ignore
        return auto, status
    except Exception as exc:
        failed = dict(status)
        failed.update({
            "available": False,
            "reason": f"uiautomation import failed: {exc}",
            "install_hint": "pip install --upgrade uiautomation",
        })
        return None, failed


def _uia_safe_attr(control: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(control, name)
        return value() if callable(value) and name.startswith("Get") else value
    except Exception:
        return default


def _uia_rect_dict(rect: Any) -> dict[str, int] | None:
    if rect is None:
        return None
    try:
        left = int(round(float(getattr(rect, "left"))))
        top = int(round(float(getattr(rect, "top"))))
        right = int(round(float(getattr(rect, "right"))))
        bottom = int(round(float(getattr(rect, "bottom"))))
        return {
            "x": left,
            "y": top,
            "width": max(0, right - left),
            "height": max(0, bottom - top),
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
        }
    except Exception:
        pass
    try:
        values = list(rect)
        if len(values) >= 4:
            left, top, right, bottom = [int(round(float(v))) for v in values[:4]]
            return {
                "x": left,
                "y": top,
                "width": max(0, right - left),
                "height": max(0, bottom - top),
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
            }
    except Exception:
        pass
    return None


def _uia_target_window(hwnd: int | None = None) -> dict[str, Any] | None:
    if hwnd is not None:
        try:
            return _window_info_from_hwnd(int(hwnd))
        except Exception:
            return None
    scope = window_scope_status()
    if scope.get("active"):
        if scope.get("stale"):
            raise ValueError("active window scope is stale; choose a new scope or clear it before accessibility inspection")
        window = scope.get("window")
        if isinstance(window, dict):
            return window
    return focused_window()


def accessibility_tree(
    *,
    hwnd: int | None = None,
    max_depth: int = 3,
    max_nodes: int = 250,
    include_offscreen: bool = False,
) -> dict[str, Any]:
    """Return a bounded, read-only UI Automation tree for one top-level window.

    This endpoint deliberately exposes no Invoke/Value/Click UIA actions.  UIA
    is an observation source only; all real input continues through the
    canonical pointer/keyboard core and therefore retains viewport/scope/safety
    validation.
    """
    try:
        max_depth = int(max_depth)
        max_nodes = int(max_nodes)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_depth and max_nodes must be integers") from exc
    if not 0 <= max_depth <= 8:
        raise ValueError("max_depth must be between 0 and 8")
    if not 1 <= max_nodes <= 2000:
        raise ValueError("max_nodes must be between 1 and 2000")

    auto, backend = _load_uia_backend()
    target = _uia_target_window(hwnd)
    payload: dict[str, Any] = {
        "ok": True,
        "available": bool(auto is not None),
        "backend": backend,
        "mode": "read_only",
        "target_window": target,
        "limits": {
            "max_depth": max_depth,
            "max_nodes": max_nodes,
            "include_offscreen": bool(include_offscreen),
        },
        "agent_contract": [
            "Use UIA text/control metadata to understand the interface before falling back to pixel guessing.",
            "Bounding rectangles are absolute Windows screen pixels; use bridge ACTION coordinates when issuing pointer calls, especially when a window scope is active.",
            "UIA is observation-only in this bridge. Do not assume a control is clickable merely because it appears in the tree.",
            "The bridge does not read ValuePattern/text-entry values through UIA; password contents are never requested.",
        ],
    }
    if auto is None:
        payload["tree"] = None
        return payload
    if not target or not target.get("hwnd"):
        payload.update({"available": True, "tree": None, "reason": "No focused/scoped target window is available."})
        return payload

    try:
        root = auto.ControlFromHandle(int(target["hwnd"]))
    except Exception as exc:
        payload.update({"tree": None, "reason": f"UI Automation could not open target HWND: {exc}"})
        return payload
    if root is None:
        payload.update({"tree": None, "reason": "UI Automation returned no root control for the target window."})
        return payload

    node_count = 0
    skipped_offscreen = 0
    property_errors = 0
    truncated = False

    def read_attr(control: Any, name: str, default: Any = None) -> Any:
        nonlocal property_errors
        try:
            value = getattr(control, name)
            return value() if callable(value) and name.startswith("Get") else value
        except Exception:
            property_errors += 1
            return default

    def walk(control: Any, depth: int) -> dict[str, Any] | None:
        nonlocal node_count, skipped_offscreen, truncated, property_errors
        if node_count >= max_nodes:
            truncated = True
            return None
        offscreen = bool(read_attr(control, "IsOffscreen", False))
        if offscreen and not include_offscreen and depth > 0:
            skipped_offscreen += 1
            return None

        rect = _uia_rect_dict(read_attr(control, "BoundingRectangle"))
        node_count += 1
        node: dict[str, Any] = {
            "index": node_count - 1,
            "depth": depth,
            "name": str(read_attr(control, "Name", "") or ""),
            "control_type": str(read_attr(control, "ControlTypeName", "") or ""),
            "automation_id": str(read_attr(control, "AutomationId", "") or ""),
            "class_name": str(read_attr(control, "ClassName", "") or ""),
            "bounding_rect": rect,
            "enabled": bool(read_attr(control, "IsEnabled", False)),
            "offscreen": offscreen,
            "keyboard_focus": bool(read_attr(control, "HasKeyboardFocus", False)),
            "keyboard_focusable": bool(read_attr(control, "IsKeyboardFocusable", False)),
            "is_password": read_attr(control, "IsPassword", None),
            "native_window_handle": int(read_attr(control, "NativeWindowHandle", 0) or 0),
            "children": [],
        }
        if depth >= max_depth or node_count >= max_nodes:
            if node_count >= max_nodes:
                truncated = True
            return node
        try:
            children = control.GetChildren() or []
        except Exception:
            property_errors += 1
            children = []
        for child in children:
            if node_count >= max_nodes:
                truncated = True
                break
            child_node = walk(child, depth + 1)
            if child_node is not None:
                node["children"].append(child_node)
        return node

    try:
        tree = walk(root, 0)
    except Exception as exc:
        payload.update({"tree": None, "reason": f"UI Automation traversal failed: {exc}"})
        return payload

    payload.update({
        "tree": tree,
        "node_count": node_count,
        "truncated": truncated,
        "skipped_offscreen": skipped_offscreen,
        "property_errors": property_errors,
    })
    return payload


class ObservationStateRegistry:
    """Bounded in-memory registry for screenshot/UIA observations.

    This registry is not a second input owner.  It only binds evidence returned
    by the observation owner to later requests; resolved actions still flow
    through the canonical pointer/keyboard dispatcher and all existing safety
    gates.
    """

    def __init__(self, *, max_states: int = OBSERVATION_STATE_MAX, ttl_seconds: float = OBSERVATION_STATE_TTL_SECONDS):
        self.max_states = int(max_states)
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._states: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [sid for sid, entry in self._states.items() if now - float(entry.get("created_monotonic", 0.0)) > self.ttl_seconds]
        for sid in expired:
            self._states.pop(sid, None)
        while len(self._states) > self.max_states:
            self._states.popitem(last=False)

    def store(self, *, private: dict[str, Any]) -> str:
        state_id = uuid.uuid4().hex
        with self._lock:
            self._purge_locked()
            self._states[state_id] = {
                "created_monotonic": time.monotonic(),
                "private": private,
            }
            self._states.move_to_end(state_id)
            self._purge_locked()
        return state_id

    def get(self, state_id: Any) -> dict[str, Any]:
        sid = str(state_id or "").strip()
        if not sid:
            raise ObservationStateError("state_id is required for a state-bound action", code="INVALID_STATE", retry="never")
        with self._lock:
            self._purge_locked()
            entry = self._states.get(sid)
            if entry is None:
                raise ObservationStateError(
                    "observation state is missing or expired; call GET /v1/app-state again before acting",
                    code="STALE_STATE",
                    retry="reobserve",
                )
            self._states.move_to_end(sid)
            return entry

    def consume_remote_action(self, state_id: Any) -> bool:
        """One real action per RDP raster observation across all transports.

        An RDP client window may remain identical while the remote focused
        control changes. Reusing its old raster is not a fresh target proof.
        """
        sid = str(state_id or "").strip()
        if not sid:
            return False
        with self._lock:
            self._purge_locked()
            entry = self._states.get(sid)
            if entry is None:
                raise ObservationStateError(
                    "observation has expired; re-observe before acting",
                    code="STALE_STATE", retry="reobserve")
            if not (entry.get("private", {}).get("surface") or {}).get("remote"):
                return False
            self._states.pop(sid, None)
            return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._purge_locked()
            return {
                "active_states": len(self._states),
                "max_states": self.max_states,
                "ttl_seconds": self.ttl_seconds,
            }


OBSERVATION_STATES = ObservationStateRegistry()


class ControllerLeaseManager:
    """Single-owner lease for mutating computer control.

    Read-only observation remains available without a lease.  Once a lease is
    acquired, every mutating action must present the matching controller_id.
    The lease auto-expires to recover from crashed agents; an explicit stop is a
    per-controller kill switch that stays effective until release or expiry.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, Any] | None = None

    @staticmethod
    def _lease_seconds(value: Any) -> float:
        if value is None:
            return CONTROLLER_LEASE_DEFAULT_SECONDS
        try:
            seconds = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("lease_seconds must be numeric") from exc
        if not CONTROLLER_LEASE_MIN_SECONDS <= seconds <= CONTROLLER_LEASE_MAX_SECONDS:
            raise ValueError(
                f"lease_seconds must be between {CONTROLLER_LEASE_MIN_SECONDS:g} and {CONTROLLER_LEASE_MAX_SECONDS:g}"
            )
        return seconds

    def _purge_expired_locked(self) -> None:
        active = self._active
        if active is None:
            return
        if time.monotonic() >= float(active.get("expires_monotonic", 0.0)):
            self._active = None

    @staticmethod
    def _public(active: dict[str, Any] | None, *, include_id: bool = False) -> dict[str, Any]:
        if active is None:
            return {"active": False}
        remaining = max(0.0, float(active.get("expires_monotonic", 0.0)) - time.monotonic())
        result = {
            "active": True,
            "owner": active.get("owner"),
            "stopped": bool(active.get("stopped", False)),
            "stop_reason": active.get("stop_reason"),
            "lease_seconds": float(active.get("lease_seconds", CONTROLLER_LEASE_DEFAULT_SECONDS)),
            "expires_in_seconds": round(remaining, 3),
            "acquired_at": active.get("acquired_at"),
            "last_seen_at": active.get("last_seen_at"),
        }
        if include_id:
            result["controller_id"] = active.get("controller_id")
        else:
            cid = str(active.get("controller_id") or "")
            result["controller_id_hint"] = cid[:8] if cid else None
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._purge_expired_locked()
            return self._public(self._active)

    def acquire(self, *, owner: Any = None, lease_seconds: Any = None) -> dict[str, Any]:
        seconds = self._lease_seconds(lease_seconds)
        owner_text = _bounded_text(owner or "agent", 120) or "agent"
        with self._lock:
            self._purge_expired_locked()
            if self._active is not None:
                raise ControllerLeaseError(
                    "computer control is already owned by another live controller session",
                    details=self._public(self._active),
                )
            now = time.monotonic()
            controller_id = uuid.uuid4().hex
            self._active = {
                "controller_id": controller_id,
                "owner": owner_text,
                "lease_seconds": seconds,
                "expires_monotonic": now + seconds,
                "acquired_at": _now_iso(),
                "last_seen_at": _now_iso(),
                "stopped": False,
                "stop_reason": None,
            }
            return {"ok": True, **self._public(self._active, include_id=True)}

    def _matching_locked(self, controller_id: Any, *, allow_stopped: bool = False, touch: bool = False) -> dict[str, Any]:
        self._purge_expired_locked()
        active = self._active
        cid = str(controller_id or "").strip()
        if active is None:
            raise ControllerLeaseError(
                "controller session is missing or expired; acquire a new controller lease before continuing",
                code="CONTROL_SESSION_GONE",
                retry="never",
            )
        if not cid or cid != str(active.get("controller_id") or ""):
            raise ControllerLeaseError(
                "computer control is owned by another live controller session",
                code="CONTROLLER_BUSY",
                retry="never",
                details=self._public(active),
            )
        if active.get("stopped") and not allow_stopped:
            raise ControllerLeaseError(
                "this controller session was stopped by its kill switch; release it or wait for lease expiry",
                code="CONTROL_STOPPED",
                retry="never",
                details=self._public(active),
            )
        if touch:
            seconds = float(active.get("lease_seconds", CONTROLLER_LEASE_DEFAULT_SECONDS))
            active["expires_monotonic"] = time.monotonic() + seconds
            active["last_seen_at"] = _now_iso()
        return active

    def validate_action(self, controller_id: Any) -> dict[str, Any] | None:
        """Validate a mutating request. Legacy requests remain valid only when no lease exists."""
        with self._lock:
            self._purge_expired_locked()
            if self._active is None:
                if controller_id:
                    raise ControllerLeaseError(
                        "controller_id does not name a live controller session",
                        code="CONTROL_SESSION_GONE",
                        retry="never",
                    )
                return None
            return self._matching_locked(controller_id, touch=True)

    def bind_observation(self, controller_id: Any) -> str | None:
        if not controller_id:
            return None
        with self._lock:
            active = self._matching_locked(controller_id, allow_stopped=False, touch=True)
            return str(active.get("controller_id"))

    def heartbeat(self, controller_id: Any) -> dict[str, Any]:
        with self._lock:
            active = self._matching_locked(controller_id, allow_stopped=False, touch=True)
            return {"ok": True, **self._public(active, include_id=True)}

    def stop(self, controller_id: Any, *, reason: Any = None) -> dict[str, Any]:
        with self._lock:
            active = self._matching_locked(controller_id, allow_stopped=True, touch=False)
            active["stopped"] = True
            active["stop_reason"] = _bounded_text(reason or "stopped by controller", 240)
            active["last_seen_at"] = _now_iso()
            return {"ok": True, **self._public(active, include_id=True)}

    def stop_active_from_user(self, *, reason: str = "physical user stop") -> dict[str, Any] | None:
        """Trusted local-user kill switch; no controller id is required."""
        with self._lock:
            self._purge_expired_locked()
            if self._active is None:
                return None
            # First physical Esc kills the active controller; a second Esc while
            # that lease is already stopped means "close the bridge".  Returning
            # None lets the server-level handler take its normal idle shutdown path.
            if self._active.get("stopped"):
                return None
            self._active["stopped"] = True
            self._active["stop_reason"] = _bounded_text(reason, 240)
            self._active["last_seen_at"] = _now_iso()
            return self._public(self._active)

    def release(self, controller_id: Any) -> dict[str, Any]:
        with self._lock:
            active = self._matching_locked(controller_id, allow_stopped=True, touch=False)
            released = self._public(active, include_id=True)
            self._active = None
            return {"ok": True, "released": True, "controller": released}


CONTROLLER_LEASE = ControllerLeaseManager()


def _flatten_accessibility_tree(tree: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(tree, dict):
        return rows

    def visit(node: dict[str, Any]) -> None:
        is_password = node.get("is_password")
        rows.append({
            "index": int(node.get("index", len(rows))),
            "depth": int(node.get("depth", 0)),
            "name": "<password-control>" if is_password is not False else _bounded_text(node.get("name", "")),
            "control_type": _bounded_text(node.get("control_type", ""), 80),
            "automation_id": _bounded_text(node.get("automation_id", ""), 160),
            "class_name": _bounded_text(node.get("class_name", ""), 160),
            "bounding_rect_windows_pixels": node.get("bounding_rect"),
            "enabled": bool(node.get("enabled", False)),
            "offscreen": bool(node.get("offscreen", False)),
            "keyboard_focus": bool(node.get("keyboard_focus", False)),
            "keyboard_focusable": bool(node.get("keyboard_focusable", False)),
            "is_password": is_password,
        })
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                visit(child)

    visit(tree)
    return rows


def _crop_window_capture_to_region(
    image: Image.Image,
    binding: dict[str, Any],
    region: dict[str, Any],
) -> Image.Image:
    """Crop an HWND capture to a Windows-pixel subregion using its declared mapping."""
    mapping = binding.get("screen_mapping") or {}
    mx = int(mapping.get("x", 0)); my = int(mapping.get("y", 0))
    mw = int(mapping.get("width", 0)); mh = int(mapping.get("height", 0))
    if mw <= 0 or mh <= 0:
        return image
    left = round((int(region["x"]) - mx) * image.size[0] / mw)
    top = round((int(region["y"]) - my) * image.size[1] / mh)
    right = round((int(region["x"]) + int(region["width"]) - mx) * image.size[0] / mw)
    bottom = round((int(region["y"]) + int(region["height"]) - my) * image.size[1] / mh)
    left = clamp(int(left), 0, image.size[0]); top = clamp(int(top), 0, image.size[1])
    right = clamp(int(right), left, image.size[0]); bottom = clamp(int(bottom), top, image.size[1])
    if right <= left or bottom <= top:
        return image
    return image.crop((left, top, right, bottom))


def _capture_target_window_state_image(
    target: dict[str, Any],
    *,
    capture_provider: Any = DEFAULT_CAPTURE_PROVIDER,
    surface: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture the observed surface for state-bound raster actions.

    Normal windows use the top-level window frame.  Remote Desktop is a nested
    desktop, so its canonical raster is the local RDP *client area* rather than
    local title bars/chrome.  Raster coordinates can therefore never silently
    mean both local window chrome and remote desktop content.
    """
    requested = normalize_capture_provider(capture_provider)
    image, binding = _capture_scoped_raw(target, capture_provider=requested)
    surface = dict(surface or _surface_context(target))
    region = {
        "x": int(target["x"]), "y": int(target["y"]),
        "width": int(target["width"]), "height": int(target["height"]),
    }
    if surface.get("remote"):
        client = surface.get("client_rect_windows_pixels")
        if isinstance(client, dict) and int(client.get("width") or 0) > 0 and int(client.get("height") or 0) > 0:
            image = _crop_window_capture_to_region(image, binding, client)
            region = {k: int(client[k]) for k in ("x", "y", "width", "height")}
            binding = dict(binding)
            binding["surface_crop"] = "remote_desktop_client_area"
            binding["screen_mapping"] = {**region, "method": "remote_desktop_client_area"}
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    return {
        "mime": "image/png",
        "width": image.size[0],
        "height": image.size[1],
        "bytes": len(raw),
        "base64": base64.b64encode(raw).decode("ascii"),
        "capture_provider": binding.get("provider"),
        "capture_binding": binding,
        "image_region_windows_pixels": region,
    }


def app_state_snapshot(**options: Any) -> dict[str, Any]:
    with COMPUTER_ACTION_LOCK:
        return _app_state_snapshot_unlocked(**options)


def _app_state_snapshot_unlocked(
    *,
    include_image: bool = True,
    max_depth: int = 4,
    max_nodes: int = 400,
    include_offscreen: bool = False,
    capture_provider: Any = DEFAULT_CAPTURE_PROVIDER,
    controller_id: Any = None,
    observation_source: str = "explicit",
) -> dict[str, Any]:
    """Return one state-bound observation: target window + UIA + optional raster.

    The public payload deliberately separates Windows/global element geometry
    from raster pixels.  Agents must use element indices or raster x/y together
    with this state_id; they must not copy UIA/window bounds into raster actions.
    """
    bound_controller_id = CONTROLLER_LEASE.bind_observation(controller_id)
    target = _uia_target_window()
    if not target or not target.get("hwnd"):
        raise ObservationStateError(
            "no focused/scoped target window is available; focus a target window or select /v1/scope, then re-observe",
            code="ELEMENT_UNAVAILABLE",
            retry="reobserve",
        )
    surface = _surface_context(target)
    ax = accessibility_tree(
        hwnd=int(target["hwnd"]),
        max_depth=max_depth,
        max_nodes=max_nodes,
        include_offscreen=include_offscreen,
    )
    # Local UI Automation cannot see controls *inside* an RDP desktop.  Returning
    # those local host/chrome nodes as actionable remote elements caused agents
    # to confuse two coordinate systems.  Preserve diagnostics, but expose no
    # semantic action indices for remote desktop content.
    all_elements = _flatten_accessibility_tree(ax.get("tree"))
    elements = [] if surface.get("remote") else all_elements

    image_info: dict[str, Any] | None = None
    if include_image:
        image_info = _capture_target_window_state_image(target, capture_provider=capture_provider, surface=surface)

    public: dict[str, Any] = {
        "ok": True,
        "time": _now_iso(),
        "target_window": _compact_window(target),
        "surface": surface,
        "accessibility": {
            "available": bool(ax.get("available")) and not bool(surface.get("remote")),
            "backend": ax.get("backend"),
            "node_count": len(elements),
            "truncated": bool(ax.get("truncated", False)),
            "elements": elements,
            "reason": (
                "Remote Desktop content is an opaque nested surface to local Windows UI Automation; use the state-bound raster."
                if surface.get("remote") else ax.get("reason")
            ),
            "local_host_node_count": len(all_elements) if surface.get("remote") else None,
        },
        "raster": None,
        "contract": {
            "element_target": (
                "Unavailable for remote desktop content; local UIA does not describe controls inside the remote session."
                if surface.get("remote") else
                "Use state_id + element_index. Element indices belong only to this observation."
            ),
            "coordinate_target": (
                "Use state_id + image_x/image_y. For Remote Desktop these pixels are bound to the local RDP client area only."
                if surface.get("remote") else
                "Use state_id + image_x/image_y. These are pixels in this target-window raster only."
            ),
            "geometry_separation": "UIA/window bounds are Windows virtual-desktop diagnostics, never raster coordinates.",
            "stale_state": "State-bound actions fail closed when the observation expires or the observed target window is no longer current.",
            "remote_focus": ("Only the local RDP client window is attested; remote application/control focus can change independently. Real actions consume this state and require fresh observation."
                             if surface.get("remote") else None),
            "success": "action_sent means input was dispatched, not that the requested UI effect happened; re-observe to verify effect.",
        },
    }
    if image_info is not None:
        public["raster"] = {
            "mime": image_info.get("mime"),
            "width": image_info.get("width"),
            "height": image_info.get("height"),
            "base64": image_info.get("base64"),
            "capture_provider": image_info.get("capture_provider"),
            "capture_binding": image_info.get("capture_binding"),
            "image_region_windows_pixels": image_info.get("image_region_windows_pixels"),
        }

    private = {
        "target_window": _compact_window(target),
        "surface": surface,
        "elements_by_index": {int(row["index"]): row for row in elements},
        "raster_width": int(image_info.get("width")) if image_info and image_info.get("width") is not None else None,
        "raster_height": int(image_info.get("height")) if image_info and image_info.get("height") is not None else None,
        "image_region_windows_pixels": image_info.get("image_region_windows_pixels") if image_info else None,
        "controller_id": bound_controller_id,
    }
    state_id = OBSERVATION_STATES.store(private=private)
    public["state_id"] = state_id
    public["expires_in_seconds"] = OBSERVATION_STATE_TTL_SECONDS
    if bound_controller_id:
        public["controller_binding"] = {"bound": True, "controller_id": bound_controller_id}
    AGENT_JOURNAL.observe(public, bound_controller_id, source=observation_source)
    return public


def _state_current_window_or_raise(entry: dict[str, Any]) -> dict[str, Any] | None:
    expected = entry.get("private", {}).get("target_window")
    if not expected or not expected.get("hwnd"):
        return None
    scope = window_scope_status()
    if scope.get("active"):
        if scope.get("stale"):
            raise ObservationStateError("active window scope became stale; re-observe before acting")
        current = scope.get("window")
    else:
        current = focused_window()
    if not current or int(current.get("hwnd") or 0) != int(expected.get("hwnd") or 0):
        raise ObservationStateError(
            "the observed target window is no longer the current focused/scoped target; re-observe before acting",
            code="STALE_STATE",
            retry="reobserve",
        )
    expected_geometry = tuple(int(expected.get(k) or 0) for k in ("x", "y", "width", "height"))
    current_geometry = tuple(int(current.get(k) or 0) for k in ("x", "y", "width", "height"))
    if current_geometry != expected_geometry:
        raise ObservationStateError(
            "the observed target window moved or resized; the referenced geometry is stale, so re-observe before acting",
            code="STALE_STATE",
            retry="reobserve",
        )
    expected_surface = entry.get("private", {}).get("surface") or {}
    current_surface = _surface_context(current)
    if bool(expected_surface.get("remote")) != bool(current_surface.get("remote")):
        raise ObservationStateError(
            "the observed desktop surface changed between local and remote; re-observe before acting",
            code="STALE_STATE", retry="reobserve",
        )
    for key in ("process_name", "class_name"):
        before = str(expected_surface.get(key) or "").casefold()
        after = str(current_surface.get(key) or "").casefold()
        if before and after and before != after:
            raise ObservationStateError(
                "the observed desktop surface identity changed; re-observe before acting",
                code="STALE_STATE", retry="reobserve",
            )
    if expected_surface.get("remote"):
        expected_client = expected_surface.get("client_rect_windows_pixels") or {}
        current_client = current_surface.get("client_rect_windows_pixels") or {}
        if expected_client != current_client:
            raise ObservationStateError(
                "the Remote Desktop client area changed size or position; its raster mapping is stale, so re-observe before acting",
                code="STALE_STATE", retry="reobserve",
            )
        expected_monitor = (expected_surface.get("monitor") or {}).get("device")
        current_monitor = (current_surface.get("monitor") or {}).get("device")
        if expected_monitor and current_monitor and expected_monitor != current_monitor:
            raise ObservationStateError(
                "the Remote Desktop surface moved to another monitor; re-observe before acting",
                code="STALE_STATE", retry="reobserve",
            )
    return current


def _same_uia_identity(expected: dict[str, Any], live: dict[str, Any] | None) -> bool:
    if not live:
        return False
    for key in ("automation_id", "control_type", "class_name"):
        a = str(expected.get(key) or "")
        b = str(live.get(key) or "")
        if a and b and a != b:
            return False
    expected_name = str(expected.get("name") or "")
    live_name = str(live.get("name") or "")
    if expected_name and expected_name != "<password-control>" and live_name and expected_name != live_name:
        return False
    return True


def resolve_state_bound_action(data: dict[str, Any], *, action: str) -> dict[str, Any]:
    """Resolve state-bound targets into canonical Windows coordinates.

    This is an adapter only.  The returned request is still executed by the
    existing canonical action owner and therefore retains every scope/viewport/
    foreground/hit-test guard.
    """
    if "state_id" not in data:
        return dict(data)
    entry = OBSERVATION_STATES.get(data.get("state_id"))
    _state_current_window_or_raise(entry)
    surface = entry.get("private", {}).get("surface") or {}
    resolved = dict(data)
    bound_controller_id = entry.get("private", {}).get("controller_id")
    requested_controller_id = str(resolved.get("controller_id") or "").strip() or None
    if bound_controller_id and requested_controller_id and requested_controller_id != bound_controller_id:
        raise ControllerLeaseError(
            "state_id belongs to a different controller session",
            code="CONTROLLER_BUSY",
            retry="never",
        )
    if bound_controller_id and not requested_controller_id:
        resolved["controller_id"] = bound_controller_id

    element_present = "element_index" in resolved
    raster_present = "image_x" in resolved or "image_y" in resolved
    point_target_actions = {"move", "click", "drag", "scroll"}
    if (element_present or raster_present) and action not in point_target_actions:
        raise ObservationStateError(
            f"{action} does not consume a point target; use state_id alone to bind it to the observed window",
            code="INVALID_STATE",
            retry="never",
        )
    if element_present and raster_present:
        raise ObservationStateError("use either element_index or image_x/image_y, not both", code="INVALID_STATE", retry="never")

    if element_present:
        if surface.get("remote"):
            raise ObservationStateError(
                "Remote Desktop content is opaque to local UI Automation; use this state_id with image_x/image_y from its raster instead of element_index",
                code="ACTION_UNAVAILABLE", retry="never",
            )
        try:
            index = int(resolved.pop("element_index"))
        except (TypeError, ValueError) as exc:
            raise ObservationStateError("element_index must be an integer", code="INVALID_STATE", retry="never") from exc
        element = entry.get("private", {}).get("elements_by_index", {}).get(index)
        if not element:
            raise ObservationStateError("element index is not present in this observation; re-observe before acting")
        rect = element.get("bounding_rect_windows_pixels")
        if not isinstance(rect, dict) or int(rect.get("width") or 0) <= 0 or int(rect.get("height") or 0) <= 0:
            raise ObservationStateError("element has no actionable bounding rectangle", code="ELEMENT_UNAVAILABLE", retry="reobserve")
        if not element.get("enabled", False) or element.get("offscreen", False):
            raise ObservationStateError("element is disabled or offscreen", code="ELEMENT_UNAVAILABLE", retry="reobserve")
        px = int(rect["x"]) + max(0, int(rect["width"]) // 2)
        py = int(rect["y"]) + max(0, int(rect["height"]) // 2)
        live = uia_context_at_point(px, py).get("target")
        if not _same_uia_identity(element, live):
            raise ObservationStateError("element identity changed at the observed location; re-observe before acting")
        owner = window_at(px, py)
        expected_hwnd = int(entry.get("private", {}).get("target_window", {}).get("hwnd") or 0)
        if not owner or int(owner.get("hwnd") or 0) != expected_hwnd:
            raise ObservationStateError("the observed element point is no longer visibly owned by the target window; re-observe before acting")
        resolved["x"] = px
        resolved["y"] = py
        resolved["coordinate_space"] = "virtual_desktop"
        resolved["resolved_target"] = {"type": "element", "state_id": data.get("state_id"), "element_index": index}
    elif raster_present:
        if "image_x" not in resolved or "image_y" not in resolved:
            raise ObservationStateError("state-bound raster target requires image_x and image_y", code="INVALID_STATE", retry="never")
        try:
            ix = int(resolved.pop("image_x")); iy = int(resolved.pop("image_y"))
        except (TypeError, ValueError) as exc:
            raise ObservationStateError("image_x and image_y must be integer raster pixels", code="INVALID_STATE", retry="never") from exc
        width = entry.get("private", {}).get("raster_width")
        height = entry.get("private", {}).get("raster_height")
        region = entry.get("private", {}).get("image_region_windows_pixels")
        if not width or not height or not isinstance(region, dict):
            raise ObservationStateError("this observation has no raster; request app-state with include_image=true", code="STALE_STATE", retry="reobserve")
        if not (0 <= ix < int(width) and 0 <= iy < int(height)):
            raise ObservationStateError("image_x/image_y are outside the referenced raster", code="INVALID_STATE", retry="never")
        px = int(region["x"]) + int(ix * int(region["width"]) / max(1, int(width)))
        py = int(region["y"]) + int(iy * int(region["height"]) / max(1, int(height)))
        owner = window_at(px, py)
        expected_hwnd = int(entry.get("private", {}).get("target_window", {}).get("hwnd") or 0)
        if not owner or int(owner.get("hwnd") or 0) != expected_hwnd:
            raise ObservationStateError("the referenced raster point is no longer visibly owned by the target window; re-observe before acting")
        resolved["x"] = px
        resolved["y"] = py
        resolved["coordinate_space"] = "virtual_desktop"
        resolved["resolved_target"] = {"type": "raster", "state_id": data.get("state_id"), "image_x": ix, "image_y": iy}
    else:
        if surface.get("remote") and action in point_target_actions:
            raise ObservationStateError(
                "Remote Desktop pointer actions require raster coordinates from the same observation; use state_id + image_x/image_y",
                code="REMOTE_SURFACE_STATE_REQUIRED", retry="reobserve",
            )
        # A state_id without an element/raster target is useful for keyboard
        # actions: the current focused/scoped window was checked above.
        resolved["resolved_target"] = {
            "type": "remote_window_binding" if surface.get("remote") else "window_binding",
            "state_id": data.get("state_id"),
        }

    return resolved


def _process_selector_matches(candidate: dict[str, Any], process: str) -> bool:
    wanted = Path(str(process).strip()).name.casefold()
    actual = str(candidate.get("process_name") or "").casefold()
    if not wanted:
        return False
    return actual == wanted or actual.removesuffix(".exe") == wanted.removesuffix(".exe")


def activate_window(hwnd: int) -> bool:
    """Best-effort foreground activation without injecting mouse/keyboard input.

    Plain SetForegroundWindow is frequently refused when the detached bridge did
    not own the last foreground interaction.  The r28 real-agent run hit exactly
    that dead-end: scope(activate=true) returned false, forcing the agent into an
    unsafe raw-click workaround.  AttachThreadInput is the documented Win32-style
    focus handoff mechanism; it keeps activation in the window/scope owner and
    does not create a second pointer/keyboard path.
    """
    if os.name != "nt":
        raise ValueError("window activation currently requires Windows")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    if not user32.IsWindow(int(hwnd)):
        raise ValueError("window no longer exists")

    SW_RESTORE = 9
    if user32.IsIconic(int(hwnd)):
        user32.ShowWindow(int(hwnd), SW_RESTORE)

    def is_target_foreground() -> bool:
        raw = user32.GetForegroundWindow()
        return bool(raw) and _nullable_hwnd_int(raw) == int(hwnd)

    # Cheap path first.
    user32.BringWindowToTop(int(hwnd))
    user32.SetForegroundWindow(int(hwnd))
    time.sleep(0.025)
    if is_target_foreground():
        return True

    # Foreground-lock fallback: temporarily join the relevant GUI input queues.
    foreground_raw = user32.GetForegroundWindow()
    current_thread = int(kernel32.GetCurrentThreadId())
    target_thread = int(user32.GetWindowThreadProcessId(int(hwnd), None))
    foreground_thread = int(user32.GetWindowThreadProcessId(foreground_raw, None)) if foreground_raw else 0
    attached: list[tuple[int, int]] = []
    try:
        for other in (foreground_thread, target_thread):
            if other and other != current_thread and (current_thread, other) not in attached:
                if user32.AttachThreadInput(current_thread, other, True):
                    attached.append((current_thread, other))
        user32.BringWindowToTop(int(hwnd))
        user32.SetForegroundWindow(int(hwnd))
        try:
            user32.SetFocus(int(hwnd))
        except Exception:
            pass
        time.sleep(0.04)
        return is_target_foreground()
    finally:
        for source, other in reversed(attached):
            try:
                user32.AttachThreadInput(source, other, False)
            except Exception:
                pass


def active_scope_window() -> dict[str, Any] | None:
    """Return the live scoped window; fail closed when a configured target vanished."""
    with WINDOW_SCOPE_LOCK:
        configured = dict(ACTIVE_WINDOW_SCOPE) if ACTIVE_WINDOW_SCOPE is not None else None
    if configured is None:
        return None
    info = _window_info_from_hwnd(int(configured["hwnd"]))
    if info is None or int(info.get("pid", -1)) != int(configured.get("pid", -2)):
        raise ValueError("active window scope target is no longer available; use POST /v1/scope-clear or select the process/window again")
    return info


def window_scope_status() -> dict[str, Any]:
    with WINDOW_SCOPE_LOCK:
        configured = dict(ACTIVE_WINDOW_SCOPE) if ACTIVE_WINDOW_SCOPE is not None else None
    if configured is None:
        return {"active": False, "coordinate_space": "screen", "action_coordinate_space": "virtual_desktop"}
    try:
        window = active_scope_window()
    except ValueError as exc:
        return {"active": True, "stale": True, "coordinate_space": "window_relative", "action_coordinate_space": "window_scope", "configured": configured, "error": str(exc)}
    return {
        "active": True,
        "stale": False,
        "coordinate_space": "window_relative",  # legacy descriptive field
        "action_coordinate_space": "window_scope",
        "window": window,
        "coordinates": {
            "origin": "scoped window top-left",
            "x": [0, max(0, int(window["width"])-1)],
            "y": [0, max(0, int(window["height"])-1)],
            "normalized": "0.0..1.0 across the scoped window",
            "request_contract": "coordinate_space is REQUIRED for pointer calls with x/y while this scope is active.",
            "window_relative_mode": "Set coordinate_space=window_scope for x/y relative to this window.",
            "absolute_mode": "Set coordinate_space=virtual_desktop for absolute Windows coordinates; target is rejected unless inside this scope.",
        },
    }


def set_window_scope(data: dict[str, Any]) -> dict[str, Any]:
    """Select exactly one visible top-level window by process/PID/HWND/title."""
    global ACTIVE_WINDOW_SCOPE
    if os.name != "nt":
        raise ValueError("process/window scope currently requires Windows")
    allowed = {"process", "pid", "hwnd", "title", "activate"}
    extra = set(data) - allowed
    if extra:
        raise ValueError(f"scope does not accept fields: {', '.join(sorted(extra))}")
    selectors = [name for name in ("process", "pid", "hwnd", "title") if data.get(name) not in (None, "")]
    if not selectors:
        raise ValueError("scope requires one or more selectors: process, pid, hwnd, or title")
    candidates = list_windows()
    if data.get("hwnd") not in (None, ""):
        try: wanted_hwnd = int(data["hwnd"])
        except (TypeError, ValueError) as exc: raise ValueError("scope hwnd must be an integer") from exc
        candidates = [w for w in candidates if int(w["hwnd"]) == wanted_hwnd]
    if data.get("pid") not in (None, ""):
        try: wanted_pid = int(data["pid"])
        except (TypeError, ValueError) as exc: raise ValueError("scope pid must be an integer") from exc
        candidates = [w for w in candidates if int(w["pid"]) == wanted_pid]
    if data.get("process") not in (None, ""):
        candidates = [w for w in candidates if _process_selector_matches(w, str(data["process"]))]
    if data.get("title") not in (None, ""):
        title = str(data["title"]).casefold()
        candidates = [w for w in candidates if title in str(w.get("title") or "").casefold()]
    if not candidates:
        raise ValueError("no visible top-level window matched the requested process/window scope")
    if len(candidates) > 1:
        compact = [{k: w.get(k) for k in ("hwnd", "pid", "process_name", "title", "x", "y", "width", "height", "focused", "minimized")} for w in candidates[:20]]
        raise WindowScopeAmbiguous(f"scope selector matched {len(candidates)} windows; refine with pid, hwnd, or title", compact)
    chosen = candidates[0]
    configured = {"hwnd": int(chosen["hwnd"]), "pid": int(chosen["pid"]), "process_name": chosen.get("process_name"), "process_path": chosen.get("process_path"), "title_at_selection": chosen.get("title"), "selected_at": _now_iso()}
    with WINDOW_SCOPE_LOCK:
        ACTIVE_WINDOW_SCOPE = configured
    activated = activate_window(int(chosen["hwnd"])) if bool(data.get("activate", True)) else False
    return {"ok": True, "activated": activated, "scope": window_scope_status()}


def clear_window_scope() -> bool:
    global ACTIVE_WINDOW_SCOPE
    with WINDOW_SCOPE_LOCK:
        existed = ACTIVE_WINDOW_SCOPE is not None
        ACTIVE_WINDOW_SCOPE = None
    return existed


def require_point_in_scope(x: int, y: int) -> None:
    scope = active_scope_window()
    if scope is None:
        return
    if not (int(scope["x"]) <= x < int(scope["x"])+int(scope["width"]) and int(scope["y"]) <= y < int(scope["y"])+int(scope["height"])):
        raise ValueError(f"target ({x}, {y}) is outside active process/window scope {scope.get('process_name') or scope.get('pid')} / {scope.get('title')!r}")


def require_pointer_allowed(x: int, y: int) -> None:
    require_point_in_scope(x, y)
    require_point_in_viewport(x, y)


def require_scope_focused_for_keyboard() -> None:
    scope = active_scope_window()
    if scope is None:
        return
    front = focused_window()
    if front is None or int(front.get("hwnd", -1)) != int(scope["hwnd"]):
        raise ValueError(
            "keyboard input blocked because the active process/window scope is not focused; "
            "focus the scoped window first or clear the scope"
        )


def _effective_pointer_bounds() -> dict[str, int]:
    """Intersection of virtual desktop, optional RDP viewport, and window scope."""
    desktop = virtual_desktop_bounds()
    left, top = desktop["x"], desktop["y"]
    right = left + desktop["width"]
    bottom = top + desktop["height"]
    viewport = load_viewport()
    if viewport is not None:
        left=max(left,int(viewport["x"])); top=max(top,int(viewport["y"])); right=min(right,int(viewport["x"])+int(viewport["width"])); bottom=min(bottom,int(viewport["y"])+int(viewport["height"]))
    scope = active_scope_window()
    if scope is not None:
        left=max(left,int(scope["x"])); top=max(top,int(scope["y"])); right=min(right,int(scope["x"])+int(scope["width"])); bottom=min(bottom,int(scope["y"])+int(scope["height"]))
    if right <= left or bottom <= top:
        raise ValueError("active viewport and process/window scope have no controllable overlap")
    return {"x": left, "y": top, "width": right-left, "height": bottom-top}


RUNTIME_OVERLAY_TITLE = "Agent Computer Bridge Runtime Overlay"


def _is_runtime_overlay_window(info: dict[str, Any] | None) -> bool:
    return bool(info and str(info.get("title") or "") == RUNTIME_OVERLAY_TITLE)


def focused_window() -> dict[str, Any] | None:
    """Return the user application that should be treated as foreground.

    The click-through topmost runtime overlay uses WS_EX_NOACTIVATE, but some
    Windows/RDP combinations can transiently report its HWND from
    GetForegroundWindow(). Never expose that instrumentation window as the
    focused application to an agent. In that case walk down Z-order to the
    first real visible top-level window.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.restype = wintypes.HWND
    raw = user32.GetForegroundWindow()
    hwnd = int(raw) if raw else 0
    if not hwnd:
        return None
    info = _window_info_from_hwnd(hwnd)
    if not _is_runtime_overlay_window(info):
        if info is not None:
            info = dict(info)
            info["focus_source"] = "win32.GetForegroundWindow"
        return info

    GW_HWNDNEXT = 2
    candidate_raw = user32.GetWindow(hwnd, GW_HWNDNEXT)
    for _ in range(256):
        if not candidate_raw:
            break
        candidate_hwnd = int(candidate_raw)
        candidate = _window_info_from_hwnd(candidate_hwnd)
        if (
            candidate
            and not _is_runtime_overlay_window(candidate)
            and candidate.get("visible")
            and not candidate.get("minimized")
            and str(candidate.get("title") or "").strip()
            and int(candidate.get("width", 0)) > 0
            and int(candidate.get("height", 0)) > 0
        ):
            candidate = dict(candidate)
            candidate["focused"] = True
            candidate["focus_source"] = "under_runtime_overlay_z_order"
            candidate["raw_foreground_hwnd"] = hwnd
            return candidate
        candidate_raw = user32.GetWindow(candidate_hwnd, GW_HWNDNEXT)
    return None


def window_at(x: Any, y: Any) -> dict[str, Any] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    try:
        px, py = int(round(float(x))), int(round(float(y)))
    except (TypeError, ValueError) as exc:
        raise ValueError("window-at x and y must be numeric") from exc
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.restype = wintypes.HWND
    point = wintypes.POINT(px, py)
    raw_hwnd = user32.WindowFromPoint(point)
    if not raw_hwnd:
        return None
    hwnd = int(raw_hwnd)
    GA_ROOT = 2
    raw_root = user32.GetAncestor(hwnd, GA_ROOT)
    root = int(raw_root) if raw_root else hwnd
    info = _window_info_from_hwnd(root)
    # The click-through runtime overlay can still be returned by WindowFromPoint
    # on some Windows builds. Fall back to the first real top-level window whose
    # rectangle contains the point; EnumWindows is z-order ordered.
    if info and _is_runtime_overlay_window(info):
        for candidate in list_windows():
            if (candidate["x"] <= px < candidate["x"] + candidate["width"] and
                    candidate["y"] <= py < candidate["y"] + candidate["height"]):
                return candidate
        return None
    return info


def list_monitors() -> list[dict[str, Any]]:
    if os.name != "nt":
        width, height = screen_size()
        return [{"index": 0, "x": 0, "y": 0, "width": width, "height": height, "primary": True, "device": None}]
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    MONITORINFOF_PRIMARY = 1
    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32),
        ]
    monitors: list[dict[str, Any]] = []
    CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def callback(hmonitor: int, _hdc: int, _rect: Any, _lparam: int) -> bool:
        info = MONITORINFOEXW(); info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r, w = info.rcMonitor, info.rcWork
            monitors.append({
                "index": len(monitors), "device": info.szDevice,
                "x": int(r.left), "y": int(r.top), "width": int(r.right-r.left), "height": int(r.bottom-r.top),
                "work": {"x": int(w.left), "y": int(w.top), "width": int(w.right-w.left), "height": int(w.bottom-w.top)},
                "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
            })
        return True
    proc = CALLBACK(callback)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    return monitors


def monitor_for_point(x: int, y: int, monitors: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Return the physical monitor containing a virtual-desktop coordinate."""
    candidates = monitors if monitors is not None else list_monitors()
    for monitor in candidates:
        if _point_in_rect(int(x), int(y), monitor):
            return monitor
    return None


def clipboard_get_text() -> dict[str, Any]:
    if os.name != "nt":
        raise ValueError("clipboard endpoints currently require Windows")
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        raise ValueError("clipboard is busy")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return {"ok": True, "text": "", "available": False}
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise ValueError("could not lock clipboard data")
        try:
            text = ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
        return {"ok": True, "text": text, "available": True, "characters": len(text)}
    finally:
        user32.CloseClipboard()


def clipboard_set_text(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if os.name != "nt":
        raise ValueError("clipboard endpoints currently require Windows")
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    CF_UNICODETEXT = 13; GMEM_MOVEABLE = 0x0002
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    raw = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
    if not handle:
        raise OSError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle); raise OSError("GlobalLock failed")
    try:
        ctypes.memmove(ptr, raw, len(raw))
    finally:
        kernel32.GlobalUnlock(handle)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        kernel32.GlobalFree(handle); raise ValueError("clipboard is busy")
    try:
        if not user32.EmptyClipboard():
            kernel32.GlobalFree(handle); raise OSError("EmptyClipboard failed")
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle); raise OSError("SetClipboardData failed")
        handle = None
    finally:
        user32.CloseClipboard()
    return {"ok": True, "characters": len(text)}


def wait_for_condition(data: dict[str, Any]) -> dict[str, Any]:
    if "until_pixel" not in data:
        try:
            seconds = float(data.get("seconds"))
        except (TypeError, ValueError) as exc:
            raise ValueError("wait requires seconds or until_pixel") from exc
        if not 0.0 <= seconds <= 30.0:
            raise ValueError("seconds must be between 0 and 30")
        time.sleep(seconds)
        return {"ok": True, "waited_seconds": seconds, "condition_met": True}
    cond = data.get("until_pixel")
    if not isinstance(cond, dict):
        raise ValueError("until_pixel must be an object")
    rgb = cond.get("rgb")
    if not isinstance(rgb, list) or len(rgb) != 3 or not all(isinstance(v, int) and 0 <= v <= 255 for v in rgb):
        raise ValueError("until_pixel.rgb must be [r,g,b] integers in 0..255")
    timeout = float(data.get("timeout", 5.0)); poll = float(data.get("poll_interval", 0.1)); tolerance = int(cond.get("tolerance", 0))
    if not 0.0 <= timeout <= 30.0: raise ValueError("timeout must be between 0 and 30")
    if not 0.05 <= poll <= 5.0: raise ValueError("poll_interval must be between 0.05 and 5")
    if not 0 <= tolerance <= 255: raise ValueError("tolerance must be between 0 and 255")
    start = time.monotonic(); last = None
    while True:
        last = pixel_color(cond.get("x"), cond.get("y"), normalized=bool(cond.get("normalized", False)))
        if all(abs(last["rgb"][i] - rgb[i]) <= tolerance for i in range(3)):
            return {"ok": True, "condition_met": True, "elapsed_seconds": round(time.monotonic()-start, 3), "pixel": last}
        if time.monotonic() - start >= timeout:
            return {"ok": True, "condition_met": False, "elapsed_seconds": round(time.monotonic()-start, 3), "pixel": last}
        time.sleep(poll)


def screenshot_as_json(
    *,
    show_cursor: bool = True,
    show_viewport: bool = True,
    use_scope: bool = True,
    region: dict[str, Any] | None = None,
    capture_provider: Any = DEFAULT_CAPTURE_PROVIDER,
) -> dict[str, Any]:
    scope_status = window_scope_status() if use_scope else {"active": False}
    live_scope = scope_status.get("window") if scope_status.get("active") and not scope_status.get("stale") else None
    requested_provider = normalize_capture_provider(capture_provider)

    if live_scope is not None:
        image, binding = capture_screenshot_with_info(
            show_cursor=show_cursor,
            show_viewport=show_viewport,
            use_scope=True,
            capture_provider=requested_provider,
        )
        image_region = {
            "x": int(live_scope["x"]), "y": int(live_scope["y"]),
            "width": int(live_scope["width"]), "height": int(live_scope["height"]),
        }
        if region is not None:
            image, image_region = crop_scoped_capture_region(image, live_scope, region)
        diagnostic = scope_capture_binding(live_scope, requested_provider=requested_provider)
        diagnostic.update(binding)
        if binding.get("provider") == "windows_graphics_capture":
            diagnostic["warning"] = (
                "This PNG is HWND-bound Windows.Graphics.Capture output. It is independent of ordinary "
                "top-level window occlusion; pointer input still passes the separate foreground + hit-test gate."
            )
        capture_binding = diagnostic
    else:
        image, binding = capture_screenshot_with_info(
            show_cursor=show_cursor,
            show_viewport=show_viewport,
            use_scope=False,
            capture_provider=requested_provider,
        )
        if region is None:
            image_region = virtual_desktop_bounds()
        else:
            screen_region = dict(region)
            image = crop_to_screen_region(
                image, screen_region.get("x"), screen_region.get("y"),
                screen_region.get("width"), screen_region.get("height"),
            )
            requested = {
                "x": int(round(float(screen_region.get("x")))),
                "y": int(round(float(screen_region.get("y")))),
                "width": int(round(float(screen_region.get("width")))),
                "height": int(round(float(screen_region.get("height")))),
            }
            desktop = virtual_desktop_bounds()
            ix1 = max(requested["x"], int(desktop["x"]))
            iy1 = max(requested["y"], int(desktop["y"]))
            ix2 = min(requested["x"] + requested["width"], int(desktop["x"]) + int(desktop["width"]))
            iy2 = min(requested["y"] + requested["height"], int(desktop["y"]) + int(desktop["height"]))
            image_region = {"x": ix1, "y": iy1, "width": max(0, ix2 - ix1), "height": max(0, iy2 - iy1)}
        capture_binding = binding

    buffer = io.BytesIO(); image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    mx, my = current_mouse()

    mouse_in_image: dict[str, Any] | None = None
    rx, ry = int(image_region["x"]), int(image_region["y"])
    rw, rh = int(image_region["width"]), int(image_region["height"])
    if rw > 0 and rh > 0 and rx <= mx < rx + rw and ry <= my < ry + rh:
        ix = int(round((mx - rx) * image.size[0] / max(1, rw)))
        iy = int(round((my - ry) * image.size[1] / max(1, rh)))
        ix = clamp(ix, 0, max(0, image.size[0] - 1))
        iy = clamp(iy, 0, max(0, image.size[1] - 1))
        mouse_in_image = {
            "x": ix, "y": iy, "coordinate_space": "png_pixels",
            "inside_image": True,
        }

    marker_rendered = bool(show_cursor and mouse_in_image is not None)
    return {
        "mime": "image/png", "width": image.size[0], "height": image.size[1], "bytes": len(raw),
        "base64": base64.b64encode(raw).decode("ascii"), "window_scope": scope_status,
        "capture_provider": capture_binding.get("provider"),
        "capture_binding": capture_binding,
        "image_region_windows_pixels": image_region,
        "mouse": pointer_coordinate_contract(mx, my, scope_window=live_scope),
        "mouse_in_image": mouse_in_image,
        "cursor_marker": {
            "rendered": marker_rendered,
            "hotspot": mouse_in_image if marker_rendered else None,
            "meaning": "White center of the glowing AGENT CURSOR crosshair is the exact click hotspot.",
        },
        "visual_guide": visual_guide_contract(screen_x=mx, screen_y=my, scope_status=scope_status),
    }


def pointer_coordinate_contract(screen_x: int, screen_y: int, *, scope_window: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canonical machine-readable interpretation of the visual cursor labels."""
    absolute = {"x": int(screen_x), "y": int(screen_y), "coordinate_space": "windows_virtual_desktop"}
    if scope_window is not None:
        inside = _point_in_rect(int(screen_x), int(screen_y), scope_window)
        action = {
            "x": int(screen_x) - int(scope_window["x"]),
            "y": int(screen_y) - int(scope_window["y"]),
            "coordinate_space": "window_scope",
            "inside_scope": bool(inside),
        }
        instruction = (
            "Use action.x/action.y AND action.coordinate_space with /v1/move, /v1/click, /v1/drag and dry_run while this scope is active. "
            "coordinate_space is mandatory for scoped pointer calls. ABS values are desktop reference coordinates only."
        )
    else:
        action = {"x": int(screen_x), "y": int(screen_y), "coordinate_space": "virtual_desktop"}
        instruction = "Use action.x/action.y directly with /v1/move, /v1/click, /v1/drag and dry_run."
    return {"absolute": absolute, "action": action, "instruction": instruction}


def visual_guide_contract(*, screen_x: int | None = None, screen_y: int | None = None, scope_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe screenshot/runtime annotations in language an AI agent can consume."""
    if screen_x is None or screen_y is None:
        screen_x, screen_y = current_mouse()
    if scope_status is None:
        scope_status = window_scope_status()
    scope_window = None
    if scope_status.get("active") and not scope_status.get("stale"):
        scope_window = scope_status.get("window")
    coords = pointer_coordinate_contract(int(screen_x), int(screen_y), scope_window=scope_window)
    return {
        "purpose": "These visual annotations are generated for the AI agent. They are not application UI.",
        "action_coordinates": coords,
        "annotations": {
            "glowing_crosshair": {
                "meaning": "Current mouse pointer. The white center dot is the exact click hotspot.",
                "agent_use": "Read the adjacent ACTION X/Y label before choosing a pointer action.",
            },
            "cursor_label": {
                "meaning": "AGENT CURSOR | ACTION X/Y are the coordinates accepted by pointer endpoints right now.",
                "scope_rule": "When a window scope is active, ACTION X/Y are scope-relative and pointer requests MUST include coordinate_space=window_scope. ABS X/Y remain Windows desktop reference pixels and require coordinate_space=virtual_desktop if intentionally used.",
            },
            "edge_guides": {
                "meaning": "ACTION X and ACTION Y projected to the active monitor/scope edge; a lightweight pixel ruler.",
            },
            "monitor_border": {
                "meaning": "Cyan glowing frame marks an AI-controlled monitor and its absolute Windows pixel bounds.",
            },
            "scope_border": {
                "meaning": "Process-scope frame marks the fail-closed window boundary. Pointer actions outside it are rejected.",
            },
            "viewport_overlay": {
                "meaning": "Configured visible RDP viewport is a safety boundary; outside areas are dimmed/blocked.",
            },
        },
        "important": [
            "Do not treat AGENT CURSOR / ACTION / ABS / AGENT MONITOR labels as application content.",
            "For pointer actions use ACTION X/Y together with ACTION coordinate_space, not a guessed coordinate from screenshot dimensions.",
            "If uncertain, call dry_run=true before a real click.",
        ],
    }


def observation_snapshot(*, include_image: bool = False, history_limit: int = 5, show_cursor: bool = True, show_viewport: bool = True, use_scope: bool = True, capture_provider: Any = DEFAULT_CAPTURE_PROVIDER) -> dict[str, Any]:
    """One current-state sidecar for an agent's visual observation loop."""
    x, y = current_mouse()
    scope_status = window_scope_status()
    scope_window = None
    if scope_status.get("active") and not scope_status.get("stale"):
        scope_window = scope_status.get("window")
    coordinate_state = pointer_coordinate_contract(x, y, scope_window=scope_window)
    monitor = monitor_for_point(x, y)
    try:
        under_pointer = window_at(x, y)
    except Exception:
        under_pointer = None
    try:
        focused = focused_window()
    except Exception:
        focused = None
    recent = get_action_history(history_limit)
    action = coordinate_state["action"]
    summary = [
        "The screenshot crosshair/halo is the current mouse pointer and its white center is the click hotspot.",
        f"Current actionable pointer coordinates: X={action['x']} Y={action['y']} in {action['coordinate_space']} space.",
        "Labels beginning AGENT/ACTION/ABS are bridge-generated coordinate guides, not text from the application.",
    ]
    if scope_window is not None:
        summary.append(
            f"Window scope is active for {scope_window.get('process_name') or scope_window.get('title')}; pointer calls with x/y MUST include coordinate_space. Use ACTION x/y with coordinate_space=window_scope, or explicit ABS x/y with coordinate_space=virtual_desktop."
        )
        summary.append(
            "Scoped clicks are target-bound: the scoped window must be foreground and visibly own the target point; client area is the safe default. Screenshot capture_provider=auto prefers HWND-bound Windows.Graphics.Capture when available and otherwise reports a desktop-composite fallback."
        )
    else:
        summary.append("No window scope is active; ACTION X/Y equal absolute Windows virtual-desktop pixels.")
    viewport = load_viewport()
    if viewport is not None:
        summary.append("An RDP viewport safety boundary is active; pointer actions outside it are rejected.")
    payload: dict[str, Any] = {
        "ok": True,
        "time": _now_iso(),
        "version": VERSION,
        "build_id": BUILD_ID,
        "read_this_first": summary,
        "visual_guide": visual_guide_contract(screen_x=x, screen_y=y, scope_status=scope_status),
        "pointer": coordinate_state,
        "monitor_under_pointer": monitor,
        "window_under_pointer": under_pointer,
        "focused_window": focused,
        "window_scope": scope_status,
        "viewport": viewport,
        "recent_actions": recent,
        "history_endpoint": f"/{API_VERSION}/history",
        "accessibility": {
            "endpoint": f"/{API_VERSION}/accessibility-tree",
            "backend": uia_backend_status(),
            "purpose": "Optional read-only structured UI metadata; call when window text/control structure is more useful than pixel-only inspection.",
        },
        "screenshot": {
            "endpoint": f"/{API_VERSION}/screenshot",
            "contains_agent_coordinate_guides": bool(show_cursor),
            "cursor_overlay": bool(show_cursor),
            "viewport_overlay": bool(show_viewport),
            "uses_window_scope": bool(use_scope),
            "capture_provider_requested": normalize_capture_provider(capture_provider),
            "capture_provider_status": capture_provider_status(),
            "capture_binding": ({
                **scope_capture_binding(scope_window, requested_provider=capture_provider),
                "requested_provider": normalize_capture_provider(capture_provider),
                "preferred_provider": (
                    "windows_graphics_capture"
                    if normalize_capture_provider(capture_provider) == "auto"
                    and windows_graphics_capture_backend_status().get("available")
                    else normalize_capture_provider(capture_provider)
                ),
            } if (use_scope and scope_window is not None) else {
                "active": False, "requested_provider": normalize_capture_provider(capture_provider),
                "capture_mode": "desktop_composite", "occlusion_independent": False
            }),
        },
        "recommended_next": "Use dry_run=true before a risky pointer action; after acting, call /v1/observe again or an act-and-screenshot endpoint.",
    }
    if include_image:
        image_info = screenshot_as_json(
            show_cursor=show_cursor, show_viewport=show_viewport, use_scope=use_scope,
            capture_provider=capture_provider,
        )
        # Keep the rich image object while also exposing the two cursor-location
        # fields at the stable observation-screenshot level.  r11 documented
        # these fields there; r12 accidentally buried them one level deeper
        # inside ``screenshot.image``.  Mirroring them preserves compatibility
        # without introducing a second source of truth.
        payload["screenshot"]["mouse_in_image"] = image_info.get("mouse_in_image")
        payload["screenshot"]["cursor_marker"] = image_info.get("cursor_marker")
        payload["screenshot"]["image"] = image_info
    return payload


# ---------------------------------------------------------------------------
# Windows runtime indication: click-through screen border + cursor halo,
# plus a physical-Escape-only stop hook.  The overlay is intentionally a
# separate process so the HTTP server remains independent from Tk's event loop.
# ---------------------------------------------------------------------------


def _windows_process_alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes

    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def run_runtime_overlay(parent_pid: int, stop_signal: str | None = None, hook_status_signal: str | None = None, parent_escape_active: bool = False) -> None:
    """Windows-only transparent click-through overlay used while server is active.

    The overlay deliberately re-reads virtual-desktop and monitor geometry while
    running. RDP reconnects, docking/undocking, MSTSC display changes and monitor
    hotplug can change Windows coordinates after the server has already started.
    Keeping a startup-only geometry snapshot caused the visual border/cursor
    overlay to become stale even while the computer-use core had newer geometry.
    """
    if os.name != "nt":
        return

    import ctypes
    import tkinter as tk

    user32 = ctypes.windll.user32
    transparent_key = "#010203"

    def request_parent_stop() -> None:
        if not stop_signal:
            return
        try:
            Path(stop_signal).write_text("physical-escape", encoding="ascii")
        except OSError:
            pass

    # Detached server processes can fail to attach WH_KEYBOARD_LL to the input
    # desktop while this visible overlay process is attached successfully. Run
    # the same injected-key-safe hook here as a fallback and signal the parent.
    overlay_escape_hook = WindowsPhysicalEscapeHook(
        request_parent_stop,
        injection_sentinel=keyboard_injection_sentinel_path(parent_pid),
    )
    overlay_escape_active = overlay_escape_hook.start()
    # Detached/RDP sessions sometimes reject WH_KEYBOARD_LL even though this
    # visible overlay is attached to the interactive desktop.  In that case,
    # fall back to polling the physical Escape key.  The bridge's keyboard
    # injection sentinel is sampled too, so agent-generated Esc does not stop
    # control.  This fallback cannot consume Esc from the foreground app, but it
    # restores the user's emergency-stop path instead of merely printing a warn.
    escape_poll_fallback = not overlay_escape_active and not parent_escape_active
    if hook_status_signal:
        try:
            Path(hook_status_signal).write_text(
                "1" if overlay_escape_active else ("poll" if escape_poll_fallback else "0"),
                encoding="ascii",
            )
        except OSError:
            pass

    initial_desktop = virtual_desktop_bounds()
    root = tk.Tk()
    root.title(RUNTIME_OVERLAY_TITLE)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=transparent_key)
    root.geometry(f"{initial_desktop['width']}x{initial_desktop['height']}+0+0")
    try:
        root.wm_attributes("-transparentcolor", transparent_key)
    except tk.TclError:
        pass

    canvas = tk.Canvas(
        root,
        width=initial_desktop["width"],
        height=initial_desktop["height"],
        bg=transparent_key,
        highlightthickness=0,
        bd=0,
    )
    canvas.pack(fill="both", expand=True)
    root.update_idletasks()

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    hwnd = root.winfo_id()
    parent_hwnd = user32.GetParent(hwnd)
    if parent_hwnd:
        hwnd = parent_hwnd
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd,
        GWL_EXSTYLE,
        style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
    )
    HWND_TOPMOST = -1
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    desktop = dict(initial_desktop)
    monitor_layout = list_monitors()
    geometry_signature: tuple[Any, ...] | None = None
    static_signature: tuple[Any, ...] | None = None
    static_ids: list[int] = []
    cursor_ids: list[int] = []

    def clear_items(ids: list[int]) -> None:
        while ids:
            canvas.delete(ids.pop())

    def monitor_signature(monitors: list[dict[str, Any]]) -> tuple[Any, ...]:
        return tuple(
            (
                int(m.get("x", 0)), int(m.get("y", 0)),
                int(m.get("width", 0)), int(m.get("height", 0)),
                bool(m.get("primary", False)), str(m.get("device") or ""),
            )
            for m in monitors
        )

    def refresh_geometry() -> None:
        nonlocal desktop, monitor_layout, geometry_signature, static_signature
        new_desktop = virtual_desktop_bounds()
        new_monitors = list_monitors()
        sig = (
            int(new_desktop["x"]), int(new_desktop["y"]),
            int(new_desktop["width"]), int(new_desktop["height"]),
            monitor_signature(new_monitors),
        )
        if sig == geometry_signature:
            return

        desktop = dict(new_desktop)
        monitor_layout = new_monitors
        geometry_signature = sig
        static_signature = None
        clear_items(static_ids)
        clear_items(cursor_ids)

        width = max(1, int(desktop["width"]))
        height = max(1, int(desktop["height"]))
        canvas.configure(width=width, height=height)
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            int(desktop["x"]), int(desktop["y"]), width, height,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )

    def to_canvas(abs_x: int, abs_y: int) -> tuple[int, int]:
        return int(abs_x) - int(desktop["x"]), int(abs_y) - int(desktop["y"])

    def add_text_box(x: int, y: int, text: str, *, anchor: str = "nw", fg: str = "#e9fbff") -> None:
        # Canvas has no rounded text background primitive. Two offset text
        # layers give a high-contrast halo that stays readable on arbitrary UI.
        static_ids.append(canvas.create_text(x + 1, y + 1, text=text, fill="#06121a", font=("Consolas", 9, "bold"), anchor=anchor, tags="runtime"))
        static_ids.append(canvas.create_text(x, y, text=text, fill=fg, font=("Consolas", 9, "bold"), anchor=anchor, tags="runtime"))

    def draw_runtime_rect(abs_x: int, abs_y: int, width_px: int, height_px: int, *, label: str | None = None, color: str = "#55c8ff") -> None:
        x1, y1 = to_canvas(abs_x, abs_y)
        x2 = x1 + int(width_px) - 1
        y2 = y1 + int(height_px) - 1
        screen_w, screen_h = int(desktop["width"]), int(desktop["height"])
        x1 = clamp(x1, 0, max(0, screen_w - 1)); y1 = clamp(y1, 0, max(0, screen_h - 1))
        x2 = clamp(x2, 0, max(0, screen_w - 1)); y2 = clamp(y2, 0, max(0, screen_h - 1))
        if x2 <= x1 or y2 <= y1:
            return
        # User feedback from the real r28 agent run: the previous nested
        # white→blue halo consumed too much screen space.  One thin semantic
        # boundary is enough; machine-readable coordinates live in observations.
        inset = 2
        if x2 - x1 > inset * 2 and y2 - y1 > inset * 2:
            static_ids.append(
                canvas.create_rectangle(
                    x1 + inset, y1 + inset, x2 - inset, y2 - inset,
                    outline=color, width=3, tags="runtime",
                )
            )
        if label:
            add_text_box(x1 + 18, y1 + 20, label)

    def current_scope_for_overlay() -> dict[str, Any] | None:
        try:
            return active_scope_window()
        except Exception:
            return None

    def draw_static() -> None:
        nonlocal static_signature
        vp = load_viewport()
        scope = current_scope_for_overlay()
        vp_sig = None if vp is None else (vp["x"], vp["y"], vp["width"], vp["height"])
        scope_sig = None if scope is None else (
            scope.get("hwnd"), scope.get("x"), scope.get("y"),
            scope.get("width"), scope.get("height"), scope.get("process_name"),
        )
        sig = (geometry_signature, vp_sig, scope_sig, overlay_escape_active, parent_escape_active, escape_poll_fallback)
        if sig == static_signature:
            return
        static_signature = sig
        clear_items(static_ids)

        if overlay_escape_active or parent_escape_active:
            stop_text = "PHYSICAL ESC = STOP"
        elif escape_poll_fallback:
            stop_text = "PHYSICAL ESC = STOP (POLL)"
        else:
            stop_text = "ESC UNAVAILABLE • /v1/stop"

        if vp is not None:
            draw_runtime_rect(
                int(vp["x"]), int(vp["y"]), int(vp["width"]), int(vp["height"]),
                label=f"AI CONTROL • VISIBLE RDP AREA | {stop_text}",
                color="#62ff9a",
            )
        else:
            if monitor_layout:
                for i, monitor in enumerate(monitor_layout, start=1):
                    mx, my = int(monitor["x"]), int(monitor["y"])
                    mw, mh = int(monitor["width"]), int(monitor["height"])
                    max_x, max_y = mx + mw - 1, my + mh - 1
                    primary = " PRIMARY" if monitor.get("primary") else ""
                    draw_runtime_rect(
                        mx, my, mw, mh,
                        label=f"AI CONTROL | MON {i}{primary} | AGENT ABS PIXELS X {mx}..{max_x}  Y {my}..{max_y} | {stop_text}",
                    )
            else:
                draw_runtime_rect(
                    int(desktop["x"]), int(desktop["y"]), int(desktop["width"]), int(desktop["height"]),
                    label=f"AI COMPUTER CONTROL ACTIVE | {stop_text}",
                )

        # A process/window scope is an additional fail-closed boundary and is
        # shown separately from the monitor/viewport runtime indicator.
        if scope is not None:
            draw_runtime_rect(
                int(scope["x"]), int(scope["y"]), int(scope["width"]), int(scope["height"]),
                label=f"PROCESS SCOPE | {scope.get('process_name') or scope.get('pid')}",
                color="#ffca55",
            )

    def draw_cursor() -> None:
        clear_items(cursor_ids)
        point = POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return
        abs_x, abs_y = int(point.x), int(point.y)
        x, y = to_canvas(abs_x, abs_y)
        screen_w, screen_h = int(desktop["width"]), int(desktop["height"])
        if not (0 <= x < screen_w and 0 <= y < screen_h):
            return

        # Keep the user-facing indicator intentionally subtle. Exact
        # coordinates and the large machine-readable marker remain available
        # in screenshot/observe output; the live desktop only needs a small
        # glow showing that agent control is active.
        for radius, color, width in [
            (17, "#12364a", 5),
            (12, "#2b83ad", 3),
            (8, "#8ee6ff", 2),
        ]:
            cursor_ids.append(
                canvas.create_oval(
                    x-radius, y-radius, x+radius, y+radius,
                    outline=color, width=width, tags="cursor"
                )
            )

    escape_was_down = False

    def poll_escape_fallback() -> None:
        nonlocal escape_was_down
        if not escape_poll_fallback:
            return
        VK_ESCAPE = 0x1B
        down = bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
        sentinel = keyboard_injection_sentinel_path(parent_pid)
        if sentinel.exists():
            # Track the injected key's physical state so removing the sentinel
            # while the key is still down cannot look like a fresh user press.
            escape_was_down = down
            return
        if down and not escape_was_down:
            request_parent_stop()
        escape_was_down = down

    def tick() -> None:
        if not _windows_process_alive(parent_pid):
            root.destroy()
            return
        refresh_geometry()
        draw_static()
        draw_cursor()
        poll_escape_fallback()
        root.after(33, tick)

    try:
        refresh_geometry()
        tick()
        root.mainloop()
    finally:
        overlay_escape_hook.stop()

def launch_runtime_overlay(parent_pid: int, stop_signal: Path, hook_status_signal: Path, parent_escape_active: bool) -> subprocess.Popen[Any] | None:
    if os.name != "nt":
        return None
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "_overlay",
        "--parent-pid", str(parent_pid),
        "--stop-signal", str(stop_signal),
        "--hook-status-signal", str(hook_status_signal),
        "--parent-escape-active", "1" if parent_escape_active else "0",
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "close_fds": True, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    try:
        return subprocess.Popen(cmd, **kwargs)
    except OSError:
        return None


class WindowsPhysicalEscapeHook:
    """Stop callback for a physically pressed Esc; injected Esc is ignored."""

    def __init__(self, on_escape: Any, *, injection_sentinel: str | os.PathLike[str] | None = None) -> None:
        self.on_escape = on_escape
        self.injection_sentinel = Path(injection_sentinel) if injection_sentinel else None
        self.ready = threading.Event()
        self.triggered = threading.Event()
        self.active = False
        self.thread_id: int | None = None
        self._hook = None
        self._proc = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, name="physical-esc-hook", daemon=True)
        self._thread.start()
        self.ready.wait(timeout=1.5)
        return self.active

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        WH_KEYBOARD_LL = 13
        WM_KEYDOWN = 0x0100
        WM_SYSKEYDOWN = 0x0104
        WM_QUIT = 0x0012
        VK_ESCAPE = 0x1B
        LLKHF_INJECTED = 0x10

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", wintypes.WPARAM),
            ]

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        LRESULT = wintypes.LPARAM
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        def callback(n_code: int, w_param: int, l_param: int) -> int:
            if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                injected = bool(info.flags & LLKHF_INJECTED)
                if (
                    info.vkCode == VK_ESCAPE
                    and not injected
                    and not BRIDGE_INJECTING_KEYBOARD.is_set()
                    and not (self.injection_sentinel is not None and self.injection_sentinel.exists())
                ):
                    if not self.triggered.is_set():
                        self.triggered.set()
                        threading.Thread(target=self.on_escape, name="esc-stop", daemon=True).start()
                    # Consume the physical Esc: its purpose here is stopping the bridge.
                    return 1
            return int(user32.CallNextHookEx(self._hook, n_code, w_param, l_param))

        self._proc = HOOKPROC(callback)
        self.thread_id = int(kernel32.GetCurrentThreadId())
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._proc,
            kernel32.GetModuleHandleW(None),
            0,
        )
        self.active = bool(self._hook)
        self.ready.set()
        if not self._hook:
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self.active = False

    def stop(self) -> None:
        if os.name != "nt" or self.thread_id is None:
            return
        import ctypes

        WM_QUIT = 0x0012
        ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=0.75)


RECORDING_FORMAT_VERSION = 8
DEFAULT_RECORDING_SETTLE_MS = 160
DEFAULT_RECORDING_SCREENSHOTS = True
DEFAULT_RECORDING_CAPTURE_SCOPE = "monitor"
RECORDING_CAPTURE_SCOPES = {"monitor", "virtual_desktop"}
RECORDING_QUEUE_LIMIT = 4096


def _event_time_iso(epoch_seconds: float | None = None) -> str:
    stamp = time.time() if epoch_seconds is None else float(epoch_seconds)
    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="milliseconds")


def _bounded_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _compact_window(window: dict[str, Any] | None) -> dict[str, Any] | None:
    if not window:
        return None
    keys = ("hwnd", "pid", "process_name", "class_name", "title", "x", "y", "width", "height", "minimized")
    return {key: window.get(key) for key in keys if window.get(key) is not None}


def _uia_node_summary(control: Any) -> dict[str, Any]:
    is_password = _uia_safe_attr(control, "IsPassword", None)
    name = "<password-control>" if is_password is not False else _bounded_text(_uia_safe_attr(control, "Name", ""))
    return {
        "name": name,
        "control_type": _bounded_text(_uia_safe_attr(control, "ControlTypeName", ""), 80),
        "automation_id": _bounded_text(_uia_safe_attr(control, "AutomationId", ""), 160),
        "class_name": _bounded_text(_uia_safe_attr(control, "ClassName", ""), 160),
        "framework_id": _bounded_text(_uia_safe_attr(control, "FrameworkId", ""), 80),
        "bounding_rect": _uia_rect_dict(_uia_safe_attr(control, "BoundingRectangle")),
        "enabled": bool(_uia_safe_attr(control, "IsEnabled", False)),
        "offscreen": bool(_uia_safe_attr(control, "IsOffscreen", False)),
        "keyboard_focus": bool(_uia_safe_attr(control, "HasKeyboardFocus", False)),
        "is_password": is_password,
        "native_window_handle": int(_uia_safe_attr(control, "NativeWindowHandle", 0) or 0),
    }


def _uia_stack_from_control(control: Any, *, max_levels: int = 24) -> list[dict[str, Any]]:
    stack: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    current = control
    while current is not None and len(stack) < max_levels:
        node = _uia_node_summary(current)
        signature = (
            node.get("native_window_handle"), node.get("automation_id"), node.get("class_name"),
            node.get("name"), json.dumps(node.get("bounding_rect"), sort_keys=True),
        )
        if signature in seen:
            break
        seen.add(signature)
        stack.append(node)
        try:
            current = current.GetParentControl()
        except Exception:
            break
    return stack


def uia_context_at_point(x: int, y: int) -> dict[str, Any]:
    auto, backend = _load_uia_backend()
    result: dict[str, Any] = {"available": bool(auto is not None), "backend": backend, "stack": []}
    if auto is None:
        return result
    try:
        control = auto.ControlFromPoint(int(x), int(y))
    except Exception as exc:
        result["error"] = f"ControlFromPoint failed: {exc}"
        return result
    if control is None:
        return result
    result["stack"] = _uia_stack_from_control(control)
    result["target"] = result["stack"][0] if result["stack"] else None
    return result


def uia_context_at_focus() -> dict[str, Any]:
    auto, backend = _load_uia_backend()
    result: dict[str, Any] = {"available": bool(auto is not None), "backend": backend, "stack": []}
    if auto is None:
        return result
    try:
        control = auto.GetFocusedControl()
    except Exception as exc:
        result["error"] = f"GetFocusedControl failed: {exc}"
        return result
    if control is None:
        return result
    result["stack"] = _uia_stack_from_control(control)
    result["target"] = result["stack"][0] if result["stack"] else None
    return result



def _event_window_snapshot(*, x: int | None = None, y: int | None = None, focused: bool = False) -> dict[str, Any] | None:
    """Capture cheap Win32 identity at input-hook time.

    This intentionally avoids UIA and process-image lookup. The snapshot is
    immutable evidence of which top-level HWND/PID/monitor owned the physical
    event; slower enrichment may happen later without changing that fact.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.restype = wintypes.HWND

    if focused:
        raw = user32.GetForegroundWindow()
    else:
        if x is None or y is None:
            return None
        raw = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not raw:
        return None

    hwnd = int(raw)
    root_raw = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    root = int(root_raw) if root_raw else hwnd
    if not user32.IsWindow(root):
        return None

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    rect = wintypes.RECT()
    if not user32.GetWindowRect(root, ctypes.byref(rect)):
        return None

    title_len = max(0, int(user32.GetWindowTextLengthW(root)))
    title_buf = ctypes.create_unicode_buffer(min(2048, title_len + 1) or 1)
    user32.GetWindowTextW(root, title_buf, len(title_buf))
    class_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(root, class_buf, len(class_buf))

    if x is None or y is None:
        anchor_x = int(rect.left + max(0, rect.right - rect.left) // 2)
        anchor_y = int(rect.top + max(0, rect.bottom - rect.top) // 2)
    else:
        anchor_x, anchor_y = int(x), int(y)
    monitor = monitor_for_point(anchor_x, anchor_y)
    foreground_raw = user32.GetForegroundWindow()
    foreground_hwnd = int(foreground_raw) if foreground_raw else 0
    return {
        "hwnd": root,
        "pid": int(pid.value),
        "title": title_buf.value,
        "class_name": class_buf.value,
        "x": int(rect.left),
        "y": int(rect.top),
        "width": max(0, int(rect.right - rect.left)),
        "height": max(0, int(rect.bottom - rect.top)),
        "focused": bool(foreground_hwnd and root == foreground_hwnd),
        "visible": bool(user32.IsWindowVisible(root)),
        "minimized": bool(user32.IsIconic(root)),
        "monitor": ({
            k: monitor.get(k)
            for k in ("index", "device", "x", "y", "width", "height", "primary")
        } if monitor else None),
    }


def _event_context_key(context: dict[str, Any] | None) -> tuple[int, int]:
    if not context:
        return (0, 0)
    try:
        return (int(context.get("hwnd") or 0), int(context.get("pid") or 0))
    except Exception:
        return (0, 0)


def _recording_double_click_metrics() -> tuple[float, int, int]:
    if os.name != "nt":
        return (0.50, 4, 4)
    import ctypes
    user32 = ctypes.windll.user32
    seconds = max(0.10, min(1.50, float(user32.GetDoubleClickTime()) / 1000.0))
    # SM_CXDOUBLECLK / SM_CYDOUBLECLK describe the accepted rectangle.
    cx = max(2, int(user32.GetSystemMetrics(36)))
    cy = max(2, int(user32.GetSystemMetrics(37)))
    return (seconds, cx, cy)


def _recording_key_text(vk: int, scan_code: int, modifiers: set[str] | list[str] | tuple[str, ...]) -> str | None:
    """Translate one physical key with the foreground Windows keyboard layout.

    This is passive observation only. The helper never reads an application's
    value property and does not inject input. Ctrl/Alt/Win shortcuts are not
    treated as text; Ctrl+Alt is kept because Windows commonly represents AltGr
    that way on international layouts.
    """
    if os.name != "nt":
        return None
    mods = {str(v).lower() for v in modifiers}
    if "win" in mods:
        return None
    if ("ctrl" in mods) != ("alt" in mods):
        return None

    import ctypes

    user32 = ctypes.windll.user32
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    state = (ctypes.c_ubyte * 256)()
    for key in (0x14, 0x90, 0x91):  # CapsLock, NumLock, ScrollLock
        try:
            if int(user32.GetKeyState(key)) & 1:
                state[key] = 1
        except Exception:
            pass
    if "shift" in mods:
        state[0x10] = 0x80
    if "ctrl" in mods:
        state[0x11] = 0x80
    if "alt" in mods:
        state[0x12] = 0x80

    try:
        foreground = user32.GetForegroundWindow()
        thread_id = int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        layout = user32.GetKeyboardLayout(thread_id)
        buf = ctypes.create_unicode_buffer(8)
        # 0x4 asks modern Windows not to mutate the keyboard/dead-key state.
        count = int(user32.ToUnicodeEx(
            int(vk), int(scan_code), state, buf, len(buf), 0x4, layout
        ))
    except Exception:
        return None
    if count <= 0:
        return None
    value = buf.value[:count]
    value = "".join(ch for ch in value if ch == " " or ch.isprintable())
    return value or None


def _recording_shortcut_name(vk: int, modifiers: set[str] | list[str] | tuple[str, ...]) -> str | None:
    present = {str(v).lower() for v in modifiers}
    mods = [m for m in ("ctrl", "alt", "shift", "win") if m in present]
    if not mods:
        return None
    key = None
    if 0x41 <= int(vk) <= 0x5A:
        key = chr(int(vk)).lower()
    elif 0x30 <= int(vk) <= 0x39:
        key = chr(int(vk))
    if not key:
        return None
    return "+".join(mods + [key])


class WindowsUserInputHook:
    """Passive low-level mouse/keyboard observer for human demonstration recording.

    The hook never injects input. Injected events are ignored so agent-generated
    actions do not contaminate a human recording session.
    """

    def __init__(
        self,
        on_event: Any,
        *,
        injection_sentinel: str | os.PathLike[str] | None = None,
        on_control_stop: Any | None = None,
        capture_text: bool = True,
    ) -> None:
        self.on_event = on_event
        self.injection_sentinel = Path(injection_sentinel) if injection_sentinel else None
        self.on_control_stop = on_control_stop
        self.capture_text = bool(capture_text)
        self.ready = threading.Event()
        self.active = False
        self.thread_id: int | None = None
        self._thread: threading.Thread | None = None
        self._mouse_hook = None
        self._keyboard_hook = None
        self._mouse_proc = None
        self._keyboard_proc = None
        self._modifiers: set[str] = set()
        self._control_stop_active = False

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, name="user-recording-input-hook", daemon=True)
        self._thread.start()
        self.ready.wait(timeout=1.5)
        return self.active

    def _safe_emit(self, event: dict[str, Any]) -> None:
        try:
            self.on_event(event)
        except Exception:
            pass

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        WH_KEYBOARD_LL = 13
        WH_MOUSE_LL = 14
        WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
        WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
        WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
        WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
        WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
        WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
        LLKHF_INJECTED = 0x10
        LLMHF_INJECTED = 0x01

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", wintypes.WPARAM),
            ]

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", wintypes.WPARAM),
            ]

        modifier_names = {0x10: "shift", 0xA0: "shift", 0xA1: "shift", 0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl", 0x12: "alt", 0xA4: "alt", 0xA5: "alt", 0x5B: "win", 0x5C: "win"}
        special_names = {0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "escape", 0x21: "page_up", 0x22: "page_down", 0x23: "end", 0x24: "home", 0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down", 0x2D: "insert", 0x2E: "delete"}

        def key_category(vk: int) -> tuple[str, str | None]:
            if vk in modifier_names:
                return "modifier", modifier_names[vk]
            if vk in special_names:
                name = special_names[vk]
                if name in {"left", "right", "up", "down", "home", "end", "page_up", "page_down", "tab"}:
                    return "navigation", name
                if name in {"backspace", "delete", "insert"}:
                    return "editing", name
                return "special", name
            if 0x70 <= vk <= 0x87:
                return "function", f"f{vk - 0x6F}"
            if vk == 0x20 or 0x30 <= vk <= 0x5A or 0x60 <= vk <= 0x6F or 0xBA <= vk <= 0xE2:
                return "text", None
            return "other", None

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        LRESULT = wintypes.LPARAM
        HHOOK = wintypes.HANDLE
        HINSTANCE = wintypes.HANDLE
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = HHOOK
        user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = HINSTANCE

        def injected_keyboard(info: Any) -> bool:
            return bool(info.flags & LLKHF_INJECTED) or BRIDGE_INJECTING_KEYBOARD.is_set() or bool(self.injection_sentinel and self.injection_sentinel.exists())

        def keyboard_callback(n_code: int, w_param: int, l_param: int) -> int:
            if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not injected_keyboard(info):
                    category, safe_name = key_category(int(info.vkCode))
                    if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if category == "modifier" and safe_name:
                            # Modifier state is carried on the actual key event; a
                            # bare Ctrl/Shift press is not a meaningful recorder
                            # step on its own. Keeping it out also lets the UI's
                            # control hotkey remain completely out of the trace.
                            self._modifiers.add(safe_name)
                            return int(user32.CallNextHookEx(self._keyboard_hook, n_code, w_param, l_param))

                        is_control_stop = (
                            self.on_control_stop is not None
                            and int(info.vkCode) == 0x7B  # F12
                            and {"ctrl", "shift"}.issubset(self._modifiers)
                        )
                        if is_control_stop:
                            if not self._control_stop_active:
                                self._control_stop_active = True
                                try:
                                    self.on_control_stop()
                                except Exception:
                                    pass
                            # This chord belongs to the recorder lifecycle, not
                            # the demonstrated app. Swallow F12 so it cannot
                            # trigger an application command and never emit it.
                            return 1

                        typed_text = (
                            _recording_key_text(int(info.vkCode), int(info.scanCode), self._modifiers)
                            if self.capture_text and category == "text"
                            else None
                        )
                        shortcut = None if typed_text else _recording_shortcut_name(int(info.vkCode), self._modifiers)
                        self._safe_emit({
                            "source": "keyboard", "phase": "down", "category": category,
                            "safe_key": safe_name, "modifiers": sorted(self._modifiers),
                            "typed_text": typed_text, "shortcut": shortcut,
                            "epoch": time.time(), "monotonic": time.monotonic(),
                            "event_context": _event_window_snapshot(focused=True),
                        })
                    elif category == "modifier" and safe_name:
                        self._modifiers.discard(safe_name)
                    elif int(info.vkCode) == 0x7B and self._control_stop_active:
                        self._control_stop_active = False
                        return 1
            return int(user32.CallNextHookEx(self._keyboard_hook, n_code, w_param, l_param))

        mouse_messages = {
            WM_LBUTTONDOWN: ("down", "left"), WM_LBUTTONUP: ("up", "left"),
            WM_RBUTTONDOWN: ("down", "right"), WM_RBUTTONUP: ("up", "right"),
            WM_MBUTTONDOWN: ("down", "middle"), WM_MBUTTONUP: ("up", "middle"),
        }

        def mouse_callback(n_code: int, w_param: int, l_param: int) -> int:
            if n_code >= 0 and w_param in (*mouse_messages, WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if not bool(info.flags & LLMHF_INJECTED):
                    px, py = int(info.pt.x), int(info.pt.y)
                    base = {
                        "source": "mouse", "x": px, "y": py,
                        "epoch": time.time(), "monotonic": time.monotonic(),
                        "event_context": _event_window_snapshot(x=px, y=py),
                    }
                    if w_param in mouse_messages:
                        phase, button = mouse_messages[w_param]
                        self._safe_emit({**base, "phase": phase, "button": button})
                    elif w_param in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                        high_word = (int(info.mouseData) >> 16) & 0xFFFF
                        delta = ctypes.c_short(high_word).value
                        self._safe_emit({**base, "phase": "wheel", "delta": int(delta), "horizontal": w_param == WM_MOUSEHWHEEL})
            return int(user32.CallNextHookEx(self._mouse_hook, n_code, w_param, l_param))

        self._keyboard_proc = HOOKPROC(keyboard_callback)
        self._mouse_proc = HOOKPROC(mouse_callback)
        self.thread_id = int(kernel32.GetCurrentThreadId())
        module = kernel32.GetModuleHandleW(None)
        self._keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._keyboard_proc, module, 0)
        self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, module, 0)
        self.active = bool(self._keyboard_hook and self._mouse_hook)
        self.ready.set()
        if not self.active:
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
            if self._mouse_hook:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            self._keyboard_hook = self._mouse_hook = None
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._keyboard_hook:
            user32.UnhookWindowsHookEx(self._keyboard_hook)
        if self._mouse_hook:
            user32.UnhookWindowsHookEx(self._mouse_hook)
        self._keyboard_hook = self._mouse_hook = None
        self.active = False

    def stop(self) -> None:
        if os.name != "nt" or self.thread_id is None:
            return
        import ctypes
        ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def default_recording_root() -> Path:
    documents = Path.home() / "Documents"
    base = documents if documents.exists() else Path.home()
    return base / "Agent Computer Bridge" / "Recordings"


def normalize_recording_capture_scope(value: Any) -> str:
    scope = str(value or DEFAULT_RECORDING_CAPTURE_SCOPE).strip().lower()
    aliases = {"desktop": "virtual_desktop", "virtual": "virtual_desktop", "screen": "monitor"}
    scope = aliases.get(scope, scope)
    if scope not in RECORDING_CAPTURE_SCOPES:
        raise ValueError(
            f"unknown recording capture scope {value!r}; expected one of {', '.join(sorted(RECORDING_CAPTURE_SCOPES))}"
        )
    return scope


def _top_level_window_handle(hwnd: int | None) -> int:
    """Resolve a Tk/child HWND to its real top-level root on Windows."""
    if os.name != "nt" or not hwnd:
        return int(hwnd or 0)
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetAncestor.restype = wintypes.HWND
    GA_ROOT = 2
    root = user32.GetAncestor(int(hwnd), GA_ROOT)
    return int(root) if root else int(hwnd)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _screen_rect_to_image(
    rect: dict[str, Any],
    image: Image.Image,
    *,
    screen_region: dict[str, Any] | None = None,
) -> tuple[int, int, int, int] | None:
    desktop = screen_region or virtual_desktop_bounds()
    try:
        x, y, w, h = (int(rect[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    sx = image.size[0] / max(1, int(desktop["width"]))
    sy = image.size[1] / max(1, int(desktop["height"]))
    x1 = int(round((x - int(desktop["x"])) * sx))
    y1 = int(round((y - int(desktop["y"])) * sy))
    x2 = int(round((x + w - int(desktop["x"])) * sx))
    y2 = int(round((y + h - int(desktop["y"])) * sy))
    mapped = (
        clamp(x1, 0, max(0, image.size[0] - 1)), clamp(y1, 0, max(0, image.size[1] - 1)),
        clamp(x2, 0, max(0, image.size[0] - 1)), clamp(y2, 0, max(0, image.size[1] - 1)),
    )
    if mapped[2] <= mapped[0] or mapped[3] <= mapped[1]:
        return None
    return mapped


def _screen_point_to_image(
    x: int,
    y: int,
    image: Image.Image,
    *,
    screen_region: dict[str, Any] | None = None,
) -> tuple[int, int]:
    region = screen_region or virtual_desktop_bounds()
    sx = image.size[0] / max(1, int(region["width"]))
    sy = image.size[1] / max(1, int(region["height"]))
    ix = int(round((int(x) - int(region["x"])) * sx))
    iy = int(round((int(y) - int(region["y"])) * sy))
    return (
        clamp(ix, 0, max(0, image.size[0] - 1)),
        clamp(iy, 0, max(0, image.size[1] - 1)),
    )


def _capture_recording_frame(
    path: Path,
    *,
    point: dict[str, int] | None,
    target_rect: dict[str, Any] | None,
    window: dict[str, Any] | None = None,
    capture_scope: str = DEFAULT_RECORDING_CAPTURE_SCOPE,
    buffered_frame: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capture_scope = normalize_recording_capture_scope(capture_scope)
    if buffered_frame:
        with Image.open(io.BytesIO(buffered_frame["jpeg"])) as saved:
            image = saved.convert("RGB")
        binding = dict(buffered_frame["binding"])
        started = buffered_frame["started_epoch"]
        completed = buffered_frame["completed_epoch"]
    else:
        started = time.time()
        image, binding = capture_screenshot_with_info(
            show_cursor=False, show_viewport=False, use_scope=False, capture_provider="desktop_composite"
        )
        completed = time.time()
    source_region = binding.get("screen_mapping") or virtual_desktop_bounds()
    image_region = dict(source_region)

    if capture_scope == "monitor":
        anchor_x: int | None = None
        anchor_y: int | None = None
        if point is not None:
            anchor_x = int(point["x"])
            anchor_y = int(point["y"])
        elif target_rect and all(target_rect.get(k) is not None for k in ("x", "y", "width", "height")):
            anchor_x = int(target_rect["x"]) + max(0, int(target_rect["width"]) // 2)
            anchor_y = int(target_rect["y"]) + max(0, int(target_rect["height"]) // 2)
        elif window and all(window.get(k) is not None for k in ("x", "y", "width", "height")):
            anchor_x = int(window["x"]) + max(0, int(window["width"]) // 2)
            anchor_y = int(window["y"]) + max(0, int(window["height"]) // 2)
        else:
            anchor_x, anchor_y = current_mouse()

        monitor = monitor_for_point(anchor_x, anchor_y, buffered_frame.get("monitors") if buffered_frame else None)
        if monitor is not None:
            sx = image.size[0] / max(1, int(source_region["width"]))
            sy = image.size[1] / max(1, int(source_region["height"]))
            crop_rect = (
                clamp(int(round((int(monitor["x"]) - int(source_region["x"])) * sx)), 0, image.size[0]),
                clamp(int(round((int(monitor["y"]) - int(source_region["y"])) * sy)), 0, image.size[1]),
                clamp(int(round((int(monitor["x"]) + int(monitor["width"]) - int(source_region["x"])) * sx)), 0, image.size[0]),
                clamp(int(round((int(monitor["y"]) + int(monitor["height"]) - int(source_region["y"])) * sy)), 0, image.size[1]),
            )
            if crop_rect[2] > crop_rect[0] and crop_rect[3] > crop_rect[1]:
                image = image.crop(crop_rect)
                image_region = {k: int(monitor[k]) for k in ("x", "y", "width", "height")}

    # Keep human demonstration screenshots visually quiet. Exact UIA target
    # geometry remains available in steps.json, so drawing a second rectangle
    # around the target only obscures the UI. The only visual annotation is the
    # compact click glow requested for the user-facing recorder.
    draw = ImageDraw.Draw(image)
    if point:
        ix, iy = _screen_point_to_image(
            int(point["x"]), int(point["y"]), image, screen_region=image_region
        )
        # Human-readable recordings use a compact glow instead of a large
        # crosshair so the marker does not cover the demonstrated UI. Exact
        # coordinates remain in steps.json.
        radius = max(6, min(12, int(round(min(image.size) / 180))))
        # Composite only the glow tile; full-desktop RGBA blur on each step
        # scales linearly with all monitor pixels rather than marker size.
        pad = radius + 20
        left, top = max(0, ix - pad), max(0, iy - pad)
        right, bottom = min(image.width, ix + pad + 1), min(image.height, iy + pad + 1)
        if right > left and bottom > top:
            glow = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            cx, cy = ix - left, iy - top
            gd.ellipse((cx-radius-5, cy-radius-5, cx+radius+5, cy+radius+5),
                       outline=(255, 80, 72, 190), width=6)
            glow = glow.filter(ImageFilter.GaussianBlur(radius=5))
            tile = Image.alpha_composite(image.crop((left, top, right, bottom)).convert("RGBA"), glow)
            image.paste(tile.convert("RGB"), (left, top))
        draw = ImageDraw.Draw(image)
        # Keep only a tiny hotspot dot over the soft halo. The previous hard
        # ring was still visually louder than the user's requested Codex-like glow.
        dot = max(1, int(round(min(image.size) / 900)))
        draw.ellipse((ix-dot, iy-dot, ix+dot, iy+dot), fill="#fff2ef")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=88, optimize=True)
    binding = dict(binding)
    binding["recording_scope"] = capture_scope
    binding["screen_mapping"] = dict(image_region)
    binding["frame_size"] = {"width": int(image.size[0]), "height": int(image.size[1])}
    binding["frame_timing"] = {"started_epoch": started, "completed_epoch": completed,
                               "source": "buffered" if buffered_frame else "live_fallback"}
    if buffered_frame:
        binding["frame_timing"]["selection"] = buffered_frame["selection"]
    if capture_scope == "monitor":
        binding["capture_mode"] = "monitor_crop_from_desktop_composite"
    return binding


def uia_text_capture_safe(context: dict[str, Any] | None) -> bool:
    """Unknown password metadata is not evidence of a non-password field."""
    if not isinstance(context, dict) or context.get("stale") or context.get("available") is False:
        return False
    target = context.get("target")
    return (isinstance(target, dict) and target.get("is_password") is False
            and all(node.get("is_password") is False for node in context.get("stack", []) if isinstance(node, dict)))


def recording_intent(goal: str = "", prerequisites: str = "", success_criteria: str = "") -> dict[str, Any]:
    values = {"goal": goal, "prerequisites": prerequisites, "success_criteria": success_criteria}
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError(f"{key} must be a string of at most 4000 characters")
    return {"source": "user_supplied", **{key: value.strip() for key, value in values.items()}}


def operational_evidence_availability(operations: list[dict[str, Any]], *,
                                      source: str) -> dict[str, Any]:
    """Compare available diagnostic signals, not usefulness or task success.

    Human context is a preceding step's observation; agent context is a
    controller-bound pre-action observation. The two differ in temporal proof.
    """
    if source not in {"human", "agent"}:
        raise ValueError("unknown evidence provenance")
    totals = {
        "operations": len(operations), "with_semantic_target_candidate": 0,
        "with_prior_visual_context": 0, "with_post_action_image_reference": 0,
        "with_visual_pair_reference": 0, "with_unresolved_target": 0,
        "with_uncertain_remote_focus": 0,
        "independently_verified_effects": 0,
    }
    for operation in operations:
        before = (operation.get("before") if source == "agent"
                  else operation.get("prior_step_context")) or {}
        after = (operation.get("after") if source == "agent"
                 else operation.get("observed_after")) or {}
        has_before = bool(before.get("screenshot"))
        has_after = bool(after.get("screenshot"))
        has_target = bool(operation.get("target")) if source == "human" else bool(
            (operation.get("request") or {}).get("element_index") is not None)
        totals["with_semantic_target_candidate"] += int(has_target)
        totals["with_prior_visual_context"] += int(has_before)
        totals["with_post_action_image_reference"] += int(has_after)
        totals["with_visual_pair_reference"] += int(has_before and has_after)
        totals["with_unresolved_target"] += int(not has_target)
        totals["with_uncertain_remote_focus"] += int(
            "remote_control_focus_not_accessible_to_local_uia"
            in (operation.get("uncertainties") or []))
        totals["independently_verified_effects"] += int(
            operation.get("effect_verified") is True)
    return {
        "source": "human_physical_events" if source == "human" else "agent_dispatched_input",
        "signals": totals,
        "prior_visual_context": ("previous_observed_step_not_event_bound" if source == "human"
                                 else "observed_before_dispatch_not_remote_focus_proof"),
        "interpretation": ("Reference presence is not screenshot integrity, correct "
                           "recipient identification, causal attribution, or success. "
                           "Use detailed timing and coverage for decisions."),
    }


class AgentJournalManager:
    """Durable evidence for canonical agent actions; never a replay/control owner."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._directory: Path | None = None
        self._controller_id: str | None = None
        self._session: dict[str, Any] | None = None
        self._sequence = 0
        self._last_observation: int | None = None
        self._capture_text = False
        self._capture_screenshots = True
        self._auto_observe_after_action = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"ok": True, "active": self._directory is not None,
                    "session_dir": str(self._directory) if self._directory else None,
                    "event_count": self._sequence,
                    "auto_observe_after_action": self._auto_observe_after_action}

    def post_action_capture_options(self, controller_id: Any) -> dict[str, bool] | None:
        with self._lock:
            if (self._directory is None or controller_id != self._controller_id
                    or not self._auto_observe_after_action):
                return None
            return {"include_image": self._capture_screenshots}

    def record_observation_failure(self, controller_id: Any, error: Exception) -> None:
        with self._lock:
            if self._directory is not None and controller_id == self._controller_id:
                self._append("observation_failed", {
                    "source": "automatic_after_action",
                    "error_type": type(error).__name__,
                    "reason": "post-action observation unavailable; effect not verified"})

    def _require_owner(self, controller_id: Any) -> None:
        if self._directory is None:
            raise ValueError("no agent journal is active")
        if controller_id != self._controller_id:
            raise ControllerLeaseError("journal belongs to a different controller", code="CONTROLLER_BUSY")

    @staticmethod
    def _safe_payload(value: Any, *, allow_text: bool = False) -> Any:
        if isinstance(value, dict):
            return {key: (f"<{len(item)} chars>" if key == "text" and isinstance(item, str) and not allow_text
                          else AgentJournalManager._safe_payload(item, allow_text=allow_text))
                    for key, item in value.items() if key not in {"controller_id", "controller_binding", "base64"}}
        if isinstance(value, (list, tuple)):
            return [AgentJournalManager._safe_payload(item, allow_text=allow_text) for item in value]
        return value

    def _append(self, kind: str, data: dict[str, Any]) -> int:
        assert self._directory is not None
        sequence = self._sequence + 1
        event = {"event": sequence, "time": _event_time_iso(), "kind": kind, **data}
        # Flush before returning, including before native dispatch. An unfinished
        # action_started event is delivery-uncertain after a process crash.
        with (self._directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._sequence = sequence
        return sequence

    def start(self, controller_id: str, *, output_dir: str | None = None, goal: str = "",
              prerequisites: str = "", success_criteria: str = "", capture_text: bool = False,
              capture_screenshots: bool = True,
              auto_observe_after_action: bool = False) -> dict[str, Any]:
        intent = recording_intent(goal, prerequisites, success_criteria)
        if not all(isinstance(v, bool) for v in
                   (capture_text, capture_screenshots, auto_observe_after_action)):
            raise ValueError("journal capture options must be booleans")
        if output_dir is not None and not isinstance(output_dir, str):
            raise ValueError("output_dir must be a string")
        with self._lock:
            if self._directory is not None:
                raise ValueError("an agent journal is already active")
            root = Path(output_dir).expanduser().resolve() if output_dir else default_recording_root().parent / "Journals"
            directory = root / ("Agent_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f"))
            directory.mkdir(parents=True, exist_ok=False)
            session = {"format_version": 1, "kind": "agent_execution_journal", "status": "recording",
                       "started_at": _event_time_iso(), "bridge": {"version": VERSION, "build_id": BUILD_ID},
                       "intent": intent, "capture_text": capture_text,
                       "capture_screenshots": capture_screenshots,
                       "auto_observe_after_action": auto_observe_after_action,
                       "provenance": "agent_dispatched_actions_not_physical_human_input",
                       "resume_guidance": "Read events and checkpoint, acquire a new controller and fresh observation; never replay unfinished actions blindly."}
            _atomic_write_json(directory / "session.json", session)
            (directory / "README.md").write_text(
                "# Agent execution journal\n\nRead session.json and events.jsonl, or use journal-read / computer_journal_read. "
                "Checkpoints are agent-supplied notes, not automatic proof of success. An action_started without "
                "action_finished may already have been dispatched. Re-observe before deciding what remains to do. "
                "Saved state IDs and coordinates are historical evidence, never fresh action targets.\n", encoding="utf-8")
            self._directory, self._controller_id, self._session = directory, controller_id, session
            self._sequence, self._last_observation = 0, None
            self._capture_text, self._capture_screenshots = capture_text, capture_screenshots
            self._auto_observe_after_action = auto_observe_after_action
            try:
                self._append("journal_started", {"intent": intent})
            except OSError:
                self._directory, self._controller_id, self._session = None, None, None
                self._auto_observe_after_action = False
                raise
            return self.status()

    def observe(self, state: dict[str, Any], controller_id: Any, *,
                source: str = "explicit") -> None:
        if source not in {"explicit", "automatic_after_action"}:
            raise ValueError("invalid observation source")
        with self._lock:
            if self._directory is None or controller_id != self._controller_id:
                return
            safe = self._safe_payload(state)
            raster = state.get("raster") or {}
            if self._capture_screenshots and raster.get("base64"):
                name = f"observation_{self._sequence + 1:06d}.png"
                (self._directory / name).write_bytes(base64.b64decode(raster["base64"]))
                safe["screenshot"] = name
            self._last_observation = self._append(
                "observation", {"state": safe, "source": source})

    def begin_action(self, action: str, request: dict[str, Any], controller_id: Any) -> int | None:
        with self._lock:
            if self._directory is None or controller_id != self._controller_id:
                return None
            allow_text = False
            if self._capture_text and "text" in request:
                focus = uia_context_at_focus()
                allow_text = uia_text_capture_safe(focus)
                if _surface_context(focused_window()).get("remote"):
                    allow_text = False
            safe = self._safe_payload(request, allow_text=allow_text)
            return self._append("action_started", {"action": action, "request": safe,
                                "before_observation": self._last_observation,
                                "text_redacted": "text" in request and not allow_text,
                                "retry": "reobserve"})

    def end_action(self, event: int | None, *, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        if event is None:
            return
        with self._lock:
            payload = {"action_event": event, "result": self._safe_payload(result)}
            if error is not None:
                payload["error"] = {"type": type(error).__name__, "message": str(error),
                                    "dispatch_status": getattr(error, "dispatch_status", "possibly_sent"),
                                    "retry": getattr(error, "retry", "reobserve")}
            try:
                self._append("action_finished", payload)
            except OSError as exc:
                raise ActionDispatchError("action journal could not save the dispatch result; re-observe before retrying",
                                          code="JOURNAL_WRITE_FAILED") from exc

    def checkpoint(self, controller_id: str, *, completed: str = "", next_step: str = "", notes: str = "") -> dict[str, Any]:
        for value in (completed, next_step, notes):
            if not isinstance(value, str) or len(value) > 4000:
                raise ValueError("checkpoint values must be strings of at most 4000 characters")
        with self._lock:
            self._require_owner(controller_id)
            event = self._append("checkpoint", {"completed": completed, "next_step": next_step,
                                 "notes": notes, "source": "agent_supplied", "observation_event": self._last_observation})
            return {**self.status(), "checkpoint_event": event}

    def stop(self, controller_id: str, *, status: str = "stopped") -> dict[str, Any]:
        with self._lock:
            self._require_owner(controller_id)
            result = self.status()
            self._append("journal_stopped", {"status": status})
            session = {**(self._session or {}), "status": status, "stopped_at": _event_time_iso()}
            _atomic_write_json(self._directory / "session.json", session)
            try:
                full = AgentJournalManager.read(str(self._directory), trace_limit=None)
                _atomic_write_json(self._directory / "procedure.json", full["procedure"])
                procedure_path = str(self._directory / "procedure.json")
            except (OSError, ValueError) as error:
                # The journal is canonical; projection failure must not
                # prevent a controller release or corrupt the raw evidence.
                procedure_path = None
                session["procedure_error"] = type(error).__name__
                _atomic_write_json(self._directory / "session.json", session)
            self._directory, self._controller_id, self._session = None, None, None
            self._auto_observe_after_action = False
            return {**result, "active": False, "event_count": self._sequence,
                    "procedure_path": procedure_path}

    def stop_if_owned(self, controller_id: Any, *, status: str = "stopped") -> None:
        with self._lock:
            if self._directory is not None and self._controller_id == controller_id:
                self.stop(controller_id, status=status)

    def close(self) -> None:
        with self._lock:
            if self._directory is not None:
                self.stop(self._controller_id, status="interrupted")

    def reconcile_controller(self, acquired_controller_id: str) -> None:
        """Called only after canonical lease acquisition succeeds."""
        with self._lock:
            if self._directory is not None and self._controller_id != acquired_controller_id:
                self.stop(self._controller_id, status="interrupted")

    @staticmethod
    def _project_trace(session: dict[str, Any], events: list[dict[str, Any]],
                       directory: Path) -> dict[str, Any]:
        """Derived teaching evidence from the canonical durable agent journal.

        This is not physical recording or replay. Images and later focus are
        observations; neither input delivery nor pixel change verifies intent.
        """
        import hashlib
        steps: list[dict[str, Any]] = []
        started: dict[int, dict[str, Any]] = {}
        observations: dict[int, dict[str, Any]] = {}
        last_completed: dict[str, Any] | None = None
        failures = 0
        for ev in events:
            kind, num = ev.get("kind"), ev.get("event")
            if kind == "observation":
                state = ev.get("state") or {}
                ref = {
                    "event": num, "time": ev.get("time"),
                    "source": ev.get("source") or "legacy_unspecified",
                    "screenshot": state.get("screenshot"),
                    "surface": (state.get("surface") or {}).get("kind"),
                    "window": state.get("target_window"),
                    "uia_available": bool((state.get("accessibility") or {}).get("available")),
                    "uia_elements": (state.get("accessibility") or {}).get("node_count"),
                    "capture_provider": (state.get("raster") or {}).get("capture_provider"),
                    "raster_size": {key: (state.get("raster") or {}).get(key)
                                    for key in ("width", "height")},
                }
                observations[num] = ref
                if last_completed is not None and last_completed.get("after") is None:
                    last_completed["after"] = ref
                continue
            if kind == "action_started":
                last_completed = None
                request = ev.get("request") or {}
                operation = {
                    "step": len(steps) + 1,
                    "operation": ev.get("action") or "unknown",
                    "input_source": "agent_dispatch",
                    "time": ev.get("time"),
                    "action_event": num,
                    "request": request,
                    "before": observations.get(ev.get("before_observation")),
                    "after": None,
                    "dispatch_status": "possibly_sent",
                    "effect_verified": False,
                    "uncertainties": ["action_started_without_delivery_receipt"],
                }
                steps.append(operation)
                started[num] = operation
                continue
            if kind == "action_finished":
                operation = started.pop(ev.get("action_event"), None)
                if operation is None:
                    continue
                receipt, err = ev.get("result") or {}, ev.get("error") or {}
                status = (err.get("dispatch_status") or receipt.get("dispatch_status")
                          or "possibly_sent")
                operation["dispatch_status"] = status
                operation["effect_verified"] = False
                operation["uncertainties"] = ["dispatch_receipt_is_not_effect_verification"]
                if err:
                    operation["error"] = {
                        "type": err.get("type"), "dispatch_status": status,
                        "retry": err.get("retry"),
                    }
                last_completed = operation
                continue
            if kind == "observation_failed":
                failures += 1
                if last_completed is not None and last_completed.get("after") is None:
                    last_completed["uncertainties"].append("post_action_observation_failed")
        differences = 0
        paired_images = 0
        missing_pairs = 0
        image_hashes: dict[Path, bytes] = {}
        folder = directory.resolve()

        def image_digest(name: str) -> bytes | None:
            candidate = (folder / name).resolve()
            if not candidate.is_relative_to(folder) or not candidate.is_file():
                return None
            if candidate.stat().st_size > 32 * 1024 * 1024:
                return None
            if candidate not in image_hashes:
                with candidate.open("rb") as stream:
                    image_hashes[candidate] = hashlib.file_digest(stream, "sha256").digest()
            return image_hashes[candidate]

        for operation in steps:
            before, after = operation.get("before") or {}, operation.get("after") or {}
            a, b = before.get("screenshot"), after.get("screenshot")
            if a and b:
                left = image_digest(a)
                right = image_digest(b)
                if left is not None and right is not None:
                    paired_images += 1
                    different = left != right
                    operation["visual_comparison"] = ("image_bytes_differ" if different
                                                       else "image_bytes_equal")
                    if different:
                        differences += 1
                else:
                    operation["visual_comparison"] = "referenced_image_missing_or_not_safe_to_read"
                    missing_pairs += 1
            else:
                operation["visual_comparison"] = "before_or_after_image_unavailable"
                missing_pairs += 1
            if not after:
                operation["uncertainties"].append("missing_post_action_observation")
            elif after.get("source") != "automatic_after_action":
                operation["uncertainties"].append("later_observation_not_event_time_bound")
            if (before.get("surface") == "remote_desktop"
                    or after.get("surface") == "remote_desktop"):
                operation["uncertainties"].append("remote_control_focus_not_accessible_to_local_uia")
        return {
            "format_version": 1,
            "kind": "agent_execution_procedure",
            "input_source": "agent_dispatch_not_human",
            "intent": session.get("intent") or {},
            "operations": steps,
            "evidence_availability": operational_evidence_availability(
                steps, source="agent"),
            "evidence_coverage": {
                "actions": len(steps),
                "observations": len(observations),
                "paired_images": paired_images,
                "image_pairs_unavailable": missing_pairs,
                "image_bytes_differ": differences,
                "observation_failures": failures,
                "unfinished_actions": len(started),
            },
            "interpretation": ("Every step records action delivery and observations, "
                               "not verified task completion. Image difference may "
                               "reflect cursor, focus, animation or unrelated activity."),
        }

    @staticmethod
    def read(session_dir: str, *, trace_limit: int | None = 50) -> dict[str, Any]:
        directory = Path(session_dir).expanduser().resolve()
        session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        if session.get("kind") != "agent_execution_journal" or session.get("format_version") != 1:
            raise ValueError("unsupported agent journal")
        checkpoint, observation, last_action = None, None, None
        pending: dict[int, dict[str, Any]] = {}
        count, incomplete_tail = 0, False
        trace_events: list[dict[str, Any]] = []
        actions_since_checkpoint: deque[dict[str, Any]] = deque(maxlen=50)
        actions_since_checkpoint_count = 0
        with (directory / "events.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Only an interrupted final write is recoverable. Mid-file
                    # corruption must never silently produce a trusted resume.
                    if line.endswith("\n") or stream.read():
                        raise ValueError("corrupt journal event")
                    incomplete_tail = True
                    break
                count += 1
                kind = event.get("kind")
                if kind in {"observation", "action_started", "action_finished", "observation_failed"}:
                    trace_events.append(event)
                if kind == "checkpoint":
                    checkpoint = event
                    actions_since_checkpoint.clear()
                    actions_since_checkpoint_count = 0
                elif kind == "observation":
                    observation = event
                elif kind == "action_started":
                    pending[event["event"]] = event
                    last_action = event
                    actions_since_checkpoint.append(event)
                    actions_since_checkpoint_count += 1
                elif kind == "action_finished":
                    started = pending.pop(event["action_event"], {})
                    last_action = {**started, "receipt": event}
                    actions_since_checkpoint.append(event)
                    actions_since_checkpoint_count += 1
        projection = AgentJournalManager._project_trace(session, trace_events, directory)
        total_steps = len(projection["operations"])
        if trace_limit is not None:
            projection["operations"] = projection["operations"][-max(0, int(trace_limit)):]
            projection["truncated_operations"] = total_steps - len(projection["operations"])
        return {"ok": True, "session_dir": str(directory), "session": session, "event_count": count,
                "procedure": projection, "procedure_path": str(directory / "procedure.json")
                if (directory / "procedure.json").is_file() else None,
                "checkpoint": checkpoint, "last_observation": observation, "last_action": last_action,
                "actions_since_checkpoint": list(actions_since_checkpoint),
                "action_summary_truncated": actions_since_checkpoint_count > len(actions_since_checkpoint),
                "unfinished_actions": list(pending.values()), "incomplete_tail": incomplete_tail,
                "requires_fresh_observation": True}


AGENT_JOURNAL = AgentJournalManager()


def _recording_frame_sampler(output: Any, stop: Any) -> None:
    """Acquire/encode pixels outside the interpreter hosting the input hook."""
    parent = multiprocessing.parent_process()
    while not stop.value and (parent is None or parent.is_alive()):
        started = time.time()
        try:
            image, binding = capture_screenshot_with_info(
                show_cursor=False, show_viewport=False, use_scope=False, capture_provider="desktop_composite")
            completed = time.time()
            monitors = list_monitors()
            stream = io.BytesIO()
            image.convert("RGB").save(stream, format="JPEG", quality=88)
            jpeg = stream.getvalue()
            if len(jpeg) <= 32 * 1024 * 1024:
                try:
                    output.send({"jpeg": jpeg, "binding": binding, "monitors": monitors,
                                 "started_epoch": started, "completed_epoch": completed})
                except (EOFError, OSError):
                    break
        except Exception:
            # Sampling failure leaves temporal selection/fallback explicit.
            pass
        time.sleep(max(0.01, 0.12 - (time.time() - started)))


class RecordingFrameBuffer:
    """Bounded, recording-only history from the canonical screenshot provider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: deque[dict[str, Any]] = deque()
        self._inputs: deque[float] = deque(maxlen=4096)
        self._input_queue = queue.SimpleQueue()
        self._process = None
        self._output = None
        self._process_stop = None
        self._bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        context = multiprocessing.get_context("spawn")
        self._output, sender = context.Pipe(duplex=False)
        self._process_stop = context.RawValue("b", False)
        self._process = context.Process(target=_recording_frame_sampler,
                                        args=(sender, self._process_stop),
                                        name="recording-frames", daemon=True)
        try:
            self._process.start()
            self._thread = threading.Thread(target=self._run, name="recording-frame-receiver", daemon=True)
            self._thread.start()
        except Exception:
            self.stop()
            raise
        finally:
            sender.close()

    def stop(self) -> None:
        if self._process_stop is not None:
            self._process_stop.value = True
        if self._process is not None and self._process.pid is not None:
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=2)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=2)
            if self._process.is_alive():
                raise OSError("recording frame process did not stop cleanly")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise OSError("recording frame receiver did not stop cleanly")
        if self._output is not None:
            self._output.close()
            self._output = None
        with self._lock:
            self._frames.clear()
            self._inputs.clear()
            while not self._input_queue.empty():
                self._input_queue.get_nowait()
            self._bytes = 0

    def note_input(self, event: dict[str, Any]) -> None:
        meaningful = (event.get("source") == "mouse" and event.get("phase") in {"down", "wheel"}
                      or event.get("source") == "keyboard" and event.get("phase") == "down"
                      and event.get("category") != "modifier")
        if meaningful:
            # SimpleQueue.put never waits for the frame/history lock.
            self._input_queue.put(float(event["epoch"]))

    def _store(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._frames.append(frame)
            self._bytes += len(frame["jpeg"])
            while self._frames and (len(self._frames) > 60 or self._bytes > 32 * 1024 * 1024
                                   or frame["completed_epoch"] - self._frames[0]["completed_epoch"] > 5):
                self._bytes -= len(self._frames.popleft()["jpeg"])

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._output.poll(0.1):
                    self._store(self._output.recv())
                elif self._process is not None and not self._process.is_alive():
                    break
            except (EOFError, OSError):
                break

    def select_before(self, epoch: float, *, max_age: float = 1.5) -> dict[str, Any] | None:
        """Latest completed frame before input, for transient menu evidence.

        Never represent a frame overlapping or following an input as its
        before-image. The time bound also rejects stale/unrelated content.
        """
        with self._lock:
            candidates = [f for f in self._frames
                          if f["completed_epoch"] <= epoch
                          and f["started_epoch"] >= epoch - max_age]
            return dict(candidates[-1], selection="immediately_before_input") if candidates else None

    def select(self, epoch: float, settle: float) -> dict[str, Any] | None:
        with self._lock:
            while not self._input_queue.empty():
                self._inputs.append(self._input_queue.get_nowait())
            frames = list(self._frames)
            boundary = next((stamp for stamp in self._inputs if stamp > epoch), float("inf"))
        clean = [f for f in frames if f["started_epoch"] >= epoch and f["completed_epoch"] < boundary]
        settled = [f for f in clean if f["started_epoch"] >= epoch + settle]
        if settled:
            frame, selection = settled[0], "settled_after_input"
        elif clean:
            frame, selection = clean[-1], "early_after_input_before_next"
        else:
            before = [f for f in frames if f["completed_epoch"] <= epoch]
            if not before:
                return None
            frame, selection = before[-1], "before_input_no_clean_after"
        return dict(frame, selection=selection)


class UserRecordingManager:
    """Canonical human-demonstration recorder.

    Human input is observed passively, correlated into semantic steps, enriched
    with the existing screenshot/UIA/window owners, and exported as JSON + HTML.
    Demonstration text may be captured when enabled so an agent can reproduce the shown workflow.
    Password controls are redacted; screenshots and ordinary visible UI labels can still contain on-screen text.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queue: queue.Queue[Any] | None = None
        self._semantic_queue: queue.Queue[Any] | None = None
        self._worker: threading.Thread | None = None
        self._enrichment_worker: threading.Thread | None = None
        self._hook: WindowsUserInputHook | None = None
        self._frame_buffer: RecordingFrameBuffer | None = None
        self._active = False
        self._session: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._dropped_events = 0
        self._dropped_semantic_steps = 0
        self._processing_errors = {"correlation": 0, "enrichment": 0}
        self._raw_queue_peak = 0
        self._semantic_queue_peak = 0
        self._last_progress_snapshot = 0.0
        self._last_context_click: dict[str, Any] | None = None
        self._pending_mouse_down: dict[str, dict[str, Any]] = {}
        self._pending_scroll: dict[str, Any] | None = None
        self._pending_keyboard: dict[str, Any] | None = None
        self._pending_clicks: dict[str, dict[str, Any]] = {}
        self._last_result: dict[str, Any] | None = None
        self._session_dir: Path | None = None
        self._settle_seconds = DEFAULT_RECORDING_SETTLE_MS / 1000.0
        self._capture_screenshots = DEFAULT_RECORDING_SCREENSHOTS
        self._capture_scope = DEFAULT_RECORDING_CAPTURE_SCOPE
        self._capture_text = True
        self._agent_steps: list[dict[str, Any]] = []
        self._recording_sensitive_focus = False
        self._ignored_hwnds: set[int] = set()
        self._ignored_pids: set[int] = set()
        self._control_restore_epoch_getter: Any | None = None
        self._double_click_seconds, self._double_click_x, self._double_click_y = _recording_double_click_metrics()

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = dict(self._session) if self._session else None
            if session:
                session.pop("steps", None)
            return {
                "ok": True,
                "active": self._active,
                "format_version": RECORDING_FORMAT_VERSION,
                "privacy": {
                    "keystroke_text_payload": (
                        "captured_except_password_controls" if self._capture_text else "not captured"
                    ),
                    "uia_value_pattern": "not requested",
                    "screenshots_and_visible_UI_text": "may contain text visible on screen",
                },
                "session": session,
                "session_dir": str(self._session_dir) if self._active and self._session_dir else None,
                "step_count": len(self._steps),
                "agent_step_count": len(self._agent_steps),
                "dropped_raw_events": self._dropped_events,
                "dropped_semantic_steps": self._dropped_semantic_steps,
                "raw_queue_peak": self._raw_queue_peak,
                "semantic_queue_peak": self._semantic_queue_peak,
                "last_recording": dict(self._last_result) if self._last_result else None,
            }

    def start(
        self,
        *,
        output_dir: str | os.PathLike[str] | None = None,
        capture_screenshots: bool = True,
        settle_ms: int = DEFAULT_RECORDING_SETTLE_MS,
        capture_scope: str = DEFAULT_RECORDING_CAPTURE_SCOPE,
        capture_text: bool = True,
        goal: str = "",
        prerequisites: str = "",
        success_criteria: str = "",
        ignored_hwnds: set[int] | None = None,
        ignored_pids: set[int] | None = None,
        control_stop_callback: Any | None = None,
        control_restore_epoch_getter: Any | None = None,
    ) -> dict[str, Any]:
        if os.name != "nt":
            raise ValueError("user recording currently requires Windows")
        try:
            settle = int(settle_ms)
        except (TypeError, ValueError) as exc:
            raise ValueError("settle_ms must be an integer") from exc
        if not 0 <= settle <= 3000:
            raise ValueError("settle_ms must be between 0 and 3000")
        normalized_capture_scope = normalize_recording_capture_scope(capture_scope)
        intent = recording_intent(goal, prerequisites, success_criteria)
        with self._lock:
            if self._active:
                raise ValueError("a user recording is already active")
            root = Path(output_dir).expanduser().resolve() if output_dir else default_recording_root().resolve()
            session_id = datetime.now().astimezone().strftime("Recording_%Y%m%d_%H%M%S_%f")[:-3]
            session_dir = root / session_id
            (session_dir / "screenshots").mkdir(parents=True, exist_ok=False)
            self._queue = queue.Queue(maxsize=RECORDING_QUEUE_LIMIT)
            self._semantic_queue = queue.Queue(maxsize=max(256, RECORDING_QUEUE_LIMIT // 2))
            self._steps = []
            self._agent_steps = []
            self._recording_sensitive_focus = False
            self._dropped_events = 0
            self._dropped_semantic_steps = 0
            self._processing_errors = {"correlation": 0, "enrichment": 0}
            self._raw_queue_peak = 0
            self._semantic_queue_peak = 0
            self._last_progress_snapshot = 0.0
            self._last_context_click = None
            self._pending_mouse_down = {}
            self._pending_scroll = None
            self._pending_keyboard = None
            self._pending_clicks = {}
            self._settle_seconds = settle / 1000.0
            self._capture_screenshots = bool(capture_screenshots)
            self._frame_buffer = RecordingFrameBuffer() if self._capture_screenshots else None
            self._capture_scope = normalized_capture_scope
            self._capture_text = bool(capture_text)
            self._ignored_hwnds = {int(v) for v in (ignored_hwnds or set()) if int(v) > 0}
            self._ignored_pids = {int(v) for v in (ignored_pids or set()) if int(v) > 0}
            self._control_restore_epoch_getter = control_restore_epoch_getter
            self._session_dir = session_dir
            self._session = {
                "format_version": RECORDING_FORMAT_VERSION,
                "session_id": session_id,
                "status": "recording",
                "started_at": _event_time_iso(),
                "stopped_at": None,
                "bridge": {"version": VERSION, "build_id": BUILD_ID},
                "platform": {"os_name": os.name, "dpi_awareness": dict(WINDOWS_DPI_STATE)},
                "recording_directory": session_id,
                "capture": {
                    "screenshots": bool(capture_screenshots),
                    "settle_ms": settle,
                    "provider": "desktop_composite",
                    "scope": normalized_capture_scope,
                    "frame_history": {"execution": "separate_process", "nominal_interval_ms": 120, "maximum_age_seconds": 5, "maximum_frames": 60, "maximum_jpeg_bytes": 33554432},
                },
                "privacy": {
                    "keystroke_text_payload": (
                        "captured_except_password_controls" if self._capture_text else "not captured"
                    ),
                    "uia_value_pattern": "not read",
                    "screenshots_and_visible_UI_text": "may contain text visible on screen",
                },
                "agent_trace": "agent_steps.json",
                "procedure_trace": "procedure.json",
                "intent": intent,
                "step_count": 0,
                "agent_step_count": 0,
                "dropped_raw_events": 0,
                "dropped_semantic_steps": 0,
                "processing_errors": dict(self._processing_errors),
                "pipeline": {
                    "raw_event_context": "frozen_in_low_level_hook",
                    "correlation": "dedicated_worker",
                    "enrichment": "dedicated_worker",
                    "target_identity_policy": "fail_stale_not_false_semantics",
                },
            }
            _atomic_write_json(session_dir / "session.json", self._session)
            self._worker = threading.Thread(target=self._run_worker, name="user-recording-correlator", daemon=True)
            self._enrichment_worker = threading.Thread(
                target=self._run_enrichment_worker, name="user-recording-enrichment", daemon=True
            )
            self._worker.start()
            self._enrichment_worker.start()
            self._hook = WindowsUserInputHook(
                self._enqueue_event,
                injection_sentinel=keyboard_injection_sentinel_path(os.getpid()),
                on_control_stop=control_stop_callback,
                capture_text=self._capture_text,
            )
            if not self._hook.start():
                self._queue.put_nowait(None)
                self._worker.join(timeout=1.0)
                if self._semantic_queue is not None:
                    try:
                        self._semantic_queue.put_nowait(None)
                    except queue.Full:
                        pass
                if self._enrichment_worker is not None:
                    self._enrichment_worker.join(timeout=1.0)
                self._session["status"] = "failed"
                self._session["error"] = "Windows low-level input hooks could not be installed"
                _atomic_write_json(session_dir / "session.json", self._session)
                self._hook = None
                self._worker = None
                self._enrichment_worker = None
                self._queue = None
                self._semantic_queue = None
                raise OSError("Windows low-level mouse/keyboard hooks could not be installed")
            self._active = True
            if self._frame_buffer:
                try:
                    self._frame_buffer.start()
                except Exception:
                    self.stop()
                    raise
            return self.status()

    def _enqueue_event(self, event: dict[str, Any]) -> None:
        q = self._queue
        if not self._active and not (self._session and self._session.get("status") == "recording"):
            return
        if q is None:
            return
        if self._frame_buffer:
            self._frame_buffer.note_input(event)
        try:
            q.put_nowait(event)
            try:
                self._raw_queue_peak = max(self._raw_queue_peak, int(q.qsize()))
            except Exception:
                pass
        except queue.Full:
            with self._lock:
                self._dropped_events += 1

    def _flush_pending_clicks(self, now_mono: float, *, force: bool = False) -> None:
        for button, pending in list(self._pending_clicks.items()):
            if force or now_mono - float(pending["monotonic"]) >= self._double_click_seconds:
                self._pending_clicks.pop(button, None)
                self._emit_step(
                    dict(pending["action"]),
                    epoch=float(pending["epoch"]),
                    point=dict(pending["point"]),
                    event_context=pending.get("event_context"),
                )

    def _flush_due(self, now_mono: float, *, force: bool = False) -> None:
        self._flush_pending_clicks(now_mono, force=force)
        if self._pending_scroll and (force or now_mono - float(self._pending_scroll["last_monotonic"]) >= 0.35):
            pending = self._pending_scroll
            self._pending_scroll = None
            direction = ("right" if pending["delta"] > 0 else "left") if pending["horizontal"] else ("up" if pending["delta"] > 0 else "down")
            action = {
                "type": "scroll", "direction": direction, "delta": int(pending["delta"]),
                "events": int(pending["events"]), "x": int(pending["x"]), "y": int(pending["y"]),
                "horizontal": bool(pending["horizontal"]),
                "duration_ms": max(0, int(round((float(pending["last_monotonic"]) - float(pending["first_monotonic"])) * 1000))),
            }
            self._emit_step(
                action,
                epoch=float(pending["last_epoch"]),
                point={"x": int(pending["x"]), "y": int(pending["y"])},
                event_context=pending.get("event_context"),
            )
        if self._pending_keyboard and (force or now_mono - float(self._pending_keyboard["last_monotonic"]) >= 0.70):
            pending = self._pending_keyboard
            self._pending_keyboard = None
            action = {
                "type": "keyboard_input",
                "events": int(pending["events"]),
                "text_key_events": int(pending["counts"].get("text", 0)),
                "navigation_key_events": int(pending["counts"].get("navigation", 0)),
                "editing_key_events": int(pending["counts"].get("editing", 0)),
                "special_key_events": int(pending["counts"].get("special", 0) + pending["counts"].get("function", 0)),
                "safe_keys": list(pending["safe_keys"]),
                "modifiers_seen": sorted(pending["modifiers"]),
                "duration_ms": max(0, int(round((float(pending["last_monotonic"]) - float(pending["first_monotonic"])) * 1000))),
                "text": pending.get("text_buffer", "") if self._capture_text else "<not captured>",
                "text_captured": bool(self._capture_text),
                "shortcuts": list(pending.get("shortcuts") or []),
                "input_sequence": list(pending.get("input_sequence") or []),
            }
            self._emit_step(
                action,
                epoch=float(pending["last_epoch"]),
                focus=True,
                event_context=pending.get("event_context"),
            )

    def _run_worker(self) -> None:
        q = self._queue
        semantic_q = self._semantic_queue
        if q is None or semantic_q is None:
            return
        try:
            while True:
                try:
                    event = q.get(timeout=0.10)
                except queue.Empty:
                    # Wall-clock flush happens only when the raw queue is empty.
                    # Slow screenshot/UIA work therefore cannot split old queued
                    # wheel or keyboard events into artificial single-event steps.
                    self._flush_due(time.monotonic())
                    continue
                if event is None:
                    self._flush_due(time.monotonic(), force=True)
                    break
                try:
                    self._consume_event(event)
                except Exception as exc:
                    self._log_recording_error("correlation", exc, event=event)
        finally:
            try:
                semantic_q.put(None, timeout=2.0)
            except Exception:
                pass

    def _run_enrichment_worker(self) -> None:
        q = self._semantic_queue
        if q is None:
            return
        while True:
            item = q.get()
            if item is None:
                break
            try:
                self._enrich_step(item)
            except Exception as exc:
                self._log_recording_error("enrichment", exc, semantic=item)

    def _log_recording_error(
        self,
        stage: str,
        exc: BaseException,
        *,
        event: dict[str, Any] | None = None,
        semantic: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._processing_errors[stage] = self._processing_errors.get(stage, 0) + 1
        session_dir = self._session_dir
        if not session_dir:
            return
        safe_event = None
        if event:
            safe_event = {
                key: event.get(key)
                for key in ("source", "phase", "button", "x", "y", "category", "safe_key", "event_context")
                if key in event
            }
        safe_semantic = None
        if semantic:
            safe_action = AgentJournalManager._safe_payload(semantic.get("action") or {})
            if "text" in safe_action:
                safe_action["text"] = "<redacted-from-error-log>"
            safe_semantic = {
                "action": safe_action,
                "epoch": semantic.get("epoch"),
                "point": semantic.get("point"),
                "event_context": semantic.get("event_context"),
            }
        with (session_dir / "errors.log").open("a", encoding="utf-8") as fh:
            fh.write(
                f"{_event_time_iso()} {stage} error: {exc}\n"
                f"event={json.dumps(safe_event, ensure_ascii=False)}\n"
                f"semantic={json.dumps(safe_semantic, ensure_ascii=False)}\n"
                f"{traceback.format_exc()}\n"
            )

    def _window_targets_ignored_ui(self, window: dict[str, Any] | None) -> bool:
        if not window:
            return False
        try:
            hwnd = int(window.get("hwnd") or 0)
            pid = int(window.get("pid") or 0)
            return hwnd in self._ignored_hwnds or pid in self._ignored_pids
        except Exception:
            return False

    def _event_targets_ignored_ui(self, event: dict[str, Any]) -> bool:
        if not self._ignored_hwnds and not self._ignored_pids:
            return False
        context = event.get("event_context")
        if isinstance(context, dict):
            return self._window_targets_ignored_ui(context)
        return False

    def _is_recorder_restore_shell_step(
        self,
        action: dict[str, Any],
        epoch: float,
        window: dict[str, Any] | None,
        target: dict[str, Any] | None = None,
    ) -> bool:
        if action.get("type") not in {"click", "double_click"} or not window:
            return False
        class_name = str(window.get("class_name") or "").strip().casefold()
        if class_name not in {"shell_traywnd", "shell_secondarytraywnd"}:
            return False
        if isinstance(target, dict):
            automation_id = str(target.get("automation_id") or "").strip().casefold()
            target_name = str(target.get("name") or "").strip().casefold()
            if (
                automation_id == "appid: agentcomputerbridge.recorder"
                or "agent-computer-bridge-recorder" in target_name
                or target_name.startswith("agent schrittaufzeichnung")
            ):
                return True
        if self._control_restore_epoch_getter is None:
            return False
        try:
            restore_epoch = float(self._control_restore_epoch_getter() or 0.0)
        except Exception:
            return False
        return restore_epoch > 0.0 and abs(float(epoch) - restore_epoch) <= 1.25

    def _consume_event(self, event: dict[str, Any]) -> None:
        source = event.get("source")
        event_mono = float(event.get("monotonic") or time.monotonic())
        context = event.get("event_context") if isinstance(event.get("event_context"), dict) else None
        if self._event_targets_ignored_ui(event):
            return

        if source == "keyboard" and event.get("phase") == "down":
            self._flush_pending_clicks(event_mono, force=True)
            if self._pending_scroll is not None:
                self._flush_due(event_mono, force=True)
            self._flush_due(event_mono)
            context_key = _event_context_key(context)
            is_text = isinstance(event.get("typed_text"), str) and bool(event.get("typed_text"))
            # Navigation, editing and shortcuts are boundaries, never a set of
            # keys to apply after a concatenated text buffer.
            if not is_text and self._pending_keyboard is not None:
                self._flush_due(event_mono, force=True)
            if self._pending_keyboard is not None and self._pending_keyboard.get("context_key") != context_key:
                self._flush_due(event_mono, force=True)
            if self._pending_keyboard is None:
                self._pending_keyboard = {
                    "events": 0, "counts": {}, "safe_keys": [], "modifiers": set(),
                    "text_buffer": "", "shortcuts": [], "input_sequence": [],
                    "first_monotonic": event_mono, "last_monotonic": event_mono,
                    "last_epoch": event["epoch"], "event_context": context, "context_key": context_key,
                }
            pending = self._pending_keyboard
            pending["events"] += 1
            category = str(event.get("category") or "other")
            pending["counts"][category] = int(pending["counts"].get(category, 0)) + 1
            if event.get("safe_key") and category != "modifier":
                pending["safe_keys"].append(str(event["safe_key"]))
            typed_text = event.get("typed_text")
            if self._capture_text and isinstance(typed_text, str) and typed_text:
                pending["text_buffer"] += typed_text
                sequence = pending["input_sequence"]
                if sequence and sequence[-1].get("kind") == "text":
                    sequence[-1]["text"] += typed_text
                else:
                    sequence.append({"kind": "text", "text": typed_text})
            shortcut = event.get("shortcut")
            if shortcut:
                pending["shortcuts"].append(str(shortcut))
                pending["input_sequence"].append({"kind": "hotkey", "keys": str(shortcut).split("+")})
            elif not is_text and event.get("safe_key"):
                pending["input_sequence"].append({"kind": "key", "key": str(event["safe_key"]), "modifiers": list(event.get("modifiers") or [])})
            pending["modifiers"].update(str(v) for v in event.get("modifiers", []))
            pending["last_monotonic"] = event_mono
            pending["last_epoch"] = event["epoch"]
            if not is_text:
                self._flush_due(event_mono, force=True)
            return

        if source != "mouse":
            return
        phase = event.get("phase")
        # A pointer event must not overtake text still waiting for its idle flush.
        if self._pending_keyboard is not None:
            self._flush_due(event_mono, force=True)
        if phase == "wheel":
            self._flush_pending_clicks(event_mono, force=True)
            self._flush_due(event_mono)
            horizontal = bool(event.get("horizontal"))
            context_key = _event_context_key(context)
            incoming_delta = int(event.get("delta") or 0)
            direction_changed = False
            if self._pending_scroll is not None:
                pending_delta = int(self._pending_scroll.get("delta") or 0)
                direction_changed = (
                    pending_delta != 0
                    and incoming_delta != 0
                    and ((pending_delta > 0) != (incoming_delta > 0))
                )
            if self._pending_scroll is not None and (
                bool(self._pending_scroll["horizontal"]) != horizontal
                or self._pending_scroll.get("context_key") != context_key
                or direction_changed
            ):
                self._flush_due(event_mono, force=True)
            if self._pending_scroll is None:
                self._pending_scroll = {
                    "delta": 0, "events": 0, "horizontal": horizontal,
                    "x": event["x"], "y": event["y"],
                    "first_monotonic": event_mono, "last_monotonic": event_mono,
                    "last_epoch": event["epoch"], "event_context": context, "context_key": context_key,
                }
            pending = self._pending_scroll
            pending["delta"] += incoming_delta
            pending["events"] += 1
            pending["x"], pending["y"] = event["x"], event["y"]
            pending["last_monotonic"], pending["last_epoch"] = event_mono, event["epoch"]
            return

        if phase in {"down", "up"}:
            if self._pending_scroll is not None:
                self._flush_due(event_mono, force=True)
            self._flush_due(event_mono)
            button = str(event.get("button") or "left")
            if phase == "down":
                # Preserve a same-button pending click long enough to recognize
                # a Windows double-click; unrelated buttons flush immediately.
                for other in list(self._pending_clicks):
                    if other != button:
                        prior = self._pending_clicks.pop(other)
                        self._emit_step(
                            dict(prior["action"]), epoch=float(prior["epoch"]),
                            point=dict(prior["point"]), event_context=prior.get("event_context"),
                        )
                self._pending_mouse_down[button] = dict(event)
                return

            down = self._pending_mouse_down.pop(button, None)
            if down is None:
                action = {
                    "type": "click", "button": button,
                    "x": int(event["x"]), "y": int(event["y"]), "duration_ms": None,
                }
            else:
                dx = int(event["x"]) - int(down["x"])
                dy = int(event["y"]) - int(down["y"])
                duration_ms = max(0, int(round((event_mono - float(down["monotonic"])) * 1000)))
                if abs(dx) > 6 or abs(dy) > 6:
                    self._flush_pending_clicks(event_mono, force=True)
                    action = {
                        "type": "drag", "button": button,
                        "from": {"x": int(down["x"]), "y": int(down["y"])},
                        "to": {"x": int(event["x"]), "y": int(event["y"])},
                        "duration_ms": duration_ms,
                    }
                    self._emit_step(
                        action, epoch=float(event["epoch"]),
                        point={"x": int(event["x"]), "y": int(event["y"])},
                        event_context=context or down.get("event_context"),
                    )
                    return
                action = {
                    "type": "click", "button": button,
                    "x": int(event["x"]), "y": int(event["y"]), "duration_ms": duration_ms,
                }

            prior = self._pending_clicks.get(button)
            if prior is not None:
                dt = event_mono - float(prior["monotonic"])
                prior_point = prior["point"]
                same_window = _event_context_key(prior.get("event_context")) == _event_context_key(context)
                if (
                    dt <= self._double_click_seconds
                    and abs(int(event["x"]) - int(prior_point["x"])) <= self._double_click_x
                    and abs(int(event["y"]) - int(prior_point["y"])) <= self._double_click_y
                    and same_window
                ):
                    self._pending_clicks.pop(button, None)
                    double_action = {
                        "type": "double_click", "button": button,
                        "x": int(event["x"]), "y": int(event["y"]),
                        "events": 2, "interval_ms": max(0, int(round(dt * 1000))),
                    }
                    self._emit_step(
                        double_action, epoch=float(event["epoch"]),
                        point={"x": int(event["x"]), "y": int(event["y"])},
                        event_context=context or prior.get("event_context"),
                    )
                    return
                self._pending_clicks.pop(button, None)
                self._emit_step(
                    dict(prior["action"]), epoch=float(prior["epoch"]),
                    point=dict(prior["point"]), event_context=prior.get("event_context"),
                )

            self._pending_clicks[button] = {
                "action": action,
                "epoch": float(event["epoch"]),
                "monotonic": event_mono,
                "point": {"x": int(event["x"]), "y": int(event["y"])},
                "event_context": context,
            }

    def _describe(self, action: dict[str, Any], target: dict[str, Any] | None, window: dict[str, Any] | None) -> str:
        target_name = _bounded_text((target or {}).get("name"))
        control_type = _bounded_text((target or {}).get("control_type"), 80)
        target_text = target_name or control_type
        window_title = _bounded_text((window or {}).get("title"))
        where = f' on "{target_text}"' if target_text else ""
        inside = f' in "{window_title}"' if window_title else ""
        kind = action.get("type")
        if kind == "click":
            return f"User {action.get('button', 'left')} click{where}{inside}"
        if kind == "double_click":
            return f"User {action.get('button', 'left')} double click{where}{inside}"
        if kind == "drag":
            return f"User {action.get('button', 'left')} drag{where}{inside}"
        if kind == "scroll":
            return f"User scroll {action.get('direction')}{where}{inside}"
        if kind == "keyboard_input":
            if action.get("text_redacted"):
                return f"User typed <redacted>{where}{inside}"
            typed = _bounded_text(action.get("text"), 160)
            shortcuts = [str(v) for v in (action.get("shortcuts") or [])]
            keys = [str(v) for v in (action.get("safe_keys") or [])]
            if typed and typed != "<not captured>":
                extra = shortcuts + keys
                suffix = " then " + ", ".join(extra) if extra else ""
                return f'User typed "{typed}"{where}{inside}{suffix}'
            if shortcuts:
                return f"User shortcut {', '.join(shortcuts)}{where}{inside}"
            return f"User keyboard input{where}{inside}"
        return f"User action {kind}{where}{inside}"

    def _emit_step(
        self,
        action: dict[str, Any],
        *,
        epoch: float,
        point: dict[str, int] | None = None,
        focus: bool = False,
        event_context: dict[str, Any] | None = None,
    ) -> None:
        semantic_q = self._semantic_queue
        if semantic_q is None:
            return
        item = {
            "action": action,
            "epoch": float(epoch),
            "point": dict(point) if point else None,
            "focus": bool(focus),
            "event_context": dict(event_context) if isinstance(event_context, dict) else None,
        }
        # A context menu can disappear as soon as the next item is chosen.
        # Use one pre-click frame on the first click after a right click in
        # the same local window, without slowing the human or doubling
        # screenshots for every action.
        prior = self._last_context_click
        action_kind = str(action.get("type") or "")
        is_click = action_kind in {"click", "double_click"}
        window_key = _event_context_key(event_context)
        should_pin_before = bool(
            is_click and action.get("button", "left") != "right"
            and prior and window_key != (0, 0)
            and window_key == prior["window"]
            and 0 <= float(epoch) - prior["epoch"] <= 6.0)
        if is_click:
            self._last_context_click = (
                {"epoch": float(epoch), "window": window_key}
                if action.get("button") == "right" else None)
        elif prior and float(epoch) - prior["epoch"] > 6.0:
            self._last_context_click = None
        if (should_pin_before and self._frame_buffer is not None
                and self._capture_screenshots):
            frame = self._frame_buffer.select_before(item["epoch"])
            if frame and len(frame["jpeg"]) <= 2 * 1024 * 1024:
                item["buffered_before_context_selection"] = frame
        # Pin valid post-input pixels near correlation time. UIA delays must
        # not cause the bounded frame cache to evict this evidence.
        if (self._frame_buffer is not None and self._capture_screenshots
                and semantic_q.qsize() < 24):
            frame = self._frame_buffer.select(item["epoch"], self._settle_seconds)
            # Bound frames held by queued semantic steps independently of
            # RecordingFrameBuffer's 32 MiB history limit (<= 48 MiB pinned).
            if (frame and frame["selection"] != "before_input_no_clean_after"
                    and len(frame["jpeg"]) <= 2 * 1024 * 1024):
                item["buffered_frame_at_emit"] = frame
        try:
            semantic_q.put_nowait(item)
            try:
                self._semantic_queue_peak = max(self._semantic_queue_peak, int(semantic_q.qsize()))
            except Exception:
                pass
        except queue.Full:
            with self._lock:
                self._dropped_semantic_steps += 1

    def _event_window_for_step(self, context: dict[str, Any] | None) -> dict[str, Any] | None:
        if not context:
            return None
        window = dict(context)
        try:
            pid = int(window.get("pid") or 0)
        except Exception:
            pid = 0
        if pid > 0:
            try:
                process_path = _process_image_path(pid)
            except Exception:
                process_path = None
            window["process_path"] = process_path
            window["process_name"] = Path(process_path).name if process_path else None
        return _compact_window(window)

    @staticmethod
    def _agent_target_projection(uia: dict[str, Any] | None) -> dict[str, Any] | None:
        """Project the sampled node, never substitute a more attractive ancestor."""
        node = (uia or {}).get("target")
        if not isinstance(node, dict):
            return None
        out = {dest: node[src] for src, dest in (
            ("name", "name"), ("control_type", "role"),
            ("automation_id", "automation_id"), ("class_name", "class_name"),
        ) if node.get(src)}
        rect = node.get("bounding_rect")
        if isinstance(rect, dict):
            out["bounds"] = {key: rect[key] for key in ("x", "y", "width", "height") if key in rect}
        return out or None

    def _agent_best_target(self, step: dict[str, Any]) -> dict[str, Any] | None:
        # UIA is sampled asynchronously after input. Focus after input is a
        # separate observation, never proof of the action's recipient.
        candidate = self._agent_target_projection(step.get("uia"))
        if not candidate or not candidate.get("name"):
            return None
        if candidate.get("role") in {"ImageControl", "DocumentControl", "GroupControl", "PaneControl", "CustomControl"}:
            return None
        action = step.get("action") or {}
        if action.get("type") in {"click", "double_click", "scroll", "drag"}:
            rect = candidate.get("bounds") or {}
            x, y = action.get("x"), action.get("y")
            if x is None or y is None or not all(key in rect for key in ("x", "y", "width", "height")):
                return None
            if rect["width"] <= 2 or rect["height"] <= 2:
                return None
            if not (rect["x"] <= x < rect["x"] + rect["width"] and rect["y"] <= y < rect["y"] + rect["height"]):
                return None
        return candidate

    @staticmethod
    def _recorded_image_point(step: dict[str, Any], point: Any) -> dict[str, int] | None:
        """Map an input's virtual-desktop position into its recorded screenshot.

        Image coordinates are historical evidence, never authority for a new
        computer-use action. Refuse incomplete or inconsistent image bindings.
        """
        if not isinstance(point, dict) or "x" not in point or "y" not in point:
            return None
        capture = step.get("capture_binding") or {}
        region = capture.get("screen_mapping") or {}
        frame = capture.get("frame_size") or {}
        try:
            rx, ry, rw, rh = (float(region[k]) for k in ("x", "y", "width", "height"))
            fw, fh = (int(frame[k]) for k in ("width", "height"))
            x, y = float(point["x"]), float(point["y"])
        except (TypeError, ValueError, KeyError, OverflowError):
            return None
        if not (rw > 0 and rh > 0 and fw > 0 and fh > 0
                and rx <= x < rx + rw and ry <= y < ry + rh):
            return None
        return {
            "x": min(fw - 1, max(0, int((x - rx) * fw / rw))),
            "y": min(fh - 1, max(0, int((y - ry) * fh / rh))),
        }

    def _agent_step_projection(self, step: dict[str, Any]) -> dict[str, Any]:
        action = dict(step.get("action") or {})
        kind = str(action.get("type") or "unknown")
        window = step.get("window") or {}
        remote = _is_remote_desktop_window(window)
        compact: dict[str, Any] = {
            "step": step.get("step"),
            "action": kind,
        }
        if step.get("time"):
            compact["time"] = step["time"]
        if step.get("capture_timing"):
            compact["capture_quality"] = step["capture_timing"].get("quality")
        confidence = step.get("target_confidence")
        if confidence and confidence != "verified":
            compact["confidence"] = confidence
        app: dict[str, Any] = {}
        if window.get("process_name"):
            app["process"] = window.get("process_name")
        if window.get("title"):
            app["title"] = window.get("title")
        if app:
            compact["app"] = app
        compact["surface"] = {
            "kind": "remote_desktop" if remote else "local_window",
            "targeting": "raster_only" if remote else "local_uia_candidate_or_raster",
        }
        # Local UIA inside mstsc describes the local input-capture shell,
        # not controls inside the application running on the remote host.
        target = None if remote else self._agent_best_target(step)
        if target:
            compact["target"] = target
        compact["confidence"] = ("remote_raster_only" if remote else
                                 confidence if confidence not in (None, "verified", "sampled_after_event")
                                 else "sampled_after_event" if target else "unresolved")
        compact["target_source"] = ("remote_pixels_only" if remote else
                                    "uia_sample_after_event" if target else "none")
        compact["targeting"] = ("remote_image_requires_visual_interpretation" if remote else
                                "semantic_candidate_requires_reobservation" if target else
                                "raster_evidence_requires_reobservation")
        compact["uncertainties"] = ["action_recipient_not_event_bound"]
        if remote:
            compact["uncertainties"].append("remote_controls_not_exposed_by_local_uia")
        sampled = None if remote else self._agent_target_projection(step.get("uia"))
        if sampled:
            compact["sampled_target"] = sampled
        focus_after = None if remote else self._agent_target_projection(step.get("focus_after"))
        compact["observed_after"] = {
            "focus_target": focus_after,
            "source": ("remote_raster_window_only" if remote
                       else "uia_focus_sample_after_event"),
            "effect_verified": False,
        }
        if (step.get("focus_after") or {}).get("sampled_at"):
            compact["observed_after"]["sampled_at"] = step["focus_after"]["sampled_at"]
        after_window = (step.get("focus_after") or {}).get("window") or {}
        after_app = {dest: after_window[src] for src, dest in (("process_name", "process"), ("title", "title")) if after_window.get(src)}
        if after_app:
            compact["observed_after"]["app"] = after_app
        if (step.get("uia") or {}).get("sampled_at"):
            compact["target_sampled_at"] = step["uia"]["sampled_at"]
        if step.get("capture_timing"):
            compact["capture_timing"] = dict(step["capture_timing"])
            selection = step["capture_timing"].get("selection")
            compact["screenshot_relation"] = "before_input" if selection == "before_input_no_clean_after" else "after_input"
            if selection == "before_input_no_clean_after":
                compact["uncertainties"].append("no_clean_post_input_frame")
            elif selection == "early_after_input_before_next":
                compact["uncertainties"].append("frame_before_requested_settle")
            elif selection == "buffer_unavailable":
                compact["uncertainties"].append("live_capture_after_processing_delay")
        if step.get("capture_binding"):
            compact["capture_binding"] = dict(step["capture_binding"])
            capture = step["capture_binding"]
            compact["raster_size"] = capture.get("frame_size")
            if remote:
                compact["raster_coordinate_space"] = (
                    "recorded_image_pixels_not_current_action_coordinates")
        if step.get("screenshot_error"):
            compact["screenshot_error"] = step["screenshot_error"]
        if step.get("screenshot") and compact.get("screenshot_relation") != "before_input":
            compact["observed_after"]["screenshot"] = step["screenshot"]
        else:
            compact["uncertainties"].append("no_result_screenshot")

        if kind in {"click", "double_click"}:
            compact["button"] = action.get("button")
            compact["at"] = {"x": action.get("x"), "y": action.get("y")}
            compact["image_at"] = self._recorded_image_point(step, compact["at"])
        elif kind == "drag":
            compact["button"] = action.get("button")
            compact["from"] = action.get("from")
            compact["to"] = action.get("to")
            compact["image_from"] = self._recorded_image_point(step, action.get("from"))
            compact["image_to"] = self._recorded_image_point(step, action.get("to"))
        elif kind == "scroll":
            compact["direction"] = action.get("direction")
            compact["events"] = action.get("events")
            compact["delta"] = action.get("delta")
            compact["horizontal"] = bool(action.get("horizontal"))
            compact["axis"] = "horizontal" if action.get("horizontal") else "vertical"
            compact["delta_unit"] = "windows_wheel_delta"
            compact["duration_ms"] = action.get("duration_ms")
            compact["at"] = {"x": action.get("x"), "y": action.get("y")}
            compact["image_at"] = self._recorded_image_point(step, compact["at"])
        elif kind == "keyboard_input":
            text_value = action.get("text")
            compact["action"] = "type" if text_value not in (None, "", "<not captured>") else "key"
            if text_value not in (None, "", "<not captured>"):
                compact["text"] = text_value
            if action.get("text_redacted"):
                compact["text_redacted"] = True
                compact["text_length"] = action.get("text_length")
            keys = [str(v) for v in (action.get("safe_keys") or [])]
            shortcuts = [str(v) for v in (action.get("shortcuts") or [])]
            if keys:
                compact["keys"] = keys
            if shortcuts:
                compact["shortcuts"] = shortcuts
            if action.get("input_sequence"):
                compact["input_sequence"] = list(action["input_sequence"])
        if step.get("screenshot"):
            compact["screenshot"] = step.get("screenshot")
        if step.get("pre_input_screenshot"):
            compact["pre_input_screenshot"] = step["pre_input_screenshot"]
            compact["pre_input_relation"] = "before_input_not_result"
            compact["pre_input_age_ms"] = (step.get("pre_input_capture") or {}).get("age_ms")
        if step.get("pre_input_screenshot_error"):
            compact["pre_input_screenshot_error"] = step["pre_input_screenshot_error"]
        return compact

    def _append_agent_step(self, step: dict[str, Any]) -> None:
        compact = self._agent_step_projection(step)
        # A screenshot may already include the next input. Mark that fact in
        # the trace saved after each step; never pretend it is an isolated effect.
        for previous in reversed(self._agent_steps):
            timing = previous.get("capture_timing") or {}
            if not previous.get("screenshot") or not timing.get("capture_started_at") or not compact.get("time"):
                continue
            completed = datetime.fromisoformat(timing["capture_started_at"]).timestamp() + float(timing.get("capture_duration_ms", 0)) / 1000
            if completed < datetime.fromisoformat(compact["time"]).timestamp():
                break
            if "capture_overlaps_later_input" not in previous.get("uncertainties", []):
                previous.setdefault("uncertainties", []).append("capture_overlaps_later_input")
            timing["overlaps_later_steps"] = list(dict.fromkeys(timing.get("overlaps_later_steps", []) + [compact.get("step")]))
        self._agent_steps.append(compact)

    @staticmethod
    def _procedure_target_label(target: dict[str, Any] | None) -> str | None:
        if not isinstance(target, dict):
            return None
        for key in ("name", "context_label", "automation_id", "class_name", "role"):
            value = _bounded_text(target.get(key), 180)
            if value:
                return value
        return None

    @staticmethod
    def _procedure_target_role(target: dict[str, Any] | None) -> str:
        if not isinstance(target, dict):
            return ""
        return _bounded_text(target.get("role"), 80)

    @staticmethod
    def _procedure_direct_input_target(target: dict[str, Any] | None) -> bool:
        if not isinstance(target, dict):
            return False
        role = _bounded_text(target.get("role"), 80)
        if role not in {"EditControl", "ComboBoxControl", "SpinnerControl", "DocumentControl"}:
            return False
        bounds = target.get("bounds") if isinstance(target.get("bounds"), dict) else None
        if bounds:
            try:
                width = int(bounds.get("width") or 0)
                height = int(bounds.get("height") or 0)
            except Exception:
                return False
            # Tiny synthetic focus/readout elements occur in custom grids and
            # canvases. Treating them as ordinary form fields would invent a
            # fill operation the user did not actually perform.
            if width <= 2 or height <= 2:
                return False
        return True

    @staticmethod
    def _procedure_same_target(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        matched = False
        for key in ("automation_id", "name", "role"):
            av = _bounded_text(a.get(key), 180)
            bv = _bounded_text(b.get(key), 180)
            if av and bv:
                if av != bv:
                    return False
                if key != "role":
                    matched = True
        ar = a.get("bounds") if isinstance(a.get("bounds"), dict) else None
        br = b.get("bounds") if isinstance(b.get("bounds"), dict) else None
        if ar and br:
            try:
                acx = int(ar.get("x") or 0) + int(ar.get("width") or 0) // 2
                acy = int(ar.get("y") or 0) + int(ar.get("height") or 0) // 2
                bcx = int(br.get("x") or 0) + int(br.get("width") or 0) // 2
                bcy = int(br.get("y") or 0) + int(br.get("height") or 0) // 2
                if abs(acx - bcx) > 8 or abs(acy - bcy) > 8:
                    return False
                return matched or (ar == br and bool(a.get("role")))
            except Exception:
                return False
        return matched

    @staticmethod
    def _procedure_app(step: dict[str, Any]) -> dict[str, Any] | None:
        app = step.get("app") if isinstance(step.get("app"), dict) else None
        return dict(app) if app else None

    def _procedure_visual_change(
        self,
        session_dir: Path,
        before_rel: str | None,
        after_rel: str | None,
    ) -> dict[str, Any] | None:
        if not before_rel or not after_rel or before_rel == after_rel:
            return None
        before_path = session_dir / before_rel
        after_path = session_dir / after_rel
        if not before_path.is_file() or not after_path.is_file():
            return None
        try:
            with Image.open(before_path) as before_img, Image.open(after_path) as after_img:
                before = before_img.convert("L").resize((160, 90))
                after = after_img.convert("L").resize((160, 90))
                diff = ImageChops.difference(before, after)
                hist = diff.histogram()
                total = max(1, sum(hist))
                changed = sum(count for level, count in enumerate(hist) if level >= 12)
                mean_delta = sum(level * count for level, count in enumerate(hist)) / total / 255.0
                return {
                    "changed_fraction": round(changed / total, 4),
                    "mean_pixel_delta": round(mean_delta, 4),
                }
        except Exception:
            return None

    def _procedure_observed_effect(
        self,
        session_dir: Path,
        previous_state: dict[str, Any] | None,
        current_state: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not previous_state:
            return None
        effect: dict[str, Any] = {}
        before_app = previous_state.get("app") if isinstance(previous_state.get("app"), dict) else {}
        after_app = current_state.get("app") if isinstance(current_state.get("app"), dict) else {}
        if before_app.get("process") and after_app.get("process") and before_app["process"] != after_app["process"]:
            effect["application_changed"] = True
        if before_app.get("title") and after_app.get("title") and before_app["title"] != after_app["title"]:
            effect["window_title_changed"] = True
        before_label = self._procedure_target_label(previous_state.get("focus_target"))
        after_label = self._procedure_target_label(current_state.get("focus_target"))
        if before_label and after_label and before_label != after_label:
            # A readout label can change without focus moving (e.g. grid cell
            # values). Report the measured label delta, not inferred identity.
            effect["focus_label_changed"] = True
            effect["focus_label_after"] = after_label
        visual = self._procedure_visual_change(
            session_dir,
            previous_state.get("screenshot"),
            current_state.get("screenshot"),
        )
        if visual:
            effect["visual_change"] = visual
        return effect or None

    def _procedure_state_from_step(self, step: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        observed = step.get("observed_after")
        if isinstance(observed, dict):
            state.update(observed)
        else:
            # Legacy traces did not distinguish action target and later focus.
            state["source"] = "legacy_screenshot_only"
            state["effect_verified"] = False
        if step.get("capture_timing"):
            state["capture_timing"] = dict(step["capture_timing"])
        if step.get("capture_binding"):
            state["capture_binding"] = dict(step["capture_binding"])
        if step.get("screenshot") and step.get("screenshot_relation") != "before_input":
            state["screenshot"] = step["screenshot"]
        return state

    def _procedure_operation_from_step(self, step: dict[str, Any]) -> dict[str, Any]:
        action = str(step.get("action") or "unknown")
        target = step.get("target") if isinstance(step.get("target"), dict) else None
        role = self._procedure_target_role(target)
        label = self._procedure_target_label(target)
        operation: dict[str, Any] = {
            "operation": action,
            "evidence_steps": [step.get("step")],
        }
        if isinstance(step.get("app"), dict):
            operation["app"] = dict(step["app"])
        if target:
            operation["target"] = dict(target)

        if action == "type":
            direct_input = (self._procedure_direct_input_target(target)
                            and step.get("confidence") in (None, "verified"))
            operation["operation"] = "fill" if direct_input else "type"
            if direct_input and label:
                operation["field"] = label
            if "text" in step:
                operation["demonstrated_value"] = step.get("text")
                operation["value_semantics"] = "demonstrated_value_not_inferred_rule"
            if step.get("text_redacted"):
                operation["value_redacted"] = True
                operation.pop("demonstrated_value", None)
            if step.get("keys"):
                operation["commit_keys"] = list(step.get("keys") or [])
            if step.get("shortcuts"):
                operation["shortcuts"] = list(step.get("shortcuts") or [])
        elif action == "key":
            operation["operation"] = "key"
            if step.get("keys"):
                operation["keys"] = list(step.get("keys") or [])
            if step.get("shortcuts"):
                operation["shortcuts"] = list(step.get("shortcuts") or [])
        elif action in {"click", "double_click"}:
            primary_click = (action == "click" and step.get("button", "left") == "left"
                             and step.get("confidence") in (None, "verified"))
            if primary_click and role == "CheckBoxControl":
                operation["operation"] = "toggle"
            elif primary_click and role in {"RadioButtonControl", "ListItemControl"}:
                operation["operation"] = "choose"
            elif primary_click and role in {"ButtonControl", "HyperlinkControl", "MenuItemControl", "TabItemControl", "TreeItemControl"}:
                operation["operation"] = "activate"
            else:
                operation["operation"] = action
            if label:
                operation["target_label"] = label
            if step.get("button"):
                operation["button"] = step.get("button")
            if isinstance(step.get("at"), dict):
                operation["at"] = dict(step["at"])
        elif action == "drag":
            operation["operation"] = "drag"
            operation["from"] = step.get("from")
            operation["to"] = step.get("to")
        elif action == "scroll":
            operation["operation"] = "scroll"
            operation["direction"] = step.get("direction")
            operation["events"] = step.get("events")
            for key in ("delta", "horizontal", "axis", "delta_unit", "duration_ms"):
                if key in step:
                    operation[key] = step[key]
            if isinstance(step.get("at"), dict):
                operation["at"] = dict(step["at"])
        if step.get("confidence"):
            operation["confidence"] = step.get("confidence")
        for key in ("target_source", "target_sampled_at", "targeting", "sampled_target",
                    "uncertainties", "screenshot_relation", "surface",
                    "image_at", "image_from", "image_to", "raster_size",
                    "pre_input_screenshot", "pre_input_relation", "pre_input_age_ms"):
            if key in step:
                operation[key] = step[key]
        if step.get("input_sequence"):
            operation["input_sequence"] = list(step["input_sequence"])
        if step.get("capture_quality"):
            operation["capture_quality"] = step["capture_quality"]
        if step.get("screenshot"):
            operation["evidence_screenshot"] = step["screenshot"]
        return operation

    def _recording_evidence_coverage(self, session_dir: Path) -> dict[str, Any]:
        """Summarize existing evidence without promoting it to verified outcomes."""
        source = self._agent_steps
        requested = (self._session or {}).get("capture", {}).get("screenshots")
        images = {"present_files": 0, "after_input": 0, "before_input": 0,
                  "missing_files": 0, "no_reference": 0, "early_after_input": 0,
                  "live_fallback": 0, "overlaps_later_input": 0,
                  "late_or_window_changed": 0, "capture_errors": 0,
                  "relation_unknown": 0, "pre_input_context_present": 0,
                  "pre_input_context_missing": 0, "pre_input_context_errors": 0}
        uia = {"with_target_candidate": 0, "without_target_candidate": 0,
               "with_sampled_focus": 0}
        for step in source:
            timing = step.get("capture_timing") or {}
            reference = step.get("screenshot")
            if not reference:
                images["no_reference"] += 1
            elif not (session_dir / reference).is_file():
                images["missing_files"] += 1
            else:
                images["present_files"] += 1
                relation = step.get("screenshot_relation")
                images[relation if relation in {"before_input", "after_input"} else "relation_unknown"] += 1
                if timing.get("selection") == "early_after_input_before_next":
                    images["early_after_input"] += 1
                if timing.get("source") == "live_fallback":
                    images["live_fallback"] += 1
                if timing.get("overlaps_later_steps"):
                    images["overlaps_later_input"] += 1
                if step.get("capture_quality") in {"late", "window_changed"}:
                    images["late_or_window_changed"] += 1
            if step.get("screenshot_error"):
                images["capture_errors"] += 1
            before_ref = step.get("pre_input_screenshot")
            if before_ref:
                images["pre_input_context_present" if (session_dir / before_ref).is_file()
                       else "pre_input_context_missing"] += 1
            if step.get("pre_input_screenshot_error"):
                images["pre_input_context_errors"] += 1
            uia["with_target_candidate" if step.get("target") or step.get("sampled_target") else "without_target_candidate"] += 1
            if (step.get("observed_after") or {}).get("focus_target"):
                uia["with_sampled_focus"] += 1
        return {
            "scope": "exported_trace_only_not_proof_of_complete_physical_input_or_business_success",
            "agent_steps": len(source), "full_trace_steps": len(self._steps),
            "screenshots_requested": requested,
            "screenshots": images, "uia": uia,
            "dropped_raw_events": self._dropped_events,
            "dropped_semantic_steps": self._dropped_semantic_steps,
            "processing_errors": (dict(self._processing_errors)
                                  if "processing_errors" in (self._session or {}) else None),
        }

    def _build_agent_handoff(self, procedure: dict[str, Any]) -> dict[str, Any]:
        """Small contextual navigation index, not a second source of truth.

        The conversation supplies the request/intent. This index shows where
        to inspect pixels and records the demonstrated input without guessing
        the meaning of an unlabeled remote menu.
        """
        segments: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        for step in self._agent_steps:
            app = step.get("app") or {}
            surface = (step.get("surface") or {}).get("kind", "unknown")
            process = app.get("process") or "unknown"
            number = step.get("step")
            if not segments or (segments[-1]["surface"], segments[-1]["process"]) != (surface, process):
                segments.append({
                    "first_step": number, "last_step": number,
                    "surface": surface, "process": process,
                    "first_image": step.get("screenshot"),
                })
            else:
                segments[-1]["last_step"] = number
            segments[-1]["last_image"] = step.get("screenshot")
            entry: dict[str, Any] = {
                "step": number, "action": step.get("action"),
                "surface": surface,
                "image": step.get("screenshot"),
                "image_relation": step.get("screenshot_relation"),
            }
            if step.get("capture_quality"):
                entry["image_quality"] = step["capture_quality"]
            if step.get("pre_input_screenshot"):
                entry["before_image"] = step["pre_input_screenshot"]
                entry["before_image_relation"] = "before_input_not_result"
                entry["before_image_age_ms"] = step.get("pre_input_age_ms")
            capture_selection = (step.get("capture_timing") or {}).get("selection")
            if capture_selection:
                entry["capture_selection"] = capture_selection
                if capture_selection == "early_after_input_before_next":
                    entry["image_warning"] = "after_input_but_not_settled"
                elif capture_selection == "before_input_no_clean_after":
                    entry["image_warning"] = "context_before_action_not_result"
            if step.get("raster_size"):
                entry["image_size"] = step["raster_size"]
            if step.get("button"):
                entry["button"] = step["button"]
            for key in ("image_at", "image_from", "image_to"):
                if step.get(key) is not None:
                    entry[key] = step[key]
            if step.get("target") and surface != "remote_desktop":
                entry["local_target_candidate"] = {
                    key: value for key, value in step["target"].items()
                    if key in ("name", "role", "automation_id")
                }
            if step.get("direction"):
                entry["direction"] = step["direction"]
            if step.get("delta") is not None:
                entry["scroll_delta"] = step["delta"]
            if step.get("keys"):
                entry["keys"] = step["keys"]
            if step.get("shortcuts"):
                entry["shortcuts"] = step["shortcuts"]
            if step.get("text_redacted"):
                entry["text_redacted"] = True
            elif step.get("text") not in (None, "", "<not captured>"):
                entry["demonstrated_text"] = step["text"]
            if surface == "remote_desktop":
                entry["target_evidence"] = "screenshot_only_not_local_uia"
            if entry.get("image_relation") == "before_input":
                entry["image_warning"] = "context_before_action_not_result"
            entries.append(entry)
        return {
            "format_version": 1,
            "kind": "contextual_user_demonstration_index",
            "input_source": "human_physical_events",
            "context_source": "user_conversation",
            "context_rule": ("Use the accompanying user message to identify the relevant "
                             "steps and intended task. This recording demonstrates HOW; "
                             "it does not independently define WHY."),
            "source_of_truth": {
                "procedure": "procedure.json",
                "machine_steps": "agent_steps.json",
                "full_trace": "steps.json",
            },
            "segments": segments,
            "steps": entries,
            "trace_integrity": procedure.get("trace_integrity"),
            "limitations": [
                "Image positions are historical pixels, not current-state action authority.",
                "Local RDP UIA cannot identify controls inside the remote application.",
                "After-input screenshot is visual evidence, not verified task success.",
                "Screenshots before input must never be treated as an effect.",
            ],
        }

    def _build_procedure(self, session_dir: Path) -> dict[str, Any]:
        source = [dict(step) for step in self._agent_steps]
        operations: list[dict[str, Any]] = []
        i = 0
        while i < len(source):
            current = source[i]
            action = str(current.get("action") or "")
            target = current.get("target") if isinstance(current.get("target"), dict) else None
            role = self._procedure_target_role(target)

            # Generic field interaction: click/focus a concrete control and then
            # type into the same semantic target. Preserve both source steps as
            # evidence but present one fill operation to the executing agent.
            if (action == "click" and current.get("button", "left") == "left" and i + 1 < len(source)
                and current.get("confidence") in (None, "verified") and source[i + 1].get("confidence") in (None, "verified")):
                nxt = source[i + 1]
                next_target = nxt.get("target") if isinstance(nxt.get("target"), dict) else None
                if (
                    nxt.get("action") == "type"
                    and current.get("app") == nxt.get("app")
                    and (
                        self._procedure_direct_input_target(target)
                        or self._procedure_direct_input_target(next_target)
                    )
                    and self._procedure_same_target(target, next_target)
                ):
                    merged = self._procedure_operation_from_step(nxt)
                    if target and not merged.get("target"):
                        merged["target"] = dict(target)
                    label = self._procedure_target_label(merged.get("target") if isinstance(merged.get("target"), dict) else target)
                    if label:
                        merged["field"] = label
                    merged["evidence_steps"] = [current.get("step"), nxt.get("step")]
                    merged["interaction"] = "focus_then_type"
                    if nxt.get("screenshot"):
                        merged["after_screenshot"] = nxt.get("screenshot")
                    operations.append(merged)
                    i += 2
                    continue

            # Generic selection pattern: activate a ComboBox, then choose one
            # option-like item. This is based solely on UI roles, not product or
            # application-specific vocabulary.
            if (action == "click" and current.get("button", "left") == "left" and role == "ComboBoxControl" and i + 1 < len(source)
                and current.get("confidence") in (None, "verified") and source[i + 1].get("confidence") in (None, "verified")):
                nxt = source[i + 1]
                next_target = nxt.get("target") if isinstance(nxt.get("target"), dict) else None
                next_role = self._procedure_target_role(next_target)
                if (nxt.get("action") == "click" and nxt.get("button", "left") == "left"
                    and current.get("app") == nxt.get("app")
                    and next_role in {"ListItemControl", "RadioButtonControl", "MenuItemControl"}):
                    selection: dict[str, Any] = {
                        "operation": "select",
                        "evidence_steps": [current.get("step"), nxt.get("step")],
                    }
                    if isinstance(current.get("app"), dict):
                        selection["app"] = dict(current["app"])
                    if target:
                        selection["target"] = dict(target)
                    field = self._procedure_target_label(target)
                    choice = self._procedure_target_label(next_target)
                    if field:
                        selection["field"] = field
                    if choice:
                        selection["demonstrated_choice"] = choice
                        selection["value_semantics"] = "demonstrated_value_not_inferred_rule"
                    if nxt.get("screenshot"):
                        selection["after_screenshot"] = nxt.get("screenshot")
                    if current.get("confidence") or nxt.get("confidence"):
                        selection["confidence"] = nxt.get("confidence") or current.get("confidence")
                    operations.append(selection)
                    i += 2
                    continue

            operations.append(self._procedure_operation_from_step(current))
            i += 1

        # Preserve an observable post-action state for each operation. This is
        # evidence only: visual change is never promoted to a business-success
        # claim by the recorder.
        previous_state: dict[str, Any] | None = None
        by_step = {step.get("step"): step for step in source}
        for index, operation in enumerate(operations, 1):
            operation["procedure_step"] = index
            if previous_state and previous_state.get("screenshot"):
                operation["prior_step_context"] = {
                    "screenshot": previous_state["screenshot"],
                    "source": "preceding_observed_step_not_action_start",
                    "time_bound_to_current_action": False,
                }
            evidence_steps = [value for value in operation.get("evidence_steps", []) if value in by_step]
            last_source = by_step[evidence_steps[-1]] if evidence_steps else None
            if last_source:
                evidence = [by_step[step] for step in evidence_steps]
                uncertain = next((step.get("confidence") for step in evidence if step.get("confidence") not in (None, "verified")), None)
                bad_capture = next((step.get("capture_quality") for step in evidence if step.get("capture_quality") in {"late", "window_changed", "before_input"}), None)
                if uncertain:
                    operation["confidence"] = uncertain
                if bad_capture:
                    operation["capture_quality"] = bad_capture
                state = self._procedure_state_from_step(last_source)
                if state:
                    operation["observed_after"] = state
                    operation.pop("after_screenshot", None)
                    effect = self._procedure_observed_effect(session_dir, previous_state, state)
                    if effect:
                        operation["observed_change"] = effect
                    previous_state = state

        applications: list[dict[str, Any]] = []
        for step in source:
            app = self._procedure_app(step)
            if app and app not in applications:
                applications.append(app)

        demonstrated_inputs: list[dict[str, Any]] = []
        ambiguities: list[dict[str, Any]] = []
        for operation in operations:
            if operation.get("operation") in {"fill", "type"}:
                item: dict[str, Any] = {
                    "procedure_step": operation.get("procedure_step"),
                    "field": operation.get("field"),
                    "target_confidence": operation.get("confidence", "unknown"),
                    "kind": "text",
                }
                if operation.get("value_redacted"):
                    item["value_redacted"] = True
                elif "demonstrated_value" in operation:
                    item["demonstrated_value"] = operation.get("demonstrated_value")
                demonstrated_inputs.append(item)
            elif operation.get("operation") == "select" and operation.get("field"):
                demonstrated_inputs.append({
                    "procedure_step": operation.get("procedure_step"),
                    "field": operation.get("field"),
                    "kind": "selection",
                    "demonstrated_value": operation.get("demonstrated_choice"),
                })
            op_kind = str(operation.get("operation") or "")
            for reason in operation.get("uncertainties", []):
                ambiguities.append({"procedure_step": operation.get("procedure_step"), "reason": reason})
            if op_kind in {"click", "double_click", "drag", "scroll", "type", "key", "fill", "select", "toggle", "choose", "activate"} and not operation.get("target"):
                ambiguities.append({"procedure_step": operation.get("procedure_step"), "reason": "no_semantic_target"})
            elif operation.get("confidence") and operation.get("confidence") != "verified":
                ambiguities.append({"procedure_step": operation.get("procedure_step"), "reason": "target_not_fully_verified"})
            if operation.get("capture_quality") in {"late", "window_changed", "before_input"}:
                ambiguities.append({"procedure_step": operation.get("procedure_step"), "reason": "unreliable_post_action_capture"})

        return {
            "format_version": 4,
            "kind": "demonstrated_procedure",
            "source_trace": "agent_steps.json",
            "generated_by": {"version": VERSION, "build_id": BUILD_ID},
            "applications": applications,
            "intent": dict((self._session or {}).get("intent") or recording_intent()),
            "evidence_coverage": self._recording_evidence_coverage(session_dir),
            "evidence_availability": operational_evidence_availability(
                operations, source="human"),
            "procedure_quality": {
                "operation_count": len(operations),
                "intent_policy": "user_conversation_supplies_task_goal_recording_goal_optional",
                "result_evidence": "screenshots_and_sampled_focus_not_business_success",
                "missing_intent": [key for key in ("goal", "prerequisites", "success_criteria") if not (self._session or {}).get("intent", {}).get(key)],
                "ambiguity_count": len(ambiguities),
                "dropped_raw_events": self._dropped_events,
                "dropped_semantic_steps": self._dropped_semantic_steps,
                "intent_complete": bool((self._session or {}).get("intent", {}).get("goal") and (self._session or {}).get("intent", {}).get("success_criteria")),
            },
            "execution_guidance": {
                "context": "Interpret this recording with the accompanying user message. Blank recording-intent fields are normal and not failures.",
                "targeting": "prefer semantic target; recorded coordinates are layout-specific fallback evidence",
                "values": "demonstrated values and choices are examples from this run, not inferred business rules",
                "outcomes": "observed changes are evidence only; the recorder does not infer business success",
                "provenance": "target and sampled_target are asynchronous UIA candidates, not event-bound recipients; observed_after.focus_target is later focus, not the click or typing destination",
                "timing": "capture_overlaps_later_input means the screenshot may include subsequent steps; consult capture_timing and do not attribute all visible changes to this operation",
                "intent": "missing_intent lists optional recording fields not supplied; the user chat may already contain the goal. Never treat blank recording fields as a quality defect.",
                "coverage": "check evidence_coverage: missing images, before-input context, live fallbacks, dropped events and processing errors are distinct; UIA candidates are not verified recipients",
                "fallback": "consult agent_steps.json and screenshots when an operation is ambiguous; use steps.json only for diagnostics",
            },
            "demonstrated_inputs": demonstrated_inputs,
            "ambiguities": ambiguities,
            "operations": operations,
        }

    def _enrich_step(self, item: dict[str, Any]) -> None:
        action = dict(item["action"])
        epoch = float(item["epoch"])
        point = item.get("point")
        focus = bool(item.get("focus"))
        event_context = item.get("event_context") if isinstance(item.get("event_context"), dict) else None
        window = self._event_window_for_step(event_context)
        remaining = max(0.0, self._settle_seconds - max(0.0, time.time() - epoch))
        if remaining:
            time.sleep(remaining)
        # Pick immutable pixels before any expensive UIA work. Correlation and
        # enrichment delays no longer determine when those pixels were read.
        buffered_frame = self._frame_buffer.select(epoch, self._settle_seconds) if self._frame_buffer else None
        early_frame = item.get("buffered_frame_at_emit")
        if early_frame:
            priority = {"settled_after_input": 3, "early_after_input_before_next": 2,
                        "before_input_no_clean_after": 1}
            a = priority.get(early_frame.get("selection"), 0)
            b = priority.get(buffered_frame.get("selection"), 0) if buffered_frame else 0
            if a > b or (a == b and buffered_frame and
                          early_frame["completed_epoch"] < buffered_frame["completed_epoch"]):
                buffered_frame = early_frame

        live_window = _compact_window(focused_window() if focus or point is None else window_at(point["x"], point["y"]))
        event_key = _event_context_key(event_context)
        live_key = _event_context_key(live_window)
        identity_match = bool(event_key != (0, 0) and live_key == event_key)

        remote = _is_remote_desktop_window(window)
        if identity_match and remote:
            # A local RDP Input Capture Window is not a remote control.
            # Do not spend UIA time sampling a misleading semantic target.
            uia = {"target": None, "stack": [], "reason": "remote_content_raster_only"}
        elif identity_match:
            uia = uia_context_at_focus() if focus or point is None else uia_context_at_point(point["x"], point["y"])
            if not isinstance(uia, dict):
                uia = {"target": None, "stack": []}
        else:
            uia = {
                "target": None,
                "stack": [],
                "stale": True,
                "reason": "event_window_changed_before_enrichment",
                "event_hwnd": event_key[0] or None,
                "live_hwnd": live_key[0] or None,
            }
        target = uia.get("target") if identity_match else None
        uia["sampled_at"] = _event_time_iso()
        target_confidence = ("remote_raster_only" if identity_match and remote else
                             "sampled_after_event" if identity_match and isinstance(target, dict) else
                             "window_only" if identity_match else "stale")

        if self._window_targets_ignored_ui(window):
            return
        if self._is_recorder_restore_shell_step(
            action, epoch, window, target if isinstance(target, dict) else None
        ):
            return

        focus_after: dict[str, Any] | None = None
        try:
            focused_after_window = _compact_window(focused_window())
            if (not remote and event_key != (0, 0)
                    and _event_context_key(focused_after_window) == event_key):
                candidate = uia_context_at_focus()
                if isinstance(candidate, dict):
                    candidate["sampled_at"] = _event_time_iso()
                    candidate["window"] = focused_after_window
                    focus_after = candidate
        except Exception:
            focus_after = None

        def _uia_contains_password(context: dict[str, Any] | None) -> bool:
            if not isinstance(context, dict):
                return False
            candidate = context.get("target")
            if isinstance(candidate, dict) and candidate.get("is_password"):
                return True
            return any(
                bool(node.get("is_password"))
                for node in (context.get("stack") or [])
                if isinstance(node, dict)
            )

        prior_sensitive_focus = bool(self._recording_sensitive_focus)
        current_sensitive_focus = _uia_contains_password(uia) or _uia_contains_password(focus_after)
        unknown_focus = (not identity_match or not uia_text_capture_safe(uia)
                         or bool(_surface_context(window).get("remote")))
        if action.get("type") == "keyboard_input" and self._capture_text and (prior_sensitive_focus or current_sensitive_focus or unknown_focus):
            raw_text = str(action.get("text") or "")
            action["text_length"] = len(raw_text)
            action["text"] = "<redacted>"
            action["text_redacted"] = True
            action["redaction_reason"] = "password_or_unverified_focus"
            action["input_sequence"] = [
                {"kind": "text", "text_redacted": True, "text_length": len(str(token.get("text") or ""))}
                if token.get("kind") == "text" else dict(token)
                for token in action.get("input_sequence", [])
            ]
        self._recording_sensitive_focus = _uia_contains_password(focus_after) if focus_after is not None else current_sensitive_focus

        with self._lock:
            if not self._session:
                return
            number = len(self._steps) + 1
            session_dir = self._session_dir
            if session_dir is None:
                return

        screenshot_rel = None
        before_image_rel = None
        before_image_binding = None
        before_image_error = None
        capture_binding = None
        screenshot_error = None
        capture_started_epoch = time.time()
        capture_completed_epoch = capture_started_epoch
        if self._capture_screenshots:
            screenshot_rel = f"screenshots/step_{number:04d}.jpg"
            try:
                capture_binding = _capture_recording_frame(
                    session_dir / screenshot_rel,
                    point=point,
                    target_rect=None if buffered_frame else (target or {}).get("bounding_rect") if isinstance(target, dict) else None,
                    window=window,
                    capture_scope=self._capture_scope,
                    buffered_frame=buffered_frame,
                )
                frame_timing = capture_binding["frame_timing"]
                capture_started_epoch = frame_timing["started_epoch"]
                capture_completed_epoch = frame_timing["completed_epoch"]
            except Exception as exc:
                capture_completed_epoch = time.time()
                screenshot_error = str(exc)
                screenshot_rel = None

        context_frame = item.get("buffered_before_context_selection")
        if self._capture_screenshots and context_frame and session_dir is not None:
            before_image_rel = f"screenshots/step_{number:04d}_before.jpg"
            try:
                before_image_binding = _capture_recording_frame(
                    session_dir / before_image_rel, point=None, target_rect=None,
                    window=window, capture_scope=self._capture_scope,
                    buffered_frame=context_frame,
                )
            except Exception as exc:
                before_image_error = type(exc).__name__
                before_image_rel = None
        capture_lag_ms = int(round((capture_started_epoch - epoch) * 1000))
        capture_duration_ms = max(0, int(round((capture_completed_epoch - capture_started_epoch) * 1000)))
        live_at_capture = _compact_window(focused_window() if focus or point is None else window_at(point["x"], point["y"]))
        capture_identity_match = (_event_context_key(live_at_capture) == event_key
                                  if not buffered_frame and event_key != (0, 0) else None)
        capture_quality = "ok"
        if capture_identity_match is False:
            capture_quality = "window_changed"
        elif capture_lag_ms > 1000:
            capture_quality = "late"
        if buffered_frame and buffered_frame["selection"] == "before_input_no_clean_after":
            capture_quality = "before_input"

        step = {
            "step": number,
            "time": _event_time_iso(epoch),
            "event_epoch": epoch,
            "action": action,
            "description": self._describe(action, target if isinstance(target, dict) else None, window),
            "window": window,
            "event_context": event_context,
            "uia": uia,
            "focus_after": focus_after,
            "target_confidence": target_confidence,
            "screenshot": screenshot_rel,
            "pre_input_screenshot": before_image_rel,
            "pre_input_capture": {
                "relation": "before_input_not_result",
                "selection": "immediately_before_input",
                "started_at": _event_time_iso(context_frame["started_epoch"]),
                "completed_at": _event_time_iso(context_frame["completed_epoch"]),
                "age_ms": int(round((epoch - context_frame["completed_epoch"]) * 1000)),
                "capture_binding": before_image_binding,
            } if before_image_rel and context_frame else None,
            "capture_binding": capture_binding,
            "capture_timing": {
                "capture_started_at": _event_time_iso(capture_started_epoch),
                "capture_lag_ms": capture_lag_ms,
                "capture_duration_ms": capture_duration_ms,
                "event_window_still_matches": capture_identity_match,
                "quality": capture_quality,
                "source": "buffered" if buffered_frame else "live_fallback",
                "selection": buffered_frame["selection"] if buffered_frame else "buffer_unavailable",
                "processing_completed_at": _event_time_iso(),
            },
        }
        if screenshot_error:
            step["screenshot_error"] = screenshot_error
        if before_image_error:
            step["pre_input_screenshot_error"] = before_image_error
        with self._lock:
            self._steps.append(step)
            self._append_agent_step(step)
            self._session["step_count"] = len(self._steps)
            self._session["agent_step_count"] = len(self._agent_steps)
            self._session["dropped_raw_events"] = self._dropped_events
            self._session["dropped_semantic_steps"] = self._dropped_semantic_steps
            self._session["raw_queue_peak"] = self._raw_queue_peak
            self._session["semantic_queue_peak"] = self._semantic_queue_peak
            self._session["processing_errors"] = dict(self._processing_errors)
            self._session["last_step_at"] = step["time"]
            # Constant-size persistence per step plus periodic full projections.
            # Rewriting all growing arrays after each event caused quadratic I/O.
            with (session_dir / "steps.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(step, ensure_ascii=False, separators=(",", ":")) + "\n")
            now = time.monotonic()
            if (len(self._steps) == 1 or len(self._steps) % 24 == 0
                    or now - self._last_progress_snapshot >= 2.0):
                _atomic_write_json(session_dir / "steps.json", self._steps)
                _atomic_write_json(session_dir / "agent_steps.json", self._agent_steps)
                _atomic_write_json(session_dir / "session.json", self._session)
                self._last_progress_snapshot = now

    def _write_report(self, session_dir: Path) -> None:
        cards: list[str] = []
        for step in self._steps:
            image_html = f'<img src="{html.escape(str(step["screenshot"]))}" alt="Step {step["step"]} screenshot">' if step.get("screenshot") else '<p class="muted">No screenshot.</p>'
            details = html.escape(json.dumps(self._agent_step_projection(step), ensure_ascii=False, indent=2))
            cards.append(f"""<section class="step"><h2>Step {step["step"]}</h2><p><strong>{html.escape(str(step.get("description") or ""))}</strong></p><p class="muted">{html.escape(str(step.get("time") or ""))}</p>{image_html}<details><summary>Machine-readable context</summary><pre>{details}</pre></details></section>""")
        body = "\n".join(cards) or "<p>No steps were captured.</p>"
        keyboard_note = (
            "Typed text/formulas are stored for agent teaching; detected password controls and unverified focus are redacted. Screenshots may contain visible text."
            if self._capture_text
            else "Typed text is not stored."
        )
        intent = (self._session or {}).get("intent") or recording_intent()
        intent_html = "".join(f"<p><strong>{html.escape(key)}:</strong> {html.escape(value or '(not supplied)')}</p>"
                              for key, value in intent.items() if key != "source")
        page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Agent Computer Bridge Recording</title><style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:1400px;margin:32px auto;padding:0 20px;background:#111;color:#eee}}.step{{margin:0 0 48px;padding:20px;border:1px solid #444;border-radius:12px;background:#181818}}img{{max-width:100%;height:auto;border:1px solid #555}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#0d0d0d;padding:12px}}.muted{{color:#aaa}}</style></head><body><h1>Agent Computer Bridge User Recording</h1>{intent_html}<p>{html.escape(keyboard_note)} The expanded machine-readable context is the compact agent trace; full point-target and post-action focus evidence remains in steps.json.</p>{body}</body></html>"""
        (session_dir / "report.html").write_text(page, encoding="utf-8")
        (session_dir / "README.md").write_text(
            "# Agent Computer Bridge User Recording\n\n"
            "Open `report.html` for the human-readable view. Agents should combine the user chat with `agent_handoff.json` first, then inspect `procedure.json`, use `agent_steps.json` plus the referenced screenshots for exact demonstrated interactions, and use `steps.json` only as the full evidence/debug trace.\n\n"
            "Schema 8 / procedure 4: action coordinates and input_sequence are recorded input. target/sampled_target candidates are sampled after input and require fresh observation. observed_after.focus_target is later focus, never proof of the input destination. Check evidence_coverage, uncertainties, missing_intent and screenshot capture_timing before interpreting or reproducing a procedure. Scroll delta uses signed Windows wheel units, not pixels. Check screenshot_relation: before_input frames are context only, never action results. Pixel capture and UIA sampling have independent timestamps; formulas and business success are not inferred.\n\n"
            + (
                "Demonstration text: typed text and formulas are stored; detected password controls and unverified focus are redacted.\n"
                if self._capture_text else
                "Demonstration text: keyboard text capture was disabled for this recording.\n"
            )
            + "UI Automation remains read-only and ValuePattern values are not requested. Screenshots can contain text displayed on screen.\n",
            encoding="utf-8",
        )

    def _zip_session(self, session_dir: Path) -> Path:
        archive = session_dir.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for file in sorted(session_dir.rglob("*")):
                if file.is_file():
                    zf.write(file, arcname=str(Path(session_dir.name) / file.relative_to(session_dir)))
        return archive

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._active or not self._session:
                raise ValueError("no user recording is active")
            self._active = False
            hook = self._hook
            worker = self._worker
            enrichment_worker = self._enrichment_worker
            q = self._queue
            session_dir = self._session_dir
            frame_buffer = self._frame_buffer
        try:
            if session_dir is None:
                raise OSError("recording session directory is missing")
            if hook is not None:
                hook.stop()
            if q is not None:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    q.put(None, timeout=1.0)
            if worker is not None:
                worker.join(timeout=15.0)
                if worker.is_alive():
                    raise OSError("recording correlator did not stop cleanly")
            if enrichment_worker is not None:
                enrichment_worker.join(timeout=30.0)
                if enrichment_worker.is_alive():
                    raise OSError("recording enrichment worker did not stop cleanly")
        finally:
            if frame_buffer is not None:
                frame_buffer.stop()
        with self._lock:
            assert self._session is not None
            self._session["status"] = "completed"
            self._session["stopped_at"] = _event_time_iso()
            self._session["step_count"] = len(self._steps)
            self._session["agent_step_count"] = len(self._agent_steps)
            self._session["dropped_raw_events"] = self._dropped_events
            self._session["dropped_semantic_steps"] = self._dropped_semantic_steps
            self._session["raw_queue_peak"] = self._raw_queue_peak
            self._session["semantic_queue_peak"] = self._semantic_queue_peak
            self._session["processing_errors"] = dict(self._processing_errors)
            _atomic_write_json(session_dir / "steps.json", self._steps)
            _atomic_write_json(session_dir / "agent_steps.json", self._agent_steps)
            procedure = self._build_procedure(session_dir)
            _atomic_write_json(session_dir / "procedure.json", procedure)
            self._session["procedure_step_count"] = len(procedure.get("operations") or [])
            self._session["evidence_coverage"] = procedure["evidence_coverage"]
            coverage = procedure["evidence_coverage"]
            image_counts = coverage["screenshots"]
            reported_loss = bool(
                self._dropped_events or self._dropped_semantic_steps
                or any(self._processing_errors.values())
            )
            incomplete_images = bool(
                self._capture_screenshots and (
                    image_counts["missing_files"] or image_counts["no_reference"]
                    or image_counts["capture_errors"]
                )
            )
            self._session["trace_integrity"] = {
                "assessment": ("degraded" if reported_loss or incomplete_images
                               else "no_reported_processing_loss"),
                "reported_loss": reported_loss,
                "missing_requested_images": incomplete_images,
                "interpretation": (
                    "Completion means files were exported, not that every physical "
                    "event or the intended business outcome was captured."
                ),
            }
            procedure["trace_integrity"] = dict(self._session["trace_integrity"])
            _atomic_write_json(session_dir / "procedure.json", procedure)
            _atomic_write_json(session_dir / "agent_handoff.json", self._build_agent_handoff(procedure))
            _atomic_write_json(session_dir / "session.json", self._session)
            self._write_report(session_dir)
            # Complete final JSON is authoritative. Retain incremental log
            # only for interrupted recordings, not successful export ZIPs.
            (session_dir / "steps.jsonl").unlink(missing_ok=True)
            archive = self._zip_session(session_dir)
            result = {
                "ok": True,
                "session_id": self._session["session_id"],
                "session_dir": str(session_dir),
                "archive": str(archive),
                "step_count": len(self._steps),
                "agent_step_count": len(self._agent_steps),
                "procedure_step_count": int(self._session.get("procedure_step_count") or 0),
                "dropped_raw_events": self._dropped_events,
                "dropped_semantic_steps": self._dropped_semantic_steps,
                "trace_integrity": dict(self._session["trace_integrity"]),
                "privacy": dict(self._session["privacy"]),
            }
            self._last_result = result
            self._hook = None
            self._worker = None
            self._enrichment_worker = None
            self._queue = None
            self._semantic_queue = None
            self._session_dir = None
            return dict(result)


def _set_window_capture_exclusion(hwnd: int, enabled: bool = True) -> dict[str, Any]:
    """Ask Windows compositor to omit one owned top-level window from captures.

    Windows 10 2004+ supports WDA_EXCLUDEFROMCAPTURE, which keeps the recorder
    visible to the human while allowing desktop capture to see the content
    underneath it. This is preferable to hiding/showing the Tk window around
    every step, which would flicker and race with human input.

    The call is best-effort because older Windows builds and some capture paths
    may not honor the affinity. Recording still works if it is unavailable.
    """
    if os.name != "nt":
        return {"supported": False, "applied": False, "reason": "windows_only"}
    import ctypes
    from ctypes import wintypes

    if not hwnd:
        return {"supported": True, "applied": False, "reason": "missing_hwnd"}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    if not user32.IsWindow(int(hwnd)):
        return {"supported": True, "applied": False, "reason": "invalid_hwnd"}

    WDA_NONE = 0x00000000
    WDA_EXCLUDEFROMCAPTURE = 0x00000011
    ctypes.set_last_error(0)
    ok = bool(
        user32.SetWindowDisplayAffinity(
            int(hwnd), WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
        )
    )
    return {
        "supported": True,
        "applied": ok,
        "mode": "exclude_from_capture" if enabled else "none",
        "win32_error": 0 if ok else int(ctypes.get_last_error()),
    }


def run_user_recording_ui(
    *,
    output_dir: str | None = None,
    capture_screenshots: bool = True,
    settle_ms: int = DEFAULT_RECORDING_SETTLE_MS,
    capture_scope: str = DEFAULT_RECORDING_CAPTURE_SCOPE,
    capture_text: bool = True,
    goal: str = "",
    prerequisites: str = "",
    success_criteria: str = "",
) -> None:
    """Small PSR-style host UI for human demonstration recording.

    The UI owns only human lifecycle controls. Capture, hooks, correlation,
    screenshots and export stay in UserRecordingManager so HTTP/CLI/UI producers
    all converge on the same recorder owner.
    """
    if os.name != "nt":
        raise ValueError("recording UI currently requires Windows")

    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    # Give the Tk window its own stable Windows application identity so the
    # supplied Agent Computer Bridge artwork is used by the taskbar/window host
    # instead of the generic Python icon.
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        shell32.SetCurrentProcessExplicitAppUserModelID("AgentComputerBridge.Recorder")
    except Exception:
        pass

    root = tk.Tk()
    root.title("Agent Schrittaufzeichnung")
    asset_dir = Path(__file__).resolve().parent / "assets"
    app_icon_png = asset_dir / "agent_computer_bridge.png"
    app_icon_ico = asset_dir / "agent_computer_bridge.ico"
    try:
        if app_icon_png.is_file():
            root._agent_bridge_icon = tk.PhotoImage(file=str(app_icon_png))
            root.iconphoto(True, root._agent_bridge_icon)
        if app_icon_ico.is_file():
            root.iconbitmap(str(app_icon_ico))
    except Exception:
        # Icon failure must never block recording.
        pass
    root.geometry("560x132")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.configure(bg="#111111")
    # Force Tk to create the native top-level HWND before applying Windows
    # capture affinity. The recorder must stay visible to the human, but it
    # should not become part of the demonstration screenshots.
    root.update_idletasks()
    try:
        recorder_ui_hwnd = _top_level_window_handle(int(root.winfo_id()))
    except Exception:
        recorder_ui_hwnd = 0
    recorder_capture_exclusion = _set_window_capture_exclusion(recorder_ui_hwnd, True)

    output_var = tk.StringVar(value=str(Path(output_dir).expanduser() if output_dir else default_recording_root()))
    screenshots_var = tk.BooleanVar(value=bool(capture_screenshots))
    settle_var = tk.IntVar(value=int(settle_ms))
    capture_scope_var = tk.StringVar(value=normalize_recording_capture_scope(capture_scope))
    capture_text_var = tk.BooleanVar(value=bool(capture_text))
    goal_var = tk.StringVar(value=goal)
    prerequisites_var = tk.StringVar(value=prerequisites)
    success_var = tk.StringVar(value=success_criteria)
    auto_minimize_var = tk.BooleanVar(value=True)
    control_restore = {"epoch": 0.0, "recording_iconified": False}
    status_var = tk.StringVar(value="Bereit")
    detail_var = tk.StringVar(value="Start drücken und den Ablauf normal vorführen.")
    state = {"busy": False}

    top = tk.Frame(root, bg="#111111")
    top.pack(fill="x", padx=12, pady=(10, 4))
    status_dot = tk.Canvas(top, width=16, height=16, bg="#111111", highlightthickness=0)
    dot = status_dot.create_oval(3, 3, 13, 13, fill="#d7d7d7", outline="")
    status_dot.pack(side="left", padx=(0, 8))
    tk.Label(top, textvariable=status_var, bg="#111111", fg="#f3f3f3", font=("Segoe UI", 10, "bold")).pack(side="left")
    tk.Label(root, textvariable=detail_var, bg="#111111", fg="#b9b9b9", anchor="w", font=("Segoe UI", 9)).pack(fill="x", padx=36, pady=(0, 8))

    buttons = tk.Frame(root, bg="#111111")
    buttons.pack(fill="x", padx=10, pady=(0, 10))

    def _set_topmost(enabled: bool) -> None:
        try:
            root.attributes("-topmost", bool(enabled))
        except tk.TclError:
            pass

    def _show_recorder_after_recording() -> None:
        try:
            control_restore["recording_iconified"] = False
            root.deiconify()
            root.title("Agent Schrittaufzeichnung")
            _set_topmost(True)
            root.lift()
        except tk.TclError:
            pass

    def _minimize_recorder_for_recording() -> None:
        # Keep a visible taskbar affordance so a user who forgot the hotkey can
        # always find the recorder again. It is no longer Topmost and remains
        # compositor-excluded from screenshots where Windows supports that.
        _set_topmost(False)
        try:
            root.title("Agent Schrittaufzeichnung — Aufnahme läuft — Strg+Shift+F12 zum Stoppen")
            if bool(auto_minimize_var.get()):
                control_restore["recording_iconified"] = True
                root.iconify()
        except tk.TclError:
            pass

    def _on_recorder_map(_event: Any = None) -> None:
        # A taskbar restore happens after the Shell_TrayWnd click. Mark the time
        # so the recorder owner can suppress only that lifecycle-control click
        # without hiding legitimate taskbar demonstrations.
        if control_restore.get("recording_iconified") and USER_RECORDING.status().get("active"):
            control_restore["recording_iconified"] = False
            control_restore["epoch"] = time.time()
            _set_topmost(False)

    def _on_recorder_unmap(_event: Any = None) -> None:
        if USER_RECORDING.status().get("active"):
            control_restore["recording_iconified"] = True

    root.bind("<Map>", _on_recorder_map, add="+")
    root.bind("<Unmap>", _on_recorder_unmap, add="+")

    def set_ui_mode(mode: str) -> None:
        if mode == "recording":
            status_var.set("Aufzeichnung läuft")
            status_dot.itemconfigure(dot, fill="#ff4d4d")
            start_btn.configure(state="disabled")
            stop_btn.configure(state="normal")
            settings_btn.configure(state="disabled")
            _set_topmost(False)
        elif mode == "busy":
            status_var.set("Wird gespeichert …")
            status_dot.itemconfigure(dot, fill="#f0b429")
            start_btn.configure(state="disabled")
            stop_btn.configure(state="disabled")
            settings_btn.configure(state="disabled")
        else:
            status_var.set("Bereit")
            status_dot.itemconfigure(dot, fill="#d7d7d7")
            start_btn.configure(state="normal")
            stop_btn.configure(state="disabled")
            settings_btn.configure(state="normal")
            try:
                root.title("Agent Schrittaufzeichnung")
            except tk.TclError:
                pass

    def run_background(name: str, func: Any, callback: Any) -> None:
        holder: dict[str, Any] = {"done": False, "result": None, "error": None}

        def worker() -> None:
            try:
                holder["result"] = func()
            except Exception as exc:
                holder["error"] = exc
            holder["done"] = True

        threading.Thread(target=worker, name=name, daemon=True).start()

        def poll() -> None:
            if holder["done"]:
                callback(holder.get("result"), holder.get("error"))
                return
            try:
                root.after(50, poll)
            except Exception:
                pass

        root.after(50, poll)

    def start_done(_result: Any, error: Exception | None) -> None:
        state["busy"] = False
        if error is not None:
            set_ui_mode("idle")
            _show_recorder_after_recording()
            detail_var.set("Aufzeichnung konnte nicht gestartet werden.")
            messagebox.showerror("Agent Schrittaufzeichnung", str(error), parent=root)
            return
        set_ui_mode("recording")
        if recorder_capture_exclusion.get("applied"):
            detail_var.set("Aufzeichnung läuft. Strg+Shift+F12 beendet und speichert.")
        else:
            detail_var.set(
                "Aufzeichnung läuft. Strg+Shift+F12 beendet und speichert."
            )
        _minimize_recorder_for_recording()

    stop_hotkey_event = threading.Event()

    def request_hotkey_stop() -> None:
        # Called from the low-level hook thread; do not touch Tk here.
        stop_hotkey_event.set()

    def poll_hotkey_stop() -> None:
        if stop_hotkey_event.is_set():
            stop_hotkey_event.clear()
            if USER_RECORDING.status().get("active") and not state["busy"]:
                stop_recording()
        try:
            root.after(75, poll_hotkey_stop)
        except tk.TclError:
            pass

    def start_recording() -> None:
        if state["busy"] or USER_RECORDING.status().get("active"):
            return
        state["busy"] = True
        set_ui_mode("busy")
        detail_var.set("Aufzeichnung wird vorbereitet …")
        ui_hwnd = recorder_ui_hwnd

        selected_output = output_var.get().strip() or None
        selected_screenshots = bool(screenshots_var.get())
        selected_settle = int(settle_var.get())
        selected_capture_scope = normalize_recording_capture_scope(capture_scope_var.get())
        selected_capture_text = bool(capture_text_var.get())
        selected_intent = recording_intent(goal_var.get(), prerequisites_var.get(), success_var.get())

        def work() -> dict[str, Any]:
            return USER_RECORDING.start(
                output_dir=selected_output,
                capture_screenshots=selected_screenshots,
                settle_ms=selected_settle,
                capture_scope=selected_capture_scope,
                capture_text=selected_capture_text,
                goal=selected_intent["goal"],
                prerequisites=selected_intent["prerequisites"],
                success_criteria=selected_intent["success_criteria"],
                ignored_hwnds={ui_hwnd} if ui_hwnd else None,
                ignored_pids={os.getpid()},
                control_stop_callback=request_hotkey_stop,
                control_restore_epoch_getter=lambda: control_restore.get("epoch", 0.0),
            )

        run_background("recording-ui-start", work, start_done)

    def stop_done(result: dict[str, Any] | None, error: Exception | None) -> None:
        state["busy"] = False
        set_ui_mode("idle")
        _show_recorder_after_recording()
        if error is not None:
            detail_var.set("Speichern fehlgeschlagen.")
            messagebox.showerror("Agent Schrittaufzeichnung", str(error), parent=root)
            return
        step_count = int((result or {}).get("step_count") or 0)
        archive = str((result or {}).get("archive") or "")
        detail_var.set(f"Gespeichert: {step_count} Schritte" + (f" — {Path(archive).name}" if archive else ""))

    def stop_recording() -> None:
        if state["busy"] or not USER_RECORDING.status().get("active"):
            return
        state["busy"] = True
        set_ui_mode("busy")

        run_background("recording-ui-stop", USER_RECORDING.stop, stop_done)

    def open_settings() -> None:
        dialog = tk.Toplevel(root)
        dialog.title("Einstellungen")
        dialog.geometry("620x500")
        dialog.resizable(False, False)
        dialog.transient(root)
        dialog.grab_set()
        dialog.configure(bg="#171717")

        for label, variable in (("Ziel des gezeigten Ablaufs", goal_var),
                                ("Voraussetzungen / Ausgangszustand", prerequisites_var),
                                ("Woran erkennt der Agent den Erfolg?", success_var)):
            tk.Label(dialog, text=label, bg="#171717", fg="#d8d8d8").pack(anchor="w", padx=16, pady=(8, 0))
            ttk.Entry(dialog, textvariable=variable).pack(fill="x", padx=16, pady=(2, 0))

        tk.Checkbutton(
            dialog, text="Screenshots pro Schritt aufnehmen", variable=screenshots_var,
            bg="#171717", fg="#f0f0f0", selectcolor="#2a2a2a",
            activebackground="#171717", activeforeground="#ffffff"
        ).pack(anchor="w", padx=16, pady=(14, 5))

        tk.Checkbutton(
            dialog, text="Eingegebenen Text/Formeln für Agenten speichern",
            variable=capture_text_var,
            bg="#171717", fg="#f0f0f0", selectcolor="#2a2a2a",
            activebackground="#171717", activeforeground="#ffffff"
        ).pack(anchor="w", padx=16, pady=(0, 5))

        tk.Checkbutton(
            dialog, text="Während der Aufnahme minimieren (empfohlen)",
            variable=auto_minimize_var,
            bg="#171717", fg="#f0f0f0", selectcolor="#2a2a2a",
            activebackground="#171717", activeforeground="#ffffff"
        ).pack(anchor="w", padx=16, pady=(0, 8))

        delay_row = tk.Frame(dialog, bg="#171717")
        delay_row.pack(fill="x", padx=16, pady=4)
        tk.Label(delay_row, text="Screenshot-Verzögerung (ms):", bg="#171717", fg="#d8d8d8").pack(side="left")
        tk.Spinbox(delay_row, from_=0, to=3000, increment=20, textvariable=settle_var, width=8).pack(side="left", padx=8)

        scope_row = tk.Frame(dialog, bg="#171717")
        scope_row.pack(fill="x", padx=16, pady=4)
        tk.Label(scope_row, text="Screenshot-Bereich:", bg="#171717", fg="#d8d8d8").pack(side="left")
        scope_box = ttk.Combobox(
            scope_row,
            textvariable=capture_scope_var,
            state="readonly",
            values=("monitor", "virtual_desktop"),
            width=20,
        )
        scope_box.pack(side="left", padx=8)
        tk.Label(
            scope_row,
            text="Monitor (empfohlen) / gesamter Desktop",
            bg="#171717",
            fg="#9f9f9f",
        ).pack(side="left")

        folder_row = tk.Frame(dialog, bg="#171717")
        folder_row.pack(fill="x", padx=16, pady=8)
        tk.Entry(folder_row, textvariable=output_var).pack(side="left", fill="x", expand=True)

        def browse() -> None:
            selected = filedialog.askdirectory(initialdir=output_var.get() or str(default_recording_root()), parent=dialog)
            if selected:
                output_var.set(selected)

        tk.Button(folder_row, text="Ordner …", command=browse, width=10).pack(side="left", padx=(8, 0))
        tk.Button(dialog, text="Schließen", command=dialog.destroy, width=12).pack(anchor="e", padx=16, pady=(4, 10))

    def destroy_root() -> None:
        if recorder_ui_hwnd and recorder_capture_exclusion.get("applied"):
            try:
                _set_window_capture_exclusion(recorder_ui_hwnd, False)
            except Exception:
                pass
        root.destroy()

    def on_close() -> None:
        if state["busy"]:
            return
        if USER_RECORDING.status().get("active"):
            state["busy"] = True
            set_ui_mode("busy")
            detail_var.set("Aufzeichnung wird vor dem Schließen gespeichert …")

            def close_done(_result: Any, _error: Exception | None) -> None:
                destroy_root()

            run_background("recording-ui-close", USER_RECORDING.stop, close_done)
            return
        destroy_root()

    start_btn = tk.Button(buttons, text="●  Aufzeichnung starten", command=start_recording, width=21, height=2)
    start_btn.pack(side="left", padx=(0, 8))
    stop_btn = tk.Button(buttons, text="■  Beenden und speichern", command=stop_recording, width=23, height=2, state="disabled")
    stop_btn.pack(side="left", padx=(0, 8))
    settings_btn = tk.Button(buttons, text="⚙  Einstellungen", command=open_settings, width=16, height=2)
    settings_btn.pack(side="left")

    def on_map(_event: Any = None) -> None:
        # If the user opted out of auto-hide and the recorder is visible while
        # active, it still must not reclaim Topmost/foreground behavior.
        if USER_RECORDING.status().get("active"):
            _set_topmost(False)

    root.bind("<Map>", on_map, add="+")
    root.protocol("WM_DELETE_WINDOW", on_close)
    set_ui_mode("idle")
    root.after(75, poll_hotkey_stop)
    root.mainloop()


USER_RECORDING = UserRecordingManager()


def point_in_viewport(x: int, y: int, viewport: dict[str, int] | None = None) -> bool:
    viewport = viewport if viewport is not None else load_viewport()
    if viewport is None:
        return True
    return (
        viewport["x"] <= x < viewport["x"] + viewport["width"]
        and viewport["y"] <= y < viewport["y"] + viewport["height"]
    )


def require_point_in_viewport(x: int, y: int) -> None:
    viewport = load_viewport()
    if viewport is not None and not point_in_viewport(x, y, viewport):
        raise ValueError(
            f"target ({x}, {y}) is outside configured visible viewport "
            f"({viewport['x']}, {viewport['y']}, {viewport['width']}x{viewport['height']})"
        )


def _draw_viewport_overlay(image: Image.Image, viewport: dict[str, int]) -> Image.Image:
    """Dim everything outside the user-visible RDP viewport and draw its border."""
    base = image.convert("RGBA")
    iw, ih = base.size
    desktop = virtual_desktop_bounds()
    left, top = desktop["x"], desktop["y"]

    x1 = int(round((viewport["x"] - left) * iw / max(1, desktop["width"])))
    y1 = int(round((viewport["y"] - top) * ih / max(1, desktop["height"])))
    x2 = int(round((viewport["x"] + viewport["width"] - left) * iw / max(1, desktop["width"])))
    y2 = int(round((viewport["y"] + viewport["height"] - top) * ih / max(1, desktop["height"])))
    x1 = clamp(x1, 0, max(0, iw - 1))
    y1 = clamp(y1, 0, max(0, ih - 1))
    x2 = clamp(x2, x1 + 1, iw)
    y2 = clamp(y2, y1 + 1, ih)

    shade = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shade)
    alpha = 165
    if y1 > 0:
        draw.rectangle((0, 0, iw, y1), fill=(0, 0, 0, alpha))
    if y2 < ih:
        draw.rectangle((0, y2, iw, ih), fill=(0, 0, 0, alpha))
    if x1 > 0:
        draw.rectangle((0, y1, x1, y2), fill=(0, 0, 0, alpha))
    if x2 < iw:
        draw.rectangle((x2, y1, iw, y2), fill=(0, 0, 0, alpha))
    base = Image.alpha_composite(base, shade)

    border = Image.new("RGBA", base.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    width = max(4, int(round(min(iw, ih) * 0.004)))
    bd.rectangle((x1, y1, x2 - 1, y2 - 1), outline=(90, 255, 120, 255), width=width)
    label = "VISIBLE RDP AREA - IGNORE OUTSIDE"
    tx = x1 + 8
    ty = y1 + 8
    bbox = bd.textbbox((tx, ty), label)
    bd.rectangle((bbox[0] - 5, bbox[1] - 3, bbox[2] + 5, bbox[3] + 3), fill=(0, 0, 0, 205))
    bd.text((tx, ty), label, fill=(255, 255, 255, 255))
    return Image.alpha_composite(base, border).convert("RGB")


def _draw_window_scope_overlay(image: Image.Image, scope: dict[str, Any]) -> Image.Image:
    """Dim the desktop outside the selected application window."""
    base = image.convert("RGBA")
    iw, ih = base.size
    desktop = virtual_desktop_bounds()
    left, top = desktop["x"], desktop["y"]
    x1 = clamp(int(round((int(scope["x"]) - left) * iw / max(1, desktop["width"]))), 0, max(0, iw - 1))
    y1 = clamp(int(round((int(scope["y"]) - top) * ih / max(1, desktop["height"]))), 0, max(0, ih - 1))
    x2 = clamp(int(round((int(scope["x"]) + int(scope["width"]) - left) * iw / max(1, desktop["width"]))), x1 + 1, iw)
    y2 = clamp(int(round((int(scope["y"]) + int(scope["height"]) - top) * ih / max(1, desktop["height"]))), y1 + 1, ih)
    shade = Image.new("RGBA", base.size, (0, 0, 0, 0)); draw = ImageDraw.Draw(shade); alpha = 185
    if y1 > 0: draw.rectangle((0, 0, iw, y1), fill=(0,0,0,alpha))
    if y2 < ih: draw.rectangle((0, y2, iw, ih), fill=(0,0,0,alpha))
    if x1 > 0: draw.rectangle((0, y1, x1, y2), fill=(0,0,0,alpha))
    if x2 < iw: draw.rectangle((x2, y1, iw, y2), fill=(0,0,0,alpha))
    base = Image.alpha_composite(base, shade)
    border = Image.new("RGBA", base.size, (0,0,0,0)); bd = ImageDraw.Draw(border)
    bw = max(4, int(round(min(iw, ih) * 0.004)))
    bd.rectangle((x1,y1,x2-1,y2-1), outline=(90,190,255,255), width=bw)
    label = f"PROCESS SCOPE: {scope.get('process_name') or scope.get('pid')}"
    tx, ty = x1 + 8, y1 + 8
    bbox = bd.textbbox((tx,ty), label)
    bd.rectangle((bbox[0]-5,bbox[1]-3,bbox[2]+5,bbox[3]+3), fill=(0,0,0,205))
    bd.text((tx,ty), label, fill=(255,255,255,255))
    return Image.alpha_composite(base, border).convert("RGB")


def _draw_cursor_marker(image: Image.Image, x: int, y: int) -> Image.Image:
    """
    Overlay a large high-contrast pointer marker with glow.

    The marker combines:
      - soft outer glow
      - large ring around the exact pointer hotspot
      - crosshair centered on the exact hotspot
      - small arrow-like pointer triangle

    This is intentionally more visible than the normal OS cursor for vision
    models, while keeping the exact click hotspot unambiguous.
    """
    base = image.convert("RGBA")
    width, height = base.size

    x = clamp(int(x), 0, max(0, width - 1))
    y = clamp(int(y), 0, max(0, height - 1))

    radius = max(18, int(round(min(width, height) * 0.018)))
    radius = min(radius, 34)
    glow_radius = radius + 12
    line_width = max(3, radius // 6)

    # Glow layer: blurred opaque shapes become a soft halo.
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_box = (
        x - glow_radius,
        y - glow_radius,
        x + glow_radius,
        y + glow_radius,
    )
    glow_draw.ellipse(glow_box, outline=(255, 70, 70, 230), width=max(7, line_width * 2))
    glow_draw.line((x - glow_radius, y, x + glow_radius, y), fill=(255, 70, 70, 180), width=5)
    glow_draw.line((x, y - glow_radius, x, y + glow_radius), fill=(255, 70, 70, 180), width=5)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=8))
    base = Image.alpha_composite(base, glow)

    marker = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(marker)

    ring_box = (x - radius, y - radius, x + radius, y + radius)
    # Dark outer edge plus bright inner ring stays readable over both bright
    # and dark UIs.
    draw.ellipse(ring_box, outline=(0, 0, 0, 235), width=line_width + 3)
    draw.ellipse(ring_box, outline=(255, 90, 90, 255), width=line_width)

    arm = radius + 8
    gap = max(5, radius // 3)
    cross_w = max(3, line_width)

    # Crosshair with dark edge.
    segments = [
        (x - arm, y, x - gap, y),
        (x + gap, y, x + arm, y),
        (x, y - arm, x, y - gap),
        (x, y + gap, x, y + arm),
    ]
    for segment in segments:
        draw.line(segment, fill=(0, 0, 0, 235), width=cross_w + 3)
        draw.line(segment, fill=(255, 255, 255, 255), width=cross_w)

    # Exact hotspot dot.
    dot_r = max(3, line_width)
    draw.ellipse(
        (x - dot_r, y - dot_r, x + dot_r, y + dot_r),
        fill=(255, 255, 255, 255),
        outline=(0, 0, 0, 255),
        width=2,
    )

    # Small pointer wedge beginning at the hotspot.
    pointer = [
        (x + 4, y + 4),
        (x + radius, y + radius // 2),
        (x + radius // 2, y + radius),
    ]
    draw.polygon(pointer, fill=(255, 255, 255, 245), outline=(0, 0, 0, 255))

    return Image.alpha_composite(base, marker).convert("RGB")


def _overlay_font(image: Image.Image, *, large: bool = False) -> Any:
    """Return a dependency-free Pillow font sized for the screenshot."""
    target = max(13, min(28, int(round(min(image.size) * (0.018 if large else 0.014)))))
    try:
        return ImageFont.load_default(size=target)
    except TypeError:
        return ImageFont.load_default()


def _draw_pointer_coordinate_guides(
    image: Image.Image,
    screen_x: int,
    screen_y: int,
    *,
    scope: dict[str, Any] | None = None,
) -> Image.Image:
    """Draw agent-facing absolute/scope coordinates at pointer and frame edges.

    The live runtime overlay is a user indicator, but Windows/RDP capture paths
    can choose whether a layered topmost window is included in a screenshot.
    These guides are therefore rendered into the returned screenshot itself so
    a vision agent always gets the same coordinate references.
    """
    base = image.convert("RGBA")
    draw = ImageDraw.Draw(base)
    iw, ih = base.size
    desktop = virtual_desktop_bounds()

    def map_abs(abs_x: int, abs_y: int) -> tuple[int, int]:
        return (
            clamp(int(round((int(abs_x) - desktop["x"]) * iw / max(1, desktop["width"]))), 0, max(0, iw - 1)),
            clamp(int(round((int(abs_y) - desktop["y"]) * ih / max(1, desktop["height"]))), 0, max(0, ih - 1)),
        )

    monitor = monitor_for_point(int(screen_x), int(screen_y))
    frame: dict[str, Any] | None = None
    frame_mode = "ABS"
    local_x, local_y = int(screen_x), int(screen_y)
    if scope is not None and _point_in_rect(int(screen_x), int(screen_y), scope):
        frame = scope
        frame_mode = "SCOPE"
        local_x = int(screen_x) - int(scope["x"])
        local_y = int(screen_y) - int(scope["y"])
    elif monitor is not None:
        frame = monitor

    px, py = map_abs(int(screen_x), int(screen_y))
    font = _overlay_font(base, large=True)
    small_font = _overlay_font(base, large=False)

    def text_box(x: int, y: int, text: str, *, anchor: str = "la", font_obj: Any = font) -> None:
        # Pillow anchors are supported in current Pillow; fall back to manual
        # placement if a future backend rejects one.
        try:
            bbox = draw.textbbox((x, y), text, font=font_obj, anchor=anchor, stroke_width=2)
            pad_x, pad_y = 6, 4
            draw.rectangle((bbox[0]-pad_x, bbox[1]-pad_y, bbox[2]+pad_x, bbox[3]+pad_y), fill=(0, 0, 0, 205))
            draw.text((x, y), text, font=font_obj, anchor=anchor, fill=(255,255,255,255), stroke_width=2, stroke_fill=(0,0,0,255))
        except (TypeError, ValueError):
            bbox = draw.textbbox((x, y), text, font=font_obj)
            draw.rectangle((bbox[0]-6, bbox[1]-4, bbox[2]+6, bbox[3]+4), fill=(0,0,0,205))
            draw.text((x, y), text, font=font_obj, fill=(255,255,255,255), stroke_width=2, stroke_fill=(0,0,0,255))

    if frame_mode == "SCOPE":
        pointer_label = (
            f"AGENT CURSOR | ACTION X={local_x} Y={local_y} "
            f"• ABS X={int(screen_x)} Y={int(screen_y)}"
        )
    else:
        pointer_label = f"AGENT CURSOR | ACTION X={int(screen_x)} Y={int(screen_y)} | ABS PIXELS"

    label_x = px + 52
    label_y = py + 28
    anchor = "la"
    if label_x > iw - max(280, iw // 5):
        label_x = px - 52
        anchor = "ra"
    if label_y > ih - 40:
        label_y = py - 30
    text_box(label_x, label_y, pointer_label, anchor=anchor, font_obj=font)

    if frame is not None:
        fx, fy = int(frame["x"]), int(frame["y"])
        fw, fh = int(frame["width"]), int(frame["height"])
        top_x, top_y = map_abs(int(screen_x), fy)
        left_x, left_y = map_abs(fx, int(screen_y))
        frame_left, frame_top = map_abs(fx, fy)
        frame_right, frame_bottom = map_abs(fx + fw - 1, fy + fh - 1)

        # Project current x/y to the active coordinate frame edges. This works
        # like a lightweight ruler and is much less visually noisy than a grid.
        tick = max(8, int(round(min(iw, ih) * 0.010)))
        top_y = clamp(frame_top + tick + 2, 0, max(0, ih - 1))
        left_x = clamp(frame_left + tick + 2, 0, max(0, iw - 1))
        draw.line((top_x, frame_top + 2, top_x, top_y + tick), fill=(85,200,255,255), width=max(2, tick//5))
        draw.line((frame_left + 2, left_y, left_x + tick, left_y), fill=(85,200,255,255), width=max(2, tick//5))
        x_value = local_x if frame_mode == "SCOPE" else int(screen_x)
        y_value = local_y if frame_mode == "SCOPE" else int(screen_y)
        text_box(top_x, top_y + tick + 4, f"ACTION X={x_value}", anchor="ma", font_obj=small_font)
        text_box(left_x + tick + 4, left_y, f"ACTION Y={y_value}", anchor="lm", font_obj=small_font)

        if frame_mode == "SCOPE":
            frame_label = (
                f"AGENT SCOPE | ACTION PIXELS X=0..{fw-1} Y=0..{fh-1} "
                f"• ABS X={fx}..{fx+fw-1} Y={fy}..{fy+fh-1}"
            )
        else:
            frame_label = f"AGENT MONITOR | ACTION=ABS PIXELS | X={fx}..{fx+fw-1} Y={fy}..{fy+fh-1}"
        text_box(frame_left + 16, frame_top + 18, frame_label, anchor="la", font_obj=small_font)

    return base.convert("RGB")



def _scope_screen_to_image(
    scope: dict[str, Any], image: Image.Image, screen_x: int, screen_y: int
) -> tuple[int, int]:
    width = max(1, int(scope["width"]))
    height = max(1, int(scope["height"]))
    ix = int(round((int(screen_x) - int(scope["x"])) * image.size[0] / width))
    iy = int(round((int(screen_y) - int(scope["y"])) * image.size[1] / height))
    return (
        clamp(ix, 0, max(0, image.size[0] - 1)),
        clamp(iy, 0, max(0, image.size[1] - 1)),
    )


def _draw_scoped_viewport_overlay(
    image: Image.Image,
    scope: dict[str, Any],
    viewport: dict[str, int] | None,
) -> Image.Image:
    """Apply the existing user-visible viewport safety boundary to a HWND frame."""
    if viewport is None:
        return image
    overlap = _rect_intersection(scope, viewport)
    base = image.convert("RGBA")
    dim = Image.new("RGBA", base.size, (0, 0, 0, 155))
    if overlap is None:
        return Image.alpha_composite(base, dim).convert("RGB")

    x1, y1 = _scope_screen_to_image(scope, image, overlap["x"], overlap["y"])
    # Map exclusive bottom/right edges separately to avoid losing the final pixel.
    x2 = clamp(
        int(round((overlap["x"] + overlap["width"] - int(scope["x"])) * image.size[0] / max(1, int(scope["width"])))),
        x1 + 1, image.size[0],
    )
    y2 = clamp(
        int(round((overlap["y"] + overlap["height"] - int(scope["y"])) * image.size[1] / max(1, int(scope["height"])))),
        y1 + 1, image.size[1],
    )
    visible = base.crop((x1, y1, x2, y2))
    shaded = Image.alpha_composite(base, dim)
    shaded.paste(visible, (x1, y1))
    draw = ImageDraw.Draw(shaded)
    draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline=(85, 200, 255, 255), width=max(2, min(image.size)//300))
    return shaded.convert("RGB")


def _draw_scoped_capture_guides(
    image: Image.Image,
    scope: dict[str, Any],
    *,
    show_cursor: bool,
) -> Image.Image:
    """Render the same ACTION/ABS contract onto an HWND-bound capture."""
    base = image.convert("RGB")
    if not show_cursor:
        return base
    screen_x, screen_y = current_mouse()
    if not _point_in_rect(screen_x, screen_y, scope):
        return base

    ix, iy = _scope_screen_to_image(scope, base, screen_x, screen_y)
    base = _draw_cursor_marker(base, ix, iy)
    draw = ImageDraw.Draw(base)
    font = _overlay_font(base, large=True)
    small = _overlay_font(base, large=False)
    local_x = int(screen_x) - int(scope["x"])
    local_y = int(screen_y) - int(scope["y"])
    label = f"AGENT CURSOR | ACTION X={local_x} Y={local_y} • ABS X={screen_x} Y={screen_y}"

    def box(x: int, y: int, text: str, *, anchor: str = "la", font_obj: Any = font) -> None:
        try:
            bbox = draw.textbbox((x, y), text, font=font_obj, anchor=anchor, stroke_width=2)
            draw.rectangle((bbox[0]-6, bbox[1]-4, bbox[2]+6, bbox[3]+4), fill=(0,0,0,205))
            draw.text((x, y), text, font=font_obj, anchor=anchor, fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
        except TypeError:
            draw.text((x, y), text, font=font_obj, fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))

    lx, ly, anchor = ix + 52, iy + 28, "la"
    if lx > base.size[0] - max(280, base.size[0] // 5):
        lx, anchor = ix - 52, "ra"
    if ly > base.size[1] - 40:
        ly = iy - 30
    box(lx, ly, label, anchor=anchor)
    box(16, 18, f"AGENT SCOPE | ACTION PIXELS X=0..{max(0,int(scope['width'])-1)} Y=0..{max(0,int(scope['height'])-1)}", font_obj=small)
    box(ix, 42, f"ACTION X={local_x}", anchor="ma", font_obj=small)
    box(16, iy, f"ACTION Y={local_y}", anchor="lm", font_obj=small)
    return base


def crop_scoped_capture_region(
    image: Image.Image,
    scope: dict[str, Any],
    region: dict[str, Any],
) -> tuple[Image.Image, dict[str, int]]:
    """Crop an HWND-bound image using window-scope-relative coordinates."""
    try:
        rx = int(round(float(region["x"])))
        ry = int(round(float(region["y"])))
        rw = int(round(float(region["width"])))
        rh = int(round(float(region["height"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("screenshot region requires numeric x, y, width, and height") from exc
    if rw <= 0 or rh <= 0:
        raise ValueError("region width and height must be positive")
    sw, sh = int(scope["width"]), int(scope["height"])
    if rx < 0 or ry < 0 or rx + rw > sw or ry + rh > sh:
        raise ValueError(f"scoped screenshot region must fit window_scope bounds 0..{sw-1}, 0..{sh-1}")
    ix1 = clamp(int(round(rx * image.size[0] / max(1, sw))), 0, max(0, image.size[0]-1))
    iy1 = clamp(int(round(ry * image.size[1] / max(1, sh))), 0, max(0, image.size[1]-1))
    ix2 = clamp(int(round((rx+rw) * image.size[0] / max(1, sw))), ix1+1, image.size[0])
    iy2 = clamp(int(round((ry+rh) * image.size[1] / max(1, sh))), iy1+1, image.size[1])
    screen_region = {"x": int(scope["x"])+rx, "y": int(scope["y"])+ry, "width": rw, "height": rh}
    return image.crop((ix1, iy1, ix2, iy2)), screen_region


def map_screen_xy_to_image(
    x: int,
    y: int,
    *,
    image_width: int,
    image_height: int,
) -> tuple[int, int]:
    """Map Windows virtual-desktop coordinates to full-desktop screenshot pixels."""
    desktop = virtual_desktop_bounds()
    if desktop["width"] <= 0 or desktop["height"] <= 0:
        raise ValueError("virtual desktop dimensions must be positive")
    mapped_x = int(round((float(x) - desktop["x"]) * image_width / desktop["width"]))
    mapped_y = int(round((float(y) - desktop["y"]) * image_height / desktop["height"]))
    return (
        clamp(mapped_x, 0, max(0, image_width - 1)),
        clamp(mapped_y, 0, max(0, image_height - 1)),
    )

def crop_to_screen_region(
    image: Image.Image,
    x: Any,
    y: Any,
    width: Any,
    height: Any,
) -> Image.Image:
    """Crop a full virtual-desktop screenshot using Windows screen coordinates."""
    desktop = virtual_desktop_bounds()
    left, top = desktop["x"], desktop["y"]
    right, bottom = left + desktop["width"], top + desktop["height"]
    try:
        rx = int(round(float(x)))
        ry = int(round(float(y)))
        rw = int(round(float(width)))
        rh = int(round(float(height)))
    except (TypeError, ValueError) as exc:
        raise ValueError("region x/y/width/height must be numeric") from exc
    if rw <= 0 or rh <= 0:
        raise ValueError("region width and height must be positive")
    x1 = max(rx, left)
    y1 = max(ry, top)
    x2 = min(rx + rw, right)
    y2 = min(ry + rh, bottom)
    if x2 <= x1 or y2 <= y1:
        raise ValueError("screenshot region does not intersect the virtual desktop")
    iw, ih = image.size
    ix1 = clamp(int(round((x1 - left) * iw / max(1, desktop["width"]))), 0, max(0, iw - 1))
    iy1 = clamp(int(round((y1 - top) * ih / max(1, desktop["height"]))), 0, max(0, ih - 1))
    ix2 = clamp(int(round((x2 - left) * iw / max(1, desktop["width"]))), ix1 + 1, iw)
    iy2 = clamp(int(round((y2 - top) * ih / max(1, desktop["height"]))), iy1 + 1, ih)
    return image.crop((ix1, iy1, ix2, iy2))

def _scope_region_screen() -> dict[str, int] | None:
    scope = active_scope_window()
    if scope is None:
        return None
    return {"x": int(scope["x"]), "y": int(scope["y"]), "width": int(scope["width"]), "height": int(scope["height"])}


def _region_in_active_space(region: dict[str, Any]) -> dict[str, Any]:
    scope = active_scope_window()
    result = dict(region)
    if scope is not None:
        result["x"] = float(region.get("x")) + int(scope["x"])
        result["y"] = float(region.get("y")) + int(scope["y"])
    return result


def capture_screenshot_with_info(
    *,
    show_cursor: bool = True,
    show_viewport: bool = True,
    use_scope: bool = True,
    capture_provider: Any = DEFAULT_CAPTURE_PROVIDER,
) -> tuple[Image.Image, dict[str, Any]]:
    """Capture through the canonical screenshot owner and report the actual provider used."""
    scope = active_scope_window() if use_scope else None
    requested = normalize_capture_provider(capture_provider)

    if scope is not None:
        image, binding = _capture_scoped_raw(scope, capture_provider=requested)
        if show_viewport:
            image = _draw_scoped_viewport_overlay(image, scope, load_viewport())
        image = _draw_scoped_capture_guides(image, scope, show_cursor=show_cursor)
        return image, binding

    if requested == "windows_graphics_capture":
        raise ValueError("capture_provider='windows_graphics_capture' requires an active window/process scope")

    image = capture_raw_desktop()
    viewport = load_viewport() if show_viewport else None
    if viewport is not None:
        image = _draw_viewport_overlay(image, viewport)
    if show_cursor:
        screen_x, screen_y = current_mouse()
        image_x, image_y = map_screen_xy_to_image(
            screen_x, screen_y,
            image_width=image.size[0], image_height=image.size[1],
        )
        image = _draw_cursor_marker(image, image_x, image_y)
        image = _draw_pointer_coordinate_guides(image, screen_x, screen_y, scope=None)
    return image, {
        "requested_provider": requested,
        "provider": "desktop_composite",
        "capture_mode": "desktop_composite",
        "occlusion_independent": False,
        "fallback_used": False,
        "frame_size": {"width": image.size[0], "height": image.size[1]},
        "screen_mapping": dict(virtual_desktop_bounds()),
    }


def capture_screenshot(
    *,
    show_cursor: bool = True,
    show_viewport: bool = True,
    use_scope: bool = True,
    capture_provider: Any = DEFAULT_CAPTURE_PROVIDER,
) -> Image.Image:
    image, _ = capture_screenshot_with_info(
        show_cursor=show_cursor,
        show_viewport=show_viewport,
        use_scope=use_scope,
        capture_provider=capture_provider,
    )
    return image


def save_screenshot(
    path: str | os.PathLike[str],
    *,
    show_cursor: bool = True,
    show_viewport: bool = True,
    use_scope: bool = True,
    capture_provider: Any = DEFAULT_CAPTURE_PROVIDER,
) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    capture_screenshot(
        show_cursor=show_cursor,
        show_viewport=show_viewport,
        use_scope=use_scope,
        capture_provider=capture_provider,
    ).save(output, format="PNG")
    return output


def _native_set_cursor_pos(x: int, y: int) -> bool:
    """Set the Windows cursor in virtual-desktop coordinates when available."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.SetCursorPos(int(x), int(y)))
    except Exception:
        return False


def _require_live_dispatch() -> None:
    controller_id = getattr(ACTION_DISPATCH_CONTEXT, "controller_id", None)
    if not controller_id:
        return
    try:
        CONTROLLER_LEASE.validate_action(controller_id)
    except ControllerLeaseError as exc:
        if getattr(ACTION_DISPATCH_CONTEXT, "possibly_sent", False):
            raise ActionDispatchError(
                "control stopped or expired after dispatch may have begun; re-observe",
                code="CONTROL_STOPPED", action_sent=None,
                dispatch_status="possibly_sent", retry="reobserve",
            ) from exc
        raise


def _dispatch_backend(operation: str, func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call an input backend after validation and preserve delivery uncertainty.

    PyAutoGUI/Win32 do not provide an acknowledgement receipt.  If the backend
    raises after invocation starts, the bridge cannot prove whether zero, some,
    or all input events reached the OS.  Report that ambiguity instead of
    inviting a blind retry of a non-idempotent action.
    """
    _require_live_dispatch()
    if getattr(ACTION_DISPATCH_CONTEXT, "controller_id", None):
        ACTION_DISPATCH_CONTEXT.possibly_sent = True
    try:
        return func(*args, **kwargs)
    except pyautogui.FailSafeException as exc:
        if getattr(ACTION_DISPATCH_CONTEXT, "possibly_sent", False):
            raise ActionDispatchError(
                "input failsafe after dispatch may have begun; re-observe before retry",
                code="FAILSAFE_TRIGGERED", action_sent=None,
                dispatch_status="possibly_sent", retry="reobserve",
            ) from exc
        raise
    except ActionDispatchError:
        raise
    except Exception as exc:
        raise ActionDispatchError(
            f"{operation} backend failed after dispatch began: {exc}",
            action_sent=None,
            dispatch_status="possibly_sent",
            retry="reobserve",
            details={"operation": operation, "backend_error": type(exc).__name__},
        ) from exc


def _safe_recovery_point() -> tuple[int, int]:
    bounds = _effective_pointer_bounds()
    x = int(bounds["x"]) + int(bounds["width"]) // 2
    y = int(bounds["y"]) + int(bounds["height"]) // 2
    x, y = _nudge_from_monitor_corner(x, y)
    require_pointer_allowed(x, y)
    return x, y


def _recover_after_pointer_safety_failure() -> tuple[int, int] | None:
    """Best-effort recovery to the center of the current allowed region."""
    try:
        rx, ry = _safe_recovery_point()
        if not _native_set_cursor_pos(rx, ry):
            old_failsafe = pyautogui.FAILSAFE
            try:
                pyautogui.FAILSAFE = False
                _dispatch_backend("pointer-recovery-move", pyautogui.moveTo, rx, ry, duration=0.0)
            finally:
                pyautogui.FAILSAFE = old_failsafe
        return current_mouse()
    except Exception:
        return None


def _verify_pointer_after_scoped_move(expected_x: int, expected_y: int, *, tolerance: int = 3) -> tuple[int, int]:
    """Verify that a scoped move remained inside the scope and reached its target."""
    actual_x, actual_y = current_mouse()
    scope = active_scope_window()
    if scope is None:
        return actual_x, actual_y

    inside = _point_in_rect(actual_x, actual_y, scope)
    close = abs(actual_x - int(expected_x)) <= tolerance and abs(actual_y - int(expected_y)) <= tolerance
    if inside and close:
        return actual_x, actual_y

    recovered = _recover_after_pointer_safety_failure()
    raise PointerSafetyConflict(
        "pointer backend did not preserve the active process/window scope; "
        f"expected ({expected_x}, {expected_y}), observed ({actual_x}, {actual_y}), "
        f"recovery={recovered}. No click/scroll action was issued after the failed move."
    )


def _move_resolved_pointer(px: int, py: int, *, duration: float = 0.0) -> tuple[int, int, float]:
    """Move to an already-validated absolute target and verify scoped safety.

    For scoped Windows work, SetCursorPos is used for the actual virtual-desktop
    position because it is native multi-monitor aware. If the cursor starts
    outside the scope, the entry is instantaneous so unrelated desktop content
    is not traversed.
    """
    require_pointer_allowed(px, py)
    move_duration = max(0.0, float(duration))
    scope = active_scope_window()
    current_x, current_y = current_mouse()

    if scope is not None and not _point_in_rect(current_x, current_y, scope):
        move_duration = 0.0

    if scope is not None and os.name == "nt":
        if move_duration <= 0.0:
            if not _native_set_cursor_pos(px, py):
                _dispatch_backend("pointer-move", pyautogui.moveTo, px, py, duration=0.0)
        else:
            # A rectangle is convex: interpolating between two points already
            # inside the scope cannot leave the scope. Use native absolute
            # cursor placement so secondary-monitor coordinates remain exact.
            steps = max(1, min(120, int(round(move_duration * 60))))
            step_delay = move_duration / steps
            start_x, start_y = current_x, current_y
            for index in range(1, steps + 1):
                _require_live_dispatch()
                ratio = index / steps
                ix = int(round(start_x + (px - start_x) * ratio))
                iy = int(round(start_y + (py - start_y) * ratio))
                require_pointer_allowed(ix, iy)
                if not _native_set_cursor_pos(ix, iy):
                    _dispatch_backend("pointer-move", pyautogui.moveTo, px, py, duration=max(0.0, move_duration - step_delay * (index - 1)))
                    break
                if index != steps and step_delay > 0:
                    time.sleep(step_delay)
    else:
        _dispatch_backend("pointer-move", pyautogui.moveTo, px, py, duration=move_duration)

    if scope is not None:
        actual_x, actual_y = _verify_pointer_after_scoped_move(px, py)
    else:
        actual_x, actual_y = current_mouse()
    return actual_x, actual_y, move_duration


def move_mouse(x: Any, y: Any, *, duration: float = 0.15, normalized: bool = False, coordinate_space: Any = None) -> dict[str, Any]:
    px, py = resolve_xy(x, y, normalized=normalized, coordinate_space=coordinate_space)
    actual_x, actual_y, move_duration = _move_resolved_pointer(px, py, duration=duration)
    return {
        "ok": True,
        "x": actual_x,
        "y": actual_y,
        "resolved_target": {"x": px, "y": py},
        "duration": move_duration,
    }


def click_mouse(
    x: Any | None = None,
    y: Any | None = None,
    *,
    button: Any = "left",
    clicks: int = 1,
    interval: float = 0.12,
    normalized: bool = False,
    coordinate_space: Any = None,
    target_area: Any = "client",
) -> dict[str, Any]:
    button_name = validate_button(button)
    clicks = int(clicks)
    if not 1 <= clicks <= 5:
        raise ValueError("clicks must be between 1 and 5")
    interval = max(0.0, float(interval))

    target_binding: dict[str, Any] | None = None
    if x is None and y is None:
        current_x, current_y = current_mouse()
        require_pointer_allowed(current_x, current_y)
        if active_scope_window() is not None:
            target_binding = validate_scoped_click_target(current_x, current_y, target_area=target_area)
        _dispatch_backend("click", pyautogui.click, clicks=clicks, interval=interval, button=button_name)
    elif x is None or y is None:
        raise ValueError("provide both x and y, or neither")
    else:
        px, py = resolve_xy(x, y, normalized=normalized, coordinate_space=coordinate_space)
        if active_scope_window() is not None:
            # In scoped mode, position and verify first. Never let PyAutoGUI
            # combine movement + click, because a backend coordinate mismatch
            # could otherwise click outside the fail-closed scope. After the
            # move, perform a fresh foreground + hit-test so a local window
            # appearing above the target cannot receive the click.
            actual_x, actual_y, _ = _move_resolved_pointer(px, py, duration=0.0)
            target_binding = validate_scoped_click_target(actual_x, actual_y, target_area=target_area)
            _dispatch_backend("click", pyautogui.click, clicks=clicks, interval=interval, button=button_name)
        else:
            require_pointer_allowed(px, py)
            _dispatch_backend(
                "click", pyautogui.click,
                x=px, y=py, clicks=clicks, interval=interval, button=button_name,
            )

    px, py = current_mouse()
    result = {
        "ok": True,
        "x": px,
        "y": py,
        "button": button_name,
        "clicks": clicks,
    }
    if target_binding is not None:
        result["target_binding"] = target_binding
    return result


def drag_mouse(
    x: Any,
    y: Any,
    *,
    duration: float = 0.5,
    button: Any = "left",
    normalized: bool = False,
    coordinate_space: Any = None,
) -> dict[str, Any]:
    px, py = resolve_xy(x, y, normalized=normalized, coordinate_space=coordinate_space)
    require_pointer_allowed(px, py)
    start_x, start_y = current_mouse()
    require_pointer_allowed(start_x, start_y)
    button_name = validate_button(button)
    scope = active_scope_window()
    if scope is not None:
        if not _point_in_rect(start_x, start_y, scope):
            raise ValueError("drag start must already be inside the active process/window scope")
        _dispatch_backend("mouse-down", pyautogui.mouseDown, button=button_name)
        try:
            actual_x, actual_y, _ = _move_resolved_pointer(px, py, duration=max(0.0, float(duration)))
        finally:
            _dispatch_backend("mouse-up", pyautogui.mouseUp, button=button_name)
        return {"ok": True, "x": actual_x, "y": actual_y, "button": button_name, "resolved_target": {"x": px, "y": py}}
    _dispatch_backend("drag", pyautogui.dragTo, px, py, duration=max(0.0, float(duration)), button=button_name)
    return {"ok": True, "x": px, "y": py, "button": button_name}


def mouse_button_down(*, button: Any = "left") -> dict[str, Any]:
    button_name = validate_button(button)
    px, py = current_mouse()
    require_pointer_allowed(px, py)
    _dispatch_backend("mouse-down", pyautogui.mouseDown, button=button_name)
    px, py = current_mouse()
    return {"ok": True, "x": px, "y": py, "button": button_name, "state": "down"}


def mouse_button_up(*, button: Any = "left") -> dict[str, Any]:
    button_name = validate_button(button)
    _dispatch_backend("mouse-up", pyautogui.mouseUp, button=button_name)
    px, py = current_mouse()
    return {"ok": True, "x": px, "y": py, "button": button_name, "state": "up"}


def scroll_mouse(amount: int, *, x: Any | None = None, y: Any | None = None, normalized: bool = False, coordinate_space: Any = None) -> dict[str, Any]:
    amount = int(amount)
    if amount == 0:
        raise ValueError("amount must not be 0")

    if x is None and y is None:
        current_x, current_y = current_mouse()
        require_pointer_allowed(current_x, current_y)
        _dispatch_backend("scroll", pyautogui.scroll, amount)
    elif x is None or y is None:
        raise ValueError("provide both x and y, or neither")
    else:
        px, py = resolve_xy(x, y, normalized=normalized, coordinate_space=coordinate_space)
        if active_scope_window() is not None:
            _move_resolved_pointer(px, py, duration=0.0)
            _dispatch_backend("scroll", pyautogui.scroll, amount)
        else:
            require_pointer_allowed(px, py)
            _dispatch_backend("scroll", pyautogui.scroll, amount, x=px, y=py)

    px, py = current_mouse()
    return {"ok": True, "amount": amount, "x": px, "y": py}


def _send_unicode_text_windows(text: str, interval: float) -> None:
    """Inject UTF-16 through Windows, without changing the user's clipboard."""
    import ctypes
    from ctypes import wintypes

    class MouseInput(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KeyboardInput(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class HardwareInput(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]

    send = ctypes.windll.user32.SendInput
    send.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    send.restype = wintypes.UINT
    dispatch_started = False
    for char in text.replace("\r\n", "\n"):
        _require_live_dispatch()
        try:
            pyautogui.failSafeCheck()
        except pyautogui.FailSafeException as exc:
            if dispatch_started:
                raise ActionDispatchError("Unicode typing stopped after partial dispatch; re-observe before retrying") from exc
            raise
        if char in "\r\n\t\b":
            units = [( {"\r": 0x0D, "\n": 0x0D, "\t": 0x09, "\b": 0x08}[char], 0, 0)]
        else:
            encoded = char.encode("utf-16-le")
            units = [(0, int.from_bytes(encoded[i:i + 2], "little"), 0x0004) for i in range(0, len(encoded), 2)]
        inputs = (Input * (len(units) * 2))()
        for i, (vk, scan, flags) in enumerate(units):
            inputs[i * 2].type = inputs[i * 2 + 1].type = 1
            inputs[i * 2].data.ki = KeyboardInput(vk, scan, flags, 0, 0)
            inputs[i * 2 + 1].data.ki = KeyboardInput(vk, scan, flags | 0x0002, 0, 0)
        dispatch_started = True
        if getattr(ACTION_DISPATCH_CONTEXT, "controller_id", None):
            ACTION_DISPATCH_CONTEXT.possibly_sent = True
        sent = int(send(len(inputs), inputs, ctypes.sizeof(Input)))
        if sent != len(inputs):
            raise ActionDispatchError("Windows did not accept the complete Unicode input; re-observe before retrying", details={"events_sent": sent, "events_requested": len(inputs)})
        if interval:
            time.sleep(interval)


def type_text(text: Any, *, interval: float = 0.02) -> dict[str, Any]:
    require_scope_focused_for_keyboard()
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    # Validate before any dispatch, so invalid surrogates cannot partially type.
    text.encode("utf-16-le")
    interval = max(0.0, float(interval))
    _begin_keyboard_injection()
    try:
        if os.name == "nt":
            _dispatch_backend("type", _send_unicode_text_windows, text, interval)
        else:
            _dispatch_backend("type", pyautogui.write, text, interval=interval)
    finally:
        _end_keyboard_injection()
    return {"ok": True, "characters": len(text), "interval": interval}


def press_key(key: Any, *, presses: int = 1, interval: float = 0.05) -> dict[str, Any]:
    require_scope_focused_for_keyboard()
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    presses = int(presses)
    if not 1 <= presses <= 100:
        raise ValueError("presses must be between 1 and 100")
    interval = max(0.0, float(interval))
    _begin_keyboard_injection()
    try:
        # Bound the cancellation latency for repeated keypresses.
        for index in range(presses):
            _dispatch_backend("key", pyautogui.press, key)
            if index + 1 < presses and interval:
                time.sleep(interval)
    finally:
        _end_keyboard_injection()
    return {"ok": True, "key": key, "presses": presses}


def press_hotkey(keys: Any) -> dict[str, Any]:
    require_scope_focused_for_keyboard()
    if (
        not isinstance(keys, list)
        or not keys
        or not all(isinstance(key, str) and key for key in keys)
    ):
        raise ValueError("keys must be a non-empty list of strings")
    _begin_keyboard_injection()
    try:
        _dispatch_backend("hotkey", pyautogui.hotkey, *keys)
    finally:
        _end_keyboard_injection()
    return {"ok": True, "keys": keys}


def key_down(key: Any) -> dict[str, Any]:
    require_scope_focused_for_keyboard()
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    _begin_keyboard_injection()
    try:
        _dispatch_backend("key-down", pyautogui.keyDown, key)
    finally:
        _end_keyboard_injection()
    return {"ok": True, "key": key, "state": "down"}


def key_up(key: Any) -> dict[str, Any]:
    require_scope_focused_for_keyboard()
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    _begin_keyboard_injection()
    try:
        _dispatch_backend("key-up", pyautogui.keyUp, key)
    finally:
        _end_keyboard_injection()
    return {"ok": True, "key": key, "state": "up"}


def recover_pointer() -> dict[str, Any]:
    """Move pointer to the center of the currently controllable area."""
    bounds = _effective_pointer_bounds()
    target_x, target_y = _safe_recovery_point()
    if not _native_set_cursor_pos(target_x, target_y):
        old_failsafe = pyautogui.FAILSAFE
        try:
            pyautogui.FAILSAFE = False
            _dispatch_backend("pointer-recovery-move", pyautogui.moveTo, target_x, target_y, duration=0.0)
        finally:
            pyautogui.FAILSAFE = old_failsafe
    actual_x, actual_y = current_mouse()
    require_pointer_allowed(actual_x, actual_y)
    return {
        "ok": True,
        "x": actual_x,
        "y": actual_y,
        "resolved_target": {"x": target_x, "y": target_y},
        "recovered": True,
        "bounds": bounds,
    }


def _perform_action_unlocked(action: str, data: dict[str, Any]) -> dict[str, Any]:
    """Canonical action dispatcher. Caller must hold COMPUTER_ACTION_LOCK."""
    normalized = bool(data.get("normalized", False))
    coordinate_space = data.get("coordinate_space")

    if action == "move":
        return move_mouse(
            data.get("x"),
            data.get("y"),
            duration=data.get("duration", 0.15),
            normalized=normalized,
            coordinate_space=coordinate_space,
        )

    if action == "click":
        return click_mouse(
            data.get("x"),
            data.get("y"),
            button=data.get("button", "left"),
            clicks=data.get("clicks", 1),
            interval=data.get("interval", 0.12),
            normalized=normalized,
            coordinate_space=coordinate_space,
            target_area=data.get("target_area", "client"),
        )

    if action == "drag":
        return drag_mouse(
            data.get("x"),
            data.get("y"),
            duration=data.get("duration", 0.5),
            button=data.get("button", "left"),
            normalized=normalized,
            coordinate_space=coordinate_space,
        )

    if action == "mouse-down":
        return mouse_button_down(button=data.get("button", "left"))

    if action == "mouse-up":
        return mouse_button_up(button=data.get("button", "left"))

    if action == "scroll":
        return scroll_mouse(
            data.get("amount", 0),
            x=data.get("x"),
            y=data.get("y"),
            normalized=normalized,
            coordinate_space=coordinate_space,
        )

    if action == "type":
        return type_text(data.get("text"), interval=data.get("interval", 0.02))

    if action == "key":
        return press_key(
            data.get("key"),
            presses=data.get("presses", 1),
            interval=data.get("interval", 0.05),
        )

    if action == "key-down":
        return key_down(data.get("key"))

    if action == "key-up":
        return key_up(data.get("key"))

    if action == "hotkey":
        return press_hotkey(data.get("keys"))

    if action == "recover":
        return recover_pointer()

    raise ValueError(f"unknown action: {action}")


def _remote_surface_windows_for_unbound_action(action: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return remote-desktop windows an unbound action could affect.

    This is a safety detector, not a second target resolver.  It uses the same
    canonical coordinate preview/current-focus facts and only decides whether a
    state-bound observation is required before the existing action owner runs.
    """
    windows: list[dict[str, Any]] = []

    def add_at(px: int, py: int) -> None:
        window = window_at(px, py)
        if window and _is_remote_desktop_window(window):
            windows.append(window)

    if action in {"move", "click", "drag", "scroll"}:
        x, y = data.get("x"), data.get("y")
        if x is None and y is None:
            px, py = current_mouse()
            add_at(px, py)
        elif x is not None and y is not None:
            preview = pointer_target_preview(
                x, y, normalized=bool(data.get("normalized", False)),
                coordinate_space=data.get("coordinate_space"),
            )
            point = preview.get("resolved") or {}
            add_at(int(point.get("x", 0)), int(point.get("y", 0)))
            if action == "drag":
                sx, sy = current_mouse(); add_at(sx, sy)
    elif action in {"mouse-down", "mouse-up"}:
        px, py = current_mouse(); add_at(px, py)
    elif action in {"type", "key", "key-down", "key-up", "hotkey"}:
        window = focused_window()
        if window and _is_remote_desktop_window(window):
            windows.append(window)
    return windows


def _require_remote_surface_state_binding(action: str, data: dict[str, Any], target_binding: dict[str, Any] | None) -> None:
    if target_binding is not None or action == "recover":
        return
    remote = _remote_surface_windows_for_unbound_action(action, data)
    if not remote:
        return
    raise ObservationStateError(
        "action blocked because it targets a Remote Desktop surface without a current state_id; call GET /v1/app-state while that RDP window is focused/scoped, then use its state-bound raster",
        code="REMOTE_SURFACE_STATE_REQUIRED",
        retry="reobserve",
    )


def perform_action(action: str, data: dict[str, Any]) -> dict[str, Any]:
    """Serialize complete computer actions, enrich responses, and keep bounded history."""
    with COMPUTER_ACTION_LOCK:
        return _perform_action_serialized(action, data)


def _perform_action_serialized(action: str, data: dict[str, Any]) -> dict[str, Any]:
    request = dict(data)
    journal_event = None
    try:
        resolved = resolve_state_bound_action(data, action=action)
        # Adapter-only metadata must not leak into the canonical PyAutoGUI owner.
        action_data = dict(resolved)
        target_binding = action_data.pop("resolved_target", None)
        action_data.pop("state_id", None)
        _require_remote_surface_state_binding(action, action_data, target_binding)
        controller_id = action_data.pop("controller_id", None)
        controller = CONTROLLER_LEASE.validate_action(controller_id)
        dry_run = bool(action_data.get("dry_run", False))
        if dry_run:
            result = preview_action(action, action_data)
        else:
            with COMPUTER_ACTION_LOCK:
                pointer_preview = None
                if action in {"move", "click", "drag", "scroll"}:
                    pointer_preview = preview_action(action, action_data)
                    pointer_preview.pop("state", None)
                    pointer_preview.pop("dry_run", None)
                    pointer_preview.pop("ok", None)
                    pointer_preview.pop("action", None)
                journal_event = AGENT_JOURNAL.begin_action(action, request, controller_id)
                # Canonical single-use RDP admission: the next real action
                # requires a new raster. Local UI states remain reusable.
                if target_binding is not None:
                    OBSERVATION_STATES.consume_remote_action(request.get("state_id"))
                ACTION_DISPATCH_CONTEXT.controller_id = controller_id
                ACTION_DISPATCH_CONTEXT.possibly_sent = False
                try:
                    _require_live_dispatch()
                    result = _perform_action_unlocked(action, action_data)
                finally:
                    ACTION_DISPATCH_CONTEXT.controller_id = None
                    ACTION_DISPATCH_CONTEXT.possibly_sent = False
                if pointer_preview is not None:
                    result["preview"] = pointer_preview
                result["state"] = current_action_state()
        if target_binding is not None:
            result["observation_binding"] = target_binding
        if controller is not None:
            result["controller"] = {
                "owner": controller.get("owner"),
                "controller_id": controller.get("controller_id"),
                "stopped": bool(controller.get("stopped", False)),
            }
        result["action_sent"] = not dry_run
        result["dispatch_status"] = "not_started" if dry_run else "sent"
        result["effect_verified"] = False
        result["retry"] = "none"
        result["verification_note"] = (
            "Input dispatch is not proof of the requested UI effect. Re-observe with /v1/app-state or /v1/observe before deciding the next step."
        )
        history_id = _record_history(action, request, result=result)
        result["history_id"] = history_id
        event, journal_event = journal_event, None
        AGENT_JOURNAL.end_action(event, result=result)
        # Optional self-observation reuses the canonical app-state owner. It
        # never asserts that a Windows dispatch achieved the intended result.
        capture = AGENT_JOURNAL.post_action_capture_options(controller_id) if event is not None else None
        if capture is not None:
            try:
                _app_state_snapshot_unlocked(include_image=capture["include_image"],
                                             max_depth=2, max_nodes=100,
                                             controller_id=controller_id,
                                             observation_source="automatic_after_action")
                result["journal_post_observation"] = "recorded_not_verified"
            except Exception as capture_error:
                try:
                    AGENT_JOURNAL.record_observation_failure(controller_id, capture_error)
                except OSError:
                    pass  # The dispatch already happened; never reclassify it.
                result["journal_post_observation"] = "unavailable_not_verified"
        return result
    except Exception as exc:
        _record_history(action, request, error=str(exc))
        AGENT_JOURNAL.end_action(journal_event, error=exc)
        raise


# ---------------------------------------------------------------------------
# HTTP adapter: transport only. It delegates actions to the Core above.
# ---------------------------------------------------------------------------




def canonical_api_path(path: str) -> str:
    """Accept both stable /v1 paths and legacy unversioned paths."""
    if path == f"/{API_VERSION}":
        return "/"
    prefix = f"/{API_VERSION}/"
    if path.startswith(prefix):
        return "/" + path[len(prefix):]
    return path


def agent_quickstart(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Compact self-description intended to be consumed directly by an AI agent."""
    base = f"http://{host}:{port}/{API_VERSION}"
    return {
        "service": "agent-computer-bridge",
        "version": VERSION,
        "build_id": BUILD_ID,
        "preferred_api": base,
        "rule": "Acquire control -> observe -> act -> observe again -> finish. Never assume dispatch proves the requested UI effect.",
        "preferred_agent_entry": {
            "mcp": "Configure agent_computer_bridge_mcp.py as a local stdio MCP server. It owns discovery/lease lifecycle but routes all desktop behavior through this HTTP/Core owner.",
            "shell_fallback": "python agent_computer_bridge.py agent-session",
            "finish_fallback": "python agent_computer_bridge.py agent-finish --controller-id <id>",
        },
        "first_steps": [
            "Preferred: MCP computer_start -> computer_observe -> state-bound MCP action -> computer_observe -> computer_finish",
            f"Without MCP: run `python agent_computer_bridge.py agent-session`; it starts this server if needed and acquires the controller lease in one step",
            f"Use GET {base}/doctor only when environment health is uncertain",
            f"Use GET {base}/discovery only for low-level/manual HTTP integration",
        ],
        "core_capabilities": {
            "observe": ["discovery", "app-state", "observe", "screen", "mouse", "screenshot", "pixel", "viewport", "windows", "processes", "focused-window", "window-at", "monitors", "scope", "accessibility-tree", "status"],
            "pointer": ["move", "click", "drag", "mouse-down", "mouse-up", "scroll", "recover", "dry_run"],
            "keyboard": ["type", "key", "key-down", "key-up", "hotkey", "clipboard-get", "clipboard-set"],
            "workflow": ["click-and-screenshot", "move-and-screenshot", "wait", "history", "stop"],
            "control_session": ["controller status", "controller-acquire", "controller-heartbeat", "controller-stop", "controller-release"],
            "diagnostics": ["doctor"],
            "user_recording": ["record CLI", "recording status", "recording-start", "recording-stop", "JSON + HTML + screenshot ZIP export"],
            "agent_journal": ["journal-start", "journal-checkpoint", "journal-read", "journal-stop", "durable dispatch/observation evidence"],
            "rdp_safety": [
                "viewport set/show/clear",
                "outside-viewport pointer targets are rejected",
                "known Remote Desktop client windows are classified as nested remote surfaces",
                "unbound mutations into a remote surface are rejected with REMOTE_SURFACE_STATE_REQUIRED",
                "remote content uses state_id + image_x/image_y from the RDP client-area raster; local UIA is not treated as remote controls",
            ],
            "process_scope": ["scope by process/pid/hwnd/title", "coordinate_space is mandatory for pointer x/y while scoped", "use window_scope for window-relative coordinates or virtual_desktop for absolute Windows coordinates", "scoped clicks are target-bound by foreground + WindowFromPoint hit-test and default to the client area", "capture_provider=auto prefers HWND-bound Windows.Graphics.Capture when the optional backend is available and reports any fallback", "keyboard input is blocked if another window has focus", "stale scopes fail closed"],
            "user_runtime_indicator": ["single thin click-through monitor/scope boundary", "small non-obscuring cursor glow", "physical Esc stops active controller first; a second press while already stopped closes the server"],
            "display_model": ["virtual-desktop coordinates", "Win32 GetCursorPos", "per-monitor-v2 DPI awareness requested before GUI/input libraries load"],
        },
        "coordinates": {
            "origin": "top-left",
            "absolute": "virtual-desktop pixels by default without scope; while a window scope is active, coordinate_space is mandatory for every pointer request that supplies x/y.",
            "normalized": "set normalized=true and use x/y in 0.0..1.0",
            "failsafe_corner_margin_px": FAILSAFE_MARGIN,
        },
        "important": [
            "Prefer /v1 paths; unversioned paths exist only for compatibility.",
            "If status version/build differs from this file, an older server process is still running.",
            "POST /move requires both x and y; missing or out-of-virtual-desktop coordinates return HTTP 400 rather than becoming a no-op or clamped action.",
            "clicks must be 1..5 and key presses must be 1..100; invalid counts return HTTP 400 instead of being clamped into real repeated input.",
            "Use POST /recover if PyAutoGUI reports a failsafe conflict.",
            "For robust multi-agent use, acquire /v1/controller-acquire before mutating the desktop. While a lease exists, mutations without its controller_id fail closed; legacy identifierless mutations remain compatible only while no lease is active.",
            "If dispatch_status=possibly_sent, do not blindly retry. Re-observe first because the local backend cannot prove whether the OS received part of the input sequence.",
            "When an RDP viewport is configured, ignore dimmed screenshot areas outside its border.",
            "Remote Desktop is a nested surface: when /v1/app-state reports surface.kind=remote_desktop, do not use local UIA element indices. Use only state_id + image_x/image_y from that state's RDP client-area raster, then re-observe after every meaningful remote UI change.",
            "Raw/unbound click, drag, scroll or keyboard input into a recognized Remote Desktop window is intentionally blocked; first focus/scope that RDP client and call /v1/app-state.",
            "On Windows, /mouse uses Win32 GetCursorPos and screenshots cover the full virtual desktop, including monitors with negative coordinates.",
            "For one-app work, POST /v1/scope with process/pid/hwnd/title. While scoped, every pointer request with x/y MUST include coordinate_space=window_scope or coordinate_space=virtual_desktop; both modes are scope-validated. Clear with POST /v1/scope-clear.",
            "A scoped click additionally requires the scoped window to be the effective foreground window and to visibly own the target point. target_area defaults to client; use target_area=window only for intentional local window-chrome clicks.",
            "For scoped screenshots, capture_provider=auto prefers HWND-bound Windows.Graphics.Capture when available; otherwise it falls back to the visible desktop-composite crop and reports fallback_used/fallback_reason in capture_binding. Explicit windows_graphics_capture never silently falls back.",
            "Screenshot labels beginning AGENT/ACTION/ABS are generated by this bridge for you, not by the application. Use ACTION X/Y together with ACTION coordinate_space for pointer calls.",
            "GET /v1/observe whenever you need a textual explanation of the current screenshot guides, cursor position, focused/window-under-pointer state, scope/viewport, and recent actions.",
            "When available, GET /v1/accessibility-tree for a bounded read-only UI Automation tree before relying on pixel/OCR guesses. The optional backend can be installed with: pip install uiautomation.",
            "For occlusion-independent scoped window capture, install the optional backend with: pip install windows-capture.",
            "On Windows, the running server shows one thin click-through boundary and a small cursor glow. Exact actionable coordinates stay in observations instead of covering the user desktop. Physical Esc first stops a live controller; a second press while that controller is already stopped closes the server. Overlay polling is used as a fallback when WH_KEYBOARD_LL cannot attach. POST /v1/controller-stop, CLI `stop`, or /v1/stop are deterministic fallbacks.",
        ],
        "full_schema": f"http://{host}:{port}/",
    }


def _interactive_desktop_status() -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False, "platform_supported": False, "reason": "Windows interactive desktop probe is not applicable."}
    try:
        import ctypes
        user32 = ctypes.windll.user32
        DESKTOP_READOBJECTS = 0x0001
        DESKTOP_SWITCHDESKTOP = 0x0100
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP)
        if not handle:
            return {"available": False, "platform_supported": True, "reason": "OpenInputDesktop failed; the process may not be attached to an interactive desktop."}
        try:
            return {"available": True, "platform_supported": True, "reason": None}
        finally:
            user32.CloseDesktop(ctypes.c_void_p(handle))
    except Exception as exc:
        return {"available": False, "platform_supported": True, "reason": f"interactive desktop probe failed: {exc}"}


def doctor_report(*, capture_probe: bool = False) -> dict[str, Any]:
    """Non-mutating runtime diagnostics for Computer Use prerequisites."""
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, **details: Any) -> None:
        checks.append({"name": name, "status": status, **details})

    is_windows = os.name == "nt"
    add("platform", "pass" if is_windows else "unsupported", value="windows" if is_windows else os.name)

    desktop = _interactive_desktop_status()
    add(
        "interactive_desktop",
        "pass" if desktop.get("available") else ("unsupported" if not desktop.get("platform_supported") else "fail"),
        **desktop,
    )

    dpi = dict(WINDOWS_DPI_STATE)
    add("dpi_awareness", "pass" if dpi.get("active") else ("unsupported" if not is_windows else "warn"), **dpi)

    try:
        monitors = list_monitors()
        add("monitors", "pass" if monitors else "fail", count=len(monitors), virtual_desktop=virtual_desktop_bounds())
    except Exception as exc:
        monitors = []
        add("monitors", "fail", error=str(exc))

    uia = uia_backend_status()
    add("uia_read_only", "pass" if uia.get("available") else ("unsupported" if not uia.get("platform_supported") else "warn"), **uia)

    capture = capture_provider_status()
    add("capture_providers", "pass", providers=capture.get("providers"), default=capture.get("default"))

    if capture_probe:
        try:
            image = capture_raw_desktop()
            add("desktop_capture_probe", "pass", width=image.size[0], height=image.size[1], pixels_captured=True)
        except Exception as exc:
            add("desktop_capture_probe", "fail", error=str(exc), pixels_captured=False)
    else:
        add("desktop_capture_probe", "skipped", reason="Set capture_probe=true to explicitly capture one diagnostic frame.")

    add("input_failsafe", "pass" if bool(pyautogui.FAILSAFE) else "fail", pyautogui_failsafe=bool(pyautogui.FAILSAFE))
    add(
        "physical_escape_stop",
        "pass" if PHYSICAL_ESCAPE_STOP_ACTIVE else "warn",
        active=bool(PHYSICAL_ESCAPE_STOP_ACTIVE),
        source=PHYSICAL_ESCAPE_STOP_SOURCE,
    )

    mandatory_fail = any(item["status"] == "fail" and item["name"] in {"interactive_desktop", "monitors", "input_failsafe"} for item in checks)
    if not is_windows:
        readiness = "unsupported"
    elif mandatory_fail:
        readiness = "blocked"
    elif any(item["status"] == "warn" for item in checks):
        readiness = "degraded"
    else:
        readiness = "ready"

    return {
        "ok": readiness in {"ready", "degraded"},
        "readiness": readiness,
        "time": _now_iso(),
        "checks": checks,
        "controller": CONTROLLER_LEASE.status(),
        "observation_registry": OBSERVATION_STATES.status(),
        "window_scope": window_scope_status(),
        "policy": {
            "mutating_probe": False,
            "capture_probe_default": False,
            "capture_probe_privacy": "When enabled, one local screenshot is captured for health checking but image bytes are not returned or persisted by doctor.",
        },
    }


def runtime_status(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    width, height = screen_size()
    desktop = virtual_desktop_bounds()
    return {
        "ok": True,
        "service": "agent-computer-bridge",
        "version": VERSION,
        "build_id": BUILD_ID,
        "pid": os.getpid(),
        "script": str(Path(__file__).resolve()),
        "python": sys.version.split()[0],
        "pyautogui": getattr(pyautogui, "__version__", "unknown"),
        "pillow": getattr(Image, "__version__", None),
        "dpi_awareness": dict(WINDOWS_DPI_STATE),
        "screen": {"width": width, "height": height, "kind": "primary_monitor"},
        "virtual_desktop": desktop,
        "monitors": list_monitors(),
        "viewport": load_viewport(),
        "window_scope": window_scope_status(),
        "accessibility": uia_backend_status(),
        "capture_providers": capture_provider_status(),
        "user_recording": USER_RECORDING.status(),
        "observation_states": OBSERVATION_STATES.status(),
        "controller": CONTROLLER_LEASE.status(),
        "runtime_visuals_active": RUNTIME_VISUALS_ACTIVE,
        "physical_escape_stop_active": PHYSICAL_ESCAPE_STOP_ACTIVE,
        "physical_escape_stop_source": PHYSICAL_ESCAPE_STOP_SOURCE,
        "api": f"http://{host}:{port}/{API_VERSION}",
    }





def _capture_scope_verification_image(
    *, capture_provider: Any = DEFAULT_CAPTURE_PROVIDER
) -> tuple[Image.Image, dict[str, int], dict[str, Any]] | None:
    """Capture the scoped client/window region without bridge annotations."""
    scope = active_scope_window()
    if scope is None:
        return None
    region = _client_rect_screen(int(scope["hwnd"])) or {
        "x": int(scope["x"]), "y": int(scope["y"]),
        "width": int(scope["width"]), "height": int(scope["height"]),
    }
    image, binding = _capture_scoped_raw(scope, capture_provider=capture_provider)
    # HWND capture is already scope-relative. Desktop fallback is also returned
    # as an exact scope crop by _capture_scoped_raw, so one local crop path is
    # sufficient for both providers.
    local_region = {
        "x": int(region["x"]) - int(scope["x"]),
        "y": int(region["y"]) - int(scope["y"]),
        "width": int(region["width"]),
        "height": int(region["height"]),
    }
    try:
        cropped, mapped_region = crop_scoped_capture_region(image, scope, local_region)
    except ValueError:
        # Some themed/window-frame geometries can report a client rect touching
        # just outside the outer rect. Fall back to the full scoped capture and
        # preserve that fact in metadata instead of silently fabricating pixels.
        cropped, mapped_region = image, {
            "x": int(scope["x"]), "y": int(scope["y"]),
            "width": int(scope["width"]), "height": int(scope["height"]),
        }
        binding = dict(binding)
        binding["client_crop_fallback"] = True
    return cropped, mapped_region, binding


def _visual_change_evidence(before: Image.Image, after: Image.Image, region: dict[str, int]) -> dict[str, Any]:
    if before.size != after.size:
        return {
            "available": True,
            "visual_change_observed": True,
            "reason": "capture_size_changed",
            "before_size": list(before.size),
            "after_size": list(after.size),
            "region_windows_pixels": region,
            "interpretation": "A visual change is evidence of UI state change, not proof that the target consumed the input.",
        }
    diff = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
    bbox = diff.getbbox()
    changed = bbox is not None
    return {
        "available": True,
        "visual_change_observed": changed,
        "changed_bbox_capture_pixels": list(bbox) if bbox is not None else None,
        "region_windows_pixels": region,
        "interpretation": (
            "A visual change is evidence of UI state change, not proof that the target consumed the click. "
            "No visual change is not proof of click failure."
        ),
    }


def perform_action_and_screenshot(action: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    try:
        wait_after = float(payload.pop("wait_after", 0.10))
    except (TypeError, ValueError) as exc:
        raise ValueError("wait_after must be numeric") from exc
    if not 0.0 <= wait_after <= 10.0:
        raise ValueError("wait_after must be between 0 and 10 seconds")
    show_cursor = bool(payload.pop("screenshot_cursor", True))
    show_viewport = bool(payload.pop("screenshot_viewport", True))
    use_scope = bool(payload.pop("screenshot_scope", True))
    capture_provider = payload.pop("screenshot_capture_provider", DEFAULT_CAPTURE_PROVIDER)
    capture_provider = normalize_capture_provider(capture_provider)
    verify_effect_raw = payload.pop("verify_effect", None)
    verify_effect = bool(verify_effect_raw) if verify_effect_raw is not None else (action == "click" and active_scope_window() is not None)
    region = payload.pop("region", None)
    if region is not None and not isinstance(region, dict):
        raise ValueError("region must be an object with x/y/width/height")

    before_capture: Image.Image | None = None
    verification_region: dict[str, int] | None = None
    if action == "click" and active_scope_window() is not None:
        # Run the exact dry-run target binding before the before-image. The
        # actual click path repeats this immediately before input to close the
        # race between observation and action.
        preview_action("click", payload)
        if verify_effect:
            captured = _capture_scope_verification_image(capture_provider=capture_provider)
            if captured is not None:
                before_capture, verification_region, before_binding = captured

    result = perform_action(action, payload)
    if wait_after:
        time.sleep(wait_after)

    verification: dict[str, Any] | None = None
    if before_capture is not None and verification_region is not None:
        captured_after = _capture_scope_verification_image(capture_provider=capture_provider)
        if captured_after is not None:
            after_capture, after_region, after_binding = captured_after
            if after_region == verification_region:
                verification = _visual_change_evidence(before_capture, after_capture, verification_region)
                verification["capture_binding_before"] = before_binding
                verification["capture_binding_after"] = after_binding
            else:
                verification = {
                    "available": True,
                    "visual_change_observed": True,
                    "reason": "target_region_changed_after_action",
                    "before_region_windows_pixels": verification_region,
                    "after_region_windows_pixels": after_region,
                    "interpretation": "The target geometry changed; this is UI state evidence, not proof of input delivery.",
                    "capture_binding_before": before_binding,
                    "capture_binding_after": after_binding,
                }

    response = {
        "ok": True,
        "action": result,
        "screenshot": screenshot_as_json(
            show_cursor=show_cursor, show_viewport=show_viewport, use_scope=use_scope,
            region=region, capture_provider=capture_provider,
        ),
        "observation": observation_snapshot(
            include_image=False, history_limit=3,
            show_cursor=show_cursor, show_viewport=show_viewport, use_scope=use_scope,
            capture_provider=capture_provider,
        ),
    }
    if action == "click" and active_scope_window() is not None:
        response["target_binding"] = result.get("target_binding") or result.get("preview", {}).get("target_binding")
        response["effect_verification"] = verification or {
            "available": False,
            "reason": "verify_effect_disabled_or_capture_unavailable",
            "interpretation": "No delivery claim is made without post-action evidence.",
        }
    return response

def endpoint_catalog() -> list[dict[str, Any]]:
    """Return every documented API operation with a compact executable-style example."""
    doc = openapi_document()
    items: list[dict[str, Any]] = []
    sample_query_values = {
        "x": "1000", "y": "500", "normalized": "false", "limit": "20",
        "cursor": "true", "viewport": "true", "width": "200", "height": "200",
    }
    for path, methods in doc.get("paths", {}).items():
        for method, op in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            stable_path = f"/{API_VERSION}{path}"
            item: dict[str, Any] = {
                "method": method.upper(),
                "path": stable_path,
                "purpose": op.get("summary", ""),
            }
            if method.lower() == "post":
                content = op.get("requestBody", {}).get("content", {}).get("application/json", {})
                item["example"] = content.get("example", {})
            else:
                query_parts: list[str] = []
                for param in op.get("parameters", []):
                    if param.get("in") != "query" or not param.get("required"):
                        continue
                    name = str(param.get("name"))
                    query_parts.append(f"{name}={sample_query_values.get(name, '1')}")
                item["example"] = stable_path + (("?" + "&".join(query_parts)) if query_parts else "")
            items.append(item)
    items.sort(key=lambda item: (item["path"], item["method"]))
    return items


def discovery_document(host: str, port: int, *, auth_required: bool, authenticated: bool = True) -> dict[str, Any]:
    status = runtime_status(host, port)
    caps = {
        "observe": ["observe","app-state","screen","mouse","screenshot","pixel","viewport","windows","processes","focused-window","window-at","monitors","scope"],
        "pointer": ["move","click","drag","mouse-down","mouse-up","scroll","recover","dry_run"],
        "keyboard": ["type","key","key-down","key-up","hotkey"],
        "workflow": ["click-and-screenshot","move-and-screenshot","wait","history"],
        "control_session": ["controller","controller-acquire","controller-heartbeat","controller-stop","controller-release"],
        "diagnostics": ["doctor"],
        "user_recording": ["recording","recording-start","recording-stop"],
        "clipboard": ["clipboard-get","clipboard-set"] if os.name == "nt" else [],
        "accessibility": ["accessibility-tree"] if uia_backend_status().get("available") else [],
        "capture": ["desktop_composite", "windows_graphics_capture"] if windows_graphics_capture_backend_status().get("available") else ["desktop_composite"],
        "runtime": ["single-thin-monitor-boundary","subtle-cursor-glow","machine-readable-screenshot-guides","physical-escape-stop","per-monitor-dpi-awareness"],
    }
    return {
        "ok": True,
        "service": "agent-computer-bridge",
        "version": VERSION,
        "build_id": BUILD_ID,
        "api_version": API_VERSION,
        "base_url": f"http://{host}:{port}/{API_VERSION}",
        "auth": {"required": auth_required, "authenticated": authenticated, "scheme": "Bearer" if auth_required else None},
        "capabilities": caps,
        "runtime": status,
        "capture_providers": capture_provider_status(),
        "accessibility": {
            **uia_backend_status(),
            "endpoint": f"/{API_VERSION}/accessibility-tree",
            "policy": "read-only observation; real input remains in the canonical pointer/keyboard core",
        },
        "user_recording": {
            "status": USER_RECORDING.status(),
            "preferred_user_entry": "python agent_computer_bridge.py record",
            "policy": "passive physical-input observation; injected bridge input ignored; raw keystroke text payloads are not captured; UIA values are never requested; screenshots/visible UI labels may contain on-screen text",
            "output": "session.json + steps.json + annotated JPEG screenshots + report.html + ZIP archive",
        },
        "focused_window": focused_window(),
        "window_scope": window_scope_status(),
        "controller": CONTROLLER_LEASE.status(),
        "controller_policy": {
            "compatibility": "Mutations remain identifierless only while no controller lease is active.",
            "exclusive_control": "Once acquired, all mutating requests must present the matching controller_id.",
            "heartbeat": "Successful mutations and explicit heartbeats renew the lease.",
            "kill_switch": "controller-stop blocks that controller until release or lease expiry. Physical Esc stops live control first; a second physical Esc while it is already stopped closes the server.",
            "expiry": f"Lease range {CONTROLLER_LEASE_MIN_SECONDS:g}..{CONTROLLER_LEASE_MAX_SECONDS:g}s; default {CONTROLLER_LEASE_DEFAULT_SECONDS:g}s.",
        },
        "window_scope_policy": {
            "selection": "process, pid, hwnd, and/or title; ambiguous matches return HTTP 409 with candidates",
            "pointer_coordinates": "coordinate_space is mandatory while scoped: window_scope = window-relative; virtual_desktop = absolute Windows coordinates; both remain scope-validated",
            "screenshots": "capture_provider=auto prefers HWND-bound Windows.Graphics.Capture when available; otherwise reports desktop-composite fallback. use ?scope=false for full desktop",
            "keyboard": "allowed only while the scoped window is focused",
            "stale_target": "fail closed; never silently returns to full-desktop control",
        },
        "remote_surface_policy": {
            "recognized_processes": sorted(REMOTE_DESKTOP_PROCESS_NAMES),
            "classification": "Known RDP client process/class windows are treated as nested remote-desktop surfaces.",
            "observation": "app-state exposes surface.kind=remote_desktop and crops the canonical raster to the local RDP client area.",
            "targeting": "Remote content is raster-only: state_id + image_x/image_y. Local Windows UIA is never promoted to remote-control semantics.",
            "state_transition": "Switching between local and remote surfaces, moving/resizing the RDP client area, or moving it to another monitor invalidates the old state_id.",
            "unbound_mutation": "Blocked with REMOTE_SURFACE_STATE_REQUIRED; re-observe before acting.",
        },
        "viewport_policy": {
            "viewport": load_viewport(),
            "outside_pointer_targets": "HTTP 400",
            "out_of_virtual_desktop_targets": "HTTP 400",
            "failsafe_corners": f"nudged inward by {FAILSAFE_MARGIN}px",
        },
        "action_response_contract": "Mutating actions report action_sent/dispatch_status/effect_verified/retry plus current state and history_id. action_sent may be null when a backend failed after dispatch began and delivery is unknowable; dispatch_status=possibly_sent requires re-observation before any retry. Delivery is never proof of UI effect.",
        "visual_annotation_contract": visual_guide_contract(),
        "observation_endpoint": {
            "path": "/v1/observe",
            "purpose": "Machine-readable sidecar explaining current screenshot annotations, actionable coordinates, focus/window/scope, viewport and recent actions.",
            "one_call_with_image": "/v1/observe?include_image=true",
        },
        "dry_run": "Set dry_run=true on move/click/drag/scroll to validate and resolve targets without input.",
        "endpoints": endpoint_catalog(),
        "openapi": f"http://{host}:{port}/{API_VERSION}/openapi.json",
        "preferred_agent_entry": {
            "mcp": "agent_computer_bridge_mcp.py (stdio) hides controller/discovery plumbing and never exposes unbound raw clicks",
            "shell_fallback": "python agent_computer_bridge.py agent-session",
            "finish_fallback": "python agent_computer_bridge.py agent-finish --controller-id <id>",
        },
        "recommended_loop": [
            "MCP: computer_start once",
            "computer_observe for a fresh target-bound state (or low-level GET /v1/app-state if MCP is unavailable)",
            "act only against that observation: local element_index preferred; remote_desktop uses that raster's image_x/image_y",
            "if dispatch_status=possibly_sent or retry=reobserve, re-observe before deciding whether a retry is safe",
            "computer_observe again to verify the requested UI effect",
            "computer_finish when done; use shutdown=always when the user expects no bridge process left running",
        ],
        "coordinate_model": {
            "primary_monitor": {"x": 0, "y": 0, "width": status["screen"]["width"], "height": status["screen"]["height"]},
            "virtual_desktop": status["virtual_desktop"],
            "absolute_pointer_coordinates": "Windows virtual-desktop coordinates; negative x/y are valid when monitor layout uses them",
            "normalized_pointer_coordinates": "0..1 across the selected coordinate space; while scoped, coordinate_space must be explicit",
            "mouse_source_windows": "Win32 GetCursorPos",
            "screenshots_windows": "Desktop provider: Pillow ImageGrab all_screens=True; scoped auto provider: optional HWND-bound windows-capture / Windows.Graphics.Capture with transparent desktop fallback",
            "dpi_awareness": status.get("dpi_awareness"),
        },
        "limitations": [
            "Windows requests per-monitor-v2 DPI awareness before PyAutoGUI/Pillow/Tk load; mixed-DPI monitor layouts still require live verification on real hardware.",
            "OCR is intentionally not included. Accessibility-tree is optional/read-only and requires the lightweight 'uiautomation' package on Windows.",
            "Window scope uses real OS pointer input constrained by validation; it does not inject WM_MOUSE messages into target processes.",
            "Remote Desktop detection is intentionally conservative and based on known Microsoft RDP client process/class identities. Unknown third-party remote-control clients remain ordinary windows until explicitly added.",
            "Scoped screenshots use capture_provider=auto: HWND-bound Windows.Graphics.Capture is preferred when the optional windows-capture backend is available; otherwise desktop-composite cropping remains the reported fallback.",
        ],
    }

def probe_server(host: str, port: int, *, token: str | None = None, timeout: float = 0.45) -> dict[str, Any] | None:
    """Probe an already-running service on the target port without requiring dependencies."""
    url = f"http://{host}:{port}/{API_VERSION}/status"
    headers = {"Connection": "close"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(256 * 1024)
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict):
                return payload
            return {"reachable": True, "http_status": response.status}
    except urllib.error.HTTPError as exc:
        server_header = exc.headers.get("Server", "") if exc.headers else ""
        return {
            "reachable": True,
            "http_status": exc.code,
            "server_header": server_header,
            "note": "Port answered HTTP but status could not be read (possibly token-protected or older bridge).",
        }
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def print_agent_banner(host: str, port: int) -> None:
    script = str(Path(__file__).resolve())
    base = f"http://{host}:{port}/{API_VERSION}"
    print("=" * 72, flush=True)
    print("AGENT COMPUTER BRIDGE READY", flush=True)
    print(f"file      : {script}", flush=True)
    print(f"version   : {VERSION}    build: {BUILD_ID}    pid: {os.getpid()}", flush=True)
    print(f"api       : {base}", flush=True)
    print(f"discovery : {base}/discovery", flush=True)
    print(f"schema    : {base}/openapi.json", flush=True)
    print("AGENT BOOTSTRAP: discovery -> controller-acquire -> app-state -> act -> app-state verify -> controller-release", flush=True)
    print("Core: state-bound window/UIA/raster observation, screenshots, pointer/keyboard, wait, history, clipboard.", flush=True)
    print("RDP: configure viewport so dimmed/outside areas are ignored and blocked.", flush=True)
    print("Windows runtime UI: glowing monitor borders + small non-obscuring cursor glow while server is active.", flush=True)
    print("VISUAL GUIDE: /v1/app-state binds element/raster targets; /v1/observe remains the lightweight diagnostic view.", flush=True)
    if PHYSICAL_ESCAPE_STOP_ACTIVE:
        print("USER STOP: physical Esc is ACTIVE. It stops an active controller session first; otherwise it stops the server. Agent/injected Esc is ignored.", flush=True)
    else:
        print("USER STOP: physical Esc hook unavailable here; use Ctrl+C or POST /v1/stop.", flush=True)
    print("=" * 72, flush=True)


def openapi_document() -> dict[str, Any]:
    """Machine-readable API contract so an agent need not inspect this source file."""
    coord_props = {
        "x": {"type": "number", "description": "Absolute Windows virtual-desktop coordinate when no scope is active; may be negative on monitors left/above primary."},
        "y": {"type": "number", "description": "Absolute Windows virtual-desktop coordinate when no scope is active; may be negative on monitors left/above primary."},
        "normalized": {"type": "boolean", "default": False},
        "coordinate_space": {
            "type": "string",
            "enum": ["auto", "window_scope", "virtual_desktop"],
            "description": "Coordinate space for x/y. Without an active scope, omitted/auto means virtual_desktop. While a process/window scope is active this field is REQUIRED: window_scope = window-relative, virtual_desktop = absolute Windows coordinates. Undocumented aliases are rejected rather than silently remapped."
        },
        "dry_run": {"type": "boolean", "default": False, "description": "Validate/resolve without performing pointer input."},
        "controller_id": {"type": "string", "description": "Controller lease id from /v1/controller-acquire. Required for mutating actions while a controller lease is active."},
        "state_id": {"type": "string", "description": "Optional /v1/app-state id. When supplied the observed focused/scoped window must still be current."},
        "element_index": {"type": "integer", "minimum": 0, "description": "Optional element index from the referenced app-state. Resolves to the current element center through read-only UIA, then uses the canonical pointer owner."},
        "image_x": {"type": "integer", "minimum": 0, "description": "Optional x pixel in the raster returned by the referenced app-state."},
        "image_y": {"type": "integer", "minimum": 0, "description": "Optional y pixel in the raster returned by the referenced app-state."},
    }

    def post_op(summary: str, properties: dict[str, Any], required: list[str] | None = None, example: dict[str, Any] | None = None) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        content: dict[str, Any] = {"schema": schema}
        if example is not None:
            content["example"] = example
        return {
            "summary": summary,
            "requestBody": {
                "required": bool(required),
                "content": {"application/json": content},
            },
            "responses": {
                "200": {"description": "Success"},
                "400": {"description": "Invalid request"},
                "401": {"description": "Unauthorized when token auth is enabled"},
                "409": {"description": "PyAutoGUI failsafe triggered"},
                "413": {"description": "Request body exceeds configured limit"},
            },
        }

    paths: dict[str, Any] = {
        "/status": {"get": {"summary": "Verify running bridge version/build and runtime state", "responses": {"200": {"description": "Runtime status"}}}},
        "/agent-help": {"get": {"summary": "Compact AI-agent quickstart and capability guide", "responses": {"200": {"description": "Agent quickstart"}}}},
        "/screen": {"get": {"summary": "Get primary monitor plus Windows virtual-desktop geometry", "responses": {"200": {"description": "Screen information"}}}},
        "/mouse": {"get": {"summary": "Get canonical pointer position (Win32 GetCursorPos on Windows)", "responses": {"200": {"description": "Pointer information"}}}},
        "/pixel": {"get": {"summary": "Read RGB pixel color", "parameters": [
            {"name": "x", "in": "query", "required": True, "schema": {"type": "number"}},
            {"name": "y", "in": "query", "required": True, "schema": {"type": "number"}},
            {"name": "normalized", "in": "query", "schema": {"type": "boolean", "default": False}},
        ], "responses": {"200": {"description": "Pixel RGB"}}}},
        "/viewport": {
            "get": {"summary": "Get configured visible RDP viewport", "responses": {"200": {"description": "Viewport or null"}}},
            "post": post_op(
                "Set visible RDP viewport",
                {
                    "x": {"type": "number"}, "y": {"type": "number"},
                    "width": {"type": "number", "exclusiveMinimum": 0},
                    "height": {"type": "number", "exclusiveMinimum": 0},
                },
                ["x", "y", "width", "height"],
                {"x": 0, "y": 0, "width": 1920, "height": 1080},
            ),
        },
        "/screenshot": {
            "get": {
                "summary": "Get PNG screenshot",
                "parameters": [
                    {"name": "cursor", "in": "query", "schema": {"type": "boolean", "default": True}},
                    {"name": "viewport", "in": "query", "schema": {"type": "boolean", "default": True}},
                    {"name": "scope", "in": "query", "schema": {"type": "boolean", "default": True, "description": "Use active process/window scope when one is selected."}},
                    {"name": "capture_provider", "in": "query", "schema": {"type": "string", "enum": sorted(VALID_CAPTURE_PROVIDERS), "default": DEFAULT_CAPTURE_PROVIDER}, "description": "auto prefers HWND-bound Windows.Graphics.Capture for scoped windows when available; explicit windows_graphics_capture fails if unavailable."},
                    {"name": "x", "in": "query", "schema": {"type": "number"}},
                    {"name": "y", "in": "query", "schema": {"type": "number"}},
                    {"name": "width", "in": "query", "schema": {"type": "number", "exclusiveMinimum": 0}},
                    {"name": "height", "in": "query", "schema": {"type": "number", "exclusiveMinimum": 0}},
                ],
                "responses": {"200": {"description": "PNG image", "content": {"image/png": {}}}},
            }
        },
        "/move": {"post": post_op("Move pointer", {**coord_props, "duration": {"type": "number", "minimum": 0}}, ["x", "y"], {"x": 1000, "y": 500, "coordinate_space": "virtual_desktop", "duration": 0.15})},
        "/click": {"post": post_op("Click pointer; while scoped, fail closed unless the target window is focused and visibly owns the target point", {**coord_props, "button": {"type": "string", "enum": sorted(VALID_BUTTONS)}, "clicks": {"type": "integer", "minimum": 1, "maximum": 5}, "interval": {"type": "number", "minimum": 0}, "target_area": {"type": "string", "enum": ["client", "window"], "default": "client"}}, None, {"x": 1000, "y": 500, "coordinate_space": "virtual_desktop", "button": "left", "clicks": 1})},
        "/drag": {"post": post_op("Drag pointer to target", {**coord_props, "button": {"type": "string", "enum": sorted(VALID_BUTTONS)}, "duration": {"type": "number", "minimum": 0}}, ["x", "y"], {"x": 1400, "y": 700, "coordinate_space": "virtual_desktop", "button": "left", "duration": 0.5})},
        "/mouse-down": {"post": post_op("Hold mouse button", {"button": {"type": "string", "enum": sorted(VALID_BUTTONS)}, "state_id": coord_props["state_id"], "controller_id": coord_props["controller_id"]}, None, {"button": "left"})},
        "/mouse-up": {"post": post_op("Release mouse button", {"button": {"type": "string", "enum": sorted(VALID_BUTTONS)}, "state_id": coord_props["state_id"], "controller_id": coord_props["controller_id"]}, None, {"button": "left"})},
        "/scroll": {"post": post_op("Scroll wheel", {"amount": {"type": "integer"}, **coord_props}, ["amount"], {"amount": -5})},
        "/type": {"post": post_op("Type text", {"text": {"type": "string"}, "interval": {"type": "number", "minimum": 0}, "state_id": coord_props["state_id"], "controller_id": coord_props["controller_id"]}, ["text"], {"text": "Hello", "interval": 0.02})},
        "/key": {"post": post_op("Press key", {"key": {"type": "string"}, "presses": {"type": "integer", "minimum": 1, "maximum": 100}, "interval": {"type": "number", "minimum": 0}, "state_id": coord_props["state_id"], "controller_id": coord_props["controller_id"]}, ["key"], {"key": "enter"})},
        "/key-down": {"post": post_op("Hold keyboard key", {"key": {"type": "string"}, "state_id": coord_props["state_id"], "controller_id": coord_props["controller_id"]}, ["key"], {"key": "shift"})},
        "/key-up": {"post": post_op("Release keyboard key", {"key": {"type": "string"}, "state_id": coord_props["state_id"], "controller_id": coord_props["controller_id"]}, ["key"], {"key": "shift"})},
        "/hotkey": {"post": post_op("Press key combination", {"keys": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "state_id": coord_props["state_id"], "controller_id": coord_props["controller_id"]}, ["keys"], {"keys": ["ctrl", "l"]})},
        "/recover": {"post": post_op("Recover pointer from a PyAutoGUI failsafe corner", {}, None, {})},
        "/viewport-clear": {"post": post_op("Clear visible RDP viewport", {}, None, {})},
    }

    paths.update({
        "/app-state": {"get": {
            "summary": "State-bound observation: focused/scoped window, read-only UIA elements and optional raster",
            "parameters": [
                {"name":"include_image","in":"query","schema":{"type":"boolean","default":True}},
                {"name":"depth","in":"query","schema":{"type":"integer","minimum":0,"maximum":8,"default":4}},
                {"name":"max_nodes","in":"query","schema":{"type":"integer","minimum":1,"maximum":2000,"default":400}},
                {"name":"include_offscreen","in":"query","schema":{"type":"boolean","default":False}},
                {"name":"capture_provider","in":"query","schema":{"type":"string","enum":sorted(VALID_CAPTURE_PROVIDERS),"default":DEFAULT_CAPTURE_PROVIDER}},
                {"name":"controller_id","in":"query","schema":{"type":"string"},"description":"Optional controller lease to bind into this state_id."},
            ],
            "responses": {"200": {"description": "Observation with state_id; element indices and raster pixels are valid only with that state_id."}},
        }},
        "/observe": {"get": {
            "summary": "Current visual observation sidecar: explains agent overlays, actionable coordinates, focus/scope/viewport and recent actions",
            "parameters": [
                {"name":"include_image","in":"query","schema":{"type":"boolean","default":False},"description":"Include PNG as base64 in the JSON response."},
                {"name":"history","in":"query","schema":{"type":"integer","minimum":1,"maximum":20,"default":5}},
                {"name":"cursor","in":"query","schema":{"type":"boolean","default":True}},
                {"name":"viewport","in":"query","schema":{"type":"boolean","default":True}},
                {"name":"scope","in":"query","schema":{"type":"boolean","default":True}},
                {"name":"capture_provider","in":"query","schema":{"type":"string","enum":sorted(VALID_CAPTURE_PROVIDERS),"default":DEFAULT_CAPTURE_PROVIDER}},
            ],
            "responses": {"200": {"description": "Machine-readable visual observation and optional image"}}
        }},
        "/discovery": {"get": {"summary": "One-call agent bootstrap with live capabilities/examples", "responses": {"200": {"description": "Discovery document"}}}},
        "/doctor": {"get": {
            "summary": "Non-mutating Computer Use prerequisite diagnostics",
            "parameters": [{"name":"capture_probe","in":"query","schema":{"type":"boolean","default":False},"description":"Explicitly capture one local diagnostic frame; bytes are not returned or persisted by doctor."}],
            "responses": {"200": {"description": "Readiness and diagnostic checks"}},
        }},
        "/controller": {"get": {"summary": "Get current mutating-control lease status", "responses": {"200": {"description": "Controller status"}}}},
        "/controller-acquire": {"post": post_op("Acquire exclusive mutating-control lease", {"owner":{"type":"string"}, "lease_seconds":{"type":"number","minimum":CONTROLLER_LEASE_MIN_SECONDS,"maximum":CONTROLLER_LEASE_MAX_SECONDS}}, None, {"owner":"coding-agent","lease_seconds":120})},
        "/controller-heartbeat": {"post": post_op("Renew controller lease", {"controller_id":{"type":"string"}}, ["controller_id"], {"controller_id":"<id>"})},
        "/controller-stop": {"post": post_op("Stop this controller session; actions remain blocked until release or lease expiry", {"controller_id":{"type":"string"}, "reason":{"type":"string"}}, ["controller_id"], {"controller_id":"<id>","reason":"user requested stop"})},
        "/controller-release": {"post": post_op("Release controller lease", {"controller_id":{"type":"string"}}, ["controller_id"], {"controller_id":"<id>"})},
        "/accessibility-tree": {"get": {
            "summary": "Bounded read-only Windows UI Automation tree for the scoped/focused window",
            "parameters": [
                {"name":"hwnd","in":"query","schema":{"type":"integer"},"description":"Optional top-level HWND; defaults to active scope, otherwise focused window."},
                {"name":"depth","in":"query","schema":{"type":"integer","minimum":0,"maximum":8,"default":3}},
                {"name":"max_nodes","in":"query","schema":{"type":"integer","minimum":1,"maximum":2000,"default":250}},
                {"name":"include_offscreen","in":"query","schema":{"type":"boolean","default":False}},
            ],
            "responses": {"200": {"description": "Accessibility availability plus bounded UIA tree or unavailable reason"}}
        }},
        "/windows": {"get": {"summary": "List visible top-level windows", "responses": {"200": {"description": "Window list"}}}},
        "/processes": {"get": {"summary": "List visible processes and their top-level windows", "parameters": [{"name":"query","in":"query","schema":{"type":"string"}}], "responses": {"200": {"description": "Process/window groups"}}}},
        "/scope": {
            "get": {"summary": "Get active process/window scope", "responses": {"200": {"description": "Scope state"}}},
            "post": post_op("Select one process/window scope", {
                "process":{"type":"string","description":"Executable name, e.g. mstsc.exe"},
                "pid":{"type":"integer"}, "hwnd":{"type":"integer"},
                "title":{"type":"string","description":"Case-insensitive title substring"},
                "activate":{"type":"boolean","default":True}
            }, None, {"process":"mstsc.exe","activate":True}),
        },
        "/scope-clear": {"post": post_op("Clear process/window scope and return to virtual-desktop coordinates", {}, None, {})},
        "/focused-window": {"get": {"summary": "Get foreground window", "responses": {"200": {"description": "Focused window"}}}},
        "/window-at": {"get": {"summary": "Get top-level window under a point", "parameters": [
            {"name":"x","in":"query","required":True,"schema":{"type":"number"}},
            {"name":"y","in":"query","required":True,"schema":{"type":"number"}},
        ], "responses": {"200": {"description": "Window or null"}}}},
        "/monitors": {"get": {"summary": "Enumerate monitor geometry", "responses": {"200": {"description": "Monitor list"}}}},
        "/history": {"get": {"summary": "Recent bounded action history (typed text redacted)", "parameters": [{"name":"limit","in":"query","schema":{"type":"integer","minimum":1,"maximum":100,"default":20}}], "responses": {"200": {"description": "Action history"}}}},
        "/recording": {"get": {"summary": "Get passive human user-recording status", "responses": {"200": {"description": "Recording status and last export"}}}},
        "/recording-start": {"post": post_op("Start passive human demonstration recording", {"output_dir":{"type":"string"}, "capture_screenshots":{"type":"boolean","default":True}, "settle_ms":{"type":"integer","minimum":0,"maximum":3000,"default":DEFAULT_RECORDING_SETTLE_MS}, "capture_scope":{"type":"string","enum":sorted(RECORDING_CAPTURE_SCOPES),"default":DEFAULT_RECORDING_CAPTURE_SCOPE}, "capture_text":{"type":"boolean","default":True}}, None, {"capture_screenshots":True,"settle_ms":DEFAULT_RECORDING_SETTLE_MS,"capture_scope":DEFAULT_RECORDING_CAPTURE_SCOPE,"capture_text":True})},
        "/recording-stop": {"post": post_op("Stop recording and finalize JSON/HTML/screenshots ZIP", {}, None, {})},
        "/clipboard-get": {"get": {"summary": "Read Windows Unicode text clipboard", "responses": {"200": {"description": "Clipboard text"}}}},
        "/clipboard-set": {"post": post_op("Set Windows Unicode text clipboard", {"text":{"type":"string"}}, ["text"], {"text":"hello"})},
        "/wait": {"post": post_op("Wait fixed time or until a pixel matches", {
            "seconds":{"type":"number","minimum":0,"maximum":30},
            "until_pixel":{"type":"object"}, "timeout":{"type":"number","minimum":0,"maximum":30}, "poll_interval":{"type":"number","minimum":0.05,"maximum":5}
        }, None, {"seconds":0.25})},
        "/click-and-screenshot": {"post": post_op("Target-bound click then return state, post-action PNG and visual-change evidence", {**coord_props, "button":{"type":"string","enum":sorted(VALID_BUTTONS)}, "clicks":{"type":"integer","minimum":1,"maximum":5}, "interval":{"type":"number","minimum":0}, "target_area":{"type":"string","enum":["client","window"],"default":"client"}, "wait_after":{"type":"number","minimum":0,"maximum":10}, "verify_effect":{"type":"boolean"}, "screenshot_cursor":{"type":"boolean"}, "screenshot_viewport":{"type":"boolean"}, "screenshot_scope":{"type":"boolean"}, "screenshot_capture_provider":{"type":"string","enum":sorted(VALID_CAPTURE_PROVIDERS)}, "region":{"type":"object"}}, None, {"x":1000,"y":500,"coordinate_space":"virtual_desktop","target_area":"client","wait_after":0.15,"verify_effect":True})},
        "/move-and-screenshot": {"post": post_op("Move then return action state plus PNG base64", {**coord_props, "duration":{"type":"number","minimum":0}, "wait_after":{"type":"number","minimum":0,"maximum":10}, "screenshot_cursor":{"type":"boolean"}, "screenshot_viewport":{"type":"boolean"}, "screenshot_scope":{"type":"boolean"}, "screenshot_capture_provider":{"type":"string","enum":sorted(VALID_CAPTURE_PROVIDERS)}, "region":{"type":"object"}}, ["x","y"], {"x":1000,"y":500,"coordinate_space":"virtual_desktop"})},
        "/stop": {"post": post_op("Gracefully stop the local bridge", {}, None, {})},
    })

    intent_props = {key: {"type": "string", "maxLength": 4000}
                    for key in ("goal", "prerequisites", "success_criteria")}
    paths["/recording-start"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"].update(intent_props)
    paths.update({
        "/journal": {"get": {"summary": "Current durable agent journal", "responses": {"200": {"description": "Journal status"}}}},
        "/journal-start": {"post": post_op("Start durable agent execution evidence", {
            "controller_id": coord_props["controller_id"], **intent_props, "output_dir": {"type": "string"},
            "capture_text": {"type": "boolean", "default": False}, "capture_screenshots": {"type": "boolean", "default": True},
        }, ["controller_id"], {"controller_id": "<active-controller>", "goal": "Demonstrated task"})},
        "/journal-checkpoint": {"post": post_op("Persist agent-supplied progress and next step", {
            "controller_id": coord_props["controller_id"],
            **{key: {"type": "string", "maxLength": 4000} for key in ("completed", "next_step", "notes")},
        }, ["controller_id"], {"controller_id": "<active-controller>", "next_step": "Re-observe the result"})},
        "/journal-stop": {"post": post_op("Stop durable evidence collection", {"controller_id": coord_props["controller_id"]}, ["controller_id"])},
        "/journal-read": {"post": post_op("Read recovery context without replaying actions", {"session_dir": {"type": "string"}}, ["session_dir"])},
    })

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Agent Computer Bridge",
            "version": VERSION,
            "description": "Local screenshot and input bridge for AI agents. Stable API prefix: /v1; legacy unversioned paths remain supported.",
        },
        "servers": [{"url": f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/{API_VERSION}"}],
        "paths": paths,
        "x-agent-notes": {
            "coordinate_origin": "top-left",
            "normalized_coordinates": "Set normalized=true to use x/y in 0.0..1.0.",
            "failsafe": f"Out-of-range pointer targets are rejected; valid targets within {FAILSAFE_MARGIN}px of a PyAutoGUI failsafe point are nudged toward screen center. POST /recover explicitly escapes a corner.",
            "viewport": "When configured, pointer targets outside the visible RDP viewport are rejected.",
            "window_scope": "POST /scope with process/pid/hwnd/title to restrict pointer input to one window. While active, coordinate_space is mandatory for pointer requests with x/y: window_scope for window-relative coordinates or virtual_desktop for absolute Windows coordinates. capture_provider=auto prefers HWND-bound Windows.Graphics.Capture when available, otherwise reports desktop-composite fallback. Ambiguous matches return 409 candidates; stale targets fail closed.",
            "capture_provider": "auto | desktop_composite | windows_graphics_capture. Explicit windows_graphics_capture is fail-closed; auto may fallback and always reports the provider actually used.",
            "max_json_body_bytes": MAX_BODY_BYTES,
            "build_id": BUILD_ID,
            "agent_quickstart": agent_quickstart(),
        },
        "x-build-id": BUILD_ID,
    }


class RequestBodyTooLarge(ValueError):
    pass


class AgentBridgeHandler(BaseHTTPRequestHandler):
    server_version = f"AgentComputerBridge/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            f"[{time.strftime('%H:%M:%S')}] "
            f"{self.client_address[0]} - {fmt % args}",
            flush=True,
        )

    @property
    def configured_token(self) -> str | None:
        return getattr(self.server, "auth_token", None)

    def _content_length(self) -> int:
        raw = self.headers.get("Content-Length", "0")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length") from exc
        if value < 0:
            raise ValueError("invalid Content-Length")
        return value

    def authorized(self) -> bool:
        token = self.configured_token
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        return hmac.compare_digest(supplied, expected)

    def _drain_body(self, length: int) -> bool:
        """Discard a bounded body without allocating it; return False if connection closes."""
        if length <= 0:
            return True
        if length > MAX_REJECT_DRAIN_BYTES:
            self.close_connection = True
            return False
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        return remaining == 0

    def _discard_request_body_for_rejection(self) -> None:
        """Drain bounded rejected request bodies so HTTP/1.1 keep-alive stays aligned."""
        self._drain_body(self._content_length())

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        try:
            self._discard_request_body_for_rejection()
        except RequestBodyTooLarge as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_code": "INVALID_REQUEST", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except ValueError as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_code": "INVALID_REQUEST", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return False
        self.send_json(
            {"ok": False, "error": "unauthorized"},
            status=HTTPStatus.UNAUTHORIZED,
        )
        return False

    def send_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_png(self, png_data: bytes, *, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png_data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Agent-Bridge-Observe", f"/{API_VERSION}/observe")
        self.send_header("X-Agent-Bridge-Guide", "Crosshair=current cursor; use ACTION X/Y labels for pointer calls")
        for key, value in (extra_headers or {}).items():
            self.send_header(str(key), str(value))
        self.end_headers()
        self.wfile.write(png_data)

    def read_json(self) -> dict[str, Any]:
        content_length = self._content_length()
        if content_length <= 0:
            return {}
        if content_length > MAX_BODY_BYTES:
            self._drain_body(content_length)
            raise RequestBodyTooLarge(
                f"request body exceeds {MAX_BODY_BYTES} bytes"
            )

        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc

        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def read_mutation_json(self) -> dict[str, Any]:
        data = self.read_json()
        CONTROLLER_LEASE.validate_action(data.pop("controller_id", None))
        return data

    def do_GET(self) -> None:
        if not self.require_auth():
            return

        try:
            parsed = urlparse(self.path)
            path = canonical_api_path(parsed.path)
            query = parse_qs(parsed.query)

            if path in {"/", "/openapi.json"}:
                self.send_json(openapi_document())
                return

            if path == "/status":
                host, port = self.server.server_address[:2]
                self.send_json(runtime_status(str(host), int(port)))
                return

            if path == "/agent-help":
                host, port = self.server.server_address[:2]
                self.send_json(agent_quickstart(str(host), int(port)))
                return

            if path == "/discovery":
                host, port = self.server.server_address[:2]
                self.send_json(discovery_document(str(host), int(port), auth_required=bool(self.configured_token), authenticated=True))
                return

            if path == "/controller":
                self.send_json({"ok": True, "controller": CONTROLLER_LEASE.status()})
                return

            if path == "/journal":
                self.send_json(AGENT_JOURNAL.status())
                return

            if path == "/doctor":
                capture_probe = query.get("capture_probe", ["0"])[0].lower() in {"1", "true", "yes"}
                self.send_json(doctor_report(capture_probe=capture_probe))
                return

            if path == "/observe":
                include_image = query.get("include_image", ["0"])[0].lower() in {"1", "true", "yes"}
                show_cursor = query.get("cursor", ["1"])[0].lower() not in {"0", "false", "no"}
                show_viewport = query.get("viewport", ["1"])[0].lower() not in {"0", "false", "no"}
                use_scope = query.get("scope", ["1"])[0].lower() not in {"0", "false", "no"}
                capture_provider = query.get("capture_provider", [DEFAULT_CAPTURE_PROVIDER])[0]
                try:
                    history_limit = int(query.get("history", [5])[0])
                except (TypeError, ValueError) as exc:
                    raise ValueError("observe history must be an integer") from exc
                if not 1 <= history_limit <= 20:
                    raise ValueError("observe history must be between 1 and 20")
                self.send_json(observation_snapshot(
                    include_image=include_image, history_limit=history_limit,
                    show_cursor=show_cursor, show_viewport=show_viewport, use_scope=use_scope,
                    capture_provider=capture_provider,
                ))
                return

            if path == "/app-state":
                include_image = query.get("include_image", ["1"])[0].lower() not in {"0", "false", "no"}
                include_offscreen = query.get("include_offscreen", ["0"])[0].lower() in {"1", "true", "yes"}
                capture_provider = query.get("capture_provider", [DEFAULT_CAPTURE_PROVIDER])[0]
                try:
                    depth = int(query.get("depth", [4])[0])
                    max_nodes = int(query.get("max_nodes", [400])[0])
                except (TypeError, ValueError) as exc:
                    raise ValueError("app-state depth/max_nodes must be integers") from exc
                self.send_json(app_state_snapshot(
                    include_image=include_image,
                    max_depth=depth,
                    max_nodes=max_nodes,
                    include_offscreen=include_offscreen,
                    capture_provider=capture_provider,
                    controller_id=query.get("controller_id", [None])[0],
                ))
                return

            if path == "/accessibility-tree":
                def _q_int(name: str, default: int | None = None) -> int | None:
                    raw = query.get(name, [default])[0]
                    if raw is None:
                        return None
                    try:
                        return int(raw)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{name} must be an integer") from exc
                include_offscreen = query.get("include_offscreen", ["0"])[0].lower() in {"1", "true", "yes"}
                depth = _q_int("depth", 3)
                max_nodes = _q_int("max_nodes", 250)
                self.send_json(accessibility_tree(
                    hwnd=_q_int("hwnd"),
                    max_depth=3 if depth is None else depth,
                    max_nodes=250 if max_nodes is None else max_nodes,
                    include_offscreen=include_offscreen,
                ))
                return

            if path == "/windows":
                self.send_json({"ok": True, "windows": list_windows()})
                return

            if path == "/processes":
                self.send_json({"ok": True, "processes": list_processes(query.get("query", [None])[0])})
                return

            if path == "/scope":
                self.send_json({"ok": True, "scope": window_scope_status()})
                return

            if path == "/focused-window":
                self.send_json({"ok": True, "window": focused_window()})
                return

            if path == "/window-at":
                if "x" not in query or "y" not in query:
                    raise ValueError("window-at requires x and y query parameters")
                self.send_json({"ok": True, "window": window_at(query["x"][0], query["y"][0])})
                return

            if path == "/monitors":
                self.send_json({"ok": True, "monitors": list_monitors(), "virtual_desktop": virtual_desktop_bounds(), "pointer_core": "virtual-desktop-aware"})
                return

            if path == "/history":
                self.send_json({"ok": True, "history": get_action_history(query.get("limit", [20])[0])})
                return

            if path == "/recording":
                self.send_json(USER_RECORDING.status())
                return

            if path == "/clipboard-get":
                self.send_json(clipboard_get_text())
                return

            if path == "/screen":
                width, height = screen_size()
                desktop = virtual_desktop_bounds()
                self.send_json(
                    {
                        "ok": True,
                        "width": width,
                        "height": height,
                        "primary_monitor": {"x": 0, "y": 0, "width": width, "height": height},
                        "virtual_desktop": desktop,
                        "coordinate_origin": "Windows virtual desktop; monitor positions may be negative",
                        "normalized": {"x": [0.0, 1.0], "y": [0.0, 1.0], "maps_to": "virtual_desktop unless window scope is active"},
                        "pointer_coordinate_space": window_scope_status(),
                    }
                )
                return

            if path == "/mouse":
                x, y = current_mouse()
                desktop = virtual_desktop_bounds()
                nx = (x - desktop["x"]) / max(1, desktop["width"] - 1)
                ny = (y - desktop["y"]) / max(1, desktop["height"] - 1)
                payload = {
                    "ok": True, "x": x, "y": y,
                    "normalized_x": nx, "normalized_y": ny,
                    "virtual_desktop": desktop,
                    "inside_virtual_desktop": _point_in_rect(x, y, desktop),
                    "source": "win32.GetCursorPos" if os.name == "nt" else "pyautogui.position",
                    "window_scope": window_scope_status(),
                }
                scope_status = payload["window_scope"]
                if scope_status.get("active") and not scope_status.get("stale"):
                    w = scope_status["window"]
                    payload["scope_relative"] = {
                        "x": x - int(w["x"]), "y": y - int(w["y"]),
                        "inside": int(w["x"]) <= x < int(w["x"]) + int(w["width"]) and int(w["y"]) <= y < int(w["y"]) + int(w["height"]),
                    }
                self.send_json(payload)
                return

            if path == "/viewport":
                self.send_json({"ok": True, "viewport": load_viewport()})
                return

            if path == "/pixel":
                if "x" not in query or "y" not in query:
                    raise ValueError("pixel requires x and y query parameters")
                normalized = query.get("normalized", ["0"])[0].lower() in {"1", "true", "yes"}
                self.send_json(pixel_color(query["x"][0], query["y"][0], normalized=normalized))
                return

            if path == "/screenshot":
                show_cursor = query.get("cursor", ["1"])[0].lower() not in {"0", "false", "no"}
                show_viewport = query.get("viewport", ["1"])[0].lower() not in {"0", "false", "no"}
                use_scope = query.get("scope", ["1"])[0].lower() not in {"0", "false", "no"}
                capture_provider = query.get("capture_provider", [DEFAULT_CAPTURE_PROVIDER])[0]
                region_keys = {"x", "y", "width", "height"}
                present = region_keys.intersection(query)
                region = None
                if present:
                    if present != region_keys:
                        raise ValueError("screenshot region requires x, y, width, and height")
                    region = {k: query[k][0] for k in region_keys}
                info = screenshot_as_json(
                    show_cursor=show_cursor, show_viewport=show_viewport, use_scope=use_scope,
                    region=region, capture_provider=capture_provider,
                )
                raw = base64.b64decode(info["base64"])
                self.send_png(raw, extra_headers={
                    "X-Agent-Bridge-Capture-Provider": str(info.get("capture_provider") or "unknown"),
                    "X-Agent-Bridge-Capture-Mode": str(info.get("capture_binding", {}).get("capture_mode") or "unknown"),
                })
                return

            self.send_json(
                {"ok": False, "error": "not found"},
                status=HTTPStatus.NOT_FOUND,
            )

        except pyautogui.FailSafeException:
            self.send_json(
                {"ok": False, "error": "pyautogui failsafe triggered", "error_code": "FAILSAFE_TRIGGERED", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.CONFLICT,
            )
        except RequestBodyTooLarge as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_code": "INVALID_REQUEST", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except WindowScopeAmbiguous as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_code": "AMBIGUOUS_TARGET", "action_sent": False, "dispatch_status": "not_started", "retry": "never", "candidates": exc.candidates},
                status=HTTPStatus.CONFLICT,
            )
        except CaptureProviderUnavailable as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_type": "capture_provider_unavailable", "error_code": "CAPTURE_UNAVAILABLE", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except PointerSafetyConflict as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_type": "pointer_safety_conflict", "error_code": "POINTER_SAFETY_CONFLICT", "action_sent": None, "dispatch_status": "possibly_sent", "retry": "reobserve"},
                status=HTTPStatus.CONFLICT,
            )
        except ObservationStateError as exc:
            self.send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                    "action_sent": False,
                    "dispatch_status": "not_started",
                    "retry": exc.retry,
                },
                status=HTTPStatus.CONFLICT if exc.retry == "reobserve" else HTTPStatus.BAD_REQUEST,
            )
        except ControllerLeaseError as exc:
            self.send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                    "action_sent": False,
                    "dispatch_status": exc.dispatch_status,
                    "retry": exc.retry,
                    **({"details": exc.details} if exc.details else {}),
                },
                status=HTTPStatus.CONFLICT,
            )
        except ActionDispatchError as exc:
            self.send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                    "action_sent": exc.action_sent,
                    "dispatch_status": exc.dispatch_status,
                    "retry": exc.retry,
                    **({"details": exc.details} if exc.details else {}),
                },
                status=HTTPStatus.CONFLICT,
            )
        except ValueError as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_code": "INVALID_REQUEST", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            self.send_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        if not self.require_auth():
            return

        try:
            path = canonical_api_path(urlparse(self.path).path)
            action = path.lstrip("/")
            if action in {"journal-start", "journal-checkpoint", "journal-stop", "journal-read"}:
                data = self.read_json()
                allowed = {
                    "journal-start": {"controller_id", "output_dir", "goal", "prerequisites", "success_criteria", "capture_text", "capture_screenshots", "auto_observe_after_action"},
                    "journal-checkpoint": {"controller_id", "completed", "next_step", "notes"},
                    "journal-stop": {"controller_id"},
                    "journal-read": {"session_dir"},
                }[action]
                if set(data) - allowed:
                    raise ValueError(action + " received unknown fields")
                with COMPUTER_ACTION_LOCK:
                    if action == "journal-read":
                        if not isinstance(data.get("session_dir"), str):
                            raise ValueError("session_dir must be a string")
                        result = AGENT_JOURNAL.read(data["session_dir"])
                    else:
                        controller_id = data.pop("controller_id", None)
                        if not controller_id:
                            raise ValueError("controller_id is required")
                        if action == "journal-stop":
                            # A user-stopped controller must still be able to
                            # close its journal without enabling more input.
                            result = AGENT_JOURNAL.stop(controller_id)
                        else:
                            CONTROLLER_LEASE.validate_action(controller_id)
                            result = (AGENT_JOURNAL.start(controller_id, **data) if action == "journal-start"
                                      else AGENT_JOURNAL.checkpoint(controller_id, **data))
                self.send_json(result)
                return
            if action == "controller-acquire":
                data = self.read_json()
                allowed = {"owner", "lease_seconds"}
                unknown = sorted(set(data) - allowed)
                if unknown:
                    raise ValueError("controller-acquire unknown fields: " + ", ".join(unknown))
                with COMPUTER_ACTION_LOCK:
                    result = CONTROLLER_LEASE.acquire(owner=data.get("owner"), lease_seconds=data.get("lease_seconds"))
                    AGENT_JOURNAL.reconcile_controller(result["controller_id"])
                self.send_json(result)
                return
            if action == "controller-heartbeat":
                data = self.read_json()
                if sorted(set(data) - {"controller_id"}):
                    raise ValueError("controller-heartbeat accepts only controller_id")
                self.send_json(CONTROLLER_LEASE.heartbeat(data.get("controller_id")))
                return
            if action == "controller-stop":
                data = self.read_json()
                unknown = sorted(set(data) - {"controller_id", "reason"})
                if unknown:
                    raise ValueError("controller-stop unknown fields: " + ", ".join(unknown))
                self.send_json(CONTROLLER_LEASE.stop(data.get("controller_id"), reason=data.get("reason")))
                return
            if action == "controller-release":
                data = self.read_json()
                if sorted(set(data) - {"controller_id"}):
                    raise ValueError("controller-release accepts only controller_id")
                with COMPUTER_ACTION_LOCK:
                    AGENT_JOURNAL.stop_if_owned(data.get("controller_id"))
                    result = CONTROLLER_LEASE.release(data.get("controller_id"))
                self.send_json(result)
                return
            if action == "recording-start":
                data = self.read_json()
                allowed = {"output_dir", "capture_screenshots", "settle_ms", "capture_scope", "capture_text", "goal", "prerequisites", "success_criteria"}
                unknown = sorted(set(data) - allowed)
                if unknown:
                    raise ValueError("recording-start unknown fields: " + ", ".join(unknown))
                capture_screenshots = data.get("capture_screenshots", True)
                if not isinstance(capture_screenshots, bool):
                    raise ValueError("recording-start capture_screenshots must be a boolean")
                capture_text = data.get("capture_text", True)
                if not isinstance(capture_text, bool):
                    raise ValueError("recording-start capture_text must be a boolean")
                output_dir = data.get("output_dir")
                if output_dir is not None and not isinstance(output_dir, str):
                    raise ValueError("recording-start output_dir must be a string")
                result = USER_RECORDING.start(
                    output_dir=output_dir,
                    capture_screenshots=capture_screenshots,
                    settle_ms=data.get("settle_ms", DEFAULT_RECORDING_SETTLE_MS),
                    capture_scope=data.get("capture_scope", DEFAULT_RECORDING_CAPTURE_SCOPE),
                    capture_text=capture_text,
                    goal=data.get("goal", ""),
                    prerequisites=data.get("prerequisites", ""),
                    success_criteria=data.get("success_criteria", ""),
                )
                self.send_json(result)
                return
            if action == "recording-stop":
                data = self.read_json()
                if data:
                    raise ValueError("recording-stop does not accept request fields")
                self.send_json(USER_RECORDING.stop())
                return
            if action == "scope":
                with COMPUTER_ACTION_LOCK:
                    data = self.read_mutation_json()
                    result = set_window_scope(data)
                result["history_id"] = _record_history("scope", data, result=result)
                self.send_json(result)
                return
            if action == "scope-clear":
                with COMPUTER_ACTION_LOCK:
                    data = self.read_mutation_json()
                    if data:
                        raise ValueError("scope-clear does not accept request fields")
                    result = {"ok": True, "cleared": clear_window_scope(), "scope": window_scope_status()}
                result["history_id"] = _record_history("scope-clear", data, result=result)
                self.send_json(result)
                return
            if action == "viewport":
                with COMPUTER_ACTION_LOCK:
                    data = self.read_mutation_json()
                    result = {"ok": True, "viewport": save_viewport(data.get("x"), data.get("y"), data.get("width"), data.get("height"))}
                result["history_id"] = _record_history("viewport", data, result=result)
                self.send_json(result)
                return
            if action == "viewport-clear":
                # Consume the request body even though this endpoint needs no
                # payload. With HTTP/1.1 keep-alive, unread bytes would become
                # the prefix of the next request line (for example ``{}GET``),
                # which BaseHTTPRequestHandler correctly rejects as HTTP 501.
                with COMPUTER_ACTION_LOCK:
                    data = self.read_mutation_json()
                    if data:
                        raise ValueError("viewport-clear does not accept request fields")
                    result = {"ok": True, "cleared": clear_viewport()}
                result["history_id"] = _record_history("viewport-clear", data, result=result)
                self.send_json(result)
                return
            if action == "wait":
                data = self.read_json()
                result = wait_for_condition(data)
                result["history_id"] = _record_history("wait", data, result=result)
                self.send_json(result)
                return
            if action == "clipboard-set":
                with COMPUTER_ACTION_LOCK:
                    data = self.read_mutation_json()
                    result = clipboard_set_text(data.get("text"))
                result["history_id"] = _record_history("clipboard-set", data, result=result)
                self.send_json(result)
                return
            if action == "click-and-screenshot":
                self.send_json(perform_action_and_screenshot("click", self.read_json()))
                return
            if action == "move-and-screenshot":
                self.send_json(perform_action_and_screenshot("move", self.read_json()))
                return
            if action == "stop":
                data = self.read_json()
                if data:
                    raise ValueError("stop does not accept request fields")
                result = {"ok": True, "stopping": True, "pid": os.getpid()}
                result["history_id"] = _record_history("stop", data, result=result)
                self.send_json(result)
                threading.Thread(target=self.server.shutdown, name="http-stop", daemon=True).start()
                return
            if action not in {
                "move",
                "click",
                "drag",
                "mouse-down",
                "mouse-up",
                "scroll",
                "type",
                "key",
                "key-down",
                "key-up",
                "hotkey",
                "recover",
            }:
                # Keep the connection framing valid even for unknown POST
                # endpoints. A client may have already sent a JSON body before
                # learning that the route does not exist.
                self._drain_body(self._content_length())
                self.send_json(
                    {"ok": False, "error": "not found"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return

            result = perform_action(action, self.read_json())
            self.send_json(result)

        except pyautogui.FailSafeException:
            self.send_json(
                {"ok": False, "error": "pyautogui failsafe triggered", "error_code": "FAILSAFE_TRIGGERED", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.CONFLICT,
            )
        except RequestBodyTooLarge as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_code": "INVALID_REQUEST", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except WindowScopeAmbiguous as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_code": "AMBIGUOUS_TARGET", "action_sent": False, "dispatch_status": "not_started", "retry": "never", "candidates": exc.candidates},
                status=HTTPStatus.CONFLICT,
            )
        except CaptureProviderUnavailable as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_type": "capture_provider_unavailable", "error_code": "CAPTURE_UNAVAILABLE", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except PointerSafetyConflict as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_type": "pointer_safety_conflict", "error_code": "POINTER_SAFETY_CONFLICT", "action_sent": None, "dispatch_status": "possibly_sent", "retry": "reobserve"},
                status=HTTPStatus.CONFLICT,
            )
        except ObservationStateError as exc:
            self.send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                    "action_sent": False,
                    "dispatch_status": "not_started",
                    "retry": exc.retry,
                },
                status=HTTPStatus.CONFLICT if exc.retry == "reobserve" else HTTPStatus.BAD_REQUEST,
            )
        except ControllerLeaseError as exc:
            self.send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                    "action_sent": False,
                    "dispatch_status": exc.dispatch_status,
                    "retry": exc.retry,
                    **({"details": exc.details} if exc.details else {}),
                },
                status=HTTPStatus.CONFLICT,
            )
        except ActionDispatchError as exc:
            self.send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                    "action_sent": exc.action_sent,
                    "dispatch_status": exc.dispatch_status,
                    "retry": exc.retry,
                    **({"details": exc.details} if exc.details else {}),
                },
                status=HTTPStatus.CONFLICT,
            )
        except ValueError as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "error_code": "INVALID_REQUEST", "action_sent": False, "dispatch_status": "not_started", "retry": "never"},
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            self.send_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def run_server(host: str, port: int, token: str | None) -> None:
    global RUNTIME_VISUALS_ACTIVE, PHYSICAL_ESCAPE_STOP_ACTIVE, PHYSICAL_ESCAPE_STOP_SOURCE

    existing = probe_server(host, port, token=token)
    if existing is not None:
        existing_version = existing.get("version")
        existing_build = existing.get("build_id")
        same_build = existing_version == VERSION and existing_build == BUILD_ID
        if same_build:
            print_json({
                "ok": True,
                "already_running": True,
                "version": existing_version,
                "build_id": existing_build,
                "pid": existing.get("pid"),
                "api": existing.get("api", f"http://{host}:{port}/{API_VERSION}"),
                "discovery": f"http://{host}:{port}/{API_VERSION}/discovery",
                "agent_help": f"http://{host}:{port}/{API_VERSION}/agent-help",
            })
            return
        raise OSError(
            "STALE_OR_FOREIGN_SERVER: target port is already in use. "
            f"Current file is v{VERSION} build {BUILD_ID}, but the running endpoint reported "
            f"version={existing_version!r} build={existing_build!r} status={existing.get('http_status')!r}. "
            "Stop the old bridge process before testing this file, or choose another --port. "
            f"Verify with: python {Path(__file__).name} status --port {port}"
        )

    server = ThreadingHTTPServer((host, port), AgentBridgeHandler)
    server.auth_token = token

    stop_signal = Path(tempfile.gettempdir()) / f"agent_computer_bridge_stop_{os.getpid()}.flag"
    overlay_hook_status = Path(tempfile.gettempdir()) / f"agent_computer_bridge_overlay_hook_{os.getpid()}.flag"
    for signal_path in (stop_signal, overlay_hook_status):
        try:
            signal_path.unlink(missing_ok=True)
        except OSError:
            pass

    def request_shutdown_from_escape() -> None:
        stopped = CONTROLLER_LEASE.stop_active_from_user(reason="physical Esc kill switch")
        if stopped is not None:
            print("Physical Esc pressed: stopped active computer-control session; bridge remains running.", flush=True)
            return
        print("Physical Esc pressed with no active controller: stopping Agent Computer Bridge...", flush=True)
        server.shutdown()

    # First try the hook in the server process. Detached/RDP contexts may reject
    # this, so the visible overlay also installs the same hook as a fallback.
    esc_hook = WindowsPhysicalEscapeHook(
        request_shutdown_from_escape,
        injection_sentinel=keyboard_injection_sentinel_path(os.getpid()),
    )
    parent_escape_active = esc_hook.start()

    overlay_process = launch_runtime_overlay(os.getpid(), stop_signal, overlay_hook_status, parent_escape_active)
    if overlay_process is not None:
        time.sleep(0.15)
        RUNTIME_VISUALS_ACTIVE = overlay_process.poll() is None
    else:
        RUNTIME_VISUALS_ACTIVE = False

    overlay_escape_active = False
    overlay_escape_mode = "0"
    if RUNTIME_VISUALS_ACTIVE:
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline and not overlay_hook_status.exists():
            time.sleep(0.03)
        try:
            overlay_escape_mode = overlay_hook_status.read_text(encoding="ascii").strip()
            overlay_escape_active = overlay_escape_mode in {"1", "poll"}
        except OSError:
            overlay_escape_active = False
            overlay_escape_mode = "0"
    PHYSICAL_ESCAPE_STOP_ACTIVE = bool(parent_escape_active or overlay_escape_active)
    if parent_escape_active and overlay_escape_mode == "1":
        PHYSICAL_ESCAPE_STOP_SOURCE = "server+overlay"
    elif parent_escape_active:
        PHYSICAL_ESCAPE_STOP_SOURCE = "server"
    elif overlay_escape_mode == "1":
        PHYSICAL_ESCAPE_STOP_SOURCE = "overlay"
    elif overlay_escape_mode == "poll":
        PHYSICAL_ESCAPE_STOP_SOURCE = "overlay-poll"
    else:
        PHYSICAL_ESCAPE_STOP_SOURCE = "unavailable"

    stop_watcher_done = threading.Event()
    def watch_overlay_stop_signal() -> None:
        while not stop_watcher_done.wait(0.05):
            if stop_signal.exists():
                try:
                    stop_signal.unlink(missing_ok=True)
                except OSError:
                    pass
                stopped = CONTROLLER_LEASE.stop_active_from_user(reason="physical Esc kill switch")
                if stopped is not None:
                    print("Physical Esc from runtime overlay: stopped active computer-control session; bridge remains running.", flush=True)
                    continue
                print("Physical Esc from runtime overlay with no active controller: stopping Agent Computer Bridge...", flush=True)
                server.shutdown()
                return
    stop_watcher = threading.Thread(target=watch_overlay_stop_signal, name="overlay-esc-stop", daemon=True)
    stop_watcher.start()

    print_agent_banner(host, port)
    width, height = screen_size()
    desktop = virtual_desktop_bounds()
    monitors = list_monitors()
    print(f"Primary monitor: {width}x{height}", flush=True)
    print(f"Virtual desktop: x={desktop['x']} y={desktop['y']} {desktop['width']}x{desktop['height']} | monitors={len(monitors)}", flush=True)
    print("Screenshot cursor marker: enabled by default", flush=True)
    print(f"Runtime overlay: {'active' if RUNTIME_VISUALS_ACTIVE else 'unavailable'}", flush=True)
    print(f"Physical Esc stop hook: {'active' if PHYSICAL_ESCAPE_STOP_ACTIVE else 'unavailable'}", flush=True)
    print("PyAutoGUI failsafe: out-of-range targets are rejected; failsafe corners are nudged inward.", flush=True)
    print("Authentication: " + ("enabled" if token else "disabled (local-only by default)"), flush=True)
    print("Stop: physical Esc stops active control first (or the server when idle); Ctrl+C always stops the server.", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        try:
            AGENT_JOURNAL.close()
        except Exception as exc:
            print(f"Agent journal finalization failed: {exc}", file=sys.stderr, flush=True)
        if USER_RECORDING.status().get("active"):
            try:
                result = USER_RECORDING.stop()
                print(f"User recording finalized: {result.get('archive')}", flush=True)
            except Exception as exc:
                print(f"User recording finalization failed: {exc}", file=sys.stderr, flush=True)
        PHYSICAL_ESCAPE_STOP_ACTIVE = False
        PHYSICAL_ESCAPE_STOP_SOURCE = "unavailable"
        stop_watcher_done.set()
        esc_hook.stop()
        if overlay_process is not None and overlay_process.poll() is None:
            overlay_process.terminate()
            try:
                overlay_process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                overlay_process.kill()
        RUNTIME_VISUALS_ACTIVE = False
        for signal_path in (stop_signal, overlay_hook_status, keyboard_injection_sentinel_path(os.getpid())):
            try:
                signal_path.unlink(missing_ok=True)
            except OSError:
                pass
        server.server_close()


def start_detached(host: str, port: int, token: str | None) -> dict[str, Any]:
    """Start a detached bridge process and return immediately to an autonomous agent."""
    existing = probe_server(host, port, token=token)
    if existing is not None:
        if existing.get("version") == VERSION and existing.get("build_id") == BUILD_ID:
            return {
                "ok": True,
                "already_running": True,
                "version": VERSION,
                "build_id": BUILD_ID,
                "pid": existing.get("pid"),
                "api": existing.get("api", f"http://{host}:{port}/{API_VERSION}"),
                "next": f"GET http://{host}:{port}/{API_VERSION}/discovery",
            }
        raise OSError(
            "STALE_OR_FOREIGN_SERVER: port already answers, but it is not this build. "
            f"Expected v{VERSION}/{BUILD_ID}; got v{existing.get('version')}/{existing.get('build_id')}. "
            "Stop the old process or select another port."
        )

    cmd = [sys.executable, str(Path(__file__).resolve()), "serve", "--host", host, "--port", str(port)]
    if token:
        cmd.extend(["--token", token])

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(cmd, **kwargs)
    deadline = time.monotonic() + 4.0
    status = None
    while time.monotonic() < deadline:
        time.sleep(0.08)
        status = probe_server(host, port, token=token, timeout=0.25)
        if status is not None and status.get("version") == VERSION and status.get("build_id") == BUILD_ID:
            break
        if process.poll() is not None:
            raise OSError(f"bridge process exited during startup with code {process.returncode}")
    else:
        raise OSError("bridge process started but did not become reachable with the expected version/build")

    return {
        "ok": True,
        "started": True,
        "pid": status.get("pid") if isinstance(status, dict) else process.pid,
        "version": VERSION,
        "build_id": BUILD_ID,
        "api": f"http://{host}:{port}/{API_VERSION}",
        "discovery": f"http://{host}:{port}/{API_VERSION}/discovery",
        "agent_help": f"http://{host}:{port}/{API_VERSION}/agent-help",
        "next": [
            f"GET http://{host}:{port}/{API_VERSION}/discovery",
            f"GET http://{host}:{port}/{API_VERSION}/observe",
            f"GET http://{host}:{port}/{API_VERSION}/screenshot",
        ],
    }


def query_status(host: str, port: int, token: str | None = None) -> dict[str, Any]:
    status = probe_server(host, port, token=token)
    if status is None:
        return {
            "ok": False,
            "running": False,
            "expected_version": VERSION,
            "expected_build_id": BUILD_ID,
            "api": f"http://{host}:{port}/{API_VERSION}",
        }
    return {
        "ok": True,
        "running": True,
        "matches_current_file": status.get("version") == VERSION and status.get("build_id") == BUILD_ID,
        "expected_version": VERSION,
        "expected_build_id": BUILD_ID,
        "reported": status,
    }


def bridge_http_json(
    host: str,
    port: int,
    path: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Small local client used by agent-session/finish and the MCP adapter.

    It deliberately talks to the running HTTP owner instead of importing its
    mutable controller/scope state into another process.
    """
    clean = str(path or "").lstrip("/")
    url = f"http://{host}:{int(port)}/{clean}"
    headers = {"Connection": "close", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = None
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=str(method).upper())
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            raise OSError(f"bridge HTTP {exc.code}: {body[:500]}") from exc
        parsed.setdefault("http_status", int(exc.code))
        return parsed
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise OSError(f"bridge returned non-JSON response from {url}: {body[:500]}") from exc


def agent_session_bootstrap(
    host: str,
    port: int,
    token: str | None,
    *,
    owner: str = "shell-agent",
    lease_seconds: float = 240.0,
) -> dict[str, Any]:
    """One shell entry point for agents that do not have MCP configured."""
    started = start_detached(host, port, token)
    if not started.get("ok"):
        return {"ok": False, "stage": "server-start", "server": started,
                "error": "cannot acquire controller until the expected bridge build is running"}
    acquire = bridge_http_json(
        host, port, f"{API_VERSION}/controller-acquire", method="POST",
        data={"owner": owner, "lease_seconds": lease_seconds}, token=token,
    )
    if not acquire.get("ok"):
        return {"ok": False, "stage": "controller-acquire", "server": started, "error": acquire}
    controller_id = acquire.get("controller_id")
    monitors = bridge_http_json(host, port, f"{API_VERSION}/monitors", token=token)
    focused = bridge_http_json(host, port, f"{API_VERSION}/focused-window", token=token)
    return {
        "ok": True,
        "server": started,
        "controller_id": controller_id,
        "lease": acquire,
        "focused_window": focused.get("window"),
        "monitors": monitors.get("monitors", []),
        "agent_contract": {
            "observe": f"GET http://{host}:{port}/{API_VERSION}/app-state?controller_id={controller_id}&include_image=1",
            "targeting": "prefer state_id+element_index; remote_desktop requires state_id+image_x/image_y",
            "verify": "re-observe after every meaningful mutation; dispatch_status=sent is delivery, not effect",
            "finish": f"python agent_computer_bridge.py agent-finish --controller-id {controller_id}",
            "optional_journal": f"python agent_computer_bridge.py journal-start --controller-id {controller_id} --goal <purpose>",
            "emergency_stop": "physical Esc; deterministic fallback: python agent_computer_bridge.py stop",
        },
        "shell_entrypoint": "agent.cmd help (start/observe/action/finish without MCP configuration)",
        "optional_mcp_integration": "agent_computer_bridge_mcp.py can also be registered as an optional stdio MCP server.",
    }


def agent_session_finish(host: str, port: int, token: str | None, controller_id: str, *, keep_server: bool = False) -> dict[str, Any]:
    release = bridge_http_json(
        host, port, f"{API_VERSION}/controller-release", method="POST",
        data={"controller_id": controller_id}, token=token,
    )
    stopped: dict[str, Any] | None = None
    if not keep_server and release.get("ok"):
        stopped = bridge_http_json(host, port, f"{API_VERSION}/stop", method="POST", data={}, token=token)
    # A failed or foreign release must never shut down an unrelated server.
    return {"ok": bool(release.get("ok")) and (keep_server or bool(stopped and stopped.get("ok"))), "released": release, "server_stop": stopped, "kept_running": bool(keep_server or not release.get("ok"))}


def stop_bridge(host: str, port: int, token: str | None) -> dict[str, Any]:
    if probe_server(host, port, token=token) is None:
        return {"ok": True, "already_stopped": True, "api": f"http://{host}:{port}/{API_VERSION}"}
    return bridge_http_json(host, port, f"{API_VERSION}/stop", method="POST", data={}, token=token)


# ---------------------------------------------------------------------------
# CLI adapter: maps shell commands to the same Core functions.
# ---------------------------------------------------------------------------


def add_xy_arguments(parser: argparse.ArgumentParser, *, optional: bool = False) -> None:
    nargs = "?" if optional else None
    parser.add_argument("x", type=float, nargs=nargs)
    parser.add_argument("y", type=float, nargs=nargs)
    parser.add_argument(
        "--normalized",
        action="store_true",
        help="Interpret x/y as 0.0..1.0 instead of pixels.",
    )
    parser.add_argument(
        "--coordinate-space",
        choices=["auto", "window_scope", "virtual_desktop"],
        default="auto",
        help="Coordinate space. Without scope, auto means virtual_desktop. With an active scope, choose window_scope or virtual_desktop explicitly.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Harness-independent screenshot + mouse/keyboard bridge for AI agents."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the local HTTP API.")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--token", default=os.environ.get("AGENT_COMPUTER_TOKEN"))

    start_cmd = sub.add_parser("start", help="Start HTTP API detached and return immediately (best for AI agents).")
    start_cmd.add_argument("--host", default=DEFAULT_HOST)
    start_cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    start_cmd.add_argument("--token", default=os.environ.get("AGENT_COMPUTER_TOKEN"))

    status_cmd = sub.add_parser("status", help="Check which bridge build is actually running on a port.")
    status_cmd.add_argument("--host", default=DEFAULT_HOST)
    status_cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    status_cmd.add_argument("--token", default=os.environ.get("AGENT_COMPUTER_TOKEN"))

    stop_cmd = sub.add_parser("stop", help="Stop the running local HTTP bridge deterministically.")
    stop_cmd.add_argument("--host", default=DEFAULT_HOST)
    stop_cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    stop_cmd.add_argument("--token", default=os.environ.get("AGENT_COMPUTER_TOKEN"))

    agent_session = sub.add_parser("agent-session", help="One-shot non-MCP bootstrap: start bridge, acquire controller lease, return compact contract.")
    agent_session.add_argument("--host", default=DEFAULT_HOST)
    agent_session.add_argument("--port", type=int, default=DEFAULT_PORT)
    agent_session.add_argument("--token", default=os.environ.get("AGENT_COMPUTER_TOKEN"))
    agent_session.add_argument("--owner", default="shell-agent")
    agent_session.add_argument("--lease-seconds", type=float, default=240.0)

    agent_finish = sub.add_parser("agent-finish", help="Release an agent controller lease and stop the bridge by default.")
    agent_finish.add_argument("--host", default=DEFAULT_HOST)
    agent_finish.add_argument("--port", type=int, default=DEFAULT_PORT)
    agent_finish.add_argument("--token", default=os.environ.get("AGENT_COMPUTER_TOKEN"))
    agent_finish.add_argument("--controller-id", required=True)
    agent_finish.add_argument("--keep-server", action="store_true")

    help_cmd = sub.add_parser("agent-help", help="Print the compact AI-agent usage contract as JSON.")
    help_cmd.add_argument("--host", default=DEFAULT_HOST)
    help_cmd.add_argument("--port", type=int, default=DEFAULT_PORT)

    doctor_cmd = sub.add_parser("doctor", help="Run non-mutating Computer Use prerequisite diagnostics.")
    doctor_cmd.add_argument("--capture-probe", action="store_true", help="Explicitly capture one local diagnostic frame; pixels are not returned or persisted by doctor.")

    observe_cmd = sub.add_parser("observe", help="Save a guided screenshot and print machine-readable visual context for an AI agent.")
    observe_cmd.add_argument("--output", "-o", default=DEFAULT_OBSERVATION_SCREENSHOT)
    observe_cmd.add_argument("--history", type=int, default=5)
    observe_cmd.add_argument("--no-image", action="store_true", help="Print context only; do not save a screenshot.")
    observe_cmd.add_argument("--no-cursor", action="store_true")
    observe_cmd.add_argument("--no-viewport", action="store_true")
    observe_cmd.add_argument("--no-scope", action="store_true")
    observe_cmd.add_argument("--capture-provider", choices=sorted(VALID_CAPTURE_PROVIDERS), default=DEFAULT_CAPTURE_PROVIDER)

    sub.add_parser("screen", help="Print screen dimensions as JSON.")
    sub.add_parser("mouse", help="Print current mouse coordinates as JSON.")
    processes = sub.add_parser("processes", help="List visible processes/windows; optionally filter by query.")
    processes.add_argument("query", nargs="?")
    accessibility = sub.add_parser("accessibility", help="Read a bounded Windows UI Automation tree (optional uiautomation backend).")
    accessibility.add_argument("--hwnd", type=int)
    accessibility.add_argument("--depth", type=int, default=3)
    accessibility.add_argument("--max-nodes", type=int, default=250)
    accessibility.add_argument("--include-offscreen", action="store_true")

    record = sub.add_parser("record", help="Record a human demonstration in the console into an agent-friendly Steps Recorder package.")
    record.add_argument("--output-dir", help="Recording root directory. Defaults to Documents\\Agent Computer Bridge\\Recordings.")
    record.add_argument("--settle-ms", type=int, default=DEFAULT_RECORDING_SETTLE_MS, help="Delay after a semantic action before its screenshot.")
    record.add_argument("--no-screenshots", action="store_true", help="Record structured steps without screenshots.")
    record.add_argument("--capture-scope", choices=sorted(RECORDING_CAPTURE_SCOPES), default=DEFAULT_RECORDING_CAPTURE_SCOPE, help="Screenshot scope per step. monitor is the privacy/readability default.")
    record.add_argument("--no-text", action="store_true", help="Do not store typed demonstration text/formulas.")

    record_ui = sub.add_parser("record-ui", help="Open the small PSR-style human recording control window.")
    record_ui.add_argument("--output-dir", help="Recording root directory. Defaults to Documents\\Agent Computer Bridge\\Recordings.")
    record_ui.add_argument("--settle-ms", type=int, default=DEFAULT_RECORDING_SETTLE_MS, help="Delay after a semantic action before its screenshot.")
    record_ui.add_argument("--no-screenshots", action="store_true", help="Record structured steps without screenshots.")
    record_ui.add_argument("--capture-scope", choices=sorted(RECORDING_CAPTURE_SCOPES), default=DEFAULT_RECORDING_CAPTURE_SCOPE, help="Screenshot scope per step. monitor is the privacy/readability default.")
    record_ui.add_argument("--no-text", action="store_true", help="Do not store typed demonstration text/formulas.")

    for recorder_parser in (record, record_ui):
        recorder_parser.add_argument("--goal", default="", help="User's purpose for this demonstration.")
        recorder_parser.add_argument("--prerequisites", default="", help="Required starting state.")
        recorder_parser.add_argument("--success-criteria", default="", help="Observable expected result.")

    journal_read = sub.add_parser("journal-read", help="Read durable progress after context/process loss; never replays input.")
    journal_read.add_argument("session_dir")
    for name in ("journal-start", "journal-checkpoint", "journal-stop", "journal-status"):
        journal_parser = sub.add_parser(name, help="Control the optional durable agent journal through the running bridge.")
        journal_parser.add_argument("--host", default=DEFAULT_HOST)
        journal_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
        journal_parser.add_argument("--token", default=os.environ.get("AGENT_COMPUTER_TOKEN"))
        if name != "journal-status":
            journal_parser.add_argument("--controller-id", required=True)
        if name == "journal-start":
            journal_parser.add_argument("--output-dir")
            journal_parser.add_argument("--goal", default="")
            journal_parser.add_argument("--prerequisites", default="")
            journal_parser.add_argument("--success-criteria", default="")
            journal_parser.add_argument("--capture-text", action="store_true")
            journal_parser.add_argument("--no-screenshots", action="store_true")
        if name == "journal-checkpoint":
            journal_parser.add_argument("--completed", default="")
            journal_parser.add_argument("--next-step", default="")
            journal_parser.add_argument("--notes", default="")

    scope = sub.add_parser("scope", help="Show, set, or clear a process/window scope.")
    scope_sub = scope.add_subparsers(dest="scope_command")
    scope_sub.add_parser("show", help="Show active process/window scope.")
    scope_set = scope_sub.add_parser("set", help="Select one visible window by process/PID/HWND/title.")
    scope_set.add_argument("--process")
    scope_set.add_argument("--pid", type=int)
    scope_set.add_argument("--hwnd", type=int)
    scope_set.add_argument("--title")
    scope_set.add_argument("--no-activate", action="store_true")
    scope_sub.add_parser("clear", help="Clear process/window scope.")

    screenshot = sub.add_parser("screenshot", help="Save a PNG screenshot.")
    screenshot.add_argument("--output", "-o", default=DEFAULT_SCREENSHOT)
    screenshot.add_argument(
        "--no-cursor",
        action="store_true",
        help="Do not overlay the enlarged glowing cursor marker.",
    )
    screenshot.add_argument(
        "--no-viewport",
        action="store_true",
        help="Do not dim/mark areas outside the configured visible viewport.",
    )
    screenshot.add_argument(
        "--no-scope",
        action="store_true",
        help="Capture the full desktop even when a process/window scope is active.",
    )
    screenshot.add_argument("--capture-provider", choices=sorted(VALID_CAPTURE_PROVIDERS), default=DEFAULT_CAPTURE_PROVIDER, help="auto prefers HWND-bound Windows.Graphics.Capture for an active scope when available.")

    viewport = sub.add_parser("viewport", help="Show, set, or clear the visible RDP viewport.")
    viewport_sub = viewport.add_subparsers(dest="viewport_command")
    viewport_sub.add_parser("show", help="Show the configured visible viewport.")
    viewport_set = viewport_sub.add_parser("set", help="Set visible viewport as x y width height.")
    viewport_set.add_argument("x", type=float)
    viewport_set.add_argument("y", type=float)
    viewport_set.add_argument("width", type=float)
    viewport_set.add_argument("height", type=float)
    viewport_sub.add_parser("clear", help="Disable viewport restriction and overlay.")

    move = sub.add_parser("move", help="Move mouse to x/y.")
    add_xy_arguments(move)
    move.add_argument("--duration", type=float, default=0.15)
    move.add_argument("--dry-run", action="store_true", help="Resolve/validate without moving the pointer.")

    click = sub.add_parser("click", help="Click at x/y or at current pointer position.")
    add_xy_arguments(click, optional=True)
    click.add_argument("--button", choices=sorted(VALID_BUTTONS), default="left")
    click.add_argument("--clicks", type=int, default=1)
    click.add_argument("--interval", type=float, default=0.12)
    click.add_argument("--target-area", choices=["client", "window"], default="client", help="Scoped click hit-test area; client is the safe default.")
    click.add_argument("--dry-run", action="store_true", help="Resolve/validate without clicking.")

    double_click = sub.add_parser("double-click", help="Double-click at x/y or current pointer position.")
    add_xy_arguments(double_click, optional=True)
    double_click.add_argument("--button", choices=sorted(VALID_BUTTONS), default="left")
    double_click.add_argument("--interval", type=float, default=0.12)
    double_click.add_argument("--target-area", choices=["client", "window"], default="client", help="Scoped click hit-test area; client is the safe default.")
    double_click.add_argument("--dry-run", action="store_true", help="Resolve/validate without clicking.")

    drag = sub.add_parser("drag", help="Drag from current pointer position to x/y.")
    add_xy_arguments(drag)
    drag.add_argument("--duration", type=float, default=0.5)
    drag.add_argument("--button", choices=sorted(VALID_BUTTONS), default="left")
    drag.add_argument("--dry-run", action="store_true", help="Resolve/validate without dragging.")

    mouse_down = sub.add_parser("mouse-down", help="Hold a mouse button.")
    mouse_down.add_argument("--button", choices=sorted(VALID_BUTTONS), default="left")

    mouse_up = sub.add_parser("mouse-up", help="Release a mouse button.")
    mouse_up.add_argument("--button", choices=sorted(VALID_BUTTONS), default="left")

    scroll = sub.add_parser("scroll", help="Scroll by wheel units.")
    scroll.add_argument("amount", type=int)
    scroll.add_argument("x", type=float, nargs="?")
    scroll.add_argument("y", type=float, nargs="?")
    scroll.add_argument("--normalized", action="store_true")
    scroll.add_argument("--coordinate-space", choices=["auto", "window_scope", "virtual_desktop"], default="auto")
    scroll.add_argument("--dry-run", action="store_true", help="Resolve/validate without scrolling.")

    type_cmd = sub.add_parser("type", help="Type text (native Unicode input on Windows).")
    type_cmd.add_argument("text")
    type_cmd.add_argument("--interval", type=float, default=0.02)

    key = sub.add_parser("key", help="Press a keyboard key.")
    key.add_argument("key")
    key.add_argument("--presses", type=int, default=1)
    key.add_argument("--interval", type=float, default=0.05)

    key_down_cmd = sub.add_parser("key-down", help="Hold a keyboard key.")
    key_down_cmd.add_argument("key")

    key_up_cmd = sub.add_parser("key-up", help="Release a keyboard key.")
    key_up_cmd.add_argument("key")

    sub.add_parser("recover", help="Move pointer away from a PyAutoGUI failsafe corner.")

    hotkey = sub.add_parser("hotkey", help="Press a key combination.")
    hotkey.add_argument("keys", nargs="+")

    wait = sub.add_parser("wait", help="Wait for a number of seconds.")
    wait.add_argument("seconds", type=float)

    return parser


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def run_cli(args: argparse.Namespace) -> None:
    command = args.command

    if command == "journal-read":
        print_json(AGENT_JOURNAL.read(args.session_dir))
        return
    if command in {"journal-start", "journal-checkpoint", "journal-stop", "journal-status"}:
        data = {"controller_id": args.controller_id} if command != "journal-status" else None
        if command == "journal-start":
            data.update(output_dir=args.output_dir, goal=args.goal, prerequisites=args.prerequisites,
                        success_criteria=args.success_criteria, capture_text=args.capture_text,
                        capture_screenshots=not args.no_screenshots)
        elif command == "journal-checkpoint":
            data.update(completed=args.completed, next_step=args.next_step, notes=args.notes)
        print_json(bridge_http_json(args.host, args.port, f"/{API_VERSION}/{'journal' if command == 'journal-status' else command}",
                                   method="GET" if command == "journal-status" else "POST", data=data, token=args.token))
        return

    if command == "start":
        print_json(start_detached(args.host, args.port, args.token))
        return

    if command == "status":
        print_json(query_status(args.host, args.port, args.token))
        return

    if command == "stop":
        print_json(stop_bridge(args.host, args.port, args.token))
        return

    if command == "agent-session":
        print_json(agent_session_bootstrap(
            args.host, args.port, args.token, owner=args.owner, lease_seconds=args.lease_seconds,
        ))
        return

    if command == "agent-finish":
        print_json(agent_session_finish(
            args.host, args.port, args.token, args.controller_id, keep_server=bool(args.keep_server),
        ))
        return

    if command == "agent-help":
        print_json(agent_quickstart(args.host, args.port))
        return

    if command == "doctor":
        print_json(doctor_report(capture_probe=bool(args.capture_probe)))
        return

    if command == "observe":
        if not 1 <= int(args.history) <= 20:
            raise ValueError("observe history must be between 1 and 20")
        payload = observation_snapshot(
            include_image=False, history_limit=int(args.history),
            show_cursor=not args.no_cursor, show_viewport=not args.no_viewport, use_scope=not args.no_scope,
            capture_provider=args.capture_provider,
        )
        if not args.no_image:
            path = save_screenshot(
                args.output, show_cursor=not args.no_cursor,
                show_viewport=not args.no_viewport, use_scope=not args.no_scope,
                capture_provider=args.capture_provider,
            )
            payload["screenshot"]["path"] = str(path)
        print_json(payload)
        return

    if command == "record-ui":
        run_user_recording_ui(
            output_dir=args.output_dir,
            capture_screenshots=not args.no_screenshots,
            settle_ms=args.settle_ms,
            capture_scope=args.capture_scope,
            capture_text=not args.no_text,
            goal=args.goal, prerequisites=args.prerequisites, success_criteria=args.success_criteria,
        )
        return

    if command == "record":
        started = USER_RECORDING.start(
            output_dir=args.output_dir,
            capture_screenshots=not args.no_screenshots,
            settle_ms=args.settle_ms,
            capture_scope=args.capture_scope,
            capture_text=not args.no_text,
            goal=args.goal, prerequisites=args.prerequisites, success_criteria=args.success_criteria,
        )
        print(f"Recording started: {started.get('session_dir')}", flush=True)
        print("Show the workflow normally. Return to this console and press Enter to stop. Ctrl+C also stops cleanly.", flush=True)
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        print_json(USER_RECORDING.stop())
        return

    if command == "accessibility":
        print_json(accessibility_tree(
            hwnd=args.hwnd, max_depth=args.depth, max_nodes=args.max_nodes,
            include_offscreen=args.include_offscreen,
        ))
        return

    if command == "processes":
        print_json({"ok": True, "processes": list_processes(args.query)})
        return

    if command == "scope":
        sc = args.scope_command or "show"
        if sc == "show":
            print_json({"ok": True, "scope": window_scope_status()})
        elif sc == "set":
            data = {
                "process": args.process, "pid": args.pid, "hwnd": args.hwnd,
                "title": args.title, "activate": not args.no_activate,
            }
            data = {k: v for k, v in data.items() if v is not None}
            print_json(set_window_scope(data))
        elif sc == "clear":
            print_json({"ok": True, "cleared": clear_window_scope(), "scope": window_scope_status()})
        else:
            raise ValueError(f"unsupported scope command: {sc}")
        return

    if command == "screen":
        width, height = screen_size()
        print_json(
            {
                "ok": True,
                "width": width,
                "height": height,
                "primary_monitor": {"x": 0, "y": 0, "width": width, "height": height},
                "virtual_desktop": virtual_desktop_bounds(),
                "coordinate_origin": "Windows virtual desktop; monitor positions may be negative",
                "pointer_coordinate_space": window_scope_status(),
            }
        )
        return

    if command == "mouse":
        x, y = current_mouse()
        desktop = virtual_desktop_bounds()
        payload = {
            "ok": True, "x": x, "y": y,
            "normalized_x": (x - desktop["x"]) / max(1, desktop["width"] - 1),
            "normalized_y": (y - desktop["y"]) / max(1, desktop["height"] - 1),
            "virtual_desktop": desktop,
            "inside_virtual_desktop": _point_in_rect(x, y, desktop),
            "source": "win32.GetCursorPos" if os.name == "nt" else "pyautogui.position",
            "window_scope": window_scope_status(),
        }
        if payload["window_scope"].get("active") and not payload["window_scope"].get("stale"):
            w = payload["window_scope"]["window"]
            payload["scope_relative"] = {"x": x-int(w["x"]), "y": y-int(w["y"])}
        print_json(payload)
        return

    if command == "viewport":
        vc = args.viewport_command or "show"
        if vc == "show":
            print_json({"ok": True, "viewport": load_viewport(), "config": str(viewport_config_path())})
        elif vc == "set":
            print_json({"ok": True, "viewport": save_viewport(args.x, args.y, args.width, args.height), "config": str(viewport_config_path())})
        elif vc == "clear":
            print_json({"ok": True, "cleared": clear_viewport(), "config": str(viewport_config_path())})
        else:
            raise ValueError(f"unsupported viewport command: {vc}")
        return

    if command == "screenshot":
        path = save_screenshot(
            args.output,
            show_cursor=not args.no_cursor,
            show_viewport=not args.no_viewport,
            use_scope=not args.no_scope,
            capture_provider=args.capture_provider,
        )
        primary_width, primary_height = screen_size()
        with Image.open(path) as saved_image:
            capture_width, capture_height = saved_image.size
        x, y = current_mouse()
        print_json(
            {
                "ok": True,
                "path": str(path),
                "width": capture_width,
                "height": capture_height,
                "primary_monitor": {"x": 0, "y": 0, "width": primary_width, "height": primary_height},
                "virtual_desktop": virtual_desktop_bounds(),
                "cursor_overlay": not args.no_cursor,
                "viewport_overlay": not args.no_viewport,
                "viewport": load_viewport(),
                "window_scope": window_scope_status() if not args.no_scope else {"active": False},
                "capture_provider_requested": args.capture_provider,
                "capture_providers": capture_provider_status(),
                "mouse": {"x": x, "y": y},
            }
        )
        return

    if command == "wait":
        seconds = max(0.0, float(args.seconds))
        time.sleep(seconds)
        print_json({"ok": True, "waited_seconds": seconds})
        return

    if command == "double-click":
        command = "click"
        payload = {
            "x": args.x,
            "y": args.y,
            "normalized": args.normalized,
            "coordinate_space": args.coordinate_space,
            "button": args.button,
            "clicks": 2,
            "interval": args.interval,
            "target_area": args.target_area,
            "dry_run": args.dry_run,
        }
    elif command == "move":
        payload = {
            "x": args.x,
            "y": args.y,
            "normalized": args.normalized,
            "coordinate_space": args.coordinate_space,
            "duration": args.duration,
            "dry_run": args.dry_run,
        }
    elif command == "click":
        payload = {
            "x": args.x,
            "y": args.y,
            "normalized": args.normalized,
            "coordinate_space": args.coordinate_space,
            "button": args.button,
            "clicks": args.clicks,
            "interval": args.interval,
            "target_area": args.target_area,
            "dry_run": args.dry_run,
        }
    elif command == "drag":
        payload = {
            "x": args.x,
            "y": args.y,
            "normalized": args.normalized,
            "coordinate_space": args.coordinate_space,
            "duration": args.duration,
            "button": args.button,
            "dry_run": args.dry_run,
        }
    elif command in {"mouse-down", "mouse-up"}:
        payload = {"button": args.button}
    elif command == "scroll":
        payload = {
            "amount": args.amount,
            "x": args.x,
            "y": args.y,
            "normalized": args.normalized,
            "coordinate_space": args.coordinate_space,
            "dry_run": args.dry_run,
        }
    elif command == "type":
        payload = {"text": args.text, "interval": args.interval}
    elif command == "key":
        payload = {"key": args.key, "presses": args.presses, "interval": args.interval}
    elif command in {"key-down", "key-up"}:
        payload = {"key": args.key}
    elif command == "recover":
        payload = {}
    elif command == "hotkey":
        payload = {"keys": args.keys}
    else:
        raise ValueError(f"unsupported CLI command: {command}")

    print_json(perform_action(command, payload))


def normalize_legacy_server_args(argv: list[str]) -> list[str]:
    """
    Preserve v1 invocation style:
        python agent_computer_bridge.py --port 9000
    behaves like:
        python agent_computer_bridge.py serve --port 9000
    """
    if not argv:
        return ["serve"]
    if argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--version"}:
        return ["serve", *argv]
    return argv


def main() -> None:
    # Internal runtime-overlay child process. Parse it before the public CLI so
    # autonomous agents never see implementation plumbing in --help.
    if len(sys.argv) >= 2 and sys.argv[1] == "_overlay":
        overlay_parser = argparse.ArgumentParser(add_help=False)
        overlay_parser.add_argument("--parent-pid", type=int, required=True)
        overlay_parser.add_argument("--stop-signal")
        overlay_parser.add_argument("--hook-status-signal")
        overlay_parser.add_argument("--parent-escape-active", choices=["0", "1"], default="0")
        overlay_args = overlay_parser.parse_args(sys.argv[2:])
        run_runtime_overlay(overlay_args.parent_pid, overlay_args.stop_signal, overlay_args.hook_status_signal, overlay_args.parent_escape_active == "1")
        return

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except (AttributeError, OSError):
            pass

    parser = build_parser()
    argv = normalize_legacy_server_args(sys.argv[1:])
    args = parser.parse_args(argv)

    try:
        if args.command == "serve":
            run_server(args.host, args.port, args.token)
        else:
            run_cli(args)
    except pyautogui.FailSafeException:
        print_json({"ok": False, "error": "pyautogui failsafe triggered", "error_code": "FAILSAFE_TRIGGERED", "action_sent": False, "dispatch_status": "not_started", "retry": "never"})
        raise SystemExit(2)
    except ObservationStateError as exc:
        print_json({"ok": False, "error": str(exc), "error_code": exc.code, "action_sent": False, "dispatch_status": "not_started", "retry": exc.retry})
        raise SystemExit(2)
    except ControllerLeaseError as exc:
        print_json({"ok": False, "error": str(exc), "error_code": exc.code, "action_sent": False, "dispatch_status": exc.dispatch_status, "retry": exc.retry, **({"details": exc.details} if exc.details else {})})
        raise SystemExit(2)
    except ActionDispatchError as exc:
        print_json({"ok": False, "error": str(exc), "error_code": exc.code, "action_sent": exc.action_sent, "dispatch_status": exc.dispatch_status, "retry": exc.retry, **({"details": exc.details} if exc.details else {})})
        raise SystemExit(2)
    except PointerSafetyConflict as exc:
        print_json({"ok": False, "error": str(exc), "error_type": "pointer_safety_conflict", "error_code": "POINTER_SAFETY_CONFLICT", "action_sent": None, "dispatch_status": "possibly_sent", "retry": "reobserve"})
        raise SystemExit(2)
    except CaptureProviderUnavailable as exc:
        print_json({"ok": False, "error": str(exc), "error_type": "capture_provider_unavailable", "error_code": "CAPTURE_UNAVAILABLE", "action_sent": False, "dispatch_status": "not_started", "retry": "never"})
        raise SystemExit(2)
    except (ValueError, OSError) as exc:
        print_json({"ok": False, "error": str(exc), "error_code": "INVALID_REQUEST", "action_sent": False, "dispatch_status": "not_started", "retry": "never"})
        raise SystemExit(2)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
