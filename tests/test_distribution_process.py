"""Real release installation with a synthetic local GitHub release service.

The release builder, downloaded standalone installer, uv environments, pinned
binary bootstrap, smoke checks and stable launcher all run as real processes.
Only GitHub metadata, downloads and immutable-release receipts are simulated;
this fixture does not establish a published release or GitHub attestation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"private", ".local", ".tools", ".venv", ".git", "__pycache__"}


class DistributionProcessTests(unittest.TestCase):
    def test_standalone_install_update_and_rollback_preserve_private_state(self) -> None:
        if shutil.which("uv") is None or shutil.which("git") is None:
            self.skipTest("The release process acceptance needs uv and git")
        with tempfile.TemporaryDirectory(prefix="waa-release-process-", dir="/tmp") as temporary:
            root = Path(temporary).resolve()
            source, remote = root / "source", root / "releases"
            home, shim = root / "installed home", root / "commands"
            source.mkdir()
            remote.mkdir()
            shim.mkdir()
            env = {"PATH": str(shim) + os.pathsep + os.environ["PATH"],
                   "WAA_HOME": str(home), "WAA_NO_UPDATE_CHECK": "1",
                   "WAA_FIXTURE_RELEASES": str(remote),
                   "WAA_FIXTURE_GH_LOG": str(root / "gh.jsonl"),
                   "WAA_FIXTURE_UV_LOG": str(root / "uv.log"),
                   "XDG_CONFIG_HOME": str(root / "empty-config"),
                   "XDG_DATA_HOME": str(root / "empty-data"),
                   "UV_NO_PROGRESS": "1", "PYTHONNOUSERSITE": "1",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
            if os.environ.get("UV_CACHE_DIR"):
                env["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]

            # The real uv binary performs every environment installation. Keep
            # its diagnostics inside the fixture. Each candidate starts with a
            # cold binary cache; actual bootstrap downloads and verifies its pins.
            real_uv = shutil.which("uv")
            uv_wrapper = r'''
import os, pathlib, subprocess, sys
with pathlib.Path(os.environ['WAA_FIXTURE_UV_LOG']).open('w') as log:
    result=subprocess.run([REAL_UV,*sys.argv[1:]],stderr=log)
sys.stderr.write(pathlib.Path(os.environ['WAA_FIXTURE_UV_LOG']).read_text())
raise SystemExit(result.returncode)
'''
            (shim / "uv").write_text("#!" + sys.executable + "\nREAL_UV=" + repr(real_uv) + "\n" + uv_wrapper)
            (shim / "uv").chmod(0o700)

            def run(argv: list[str], *, cwd: Path | None = None,
                    timeout: int = 240, success: bool = True) -> subprocess.CompletedProcess[str]:
                result = subprocess.run(argv, cwd=cwd or root, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
                if success:
                    diagnostics = ""
                    if result.returncode:
                        for name in ("gh.jsonl", "uv.log", "installer.jsonl"):
                            path = root / name
                            if path.exists():
                                diagnostics += "\n" + name + ":\n" + path.read_text()[-6000:]
                    self.assertEqual(result.returncode, 0,
                                     "Release fixture command failed: " + repr(argv[:4])
                                     + "\n" + result.stdout[-4000:] + result.stderr[-6000:] + diagnostics)
                return result

            tracked = run(["git", "-C", str(ROOT), "ls-files", "-z"]).stdout.split("\0")
            for name in filter(None, tracked):
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or set(relative.parts) & FORBIDDEN:
                    continue
                original = ROOT / relative
                self.assertFalse(original.is_symlink(), "Public source fixture must not follow symlinks")
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, destination)
            self.assertTrue((source / "waa/distribution.py").is_file(),
                            "Stage the new distribution source before running release process acceptance")
            self.assertTrue((source / "scripts/build_release.py").is_file(),
                            "Stage the release builder before running release process acceptance")
            run(["git", "init", "--template=", "-b", "main", str(source)])

            def commit(message: str) -> None:
                run(["git", "add", "."], cwd=source)
                run(["git", "-c", "commit.gpgSign=false", "-c", "core.hooksPath=" + os.devnull,
                     "-c", "user.name=Synthetic Release", "-c", "user.email=release@example.test",
                     "commit", "-m", message], cwd=source)

            def build(version: str) -> None:
                run([sys.executable, str(source / "scripts/build_release.py"),
                     "--source", str(source), "--output", str(remote / ("v" + version)),
                     "--tag", "v" + version])

            version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
            numbers = [int(part) for part in version.split(".")]
            updated = ".".join(map(str, [*numbers[:2], numbers[2] + 1]))
            incompatible = ".".join(map(str, [*numbers[:2], numbers[2] + 2]))

            def set_version(value: str) -> None:
                project = source / "pyproject.toml"
                old = tomllib.loads(project.read_text())["project"]["version"]
                project.write_text(project.read_text().replace('version = "' + old + '"',
                                                               'version = "' + value + '"', 1))
                lock = source / "uv.lock"
                changed, count = re.subn(r'(?m)(^name = "we-are-agents"\nversion = ")[^"]+("$)',
                                         lambda match: match[1] + value + match[2], lock.read_text())
                self.assertEqual(count, 1)
                lock.write_text(changed)

            commit("Synthetic initial release")
            build(version)
            set_version(updated)
            commit("Synthetic compatible update")
            build(updated)
            set_version(incompatible)
            upstream_path = source / "upstream.lock.json"
            upstream = json.loads(upstream_path.read_text())
            upstream["tools"]["reason-network"]["version"] = "v9.9.9"
            upstream_path.write_text(json.dumps(upstream))
            commit("Synthetic incompatible engine update")
            build(incompatible)
            (remote / "latest").write_text("v" + updated)

            # This gh executable is a local protocol fixture, not a replacement
            # for real release attestation in deployment. It checks every local
            # downloaded asset against the bytes built for its selected tag.
            fake_gh = r'''import hashlib, json, os, shutil, sys
from pathlib import Path
arguments = sys.argv[1:]
remote = Path(os.environ['WAA_FIXTURE_RELEASES'])
with Path(os.environ['WAA_FIXTURE_GH_LOG']).open('a') as log:
    log.write(json.dumps(arguments) + '\n')
def option(name):
    return arguments[arguments.index(name) + 1]
if option('--repo') != 'kandotrun/we-are-agents':
    raise SystemExit('Synthetic fixture repository mismatch')
operation = arguments[:2]
if operation == ['release', 'view']:
    print(json.dumps({'tagName': (remote / 'latest').read_text(), 'isDraft': False, 'isPrerelease': False}))
elif operation == ['release', 'verify']:
    if not (remote / arguments[2] / 'release-manifest.json').is_file():
        raise SystemExit('Synthetic fixture release missing')
    print('synthetic immutable-release receipt')
elif operation == ['release', 'download']:
    target = Path(option('--dir'))
    target.mkdir(parents=True, exist_ok=True)
    asset = option('--pattern')
    if Path(asset).name != asset:
        raise SystemExit('Synthetic fixture asset mismatch')
    shutil.copyfile(remote / arguments[2] / asset, target / asset)
elif operation == ['release', 'verify-asset']:
    artifact = Path(arguments[3])
    expected = remote / arguments[2] / artifact.name
    if hashlib.sha256(artifact.read_bytes()).digest() != hashlib.sha256(expected.read_bytes()).digest():
        raise SystemExit('Synthetic fixture asset differs')
    print('synthetic immutable-asset receipt')
else:
    raise SystemExit('Unsupported synthetic GitHub invocation')
'''
            (shim / "gh").write_text("#!" + sys.executable + "\n" + fake_gh)
            (shim / "gh").chmod(0o700)
            installer = remote / ("v" + version) / "waa-install.py"
            self.assertEqual(installer.read_bytes(), (source / "waa/distribution.py").read_bytes())
            trace_installer = r'''
import json, pathlib, runpy, subprocess, sys
original_run=subprocess.run
log=pathlib.Path(sys.argv[1])
def traced_run(argv,*args,**kwargs):
    try:
        result=original_run(argv,*args,**kwargs)
    except Exception as error:
        with log.open('a') as output:
            output.write(json.dumps({'argv':list(map(str,argv)),'error':type(error).__name__})+'\n')
        raise
    with log.open('a') as output:
        output.write(json.dumps({'argv':list(map(str,argv)),'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})+'\n')
    return result
subprocess.run=traced_run
sys.argv=sys.argv[2:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
            installed = json.loads(run([sys.executable, "-I", "-c", trace_installer, str(root / "installer.jsonl"),
                                        str(installer), "install", "--version", version,
                                        "--home", str(home)]).stdout)
            self.assertEqual(installed["current_version"], version)
            launcher = home / "bin/waa"
            self.assertEqual(run([str(launcher), "--version"]).stdout.strip(), version)
            self.assertEqual((home / "current").resolve(), home / "releases" / version)

            def python(code: str) -> dict:
                result = run([str(home / "current/.venv/bin/python"), "-I", "-c", code, str(home)])
                return json.loads(result.stdout)

            interpreter = python("import json, pathlib, sys, waa; print(json.dumps({'prefix':str(pathlib.Path(sys.prefix).resolve()), 'module':str(pathlib.Path(waa.__file__).resolve())}))")
            self.assertTrue(Path(interpreter["module"]).is_relative_to(home / "releases" / version))
            self.assertEqual(Path(interpreter["prefix"]), home / "releases" / version / ".venv")
            endpoint = run([str(launcher), "network", "--relay", "https://relay.example.test", "--", "endpoint"]).stdout.strip()
            self.assertRegex(endpoint, r"^[0-9a-f]{64}$")
            self.assertTrue((home / "state/network/machine.key").is_file())
            initialized = python(r'''
import json, pathlib, sys
from waa.identity import IdentityGate, initialize_identity
from waa.mailbox import AgentMailbox, MailboxBinding
from waa.t3 import Binding, Journal
home=pathlib.Path(sys.argv[1])
profile=home/'config/identity/profile.json'
info=initialize_identity(profile.parent,owner_id='synthetic',owner_name='Synthetic',agent_name='Test')
gate=IdentityGate.from_file(profile)
envelope={'id':'original-admission','wake':True,'reply_to':None,'body':gate.sign('Synthetic state-preservation proof',sender='synthetic/sender',recipient='synthetic/receiver',reply_to=None,wake=True,idempotency_key='state-proof')}
assert len(gate.filter_inbox({'messages':[envelope],'text':'raw'},recipient='synthetic/receiver')['messages'])==1
(home/'state/envelope.json').write_text(json.dumps(envelope))
(home/'workspace/sentinel.txt').write_text('Keep this workspace through code updates.\n')
mailbox_path=home/'config/mailbox.json'
mailbox_path.write_text(json.dumps({'reason_state':str(home/'state/network'),'workspace':str(home/'workspace'),'identity':str(profile)}))
mailbox=AgentMailbox(MailboxBinding.load(mailbox_path),'agent:test:main',object())
binding_path=home/'config/t3.json'
binding_path.write_text(json.dumps({'reason_state':str(home/'state/network'),'environment_id':'synthetic-env','project_id':'synthetic-project','thread_id':'synthetic-thread','workspace':str(home/'workspace'),'mcp_url':'http://127.0.0.1:9/mcp','token_file':str(home/'config/absent-token'),'identity':str(profile)}))
binding=Binding.load(binding_path)
journal=Journal(binding)
operation,ids=journal.claim(['held-wake'])
journal.finish(operation,'unknown')
journal.close()
print(json.dumps({'public_key':info['public_key'],'profile':str(profile),'ledger':str(gate.ledger_path),'mailbox_key':mailbox.key,'session_key':binding.session_key,'binding_digest':binding.digest}))
mailbox.close(); gate.close()
''')
            protected = [path for folder in (home / "config", home / "state", home / "workspace")
                         for path in folder.rglob("*") if path.is_file()]
            before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
            checked = json.loads(run([str(launcher), "update", "check"]).stdout)
            self.assertIs(checked["update_available"], True)
            self.assertEqual((home / "current").resolve(), home / "releases" / version)
            applied = json.loads(run([str(launcher), "update", "apply", "--version", updated]).stdout)
            self.assertEqual(applied["current_version"], updated)
            self.assertIs(applied["restart_required"], True)
            self.assertEqual(run([str(launcher), "--version"]).stdout.strip(), updated)
            self.assertEqual((home / "current").resolve(), home / "releases" / updated)
            refused = run([str(launcher), "update", "apply", "--version", incompatible], success=False)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual((home / "current").resolve(), home / "releases" / updated)
            rolled_back = json.loads(run([str(launcher), "update", "rollback", "--version", version]).stdout)
            self.assertEqual(rolled_back["current_version"], version)
            self.assertEqual(run([str(launcher), "--version"]).stdout.strip(), version)
            self.assertEqual(before, {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected})
            self.assertEqual(endpoint, run([str(launcher), "network", "--relay", "https://relay.example.test", "--", "endpoint"]).stdout.strip())
            retained = python(r'''
import json, pathlib, sys
from waa.identity import IdentityGate, public_identity
from waa.mailbox import AgentMailbox, MailboxBinding
from waa.t3 import Binding, Journal
home=pathlib.Path(sys.argv[1]); profile=home/'config/identity/profile.json'
gate=IdentityGate.from_file(profile)
original=json.loads((home/'state/envelope.json').read_text())
copied={**original,'id':'copied-after-updates'}
assert gate.filter_inbox({'messages':[copied],'text':'raw'},recipient='synthetic/receiver')['quarantined']==[{'id':'copied-after-updates','reason':'replayed_message'}]
assert len(gate.filter_inbox({'messages':[original],'text':'raw'},recipient='synthetic/receiver')['messages'])==1
mailbox=AgentMailbox(MailboxBinding.load(home/'config/mailbox.json'),'agent:test:main',object())
binding=Binding.load(home/'config/t3.json'); journal=Journal(binding)
assert journal.claim(['held-wake']) is None
assert journal.db.execute('SELECT status FROM wakes').fetchone()[0]=='unknown'
journal.close()
print(json.dumps({'public_key':public_identity(profile)['public_key'],'profile':str(profile),'ledger':str(gate.ledger_path),'mailbox_key':mailbox.key,'session_key':binding.session_key,'binding_digest':binding.digest}))
mailbox.close(); gate.close()
''')
            self.assertEqual(retained, initialized)
            for expected in (version, updated):
                release = home / "releases" / expected
                self.assertTrue((release / ".waa-installed.json").is_file())
                self.assertTrue((release / ".venv/bin/waa").is_file())
                self.assertTrue((release / "integrations/pi/extension.mjs").is_file())
                self.assertFalse((release / ".local").exists())
            gh_calls = [json.loads(line) for line in (root / "gh.jsonl").read_text().splitlines()]
            for tag in ("v" + version, "v" + updated):
                self.assertTrue(any(call[:3] == ["release", "verify", tag] for call in gh_calls))
                self.assertTrue(any(call[:3] == ["release", "verify-asset", tag]
                                    and call[3].endswith("release-manifest.json") for call in gh_calls))
                self.assertTrue(any(call[:3] == ["release", "verify-asset", tag]
                                    and call[3].endswith(".tar.gz") for call in gh_calls))


if __name__ == "__main__":
    unittest.main()
