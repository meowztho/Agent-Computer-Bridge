"""Build and verify the current, domain-neutral Windows release ZIP.

This is a closed allowlist: developer history, recordings, journals,
screenshots, obsolete source builds and any untracked files are excluded.
No network or pip installation is required.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]

RELEASE_FILES = (
    ".gitignore",
    ".zcode-plugin/plugin.json",
    "AGENTS.md",
    "README.md",
    "VERSION.txt",
    "agent_computer_bridge.py",
    "agent_computer_bridge_mcp.py",
    "agent_cli.py",
    "agent_setup.py",
    "agent.cmd",
    "recorder_portable.py",
    "install.cmd",
    "install-mcp.cmd",
    "install-wgc.cmd",
    "start.cmd",
    "record.cmd",
    "mcp.cmd",
    "build-recorder-portable.cmd",
    "build-recorder-portable.bat",
    "requirements.txt",
    "requirements-mcp.txt",
    "requirements-wgc.txt",
    "requirements-portable.txt",
    "zcode-config-example.json",
    "assets/agent_computer_bridge.ico",
    "assets/agent_computer_bridge.png",
    "skills/agent-computer-bridge/SKILL.md",
    "skills/agent-computer-bridge/references/windows-surfaces.md",
    "skills/agent-computer-bridge/references/shell-roundtrip.md",
    "tools/build_portable_recorder.py",
    "tools/build_release_package.py",
    "tests/test_bridge.py",
    "tests/test_policy_and_recorder.py",
    "tests/test_agent_self_recording.py",
    "tests/test_remote_surface_and_mcp.py",
    "tests/test_contextual_handoff.py",
    "tests/test_release_package.py",
    "tests/test_shell_entrypoint.py",
    "tests/test_dependency_bootstrap.py",
)
BINARY_SUFFIXES = {".exe", ".png", ".ico"}
# Application-specific cases are historical evidence, not reusable guidance.
FORBIDDEN_SHIPPED_TEXT = ("a" + "tas", "bridge " + "executor", "chart " + "strategies")
ZIP_TIME = (2026, 10, 17, 0, 0, 0)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_files() -> tuple[str, list[str]]:
    lines = (ROOT / "VERSION.txt").read_text(encoding="utf-8").splitlines()
    if len(lines) != 2 or not re.fullmatch(r"\d+\.\d+", lines[0]):
        raise ValueError("invalid VERSION.txt")
    version, build = (v.strip() for v in lines)
    revision = build.rsplit("-", 1)[-1]
    if not re.fullmatch(r"r\d+", revision):
        raise ValueError("invalid build revision")
    if not build.startswith("2026-") or len(build) < 14:
        raise ValueError("invalid build metadata")
    # Source snapshots follow the existing vX_Y_rN naming convention.
    artifact = f"agent_computer_bridge_v{version.replace('.', '_')}_{revision}.py"
    exe = f"portable/Agent-Computer-Bridge-Recorder-v{version}-{revision}-portable.exe"
    source = (ROOT / "agent_computer_bridge.py").read_bytes()
    if (ROOT / artifact).read_bytes() != source:
        raise ValueError("canonical source and versioned source differ")
    if f'VERSION = "{version}"'.encode() not in source or f'BUILD_ID = "{build}"'.encode() not in source:
        raise ValueError("source/version metadata mismatch")
    plugin = json.loads((ROOT / ".zcode-plugin/plugin.json").read_text(encoding="utf-8"))
    if plugin.get("version") != version + ".0":
        raise ValueError("plugin version metadata mismatch")
    return f"Agent-Computer-Bridge-v{version}-{revision}", list(RELEASE_FILES) + [artifact, exe]


def files_and_hashes() -> tuple[str, dict[str, bytes], dict[str, str]]:
    folder, names = release_files()
    if len(names) != len(set(names)):
        raise ValueError("duplicate release entries")
    contents: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    for name in sorted(names):
        normalized = PurePosixPath(name)
        if normalized.is_absolute() or ".." in normalized.parts or str(normalized) != name:
            raise ValueError("unsafe release path: " + name)
        file = ROOT / Path(*normalized.parts)
        if not file.is_file() or file.is_symlink():
            raise FileNotFoundError(f"required release file missing or symlinked: {name}")
        body = file.read_bytes()
        if file.suffix.lower() not in BINARY_SUFFIXES:
            text = body.decode("utf-8")
            violations = [term for term in FORBIDDEN_SHIPPED_TEXT if term in text.casefold()]
            if violations:
                raise ValueError(f"domain-specific text in {name}: {violations}")
        contents[name] = body
        digests[name] = sha(body)
    return folder, contents, digests


def manifest_text(digests: dict[str, str]) -> bytes:
    return ("\n".join(f"{digest}  {name}" for name, digest in sorted(digests.items()))
            + "\n").encode("utf-8")


def verify_archive(path: Path, folder: str, expected: dict[str, str]) -> None:
    with ZipFile(path) as archive:
        entries = archive.namelist()
        expected_names = {f"{folder}/{name}" for name in expected}
        expected_names.add(f"{folder}/SHA256SUMS.txt")
        if len(entries) != len(set(entries)) or set(entries) != expected_names:
            raise ValueError("archive has missing, duplicate, or unexpected members")
        for name in entries:
            prefix = folder + "/"
            if not name.startswith(prefix) or ".." in PurePosixPath(name).parts:
                raise ValueError("unsafe archive entry " + name)
        digest_lines = archive.read(f"{folder}/SHA256SUMS.txt").decode("utf-8").splitlines()
        actual = {}
        for entry in digest_lines:
            digest, _, name = entry.partition("  ")
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not name or name in actual:
                raise ValueError("invalid SHA256SUMS entry")
            actual[name] = digest
        if actual != expected:
            raise ValueError("archive checksum manifest differs from source")
        for name, digest in actual.items():
            if sha(archive.read(f"{folder}/{name}")) != digest:
                raise ValueError("archive member failed checksum: " + name)
        if archive.testzip() is not None:
            raise ValueError("archive integrity check failed")


def main() -> int:
    folder, contents, digests = files_and_hashes()
    destination = ROOT / "dist"
    destination.mkdir(parents=True, exist_ok=True)
    package = destination / (folder + ".zip")
    temporary = destination / (folder + ".zip.tmp")
    try:
        with ZipFile(temporary, "w") as archive:
            for name, body in sorted(contents.items()):
                info = ZipInfo(f"{folder}/{name}", ZIP_TIME)
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, body, compresslevel=6)
            info = ZipInfo(f"{folder}/SHA256SUMS.txt", ZIP_TIME)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, manifest_text(digests), compresslevel=6)
        verify_archive(temporary, folder, digests)
        temporary.replace(package)
    finally:
        if temporary.exists():
            temporary.unlink()
    sidecar = package.with_suffix(".zip.sha256")
    sidecar.write_text(sha(package.read_bytes()) + "  " + package.name + "\n",
                       encoding="ascii", newline="\n")
    print(json.dumps({
        "package": str(package),
        "files": len(digests),
        "size_bytes": package.stat().st_size,
        "sha256": sha(package.read_bytes()),
        "sidecar": str(sidecar),
        "verified": True,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
