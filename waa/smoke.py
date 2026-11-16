"""An isolated transport check; no model or installed agent is involved."""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from typing import Any, Callable
from uuid import uuid4

from .bootstrap import ensure_tools
from .reason import ReasonClient, ReasonError
from .runtime import isolated_env, ticket_relay


class SmokeFailure(RuntimeError):
    """A smoke assertion failed. Its text contains no upstream payloads."""


def _require(condition: bool, name: str, checks: dict[str, bool]) -> None:
    checks[name] = bool(condition)
    if not condition:
        raise SmokeFailure(name)


def _eventually(probe: Callable[[], Any], *, timeout: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise SmokeFailure("timed out waiting for the expected state")
        time.sleep(0.1)


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _relay_ready(process: subprocess.Popen[bytes], port: int) -> bool:
    if process.poll() is not None:
        raise SmokeFailure("the local relay exited")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class _Daemon:
    def __init__(self, binary: Path, state: Path, machine: str, stack: ExitStack,
                 rejected_proxy: str):
        self.binary = binary
        self.state = state
        self.stack = stack
        self.env = isolated_env(state, machine=machine)
        # A daemon without membership refreshes the default relay list at
        # startup. Fence that pre-membership request off from public services.
        # The explicit loopback relay bypasses these proxies.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self.env[name] = rejected_proxy
        self.env["NO_PROXY"] = self.env["no_proxy"] = "localhost,127.0.0.1,::1"
        self.process: subprocess.Popen[bytes] | None = None
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Disable optional integrations explicitly, even if defaults change.
        (state / "config.toml").write_text(
            "share_unmanaged = false\nlan = false\ntray = false\n"
            "reason = false\nauto_update = false\n",
            encoding="utf-8",
        )
        self.stack.callback(self.stop)

    def start(self) -> None:
        self.stop()
        output = self.stack.enter_context((self.state / "process.log").open("ab"))
        self.process = subprocess.Popen(
            [str(self.binary), "daemon"],
            env=self.env,
            cwd=self.state,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )

        def ready() -> bool:
            if self.process is None or self.process.poll() is not None:
                raise SmokeFailure("a synthetic daemon exited")
            try:
                with self.client() as client:
                    client.request("who", all=True)
                return True
            except (OSError, TimeoutError, ConnectionError):
                return False

        _eventually(ready)

    def after_membership_change(self) -> None:
        # CreateTeam/Join request a restart themselves. We own the replacement;
        # using CLI init/join here would silently launch a detached daemon.
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.stop()
        self.start()

    def client(self) -> ReasonClient:
        return ReasonClient(self.state, timeout=15)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                with ReasonClient(self.state, timeout=1) as client:
                    client.request("shutdown")
                self.process.wait(timeout=5)
            except (OSError, ReasonError, subprocess.TimeoutExpired):
                pass
        _stop(self.process)
        self.process = None


def _register(client: ReasonClient, key: str, cwd: Path, *, name: str = "terminal") -> str:
    registered = client.request(
        "register",
        session={
            "key": key,
            "agent": "cli",
            "agent_session_id": key.removeprefix("cli:"),
            "cwd": str(cwd),
            "tier": "pull",
            "name": name,
        },
    )
    return str(registered["address"])


def _messages(client: ReasonClient, key: str) -> list[dict[str, Any]]:
    return client.request("inbox", key=key, peek=True, wait_secs=0)["messages"]


def _stage(client: ReasonClient, key: str, message_id: str) -> str:
    receipts = client.request("delivery_status", **{"from": key, "ids": [message_id]})
    return str(receipts[0]["stage"])


def _send(client: ReasonClient, key: str, to: str, body: str, token: str,
          *, reply_to: str | None = None) -> dict[str, Any]:
    return client.request(
        "send", **{
            "from": key,
            "to": to,
            "body": body,
            "idempotency_key": token,
            "wake": False,
            "notify": False,
            "reply_to": reply_to,
        },
    )


def _only(messages: list[dict[str, Any]], message_id: str) -> bool:
    return len(messages) == 1 and messages[0]["id"] == message_id


def run_smoke() -> dict[str, Any]:
    """Run the pinned binaries with disposable, unconfigured synthetic sessions.

    The report deliberately excludes message content, invite tickets, endpoint
    credentials, raw process output and temporary filesystem paths.
    """
    tools = ensure_tools()
    checks: dict[str, bool] = {}
    report: dict[str, Any] = {
        "ok": False,
        "scope": "synthetic transport and mailbox recovery",
        "checks": checks,
        "versions": {},
        "real_agent_wake_tested": False,
        "model_execution_tested": False,
        "tailnet_tested": False,
        "tls_tested": False,
        "remote_handoff_receipt_available": False,
        "limitations": [
            "The pinned engine reports remote acceptance and correlated replies, "
            "but does not return recipient handoff acknowledgements to the sender."
        ],
    }
    phase = "versions"
    try:
        for name in ("reason-network", "iroh-relay"):
            version = subprocess.run(
                [str(tools[name]), "--version"], check=True, capture_output=True,
                text=True, timeout=5,
            ).stdout.strip()
            report["versions"][name] = version

        with tempfile.TemporaryDirectory(prefix="waa-", dir="/tmp") as directory, ExitStack() as stack:
            temporary = Path(directory).resolve()
            os.chmod(temporary, 0o700)
            port = _free_port()
            relay_config = temporary / "relay.toml"
            relay_config.write_text(
                f'http_bind_addr = "127.0.0.1:{port}"\n'
                'enable_quic_addr_discovery = false\nenable_metrics = false\naccess = "everyone"\n',
                encoding="utf-8",
            )
            phase = "relay_start"
            relay_log = stack.enter_context((temporary / "relay.log").open("wb"))
            relay = subprocess.Popen(
                [str(tools["iroh-relay"]), "--dev", "--config-path", str(relay_config)],
                stdin=subprocess.DEVNULL, stdout=relay_log, stderr=relay_log,
                cwd=temporary,
            )
            stack.callback(_stop, relay)
            _eventually(lambda: _relay_ready(relay, port))
            checks["loopback_relay_ready"] = True
            rejected_socket = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            rejected_socket.bind(("127.0.0.1", 0))
            rejected_proxy = f"http://127.0.0.1:{rejected_socket.getsockname()[1]}"
            alice = _Daemon(tools["reason-network"], temporary / "a", "smoke-a", stack, rejected_proxy)
            bob = _Daemon(tools["reason-network"], temporary / "b", "smoke-b", stack, rejected_proxy)
            alice.start()
            bob.start()
            phase = "membership"
            with alice.client() as client:
                client.request("create_team", name="synthetic-smoke", person="alice",
                               relay=f"http://127.0.0.1:{port}")
            alice.after_membership_change()
            with alice.client() as client:
                ticket = client.request("link", new=False)["ticket"]
            _require(ticket_relay(ticket) == f"http://127.0.0.1:{port}/",
                     "actual_invite_pins_own_relay", checks)
            with bob.client() as client:
                client.request("join", ticket=ticket, person="bob")
            del ticket
            bob.after_membership_change()
            checks["standalone_membership"] = True

            alice_key, bob_key = "cli:smoke-a", "cli:smoke-b"
            phase = "ping_and_receipts"
            with alice.client() as a, bob.client() as b:
                alice_address = _register(a, alice_key, temporary / "a")
                bob_address = _register(b, bob_key, temporary / "b")
                _eventually(lambda: any(
                    row.get("address") == bob_address
                    for row in a.request("who", all=True).get("sessions", [])
                ))
                payload, token = uuid4().hex, uuid4().hex
                sent = _send(a, alice_key, bob_address, payload, token)
                message_id = sent["ids"][0]
                inbox = _eventually(lambda: _messages(b, bob_key))
                _require(_only(inbox, message_id) and inbox[0]["body"] == payload,
                         "ping_arrived_exactly_once", checks)
                _eventually(lambda: _stage(a, alice_key, message_id) == "accepted")
                _require(_only(_messages(b, bob_key), message_id)
                         and _stage(a, alice_key, message_id) == "accepted",
                         "peek_does_not_acknowledge", checks)
                replay = _send(a, alice_key, bob_address, payload, token)
                _require(replay["ids"] == sent["ids"] and _only(_messages(b, bob_key), message_id),
                         "same_key_is_idempotent", checks)
                rejected = False
                try:
                    _send(a, alice_key, bob_address, payload + "changed", token)
                except ReasonError as exc:
                    rejected = "idempotency key already used for a different message" in str(exc)
                _require(rejected and _only(_messages(b, bob_key), message_id),
                         "changed_payload_is_rejected", checks)
                b.request("delivered", key=bob_key, ids=[message_id])
                _require(not _messages(b, bob_key), "delivered_ack_clears_unread", checks)
                _require(_stage(a, alice_key, message_id) == "accepted",
                         "remote_ack_does_not_overclaim_handoff", checks)
                reply = _send(b, bob_key, alice_address, uuid4().hex, uuid4().hex,
                              reply_to=message_id)
                response = _eventually(lambda: _messages(a, alice_key))
                _require(_only(response, reply["ids"][0])
                         and response[0].get("reply_to") == message_id,
                         "correlated_reply_arrived", checks)
                _eventually(lambda: _stage(a, alice_key, message_id) == "replied")
                checks["reply_receipt"] = True
                a.request("delivered", key=alice_key, ids=reply["ids"])

                # Handoff receipts are observable for a local recipient. The
                # pinned peer protocol does not transport this later stage.
                with alice.client() as local:
                    local_key = "cli:local-check"
                    local_address = _register(local, local_key, temporary / "a", name="local-check")
                    local_sent = _send(a, alice_key, local_address, uuid4().hex, uuid4().hex)
                    local_id = local_sent["ids"][0]
                    local_before = _stage(a, alice_key, local_id)
                    _require(_only(_messages(local, local_key), local_id)
                             and local_before in {"queued", "accepted"}
                             and _stage(a, alice_key, local_id) == local_before,
                             "local_peek_does_not_acknowledge", checks)
                    local.request("delivered", key=local_key, ids=[local_id])
                    _require(_stage(a, alice_key, local_id) == "handed_to_agent"
                             and not _messages(local, local_key),
                             "local_handoff_receipt", checks)

            phase = "offline_queue"
            bob.stop()
            with alice.client() as a:
                _register(a, alice_key, temporary / "a")
                _eventually(lambda: not any(
                    machine.get("person") == "bob" and machine.get("online")
                    for machine in a.request("team").get("machines", [])
                ))
                offline_payload, offline_token = uuid4().hex, uuid4().hex
                queued = _send(a, alice_key, bob_address, offline_payload, offline_token)
                queued_id = queued["ids"][0]
                _require(_stage(a, alice_key, queued_id) == "queued",
                         "offline_message_is_queued", checks)

            phase = "restart_recovery"
            alice.stop()
            alice.start()
            with alice.client() as a:
                _require(_register(a, alice_key, temporary / "a") == alice_address,
                         "sender_identity_survives_restart", checks)
                _require(_stage(a, alice_key, queued_id) == "queued",
                         "sender_queue_survives_restart", checks)
                replay = _send(a, alice_key, bob_address, offline_payload, offline_token)
                _require(replay["ids"] == queued["ids"],
                         "idempotency_survives_restart", checks)
                bob.start()
                with bob.client() as b:
                    _require(_register(b, bob_key, temporary / "b") == bob_address,
                             "recipient_identity_survives_restart", checks)
                    recovered = _eventually(lambda: _messages(b, bob_key), timeout=30)
                    _require(_only(recovered, queued_id) and recovered[0]["body"] == offline_payload,
                             "original_message_recovers_once", checks)
                    _eventually(lambda: _stage(a, alice_key, queued_id) == "accepted")
                    # Retry once more after acceptance: no second inbox copy.
                    _send(a, alice_key, bob_address, offline_payload, offline_token)
                    _require(_only(_messages(b, bob_key), queued_id),
                             "recovery_retry_does_not_duplicate", checks)
                    b.request("delivered", key=bob_key, ids=[queued_id])
                    _require(not _messages(b, bob_key)
                             and _stage(a, alice_key, queued_id) == "accepted",
                             "recovered_ack_clears_unread", checks)
            report["ok"] = bool(checks) and all(checks.values())
    except Exception as exc:
        # Upstream errors can quote a ticket, body or filesystem path. Keep the
        # report useful without serializing those exception messages or logs.
        report["failure"] = {"phase": phase, "kind": type(exc).__name__}
        if isinstance(exc, SmokeFailure):
            report["failure"]["check"] = str(exc)
    return report
