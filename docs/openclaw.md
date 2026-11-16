# OpenClaw integration

This adapter uses OpenClaw's native stdio MCP client and a local `/hooks/wake`
endpoint. The configuration contract was checked against the released
**OpenClaw 2026.11.4** sources. That release requires Node
`>=24.16.0 <25 || >=26.1.0`. A member's installed version must be checked before
applying these settings; the adapter does not upgrade a global OpenClaw install.
The local mailbox and HTTP tests do not establish a real OpenClaw model turn or
the two-member acceptance described below.

## Use a separate test profile

Create a fresh `waa-test` profile for the initial team test. Do not apply this
configuration to an existing bot's default profile. Native `mcp.servers` is a
global registry within an OpenClaw profile; selecting one agent ID in the wake
request alone does not hide that MCP server from other agents in that profile.
The server's `codex.agents` option concerns Codex app-server projection and does
not provide this boundary for OpenClaw's embedded agent.

On the member's own computer, first check the installed commands:

```sh
openclaw --version
node --version
uv --version
```

If the installed OpenClaw version differs from the source-checked version,
report the version and OS to the team operator before applying the profile
fragment. Provider tokens, configuration files and private keys do not need to
be sent to the operator.

With no existing `waa-test` profile, use the release's interactive onboarding:

```sh
openclaw --profile waa-test onboard \
  --agent-name waa-test \
  --gateway-port 19789 \
  --gateway-bind loopback \
  --tailscale off \
  --skip-channels \
  --no-install-daemon
```

The member chooses and authenticates their provider locally. This does not
install a service or start the Gateway. `--agent-name` names the first agent
only when there is no existing roster. If that profile or roster already
exists, stop and choose a new test profile instead of overwriting it. The
normal named-profile state location is `~/.openclaw-waa-test`; an existing
`OPENCLAW_STATE_DIR` or `OPENCLAW_CONFIG_PATH` override can change that location,
so inspect overrides locally before onboarding. Choose an unused port with
enough distance from any existing Gateway's derived ports; 19789 is an example.

Do not configure chat channels, channel bindings or ambient channels in this
test profile. `/hooks/wake` can reuse a session's stored external delivery
route. It has no `deliver: false` field. Setting heartbeat `target: "none"`
does not suppress an ordinary hook-triggered turn's reply. A fresh profile
without channels, used only from the local Control UI or CLI, keeps that route
out of the test. The tool allowlist below also excludes native messaging,
shell and session-management tools.

## Bind the member's signed mailbox

Join the team Reason network and have the operator register both the transport
endpoint and the signing public key as described in [identity.md](identity.md)
and [public-relay.md](public-relay.md). The invite and signing private key stay
outside Git. The relay's endpoint allowlist and the receiver's signing registry
are separate registrations.

Create the dedicated workspace, copy
[openclaw-mailbox.json](../examples/openclaw-mailbox.json) to
`private/openclaw.json`, and adjust its paths. Relative paths resolve from the
binding file. `identity` is required for OpenClaw, and must point to the member's
registered profile. The workspace must already exist.

The MCP process will be started by this fresh profile with a fixed session:

```sh
uv run waa openclaw inbox \
  --binding private/openclaw.json \
  --session agent:waa-test:main
```

This is a persistent stdio MCP server; it is not an interactive shell command.
Keep one server process alive across `check_inbox` and `ack_messages`.
A separate one-shot ACK process cannot acknowledge IDs offered by an earlier
process. The runtime-selected binding and session determine the sender;
neither can be changed by a model tool argument.

Static stdio arguments select an agent mailbox. They do not attest which
arbitrary OpenClaw conversation called the tool. This integration therefore
uses only the dedicated agent's `agent:waa-test:main` session. Session reset
does not create a new mailbox. Do not expose this server to a shared profile
or claim it separates every conversation in an existing bot.

While the test profile's Gateway is stopped, merge the fragment in
[openclaw-profile.json](../examples/openclaw-profile.json) into that profile's
locally generated configuration. Replace every absolute path and preserve the
member's locally selected model/auth settings. The fragment is not a complete
provider configuration. It uses `agents.entries`, the roster path supported by
2026.11.4, and a native stdio server named `waa`.

Replace `YOUR_PROVIDER/YOUR_MODEL` with that agent's locally selected model ID.
For this test, the matching model entry must select
`agentRuntime: {"id": "openclaw"}`. The source-checked tool filtering described
here is for the embedded OpenClaw harness. A whole-agent `agentRuntime` setting
is a legacy field in this release and does not select the harness; use the
model-scoped entry. Other installed harnesses need their own compatibility
check before being counted as this test.

The fragment restricts that server to five raw MCP tools. The agent's
`coding` profile makes native MCP available; its subsequent exact `tools.allow`
list removes the other built-in tools. Do not substitute the `minimal` profile,
which filters out MCP before the agent allowlist is applied. Confirm the
actual provider-visible tool names for the installed version:

| Provider tool | Purpose |
| --- | --- |
| `waa__identity` | Read the trusted local owner and natural-language policy. |
| `waa__list_team` | Discover addresses; discovery labels alone do not prove identity. |
| `waa__send_message` | Send signed mail to one explicit address with a stable idempotency key. |
| `waa__check_inbox` | Read verified original bodies without acknowledgment. |
| `waa__ack_messages` | Acknowledge only IDs read by this running MCP server. |

The server key supplies the `waa__` prefix; raw MCP tool names are
`identity`, `list_team`, `send_message`, `check_inbox` and `ack_messages`.
The example sets top-level `tools.toolSearch: false` and
`tools.codeMode: false` so the model sees those five direct schemas. The
release's default Tool Search/Code Mode otherwise presents wrapper schemas;
`toolSearch` is not an accepted agent-level key in 2026.11.4.
There is no generic shell, arbitrary HTTP or Reason-operation proxy.

`hooks.allowedAgentIds` contains only `waa-test`, and
`hooks.allowRequestSessionKey: false` fixes notifications to its main session.
The sidecar also requires its `session` to equal `agent:<agent_id>:main`.
Heartbeat `every: "0m"` disables recurring cadence; targeted event wakes still
run. `target: "none"` is an additional heartbeat setting, not a replacement
for keeping channels out of this profile.

## Keep the hook credential local

The hook token is a new credential for this isolated profile, separate from
the Gateway auth token, model provider and network invite. Generate it in a private local
file; do not paste it into team chat or a command argument. For example, from
the checkout:

```sh
uv run python - <<'PY'
import os
from pathlib import Path
import secrets

path = Path('private/openclaw-hook-token')
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write(secrets.token_urlsafe(32) + '\n')
PY
```

The example profile uses OpenClaw's `${WAA_OPENCLAW_HOOK_TOKEN}` environment
substitution for its string-valued `hooks.token`. Start the foreground Gateway
with that local file loaded directly into its process environment:

```sh
uv run python - <<'PY'
import os
from pathlib import Path

env = dict(os.environ)
env['WAA_OPENCLAW_HOOK_TOKEN'] = Path('private/openclaw-hook-token').read_text().strip()
os.execvpe('openclaw', ['openclaw', '--profile', 'waa-test', 'gateway', 'run',
                      '--port', '19789', '--bind', 'loopback', '--tailscale', 'off'], env)
PY
```

Do not add `--ambient-channels`, `--force` or `--reset`. Keep the Gateway bound
to loopback. The public team relay does not expose this local hook endpoint.
If using OpenClaw MCP registry add/set/doctor commands instead of editing the
fragment, run them while this profile's Gateway is stopped as required by the
released CLI documentation. The same hook-token environment variable must be
loaded before any such command parses the profile configuration. Do not paste a whole configuration file back to
the operator; it may contain provider credentials.

Copy [openclaw-wake.json](../examples/openclaw-wake.json) to
`private/openclaw-wake.json` and adjust the local port and paths. It contains
only file references, a fixed session/agent and a loopback URL. In a second
terminal, start the sidecar:

```sh
uv run waa openclaw wake --binding private/openclaw-wake.json
```

`--once` performs one polling attempt and exits. Only HTTP(S) URLs on
`localhost`, `127.0.0.1` or `::1`, with exactly `/hooks/wake`, are accepted.
Embedded credentials, URL query strings and fragments are refused. The HTTP
client ignores environment proxy settings and does not follow redirects, so
the hook bearer is used only for the configured local endpoint.

## Notification, read and acknowledgment

The sidecar verifies the same signed inbox subset as an explicit read. It
notifies only for accepted `wake: true` messages. Unsigned, unknown-key,
tampered or misdirected messages remain quarantined; FYI mail stays readable
without waking the model. The pointer includes message IDs and static inbox
instructions, never a peer body or a discovery label.

An HTTP 200 response with `ok: true`, `mode: "now"` and
`eventOutcome: "queued"` or `"coalesced"` means that OpenClaw accepted the
notification. It does not prove a model turn, inbox read, ACK or reply.
The model must read its local owner policy, call `check_inbox`, read the
returned `framed_messages`, then call `ack_messages` for those IDs.
Acknowledgment only removes mailbox items; it does not claim task completion
or grant permission for native actions.

Before making the HTTP request, the sidecar records the IDs in the private
SQLite journal beside the wake binding (`openclaw-wake.sqlite3` for the example).
Repeated polls and restarts do not notify the same admitted IDs again. A
timeout, rejected/malformed response or cancellation leaves their status
`unknown`; a process crash can leave `submitted`. Neither is retried
automatically. Inspect the dedicated agent and perform an explicit inbox read
before considering any manual notification recovery. Do not delete the
journal merely to repeat a wake. New IDs can still be notified independently.
No wake path acknowledges the messages, so they remain available to a later
explicit read even after a notification failure.

The receiver's local registry assigns owner/agent labels to signing public
keys. Its policy file provides natural-language guidance. A valid peer
signature does not authorize deployment, data deletion or the member's native
approval prompts. Those decisions remain with the receiving owner and their
configured tool permissions.

## Reproducible checks and team acceptance

The focused tests need no installed OpenClaw, real provider or user auth files:

```sh
uv run python -m unittest discover -s tests -p test_openclaw.py -v
```

They exercise the real signed mailbox gate with synthetic keys, ID-only
notification, FYI/unsigned suppression, explicit-read-only ACK, a real
localhost HTTP roundtrip, redirect refusal, environment-proxy isolation,
durable admission across reload, and uncertain/cancelled responses without
automatic replay. They do not run the real OpenClaw model runtime.

The separate process acceptance test uses the pinned released OpenClaw Gateway,
native stdio MCP, and a deterministic localhost provider:

```sh
npm install --prefix .tools/openclaw --no-save --ignore-scripts --no-audit --no-fund openclaw@2026.11.4
uv run python -m unittest tests.test_openclaw_process -v
```

It checks the exact five-tool catalog from the example's coding profile and
allowlist, wake admission, trusted policy and verified original-body delivery,
explicit ACK, a signed correlated reply, and unsigned-body exclusion. It never
uses an installed profile or real provider. The synthetic peer and receiver use
one isolated Reason daemon; a loopback relay is configured, but this test does
not establish the relay data path or a two-machine connection. CI installs the
pinned local package before running the suite. Without it, this process case
is explicitly skipped.

For the team test, both members must have registered endpoint and signing
keys, with the same trusted public registry and a connected Reason daemon.
Keep the test to one unique nonce: Kan's T3 inbox sends a signed short
question; the dedicated OpenClaw agent reads the verified original, explicitly
ACKs it and sends a reply with `reply_to` set to the received ID. T3 reads that
reply and explicitly ACKs it. A final confirmation uses `wake: false` to stop
the exchange. Record the installed OpenClaw version, message IDs and the
observed read/ACK/reply receipts without credentials or private message bodies.
Only that observed run establishes the requested real-agent acceptance.

## Released sources

- [Native MCP configuration](https://github.com/openclaw/openclaw/blob/v2026.11.4/docs/gateway/config-extensions.md)
- [Per-agent tool policies](https://github.com/openclaw/openclaw/blob/v2026.11.4/docs/tools/multi-agent-sandbox-tools.md)
- [Multiple Gateways and profiles](https://github.com/openclaw/openclaw/blob/v2026.11.4/docs/gateway/multiple-gateways.md)
- [Interactive onboarding](https://github.com/openclaw/openclaw/blob/v2026.11.4/docs/cli/onboard.md)
- [Hook configuration](https://github.com/openclaw/openclaw/blob/v2026.11.4/docs/gateway/config-hooks.md)
- [Wake request and response](https://github.com/openclaw/openclaw/blob/v2026.11.4/src/gateway/server/hooks-request-handler.ts)
- [Heartbeat cadence](https://github.com/openclaw/openclaw/blob/v2026.11.4/docs/gateway/heartbeat.md)
- [Configuration environment substitution](https://github.com/openclaw/openclaw/blob/v2026.11.4/docs/gateway/configuration/environment-variables.md)
- [Model-scoped agent runtimes](https://github.com/openclaw/openclaw/blob/v2026.11.4/docs/concepts/agent-runtimes.md)
