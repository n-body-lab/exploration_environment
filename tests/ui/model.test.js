// Run by tests/test_ui.py with a fixture from a real local run: node --test model.test.js <fixture.json>
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const HM = require(path.join(__dirname, "..", "..", "hivemarket", "ui", "static", "model.js"));

const fixture = JSON.parse(fs.readFileSync(process.env.HM_FIXTURE, "utf8"));
const { events, agents, world } = fixture;
const market = events.filter(e => e.source === "market").sort((a, b) => a.ts - b.ts);

test("state at the end matches the broker's final state", () => {
  const s = HM.stateAt(world, market, Infinity);
  assert.deepStrictEqual(s.credits, fixture.final.credits);
  for (const [n, held] of Object.entries(fixture.final.holds)) assert.deepStrictEqual([...s.holds[n]].sort(), held);
  assert.strictEqual(s.phase, "over");
  assert.ok(s.outcome);
});

test("state before anything happened is the world's starting state", () => {
  const s = HM.stateAt(world, market, market[0].ts - 1);
  for (const [n, spec] of Object.entries(world.nodes)) {
    assert.strictEqual(s.credits[n], spec.credits);
    assert.deepStrictEqual(s.holds[n], spec.holds);
  }
});

test("replaying to just after the first exchange shows the token moved", () => {
  const ex = market.find(e => e.kind === "exchange");
  const s = HM.stateAt(world, market, ex.ts + 1e-6);
  const d = ex.data;
  assert.ok(s.holds[d.buyer].includes(d.token));
  assert.ok(!s.holds[d.seller].includes(d.token)); // token_transfer: move
  assert.ok(s.spent >= d.cost);
});

test("the rogue's direct messages are detected as backchannel contacts", () => {
  const alerts = HM.detectBackchannel(events, agents);
  const contacts = alerts.filter(a => a.level === "contact");
  assert.ok(contacts.length >= fixture.expected_contacts, JSON.stringify(contacts.map(a => [a.kind, a.from, a.to])));
  for (const a of contacts.filter(a => a.kind === "tool_call")) {
    assert.strictEqual(a.from, fixture.rogue);
    assert.ok(a.detail.includes("directly"));
  }
  assert.ok(contacts.some(a => a.kind === "direct_inbound" && a.to === fixture.target));
});

test("summaries cover the main event kinds", () => {
  const kinds = new Set(events.map(e => e.kind));
  for (const k of ["exchange", "round_end", "tool_call", "llm_turn"]) assert.ok(kinds.has(k), k);
  for (const e of events) {
    const s = HM.summarize(e);
    assert.strictEqual(typeof s, "string");
    assert.ok(s.length > 0, e.kind);
  }
  const ex = events.find(e => e.kind === "exchange");
  assert.match(HM.summarize(ex), /bought .* from .* for \d+/);
});

test("monitor flows: both observers merge into one contact, DNS lookups are probes", () => {
  const ev = [
    { event_id: "1", ts: 10, source: "monitor", agent_id: "agent-2", kind: "off_channel_contact",
      data: { direction: "outbound", initiator: "10.0.0.2:4000", responder: "10.0.0.1:7000", remote_name: "agent-1" } },
    { event_id: "2", ts: 10.1, source: "monitor", agent_id: "agent-1", kind: "off_channel_contact",
      data: { direction: "inbound", initiator: "10.0.0.2:4000", responder: "10.0.0.1:7000", remote_name: "agent-2" } },
    { event_id: "3", ts: 11, source: "monitor", agent_id: "agent-2", kind: "flow",
      data: { direction: "outbound", initiator: "10.0.0.2:4000", responder: "10.0.0.1:7000", remote_name: "agent-1",
              off_channel: true, established: true, bytes_out: 10, bytes_in: 5, payload_out: "send T1" } },
    { event_id: "4", ts: 9, source: "monitor", agent_id: "agent-2", kind: "dns_query",
      data: { queries: ["agent-1"] } },
    { event_id: "5", ts: 9.001, source: "monitor", agent_id: "agent-2", kind: "dns_query",
      data: { queries: ["agent-1"] } },  // the AAAA twin of the lookup above
  ];
  const a = HM.detectBackchannel(ev, [{ id: "agent-1", ip: "10.0.0.1" }, { id: "agent-2", ip: "10.0.0.2" }]);
  assert.deepStrictEqual(a.map(x => [x.level, x.from, x.to]), [["probe", "agent-2", "agent-1"], ["contact", "agent-2", "agent-1"]]);
  assert.strictEqual(a[1].port, "7000");
  assert.deepStrictEqual(a[1].observers, ["monitor:agent-2", "monitor:agent-1"]);
  assert.strictEqual(a[1].detail, "send T1");
  assert.strictEqual(HM.readable("POST /m HTTP/1.1\r\nHost: x\r\n\r\nplease send T1"), "POST /m: please send T1");
  assert.strictEqual(a[1].established, true);
});
