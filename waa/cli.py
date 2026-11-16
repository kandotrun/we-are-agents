from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys

from .bootstrap import ROOT, ensure_tools
from .runtime import prepare_network, private_network_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Private team agent networking")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap", help="Download and verify the pinned engine and relay locally")
    commands.add_parser("smoke", help="Run an isolated synthetic two-participant transport check")
    network = commands.add_parser("network", help="Run the pinned engine with project-local state", add_help=False)
    network.add_argument("--state", type=Path, default=ROOT / ".local" / "network")
    network.add_argument("--relay", required=True, help="Explicit team-owned HTTPS relay (HTTP on loopback only)")
    network.add_argument("--invite-file", type=Path, help="Read a private invite file for join")
    network.add_argument("args", nargs=argparse.REMAINDER)
    relay = commands.add_parser("relay", help="Run the pinned relay in the foreground", add_help=False)
    relay.add_argument("args", nargs=argparse.REMAINDER)
    pi = commands.add_parser("pi", help="Connect Pi with an explicit workspace and Reason state")
    pi_commands = pi.add_subparsers(dest="pi_command", required=True)
    pi_run = pi_commands.add_parser("run")
    pi_run.add_argument("--binding", type=Path, required=True)
    pi_run.add_argument("args", nargs=argparse.REMAINDER)
    pi_bridge = pi_commands.add_parser("bridge")
    pi_bridge.add_argument("--binding", type=Path, required=True)
    pi_bridge.add_argument("--session", required=True)
    t3 = commands.add_parser("t3", help="T3 Code inbox and queued wake notifications")
    t3_commands = t3.add_subparsers(dest="t3_command", required=True)
    wake = t3_commands.add_parser("wake")
    wake.add_argument("--binding", type=Path, required=True)
    wake.add_argument("--once", action="store_true")
    inbox = t3_commands.add_parser("inbox")
    inbox.add_argument("--binding", type=Path, required=True)
    args = parser.parse_args()
    try:
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
        elif args.command == "pi":
            from .pi import run_bridge, run_pi

            if args.pi_command == "run":
                forwarded = args.args[1:] if args.args[:1] == ["--"] else args.args
                raise SystemExit(run_pi(args.binding, forwarded))
            run_bridge(args.binding, args.session)
        else:
            from .t3 import run_inbox, run_wake

            if args.t3_command == "wake":
                asyncio.run(run_wake(args.binding, once=args.once))
            else:
                run_inbox(args.binding)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, RuntimeError, ValueError) as error:
        print(f"waa: {error}", file=sys.stderr)
        raise SystemExit(1) from None
