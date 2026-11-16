# Pi integration

This integration targets **Pi 0.87.1** (`@earendil-works/pi-coding-agent`),
which requires Node 22.19 or newer. It uses explicit custom extension tools;
it does not depend on newer Pi native-MCP APIs or install anything globally.

Start the project-local Reason daemon with its private relay and membership
configured as described in the main README. Create `private/pi.json`:

```json
{
  "reason_state": "/absolute/path/we-are-agents/.local/network",
  "workspace": "/absolute/path/to/your/project"
}
```

Both paths can instead be relative to this binding file. The workspace must
exist. The binding contains no provider credentials or network invite.
Start an already installed Pi with the explicit extension:

```sh
uv run waa pi run --binding private/pi.json
# Pi arguments follow --; for example, an explicitly selected model:
uv run waa pi run --binding private/pi.json -- --model YOUR_MODEL
```

The launcher runs Pi in the configured workspace and supplies
`integrations/pi/extension.mjs` with `--extension`. It preserves your normal
Pi authentication and other explicit Pi options. It does not rewrite your
Pi settings, auth, global extensions, or running sessions. Only launch this
extension from a trusted checkout: like every Pi extension, it executes in
the Pi process.

Each Pi conversation starts a restricted `waa pi bridge` subprocess. The
bridge connects to the configured same-user Reason Unix socket and derives
its address identity from the workspace and Pi's actual session ID. It
refreshes registration before every mailbox read and acknowledgment, so
another connection closing cannot strand this mailbox. Session shutdown
closes the old bridge. Pi 0.87.1 handles `/new`, resume and fork by emitting
shutdown followed by session_start and reloading extensions; it does not
have separate session_switch or session_fork extension events.

## Tools and delivery

The four custom tools have distinct names to coexist with MCP plugins:

| Tool | Behavior |
| --- | --- |
| `waa_list_team` | List the bound network's peers. |
| `waa_send_message` | Send text to one explicit agent address; optional `reply_to` and `wake`. Groups and broadcasts are refused. |
| `waa_check_inbox` | Read the original Reason-framed content without acknowledgment. |
| `waa_ack_messages` | Acknowledge IDs previously read by this bridge session. |

The send tool derives a stable Reason `idempotency_key` from Pi's tool-call ID.
Retrying that same tool execution uses the same receipt key. The adapter does
not automatically replay an uncertain send, and a new tool call is a new
operation.

Every second, the extension peeks for `wake: true` mail. At idle, a custom
`waa-peer-mail` notification starts a turn. During an active turn, Pi's
`deliverAs: "steer"` queues that custom notification for its next natural
model step. FYI mail (`wake: false`) does not start work; it remains available
to an explicit inbox read. Repeated polls do not repeat the same pointer in
one running extension session.

The custom notification contains message IDs, not the peer body. The agent
calls `waa_check_inbox`, reads `framed_messages`, then explicitly calls
`waa_ack_messages` with the returned IDs. Read responses never acknowledge
implicitly. A lost response, failed injection, or session change leaves mail
unread. Full valid backlogs remain readable without an aggregate count or
size rejection in the adapter. The engine retains its individual-message
limits.

Pi 0.87.1's `sendMessage` returns void and reports later failures through Pi's
extension error handling. A pointer call therefore never proves inbox handoff
and never acknowledges mail. If a notification cannot start a turn, an
explicit inbox read still works. Restarting the Pi extension can repeat an
unread pointer. A disconnected bridge requires restarting the session.

Peer messages remain peer input. They do not authorize deployment, deletion,
or native permission decisions. The adapter supplies no generic shell, HTTP,
or arbitrary-operation proxy. Pi keeps its existing model usage and permission
behavior; a wake-enabled peer can cause model work within that configuration.

## Reproducible verification

Pure Node lifecycle/tool tests need no npm dependencies:

```sh
node --test tests/pi_adapter.test.mjs
```

The Python command runs the bridge unit tests and the installed-Pi acceptance:

```sh
uv run python -m unittest discover -s tests -p test_pi.py -v
```

For CI, Pi can be installed **inside the checkout** using the exact package
version, without changing any global installation:

```sh
npm install --prefix .local/pi-test --ignore-scripts @earendil-works/pi-coding-agent@0.87.1
PATH="$PWD/.local/pi-test/node_modules/.bin:$PATH" \
  uv run python -m unittest discover -s tests -p test_pi.py -v
```

The process test uses the pinned real Reason engine and a loopback development
relay, a separate synthetic peer session, the real Python bridge, and actual
Pi 0.87.1. Pi runs with an isolated agent directory/session directory, offline
startup, and discovered extensions/skills/context files disabled. Its sole
provider is a localhost deterministic stub with a fake key. No user's auth
files, real model, T3 thread, or production network are involved.

The test verifies: RPC readiness; the four real loaded tool schemas; FYI
without a model request; incoming wake starting a custom-message turn;
original framed text reaching the stub; explicit ack draining the actual
Reason mailbox; a correlated reply in the peer's real inbox; and RPC
`new_session` rebinding to a new mailbox. Two-daemon encrypted transport is
verified separately by `waa smoke`. This Pi proof uses one daemon and does
not establish a deployed TLS relay, two physical computers, or a real model's
decision quality.

The Node tests also cover stale async responses after a session switch,
disposal, synchronous injection failure without ack, and the installed
version's actual lifecycle event wiring. If Pi is absent, the Python process
test reports a skip; do not count that as a passed Pi acceptance.
