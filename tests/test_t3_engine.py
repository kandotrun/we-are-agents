"""Regressions against the pinned engine, with no installed agent or T3 server."""

import asyncio
from contextlib import ExitStack
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from waa.bootstrap import ensure_tools
from waa.reason import ReasonError
from waa.smoke import _Daemon, _eventually, _free_port, _register, _relay_ready, _send, _stop
from waa.t3 import Binding, Inbox, Journal, register, wake_once
from tests.test_t3 import FakeT3


class PinnedEngineRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = ensure_tools()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="waa-reg-", dir="/tmp"))).resolve()
        rejected = self.stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        rejected.bind(("127.0.0.1", 0))
        rejected_proxy = f"http://127.0.0.1:{rejected.getsockname()[1]}"
        port = _free_port()
        config = self.root / "relay.toml"
        config.write_text(f'http_bind_addr="127.0.0.1:{port}"\nenable_metrics=false\nenable_quic_addr_discovery=false\naccess="everyone"\n')
        output = self.stack.enter_context((self.root / "relay.log").open("wb"))
        process = subprocess.Popen([str(self.tools["iroh-relay"]), "--dev", "--config-path", str(config)],
                                   stdout=output, stderr=output, stdin=subprocess.DEVNULL)
        self.stack.callback(_stop, process)
        _eventually(lambda: _relay_ready(process, port))
        relay = f"http://127.0.0.1:{port}"
        self.daemon = _Daemon(self.tools["reason-network"], self.root / "state", "regression", self.stack, rejected_proxy)
        self.daemon.start()
        with self.daemon.client() as client:
            client.request("create_team", name="regression", person="alice", relay=relay)
        self.daemon.after_membership_change()
        self.binding = Binding(self.root / "binding.json", self.daemon.state, "test-env", "test-project",
                               "test-thread", self.root, "http://127.0.0.1/mcp", self.root / "unused-token")
        self.observer = self.stack.enter_context(self.daemon.client())

    def disappeared(self):
        try:
            self.observer.request("inbox", key=self.binding.session_key, peek=True, wait_secs=0)
            return False
        except ReasonError as error:
            if "isn't registered" not in str(error):
                raise
            return True

    def owner_closed(self, owner):
        owner.close()
        _eventually(self.disappeared, timeout=5)

    def test_inbox_survives_wake_once_connection_closing(self):
        with self.daemon.client() as reader:
            register(reader, self.binding)
            inbox = Inbox(self.binding, reader)
            self.assertEqual(inbox.check_inbox()["message_ids"], [])
            waker = self.daemon.client()
            register(waker, self.binding)
            self.owner_closed(waker)
            self.assertEqual(inbox.check_inbox()["message_ids"], [])

    def test_wake_poll_survives_inbox_connection_closing(self):
        with self.daemon.client() as waker:
            register(waker, self.binding)
            reader = self.daemon.client()
            register(reader, self.binding)
            self.owner_closed(reader)
            journal = Journal(self.binding)
            self.addCleanup(journal.close)
            self.assertEqual(asyncio.run(wake_once(self.binding, waker, FakeT3(), journal)), "idle")

    def test_ack_refreshes_registration_after_other_owner_closes(self):
        with self.daemon.client() as reader, self.daemon.client() as sender:
            register(reader, self.binding)
            _register(sender, "cli:sender", self.root, name="sender")
            address = next(row["address"] for row in sender.request("who", all=True)["sessions"]
                           if row["agent"] == "t3")
            _send(sender, "cli:sender", address, "synthetic message", "one")
            inbox = Inbox(self.binding, reader)
            ids = inbox.check_inbox()["message_ids"]
            other = self.daemon.client()
            register(other, self.binding)
            self.owner_closed(other)
            self.assertEqual(inbox.ack_messages(ids)["acknowledged_ids"], ids)
            self.assertEqual(inbox.check_inbox()["message_ids"], [])

    def test_large_valid_backlog_can_be_read_and_partially_acknowledged(self):
        with self.daemon.client() as reader, self.daemon.client() as sender, self.daemon.client() as second:
            register(reader, self.binding)
            _register(sender, "cli:sender", self.root, name="sender")
            _register(second, "cli:second", self.root, name="second")
            address = next(row["address"] for row in sender.request("who", all=True)["sessions"]
                           if row["agent"] == "t3")
            for number in range(16):
                connection, key = (sender, "cli:sender") if number < 8 else (second, "cli:second")
                _send(connection, key, address, "x" * 65536, f"backlog-{number}")
            inbox = Inbox(self.binding, reader)
            offered = inbox.check_inbox()
            self.assertEqual(len(offered["message_ids"]), 16)
            self.assertGreater(len(offered["framed_messages"].encode()), 1024 * 1024)
            inbox.ack_messages(offered["message_ids"][:8])
            remaining = inbox.check_inbox()["message_ids"]
            self.assertEqual(set(remaining), set(offered["message_ids"][8:]))
            inbox.ack_messages(remaining)
            self.assertEqual(inbox.check_inbox()["message_ids"], [])
