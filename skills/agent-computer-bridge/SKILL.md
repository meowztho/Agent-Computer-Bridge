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
`agent.cmd diagnose` checks the desktop/UIA and attempts one transient
screenshot capture (no image saved or returned); capture failure is reported
as blocked readiness. The standalone process cannot assess the server's
physical Esc hook; `diagnose` reports its own readiness separately from an
identity-verified `server_diagnostics` result (or an explicit unavailable/
mismatched status). Neither hook availability nor captured pixels prove an
actual physical stop or correct application rendering. `agent.cmd doctor`
does not capture the desktop.

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

`agent.cmd focus --controller-id ID --hwnd HWND` selects the scope and
verifies that the target is really the foreground window after activation.
`activated: false` with `retry: reobserve` means another window re-took focus:
re-observe and do not assume the target is reachable. `focus` injects no input
and does not force the window topmost.

Every observation carries `pointer` (screen and target-relative position,
this state's raster coordinates, and the hovered control when local UIA
exposes one) and `input_readiness` (`target_is_foreground`, `foreground`,
`minimized`, `occluded_by`, `capture_occlusion_independent`, `keyboard_ready`).
A valid screenshot does **not** prove input is possible: keyboard input needs
the target in the foreground, and pointer clicks additionally pass the point
hit-test. `--no-image` still returns both blocks.

Use [references/shell-roundtrip.md](references/shell-roundtrip.md) for complete
copyable examples, recovery, diagnostic errors and the transport decision.
This is the first stop if your harness has no registered tools.

## Text-first operation when the model has no vision

`agent.cmd observe --controller-id ID --no-image` returns a fresh
`state_id` and `machine_view` without transferring a PNG. Examine
`machine_view.control_candidates`: current element indices, names,
roles, enabled state and **candidate-only** provenance from local UIA.
If a named local control clearly matches the user's task, prefer its
current index to guessing coordinates. A new observation is still needed
to verify the result. `machine_view.pointer` shows where the pointer is,
and `machine_view.input_readiness` shows whether the target is foreground
and keyboard-ready before you choose an action.

`machine_view.vision.recommended` flags missing structural evidence:
RDP local UIA does not see remote content. This does **not authorize**
blind mouse coordinates. When text metadata is insufficient, observe
again with an image and consult a vision-capable subagent/tool **if one
is already available**. Pass only the relevant screenshot, user context,
state and coordinate frame; the original controller must verify any
proposed coordinate against a fresh state before acting. The bridge
does not launch subagents. Without reliable semantic or visual evidence,
do not guess an action: report the limitation.

## Alternative: already-available MCP tools

If the same bridge is **already** connected via MCP, use
`computer_start` → `computer_windows` (if needed) →
`computer_observe` → state-bound `computer_click`/`computer_type`/etc. →
re-observe → `computer_finish`. This uses the same canonical input and
observation owner. Do not ask the user to set up MCP just to use the bridge:
prefer the included shell path when an agent can execute commands.

Never issue a second controller lease or start a competing server from a
different transport; choose **one** transport for the task.

## Human recorder launcher (no manual pip commands)

On Windows, double-click `record.bat` or `record.cmd` to record a
demonstration. The first launch automatically verifies the Core requirements
and installs missing packages via the existing `agent_setup.py` owner.
Subsequent launches reuse a per-user receipt and skip full package checks,
while quickly noticing changed Python, requirements, runtime or installed
package files. `record.bat --check` verifies without opening the GUI;
`record.bat --repair` forces fresh verification/repair and then opens it.
Failed setup never receives a success receipt. Python 3.10+ is required
for this source launcher; the separate portable recorder EXE needs no
Python. Recorder auto-install does **not** enable MCP or build tools.

## Human demonstration: conversation + recording

The **user's chat message is the task context**. The recorder's
Goal/Prerequisites/Success fields are optional. A demonstration ZIP describes
how something was shown, not an independently determined purpose.

1. Read `agent_handoff.json` with the user's message; `machine_view`
   and `steps[].machine_instruction` provide a **text-first** route
   through observed human actions and post-event local target candidates.
   These candidates are not proven action recipients. Older ZIPs use
   `procedure.json` directly.
2. Use `machine_view.vision_review.requests` only for relevant unresolved
   actions selected by conversation context. Pass specific historical
   screenshot references (including `before_image` for transient menus)
   to an available vision tool when needed, not the whole ZIP by default.
   For deeper evidence check `procedure.json`, `agent_steps.json` and only
   then `steps.json` for diagnostics.
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
