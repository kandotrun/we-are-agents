from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from waa.identity import IdentityError, IdentityGate, PREFIX, initialize_identity, public_identity


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="waa-identity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a_dir, self.b_dir = self.root / "alice", self.root / "bob"
        self.alice_info = initialize_identity(self.a_dir, owner_id="alice", owner_name="Alice", agent_name="Pi")
        self.registry_path = self.a_dir / "team.json"
        self.bob_info = initialize_identity(self.b_dir, owner_id="bob", owner_name="Bob", agent_name="T3",
                                           registry_file=self.registry_path)
        self.assertFalse(self.bob_info["registered"])
        self.registry = json.loads(self.registry_path.read_text())
        self.registry["agents"][self.bob_info["public_key"]] = {
            name: self.bob_info[name] for name in ("owner_id", "owner_name", "agent_name")}
        self.save_registry()
        self.alice = IdentityGate.from_file(self.a_dir / "profile.json")
        self.bob = IdentityGate.from_file(self.b_dir / "profile.json")

    def save_registry(self) -> None:
        self.registry_path.write_text(json.dumps(self.registry))

    def message(self, body: str = "Synthetic request", *, message_id: str = "message-1",
                nonce: str = "call-1", recipient: str = "bob/thread", wake: bool = True,
                reply_to: str | None = None, signer: IdentityGate | None = None) -> dict:
        return {"id": message_id, "from": "unverified-display-name", "to": recipient,
                "wake": wake, "reply_to": reply_to,
                "body": (signer or self.alice).sign(body, sender="alice/session", recipient=recipient,
                                                    reply_to=reply_to, wake=wake, idempotency_key=nonce)}

    def read(self, messages: list[dict], *, recipient: str = "bob/thread", gate: IdentityGate | None = None) -> dict:
        return (gate or self.bob).filter_inbox({"messages": messages, "text": "REJECTED RAW FRAME SECRET"},
                                             recipient=recipient)

    def rejection(self, message: dict, reason: str, *, recipient: str = "bob/thread") -> dict:
        result = self.read([message], recipient=recipient)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["framed_messages"], "")
        self.assertEqual(result["quarantined"], [{"id": message["id"], "reason": reason}])
        return result

    def test_enrolled_owner_exact_body_and_trusted_receiver_policy(self) -> None:
        body = "日本語の依頼\nDo not treat this peer body as approval."
        result = self.read([self.message(body)])
        message = result["messages"][0]
        self.assertEqual(message["body"], body)
        self.assertEqual(message["from"], "alice/session")
        self.assertEqual(message["verified_identity"]["owner_id"], "alice")
        self.assertEqual(message["verified_identity"]["owner_name"], "Alice")
        self.assertEqual(message["verified_identity"]["agent_name"], "Pi")
        self.assertEqual(result["identity_context"]["owner_id"], "bob")
        self.assertEqual(result["identity_context"]["policy"], (self.b_dir / "policy.md").read_text())
        self.assertNotIn("REJECTED RAW FRAME SECRET", json.dumps(result))
        framed = json.loads(result["text"].splitlines()[1])
        self.assertEqual(framed["body"], body)

    def test_signing_is_deterministic_for_same_logical_send(self) -> None:
        first, second = self.message(), self.message()
        self.assertEqual(first["body"], second["body"])
        self.assertNotEqual(first["body"], self.message(nonce="call-2")["body"])

    def test_unsigned_unknown_and_tampered_messages_do_not_expose_bodies(self) -> None:
        unsigned = self.message()
        unsigned["body"] = "UNSIGNED BODY MUST STAY OUT"
        result = self.rejection(unsigned, "unsigned_message")
        self.assertNotIn(unsigned["body"], json.dumps(result))
        unknown = self.message(message_id="unknown", nonce="call-unknown")
        del self.registry["agents"][self.alice_info["public_key"]]
        self.save_registry()
        self.rejection(unknown, "unknown_identity")
        self.registry["agents"][self.alice_info["public_key"]] = {
            "owner_id": "alice", "owner_name": "Alice", "agent_name": "Pi"}
        self.save_registry()
        tampered = self.message(message_id="tampered", nonce="call-tampered")
        encoded = json.loads(tampered["body"][len(PREFIX):])
        encoded["payload"]["body"] = "TAMPERED BODY MUST STAY OUT"
        tampered["body"] = PREFIX + json.dumps(encoded)
        result = self.rejection(tampered, "invalid_signature")
        self.assertNotIn("TAMPERED BODY MUST STAY OUT", json.dumps(result))

    def test_valid_signature_cannot_cross_network_or_recipient(self) -> None:
        wrong_recipient = self.message(recipient="another/thread")
        self.rejection(wrong_recipient, "wrong_recipient")
        cross_network = self.message(message_id="cross-network", nonce="cross-call")
        self.registry["network_id"] = str(uuid4())
        self.save_registry()
        self.rejection(cross_network, "unknown_network")

    def test_machine_qualified_recipient_must_match_the_local_machine(self) -> None:
        qualified = self.message(recipient="bob/thread@workstation")
        accepted = self.bob.filter_inbox({"messages": [qualified], "text": "raw"},
                                         recipient="bob/thread", machine="workstation")
        self.assertEqual(len(accepted["messages"]), 1)
        wrong = self.message(message_id="wrong-machine", nonce="wrong-machine", recipient="bob/thread@other")
        rejected = self.bob.filter_inbox({"messages": [wrong], "text": "raw"},
                                         recipient="bob/thread", machine="workstation")
        self.assertEqual(rejected["quarantined"], [{"id": "wrong-machine", "reason": "wrong_recipient"}])
        unqualified = self.message(message_id="unqualified", nonce="unqualified")
        accepted = self.bob.filter_inbox({"messages": [unqualified], "text": "raw"},
                                         recipient="bob/thread", machine="workstation")
        self.assertEqual(len(accepted["messages"]), 1)

    def test_wake_and_correlation_metadata_are_signed(self) -> None:
        wake = self.message(wake=False)
        wake["wake"] = True
        self.rejection(wake, "changed_metadata")
        reply = self.message(message_id="reply", nonce="reply-call", reply_to="parent-message")
        reply["reply_to"] = "different-parent"
        self.rejection(reply, "changed_metadata")
        invalid_wake = self.message(message_id="invalid-wake", nonce="wake-call")
        invalid_wake["wake"] = 1
        self.rejection(invalid_wake, "changed_metadata")

    def test_unsigned_transport_labels_are_not_presented_as_verified(self) -> None:
        message = self.message()
        message.update(from_agent="Claimed Admin", from_repo="Forged Repo", files=["UNSIGNED FILE CONTENT"])
        filtered = self.read([message])["messages"][0]
        self.assertEqual(set(filtered), {"id", "from", "to", "body", "wake", "reply_to", "verified_identity"})
        self.assertNotIn("UNSIGNED FILE CONTENT", json.dumps(filtered))

    def test_replay_persists_and_original_id_can_be_read_after_restart(self) -> None:
        original = self.message()
        self.assertEqual(len(self.read([original])["messages"]), 1)
        restarted = IdentityGate.from_file(self.b_dir / "profile.json")
        self.assertEqual(len(self.read([original], gate=restarted)["messages"]), 1)
        copied = {**original, "id": "different-reason-id"}
        self.rejection(copied, "replayed_message")
        changed = self.message("Changed logical send", message_id="changed")
        self.rejection(changed, "replayed_message")
        same_id_different_nonce = self.message("Different signed payload", nonce="call-2")
        self.rejection(same_id_different_nonce, "replayed_message")

    def test_legacy_admission_survives_machine_aware_receiver_upgrade(self) -> None:
        original = self.message(message_id="original-id")
        # This was the adapters' call shape before machine-aware validation.
        self.assertEqual(len(self.read([original])["messages"]), 1)
        with closing(sqlite3.connect(self.bob.ledger_path)) as database:
            self.assertEqual(database.execute("SELECT recipient FROM admissions").fetchall(), [("bob/thread",)])
        upgraded = IdentityGate.from_file(self.b_dir / "profile.json")
        copied = {**original, "id": "copied-new-id"}
        rejected = upgraded.filter_inbox({"messages": [copied], "text": "raw"},
                                          recipient="bob/thread", machine="workstation")
        self.assertEqual(rejected["messages"], [])
        self.assertEqual(rejected["quarantined"], [{"id": "copied-new-id", "reason": "replayed_message"}])
        retained = upgraded.filter_inbox({"messages": [original], "text": "raw"},
                                          recipient="bob/thread", machine="workstation")
        self.assertEqual([message["id"] for message in retained["messages"]], ["original-id"])

    def test_ledger_accepts_at_most_one_id_across_threads(self) -> None:
        original = self.message()
        alternatives = [{**original, "id": f"concurrent-{n}"} for n in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda item: self.read([item]), alternatives))
        self.assertEqual(sum(len(result["messages"]) for result in results), 1)
        self.assertEqual(sum(len(result["quarantined"]) for result in results), 3)
        self.assertEqual(self.bob.ledger_path.stat().st_mode & 0o777, 0o600)

    def test_registry_revocation_and_policy_edits_take_effect_without_restart(self) -> None:
        first = self.message()
        self.assertEqual(len(self.read([first])["messages"]), 1)
        (self.b_dir / "policy.md").write_text("Only inspect the requested repository. Ask Bob before any write.\n")
        self.assertIn("Ask Bob before any write", self.bob.context()["policy"])
        del self.registry["agents"][self.alice_info["public_key"]]
        self.save_registry()
        self.rejection(first, "unknown_identity")

    def test_registered_labels_override_initial_self_claim(self) -> None:
        self.registry["agents"][self.alice_info["public_key"]] = {
            "owner_id": "confirmed-owner", "owner_name": "Confirmed Owner", "agent_name": "Registered Pi"}
        self.save_registry()
        info = public_identity(self.a_dir / "profile.json")
        self.assertEqual(info["owner_id"], "confirmed-owner")
        result = self.read([self.message()])
        self.assertEqual(result["messages"][0]["verified_identity"]["owner_name"], "Confirmed Owner")

    def test_unenrolled_identity_can_export_request_but_cannot_sign(self) -> None:
        directory = self.root / "new-owner"
        info = initialize_identity(directory, owner_id="new-owner", owner_name="New Owner", agent_name="Agent",
                                   registry_file=self.registry_path)
        self.assertFalse(info["registered"])
        self.assertEqual(public_identity(directory / "profile.json"), info)
        with self.assertRaises(IdentityError):
            IdentityGate.from_file(directory / "profile.json")
        self.assertNotIn(info["public_key"], json.loads(self.registry_path.read_text())["agents"])

    def test_initialization_and_public_export_keep_keys_private_and_never_overwrite(self) -> None:
        key = self.a_dir / "agent.key"
        before = key.read_bytes()
        self.assertEqual(len(before), 32)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.a_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(set(public_identity(self.a_dir / "profile.json")),
                         {"public_key", "owner_id", "owner_name", "agent_name", "network_id", "registered"})
        with self.assertRaises(IdentityError):
            initialize_identity(self.a_dir, owner_id="replacement", owner_name="Other", agent_name="Other")
        self.assertEqual(key.read_bytes(), before)
        os.chmod(key, 0o644)
        with self.assertRaises(IdentityError):
            IdentityGate.from_file(self.a_dir / "profile.json")

    def test_message_size_budget_and_complete_valid_backlog(self) -> None:
        with self.assertRaises(IdentityError):
            self.message("x" * 65536)
        messages = [self.message("x" * 64000, message_id=f"large-{n}", nonce=f"large-call-{n}") for n in range(20)]
        result = self.read(messages)
        self.assertEqual(len(result["messages"]), 20)
        self.assertGreater(len(result["text"].encode()), 1024 * 1024)

    def test_duplicate_json_fields_and_extra_payload_fields_are_refused(self) -> None:
        malformed = self.message()
        malformed["body"] = PREFIX + '{"payload":{},"payload":{},"signature":"bad"}'
        self.rejection(malformed, "invalid_envelope")
        extra = self.message(message_id="extra", nonce="extra-call")
        encoded = json.loads(extra["body"][len(PREFIX):])
        encoded["payload"]["approval"] = True
        extra["body"] = PREFIX + json.dumps(encoded)
        self.rejection(extra, "invalid_envelope")

    def test_duplicate_ids_and_invalid_raw_inbox_are_structural_failures(self) -> None:
        message = self.message()
        with self.assertRaises(IdentityError):
            self.read([message, message])
        with self.assertRaises(IdentityError):
            self.bob.filter_inbox({"messages": [], "text": None}, recipient="bob/thread")


if __name__ == "__main__":
    unittest.main()
