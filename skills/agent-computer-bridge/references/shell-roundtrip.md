# Shell roundtrip — no MCP installation or code reading

This is the primary agent onboarding contract for the **existing**
Agent Computer Bridge Core. Runs on a Windows desktop with Python and
the core `requirements.txt` installed. From the extracted project directory:

```bat
agent.cmd help
agent.cmd doctor
rem If the doctor reports missing/incompatible core packages:
agent.cmd setup
agent.cmd doctor
agent.cmd diagnose
agent.cmd start
```

The `start` JSON returns `controller_id` (an ephemeral capability token)
and reports whether the matching local bridge was already running. Copy
this returned value into further calls. Do not invent a controller ID.

## One complete read → image → action → verify cycle

```bat
agent.cmd windows
agent.cmd observe --controller-id CONTROLLER_ID
```

To scope a specific window, choose its **current** HWND from `windows`:

```bat
agent.cmd observe --controller-id CONTROLLER_ID --hwnd CURRENT_HWND
```

When another window keeps re-taking focus, select the scope and verify the
foreground explicitly:

```bat
agent.cmd focus --controller-id CONTROLLER_ID --hwnd CURRENT_HWND
```

`focus` injects no input and does not force topmost. It returns `activated`,
`foreground`, `foreground_lost`, `retry` and `message`. `activated: false`
with `retry: reobserve` means the window did **not** hold the foreground
after a short settle; re-observe, then raise or re-focus the target instead
of clicking through an unknown owner.

The observation returns `state_id`, `target_window`, `surface`,
`accessibility.elements`, `pointer`, `input_readiness` and `screenshot_path`.
The screenshot is a PNG saved in the Windows temp directory; only its
**path**, not multi-MB base64, appears in the console JSON.

`pointer` contains `screen` (virtual-desktop x/y), `target_relative`
(relative to the observed target, or null), `inside_target`, `image`
(this state's raster pixel, or null) and `hover` (the local UIA control at
the pointer, or null; password/unknown text is never exposed). Image pixels
are valid only for that `state_id`.

`input_readiness` contains `target_is_foreground`, `foreground`, `minimized`,
`occluded_by`, `capture_occlusion_independent` and `keyboard_ready`. A valid
screenshot does **not** prove input is possible: keyboard input needs the
target in the foreground, and pointer clicks additionally pass the point
hit-test. `--no-image` still returns both blocks.

**Text-only agents:** Use `agent.cmd observe --controller-id ID --no-image`
to avoid transferring a screenshot when a native local UIA candidate can
resolve the task. Read `machine_view.control_candidates` and check the
candidate-only attribution. If the target is ambiguous or raster-only,
use a **new** screenshot and ask an available vision tool/subagent for
that image and goal. The original agent retains input control and must
re-observe to validate proposed coordinates. Do not click blindly.

**Visual agents:** Open `screenshot_path` using your available file-image
inspection tool. This is especially important for icons, menus, custom-drawn
surfaces, image-heavy apps and Remote Desktop. Do not assume a terminal
can interpret PNG bytes from a filename.

**Text-only agents:** Use the local UIA tree when trustworthy and say
when image interpretation is unavailable. Never claim to have seen pixels
from a path alone.

Select *one* target from that exact observation:

```bat
agent.cmd click --controller-id CONTROLLER_ID --state-id FRESH_STATE_ID --element-index 4
```

or, based on the observed raster's image coordinates:

```bat
agent.cmd click --controller-id CONTROLLER_ID --state-id FRESH_STATE_ID --image-x 270 --image-y 160
```

For risky positioning, `--dry-run` previews target binding without
sending a click. Index and image coordinates are alternatives, not
composable. Never reuse pixels from a recording or previous live image.

Other actions have the same `--controller-id` and `--state-id` contract:

```bat
agent.cmd type --controller-id CONTROLLER_ID --state-id FRESH_STATE_ID --text "Example"
agent.cmd key --controller-id CONTROLLER_ID --state-id FRESH_STATE_ID --key enter
agent.cmd hotkey --controller-id CONTROLLER_ID --state-id FRESH_STATE_ID ctrl s
agent.cmd scroll --controller-id CONTROLLER_ID --state-id FRESH_STATE_ID --image-x 200 --image-y 200 --amount -3
```

**Always** re-observe to verify visible effects. For RDP, you must
re-observe **before every subsequent real action**, because local UIA
cannot attest the remote app's focus or recipient:

```bat
agent.cmd observe --controller-id CONTROLLER_ID
```

At completion:

```bat
agent.cmd finish --controller-id CONTROLLER_ID
```

This releases your exclusive lease and leaves a pre-existing server
running. Only `--stop-server` explicitly requests server shutdown;
use it solely when you deliberately own that server lifetime.

## Who installs what`

- Windows with Python 3.10+ and the `py` launcher is required for the
  full agent Computer Use runtime. A missing launcher produces a clear
  `PYTHON_REQUIRED` diagnostic; system Python is not silently installed.
- `agent.cmd doctor` runs with **standard-library Python only**. It checks
  required modules and pinned versions from requirements files, and
  verifies a real Core import when those packages appear installed.
- `agent.cmd setup` explicitly installs the core group and verifies it.
  If already compatible, nothing is installed. `install.cmd` is the same
  route. Missing pip yields `ensurepip` recovery guidance.
- `agent.cmd setup --with-wgc` adds optional graphics capture,
  `--with-mcp` adds MCP, `--with-build-tools` adds developer build tools.
  None is required for standard shell Computer Use.
- `agent.cmd diagnose` checks runtime display/UIA/capture readiness
  separately from Python package readiness.
- The portable **human recorder** EXE does not require Python. All
  agent Computer Use modes require Python and the core packages.

## Recovery, errors and safety

- `agent.cmd doctor` is the non-mutating prerequisite check.
- A version/build mismatch means an old or unknown process owns the port.
  Do not take over the socket. Investigate the existing service.
- `CONTROLLER_BUSY`: another agent holds the lease. Do not bypass it.
- `STALE_STATE`, `AMBIGUOUS_TARGET`, `ACTION_UNAVAILABLE`, occlusion or
  remote-focus uncertainty: return to `windows`/`observe` and resolve.
- `possibly_sent`: do not repeat the action without observing. A
  keyboard/mouse input could already have partially reached Windows.
- Physical Esc stops the active controller; do not immediately reacquire
  after that user stop.
- Screenshot files can contain sensitive content. The adapter keeps
  them outside the source directory; agents should not upload/share
  them indiscriminately. Remove temporary captures after their task
  when safe.
- All commands invoke the canonical local HTTP/Core owner.
  The CLI is not a separate input implementation.
