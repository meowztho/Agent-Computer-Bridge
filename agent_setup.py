"""Dependency preflight and explicit installation for Agent Computer Bridge.

Only Python standard library imports: usable before core/MCP/UIA installation.
The requirements files are the single source of package version authority.
No changes to the desktop and no MCP registration occur here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import time
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
GROUPS = {
    "core": ("requirements.txt", {
        "PyAutoGUI": "pyautogui",
        "Pillow": "PIL",
        "uiautomation": "uiautomation",
    }),
    "mcp": ("requirements-mcp.txt", {"mcp": "mcp"}),
    "wgc": ("requirements-wgc.txt", {"windows-capture": "windows_capture"}),
    "build": ("requirements-portable.txt", {"pyinstaller": "PyInstaller"}),
}
MIN_PYTHON = (3, 10)


def requirements(group: str) -> dict[str, str]:
    file, modules = GROUPS[group]
    names = {}
    for line in (ROOT / file).read_text(encoding="utf-8").splitlines():
        item = line.partition("#")[0].strip()
        if not item or item.startswith("#"):
            continue
        item = item.split(";", 1)[0].strip()
        m = re.fullmatch(r"([A-Za-z0-9._-]+)((?:[<>=!~].+)?)", item)
        if not m:
            raise ValueError(f"Unsupported requirement entry in {file}: {item}")
        name, spec = m.groups()
        if name.lower() not in {n.lower() for n in modules}:
            raise ValueError(f"Undocumented dependency in {file}: {name}")
        names[name] = spec
    if len(names) != len(modules):
        raise ValueError(f"Incomplete requirement manifest: {file}")
    return names


def version_matches(installed: str, spec: str) -> bool:
    """Accept only version operators actually used in our requirement files.

    Conservative on prereleases and unknown version schemes: diagnostics
    must not declare incompatible optional backends ready.
    """
    if not spec:
        return True
    release = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.post\d+)?", installed)
    if not release:
        return False
    actual = [int(n) for n in release.group(1).split(".")]
    for criterion in spec.split(","):
        match = re.fullmatch(r"(==|!=|>=|<=|>|<)(\d+(?:\.\d+)*)", criterion.strip())
        if not match:
            return False
        operator, version = match.groups()
        expected = [int(n) for n in version.split(".")]
        count = max(len(actual), len(expected))
        a = tuple(actual + [0] * (count - len(actual)))
        b = tuple(expected + [0] * (count - len(expected)))
        if not {"==": a == b, "!=": a != b, ">=": a >= b,
                "<=": a <= b, ">": a > b, "<": a < b}[operator]:
            return False
    return True


def group_status(group: str) -> dict:
    file, modules = GROUPS[group]
    installed = []
    missing = []
    incompatible = []
    for name, spec in requirements(group).items():
        info = {"package": name, "requirement": name + spec, "module": modules[name]}
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(info)
            continue
        info["installed_version"] = version
        try:
            available = importlib.util.find_spec(modules[name]) is not None
        except (ModuleNotFoundError, ImportError, ValueError):
            available = False
        info["module_available"] = available
        if not available or not version_matches(version, spec):
            incompatible.append(info)
        else:
            installed.append(info)
    return {"requirements_file": file, "installed": installed,
            "missing": missing, "incompatible": incompatible,
            "ready": not missing and not incompatible}


def status(*, with_mcp: bool = False, with_wgc: bool = False,
           with_build: bool = False) -> dict:
    python_ok = sys.version_info >= MIN_PYTHON
    windows_ok = os.name == "nt"
    chosen = ["core"] + [group for group, selected in (
        ("mcp", with_mcp), ("wgc", with_wgc), ("build", with_build)
    ) if selected]
    groups = {group: group_status(group) for group in chosen}
    ready = python_ok and windows_ok and all(v["ready"] for v in groups.values())
    import_probe = {"checked": False, "ok": False}
    if python_ok and windows_ok and groups["core"]["ready"]:
        # Versions/metadata alone do not prove that transitive Windows
        # modules can import. Probe without executing the core's CLI.
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import agent_computer_bridge"],
                cwd=str(ROOT), text=True, capture_output=True, timeout=20,
                check=False)
            import_probe = {
                "checked": True, "ok": probe.returncode == 0,
                "error_tail": probe.stderr[-900:] if probe.returncode else None,
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            import_probe = {"checked": True, "ok": False,
                            "error_tail": type(exc).__name__}
        ready = ready and import_probe["ok"]
    return {
        "ok": ready,
        "core_runtime_import": import_probe,
        "kind": "agent_computer_bridge_dependency_check",
        "python": {"version": sys.version.split()[0], "minimum": "3.10",
                   "supported": python_ok, "executable": sys.executable},
        "windows": windows_ok,
        "groups": groups,
        "next": (
            "agent.cmd start" if ready else
            "Run agent.cmd setup to install missing core requirements; optional "
            "groups: --with-mcp, --with-wgc, --with-build-tools. If Python or "
            "Windows is unsupported, install a supported Python on Windows first."
        ),
        "installation_is_explicit": True,
        "mcp_configuration_required": False,
    }


def install(*, with_mcp: bool = False, with_wgc: bool = False,
            with_build: bool = False) -> dict:
    before = status(with_mcp=with_mcp, with_wgc=with_wgc, with_build=with_build)
    if not before["python"]["supported"] or not before["windows"]:
        return before
    chosen = list(before["groups"])
    if before["ok"]:
        return {**before, "already_satisfied": True, "install_invoked": False}
    commands = [sys.executable, "-m", "pip", "install"]
    for group in chosen:
        commands.extend(["-r", str(ROOT / GROUPS[group][0])])
    try:
        result = subprocess.run(commands, cwd=str(ROOT), text=True,
                                capture_output=True, timeout=240, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**before, "ok": False, "install_invoked": True,
                "error": type(exc).__name__,
                "next": "Inspect the interpreter, pip availability and installer access; retry agent.cmd setup."}
    after = status(with_mcp=with_mcp, with_wgc=with_wgc, with_build=with_build)
    failed = result.returncode != 0
    if failed and importlib.util.find_spec("pip") is None:
        # A subprocess can exit normally when its interpreter has no pip.
        # Diagnose the module, not a coincidental phrase in pip's error log.
        next_step = (f'pip is missing for interpreter "{sys.executable}". '
                     f'Run "{sys.executable}" -m ensurepip --upgrade, '
                     "then retry agent.cmd setup or record.cmd --check.")
    elif failed:
        next_step = ("pip ran but installation failed. Inspect installer_tail for "
                     "network, package-index, permission or dependency errors; "
                     "resolve the cause and retry agent.cmd setup.")
    else:
        next_step = after.get("next", "agent.cmd start")
    return {**after, "install_invoked": True, "installer_exit_code": result.returncode,
            "installer_tail": (result.stdout + "\n" + result.stderr)[-2500:],
            "ok": after["ok"] and not failed, "next": next_step}



def recorder_cache_dir() -> Path:
    """Per-user cache, outside the source folder and shared by .cmd/.bat."""
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "Agent-Computer-Bridge"


def recorder_fingerprint() -> dict[str, str | int]:
    """Cheap identity check; never claim this continuously validates packages."""
    return {
        "schema": 1,
        "interpreter": str(Path(sys.executable).resolve()).casefold(),
        "python": sys.version,
        "prefix": str(Path(sys.prefix).resolve()).casefold(),
        "requirements_sha256": hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest(),
        "runtime_sha256": hashlib.sha256((ROOT / "agent_computer_bridge.py").read_bytes()).hexdigest(),
    }



def recorder_dependency_witnesses() -> list[dict[str, str | int]]:
    """Remember file identities once; cache hits use only cheap stat calls."""
    witnesses: list[dict[str, str | int]] = []
    for package, module in GROUPS["core"][1].items():
        spec = importlib.util.find_spec(module)
        distribution = importlib.metadata.distribution(package)
        metadata_folder = getattr(distribution, "_path", None)
        if spec is None or not spec.origin or metadata_folder is None:
            raise ValueError(f"Cannot validate installed recorder package: {package}")
        for file in (Path(spec.origin), Path(metadata_folder) / "METADATA"):
            stamp = file.stat()
            witnesses.append({
                "path": str(file.resolve()),
                "size": stamp.st_size,
                "modified_ns": stamp.st_mtime_ns,
            })
    return witnesses


def recorder_cache_valid(cache: dict, signature: dict) -> bool:
    if cache.get("verified") is not True or cache.get("fingerprint") != signature:
        return False
    witnesses = cache.get("witnesses")
    if not isinstance(witnesses, list) or len(witnesses) != 6:
        return False
    try:
        for witness in witnesses:
            path = Path(witness["path"])
            stamp = path.stat()
            if not path.is_file() or (stamp.st_size, stamp.st_mtime_ns) != (
                witness["size"], witness["modified_ns"]
            ):
                return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return True


@contextmanager
def recorder_bootstrap_lock(directory: Path):
    """Serialize first-time pip installation across simultaneous double-clicks."""
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / "recorder-bootstrap.lock"
    with file.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            # Lock byte 0 directly. Even reading it before locking raises a
            # Windows sharing violation while another process owns the lock.
            handle.seek(0)
            deadline = time.monotonic() + 240
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Another recorder setup is still in progress.")
                    time.sleep(0.25)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def recorder_ready(*, repair: bool = False) -> dict:
    """First launch: verify/install Core; later launches: cheap local cache.

    Explicit --repair invalidates an otherwise successful cache after a
    manual pip uninstall or damaged installation. Failed checks never cache.
    """
    if os.name != "nt" or sys.version_info < MIN_PYTHON:
        return {"ok": False, "error": "The recorder needs Windows and Python 3.10+."}
    directory = recorder_cache_dir()
    with recorder_bootstrap_lock(directory):
        signature = recorder_fingerprint()
        cache_file = directory / "recorder-dependencies.json"
        if not repair:
            try:
                previous = json.loads(cache_file.read_text(encoding="utf-8"))
                if recorder_cache_valid(previous, signature):
                    return {"ok": True, "cache_hit": True, "checked": False,
                            "installed": False, "message": "already_verified"}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        report = status()
        installed = False
        if not report["ok"]:
            report = install()
            installed = bool(report.get("install_invoked"))
        if not report["ok"]:
            # Never trust an old receipt after failed verification.
            cache_file.unlink(missing_ok=True)
            return {"ok": False, "cache_hit": False, "checked": True,
                    "installed": installed,
                    "error": (report.get("error")
                              or report.get("installer_tail")
                              or (report.get("core_runtime_import") or {}).get("error_tail")
                              or "Required recorder dependencies are not ready."),
                    "next": report.get("next") or "Check installer output, then retry record.cmd --check.",
                    "requirements": "requirements.txt"}
        receipt = {"verified": True, "fingerprint": signature,
                   "witnesses": recorder_dependency_witnesses()}
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(directory),
                prefix="recorder-deps-", suffix=".tmp", delete=False,
            ) as temp:
                json.dump(receipt, temp)
                path = Path(temp.name)
            os.replace(path, cache_file)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        return {"ok": True, "cache_hit": False, "checked": True,
                "installed": installed, "message": "recorder_ready"}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Dependency-only diagnostics; no core imports, no MCP registration")
    commands = p.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    doctor = commands.add_parser("doctor")
    setup = commands.add_parser("setup")
    recorder = commands.add_parser("recorder-ready",
                                   help="verify/install recorder dependencies once and cache success")
    recorder.add_argument("--repair", action="store_true",
                          help="ignore the cached receipt and verify the environment again")
    commands.add_parser("help")
    for option in (check, doctor, setup):
        option.add_argument("--with-mcp", action="store_true")
        option.add_argument("--with-wgc", action="store_true")
        option.add_argument("--with-build-tools", action="store_true")
    a = p.parse_args(argv)
    if a.command == "help":
        answer = {
            "ok": True, "entry": "agent.cmd",
            "first_run": ["agent.cmd doctor", "agent.cmd setup (only if core dependencies missing)",
                          "agent.cmd start", "agent.cmd windows",
                          "agent.cmd observe --controller-id ID",
                          "agent.cmd finish --controller-id ID"],
            "installation": {
                "core": "agent.cmd setup",
                "optional_mcp": "agent.cmd setup --with-mcp",
                "optional_graphics_capture": "agent.cmd setup --with-wgc",
                "developer_build": "agent.cmd setup --with-build-tools",
            },
            "guide": "skills/agent-computer-bridge/SKILL.md",
            "no_mcp_needed": True, "requires_windows_python": "3.10+",
        }
    elif a.command == "setup":
        answer = install(with_mcp=a.with_mcp, with_wgc=a.with_wgc,
                         with_build=a.with_build_tools)
    elif a.command == "recorder-ready":
        try:
            answer = recorder_ready(repair=a.repair)
        except (OSError, ValueError, TimeoutError) as error:
            answer = {"ok": False, "error": f"{type(error).__name__}: {error}",
                      "next": "Check write access and Python installation, then run record.cmd --repair."}
    else:
        answer = status(with_mcp=a.with_mcp, with_wgc=a.with_wgc,
                        with_build=a.with_build_tools)
    print(json.dumps(answer, ensure_ascii=True, indent=2))
    return 0 if answer["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
