from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import subprocess
import sys

from .bootstrap import ROOT, ensure_tools
from .runtime import prepare_network, private_network_env


def _version() -> str:
    try:
        return version("we-are-agents")
    except PackageNotFoundError:
        import tomllib

        return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _network_state() -> Path:
    if os.environ.get("WAA_HOME"):
        return Path(os.environ["WAA_HOME"]).expanduser().resolve() / "state" / "network"
    if ROOT.parent.name == "releases" and (ROOT / "release.json").is_file():
        return ROOT.parent.parent / "state" / "network"
    return ROOT / ".local" / "network"


def main() -> None:
    if sys.argv[1:2] == ["update"]:
        from .distribution import main as update_main

        update_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="Private team agent networking")
    parser.add_argument("--version", action="version", version=_version())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap", help="Download and verify the pinned engine and relay locally")
    commands.add_parser("smoke", help="Run an isolated synthetic two-participant transport check")
    commands.add_parser("update", help="Install, check, apply, or roll back a verified GitHub release")
    network = commands.add_parser("network", help="Run the pinned engine with project-local state", add_help=False)
    network.add_argument("--state", type=Path, default=_network_state())
    network.add_argument("--relay", required=True, help="Explicit team-owned HTTPS relay (HTTP on loopback only)")
    network.add_argument("--invite-file", type=Path, help="Read a private invite file for join")
    network.add_argument("args", nargs=argparse.REMAINDER)
    relay = commands.add_parser("relay", help="Run the pinned relay in the foreground", add_help=False)
    relay.add_argument("args", nargs=argparse.REMAINDER)
    identity = commands.add_parser("identity", help="Create or show an agent's registered signing identity")
    identity_commands = identity.add_subparsers(dest="identity_command", required=True)
    identity_init = identity_commands.add_parser("init")
    identity_init.add_argument("--directory", type=Path, required=True)
    identity_init.add_argument("--owner-id", required=True)
    identity_init.add_argument("--owner-name", required=True)
    identity_init.add_argument("--agent-name", required=True)
    identity_init.add_argument("--registry", type=Path)
    identity_show = identity_commands.add_parser("show")
    identity_show.add_argument("--profile", type=Path, required=True)
    pi = commands.add_parser("pi", help="Connect Pi with an explicit workspace and Reason state")
    pi_commands = pi.add_subparsers(dest="pi_command", required=True)
    pi_run = pi_commands.add_parser("run")
    pi_run.add_argument("--binding", type=Path, required=True)
    pi_run.add_argument("args", nargs=argparse.REMAINDER)
    pi_bridge = pi_commands.add_parser("bridge")
    pi_bridge.add_argument("--binding", type=Path, required=True)
    pi_bridge.add_argument("--session", required=True)
    openclaw = commands.add_parser("openclaw", help="Signed OpenClaw inbox and local wake notifications")
    openclaw_commands = openclaw.add_subparsers(dest="openclaw_command", required=True)
    openclaw_inbox = openclaw_commands.add_parser("inbox")
    openclaw_inbox.add_argument("--binding", type=Path, required=True)
    openclaw_inbox.add_argument("--session", required=True)
    openclaw_wake = openclaw_commands.add_parser("wake")
    openclaw_wake.add_argument("--binding", type=Path, required=True)
    openclaw_wake.add_argument("--once", action="store_true")
    t3 = commands.add_parser("t3", help="T3 Code inbox and queued wake notifications")
    t3_commands = t3.add_subparsers(dest="t3_command", required=True)
    wake = t3_commands.add_parser("wake")
    wake.add_argument("--binding", type=Path, required=True)
    wake.add_argument("--once", action="store_true")
    inbox = t3_commands.add_parser("inbox")
    inbox.add_argument("--binding", type=Path, required=True)
    t3_bridge = t3_commands.add_parser("bridge")
    t3_bridge.add_argument("--binding", type=Path, required=True)
    args = parser.parse_args()
    try:
        if (args.command == "pi" and args.pi_command == "run"
                and sys.stdout.isatty() and sys.stderr.isatty()
                and os.environ.get("WAA_NO_UPDATE_CHECK") != "1"):
            from .distribution import maybe_notice

            maybe_notice()
        if args.command == "bootstrap":
            print(json.dumps({name: str(path.relative_to(ROOT)) for name, path in ensure_tools().items()}, indent=2))
        elif args.command == "smoke":
            from .smoke import run_smoke

            report = run_smoke()
            print(json.dumps(report, indent=2))
            if not report["ok"]:
                raise SystemExit(1)
        elif args.command in {"network", "relay"}:
            forwarded = args.args[1:] if args.args[:1] == ["--"] else args.args
            if not forwarded:
                parser.error(f"{args.command} requires arguments after --; use -- --help")
            if args.command == "network" and args.invite_file is not None:
                if forwarded[0] != "join":
                    raise ValueError("--invite-file is only supported with join.")
                invite = args.invite_file.read_text().strip()
                if len(invite) > 16384:
                    raise ValueError("Invite file is too large.")
                forwarded = ["join", invite, *forwarded[1:]]
            tool = ensure_tools()["reason-network" if args.command == "network" else "iroh-relay"]
            if args.command == "network":
                forwarded = prepare_network(args.state, args.relay, forwarded)
            with private_network_env(args.state, args.relay) if args.command == "network" else nullcontext(None) as env:
                result = subprocess.run([str(tool), *forwarded], env=env)
            raise SystemExit(result.returncode)
        elif args.command == "identity":
            from .identity import initialize_identity, public_identity

            if args.identity_command == "init":
                result = initialize_identity(
                    args.directory, owner_id=args.owner_id, owner_name=args.owner_name,
                    agent_name=args.agent_name, registry_file=args.registry,
                )
            else:
                result = public_identity(args.profile)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "pi":
            from .pi import run_bridge, run_pi

            if args.pi_command == "run":
                forwarded = args.args[1:] if args.args[:1] == ["--"] else args.args
                raise SystemExit(run_pi(args.binding, forwarded))
            run_bridge(args.binding, args.session)
        elif args.command == "openclaw":
            if args.openclaw_command == "inbox":
                from .mailbox import run_mcp

                run_mcp(args.binding, args.session, adapter="openclaw")
            else:
                from .openclaw import run_wake

                asyncio.run(run_wake(args.binding, once=args.once))
        else:
            from .t3 import run_inbox, run_wake

            if args.t3_command == "wake":
                asyncio.run(run_wake(args.binding, once=args.once))
            elif args.t3_command == "inbox":
                run_inbox(args.binding)
            else:
                from .t3_bridge import run_bridge

                run_bridge(args.binding)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, RuntimeError, ValueError) as error:
        print(f"waa: {error}", file=sys.stderr)
        raise SystemExit(1) from None
