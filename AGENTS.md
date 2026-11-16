# Agent Computer Bridge — repository operating rules

This is a domain-neutral Windows Computer-Use and human-demonstration recorder. The authoritative runtime is `agent_computer_bridge.py`; the identical versioned source is a release copy. `agent_computer_bridge_mcp.py` is a thin adapter. The user-facing agent workflow is described in `skills/agent-computer-bridge/SKILL.md`.

## Development

- First inspect root, these instructions, Git HEAD/status/diff, the relevant owner and its tests.
- Keep screenshot/UIA/state binding, controller admission, native dispatch, human recording and journal export at their existing owners. Do not create alternative input stacks or copy validation rules into an adapter.
- Prefer a small reversible edit and tightly scoped regression tests. Test with simulated input before doing harmless live Windows tests. Never use unknown production controls for input smoke tests.
- Preserve other people's changes and untracked historical data. Current package releases are assembled with the allowlisted `tools/build_release_package.py`; do not delete history just to clean a distributable artifact.
- On a code release, synchronize the canonical file and versioned artifact, plugin descriptor/version, README, VERSION.txt, portable EXE, tests and release checksum list.
- Run `py -3 -m unittest discover -s tests -q`, compile the runtime/MCP adapter and validate the distributable ZIP before reporting success.

## Agent Computer Use

Prefer the leased agent.cmd shell entrypoint (no MCP registration); preconfigured MCP is optional. Both converge on the same state-bound owner; not raw/unbound Windows automation: start, observe, one state-bound action, observe its effect, finish. Local UIA nodes can be used only on local surfaces. Nested/remote desktop controls are raster-only. A real remote action consumes its state; re-observe before the next action. Respect scope, focus, occlusion, physical emergency stop and uncertain partial dispatch. Dispatch is not proof of UI effect.

## Human demonstration interpretation

The **user's message supplies the task context**; recording fields for intent are optional. Read `agent_handoff.json` then relevant `procedure.json`, screenshots and `agent_steps.json`. Full `steps.json` is for targeted debugging. Do not treat sampled UIA as proven recipients, remote client shell elements as remote controls, or historical pixels as live action coordinates.

Menu `before_image` is a time-bounded pre-input screenshot, not a post-action result. Do not invent missing frames or delay physical input to acquire them. The agent journal is distinct from physical user recording; screenshots may contain private data.

## Distribution

The active README and bundled agent skill must remain domain neutral. Internal historical notes, obsolete builds, session ZIPs, hidden build folders and recordings must never enter the release ZIP. The packaging script's closed allowlist is the authoritative selection; historical documentation remains available through Git, not as current usage guidance.


## r46 shell entry and visual roundtrip

- Shell-first agent onboarding: agent.cmd help/start/windows/observe/action/finish.
  The included agent_cli.py is a thin HTTP client of the running canonical
  Core, not another input or UIA owner. Use one lease and fresh state per task;
  it must reject incompatible/unknown live builds and never take over a busy controller.
- Saved screenshots are image files in an OS temp directory, not base64
  blobs in the console. Agents with image-capable tools must open the returned
  screenshot_path to see pixels. Text-only agents must not pretend that
  a filename conveys an image. UIA remains useful for local native surfaces.
- MCP is optional and not part of the default setup. The one-page
  references/shell-roundtrip.md contains the full start/observe/act/verify/
  finish contract; do not require source analysis from consumers.


## r47 dependency bootstrap

- agent_setup.py is standard-library-only and the single owner of
  dependency checks and explicit install routing. Requirements files
  remain canonical for package groups and versions.
- agent.cmd doctor checks Python, Windows, pinned core libraries and
  core-import readiness. agent.cmd setup is an explicit pip operation;
  no hidden install during a start or observation.
- Optional WGC, MCP and PyInstaller groups are selected only when
  needed. agent.cmd diagnose retains full runtime display/UIA checks.
- Missing Python must report its prerequisite, not implicitly install
  a system interpreter. The existing controller and input owner is
  unchanged.


## r48 recorder launch (one-time user bootstrap)

- The physical-human entrypoints record.cmd and record.bat share the
  standard-library dependency owner in agent_setup.py. Only the recorder
  launcher may automatically install core Python packages; the agent shell
  intentionally requires explicit agent.cmd setup.
- A per-user cache under LOCALAPPDATA permits fast subsequent starts.
  It is bound to interpreter identity, runtime and requirements SHA-256
  and lightweight installed-package file stamps. Missing/changed artifacts
  or --repair trigger revalidation, never a second input owner.
- Initial installation must finish successfully before spawning record-ui.
  Failures cannot create cache entries and remain visible in the console.
  --check preflights without opening the GUI; --repair ignores valid cache.
- The portable human-recorder EXE remains independent of Python.


## r49 text-first machine guidance

- machine_readable_live_view is the Core's bounded projection of local
  UIA metadata, not another observation or input owner. Shell and MCP
  receive its unchanged evidence/provenance.
- A named element_index remains an observation-scoped candidate, never
  a verified recipient. Password/unknown, disabled, offscreen and remote
  client UIA must not be promoted to semantic action targets.
- Recorder agent_handoff.json has grounded machine_instruction fields
  and a bounded vision_review queue. No remote UI labels invented.
- Prefer structure first, consult visual evidence only for task-relevant
  unresolved steps. Vision subagents are external/optional and read-only;
  canonical Core retains state-bound action admission.

## r54 admission identity and reproducible release bytes

- Admission re-resolves an observed element against a live read and compares
  the control's own native window handle when both sides expose one. Two
  same-named HWND-backed controls must never be interchangeable; zero/unknown
  handles keep the semantic fallback instead of inventing a mismatch.
- Repository bytes are pinned with .gitattributes (`* -text`). Manifest hashes
  are computed from working-tree bytes, so EOL conversion must never run.
- Every tests/test_*.py must be listed in the release allowlist; the packaging
  test enforces set equality so the shipped suite covers the shipped fix.
- The portable recorder bundle excludes numpy/cv2 explicitly. pyscreeze imports
  them only for image-search helpers the recorder never calls, and their
  presence depends on the build host rather than the product.
- Optional MCP SDK absence must skip only the MCP adapter tests; the
  remote-surface suite has to run without it.

## r55 pointer and foreground readiness

- app-state/observe expose pointer (screen, target-relative, state raster pixel,
  bounded hover) and input_readiness (foreground owner, minimized, occluders,
  capture independence, keyboard_ready). Both survive --no-image; a text-only
  agent must not infer pointer position from a filename or screenshot.
- A valid screenshot does not prove input is possible. Keyboard input needs the
  target to be the foreground window; pointer clicks additionally pass the
  point hit-test. Do not retry a click that failed the hit-test without a fresh
  observation.
- Scope activation is verified after one settle and reports foreground_lost
  with retry=reobserve instead of claiming success. agent.cmd focus is the only
  shell path that raises a target; it never injects input and never forces
  topmost.
- Pointer hover never exposes password text; remote surfaces report no local
  hover evidence.
