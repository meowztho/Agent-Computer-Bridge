from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller fehlt.")
        print("Installieren mit: py -3 -m pip install -r requirements-portable.txt")
        return 2
    lines = (ROOT / "VERSION.txt").read_text(encoding="utf-8").splitlines()
    version = lines[0].strip()
    build = lines[1].strip()
    revision = build.rsplit("-", 1)[-1]
    name = f"Agent-Computer-Bridge-Recorder-v{version}-{revision}-portable"
    portable = ROOT / "portable"
    work = ROOT / ".portable-build"
    portable.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", name,
        "--icon", str(ROOT / "assets" / "agent_computer_bridge.ico"),
        "--add-data", f"{ROOT / 'assets'};assets",
        "--hidden-import", "uiautomation",
        "--collect-all", "uiautomation",
        # pyscreeze (via pyautogui) imports numpy/cv2 only for its optional
        # image-search helpers, which this recorder never calls. Without these
        # excludes the bundle depends on whether the build host happens to have
        # them installed, roughly doubling the EXE size.
        "--exclude-module", "numpy",
        "--exclude-module", "cv2",
        "--distpath", str(portable),
        "--workpath", str(work / "work"),
        "--specpath", str(work),
        str(ROOT / "recorder_portable.py"),
    ]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode:
        return result.returncode
    print("Portable recorder:", portable / f"{name}.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
