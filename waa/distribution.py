"""Verified source releases with stable local state and explicit activation.

This module deliberately uses only the standard library: a release contains an
identical copy named ``waa-install.py`` for the first installation. GitHub CLI
verifies the immutable release and its assets before any downloaded code runs.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, Iterator
from uuid import uuid4


REPOSITORY = "kandotrun/we-are-agents"
MANIFEST = "release-manifest.json"
INSTALLER = "waa-install.py"
MAX_MANIFEST = 64 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_FORBIDDEN = {".git", ".tools", ".local", "private", ".venv", "__pycache__"}
_REQUIRED = {"release.json", "pyproject.toml", "uv.lock", "upstream.lock.json",
             "waa/cli.py", "waa/distribution.py", "integrations/pi/extension.mjs",
             "integrations/pi/adapter.mjs"}


class DistributionError(RuntimeError):
    """An installation failure whose text excludes remote output and secrets."""


def version_tuple(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 64 or not _VERSION.fullmatch(value):
        raise DistributionError("Use an explicit stable version such as 0.2.0")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def default_home() -> Path:
    explicit = os.environ.get("WAA_HOME")
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return Path(explicit).expanduser().resolve() if explicit else (base / "we-are-agents").expanduser().resolve()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _json(raw: bytes) -> dict[str, Any]:
    try:
        result = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        raise DistributionError("Invalid release metadata") from None
    if not isinstance(result, dict):
        raise DistributionError("Invalid release metadata")
    return result


def descriptor(data: Any, version: str | None = None) -> dict[str, Any]:
    fields = {"schema", "version", "tag", "repository", "source_commit", "compatibility"}
    try:
        if not isinstance(data, dict) or set(data) != fields or type(data["schema"]) is not int or data["schema"] != 1:
            raise ValueError
        version_tuple(data["version"])
        if (version is not None and data["version"] != version
                or data["tag"] != "v" + data["version"] or data["repository"] != REPOSITORY
                or not isinstance(data["source_commit"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", data["source_commit"])):
            raise ValueError
        compatibility = data["compatibility"]
        if (not isinstance(compatibility, dict)
                or set(compatibility) != {"state_format", "reason_network", "iroh_relay", "python", "adapters"}
                or type(compatibility["state_format"]) is not int or compatibility["state_format"] < 1
                or compatibility["python"] != ">=3.11"):
            raise ValueError
        for tool in ("reason_network", "iroh_relay"):
            if not isinstance(compatibility[tool], str) or not compatibility[tool].startswith("v"):
                raise ValueError
            version_tuple(compatibility[tool][1:])
        adapters = compatibility["adapters"]
        if not isinstance(adapters, dict) or set(adapters) != {"pi", "openclaw"}:
            raise ValueError
        for versions in adapters.values():
            if not isinstance(versions, list) or not versions:
                raise ValueError
            for item in versions:
                version_tuple(item)
    except (KeyError, TypeError, ValueError, DistributionError):
        raise DistributionError("Release descriptor does not match the supported format or repository") from None
    return data


def manifest(data: Any, version: str) -> dict[str, Any]:
    if not isinstance(data, dict) or "assets" not in data:
        raise DistributionError("Invalid release manifest")
    descriptor({key: value for key, value in data.items() if key != "assets"}, version)
    expected = {f"we-are-agents-v{version}.tar.gz", INSTALLER}
    assets = data["assets"]
    if not isinstance(assets, dict) or set(assets) != expected:
        raise DistributionError("Release manifest must name the archive and installer")
    for name, asset in assets.items():
        maximum = MAX_ARCHIVE if name.endswith(".tar.gz") else 1024 * 1024
        if (not isinstance(asset, dict) or set(asset) != {"sha256", "size"}
                or not isinstance(asset["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
                or type(asset["size"]) is not int or not 0 < asset["size"] <= maximum):
            raise DistributionError("Invalid release asset digest or size")
    return data


def _read(path: Path, maximum: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ValueError
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError
        return data
    except (OSError, ValueError):
        raise DistributionError("Cannot read a complete regular release file within its size limit") from None


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".waa-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _encoded(data: Any) -> bytes:
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def _payload(raw: bytes, metadata: dict[str, Any]) -> list[tuple[str, bytes, int]]:
    """Validate and unpack the same bytes whose archive digest was checked."""
    root = f"we-are-agents-v{metadata['version']}"
    result: list[tuple[str, bytes, int]] = []
    seen: set[str] = set()
    expanded = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for index, member in enumerate(archive):
                path = PurePosixPath(member.name)
                normalized = member.name.rstrip("/")
                if (index >= 10000 or len(member.name) > 1024 or "\\" in member.name
                        or path.is_absolute() or ".." in path.parts or normalized != str(path)
                        or not path.parts or path.parts[0] != root or normalized in seen
                        or not (member.isdir() or member.isfile())):
                    raise ValueError
                seen.add(normalized)
                relative = path.parts[1:]
                if not relative:
                    if member.isdir():
                        continue
                    raise ValueError
                if any(part in _FORBIDDEN or part.startswith(".waa-") for part in relative):
                    raise ValueError
                if member.isdir():
                    continue
                expanded += member.size
                if member.size < 0 or expanded > MAX_EXPANDED:
                    raise ValueError
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError
                body = source.read(member.size + 1)
                if len(body) != member.size:
                    raise ValueError
                result.append((str(PurePosixPath(*relative)), body, 0o755 if member.mode & 0o111 else 0o644))
        files = {name: body for name, body, _mode in result}
        if not _REQUIRED <= files.keys():
            raise ValueError
        expected = {key: value for key, value in metadata.items() if key != "assets"}
        if descriptor(_json(files["release.json"]), metadata["version"]) != expected:
            raise ValueError
        installer = metadata["assets"][INSTALLER]
        if (len(files["waa/distribution.py"]) != installer["size"]
                or hashlib.sha256(files["waa/distribution.py"]).hexdigest() != installer["sha256"]):
            raise ValueError
        import tomllib

        if tomllib.loads(files["pyproject.toml"].decode())["project"]["version"] != metadata["version"]:
            raise ValueError
    except (tarfile.TarError, OSError, ValueError, KeyError, TypeError, UnicodeError, DistributionError):
        raise DistributionError("Release archive has unsafe paths, missing files, or mismatched metadata") from None
    return result


def _runner(argv: list[str], *, timeout: float, cwd: Path | None = None,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class Manager:
    def __init__(self, home: Path | str | None = None, *, runner: Callable[..., Any] | None = None,
                 command_budget: float | None = None):
        self.home = Path(home).expanduser().resolve() if home is not None else default_home()
        if self.home == Path(self.home.anchor):
            raise DistributionError("Choose a dedicated installation directory")
        self.runner = runner or _runner
        self.deadline = time.monotonic() + command_budget if command_budget is not None else None

    def _run(self, argv: list[str], *, timeout: float = 60, cwd: Path | None = None,
             env: dict[str, str] | None = None) -> str:
        try:
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError
                timeout = min(timeout, remaining)
            result = self.runner(argv, timeout=timeout, cwd=cwd, env=env)
            if result.returncode != 0 or not isinstance(result.stdout, str):
                raise ValueError
            return result.stdout
        except (OSError, ValueError, subprocess.SubprocessError):
            raise DistributionError("Release command failed; install current gh/uv and inspect the command locally; remote output omitted") from None

    def _layout(self) -> None:
        for path in (self.home, *(self.home / name for name in ("bin", "releases", "config", "state", "workspace"))):
            try:
                path.mkdir(parents=True, mode=0o700, exist_ok=True)
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError
            except (OSError, ValueError):
                raise DistributionError("Installation directories must be owned, private directories") from None

    @contextmanager
    def _lock(self) -> Iterator[None]:
        import fcntl

        self._layout()
        try:
            fd = os.open(self.home / "update.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "r+b") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise DistributionError("Update lock must be an owned private file")
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise DistributionError("Another installation or update is in progress") from None
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            raise DistributionError("Cannot lock the private installation directory") from None

    def _installed(self, version: str) -> dict[str, Any]:
        version_tuple(version)
        directory = self.home / "releases" / version
        if directory.is_symlink() or not directory.is_dir():
            raise DistributionError("Requested version is not a completed local installation")
        marker = _json(_read(directory / ".waa-installed.json", MAX_MANIFEST * 16))
        if not isinstance(marker, dict) or set(marker) != {"manifest", "payload_sha256"}:
            raise DistributionError("Invalid installed-release receipt")
        metadata = manifest(marker["manifest"], version)
        hashes = marker["payload_sha256"]
        if not isinstance(hashes, dict) or not _REQUIRED <= hashes.keys():
            raise DistributionError("Invalid installed payload receipt")
        for name, digest in hashes.items():
            if not isinstance(name, str):
                raise DistributionError("Invalid installed payload receipt")
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts
                    or str(path) != name or any(part in _FORBIDDEN for part in path.parts)
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or hashlib.sha256(_read(directory / name, MAX_EXPANDED)).hexdigest() != digest):
                raise DistributionError("Installed release payload changed; leave the current version running")
        expected = {key: value for key, value in metadata.items() if key != "assets"}
        if descriptor(_json(_read(directory / "release.json", MAX_MANIFEST)), version) != expected:
            raise DistributionError("Installed descriptor does not match its verified receipt")
        executable = directory / ".venv/bin/waa"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise DistributionError("Installed version is missing its executable")
        return metadata

    def _pointer(self, name: str) -> str | None:
        path = self.home / name
        if not os.path.lexists(path):
            return None
        if not path.is_symlink():
            raise DistributionError("Installation pointers must be managed symlinks")
        target = path.resolve()
        if target.parent != self.home / "releases":
            raise DistributionError("Installation pointer is outside the release directory")
        version_tuple(target.name)
        return target.name

    def status(self) -> dict[str, Any]:
        current = self._pointer("current")
        if current is not None:
            self._installed(current)
        versions: list[str] = []
        releases = self.home / "releases"
        if releases.is_dir():
            for path in releases.iterdir():
                if _VERSION.fullmatch(path.name) and (path / ".waa-installed.json").is_file():
                    versions.append(path.name)
        return {"installed": current is not None, "current_version": current,
                "previous_version": self._pointer("previous"),
                "installed_versions": sorted(versions, key=version_tuple), "home": str(self.home),
                "config": str(self.home / "config"), "state": str(self.home / "state"),
                "workspace": str(self.home / "workspace"), "launcher": str(self.home / "bin/waa")}

    def _latest(self) -> str:
        raw = self._run(["gh", "release", "view", "--repo", REPOSITORY,
                         "--json", "tagName,isDraft,isPrerelease"], timeout=15)
        result = _json(raw.encode())
        tag = result.get("tagName")
        if (not isinstance(tag, str) or not tag.startswith("v")
                or result.get("isDraft") is not False or result.get("isPrerelease") is not False):
            raise DistributionError("Latest release is not a published stable version")
        version_tuple(tag[1:])
        return tag[1:]

    def _fetch_manifest(self, version: str, directory: Path) -> dict[str, Any]:
        version_tuple(version)
        tag = "v" + version
        self._run(["gh", "release", "verify", tag, "--repo", REPOSITORY])
        self._run(["gh", "release", "download", tag, "--repo", REPOSITORY,
                   "--pattern", MANIFEST, "--dir", str(directory)])
        self._run(["gh", "release", "verify-asset", tag, str(directory / MANIFEST), "--repo", REPOSITORY])
        return manifest(_json(_read(directory / MANIFEST, MAX_MANIFEST)), version)

    def check(self) -> dict[str, Any]:
        current = self._pointer("current")
        active = self._installed(current) if current else None
        latest = self._latest()
        with tempfile.TemporaryDirectory(prefix="waa-release-check-") as folder:
            metadata = self._fetch_manifest(latest, Path(folder))
        compatible = active is None or self._compatible(active, metadata)
        return {"current_version": current, "latest_version": latest,
                "update_available": current is not None and version_tuple(latest) > version_tuple(current),
                "compatible": compatible, "compatibility": metadata["compatibility"],
                "repository": REPOSITORY, "automatic_apply": False}

    @staticmethod
    def _compatible(first: dict[str, Any], second: dict[str, Any]) -> bool:
        return all(first["compatibility"][field] == second["compatibility"][field]
                   for field in ("state_format", "reason_network"))

    def _prepare(self, directory: Path, version: str) -> None:
        env = {**os.environ, "WAA_HOME": str(self.home), "WAA_NO_UPDATE_CHECK": "1", "UV_NO_PROGRESS": "1"}
        self._run(["uv", "sync", "--locked", "--project", str(directory), "--python", "3.13", "--managed-python"],
                  timeout=600, cwd=directory, env=env)
        executable = directory / ".venv/bin/waa"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise DistributionError("Prepared release did not create its executable")
        if self._run([str(executable), "--version"], timeout=30, cwd=directory, env=env).strip() != version:
            raise DistributionError("Prepared executable reports a different release version")
        self._run([str(executable), "bootstrap"], timeout=300, cwd=directory, env=env)
        smoke = _json(self._run([str(executable), "smoke"], timeout=300, cwd=directory, env=env).encode())
        if smoke.get("ok") is not True:
            raise DistributionError("Prepared release did not pass its isolated transport check")

    def _stage(self, version: str, active: dict[str, Any] | None) -> dict[str, Any]:
        directory = self.home / "releases" / version
        if os.path.lexists(directory):
            metadata = self._installed(version)
            if active is not None and not self._compatible(active, metadata):
                raise DistributionError("This release changes state or engine compatibility; a reviewed migration is required")
            self._prepare(directory, version)
            return metadata
        with tempfile.TemporaryDirectory(prefix="waa-release-download-", dir=self.home) as temporary:
            download = Path(temporary)
            metadata = self._fetch_manifest(version, download)
            if active is not None and not self._compatible(active, metadata):
                raise DistributionError("This release changes state or engine compatibility; a reviewed migration is required")
            name = f"we-are-agents-v{version}.tar.gz"
            self._run(["gh", "release", "download", "v" + version, "--repo", REPOSITORY,
                       "--pattern", name, "--dir", str(download)])
            self._run(["gh", "release", "verify-asset", "v" + version, str(download / name), "--repo", REPOSITORY])
            raw = _read(download / name, MAX_ARCHIVE)
            expected = metadata["assets"][name]
            if len(raw) != expected["size"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
                raise DistributionError("Release archive digest or size does not match its verified manifest")
            payload = _payload(raw, metadata)
        try:
            directory.mkdir(mode=0o700)
        except OSError:
            raise DistributionError("Candidate release directory already exists or cannot be created") from None
        try:
            hashes: dict[str, str] = {}
            for name, body, mode in payload:
                target = directory / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write(target, body, mode)
                hashes[name] = hashlib.sha256(body).hexdigest()
            self._prepare(directory, version)
            _write(directory / ".waa-installed.json", _encoded({"manifest": metadata, "payload_sha256": hashes}))
        except BaseException:
            # Only the fresh code directory owned by this invocation is removed.
            # Stable config/state/workspace and all previous releases are untouched.
            shutil.rmtree(directory)
            raise
        return metadata

    def _activate(self, version: str, previous: str | None) -> None:
        self._pointer("previous")  # Refuse to overwrite an unrelated regular file.
        launcher = self.home / "bin/waa"
        content = ("#!/bin/sh\n# Managed by We Are Agents\nexport WAA_HOME="
                   + shlex.quote(str(self.home)) + "\nexec "
                   + shlex.quote(str(self.home / "current/.venv/bin/waa")) + ' "$@"\n').encode()
        if launcher.exists() and not _read(launcher, 64 * 1024).startswith(b"#!/bin/sh\n# Managed by We Are Agents\n"):
            raise DistributionError("Existing launcher is not managed by We Are Agents")
        _write(launcher, content, 0o755)
        for name, target in (("previous", previous), ("current", version)):
            if target is None:
                continue
            temporary = self.home / (".waa-link-" + uuid4().hex)
            try:
                temporary.symlink_to(Path("releases") / target)
                os.replace(temporary, self.home / name)
            finally:
                temporary.unlink(missing_ok=True)
        fd = os.open(self.home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def install(self, version: str) -> dict[str, Any]:
        version_tuple(version)
        with self._lock():
            current = self._pointer("current")
            if current is not None:
                raise DistributionError("An active installation already exists; use apply for an update")
            self._stage(version, None)
            self._activate(version, None)
        return {**self.status(), "action": "installed", "restart_required": False}

    def apply(self, version: str) -> dict[str, Any]:
        version_tuple(version)
        with self._lock():
            current = self._pointer("current")
            if current is None:
                raise DistributionError("Install an explicit first version before applying updates")
            active = self._installed(current)
            if version_tuple(version) < version_tuple(current):
                raise DistributionError("Use rollback to select an older installed version")
            if current == version:
                return {**self.status(), "action": "already_current", "restart_required": False}
            self._stage(version, active)
            self._activate(version, current)
        return {**self.status(), "action": "updated", "restart_required": True,
                "notice": "Restart owned adapter processes; read messages again before acknowledging them."}

    def rollback(self, version: str) -> dict[str, Any]:
        version_tuple(version)
        with self._lock():
            current = self._pointer("current")
            if current is None or version_tuple(version) >= version_tuple(current):
                raise DistributionError("Rollback requires an older completed local installation")
            active, target = self._installed(current), self._installed(version)
            if not self._compatible(active, target):
                raise DistributionError("Rollback changes state or engine compatibility; leave the current version running")
            self._prepare(self.home / "releases" / version, version)
            self._activate(version, current)
        return {**self.status(), "action": "rolled_back", "restart_required": True,
                "notice": "Code only was switched. Keys, registry, unread mail and journals were not restored."}


def maybe_notice(home: Path | str | None = None) -> None:
    """Best-effort daily notice; callers must restrict this to interactive CLI use."""
    if os.environ.get("WAA_NO_UPDATE_CHECK") == "1":
        return
    try:
        manager = Manager(home, command_budget=3)
        if manager._pointer("current") is None:
            return
        cache = manager.home / "last-update-check.json"
        try:
            previous = _json(_read(cache, MAX_MANIFEST)) if cache.exists() else {}
        except DistributionError:
            previous = {}
        now = time.time()
        checked = previous.get("checked_at")
        if type(checked) in {int, float} and 0 <= now - checked < 24 * 60 * 60:
            return
        # Record the attempt too: an unavailable network must not slow every CLI invocation.
        _write(cache, _encoded({"checked_at": now}))
        result = manager.check()
        _write(cache, _encoded({"checked_at": now, **result}))
        if result["update_available"]:
            print(f"We Are Agents {result['latest_version']} is available; run waa update check.", file=sys.stderr)
    except (OSError, RuntimeError, ValueError):
        return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Verified We Are Agents releases; application is always explicit")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "check", "apply", "rollback", "status"):
        command = commands.add_parser(name)
        command.add_argument("--home", type=Path)
        if name in {"install", "apply", "rollback"}:
            command.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise DistributionError("Python 3.11 or later is required")
        manager = Manager(args.home)
        operation = getattr(manager, args.command)
        result = operation(args.version) if hasattr(args, "version") else operation()
        print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except DistributionError as error:
        print(f"waa update: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, RuntimeError, ValueError):
        print("waa update: operation failed; no remote diagnostics or credentials are shown", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
