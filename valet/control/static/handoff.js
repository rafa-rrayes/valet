"use strict";
// Continuing and handing off conversations (docs/API.md, "Continuing and handing off"), on an agent's page:
//   #/a/<name>/continue  a Claude Code session that ran in this agent's working folder goes on here, with its history
//                        (a Claude Code agent on this machine only)
//   #/a/<name>/handoff   a new session of this agent starts from an earlier conversation, condensed: a Claude Code
//                        session on a runner's machine, or a session of any agent in Valet
// Loaded after app.js (index.html), whose helpers it uses.

function kb(n) { return n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB"; }

function backTo(name) {
  return h("a", { class: "back", href: `#/a/${enc(name)}`, text: "‹ " + agentName(name) });
}

function canContinue(a) { return !!a && a.harness === "claude" && a.level === "host"; }

// A Claude Code session as a card: its title, when, how big; `action` on the right.
function claudeCard(s, action) {
  return h("div", { class: "card session", "data-row": "claude:" + s.id },
    h("div", { class: "row between" }, h("span", { class: "grow", text: s.title || "Untitled session" }), action),
    h("div", { class: "meta", text: [ago(s.updated_at), kb(s.size || 0), s.id.slice(0, 8)].filter(Boolean).join(" · ") }));
}

function ContinueView(name) {
  const body = h("div", {}, h("p", { class: "sub", text: "Loading…" }));
  const err = h("p", { class: "err", hidden: true });
  let busy = false;

  function fail(x) { err.textContent = x.message; err.hidden = false; }

  async function go(s, btn) {
    if (busy) return;
    busy = true;
    err.hidden = true;
    btn.disabled = true;
    btn.textContent = "Loading its history…";
    try {
      const session = await api("POST", `/api/agents/${enc(name)}/claude-sessions/${enc(s.id)}/import`);
      location.hash = `#/a/${enc(name)}/s/${enc(session.id)}`;
    } catch (x) {
      fail(x);
      btn.disabled = false;
      btn.textContent = "Continue here";
    }
    busy = false;
  }

  function draw(r) {
    const list = r.sessions.map((s) => {
      if (s.imported) {
        return claudeCard(s, h("a", { class: "button small", href: `#/a/${enc(s.imported)}/s/${enc(s.id)}`,
          text: s.imported === name ? "Open" : "With " + agentName(s.imported) }));
      }
      const btn = h("button", { type: "button", class: "small primary", text: "Continue here" });
      btn.addEventListener("click", () => go(s, btn));
      return claudeCard(s, btn);
    });
    fill(body, h("p", { class: "sub", text: "Sessions Claude Code ran in " + r.cwd + ", this agent's working folder. "
      + "One continued here goes on with its whole history. Stop it wherever it still runs (a "
      + "terminal, say) first: two of them writing to one session would mix it up." }),
      list.length ? list : h("p", { class: "empty", text: "Claude Code has no sessions in this folder. Set the "
        + "agent's working folder to the project's folder (Settings, Computer), or hand a session off instead." }));
  }

  async function load() {
    const a = S.agents.get(name);
    if (a && !canContinue(a)) {
      fill(body, h("p", { class: "note", text: "Only a Claude Code agent on this machine can continue a Claude Code "
        + "session: its transcripts are there. Hand the conversation off instead: the new session starts with it, "
        + "condensed." }), h("a", { class: "button small", href: `#/a/${enc(name)}/handoff`, text: "Hand off a conversation" }));
      return;
    }
    try { draw(await api("GET", `/api/agents/${enc(name)}/claude-sessions`)); } catch (x) {
      fill(body, h("p", { class: x.status === 503 ? "note" : "err", text: x.status === 503
        ? `${agentName(name)} isn't running: start it, then come back.` : x.message }));
    }
  }

  const el = h("section", {}, backTo(name), h("h1", { text: "Continue a Claude Code session" }), err, body);
  load();
  return { el, frame(f) { if (f.type === "hello") load(); } };
}

function HandoffView(name) {
  const st = { from: "claude", runner: null, folder: null, agent: null, source: null, preview: null };
  const pick = h("div");
  const out = h("div");
  const err = h("p", { class: "err", hidden: true });
  const note = h("textarea", { "data-field": "note", rows: 3, "aria-label": "What to do now",
    placeholder: "What it should do now (optional). Without it, it says where things stand and waits." });
  const start = h("button", { type: "button", class: "primary", text: "Start the session", disabled: true });
  let token = 0;          // the latest list request: a slower older one doesn't draw over it

  function fail(x) { err.textContent = x.message; err.hidden = false; }

  function list(items, empty) { return items.length ? items : h("p", { class: "empty", text: empty }); }

  async function choose(source) {
    st.source = source;
    st.preview = null;
    err.hidden = true;
    start.disabled = true;
    fill(out, h("p", { class: "sub", text: "Condensing…" }));
    try {
      st.preview = await api("POST", "/api/handoff/preview", { source });
      drawPreview();
    } catch (x) { fill(out); fail(x); }
  }

  function drawPreview() {
    const p = st.preview;
    const left = p.omitted ? `; ${p.omitted} earlier ${p.omitted === 1 ? "entry" : "entries"} didn't fit` : "";
    fill(out, h("h2", { text: "What it gets" }),
      h("p", { class: "sub", text: `${p.entries} ${p.entries === 1 ? "entry" : "entries"}, ${kb(p.bytes)}${left}.` }),
      h("details", { class: "more" }, h("summary", { text: "Read it" }), h("pre", { class: "handoff-text", text: p.text })),
      h("div", { class: "field" }, h("label", { text: "What to do now" }), note), start);
    start.disabled = false;
  }

  async function drawClaude() {
    const mine = ++token;
    const runners = [...S.runners.values()].filter((r) => r.connected).map((r) => r.name).sort();
    const runnerRow = runners.length > 1 && h("div", { class: "chips" }, runners.map((r) =>
      chip("runner:" + r, r === (st.runner || runners[0]), r, () => { st.runner = r; st.folder = null; drawClaude(); })));
    const q = st.runner ? "?runner=" + enc(st.runner) : "";
    fill(pick, runnerRow, h("p", { class: "sub", text: "Loading…" }));
    try {
      if (!st.folder) {
        const r = await api("GET", "/api/claude/folders" + q);
        if (mine !== token) return;
        fill(pick, runnerRow, h("p", { class: "sub", text: `Claude Code's project folders on ${r.runner}, newest first.` }),
          list(r.folders.map((f) => h("button", { type: "button", class: "card session", "data-choice": "folder:" + f.folder,
            onclick: () => { st.folder = f.folder; drawClaude(); } },
          h("div", { class: "grow mono", text: f.cwd || f.folder }),
          h("div", { class: "meta", text: `${f.sessions} ${f.sessions === 1 ? "session" : "sessions"} · ${ago(f.updated_at)}` }))),
          "No Claude Code sessions on that machine."));
        return;
      }
      const r = await api("GET", `/api/claude/sessions?folder=${enc(st.folder)}${q ? "&" + q.slice(1) : ""}`);
      if (mine !== token) return;
      fill(pick, runnerRow, h("button", { type: "button", class: "small", text: "‹ Folders",
        onclick: () => { st.folder = null; drawClaude(); } }),
      list(r.sessions.map((s) => claudeCard(s, h("button", { type: "button", class: "small", text: "Use this",
        onclick: () => choose({ kind: "claude", folder: st.folder, session_id: s.id, runner: r.runner }) }))),
      "No sessions in this folder."));
    } catch (x) { if (mine === token) { fill(pick, runnerRow); fail(x); } }
  }

  async function drawValet() {
    const mine = ++token;
    const names = [...S.agents.keys()].sort();
    const agentRow = h("div", { class: "chips" }, names.map((n) =>
      chip("agent:" + n, n === st.agent, agentName(n), () => { st.agent = n; drawValet(); })));
    if (!st.agent) { fill(pick, agentRow, h("p", { class: "sub", text: "Pick the agent whose session it was." })); return; }
    fill(pick, agentRow, h("p", { class: "sub", text: "Loading…" }));
    try {
      const ss = await api("GET", `/api/agents/${enc(st.agent)}/sessions`);
      if (mine !== token) return;
      fill(pick, agentRow, list(ss.map((s) => h("div", { class: "card session", "data-row": "session:" + s.id },
        h("div", { class: "row between" }, h("span", { class: "grow", text: s.title || "New session" }),
          h("button", { type: "button", class: "small", text: "Use this",
            onclick: () => choose({ kind: "session", agent: st.agent, session_id: s.id }) })),
        h("div", { class: "meta", text: ago(s.updated_at) }))), "No sessions yet."));
    } catch (x) { if (mine === token) { fill(pick, agentRow); fail(x); } }
  }

  function drawFrom() {
    fill(from, choice("from:claude", st.from === "claude", "A Claude Code session",
      [h("span", { class: "sub", text: "From a terminal, on a runner's machine." })], () => setFrom("claude")),
    choice("from:session", st.from === "session", "A Valet session",
      [h("span", { class: "sub", text: "Of any agent, whatever its harness." })], () => setFrom("session")));
  }

  function setFrom(f) {
    if (st.from === f) return;
    st.from = f;
    st.source = st.preview = null;
    start.disabled = true;
    fill(out);
    drawFrom();
    (f === "claude" ? drawClaude : drawValet)();
  }

  start.addEventListener("click", async () => {
    if (!st.source) return;
    start.disabled = true;
    err.hidden = true;
    try {
      const r = await api("POST", `/api/agents/${enc(name)}/handoff`, { source: st.source, note: note.value });
      location.hash = `#/a/${enc(name)}/s/${enc(r.session.id)}`;
    } catch (x) { fail(x); start.disabled = false; }
  });

  const from = h("div", { class: "choices", role: "radiogroup", "aria-label": "From" });
  drawFrom();
  drawClaude();
  const el = h("section", {}, backTo(name), h("h1", { text: "Hand off a conversation" }),
    h("p", { class: "sub", text: `A new session of ${agentName(name)} starts with an earlier conversation as its first `
      + "message. Valet condenses it without a model: what was said, each tool call with its main argument, the "
      + "newest kept within 60 KB. It works with any agent, whatever its harness or computer." }),
    from, pick, err, out);
  return { el };
}

addRoute((parts) => {
  if (parts[0] !== "a" || !parts[1]) return null;
  if (parts[2] === "continue") return ContinueView(parts[1]);
  if (parts[2] === "handoff") return HandoffView(parts[1]);
  return null;
});

addAgentAction((a) => [
  canContinue(a) && h("a", { class: "button small", href: `#/a/${enc(a.name)}/continue`, text: "Continue a Claude Code session" }),
  h("a", { class: "button small", href: `#/a/${enc(a.name)}/handoff`, text: "Hand off a conversation" }),
]);
