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
from waa.t3 import Binding, Inbox, Journal, T3Error, register, wake_once
from waa.pi import PiBinding, PiMailbox
from tests.test_t3 import FakeT3, Fixture


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

    def test_signed_identity_filters_real_mail_and_sends_a_correlated_signed_reply(self):
        Fixture.signed_binding(self)
        with self.daemon.client() as reader, self.daemon.client() as sender:
            address = register(reader, self.binding)
            peer_address = _register(sender, "cli:signed-peer", self.root, name="signed-peer")
            signed = self.peer_gate.sign("A signed development request", sender=peer_address, recipient=address,
                                         reply_to=None, wake=True, idempotency_key="signed-request")
            incoming = sender.request("send", **{"from": "cli:signed-peer"}, to=address, body=signed,
                                      wake=True, notify=False, files=[], idempotency_key="signed-request")
            unsigned = _send(sender, "cli:signed-peer", address, "UNSIGNED PRIVATE BODY", "unsigned")
            inbox = Inbox(self.binding, reader)
            offered = inbox.check_inbox()
            self.assertEqual(offered["message_ids"], incoming["ids"])
            self.assertEqual(offered["messages"][0]["verified_identity"]["owner_name"], "Bob")
            self.assertNotIn("UNSIGNED PRIVATE BODY", str(offered))
            self.assertEqual(offered["quarantined"][0]["id"], unsigned["ids"][0])
            with self.assertRaises(T3Error):
                inbox.ack_messages(unsigned["ids"])
            reply = inbox.send_message(peer_address, "A signed response", "signed-reply",
                                       reply_to=incoming["ids"][0], wake=False)
            repeat = inbox.send_message(peer_address, "A signed response", "signed-reply",
                                        reply_to=incoming["ids"][0], wake=False)
            self.assertEqual(reply["message_ids"], repeat["message_ids"])
            peer_mail = sender.request("inbox", key="cli:signed-peer", peek=True, wait_secs=0)
            verified = self.peer_gate.filter_inbox(peer_mail, recipient=peer_address)
            self.assertEqual(verified["messages"][0]["body"], "A signed response")
            self.assertEqual(verified["messages"][0]["reply_to"], incoming["ids"][0])
            self.assertEqual(verified["messages"][0]["verified_identity"]["owner_name"], "Alice")
            inbox.ack_messages(offered["message_ids"])
            self.assertEqual(inbox.check_inbox()["message_ids"], [])
            pending = reader.request("inbox", key=self.binding.session_key, peek=True, wait_secs=0)["messages"]
            self.assertEqual([message["id"] for message in pending], unsigned["ids"])

    def test_signed_machine_qualified_delivery_read_and_ack_in_both_adapters(self):
        Fixture.signed_binding(self)
        with self.daemon.client() as reader, self.daemon.client() as sender:
            address = register(reader, self.binding)
            pi_binding = PiBinding(self.root / "pi.json", self.daemon.state, self.root, self.peer_gate.path)
            pi = PiMailbox(pi_binding, "qualified-pi", sender)
            peer_address = pi.register()
            incoming = pi.call("send_message", {"to": address + "@regression", "message": "qualified request",
                                                "idempotency_key": "qualified-request"})
            inbox = Inbox(self.binding, reader)
            offered = inbox.check_inbox()
            self.assertEqual(offered["message_ids"], incoming["ids"])
            self.assertEqual(offered["messages"][0]["body"], "qualified request")
            inbox.ack_messages(offered["message_ids"])
            self.assertEqual(inbox.check_inbox()["message_ids"], [])
            reply = inbox.send_message(peer_address + "@regression", "qualified response", "qualified-reply",
                                       reply_to=incoming["ids"][0], wake=False)
            response = pi.call("check_inbox", {})
            self.assertEqual(response["message_ids"], reply["message_ids"])
            self.assertEqual(response["messages"][0]["body"], "qualified response")
            pi.call("ack_messages", {"ids": response["message_ids"]})
            self.assertEqual(pi.call("check_inbox", {})["message_ids"], [])

    def test_independent_pi_sessions_can_reuse_a_profile_and_tool_call_id(self):
        Fixture.signed_binding(self)
        with self.daemon.client() as reader, self.daemon.client() as first, self.daemon.client() as second:
            address = register(reader, self.binding)
            binding = PiBinding(self.root / "pi.json", self.daemon.state, self.root, self.peer_gate.path)
            one, two = PiMailbox(binding, "conversation-one", first), PiMailbox(binding, "conversation-two", second)
            self.assertNotEqual(one.register(), two.register())
            args = {"to": address, "message": "independent request", "idempotency_key": "call_1"}
            sent_one, sent_two = one.call("send_message", args), two.call("send_message", args)
            self.assertNotEqual(sent_one["ids"], sent_two["ids"])
            raw = reader.request("inbox", key=self.binding.session_key, peek=True, wait_secs=0)
            original = next(m["body"] for m in raw["messages"] if m["id"] == sent_one["ids"][0])
            inbox = Inbox(self.binding, reader)
            offered = inbox.check_inbox()
            self.assertEqual(set(offered["message_ids"]), set(sent_one["ids"] + sent_two["ids"]))
            inbox.ack_messages(offered["message_ids"])
            one.register()
            replay = first.request("send", **{"from": one.key}, to=address, body=original, wake=True,
                                   notify=False, files=[], idempotency_key="copied-under-new-id")
            reloaded = Inbox(self.binding, reader).check_inbox()
            self.assertEqual(reloaded["message_ids"], [])
            self.assertEqual(reloaded["quarantined"], [{"id": replay["ids"][0], "reason": "replayed_message"}])
