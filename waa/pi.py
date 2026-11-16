"""Explicit, project-local Pi extension launcher and restricted Reason bridge."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from waa.mailbox import AgentMailbox, MailboxBinding, MailboxError, serve_bridge
from waa.reason import ReasonClient


class PiError(MailboxError):
    pass


@dataclass(frozen=True)
class PiBinding(MailboxBinding):
    @classmethod
    def load(cls, path: Path) -> PiBinding:
        return super().load(path, error_type=PiError, label="Pi")


class PiMailbox(AgentMailbox):
    def __init__(self, binding: PiBinding, session: str, reason: Any):
        super().__init__(binding, session, reason, adapter="pi", error_type=PiError)


def run_bridge(binding_path: Path, session: str) -> None:
    binding = PiBinding.load(binding_path)
    with ReasonClient(binding.reason_state) as reason:
        mailbox = PiMailbox(binding, session, reason)
        try:
            mailbox.register()
            serve_bridge(mailbox)
        finally:
            mailbox.close()


def run_pi(binding_path: Path, args: list[str] | None = None) -> int:
    binding = PiBinding.load(binding_path)
    executable = shutil.which("pi")
    if not executable:
        raise PiError("Pi must already be installed; no global install is performed")
    extension = Path(__file__).resolve().parent.parent / "integrations" / "pi" / "extension.mjs"
    env = os.environ.copy()
    env["WAA_PI_BINDING"] = str(binding.path)
    env["WAA_PI_BRIDGE_ARGV"] = json.dumps([sys.executable, "-m", "waa.pi", "bridge", "--binding", str(binding.path)])
    return subprocess.run([executable, "--extension", str(extension), *(args or [])],
                          cwd=binding.workspace, env=env).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["bridge"])
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--session", required=True)
    options = parser.parse_args()
    run_bridge(options.binding, options.session)
