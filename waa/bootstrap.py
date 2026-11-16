"""Fetch reviewed upstream releases without running their installers."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
from pathlib import Path
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "upstream.lock.json"


def target() -> str:
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "AMD64": "x86_64"}.get(platform.machine())
    suffix = {"Darwin": "apple-darwin", "Linux": "unknown-linux-musl"}.get(platform.system())
    if arch is None or suffix is None:
        raise RuntimeError("This bootstrap supports macOS and Linux on arm64 and x86_64.")
    return f"{arch}-{suffix}"


def checked_archive(data: bytes, digest: str, executable: str) -> bytes:
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError(f"Checksum mismatch for {executable}; nothing was installed.")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [item for item in archive.getmembers() if item.isfile() and Path(item.name).name == executable]
        if len(members) != 1:
            raise RuntimeError(f"Archive must contain exactly one regular {executable} binary.")
        stream = archive.extractfile(members[0])
        assert stream is not None
        return stream.read()


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def ensure_tools() -> dict[str, Path]:
    lock = json.loads(LOCK.read_text())
    selected = target()
    tools: dict[str, Path] = {}
    for name, spec in lock["tools"].items():
        asset = spec["assets"][selected]
        cache = ROOT / ".tools" / "downloads" / f"{asset['sha256']}.tar.gz"
        if cache.exists():
            data = cache.read_bytes()
        else:
            request = urllib.request.Request(asset["url"], headers={"User-Agent": "we-are-agents-bootstrap/0.1"})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(128 * 1024 * 1024 + 1)
            if len(data) > 128 * 1024 * 1024:
                raise RuntimeError(f"Download too large for {name}.")
        binary = checked_archive(data, asset["sha256"], name)
        if not cache.exists():
            atomic_write(cache, data, 0o600)
        destination = ROOT / ".tools" / name / spec["version"] / name
        if not destination.exists() or destination.read_bytes() != binary:
            atomic_write(destination, binary, 0o755)
        tools[name] = destination
    return tools
