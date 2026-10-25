"""No-MCP shell entry into the canonical Agent Computer Bridge HTTP/Core.

This adapter owns no keyboard, screenshot, lease or target-resolution state.
Carry controller_id and fresh state_id explicitly between commands.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlencode

try:
    import agent_computer_bridge as core
except (ModuleNotFoundError, ImportError) as error:
    # The normal adapter imports the full Core only after setup. If this
    # command was called directly before setup, provide actionable JSON
    # rather than a Python traceback or installing packages implicitly.
    from agent_setup import status as dependency_status
    print(json.dumps({
        "ok": False,
        "error_code": "MISSING_OR_BROKEN_DEPENDENCY",
        "detail": f"{type(error).__name__}: {error}",
        "dependencies": dependency_status(),
        "next": "Run agent.cmd doctor, then agent.cmd setup. No MCP configuration is needed.",
    }, ensure_ascii=True), flush=True)
    sys.exit(2)

ACTION_NAMES = ("click", "move", "drag", "scroll", "type", "key", "hotkey")


def api(args: argparse.Namespace, endpoint: str, *, data: dict | None = None,
        query: dict | None = None) -> dict:
    path = f"/{core.API_VERSION}/{endpoint}"
    if query:
        path += "?" + urlencode(query)
    return core.bridge_http_json(
        "127.0.0.1", args.port, path,
        method="POST" if data is not None else "GET",
        data=data, token=os.environ.get("AGENT_COMPUTER_TOKEN"), timeout=45)


def require_ok(result: dict, *, stage: str) -> dict:
    if not isinstance(result, dict) or not result.get("ok"):
        return {**result, "stage": stage} if isinstance(result, dict) else {
            "ok": False, "stage": stage, "error": "invalid bridge reply"}
    return result


def preflight(args: argparse.Namespace) -> dict | None:
    if args.command in {"start", "doctor", "diagnose", "help"}:
        return None
    # Fail closed when the current socket belongs to a different build or
    # auth/configuration. Never attempt a silent replacement of a live server.
    status = core.query_status("127.0.0.1", args.port, os.environ.get("AGENT_COMPUTER_TOKEN"))
    if not status.get("ok") or not status.get("matches_current_file"):
        return {"ok": False, "error_code": "BRIDGE_VERSION_OR_CONNECTION_MISMATCH",
                "error": "No matching bridge instance is running. Run agent.cmd start; never bypass an existing incompatible server.",
                "status": status}
    return None


def observation(args: argparse.Namespace) -> dict:
    controller = args.controller_id
    if args.hwnd is not None:
        scoped = api(args, "scope", data={"controller_id": controller,
                                          "hwnd": args.hwnd, "activate": True})
        if not scoped.get("ok"):
            return require_ok(scoped, stage="scope")
    payload = api(args, "app-state", query={
        "controller_id": controller, "include_image": int(not args.no_image),
        "depth": min(4, max(1, args.depth)),
        "max_nodes": min(250, max(1, args.max_nodes)),
    })
    if not payload.get("ok"):
        return require_ok(payload, stage="observe")
    raster = dict(payload.get("raster") or {})
    save_path = None
    if raster.get("base64"):
        raw = base64.b64decode(raster.pop("base64"), validate=True)
        # Use an OS temp location, not the project or user-specified path.
        # This avoids overwriting source/user data and keeps file paths
        # unique between different agent sessions and observed states.
        root = Path(tempfile.gettempdir()) / "Agent-Computer-Bridge" / "observations"
        root.mkdir(parents=True, exist_ok=True)
        screenshot = root / f"{payload['state_id']}.png"
        screenshot.write_bytes(raw)
        save_path = str(screenshot.resolve())
    else:
        raster.pop("base64", None)
    ax = payload.get("accessibility") or {}
    return {
        "ok": True,
        "state_id": payload.get("state_id"),
        "expires_in_seconds": payload.get("expires_in_seconds"),
        "surface": payload.get("surface"),
        "target_window": payload.get("target_window"),
        "screenshot_path": save_path,
        "raster": raster,
        "accessibility": {
            "available": ax.get("available"), "reason": ax.get("reason"),
            "elements": (ax.get("elements") or [])[:args.max_nodes],
            "node_count": ax.get("node_count"), "truncated": ax.get("truncated"),
        },
        "next": ("Choose element_index for local UI or image_x/image_y from this screenshot. "
                 "Run one agent.cmd action with --controller-id and --state-id, "
                 "then run agent.cmd observe again to verify its result."),
    }


def action_payload(args: argparse.Namespace) -> dict:
    payload: dict = {"controller_id": args.controller_id, "state_id": args.state_id}
    kind = args.command
    if kind in {"click", "move", "drag", "scroll"}:
        idx, x, y = args.element_index, args.image_x, args.image_y
        if idx is not None and (x is not None or y is not None):
            raise ValueError("choose either --element-index or --image-x and --image-y")
        if idx is None and (x is None or y is None):
            raise ValueError("supply --element-index or both --image-x and --image-y from the observation")
        if idx is not None:
            payload["element_index"] = idx
        else:
            payload.update(image_x=x, image_y=y)
        if kind in {"click", "drag"}:
            payload["button"] = args.button
        if kind == "click":
            payload["clicks"] = args.clicks
        elif kind in {"move", "drag"}:
            payload["duration"] = args.duration
        elif kind == "scroll":
            payload["amount"] = args.amount
        if args.dry_run:
            payload["dry_run"] = True
    elif kind == "type":
        payload["text"] = args.text
        payload["interval"] = args.interval
    elif kind == "key":
        payload.update(key=args.key, presses=args.presses, interval=args.interval)
    elif kind == "hotkey":
        payload["keys"] = args.keys
    return payload


def execute(args: argparse.Namespace) -> dict:
    if args.command == "help":
        return {
            "ok": True, "transport": "shell_without_mcp",
            "commands": [
                "agent.cmd start",
                "agent.cmd windows",
                "agent.cmd observe --controller-id <id> [--hwnd <number>]",
                "agent.cmd click --controller-id <id> --state-id <fresh_state> --image-x 100 --image-y 100",
                'agent.cmd type --controller-id <id> --state-id <fresh_state> --text "Hello"',
                "agent.cmd observe --controller-id <id>",
                "agent.cmd finish --controller-id <id> [--stop-server]",
            ],
            "guide": "skills/agent-computer-bridge/SKILL.md",
            "rules": ["Do not reuse stale state IDs or recorded coordinates.",
                      "For RDP, observe before every real action.",
                      "Sent means dispatched, not verified. Re-observe.",
                      "Do not bypass physical Esc or the active controller."],
        }
    if args.command in {"doctor", "diagnose"}:
        return core.doctor_report(capture_probe=False)
    if args.command == "start":
        result = core.agent_session_bootstrap(
            "127.0.0.1", args.port, os.environ.get("AGENT_COMPUTER_TOKEN"),
            owner="shell-agent", lease_seconds=args.lease_seconds)
        if not result.get("ok"):
            return result
        return {
            "ok": True, "transport": "shell_without_mcp",
            "controller_id": result["controller_id"],
            "server_started": not bool((result.get("server") or {}).get("already_running")),
            "lease_seconds": (result.get("lease") or {}).get("lease_seconds"),
            "next": ("agent.cmd windows OR agent.cmd observe --controller-id "
                     + result["controller_id"]),
            "finish": "agent.cmd finish --controller-id " + result["controller_id"],
            "safety": "Read the current screenshot/state before every action; physical Esc stops control.",
        }
    error = preflight(args)
    if error:
        return error
    if args.command == "windows":
        reply = api(args, "windows")
        if not reply.get("ok"):
            return require_ok(reply, stage="windows")
        words = (args.query or "").casefold()
        matches = [w for w in reply.get("windows", [])
                   if not words or words in " ".join(str(w.get(k) or "") for k in
                       ("process_name", "title", "class_name")).casefold()]
        return {"ok": True, "windows": matches[:40], "count": len(matches),
                "truncated": len(matches) > 40,
                "next": "Use agent.cmd observe --controller-id <id> --hwnd <current HWND> to target one window."}
    if args.command == "observe":
        return observation(args)
    if args.command in ACTION_NAMES:
        reply = api(args, args.command, data=action_payload(args))
        if reply.get("ok"):
            reply["next"] = ("Action dispatch is not proof of effect. "
                             "Re-observe before another action; for RDP a new observation is required.")
        return require_ok(reply, stage=args.command)
    if args.command == "finish":
        # Safe default: release just this controller. Do not shut down a
        # bridge that may have existed before this shell agent connected.
        return core.agent_session_finish(
            "127.0.0.1", args.port, os.environ.get("AGENT_COMPUTER_TOKEN"),
            args.controller_id, keep_server=not args.stop_server)
    raise ValueError("unknown command")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Agent Computer Bridge: shell Computer Use without MCP registration")
    p.add_argument("--port", type=int, default=core.DEFAULT_PORT,
                   help="existing bridge TCP port; default is the Core port")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("help")
    sub.add_parser("doctor")
    sub.add_parser("diagnose", help="full Core doctor for runtime capture/UIA after dependencies are installed")
    start = sub.add_parser("start", help="start or connect to the existing Bridge and acquire a controller")
    start.add_argument("--lease-seconds", type=float, default=240)
    windows = sub.add_parser("windows", help="list or filter windows; no controller mutation")
    windows.add_argument("query", nargs="?")
    observe = sub.add_parser("observe", help="fresh state + saved local screenshot")
    observe.add_argument("--controller-id", required=True)
    observe.add_argument("--hwnd", type=int)
    observe.add_argument("--no-image", action="store_true")
    observe.add_argument("--depth", type=int, default=3)
    observe.add_argument("--max-nodes", type=int, default=80)
    for name in ACTION_NAMES:
        ap = sub.add_parser(name)
        ap.add_argument("--controller-id", required=True)
        ap.add_argument("--state-id", required=True)
        if name in {"click", "move", "drag", "scroll"}:
            ap.add_argument("--element-index", type=int)
            ap.add_argument("--image-x", type=int)
            ap.add_argument("--image-y", type=int)
            ap.add_argument("--dry-run", action="store_true")
        if name in {"click", "drag"}:
            ap.add_argument("--button", choices=("left", "middle", "right"), default="left")
        if name == "click":
            ap.add_argument("--clicks", type=int, choices=range(1, 6), default=1)
        if name in {"move", "drag"}:
            ap.add_argument("--duration", type=float, default=0.25)
        if name == "scroll":
            ap.add_argument("--amount", type=int, required=True)
        if name == "type":
            ap.add_argument("--text", required=True)
            ap.add_argument("--interval", type=float, default=0.02)
        if name == "key":
            ap.add_argument("--key", required=True)
            ap.add_argument("--presses", type=int, default=1)
            ap.add_argument("--interval", type=float, default=0.05)
        if name == "hotkey":
            ap.add_argument("keys", nargs="+")
    finish = sub.add_parser("finish", help="release this controller; keep server by default")
    finish.add_argument("--controller-id", required=True)
    finish.add_argument("--stop-server", action="store_true",
                        help="also stop server, only when the agent started this instance")
    return p


def main() -> int:
    try:
        args = parser().parse_args()
        result = execute(args)
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
        return 0 if result.get("ok") else 2
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "stage": "shell-adapter",
                          "action_sent": False, "retry": "never"}, ensure_ascii=True), flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
