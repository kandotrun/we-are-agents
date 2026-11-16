import test from "node:test";
import assert from "node:assert/strict";
import { PiAdapter, installTools } from "../integrations/pi/adapter.mjs";
import installExtension from "../integrations/pi/extension.mjs";

const context = session => ({ sessionManager: { getSessionId: () => session } });
function fixture() {
  const calls = [], messages = [], tools = new Map();
  let wakeIds = [];
  const bridge = { async call(op, args) { calls.push([op, args]); return op === "poll" ? { wake_ids: wakeIds } : { framed_messages: "ORIGINAL" }; }, close() {} };
  const pi = { sendMessage(message, options) { messages.push([message, options]); }, registerTool(tool) { tools.set(tool.name, tool); } };
  const adapter = new PiAdapter(pi, () => bridge);
  adapter.start(context("session-one"));
  installTools(pi, adapter);
  return { calls, messages, tools, adapter, bridge, pi, wake(ids) { wakeIds = ids; } };
}

test("FYI-only inbox never wakes; idle and busy notifications use custom steering", async () => {
  const f = fixture();
  await f.adapter.poll();
  assert.equal(f.messages.length, 0);
  f.wake(["mail-1"]);
  await f.adapter.poll();
  assert.equal(f.messages[0][0].customType, "waa-peer-mail");
  assert.equal(f.messages[0][1].triggerTurn, true);
  assert.equal(f.messages[0][1].deliverAs, "steer");
  await f.adapter.poll();
  assert.equal(f.messages.length, 1);
  f.wake(["mail-1", "busy-mail"]);
  await f.adapter.poll();
  assert.equal(f.messages.length, 2);
  assert.deepEqual(f.messages[1][0].details.messageIds, ["busy-mail"]);
  assert.equal(f.calls.some(([op]) => op === "ack_messages"), false);
});

test("injection failure never acknowledges and permits another pointer attempt", async () => {
  const f = fixture(); f.wake(["mail-1"]);
  f.pi.sendMessage = () => { throw new Error("injection refused"); };
  await assert.rejects(f.adapter.poll());
  assert.equal(f.adapter.active.announced.size, 0);
  assert.equal(f.calls.some(([op]) => op === "ack_messages"), false);
});

test("late poll response after session switch cannot inject into the replacement", async () => {
  const f = fixture();
  let resolve;
  f.bridge.call = () => new Promise(done => { resolve = done; });
  const pending = f.adapter.poll();
  f.adapter.start(context("session-two"));
  resolve({ wake_ids: ["stale-mail"] });
  await pending;
  assert.equal(f.messages.length, 0);
});

test("late tool result after disposal is refused; tools are explicit operations", async () => {
  const f = fixture();
  assert.deepEqual([...f.tools.keys()], ["waa_list_team", "waa_send_message", "waa_check_inbox", "waa_ack_messages"]);
  const response = await f.tools.get("waa_check_inbox").execute("call-1", {});
  assert.equal(JSON.parse(response.content[0].text).framed_messages, "ORIGINAL");
  await f.tools.get("waa_send_message").execute("call-2", { to: "peer/agent", message: "hello" });
  assert.equal(f.calls.at(-1)[0], "send_message");
  const firstKey = f.calls.at(-1)[1].idempotency_key;
  await f.tools.get("waa_send_message").execute("call-2", { to: "peer/agent", message: "hello" });
  assert.equal(f.calls.at(-1)[1].idempotency_key, firstKey);
  await f.tools.get("waa_send_message").execute("call-3", { to: "peer/agent", message: "hello" });
  assert.notEqual(f.calls.at(-1)[1].idempotency_key, firstKey);
  let resolve;
  f.bridge.call = () => new Promise(done => { resolve = done; });
  const pending = f.adapter.call("check_inbox");
  f.adapter.close();
  resolve({ framed_messages: "stale" });
  await assert.rejects(pending, /session changed/);
});

test("installed-version lifecycle wiring rebinds after shutdown and session_start", async () => {
  const handlers = new Map(), bridges = [], sessions = [];
  const pi = { registerTool() {}, on(event, handler) { handlers.set(event, handler); }, sendMessage() {} };
  installExtension(pi, {
    bridgeFactory(session) {
      sessions.push(session);
      const bridge = { closed: false, async call() { return { wake_ids: [] }; }, close() { this.closed = true; } };
      bridges.push(bridge); return bridge;
    },
    setInterval() { return { unref() {} }; }, clearInterval() {},
  });
  // Pi 0.87.1 emits this sequence for /new, resume, and fork; it has no
  // session_switch/session_fork event in its public ExtensionAPI.
  handlers.get("session_start")({}, context("session-one"));
  handlers.get("session_shutdown")();
  assert.equal(bridges[0].closed, true);
  handlers.get("session_start")({}, context("session-two"));
  assert.deepEqual(sessions, ["session-one", "session-two"]);
  assert.equal(bridges[1].closed, false);
  handlers.get("session_shutdown")();
});

test("old-session bridge failure cannot notify the replacement UI", async () => {
  const handlers = new Map(), notifications = [];
  let rejectOld;
  const pi = { registerTool() {}, on(event, handler) { handlers.set(event, handler); }, sendMessage() {} };
  installExtension(pi, {
    bridgeFactory(session) {
      return { call: () => session === "old" ? new Promise((_done, reject) => { rejectOld = reject; })
        : Promise.resolve({ wake_ids: [] }), close() {} };
    }, setInterval() { return { unref() {} }; }, clearInterval() {},
  });
  const ctx = id => ({ ...context(id), ui: { notify: message => notifications.push(message) } });
  handlers.get("session_start")({}, ctx("old"));
  handlers.get("session_shutdown")();
  handlers.get("session_start")({}, ctx("new"));
  rejectOld(new Error("old bridge ended"));
  await new Promise(done => setImmediate(done));
  assert.deepEqual(notifications, []);
  handlers.get("session_shutdown")();
});
