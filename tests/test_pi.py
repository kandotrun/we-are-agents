from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from waa.pi import PiBinding, PiError, PiMailbox

ROOT = Path(__file__).resolve().parents[1]


class FakeReason:
    def __init__(self):
        self.calls = []
        self.messages = [{"id": "m1", "wake": True}]
        self.text = '<reason-message from="peer/agent" id="m1">hello</reason-message>'

    def request(self, op, **fields):
        self.calls.append((op, fields))
        if op == "inbox":
            return {"messages": self.messages, "text": self.text}
        return {"ok": True}


class PiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="waa-pi-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "pi.json"
        self.path.write_text(json.dumps({"reason_state": str(self.directory / "reason"), "workspace": str(self.directory)}))
        self.binding = PiBinding.load(self.path)

    def test_fixed_key_and_read_ack_each_reacquire_registration(self):
        reason = FakeReason()
        inbox = PiMailbox(self.binding, "session-1", reason)
        self.assertEqual(inbox.call("poll", {}), {"wake_ids": ["m1"]})
        with self.assertRaises(PiError):
            inbox.call("ack_messages", {"ids": ["m1"]})
        result = inbox.call("check_inbox", {})
        self.assertEqual(result["framed_messages"], reason.text)
        self.assertNotIn("delivered", [op for op, _ in reason.calls])
        inbox.call("ack_messages", {"ids": ["m1"]})
        self.assertEqual(reason.calls[-2][0], "register")
        self.assertEqual(reason.calls[-1], ("delivered", {"key": inbox.key, "ids": ["m1"]}))
        self.assertNotEqual(inbox.key, PiMailbox(self.binding, "session-2", reason).key)

    def test_fyi_waits_and_valid_large_backlog_remains_readable(self):
        reason = FakeReason()
        reason.messages = [{"id": f"mail-{n}", "wake": False} for n in range(1201)]
        reason.text = "valid framed backlog" * 100000
        inbox = PiMailbox(self.binding, "session-1", reason)
        self.assertEqual(inbox.call("poll", {}), {"wake_ids": []})
        self.assertEqual(inbox.call("check_inbox", {})["framed_messages"], reason.text)
        inbox.call("ack_messages", {"ids": ["mail-1200"]})

    def test_send_has_fixed_from_and_no_group_or_operation_proxy(self):
        reason = FakeReason(); inbox = PiMailbox(self.binding, "session-1", reason)
        inbox.call("send_message", {"to": "peer/agent", "message": "hello", "wake": False, "idempotency_key": "call-1"})
        self.assertEqual(reason.calls[-1][1]["from"], inbox.key)
        self.assertFalse(reason.calls[-1][1]["wake"])
        self.assertEqual(reason.calls[-1][1]["idempotency_key"], "call-1")
        for target in ("*", "peer/*", "repo:project", "*@machine"):
            with self.subTest(target=target), self.assertRaises(PiError):
                inbox.call("send_message", {"to": target, "message": "hello"})
        for op, args in (("shutdown", {}), ("send_message", {"from": "other", "to": "peer", "message": "x"})):
            with self.assertRaises(PiError):
                inbox.call(op, args)

    def test_node_lifecycle_and_tool_contract(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is needed for Pi extension checks")
        result = subprocess.run([node, "--test", str(ROOT / "tests/pi_adapter.test.mjs")],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installed_pi_rpc_wakes_and_replies_through_synthetic_provider(self):
        executable = shutil.which("pi")
        if not executable:
            self.skipTest("An installed Pi is needed for process acceptance")
        from waa.bootstrap import ensure_tools
        from waa.smoke import _Daemon, _eventually, _free_port, _register, _relay_ready, _stop

        stack = ExitStack()
        self.addCleanup(stack.close)
        tools = ensure_tools()
        rejected = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        rejected.bind(("127.0.0.1", 0))
        port = _free_port()
        config = self.directory / "relay.toml"
        config.write_text(f'http_bind_addr="127.0.0.1:{port}"\nenable_metrics=false\nenable_quic_addr_discovery=false\naccess="everyone"\n')
        relay_log = stack.enter_context((self.directory / "relay.log").open("wb"))
        relay = subprocess.Popen([str(tools["iroh-relay"]), "--dev", "--config-path", str(config)],
                                 stdout=relay_log, stderr=relay_log, stdin=subprocess.DEVNULL)
        stack.callback(_stop, relay)
        _eventually(lambda: _relay_ready(relay, port))
        daemon = _Daemon(tools["reason-network"], self.binding.reason_state, "pi-proof", stack,
                         f"http://127.0.0.1:{rejected.getsockname()[1]}")
        daemon.start()
        with daemon.client() as client:
            client.request("create_team", name="synthetic-pi-proof", person="alice", relay=f"http://127.0.0.1:{port}")
        daemon.after_membership_change()
        sender = stack.enter_context(daemon.client())
        sender_key = "cli:pi-proof-peer"
        sender_address = _register(sender, sender_key, self.directory, name="synthetic-peer")
        provider_requests = []
        expected_ids = []
        wake_id = None

        class ProviderHandler(BaseHTTPRequestHandler):
            def log_message(handler, *_args):
                pass

            def do_POST(handler):
                data = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                provider_requests.append(data)
                names = {call.get("function", {}).get("name") for message in data["messages"]
                         for call in message.get("tool_calls", [])}
                next_tool = next((name for name in ("waa_check_inbox", "waa_ack_messages", "waa_send_message") if name not in names), None)
                args = {"waa_check_inbox": {}, "waa_ack_messages": {"ids": list(expected_ids)},
                        "waa_send_message": {"to": sender_address, "message": "synthetic reply", "reply_to": wake_id}}
                if next_tool:
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call-" + next_tool,
                             "type": "function", "function": {"name": next_tool, "arguments": json.dumps(args[next_tool])}}]}
                    finish = "tool_calls"
                else:
                    delta = {"role": "assistant", "content": "Synthetic peer exchange complete."}; finish = "stop"
                handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
                for delta, ending in ((delta, None), ({}, finish)):
                    chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "synthetic",
                             "choices": [{"index": 0, "delta": delta, "finish_reason": ending}]}
                    handler.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                handler.wfile.write(b"data: [DONE]\n\n"); handler.wfile.flush()

        provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True); provider_thread.start()
        agent_dir = self.directory / "agent"; agent_dir.mkdir()
        (agent_dir / "models.json").write_text(json.dumps({"providers": {"waa-test": {
            "baseUrl": f"http://127.0.0.1:{provider.server_port}/v1", "api": "openai-completions",
            "apiKey": "fake-local-test", "models": [{"id": "synthetic", "contextWindow": 32000, "maxTokens": 1024}]}}}))
        env = {"PATH": os.environ["PATH"], "PI_CODING_AGENT_DIR": str(agent_dir), "PI_TELEMETRY": "0", "PI_OFFLINE": "1",
               "WAA_PI_BRIDGE_ARGV": json.dumps([sys.executable, "-m", "waa.pi", "bridge", "--binding", str(self.path)])}
        process = subprocess.Popen([executable, "--offline", "--no-extensions", "--no-skills", "--no-context-files",
                                    "--no-prompt-templates", "--no-themes", "--no-builtin-tools", "--mode", "rpc",
                                    "--provider", "waa-test", "--model", "synthetic", "--session-dir", str(self.directory / "sessions"),
                                    "--extension", str(ROOT / "integrations/pi/extension.mjs")],
                                   cwd=self.directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        events = queue.Queue(); errors = []

        def read_events():
            for line in process.stdout:
                try: events.put(json.loads(line))
                except ValueError: errors.append(line)

        reader = threading.Thread(target=read_events, daemon=True); reader.start()
        try:
            process.stdin.write('{"id":"state","type":"get_state"}\n'); process.stdin.flush()
            deadline = time.monotonic() + 12
            state = None
            while time.monotonic() < deadline:
                event = events.get(timeout=max(0.1, deadline - time.monotonic()))
                if event.get("id") == "state": state = event; break
            self.assertIsNotNone(state); self.assertTrue(state["success"], state)
            def pi_session():
                return next((row for row in sender.request("who", all=True)["sessions"] if row["agent"] == "pi"), None)

            recipient = _eventually(pi_session, timeout=8)
            fyi = sender.request("send", **{"from": sender_key}, to=recipient["address"], body="synthetic FYI",
                                 wake=False, notify=False, idempotency_key="pi-proof-fyi")
            expected_ids.extend(fyi["ids"])
            time.sleep(1.2)
            self.assertEqual(provider_requests, [], "FYI started model work")
            wake = sender.request("send", **{"from": sender_key}, to=recipient["address"], body="synthetic question",
                                  wake=True, notify=False, idempotency_key="pi-proof-wake")
            expected_ids.extend(wake["ids"]); wake_id = wake["ids"][0]
            observed = []
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                event = events.get(timeout=max(0.1, deadline - time.monotonic())); observed.append(event)
                if event.get("type") == "agent_settled": break
            self.assertTrue(provider_requests, "peer arrival did not start a model turn")
            replies = sender.request("inbox", key=sender_key, peek=True, wait_secs=0)["messages"]
            self.assertEqual([reply["body"] for reply in replies], ["synthetic reply"])
            self.assertEqual(replies[0]["reply_to"], wake_id)
            pi_key = PiMailbox(self.binding, state["data"]["sessionId"], sender).key
            self.assertEqual(sender.request("inbox", key=pi_key, peek=True, wait_secs=0)["messages"], [])
            original_frame_reached_model = any("<reason-message" in str(request["messages"])
                                               and "synthetic question" in str(request["messages"])
                                               for request in provider_requests)
            self.assertTrue(original_frame_reached_model)
            offered = {tool["function"]["name"] for tool in provider_requests[0]["tools"]}
            self.assertEqual(offered, {"waa_list_team", "waa_send_message", "waa_check_inbox", "waa_ack_messages"})
            custom = [event for event in observed if event.get("type") == "message_start"
                      and event.get("message", {}).get("customType") == "waa-peer-mail"]
            self.assertTrue(custom, "wake was not retained as a custom peer notification")
            self.assertFalse(errors, errors)
            process.stdin.write('{"id":"new","type":"new_session"}\n'); process.stdin.flush()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                response = events.get(timeout=max(0.1, deadline - time.monotonic()))
                if response.get("id") == "new":
                    self.assertTrue(response["success"], response)
                    break
            else:
                self.fail("Pi new_session did not complete")
            process.stdin.write('{"id":"new-state","type":"get_state"}\n'); process.stdin.flush()
            while time.monotonic() < deadline:
                response = events.get(timeout=max(0.1, deadline - time.monotonic()))
                if response.get("id") == "new-state":
                    self.assertTrue(response["success"], response)
                    new_session = response["data"]["sessionId"]
                    self.assertNotEqual(new_session, state["data"]["sessionId"])
                    break
            else:
                self.fail("Pi replacement state was not returned")
            new_key = PiMailbox(self.binding, new_session, sender).key
            def rebound():
                rows = sender.request("who", all=True)["sessions"]
                return next((row for row in rows if row["agent"] == "pi" and row["status"] != "offline"
                             and row["address"] != recipient["address"]), None)
            self.assertTrue(_eventually(rebound, timeout=8), "Pi new_session kept the old mailbox binding")
            self.assertEqual(sender.request("inbox", key=new_key, peek=True, wait_secs=0)["messages"], [])
        finally:
            process.stdin.close()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            stderr = process.stderr.read()
            process.stdout.close(); process.stderr.close()
            provider.shutdown(); provider.server_close()
            provider_thread.join(timeout=2)
        self.assertEqual(process.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main()
