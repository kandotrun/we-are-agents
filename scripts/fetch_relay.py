"""Fetch only the locked relay for a Docker target, without executing it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import urllib.request

from waa.bootstrap import atomic_write, checked_archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}[args.arch]
    root = Path(__file__).resolve().parent.parent
    lock = json.loads((root / "upstream.lock.json").read_text())
    asset = lock["tools"]["iroh-relay"]["assets"][f"{arch}-unknown-linux-musl"]
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "we-are-agents-relay-build"})
    limit = 128 * 1024 * 1024
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("Relay archive exceeds the download limit")
    atomic_write(args.output, checked_archive(data, asset["sha256"], "iroh-relay"), 0o755)


if __name__ == "__main__":
    main()
