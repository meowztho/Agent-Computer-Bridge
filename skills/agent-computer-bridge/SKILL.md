---
name: agent-computer-bridge
description: Operate Windows through the included shell interface without MCP setup, optionally through MCP, and interpret passive human recordings using chat context and screenshots.
---

# Agent Computer Bridge — agent entry

The user provides an application/task in chat; the bridge provides Windows
**Computer Use** and passive human demonstration evidence. It is domain-neutral.
**MCP is optional.** A shell/terminal agent starts from `agent.cmd` in this
folder, without reading or reverse-engineering project code.

## Default: shell roundtrip (no MCP configuration)

From the extracted folder use `agent.cmd help` and `agent.cmd doctor`.
The doctor uses only the Python standard library: it detects missing or
incompatible packages **before importing the Bridge**. If it reports an
unready environment, run `agent.cmd setup` explicitly to install the pinned
core requirements from `requirements.txt` and verify them.

If Python itself is missing, the batch launcher prints a Python 3.10+ for
Windows prerequisite. Do not configure MCP for the default shell mode.
`agent.cmd diagnose` performs further desktop/UIA/capture checks when the
core dependencies are ready.

Optional capabilities can be installed as required using
`agent.cmd setup --with-wgc` (graphics), `--with-mcp` (MCP SDK), or
`--with-build-tools` (EXE rebuilding). Do not install optional modules
by default or silently during normal Computer Use.

1. `agent.cmd start` — connects to an existing matching bridge or starts
   one and returns a unique `controller_id`. A conflicting lease/build is
   an error; **never** bypass or kill another controller.
2. `agent.cmd windows` — discover fresh HWNDs. For an ambiguous target,
   choose the exact current HWND.
3. `agent.cmd observe --controller-id ID --hwnd HWND` — obtain a
   fresh `state_id`, current UI Automation candidates, and a saved
   `screenshot_path`. The command saves a PNG outside the project and returns
   metadata rather than flooding the context with base64.
4. **Open the screenshot with the agent's image/file-viewing tool.**
   An image-capable agent can directly inspect menus, icons, dialogs, remote
   desktops and custom canvases. A text-only terminal cannot infer screenshot
   contents from the filename; use UIA when available and say when visual
   inspection is unavailable.
5. `agent.cmd click --controller-id ID --state-id STATE --image-x X --image-y Y`
   or `--element-index N` (for **native local UIA only**). Other commands:
   `move`, `drag`, `scroll --amount N`, `type --text "text"`,
   `key --key enter`, `hotkey ctrl s`. All mutating commands require
   the lease ID + **observed** state ID. Use `--dry-run` for supported
   pointer-action target previews.
6. `agent.cmd observe --controller-id ID` again. `sent` is input dispatch,
   **not** a verified UI effect. For RDP, every real action consumes its
   state; always re-observe **before** the next action.
7. `agent.cmd finish --controller-id ID` releases only your lease and keeps
   a pre-existing server running. Physical **Esc** stops computer control.

Use [references/shell-roundtrip.md](references/shell-roundtrip.md) for complete
copyable examples, recovery, diagnostic errors and the transport decision.
This is the first stop if your harness has no registered tools.

## Alternative: already-available MCP tools

If the same bridge is **already** connected via MCP, use
`computer_start` → `computer_windows` (if needed) →
`computer_observe` → state-bound `computer_click`/`computer_type`/etc. →
re-observe → `computer_finish`. This uses the same canonical input and
observation owner. Do not ask the user to set up MCP just to use the bridge:
prefer the included shell path when an agent can execute commands.

Never issue a second controller lease or start a competing server from a
different transport; choose **one** transport for the task.

## Human demonstration: conversation + recording

The **user's chat message is the task context**. The recorder's
Goal/Prerequisites/Success fields are optional. A demonstration ZIP describes
how something was shown, not an independently determined purpose.

1. Read `agent_handoff.json` with the chat, locate the relevant application
   segments and screenshot references. For older ZIPs, start with
   `procedure.json`.
2. Inspect relevant screenshots (and optional `before_image` for transient
   menus), then `procedure.json` for timing, confidence, pixel positions,
   ambiguities, capture quality and integrity. Use `agent_steps.json` for
   details, `steps.json` only for debugging.
3. Local UIA candidates sampled after the event are **not proven** action
   recipients. Remote Desktop content is raster-only to the local bridge,
   so the client's UIA nodes never identify remote app controls.
4. A before-input frame is context, not an effect; an early after-image
   may still show the old state. Never blindly replay recorded desktop
   coordinates; observe the **current** UI and act using fresh state.
5. Combine user intent and demonstrated operation into a short actionable
   explanation. Do not require the user to restate context already in chat.

## Optional agent evidence

To audit a longer session with MCP use `computer_journal_start`, optionally
`auto_observe_after_action=true` (off by default due to privacy and overhead).
The shell also exposes the same optional journal through the Core's existing
CLI/HTTP contract if needed. Agent actions and human recordings have distinct
provenance. A screenshot or action receipt cannot prove intended outcome.

## Boundaries and troubleshooting

Only the bridge's leased, state-bound Core may inject input. Do not replace
it with raw Win32 commands, unbound HTTP calls or coordinate replay.
No local UIA sees inside a remote desktop. For GPU/WPF/native popup
difficulties, refer to [references/windows-surfaces.md](references/windows-surfaces.md).
Run `agent.cmd doctor` for environment diagnostics. On stale state,
occlusion, unknown focus, contradictory evidence, possibly-sent input or
emergency stop, fail closed and re-observe or stop. Confirm approval for
consequential actions.

Source code is **not** needed for ordinary installation, control, recording,
diagnosis or recovery. Read it only if maintaining the bridge itself.
