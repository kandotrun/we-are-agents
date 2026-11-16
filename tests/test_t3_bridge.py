from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest

from waa.identity import PREFIX, initialize_identity
from waa.t3 import Binding, Inbox, T3Error
from waa.t3_bridge import Bridge, serve_bridge


class FakeReason:
    def __init__(self):
        self.calls = []
        self.fail_ack = False
        self.text = "Original framed peer body"

    def request(self, op, **fields):
        self.calls.append((op, fields))
        if op == "register":
            return {"address": "owner/t3"}
        if op == "inbox":
            return {"messages": [{"id": "mail-1", "body": "Original peer body", "wake": True}],
                    "text": self.text}
        if op == "delivered" and self.fail_ack:
            raise RuntimeError("SECRET transport error and PRIVATE BODY")
        if op == "who":
            return {"machine": "synthetic", "sessions": [{"address": "claimed/peer"}]}
        if op == "send":
            return {"ids": ["sent-1"]}
        return {}


class T3BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="waa-t3-bridge-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "t3.json"
        self.path.write_text(json.dumps({
            "reason_state": "reason", "environment_id": "environment-1", "project_id": "project-1",
            "thread_id": "thread-1", "workspace": ".", "mcp_url": "http://127.0.0.1:3773/mcp",
            "token_file": "must-not-read.token",
        }))
        self.binding = Binding.load(self.path)
        self.reason = FakeReason()
        self.bridge = Bridge(Inbox(self.binding, self.reason))
        self.addCleanup(lambda: self.bridge.close())

    def exchange(self, requests):
        source = io.BytesIO(b"".join(json.dumps(request).encode() + b"\n" for request in requests))
        target = io.StringIO()
        serve_bridge(self.bridge, input_stream=source, output_stream=target)
        return [json.loads(line) for line in target.getvalue().splitlines()]

    def test_separate_requests_keep_offered_ids_and_only_explicit_ack_delivers(self):
        denied = self.exchange([{"id": 1, "op": "ack_messages", "args": {"ids": ["mail-1"]}}])[0]
        self.assertFalse(denied["ok"])
        self.assertFalse(self.reason.calls)
        read = self.exchange([{"id": 2, "op": "check_inbox"}])[0]
        self.assertEqual(read["result"]["framed_messages"], self.reason.text)
        self.assertNotIn("delivered", [op for op, _ in self.reason.calls])
        self.reason.fail_ack = True
        failed = self.exchange([{"id": 3, "op": "ack_messages", "args": {"ids": ["mail-1"]}}])[0]
        self.assertFalse(failed["ok"])
        self.assertNotIn("SECRET", json.dumps(failed))
        self.assertIn("mail-1", self.bridge.inbox.offered)
        self.reason.fail_ack = False
        ack = self.exchange([{"id": 4, "op": "ack_messages", "args": {"ids": ["mail-1"]}}])[0]
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["result"]["acknowledged_ids"], ["mail-1"])
        self.assertNotIn("mail-1", self.bridge.inbox.offered)
        second = Bridge(Inbox(self.binding, self.reason))
        with self.assertRaises(T3Error):
            second.call("ack_messages", {"ids": ["mail-1"]})

    def test_untrusted_fields_and_malformed_requests_never_reach_reason(self):
        bad = [
            [], {"id": True, "op": "check_inbox"}, {"id": -1, "op": "check_inbox"},
            {"id": 1, "op": "check_inbox", "from": "other"},
            {"id": 2, "op": "register", "args": {}},
            {"id": 3, "op": "check_inbox", "args": {"session": "other"}},
            {"id": 4, "op": "identity", "args": []},
            {"id": 5, "op": "send_message", "args": {"to": "peer"}},
            {"id": 6, "op": "send_message", "args": {
                "to": "peer", "message": "PRIVATE", "idempotency_key": "one", "from": "other"}},
            {"id": 7, "op": "ack_messages", "args": {}},
            {"id": 8, "op": "list_team", "args": {"workspace": "/other"}},
        ]
        responses = self.exchange(bad)
        self.assertTrue(all(not response["ok"] for response in responses))
        self.assertNotIn("PRIVATE", json.dumps(responses))
        self.assertEqual(self.reason.calls, [])

    def test_large_valid_response_is_complete_and_input_is_bounded(self):
        self.reason.text = "original body " + "x" * (2 * 1024 * 1024)
        response = self.exchange([{"id": 1, "op": "check_inbox"}])[0]
        self.assertEqual(response["result"]["framed_messages"], self.reason.text)
        with self.assertRaisesRegex(T3Error, "input frame"):
            serve_bridge(self.bridge, input_stream=io.BytesIO(b"x" * (1024 * 1024 + 1)),
                         output_stream=io.StringIO())
        with self.assertRaisesRegex(T3Error, "input frame"):
            serve_bridge(self.bridge, input_stream=io.BytesIO(b'{"id":2,"op":"identity"}'),
                         output_stream=io.StringIO())

    def test_signed_identity_directory_and_send_use_bound_mailbox_without_token(self):
        info = initialize_identity(self.root / "identity", owner_id="synthetic", owner_name="Owner",
                                   agent_name="T3")
        signed = replace(self.binding, identity=self.root / "identity" / "profile.json")
        self.bridge = Bridge(Inbox(signed, self.reason))
        responses = self.exchange([
            {"id": 1, "op": "identity"}, {"id": 2, "op": "list_team"},
            {"id": 3, "op": "send_message", "args": {
                "to": "peer/openclaw", "message": "Synthetic signed response",
                "idempotency_key": "logical-1", "reply_to": "parent-1", "wake": False}},
        ])
        self.assertTrue(all(response["ok"] for response in responses))
        self.assertEqual(responses[0]["result"]["public_key"], info["public_key"])
        self.assertEqual(responses[1]["result"]["identity_context"]["owner_id"], "synthetic")
        self.assertIn("claims", responses[1]["result"]["notice"])
        directory = [fields for op, fields in self.reason.calls if op == "who"]
        self.assertEqual(directory, [{"me": signed.session_key}])
        sends = [fields for op, fields in self.reason.calls if op == "send"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["from"], signed.session_key)
        self.assertEqual(sends[0]["reply_to"], "parent-1")
        self.assertFalse(sends[0]["wake"])
        payload = json.loads(sends[0]["body"][len(PREFIX):])["payload"]
        self.assertEqual(payload["sender"], "owner/t3")
        self.assertEqual(payload["recipient"], "peer/openclaw")
        self.assertEqual(payload["body"], "Synthetic signed response")
        self.assertFalse(signed.token_file.exists())
        for op, fields in self.reason.calls:
            if op == "register":
                self.assertEqual(fields["session"]["key"], signed.session_key)
                self.assertEqual(fields["session"]["agent_session_id"], "thread-1")
                self.assertEqual(fields["session"]["cwd"], str(self.root.resolve()))


if __name__ == "__main__":
    unittest.main()
