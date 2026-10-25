# Windows surfaces: actionable boundaries

This is a **domain-neutral** supplemental reference for Agent Computer Bridge. Load it only when the target involves a remote desktop, transient popup, overlapping windows or nonstandard graphics capture. The main `SKILL.md` is the normal entrypoint.

## Local window selection

- Discover candidate processes/windows with `computer_windows`, then scope/observe the **exact current HWND** when several windows share a title, process or PID. Old window handles are not portable between runs.
- Local UI Automation nodes are sampled independently of the physical event. Popup menus, dialogs and tool windows can have separate top-level HWNDs; a target observed on another HWND is **not** in the current state-bound window by implication.
- On stale, occluded, changed-window or ambiguous-target refusal, gather a fresh observation of the intended window. Do not reach around the gate by sending unbound input.

## Remote desktop / nested GUI

- A remote-desktop client is a **local image of another desktop**. The bridge can observe its window and map local raster pixels to the client's screen region, but local UIA describes only the local client shell.
- Prefer `image_x`/`image_y` relative to the exact current screenshot; do not reuse absolute desktop coordinates or recorded image pixels in a different session or window size.
- A locally focused remote client does not prove that a particular application or field inside the remote session is focused. Mouse input may reach another program/dialog even if `dispatch_status=sent`.
- Every real remote action consumes its observed state ID. Re-observe and inspect the **remote visual result** before the next action. Do not treat that as a guarantee against a concurrent focus change.
- Full-screen clients, minimized/disconnected sessions, display scaling, nested remote sessions and secure desktop may change visibility/targeting. Treat those as separate diagnostic cases.

## Capture provider and transient UI

- The default desktop-composite capture can omit or obscure graphics rendered with some accelerated APIs; optional Windows.Graphics.Capture may improve coverage when installed. Check `computer_doctor` and the response's actual `capture_provider`, rather than assuming availability.
- Physical human recordings sample screenshots asynchronously, separate from input hooks. A click's after-image may show a closed menu. On a selection shortly after a right-click, an additional `before_image` may contain the open menu; check its timestamps, pixel mapping and `before_input_not_result` provenance.
- If a screenshot is blank, late, before-input, mismatched to the window, or overlapped by another event, mark evidence uncertain rather than inventing a UI target.

## Verification and safety

Use a dedicated harmless test application/document for diagnosing dispatch and confirm the effect independently where practical. `action_sent` is not `effect_verified`. Do not probe destructive, privileged, trading, payment, external-communication or other consequential controls merely to test the bridge. Always release the controller with `computer_finish`.
