# Agent Computer Bridge

Windows bridge for two complementary capabilities:

1. **Computer Use for agents**: observe the desktop (screenshots and local UI Automation), select a scoped window, send state-bound mouse/keyboard input and verify the visible effect.
2. **Human demonstration recording**: record physical interaction at normal speed and export a machine-readable ZIP that an agent interprets **together with the user's conversation**. Recording is passive: no input is injected.

The implementation is application/domain neutral. RDP and other nested desktop clients are handled as raster surfaces, not as remotely accessible local UI Automation trees.

## Release

- **Version:** 2.42 / r47
- **Canonical runtime:** `agent_computer_bridge.py`
- **Byte-identical, versioned source:** `agent_computer_bridge_v2_42_r47.py`
- **MCP adapter:** `agent_computer_bridge_mcp.py`
- **Agent instructions:** `skills/agent-computer-bridge/SKILL.md`
- **Windows support:** Windows 11; Python 3 with the Windows `py` launcher for the source/MCP runtime.
- **Portable recorder:** `portable/Agent-Computer-Bridge-Recorder-v2.42-r47-portable.exe` (human recorder only, no Python required on the target PC).
- **Deployment:** The distributable ZIP contains the current files only, a `SHA256SUMS.txt` for its contents, and this guide. Historical sources, audits, temporary recordings and previous builds are excluded.

## Quick start ? no MCP configuration required

Extract the package to a writable Windows folder. The standard shell
mode needs Python 3.10+ with the Windows `py` launcher, but **not MCP**.
The dependency preflight runs before Core imports:

```bat
agent.cmd help
agent.cmd doctor
```

If the doctor reports missing/incorrect libraries, run
`agent.cmd setup` explicitly, then check again with `agent.cmd doctor`.
This installs pinned packages from `requirements.txt`, verifies their
versions and attempts a real Core import. If Python is missing, the
launcher prints a prerequisite message. If pip is missing, setup
reports `ensurepip` guidance. `install.cmd` is an alias for setup.
No normal start, observe or input command silently installs packages.

Optional groups: `agent.cmd setup --with-wgc` for accelerated capture,
`--with-mcp` for an already-chosen MCP workflow, and `--with-build-tools`
for developers rebuilding the EXE. Install only what the current use case
needs. `agent.cmd diagnose` runs fuller desktop/UIA/capture diagnostics
after the core environment is ready.

For an agent that can run shell commands, the supported default is:

```bat
agent.cmd start
agent.cmd windows
agent.cmd observe --controller-id CONTROLLER_ID
agent.cmd click --controller-id CONTROLLER_ID --state-id FRESH_STATE_ID --image-x 300 --image-y 180
agent.cmd observe --controller-id CONTROLLER_ID
agent.cmd finish --controller-id CONTROLLER_ID
```

**Replace all placeholders with values returned by the prior command.**
The agent receives a fresh local `screenshot_path` (PNG), current window
details, local UIA elements and the state ID. An **image-capable** agent should
open that PNG using its own image/file-viewing tool; a text-only terminal
cannot interpret image pixels just from a filename. Use local UIA where
available. **Do not click example coordinates without seeing the target.**

`agent.cmd` reuses the same lease, scoped observations and native input
owner as MCP. It does **not** require plugin installation or MCP registration.
After installation, the agent need not inspect the Python project to operate
the bridge. The complete, copyable recovery/roundtrip contract is in
`skills/agent-computer-bridge/references/shell-roundtrip.md`.

### Optional MCP integration

Already configured MCP remains supported with `mcp.cmd` as a stdio server,
or `agent_computer_bridge_mcp.py` as its entrypoint. To install optional
MCP dependencies on this host use `install-mcp.cmd`; `install-wgc.cmd`
installs an optional accelerated capture provider. Neither is required for
the default shell workflow.

The MCP equivalent is `computer_start` ? `computer_observe` ? state-bound
action ? re-observe ? `computer_finish`. Use exactly one transport/lease
per controller session. The portable human-recorder EXE is independent of
Python, but the *agent Computer Use* runtime still needs Python + core
requirements on the host.

### Human recorder

Launch `record.cmd` for the source-based control window. Alternatively start the standalone `portable/Agent-Computer-Bridge-Recorder-v2.42-r47-portable.exe` on a Windows host.

Press Start, demonstrate normally, and Stop & Save. Goal/prerequisites/expected outcome fields are **optional**; the user's chat message is the usual task context. Avoid entering passwords or exposing secrets in screenshots. A successful archive contains:

| File | Purpose |
| --- | --- |
| `agent_handoff.json` | Short navigation index for the user conversation, application segments, step numbers, screenshot and pixel pointers |
| `procedure.json` | Rich sequence, uncertainty, capture quality, example values and evidence coverage |
| `agent_steps.json` | Per-step machine-friendly target candidates, timing and screenshot relations |
| `steps.json` | Full recorded evidence for debugging |
| `session.json` | Session configuration, drop counters and reported processing integrity |
| `screenshots/` | Images including optional pre-click menu images |
| `report.html` | Human-readable rendering |

A `before_image` may be captured from the buffer immediately before a menu selection following a right-click. It is **not** proof of the outcome. A frame can also be early, late, overlapped, missing or simply visually ambiguous. A low error count does not prove every physical action was captured.

### Optional agent journal

Call `computer_journal_start` for longer or restart-sensitive sessions. You may set `auto_observe_after_action=true` to record follow-up screenshots after agent actions; it is disabled by default due to overhead and possible sensitive-data retention. Use `computer_checkpoint` and `computer_journal_read(session_dir)` as needed; `computer_finish` closes the journal. Agent actions remain distinct from physical human recordings. Durable `events.jsonl` is authoritative; `procedure.json` is a derived index and images/dispatch receipts are not proof of task success.

## Safety and operational limitations

- Both shell and MCP flows use the same exclusive **controller lease**. The agent must observe before acting, use a state-bound action, and re-observe to verify visible effects.
- An action reported `sent` may have reached the wrong remote control if remote focus changed; do not blindly retry a `possibly_sent` action.
- RDP observations are raster-only for remote content. The local client window's UIA hierarchy is not remote UIA. A real remote action consumes the state ID; **re-observe before the next real remote action**.
- Window popups can have different HWNDs. On stale/focus/occlusion errors, refresh scope and observations instead of bypassing gates.
- Physical **Esc** is the emergency stop. Already started OS dispatch calls cannot be rolled back. Agents must release normally with `computer_finish`.
- The low-level HTTP/CLI interface retains legacy compatibility behavior; for agent automation use the safe `agent.cmd` shell adapter or optional MCP instead. Do not expose the HTTP listener on an untrusted network.
- User recordings and journals may include sensitive screenshots; store/share them deliberately. The distributable package contains **no user recordings or journals**.

## Diagnostics, testing, rebuilding

```bat
py -3 agent_computer_bridge.py doctor
py -3 agent_computer_bridge.py agent-help
py -3 agent_computer_bridge.py status
py -3 -m unittest discover -s tests -q
build-recorder-portable.bat /nopause
```

To reproduce the clean full release ZIP (the portable EXE must already have been built), run:

```bat
py -3 tools\build_release_package.py
```

The packaging script validates that the current source/version match, restricts the file list, checks domain neutrality of shipped text, hashes each member, and verifies the resulting archive.

**Not independently certified:** physical latency across every Windows/RDP environment, remote focus tracking inside arbitrary applications, and semantics or business success inferred from screenshots. Use a harmless smoke test on the destination host.

## Source layout

```text
agent_computer_bridge.py             canonical core
agent_computer_bridge_mcp.py         optional thin stdio MCP adapter\nagent_cli.py, agent.cmd               default shell Computer Use (no MCP)
recorder_portable.py                 portable recorder entrypoint
skills/agent-computer-bridge/        skill, shell roundtrip and Windows surface guidance
tests/                               unit and isolated integration regressions
tools/build_portable_recorder.py     reproducible recorder executable builder
tools/build_release_package.py      curated release ZIP builder and verifier
assets/                              executable icon/artwork
portable/                            current recorder executable
requirements*.txt                   dependency groups
install*.cmd, record.cmd, mcp.cmd    Windows commands
.zcode-plugin/                       optional provider-specific plugin descriptor
```

The source repository retains Git history and internal historical audits. The release ZIP is the supported handoff artifact; previous source snapshots, recordings and internal investigations are not part of it.
