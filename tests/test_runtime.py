import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from waa.runtime import isolated_env, prepare_network, private_network_env, relay_url, ticket_relay


def ticket(relay):
    name, url = b"test", relay.encode()
    raw = bytes(32) + bytes([len(name)]) + name + bytes([len(url)]) + url
    return "rn1-" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


class PrivateRuntime(unittest.TestCase):
    def test_invite_requires_matching_explicit_private_relay(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            with self.assertRaisesRegex(ValueError, "relay"):
                prepare_network(state, "https://relay.internal", ["join", ticket("")])
            with self.assertRaisesRegex(ValueError, "private relay"):
                prepare_network(state, "https://relay.internal", ["join", ticket("https://other.internal")])
            self.assertFalse((state / "waa-relay.json").exists())
            result = prepare_network(state, "https://relay.internal", ["join", ticket("https://relay.internal")])
            self.assertIn("--no-setup", result)

    def test_init_pins_relay_and_disables_global_setup(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            result = prepare_network(state, "https://relay.internal", ["init", "team"])
            self.assertEqual(result, ["init", "team", "--relay", "https://relay.internal/", "--no-setup"])
            with self.assertRaisesRegex(ValueError, "another relay"):
                prepare_network(state, "https://other.internal", ["who"])
            with self.assertRaisesRegex(ValueError, "global setup"):
                prepare_network(state, "https://relay.internal", ["setup"])

    def test_rejects_external_plaintext_and_url_credentials(self):
        for url in ("http://relay.internal", "https://user:secret@relay.internal", "https://relay.reasonmachines.com", "https://relay.internal/?token=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                relay_url(url)
        self.assertEqual(relay_url("http://127.0.0.1:3340"), "http://127.0.0.1:3340/")

    def test_inherited_proxy_bypass_cannot_allow_public_default_lookup(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"NO_PROXY": "*"}):
            with private_network_env(Path(folder), "https://relay.internal") as env:
                self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1,relay.internal")
                self.assertTrue(env["HTTPS_PROXY"].startswith("http://127.0.0.1:"))

    def test_smoke_does_not_inherit_live_agent_identity_or_home_override(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": "/private/live", "CODEX_THREAD_ID": "real", "REASON_NETWORK_SESSION": "real"}):
            env = isolated_env(Path(folder), machine="test")
            self.assertNotIn("CLAUDE_CODE_MESSAGING_SOCKET", env)
            self.assertNotIn("CODEX_THREAD_ID", env)
            self.assertNotIn("REASON_NETWORK_SESSION", env)
            self.assertEqual(env.get("HOME"), os.environ.get("HOME"))
            self.assertEqual(env["REASON_NETWORK_NO_UPDATE"], "1")
            self.assertEqual(ticket_relay(ticket("https://relay.internal/")), "https://relay.internal/")
