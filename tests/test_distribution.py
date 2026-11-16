"""Local synthetic release fixtures; no GitHub, user state or installed agents."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from waa.distribution import (DistributionError, INSTALLER, MANIFEST, Manager, REPOSITORY,
                              default_home, main, manifest, maybe_notice)


def release(version="0.2.0", *, reason="v0.12.17", state_format=1, extra=None):
    descriptor = {"schema": 1, "version": version, "tag": "v" + version,
                  "repository": REPOSITORY, "source_commit": "a" * 40,
                  "compatibility": {"state_format": state_format, "reason_network": reason,
                                    "iroh_relay": "v1.4.1", "python": ">=3.11",
                                    "adapters": {"pi": ["0.87.1"], "openclaw": ["2026.11.4"]}}}
    installer = b"# Synthetic standalone installer fixture\n"
    files = {"release.json": json.dumps(descriptor).encode(),
             "pyproject.toml": f'[project]\nname="we-are-agents"\nversion="{version}"\n'.encode(),
             "uv.lock": b"version = 1\n", "upstream.lock.json": b"{}\n",
             "waa/cli.py": b"# Synthetic CLI fixture\n", "waa/distribution.py": installer,
             "integrations/pi/extension.mjs": b"// Synthetic fixture\n",
             "integrations/pi/adapter.mjs": b"// Synthetic fixture\n"}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(f"we-are-agents-v{version}/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if extra is not None:
            member, data = extra
            archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
    raw = output.getvalue()
    name = f"we-are-agents-v{version}.tar.gz"
    metadata = {**descriptor, "assets": {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)},
        INSTALLER: {"sha256": hashlib.sha256(installer).hexdigest(), "size": len(installer)}}}
    return {MANIFEST: json.dumps(metadata).encode(), name: raw, INSTALLER: installer}


class LocalCommands:
    """A fake gh verifier and process runner, never an authenticity proof."""

    def __init__(self):
        self.releases = {"0.2.0": release(), "0.2.1": release("0.2.1")}
        self.calls = []
        self.fail_verify = False
        self.fail_prepare = None
        self.latest = "0.2.1"

    def __call__(self, argv, *, timeout, cwd=None, env=None):
        self.calls.append((list(argv), timeout, cwd, env))
        if argv[0] == "gh":
            if argv[1:3] == ["release", "view"]:
                result = {"tagName": "v" + self.latest, "isDraft": False, "isPrerelease": False}
                return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
            version = argv[3][1:]
            if argv[2] in {"verify", "verify-asset"}:
                return subprocess.CompletedProcess(argv, 1 if self.fail_verify else 0,
                                                   "", "SYNTHETIC_SECRET_FROM_REMOTE")
            if argv[2] == "download":
                destination = Path(argv[argv.index("--dir") + 1])
                name = argv[argv.index("--pattern") + 1]
                (destination / name).write_bytes(self.releases[version][name])
                return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "uv":
            directory = Path(argv[argv.index("--project") + 1])
            executable = directory / ".venv/bin/waa"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            return subprocess.CompletedProcess(argv, 0, "", "")
        version = Path(argv[0]).parents[2].name
        operation = argv[1]
        if self.fail_prepare == (version, operation):
            return subprocess.CompletedProcess(argv, 1, "", "SYNTHETIC_SECRET_FROM_REMOTE")
        result = version if operation == "--version" else json.dumps({"ok": True})
        return subprocess.CompletedProcess(argv, 0, result, "")


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="waa-distribution-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "managed install"
        self.commands = LocalCommands()
        self.manager = Manager(self.home, runner=self.commands)

    def test_install_update_and_code_only_rollback_preserve_stable_state_and_paths(self):
        installed = self.manager.install("0.2.0")
        self.assertEqual(installed["action"], "installed")
        self.assertFalse(installed["restart_required"])
        config = self.home / "config/profile.json"
        state = self.home / "state/unread-and-replay.db"
        workspace = self.home / "workspace/note.txt"
        original = {config: b"SYNTHETIC_SIGNING_PROFILE", state: b"UNREAD_AND_REPLAY_LEDGER", workspace: b"LOCAL_WORK"}
        for path, data in original.items():
            path.write_bytes(data)
        resolved = {path: path.resolve() for path in original}
        result = self.manager.apply("0.2.1")
        self.assertEqual(result["current_version"], "0.2.1")
        self.assertEqual(result["previous_version"], "0.2.0")
        self.assertTrue(result["restart_required"])
        self.assertEqual((self.home / "current").resolve(), self.home / "releases/0.2.1")
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.resolve(), resolved[path])
        result = self.manager.rollback("0.2.0")
        self.assertEqual(result["current_version"], "0.2.0")
        self.assertEqual(result["installed_versions"], ["0.2.0", "0.2.1"])
        self.assertTrue((self.home / "releases/0.2.1/.waa-installed.json").is_file())
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.resolve(), resolved[path])
        launcher = (self.home / "bin/waa").read_text()
        self.assertIn(str(self.home / "current/.venv/bin/waa"), launcher)
        self.assertIn("export WAA_HOME=", launcher)
        self.assertIn(str(self.home), launcher)
        self.assertIn('"$@"', launcher)
        preparation = [call for call in self.commands.calls if call[0][0] == "uv"]
        self.assertEqual(len(preparation), 3)
        for argv, _timeout, directory, env in preparation:
            self.assertIn(str(directory), argv)
            self.assertEqual(directory.parent, self.home / "releases")
            self.assertEqual(argv[argv.index("--python") + 1], "3.13")
            self.assertIn("--managed-python", argv)
            self.assertEqual(env["WAA_NO_UPDATE_CHECK"], "1")
        names = [call[0][2] if call[0][0] == "gh" else call[0][0] for call in self.commands.calls]
        self.assertLess(names.index("verify-asset"), names.index("uv"))

    def test_failed_preparation_never_switches_current_or_removes_existing_releases(self):
        self.manager.install("0.2.0")
        (self.home / "state/mail.db").write_bytes(b"KEEP_PENDING_MAIL")
        self.commands.fail_prepare = ("0.2.1", "smoke")
        with self.assertRaises(DistributionError) as failure:
            self.manager.apply("0.2.1")
        self.assertNotIn("SYNTHETIC_SECRET", str(failure.exception))
        self.assertEqual(self.manager.status()["current_version"], "0.2.0")
        self.assertFalse((self.home / "releases/0.2.1").exists())
        self.assertTrue((self.home / "releases/0.2.0").is_dir())
        self.assertEqual((self.home / "state/mail.db").read_bytes(), b"KEEP_PENDING_MAIL")

    def test_unverified_assets_bad_hash_and_wrong_context_fail_before_execution(self):
        self.commands.fail_verify = True
        with self.assertRaises(DistributionError) as failure:
            self.manager.install("0.2.0")
        self.assertNotIn("SYNTHETIC_SECRET", str(failure.exception))
        self.assertFalse(any(call[0][0] == "uv" for call in self.commands.calls))
        self.commands.fail_verify = False
        metadata = json.loads(self.commands.releases["0.2.0"][MANIFEST])
        metadata["assets"]["we-are-agents-v0.2.0.tar.gz"]["sha256"] = "0" * 64
        self.commands.releases["0.2.0"][MANIFEST] = json.dumps(metadata).encode()
        with self.assertRaisesRegex(DistributionError, "digest or size"):
            self.manager.install("0.2.0")
        metadata["repository"] = "untrusted/repository"
        self.commands.releases["0.2.0"][MANIFEST] = json.dumps(metadata).encode()
        with self.assertRaisesRegex(DistributionError, "repository"):
            self.manager.install("0.2.0")
        self.assertFalse(any(call[0][0] == "uv" for call in self.commands.calls))
        self.assertFalse((self.home / "current").exists())

    def test_unsafe_archive_members_never_escape_or_become_current(self):
        entries = []
        for name in ("/tmp/waa-escape", "we-are-agents-v0.2.0/../../escape",
                     "wrong-root/file", "we-are-agents-v0.2.0/private/credential",
                     "we-are-agents-v0.2.0/waa/cli.py"):
            entry = tarfile.TarInfo(name)
            entry.size = 1
            entries.append((entry, b"x"))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
            entry = tarfile.TarInfo("we-are-agents-v0.2.0/unsafe")
            entry.type, entry.linkname = kind, "/tmp/waa-escape"
            entries.append((entry, b""))
        for entry in entries:
            with self.subTest(name=entry[0].name, type=entry[0].type):
                self.commands.releases["0.2.0"] = release(extra=entry)
                with self.assertRaisesRegex(DistributionError, "unsafe paths"):
                    self.manager.install("0.2.0")
                self.assertFalse((self.home / "current").exists())
                self.assertFalse((self.home / "releases/0.2.0").exists())
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse(any(call[0][0] == "uv" for call in self.commands.calls))

    def test_engine_or_state_changes_need_reviewed_migration_and_preserve_current(self):
        self.manager.install("0.2.0")
        for kwargs in ({"reason": "v0.13.0"}, {"state_format": 2}):
            with self.subTest(kwargs=kwargs):
                self.commands.releases["0.2.1"] = release("0.2.1", **kwargs)
                with self.assertRaisesRegex(DistributionError, "reviewed migration"):
                    self.manager.apply("0.2.1")
                self.assertEqual(self.manager.status()["current_version"], "0.2.0")
                self.assertFalse((self.home / "releases/0.2.1").exists())

    def test_changed_or_incomplete_local_payload_is_not_activated(self):
        self.manager.install("0.2.0")
        self.manager.apply("0.2.1")
        (self.home / "releases/0.2.0/waa/cli.py").write_text("changed source")
        with self.assertRaisesRegex(DistributionError, "payload changed"):
            self.manager.rollback("0.2.0")
        self.assertEqual(self.manager.status()["current_version"], "0.2.1")
        incomplete = self.home / "releases/0.2.2"
        incomplete.mkdir()
        sentinel = incomplete / "owner-file"
        sentinel.write_bytes(b"DO_NOT_REMOVE")
        with self.assertRaises(DistributionError):
            self.manager.apply("0.2.2")
        self.assertEqual(sentinel.read_bytes(), b"DO_NOT_REMOVE")
        self.assertEqual(self.manager.status()["current_version"], "0.2.1")

    def test_nonblocking_lock_refuses_concurrent_mutation_before_remote_operations(self):
        self.manager.install("0.2.0")
        count = len(self.commands.calls)
        with (self.home / "update.lock").open("r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DistributionError, "in progress"):
                self.manager.apply("0.2.1")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertEqual(len(self.commands.calls), count)
        self.assertEqual(self.manager.status()["current_version"], "0.2.0")

    def test_check_reports_available_version_and_compatibility_without_activation(self):
        self.manager.install("0.2.0")
        result = self.manager.check()
        self.assertTrue(result["update_available"])
        self.assertTrue(result["compatible"])
        self.assertFalse(result["automatic_apply"])
        self.assertEqual(self.manager.status()["current_version"], "0.2.0")
        self.assertFalse((self.home / "releases/0.2.1").exists())
        self.commands.releases["0.2.1"] = release("0.2.1", state_format=2)
        self.assertFalse(self.manager.check()["compatible"])

    def test_notice_is_daily_best_effort_and_opt_out_keeps_standard_output_clean(self):
        self.manager.install("0.2.0")
        result = {"update_available": True, "latest_version": "0.2.1"}
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(Manager, "check", return_value=result) as check:
            with patch("waa.distribution.time.time", return_value=100000), redirect_stdout(output), redirect_stderr(errors):
                maybe_notice(self.home)
                maybe_notice(self.home)
            self.assertEqual(check.call_count, 1)
            with patch.dict(os.environ, {"WAA_NO_UPDATE_CHECK": "1"}):
                maybe_notice(self.home)
            self.assertEqual(check.call_count, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue().count("0.2.1 is available"), 1)
        cache = self.home / "last-update-check.json"
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        cache.write_text("not json")
        with patch.object(Manager, "check", side_effect=RuntimeError("SYNTHETIC_SECRET")):
            with redirect_stderr(errors):
                maybe_notice(self.home)
        self.assertNotIn("SYNTHETIC_SECRET", errors.getvalue())

    def test_home_selection_stable_versions_and_machine_readable_cli_status(self):
        with patch.dict(os.environ, {"WAA_HOME": str(self.home)}):
            self.assertEqual(default_home(), self.home)
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg")}):
            with patch.dict(os.environ) as environment:
                environment.pop("WAA_HOME", None)
                self.assertEqual(default_home(), self.root / "xdg/we-are-agents")
        for version in ("latest", "v0.2.0", "../0.2.0", "0.2.0-rc.1", "01.2.0"):
            with self.subTest(version=version), self.assertRaises(DistributionError):
                self.manager.install(version)
        output = io.StringIO()
        with redirect_stdout(output):
            main(["status", "--home", str(self.home)])
        self.assertFalse(json.loads(output.getvalue())["installed"])
        metadata = json.loads(release()[MANIFEST])
        metadata["compatibility"]["state_format"] = True
        with self.assertRaises(DistributionError):
            manifest(metadata, "0.2.0")


if __name__ == "__main__":
    unittest.main()
