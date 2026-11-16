"""Build explicit, reproducible GitHub release assets from a clean Git commit."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tomllib
from typing import Any


REPOSITORY = "kandotrun/we-are-agents"
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
FORBIDDEN = {"private", ".local", ".tools", ".venv", ".git", "__pycache__"}
ROOT_FILES = {"README.md", "AGENTS.md", "pyproject.toml", "uv.lock", "upstream.lock.json",
              "LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "NOTICE.md"}
REQUIRED = {"waa/cli.py", "waa/distribution.py", "pyproject.toml", "uv.lock", "upstream.lock.json",
            "integrations/pi/extension.mjs", "integrations/pi/adapter.mjs"}


class BuildError(RuntimeError):
    pass


def _git(source: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(["git", "-C", str(source), *arguments], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise BuildError("Cannot read the clean release source commit") from None
    return result.stdout


def _safe_path(name: str) -> bool:
    return (bool(name) and not PurePosixPath(name).is_absolute()
            and not any(part in {"", ".", ".."} | FORBIDDEN for part in name.split("/"))
            and not any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in name))


def _included(name: str) -> bool:
    if not _safe_path(name):
        return False
    path = PurePosixPath(name)
    if name in ROOT_FILES:
        return True
    if path.parts[0] == "waa":
        return path.suffix == ".py"
    if name in {"integrations/pi/extension.mjs", "integrations/pi/adapter.mjs"}:
        return True
    if path.parts[0] in {"docs", "examples"}:
        return path.suffix in {".md", ".json", ".toml", ".yaml", ".yml", ".service", ".timer", ".plist", ".sh"}
    if path.parts[0] in {"tests", "scripts"}:
        return path.suffix in {".py", ".mjs", ".sh"}
    if path.parts[0] in {"third_party", "third-party"}:
        return path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "README"))
    return False


def _snapshot(source: Path) -> tuple[str, dict[str, tuple[int, bytes]]]:
    source = source.resolve()
    if Path(_git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve() != source:
        raise BuildError("The release source must be the Git repository root")
    commit = _git(source, "rev-parse", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BuildError("The release source requires a SHA-1 commit ID")
    try:
        clean = subprocess.run(["git", "-C", str(source), "diff-index", "--quiet", "HEAD", "--"],
                               capture_output=True)
    except OSError:
        raise BuildError("Cannot inspect the release source") from None
    if clean.returncode != 0:
        raise BuildError("Commit tracked source changes before building a release")
    files: dict[str, tuple[int, bytes]] = {}
    for raw in _git(source, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if not raw:
            continue
        try:
            metadata, encoded_name = raw.split(b"\t", 1)
            mode, kind, object_id = metadata.decode("ascii").split()
            name = encoded_name.decode("utf-8")
        except (UnicodeError, ValueError):
            raise BuildError("Invalid tracked release path") from None
        if not _included(name):
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise BuildError("Release payload must contain only regular tracked files")
        files[name] = (0o755 if mode == "100755" else 0o644, _git(source, "cat-file", "blob", object_id))
    if not REQUIRED <= files.keys():
        raise BuildError("The committed source is missing required client release assets")
    return commit, files


def _descriptor(commit: str, files: dict[str, tuple[int, bytes]], tag: str | None) -> dict[str, Any]:
    try:
        project = tomllib.loads(files["pyproject.toml"][1].decode())
        version = project["project"]["version"]
        python = project["project"]["requires-python"]
        tools = json.loads(files["upstream.lock.json"][1])["tools"]
        reason = tools["reason-network"]["version"]
        relay = tools["iroh-relay"]["version"]
    except (UnicodeError, ValueError, KeyError, TypeError):
        raise BuildError("Invalid committed release metadata") from None
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise BuildError("Release versions must use stable X.Y.Z format")
    if tag is not None and tag != f"v{version}":
        raise BuildError("The requested tag does not match the committed project version")
    if (python != ">=3.11" or not isinstance(reason, str) or not isinstance(relay, str)
            or VERSION.fullmatch(reason.removeprefix("v")) is None
            or VERSION.fullmatch(relay.removeprefix("v")) is None):
        raise BuildError("Invalid supported runtime compatibility metadata")
    return {"schema": 1, "version": version, "tag": f"v{version}", "repository": REPOSITORY,
            "source_commit": commit, "compatibility": {"state_format": 1,
                "reason_network": reason, "iroh_relay": relay, "python": python,
                "adapters": {"pi": ["0.87.1"], "openclaw": ["2026.11.4"]}}}


def _json(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _archive(root: str, files: dict[str, tuple[int, bytes]]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name in sorted(files):
                if not _safe_path(name):
                    raise BuildError("Invalid release archive path")
                mode, content = files[name]
                member = tarfile.TarInfo(f"{root}/{name}")
                member.size = len(content)
                member.mode = mode
                member.mtime = member.uid = member.gid = 0
                member.uname = member.gname = ""
                archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def build_release(source: Path, output: Path, *, tag: str | None = None) -> dict[str, Any]:
    """Read committed blobs, never ignored local state, and build the three assets."""
    commit, files = _snapshot(source)
    descriptor = _descriptor(commit, files, tag)
    files["release.json"] = (0o644, _json(descriptor))
    name = f"we-are-agents-{descriptor['tag']}"
    assets = {f"{name}.tar.gz": _archive(name, files),
              "waa-install.py": files["waa/distribution.py"][1]}
    manifest = {**descriptor, "assets": {name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
                                          for name, content in assets.items()}}
    output.mkdir(parents=True, exist_ok=True)
    complete = {**assets, "release-manifest.json": _json(manifest)}
    if any((output / name).exists() or (output / name).is_symlink() for name in complete):
        raise BuildError("Choose an output directory without existing release assets")
    for name, content in complete.items():
        target = output / name
        with target.open("xb") as stream:
            stream.write(content)
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag")
    arguments = parser.parse_args(argv)
    try:
        manifest = build_release(arguments.source, arguments.output, tag=arguments.tag)
    except (BuildError, OSError) as error:
        raise SystemExit(f"build_release: {error}") from None
    print(json.dumps({"tag": manifest["tag"], "source_commit": manifest["source_commit"],
                      "assets": sorted([*manifest["assets"], "release-manifest.json"])}, indent=2))


if __name__ == "__main__":
    main()
