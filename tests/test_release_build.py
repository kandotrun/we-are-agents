from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_release.py"
SPEC = importlib.util.spec_from_file_location("waa_release_builder", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="waa-release-build-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Release Test")
        self.git("config", "user.email", "release-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.write("pyproject.toml", '[project]\nname="we-are-agents"\nversion="0.2.0"\nrequires-python=">=3.11"\n')
        self.write("uv.lock", 'version = 1\n')
        self.write("upstream.lock.json", json.dumps({"tools": {"reason-network": {"version": "v0.12.17"},
                                                                 "iroh-relay": {"version": "v1.4.1"}}}))
        self.write("waa/cli.py", 'print("synthetic client")\n')
        self.write("waa/distribution.py", '"""Synthetic standalone installer."""\nprint("install")\n')
        self.write("integrations/pi/extension.mjs", 'import "./adapter.mjs";\n')
        self.write("integrations/pi/adapter.mjs", 'export const adapter = "synthetic";\n')
        self.write("docs/updates.md", "Synthetic release instructions.\n")
        self.write("examples/systemd/waa-relay.service", "[Service]\nExecStart=/synthetic/iroh-relay\n")
        self.write("README.md", "Synthetic distribution fixture.\n")
        self.write("LICENSE", "Synthetic license fixture.\n")
        self.commit()

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.source), *arguments], stderr=subprocess.DEVNULL,
                                       text=True).strip()

    def write(self, name: str, text: str) -> Path:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def commit(self) -> None:
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic release fixture")

    def build(self, name="output", *, tag=None):
        output = self.root / name
        manifest = builder.build_release(self.source, output, tag=tag)
        return output, manifest

    def archive_files(self, output: Path, manifest: dict) -> dict[str, bytes]:
        with tarfile.open(output / f"we-are-agents-{manifest['tag']}.tar.gz", "r:gz") as archive:
            prefix = f"we-are-agents-{manifest['tag']}/"
            return {member.name.removeprefix(prefix): archive.extractfile(member).read()
                    for member in archive.getmembers()}

    def test_assets_are_complete_reproducible_and_match_the_committed_source(self) -> None:
        first, manifest = self.build(tag="v0.2.0")
        second, repeated = self.build("repeated", tag="v0.2.0")
        self.assertEqual(manifest, repeated)
        self.assertEqual(set(path.name for path in first.iterdir()),
                         {"release-manifest.json", "waa-install.py", "we-are-agents-v0.2.0.tar.gz"})
        for path in first.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes())
        for name, metadata in manifest["assets"].items():
            content = (first / name).read_bytes()
            self.assertEqual(metadata, {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
        files = self.archive_files(first, manifest)
        self.assertTrue(builder.REQUIRED <= files.keys())
        self.assertIn("docs/updates.md", files)
        self.assertIn("examples/systemd/waa-relay.service", files)
        self.assertIn("LICENSE", files)
        self.assertEqual(files["integrations/pi/extension.mjs"], (self.source / "integrations/pi/extension.mjs").read_bytes())
        self.assertEqual((first / "waa-install.py").read_bytes(), files["waa/distribution.py"])
        descriptor = json.loads(files["release.json"])
        self.assertEqual(descriptor, {name: value for name, value in manifest.items() if name != "assets"})
        self.assertEqual(descriptor["source_commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(descriptor["compatibility"]["reason_network"], "v0.12.17")
        self.assertEqual(descriptor["compatibility"]["adapters"],
                         {"pi": ["0.87.1"], "openclaw": ["2026.11.4"]})
        with tarfile.open(first / "we-are-agents-v0.2.0.tar.gz", "r:gz") as archive:
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                self.assertEqual((member.uname, member.gname), ("", ""))

    def test_untracked_and_forbidden_tracked_data_are_not_distributed(self) -> None:
        self.write("private/token.txt", "SYNTHETIC PRIVATE TOKEN")
        self.write("waa/private/config.py", "SYNTHETIC PRIVATE CONFIG")
        self.write(".local/mail.txt", "SYNTHETIC MAIL")
        self.write(".tools/cache.bin", "SYNTHETIC CACHE")
        self.write(".venv/credentials.txt", "SYNTHETIC ENV")
        self.write(".env", "SYNTHETIC SECRET")
        self.commit()
        self.write("waa/untracked.py", "UNTRACKED SOURCE MUST NOT SHIP")
        self.write("docs/untracked.md", "UNTRACKED DOCUMENT MUST NOT SHIP")
        output, manifest = self.build()
        files = self.archive_files(output, manifest)
        self.assertFalse(any(name.startswith(("private/", ".local/", ".tools/", ".venv/")) for name in files))
        self.assertNotIn("waa/private/config.py", files)
        self.assertNotIn(".env", files)
        self.assertNotIn("waa/untracked.py", files)
        self.assertNotIn("docs/untracked.md", files)
        self.assertNotIn(b"SYNTHETIC PRIVATE", b"".join(files.values()))

    def test_dirty_or_staged_source_and_wrong_tag_are_refused(self) -> None:
        with self.assertRaisesRegex(builder.BuildError, "tag does not match"):
            self.build(tag="v0.3.0")
        self.write("waa/cli.py", "CHANGED WORKTREE")
        with self.assertRaisesRegex(builder.BuildError, "Commit tracked"):
            self.build()
        self.git("add", "waa/cli.py")
        with self.assertRaisesRegex(builder.BuildError, "Commit tracked"):
            self.build()

    def test_prerelease_or_unsafe_versions_are_refused(self) -> None:
        for version in ("0.2.0rc1", "0.2.0-rc.1", "01.2.0", "../0.2.0"):
            with self.subTest(version=version):
                self.write("pyproject.toml", '[project]\nversion=' + json.dumps(version) + '\nrequires-python=">=3.11"\n')
                self.commit()
                with self.assertRaisesRegex(builder.BuildError, "stable X.Y.Z"):
                    self.build()

    def test_selected_symlink_and_missing_required_asset_are_refused(self) -> None:
        target = self.root / "outside.txt"
        target.write_text("SYNTHETIC OUTSIDE PAYLOAD")
        (self.source / "waa/link.py").symlink_to(target)
        self.commit()
        with self.assertRaisesRegex(builder.BuildError, "regular tracked"):
            self.build()
        (self.source / "waa/link.py").unlink()
        (self.source / "integrations/pi/adapter.mjs").unlink()
        self.commit()
        with self.assertRaisesRegex(builder.BuildError, "missing required"):
            self.build()

    def test_existing_asset_is_preserved_without_partial_new_outputs(self) -> None:
        output = self.root / "output"
        output.mkdir()
        (output / "release-manifest.json").write_text("EXISTING ARTIFACT")
        with self.assertRaisesRegex(builder.BuildError, "existing release assets"):
            builder.build_release(self.source, output)
        self.assertEqual((output / "release-manifest.json").read_text(), "EXISTING ARTIFACT")
        self.assertEqual(set(path.name for path in output.iterdir()), {"release-manifest.json"})

    def test_archive_path_escape_is_refused(self) -> None:
        for path in ("../escape.py", "/absolute.py", "waa/../../escape.py", "waa\\escape.py", "waa//escape.py"):
            with self.subTest(path=path), self.assertRaisesRegex(builder.BuildError, "archive path"):
                builder._archive("we-are-agents-v0.2.0", {path: (0o644, b"synthetic")})


if __name__ == "__main__":
    unittest.main()
