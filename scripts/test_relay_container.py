"""Isolated Docker startup check (no identities, team traffic, or host services)."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def check_build_context(root):
    allowed = {"Dockerfile", "upstream.lock.json", "waa/__init__.py", "waa/bootstrap.py",
               "scripts/fetch_relay.py", "examples/docker-relay.toml"}
    with tempfile.TemporaryDirectory(prefix="waa-context-") as temporary:
        base = Path(temporary)
        context, output = base / "context", base / "output"
        context.mkdir()
        for name in allowed | {".dockerignore"}:
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
        for name in ("private/token", ".local/state", "waa/private/token", "waa/__pycache__/cache.pyc",
                     "waa/unexpected.py", "scripts/.env", "examples/private/token"):
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("SYNTHETIC EXCLUDED BUILD INPUT\n")
        guard = base / "context.Dockerfile"
        guard.write_text("FROM scratch\nCOPY . /\n")
        docker("build", "--file", str(guard), "--output", f"type=local,dest={output}", str(context))
        exported = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
        assert exported == allowed, f"Unexpected Docker build context: {sorted(exported ^ allowed)}"


def main():
    image = sys.argv[1]
    root = Path(__file__).resolve().parent.parent
    check_build_context(root)
    version = json.loads((root / "upstream.lock.json").read_text())["tools"]["iroh-relay"]["version"].removeprefix("v")
    assert docker("run", "--rm", image, "--version") == f"iroh-relay {version}"
    container = docker("run", "-d", "--read-only", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges:true",
                       "-p", "127.0.0.1::3340", image)
    try:
        info = json.loads(docker("inspect", container))[0]
        assert info["Config"]["User"] == "65532:65532"
        assert info["Config"]["StopSignal"] == "SIGINT"
        assert info["HostConfig"]["ReadonlyRootfs"]
        binding = info["NetworkSettings"]["Ports"]["3340/tcp"][0]
        assert binding["HostIp"] == "127.0.0.1"
        url = f"http://127.0.0.1:{binding['HostPort']}/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(50):
            try:
                with opener.open(url, timeout=1) as response:
                    assert response.status == 200
                break
            except (urllib.error.URLError, TimeoutError):
                if attempt == 49:
                    raise
                time.sleep(0.1)
        docker("stop", "--time", "5", container)
        info = json.loads(docker("inspect", container))[0]
        assert not info["State"]["Running"]
        assert not info["State"]["OOMKilled"]
        assert info["State"]["ExitCode"] == 0
        print("Relay container: version, nonroot/read-only startup, loopback HTTP, stop OK")
        print("Not a team, relay data-path, or real-agent acceptance test")
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
