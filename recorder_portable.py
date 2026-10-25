from __future__ import annotations

import os
import multiprocessing
import traceback
from pathlib import Path


def _report_fatal_error(exc: BaseException) -> None:
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        log_dir = base / "Agent Computer Bridge Recorder"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "last_error.log"
        log_path.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")
    except Exception:
        log_path = None
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk(); root.withdraw()
        suffix = f"\n\nFehlerprotokoll: {log_path}" if log_path else ""
        messagebox.showerror("Agent Computer Bridge Recorder", f"Die Schrittaufzeichnung konnte nicht gestartet werden.\n\n{exc}{suffix}", parent=root)
        root.destroy()
    except Exception:
        pass


def main() -> None:
    import agent_computer_bridge as bridge
    bridge.run_user_recording_ui()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        main()
    except BaseException as exc:
        _report_fatal_error(exc)
