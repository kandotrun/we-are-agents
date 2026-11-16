"""Synthetic native IPC/hook recovery; no installed agent or model is involved.

Both independently signed mailboxes share one owned daemon. This is not
two-machine network or tailnet acceptance.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from waa.bootstrap import ensure_tools
from waa.identity import initialize_identity
from waa.mailbox import AgentMailbox, MailboxBinding
from waa.openclaw import run_wake
from waa.reason import ReasonClient
from waa.smoke import _Daemon, _eventually, _free_port, _relay_ready, _stop


SESSION = "agent:waa-recovery:main"


class OpenClawRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_native_wake_recovers_without_replaying_or_acknowledging(self) -> None:
        tools = await asyncio.to_thread(ensure_tools)
        with tempfile.TemporaryDirectory(prefix="waa-wake-recovery-", dir="/tmp") as temporary, ExitStack() as stack:
            root = Path(temporary)
            initialize_identity(root / "receiver", owner_id="bob", owner_name="Bob",
                                agent_name="Synthetic receiver")
            registry_path = root / "receiver/team.json"
            source_identity = initialize_identity(
                root / "source", owner_id="alice", owner_name="Alice", agent_name="Synthetic source",
                registry_file=registry_path,
            )
            registry = json.loads(registry_path.read_text())
            registry["agents"][source_identity["public_key"]] = {
                name: source_identity[name] for name in ("owner_id", "owner_name", "agent_name")
            }
            registry_path.write_text(json.dumps(registry))
            state = root / "reason"

            def mailbox_binding(name: str) -> MailboxBinding:
                workspace = root / (name + "-workspace")
                workspace.mkdir(mode=0o700)
                path = root / (name + "-mailbox.json")
                path.write_text(json.dumps({
                    "reason_state": str(state), "workspace": str(workspace),
                    "identity": str(root / name / "profile.json"),
                }))
                return MailboxBinding.load(path)

            receiver_binding, source_binding = mailbox_binding("receiver"), mailbox_binding("source")
            rejected = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            rejected.bind(("127.0.0.1", 0))
            rejected_proxy = f"http://127.0.0.1:{rejected.getsockname()[1]}"
            relay_port = _free_port()
            relay_config = root / "relay.toml"
            relay_config.write_text(
                f'http_bind_addr="127.0.0.1:{relay_port}"\n'
                'enable_metrics=false\nenable_quic_addr_discovery=false\naccess="everyone"\n'
            )
            relay_log = stack.enter_context((root / "relay.log").open("wb"))
            relay = subprocess.Popen(
                [str(tools["iroh-relay"]), "--dev", "--config-path", str(relay_config)],
                stdin=subprocess.DEVNULL, stdout=relay_log, stderr=relay_log, cwd=root,
            )
            stack.callback(_stop, relay)
            await asyncio.to_thread(_eventually, lambda: _relay_ready(relay, relay_port))
            daemon = _Daemon(tools["reason-network"], state, "wake-recovery", stack, rejected_proxy)
            await asyncio.to_thread(daemon.start)
            def create_team() -> None:
                with daemon.client() as client:
                    client.request("create_team", name="synthetic-wake-recovery", person="bob",
                                   relay=f"http://127.0.0.1:{relay_port}")

            await asyncio.to_thread(create_team)
            await asyncio.to_thread(daemon.after_membership_change)

            # Resolve the stable destination before the persistent sidecar owns
            # its own connection. Neither this reader nor the sidecar ACKs mail.
            with daemon.client() as client:
                receiver = AgentMailbox(receiver_binding, SESSION, client)
                try:
                    recipient = receiver.register()
                    receiver_key = receiver.key
                finally:
                    receiver.close()
            self.assertIsInstance(recipient, str)

            def source_mailbox() -> AgentMailbox:
                client = stack.enter_context(daemon.client())
                mailbox = AgentMailbox(source_binding, "agent:synthetic-source:main", client)
                stack.callback(mailbox.close)
                mailbox.register()
                return mailbox

            source = source_mailbox()
            source_address = source.address

            def send(mailbox: AgentMailbox, name: str) -> str:
                result = mailbox.call("send_message", {
                    "to": recipient, "message": "synthetic recovery " + name,
                    "wake": True, "idempotency_key": "recovery-" + name,
                })
                self.assertEqual(len(result["ids"]), 1)
                return result["ids"][0]

            healthy = threading.Event()
            requests: list[list[str]] = []
            handler_errors: list[str] = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_POST(handler):
                    try:
                        payload = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                        if (handler.path != "/hooks/wake"
                                or handler.headers.get("Authorization") != "Bearer synthetic-recovery-hook"
                                or payload["agentId"] != "waa-recovery" or payload["mode"] != "now"):
                            raise ValueError("Unexpected synthetic hook request")
                        ids = json.loads(payload["text"].split("Notification IDs: ", 1)[1])
                        if not isinstance(ids, list) or not ids or "synthetic recovery" in payload["text"]:
                            raise ValueError("Hook must receive only an ID pointer")
                        requests.append(ids)
                        body = json.dumps({"ok": True, "mode": "now", "eventOutcome": "queued"}).encode()
                        handler.send_response(200 if healthy.is_set() else 503)
                        handler.send_header("Content-Type", "application/json")
                        handler.send_header("Content-Length", str(len(body)))
                        handler.end_headers()
                        handler.wfile.write(body)
                    except Exception as error:
                        handler_errors.append(type(error).__name__)
                        handler.send_error(500)

            hook = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            hook_thread = threading.Thread(target=hook.serve_forever, daemon=True)
            hook_thread.start()
            stack.callback(hook_thread.join, 2)
            stack.callback(hook.server_close)
            stack.callback(hook.shutdown)
            token_path = root / "hook.token"
            token_path.write_text("synthetic-recovery-hook")
            token_path.chmod(0o600)
            wake_path = root / "wake.json"
            wake_path.write_text(json.dumps({
                "mailbox_binding": str(receiver_binding.path), "session": SESSION,
                "gateway_url": f"http://127.0.0.1:{hook.server_port}/hooks/wake",
                "token_file": str(token_path), "agent_id": "waa-recovery",
            }))
            journal_path = wake_path.with_suffix(".sqlite3")

            def journal_rows() -> dict[str, tuple[str, str | None]]:
                if not journal_path.exists():
                    return {}
                with closing(sqlite3.connect(journal_path)) as db:
                    return {message_id: (status, receipt)
                            for status, encoded, receipt in db.execute("SELECT status,message_ids,receipt FROM wakes")
                            for message_id in json.loads(encoded)}

            attempts: list[None] = []
            connections: list[ReasonClient] = []

            def native_client(*args, **kwargs):
                # This spy still opens the real native Unix socket. Observing a
                # reconnect attempt while stopped makes the outage deterministic.
                attempts.append(None)
                client = ReasonClient(*args, **kwargs)
                connections.append(client)
                return client

            failed_id = send(source, "failed")
            with (patch("waa.openclaw._POLL_INTERVAL", 0.02),
                  patch("waa.openclaw._RETRY_INITIAL", 0.02),
                  patch("waa.openclaw._RETRY_MAX", 0.04),
                  patch("waa.openclaw.ReasonClient", new=native_client)):
                wake = asyncio.create_task(run_wake(wake_path))

                async def eventually(probe, label: str) -> None:
                    deadline = asyncio.get_running_loop().time() + 8
                    while not probe():
                        if wake.done():
                            await wake
                            self.fail("Persistent wake unexpectedly returned")
                        self.assertLess(asyncio.get_running_loop().time(), deadline, label)
                        await asyncio.sleep(0.01)

                try:
                    await eventually(lambda: journal_rows().get(failed_id) == ("unknown", None),
                                     "HTTP503 must hold the first admission as unknown")
                    self.assertFalse(wake.done())
                    self.assertEqual(requests, [[failed_id]])
                    healthy.set()
                    healthy_id = send(source, "healthy")
                    await eventually(lambda: journal_rows().get(healthy_id, (None,))[0] == "accepted",
                                     "The same wake task must notify a fresh ID after HTTP recovery")
                    self.assertEqual(requests, [[failed_id], [healthy_id]])

                    source.close()
                    source.reason.close()
                    await asyncio.to_thread(daemon.stop)
                    attempts_before_outage = len(attempts)
                    await eventually(lambda: len(attempts) > attempts_before_outage,
                                     "Wake must observe the actual daemon socket outage")
                    connected_before_restart = len(connections)
                    await asyncio.to_thread(daemon.start)
                    source = source_mailbox()
                    self.assertEqual(source.address, source_address)
                    recovered_id = send(source, "restarted")
                    await eventually(lambda: journal_rows().get(recovered_id, (None,))[0] == "accepted",
                                     "A fresh native connection must resume wake delivery after restart")
                    self.assertGreater(len(connections), connected_before_restart)
                    # Leave multiple polls to expose a repeated notification.
                    await asyncio.sleep(0.12)
                    self.assertFalse(wake.done())
                    self.assertEqual(requests, [[failed_id], [healthy_id], [recovered_id]])
                    self.assertEqual(handler_errors, [])
                finally:
                    wake.cancel()
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait_for(wake, timeout=2)

            rows = journal_rows()
            self.assertTrue(all(client.socket.fileno() == -1 for client in connections),
                            "Cancellation must close every sidecar native connection")
            self.assertEqual(set(rows), {failed_id, healthy_id, recovered_id})
            self.assertEqual(rows[failed_id], ("unknown", None))
            for message_id in (healthy_id, recovered_id):
                self.assertEqual(rows[message_id][0], "accepted")
                self.assertEqual(json.loads(rows[message_id][1]), {"mode": "now", "eventOutcome": "queued"})
            with closing(sqlite3.connect(journal_path)) as db:
                self.assertEqual(dict(db.execute(
                    "SELECT message_id,status FROM wake_messages JOIN wakes USING(operation_id)"
                )), {failed_id: "unknown", healthy_id: "accepted", recovered_id: "accepted"})
            with daemon.client() as client:
                receiver = AgentMailbox(receiver_binding, SESSION, client)
                try:
                    self.assertEqual(receiver.register(), recipient)
                    unread = client.request("inbox", key=receiver_key, peek=True, wait_secs=0)["messages"]
                    self.assertEqual(len(unread), 3)
                    self.assertEqual({row["id"] for row in unread}, {failed_id, healthy_id, recovered_id})
                    self.assertEqual(receiver.offered, set())
                finally:
                    receiver.close()


if __name__ == "__main__":
    unittest.main()
