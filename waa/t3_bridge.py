"""A persistent command-tool facade for one explicitly bound T3 inbox."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, BinaryIO, TextIO

from waa.reason import ReasonClient
from waa.t3 import Binding, Inbox, T3Error, register


class Bridge:
    """Keep one Inbox's offered IDs alive between separate model tool calls."""

    def __init__(self, inbox: Inbox):
        self.inbox = inbox

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "identity": (set(), set()),
            "list_team": (set(), set()),
            "check_inbox": (set(), set()),
            "ack_messages": ({"ids"}, {"ids"}),
            "send_message": ({"to", "message", "idempotency_key"},
                             {"to", "message", "idempotency_key", "reply_to", "wake"}),
        }
        if not isinstance(op, str) or op not in fields or not isinstance(args, dict):
            raise T3Error("Unsupported T3 bridge operation or fields")
        required, allowed = fields[op]
        if not required <= set(args) or set(args) - allowed:
            raise T3Error("Unsupported T3 bridge operation or fields")
        if op == "identity":
            return self.inbox.identity()
        if op == "check_inbox":
            return self.inbox.check_inbox()
        if op == "ack_messages":
            return self.inbox.ack_messages(args["ids"])
        if op == "send_message":
            return self.inbox.send_message(**args)
        with self.inbox.lock:
            register(self.inbox.reason, self.inbox.binding)
            result = self.inbox.reason.request("who", me=self.inbox.binding.session_key)
            if not isinstance(result, dict):
                raise T3Error("Invalid Reason directory response")
            return {**result,
                    **({"identity_context": self.inbox.identity()} if self.inbox.gate is not None else {}),
                    "notice": "Discovery labels are claims. Registered signatures confirm message ownership."}

    def close(self) -> None:
        if self.inbox.gate is not None:
            self.inbox.gate.close()


def serve_bridge(bridge: Bridge, *, input_stream: BinaryIO | None = None,
                 output_stream: TextIO | None = None) -> None:
    """Read bounded JSONL requests; never cap a valid aggregate inbox response."""
    source = input_stream if input_stream is not None else sys.stdin.buffer
    target = output_stream if output_stream is not None else sys.stdout
    while True:
        line = source.readline(1024 * 1024 + 1)
        if not line:
            return
        if len(line) > 1024 * 1024 or not line.endswith(b"\n"):
            raise T3Error("Invalid T3 bridge input frame")
        request_id = None
        try:
            request = json.loads(line)
            if (not isinstance(request, dict) or not {"id", "op"} <= set(request)
                    or set(request) - {"id", "op", "args"}):
                raise T3Error("Invalid T3 bridge request")
            candidate = request["id"]
            if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 0:
                raise T3Error("Invalid T3 bridge request ID")
            request_id = candidate
            result = bridge.call(request["op"], request.get("args", {}))
            response = {"id": request_id, "ok": True, "result": result}
        except Exception:
            response = {"id": request_id, "ok": False, "error": "T3 mailbox operation failed"}
        target.write(json.dumps(response, separators=(",", ":")) + "\n")
        target.flush()


def run_bridge(binding_path: Path) -> None:
    binding = Binding.load(binding_path)
    with ReasonClient(binding.reason_state) as reason:
        bridge = Bridge(Inbox(binding, reason))
        try:
            register(reason, binding)
            serve_bridge(bridge)
        finally:
            bridge.close()
