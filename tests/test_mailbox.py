from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from waa.identity import IdentityGate, PREFIX, initialize_identity
from waa.mailbox import AgentMailbox, MailboxBinding, MailboxError, create_mcp_server, serve_bridge
from waa.pi import PiBinding, PiError, PiMailbox


class FakeReason:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.text = "REJECTED ENGINE AGGREGATE"
        self.fail_delivery = False

    def request(self, op, **fields):
        self.calls.append((op, fields))
        if op == "register":
            session = fields["session"]
            return {"address": f"fukumoto/{session['agent']}-{session['agent_session_id']}"}
        if op == "inbox":
            return {"messages": self.messages, "text": self.text}
        if op == "who":
            return {"machine": "workstation", "sessions": []}
        if op == "delivered" and self.fail_delivery:
            raise RuntimeError("PRIVATE TRANSPORT ERROR")
        return {"ok": True}


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="waa-mailbox-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.own_dir, self.peer_dir = self.directory / "own", self.directory / "peer"
        own_info = initialize_identity(self.own_dir, owner_id="fukumoto", owner_name="Fukumoto",
                                       agent_name="OpenClaw")
        self.registry_path = self.own_dir / "team.json"
        self.peer_info = initialize_identity(self.peer_dir, owner_id="kan", owner_name="Kan",
                                            agent_name="T3 Code", registry_file=self.registry_path)
        registry = json.loads(self.registry_path.read_text())
        registry["agents"][self.peer_info["public_key"]] = {
            key: self.peer_info[key] for key in ("owner_id", "owner_name", "agent_name")}
        self.registry_path.write_text(json.dumps(registry))
        self.profile = self.own_dir / "profile.json"
        self.peer = IdentityGate.from_file(self.peer_dir / "profile.json")
        self.addCleanup(self.peer.close)
        self.path = self.directory / "mailbox.json"
        self.path.write_text(json.dumps({"reason_state": "reason", "workspace": ".",
                                        "identity": "own/profile.json"}))
        self.binding = MailboxBinding.load(self.path)
        self.own_info = own_info

    def mailbox(self, session="main", *, reason=None, binding=None, adapter="openclaw"):
        result = AgentMailbox(binding or self.binding, session, reason or FakeReason(), adapter=adapter)
        self.addCleanup(result.close)
        return result

    def message(self, body="Verified original body", *, message_id="message-1", nonce=None,
                recipient="fukumoto/openclaw-main", wake=True, reply_to=None, signer=None):
        return {"id": message_id, "wake": wake, "reply_to": reply_to,
                "body": (signer or self.peer).sign(body, sender="kan/t3-thread", recipient=recipient,
                                                    reply_to=reply_to, wake=wake,
                                                    idempotency_key=nonce or message_id)}

    def bridge(self, mailbox, requests):
        output = io.StringIO()
        incoming = SimpleNamespace(buffer=io.BytesIO(requests))
        with patch("waa.mailbox.sys.stdin", incoming), patch("waa.mailbox.sys.stdout", output):
            serve_bridge(mailbox)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_relative_binding_and_pi_load_errors_remain_compatible(self):
        self.assertEqual(self.binding.reason_state, self.directory / "reason")
        self.assertEqual(self.binding.workspace, self.directory)
        self.assertEqual(self.binding.identity, self.profile)
        legacy = PiBinding.load(self.path)
        self.assertIsInstance(legacy, PiBinding)
        self.assertEqual(legacy.reason_state, self.binding.reason_state)
        self.path.write_text('{"workspace":"."}')
        with self.assertRaisesRegex(PiError, "Pi binding requires"):
            PiBinding.load(self.path)
        with self.assertRaisesRegex(MailboxError, "Agent binding requires"):
            MailboxBinding.load(self.path)

    def test_pi_persisted_key_and_signed_idempotency_golden_bytes(self):
        # These constants were captured from the original Pi implementation.
        # Default json.dumps whitespace is part of its persisted mailbox key.
        binding = PiBinding(self.path, Path("/tmp/waa-compat-state"),
                            Path("/tmp/waa-compat-workspace"), self.profile)
        reason = FakeReason()
        pi = PiMailbox(binding, "conversation-1", reason)
        self.addCleanup(pi.close)
        self.assertEqual(pi.key, "pi:d257912fed81296ee8a79018e04f2d25b6a5ab6580dbbfd1d0a24900f2dce10a")
        args = {"to": "kan/t3-thread", "message": "Exact original Pi send", "idempotency_key": "call_1"}
        pi.call("send_message", args)
        sent = reason.calls[-1][1]
        logical = "waa-pi-4cf6ff08a7c79aea52d2d208335bb33a6a2e6f52b58ad5908efab083f290981b"
        self.assertEqual(sent["idempotency_key"], logical)
        self.assertEqual(sent["body"], pi.identity.sign(args["message"], sender="fukumoto/pi-conversation-1",
                                                       recipient=args["to"], reply_to=None, wake=True,
                                                       idempotency_key=logical))
        self.assertEqual(sent["from"], pi.key)

    def test_adapter_is_app_bound_and_openclaw_requires_signing(self):
        with self.assertRaisesRegex(MailboxError, "signed identity"):
            self.mailbox(binding=replace(self.binding, identity=None))
        with self.assertRaisesRegex(MailboxError, "Unsupported local"):
            self.mailbox(adapter="pretend-codex")
        mailbox = self.mailbox()
        mailbox.register()
        registration = mailbox.reason.calls[-1][1]["session"]
        self.assertEqual(registration["agent"], "openclaw")
        self.assertEqual(registration["agent_session_id"], "main")
        self.assertEqual(registration["cwd"], str(self.directory))
        pi = self.mailbox(adapter="pi")
        self.assertEqual(mailbox.key.split(":", 1)[1], pi.key.split(":", 1)[1])
        self.assertNotEqual(mailbox.key, pi.key)
        self.assertEqual(mailbox.key, self.mailbox().key)
        for fields in ({"from": "other"}, {"adapter": "pi"}, {"session": "other"}, {"cwd": "/other"}):
            with self.subTest(fields=fields), self.assertRaises(MailboxError):
                mailbox.call("send_message", {"to": "kan/t3-thread", "message": "x",
                                               "idempotency_key": "call_1", **fields})
        for op in ("shutdown", "create_team", "register"):
            with self.subTest(op=op), self.assertRaises(MailboxError):
                mailbox.call(op, {})

    def test_same_profile_call_id_in_two_conversations_is_independent(self):
        args = {"to": "kan/t3-thread", "message": "Independent request", "idempotency_key": "call_1"}
        first, second = self.mailbox("conversation-1"), self.mailbox("conversation-2")
        first.call("send_message", args)
        a = first.reason.calls[-1][1]
        first.call("send_message", args)
        self.assertEqual(first.reason.calls[-1][1]["body"], a["body"])
        restarted = self.mailbox("conversation-1")
        restarted.call("send_message", args)
        self.assertEqual(restarted.reason.calls[-1][1]["body"], a["body"])
        second.call("send_message", args)
        b = second.reason.calls[-1][1]
        self.assertTrue(a["idempotency_key"].startswith("waa-openclaw-"))
        self.assertNotEqual(a["idempotency_key"], b["idempotency_key"])
        read = self.peer.filter_inbox({"messages": [{"id": "first", "body": a["body"], "wake": True},
                                                   {"id": "second", "body": b["body"], "wake": True}],
                                       "text": "unused"}, recipient="kan/t3-thread")
        self.assertEqual([row["id"] for row in read["messages"]], ["first", "second"])
        self.assertEqual(read["quarantined"], [])
        copied = self.peer.filter_inbox({"messages": [{"id": "copied", "body": a["body"], "wake": True}],
                                         "text": "unused"}, recipient="kan/t3-thread")
        self.assertEqual(copied["quarantined"], [{"id": "copied", "reason": "replayed_message"}])

    def test_signature_gate_excludes_unknown_tampered_wrong_and_fyi_wakes(self):
        outsider_dir = self.directory / "outsider"
        initialize_identity(outsider_dir, owner_id="outsider", owner_name="Outsider", agent_name="Unknown")
        outsider_registry = outsider_dir / "team.json"
        data = json.loads(outsider_registry.read_text())
        data["network_id"] = self.own_info["network_id"]
        outsider_registry.write_text(json.dumps(data))
        outsider = IdentityGate.from_file(outsider_dir / "profile.json")
        self.addCleanup(outsider.close)
        tampered = self.message("TAMPERED PRIVATE BODY", message_id="tampered")
        decoded = json.loads(tampered["body"][len(PREFIX):])
        decoded["payload"]["body"] = "CHANGED PRIVATE BODY"
        tampered["body"] = PREFIX + json.dumps(decoded)
        reason = FakeReason()
        reason.messages = [{"id": "unsigned", "wake": True, "body": "UNSIGNED PRIVATE BODY"},
                           self.message("UNKNOWN PRIVATE BODY", message_id="unknown", signer=outsider),
                           tampered,
                           self.message("WRONG PRIVATE BODY", message_id="wrong", recipient="someone/else"),
                           self.message("WRONG MACHINE BODY", message_id="wrong-machine",
                                        recipient="fukumoto/openclaw-main@other"),
                           self.message("Verified FYI", message_id="fyi", wake=False),
                           self.message("原文\nVerified qualified question", message_id="good",
                                        recipient="fukumoto/openclaw-main@workstation")]
        mailbox = self.mailbox(reason=reason)
        polled = mailbox.call("poll", {})
        self.assertEqual(polled["wake_ids"], ["good"])
        self.assertEqual(mailbox.offered, set())
        self.assertEqual({row["reason"] for row in polled["quarantined"]},
                         {"unsigned_message", "unknown_identity", "invalid_signature", "wrong_recipient"})
        self.assertNotIn("PRIVATE BODY", json.dumps(polled))
        self.assertNotIn("Verified qualified question", json.dumps(polled))
        read = mailbox.call("check_inbox", {})
        self.assertEqual(read["message_ids"], ["fyi", "good"])
        self.assertEqual([row["body"] for row in read["messages"]],
                         ["Verified FYI", "原文\nVerified qualified question"])
        self.assertEqual(read["messages"][1]["verified_identity"]["owner_id"], "kan")
        self.assertNotIn(reason.text, json.dumps(read))
        self.assertNotIn("PRIVATE BODY", json.dumps(read))
        self.assertNotIn("delivered", [op for op, _ in reason.calls])
        with self.assertRaises(MailboxError):
            mailbox.call("ack_messages", {"ids": ["unknown"]})
        self.assertEqual(mailbox.call("ack_messages", {"ids": ["good"]})["acknowledged_ids"], ["good"])

    def test_ack_requires_read_in_this_process_and_retry_survives_delivery_failure(self):
        reason = FakeReason()
        reason.messages = [self.message()]
        mailbox = self.mailbox(reason=reason)
        mailbox.call("poll", {})
        with self.assertRaises(MailboxError):
            mailbox.call("ack_messages", {"ids": ["message-1"]})
        mailbox.call("check_inbox", {})
        replacement = self.mailbox(reason=reason)
        with self.assertRaises(MailboxError):
            replacement.call("ack_messages", {"ids": ["message-1"]})
        reason.fail_delivery = True
        with self.assertRaisesRegex(RuntimeError, "PRIVATE TRANSPORT ERROR"):
            mailbox.call("ack_messages", {"ids": ["message-1"]})
        self.assertEqual(mailbox.offered, {"message-1"})
        self.assertEqual(reason.calls[-2][0], "register")
        reason.fail_delivery = False
        mailbox.call("ack_messages", {"ids": ["message-1", "message-1"]})
        self.assertEqual(reason.calls[-1], ("delivered", {"key": mailbox.key, "ids": ["message-1"]}))
        self.assertEqual(mailbox.offered, set())
        with self.assertRaises(MailboxError):
            mailbox.call("ack_messages", {"ids": ["message-1"]})

    def test_complete_signed_large_backlog_can_be_read_and_partially_acked(self):
        reason = FakeReason()
        reason.messages = [self.message("x" * 64000, message_id=f"mail-{n}", wake=False) for n in range(20)]
        mailbox = self.mailbox(reason=reason)
        self.assertEqual(mailbox.call("poll", {})["wake_ids"], [])
        frames = self.bridge(mailbox, b'{"id":1,"op":"check_inbox"}\n')
        read = frames[0]["result"]
        self.assertTrue(frames[0]["ok"])
        self.assertEqual(len(read["message_ids"]), 20)
        self.assertGreater(len(read["framed_messages"].encode()), 1024 * 1024)
        mailbox.call("ack_messages", {"ids": ["mail-0", "mail-19"]})
        self.assertEqual(len(mailbox.offered), 18)

    def test_parallel_calls_serialize_transport_and_ack_admission(self):
        class DetectOverlapReason(FakeReason):
            def __init__(self):
                super().__init__()
                self.guard = threading.Lock()
                self.active = self.peak = 0
                self.sequence = []

            def request(self, op, **fields):
                with self.guard:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    self.sequence.append((threading.get_ident(), op))
                try:
                    # A socket read releases the GIL, permitting other tool
                    # workers to enter an unprotected transport concurrently.
                    time.sleep(0.005)
                    return super().request(op, **fields)
                finally:
                    with self.guard:
                        self.active -= 1

        reason = DetectOverlapReason()
        reason.messages = [self.message(message_id=f"mail-{n}") for n in range(2)]
        mailbox = self.mailbox(reason=reason)

        def parallel(jobs):
            ready = threading.Barrier(len(jobs))

            def run(job):
                ready.wait(timeout=3)
                return job()

            with ThreadPoolExecutor(max_workers=len(jobs)) as workers:
                return list(workers.map(run, jobs))

        parallel([lambda: mailbox.call("check_inbox", {}), lambda: mailbox.call("poll", {}),
                  lambda: mailbox.call("send_message", {"to": "kan/t3-thread", "message": "Signed request",
                                                         "idempotency_key": "parallel-send"}),
                  lambda: mailbox.call("list_team", {}), lambda: mailbox.call("identity", {}),
                  mailbox.register])
        self.assertEqual(reason.peak, 1, "parallel workers overlapped the shared transport")
        # Each complete registration + operation must stay contiguous, beyond
        # merely locking one request at a time on the underlying connection.
        groups = []
        for thread_id, op in reason.sequence:
            if op == "register":
                groups.append([])
            groups[-1].append((thread_id, op))
        self.assertEqual(len(groups), 6)
        for group in groups:
            self.assertEqual(len({thread_id for thread_id, _ in group}), 1)
            self.assertIn([op for _, op in group],
                          (["register"], ["register", "who"], ["register", "send"],
                           ["register", "inbox", "who"]))
        self.assertEqual(mailbox.offered, {"mail-0", "mail-1"})

        def ack():
            try:
                return mailbox.call("ack_messages", {"ids": ["mail-0"]})
            except MailboxError:
                return "not_offered"

        reason.fail_delivery = True

        def failed_ack():
            with self.assertRaisesRegex(RuntimeError, "PRIVATE TRANSPORT ERROR"):
                ack()

        parallel([failed_ack] * 4)
        self.assertEqual(mailbox.offered, {"mail-0", "mail-1"})
        reason.fail_delivery = False
        before = len(reason.calls)
        results = parallel([ack] * 4)
        self.assertEqual(results.count("not_offered"), 3)
        self.assertEqual([row for row in results if isinstance(row, dict)], [{"acknowledged_ids": ["mail-0"]}])
        self.assertEqual([op for op, _ in reason.calls[before:]].count("delivered"), 1)
        self.assertEqual(mailbox.offered, {"mail-1"})
        self.assertEqual(reason.peak, 1)

    def test_jsonl_errors_are_sanitized_and_requests_remain_bound(self):
        mailbox = self.mailbox()
        requests = [{"id": 0, "op": "identity"}, {"id": True, "op": "identity"},
                    {"id": 2, "op": "send_message", "args": {"from": "PRIVATE FORGED SENDER"}},
                    {"id": 3, "op": "check_inbox", "args": []}, {"id": 4, "op": "list_team"}]
        frames = self.bridge(mailbox, b"null\n" + b"".join(json.dumps(row).encode() + b"\n" for row in requests))
        self.assertEqual([row["ok"] for row in frames], [False, True, False, False, False, True])
        for frame in (frames[0], *frames[2:5]):
            self.assertEqual(frame["error"], "OpenClaw mailbox operation failed")
        self.assertNotIn("PRIVATE", json.dumps(frames))
        for bad_frame in (b'{"id":1}', b"x" * (1024 * 1024) + b"\n"):
            with self.subTest(size=len(bad_frame)), self.assertRaisesRegex(MailboxError, "input frame"):
                self.bridge(mailbox, bad_frame)

    def test_mcp_exposes_only_signed_bound_tools_and_required_idempotency(self):
        server = create_mcp_server(self.mailbox())
        tools = asyncio.run(server.list_tools())
        self.assertEqual({tool.name for tool in tools},
                         {"identity", "list_team", "send_message", "check_inbox", "ack_messages"})
        send = next(tool for tool in tools if tool.name == "send_message")
        self.assertIn("idempotency_key", send.input_schema["required"])
        self.assertNotIn("from", send.input_schema["properties"])
        unsigned = self.mailbox(adapter="pi", binding=replace(self.binding, identity=None))
        with self.assertRaisesRegex(MailboxError, "Signed identity"):
            create_mcp_server(unsigned)


if __name__ == "__main__":
    unittest.main()
