"""The pinned engine's same-user Unix socket protocol; no network listener."""

from __future__ import annotations

import json
from pathlib import Path
import socket
from typing import Any


class ReasonError(RuntimeError):
    pass


class ReasonDisconnectedError(ReasonError):
    """The IPC connection closed before a complete response arrived."""


class ReasonClient:
    def __init__(self, state: Path, timeout: float = 15):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect(str(state / "network.sock"))
        except BaseException:
            self.socket.close()
            raise
        self.reader = self.socket.makefile("rb")
        self.sequence = 0

    def __enter__(self) -> ReasonClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.reader.close()
        self.socket.close()

    def request(self, op: str, **fields: Any) -> Any:
        self.sequence += 1
        request = {**fields, "id": self.sequence, "op": op}
        self.socket.sendall(json.dumps(request).encode() + b"\n")
        while True:
            line = self.reader.readline()
            if not line or not line.endswith(b"\n"):
                raise ReasonDisconnectedError("Engine closed the connection before a complete response.")
            response = json.loads(line)
            if response.get("id") != self.sequence:
                if response.get("event") == "deliver":
                    continue  # Pull clients acknowledge only after explicitly reading their inbox.
                raise ReasonError("Unexpected engine response id.")
            if response.get("ok") is not True:
                raise ReasonError(response.get("error", "Engine refused the request."))
            return response.get("result")
