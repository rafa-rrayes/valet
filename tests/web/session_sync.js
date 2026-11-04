// Runs app.js's SessionView under the stand-in DOM (fakedom.js) to check sync(): a history fetch
// that fails must be retried from the old cursor, not skipped by applying the live events queued meanwhile.
// Usage: node session_sync.js <path to app.js>. Prints JSON {urls, applied}.
"use strict";
const path = require("path");
const { load } = require("./fakedom");

const urls = [];
let calls = 0;
const events = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => ({
  id: from + i, agent: "echo", session_id: "S1", turn_id: "T", ts: 0, kind: "agent_message",
  data: { content: { type: "text", text: `m${from + i} ` } },
}));
async function fetch(path) {
  urls.push(path);
  if (path.includes("/events")) {
    calls += 1;
    if (calls === 1) throw new TypeError("network down");          // the first history fetch fails
    const after = Number(new URL(path, "http://x").searchParams.get("after"));
    return { ok: true, status: 200, json: async () => ({ events: after < 3 ? events(after + 1, 3) : [], more: false }) };
  }
  return { ok: true, status: 200,
           json: async () => ({ agent: "echo", id: "S1", title: "t", state: "working", turn_id: "T" }) };
}

const ctx = load(path.dirname(process.argv[2]), fetch);
const view = ctx.SessionView("echo", "S1");
// Live events arrive while the (failing) first sync runs.
setTimeout(() => { for (const ev of events(4, 5)) view.frame({ type: "event", event: ev }); }, 0);
setTimeout(() => {
  const applied = view.el.textContent.match(/m\d+/g) || [];
  console.log(JSON.stringify({ urls: urls.filter((u) => u.includes("/events")), applied }));
}, 1800);
