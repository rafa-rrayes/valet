"use strict";
// Valet web UI (M0 phone client). Speaks only docs/API.md: REST for actions and history, one /ws/client socket for
// live state. Anything that came from an agent or the owner is put in the page with textContent, never as HTML;
// agent markdown is turned into DOM nodes by markdown.js.

const S = {
  agents: new Map(),   // name -> Agent
  runners: new Map(),  // name -> Runner
  inbox: new Map(),    // id -> InboxItem: the pending ones from the hello, plus whatever changed since
  view: null,          // the screen on display: {el, frame(f), leave(hash)?, close()?}
  hash: "",            // the location.hash it was drawn for
  ws: null, wsDelay: 1000, wsTimer: 0, hiddenAt: 0,
  signedIn: false,
  catalog: null,       // GET /api/catalog: what the settings page and the new-agent form offer
  flash: null,         // {agent, text}: a note for that agent's page, shown once (e.g. created but not started)
};
const SLEEP_MS = 10000;  // hidden this long (phone asleep, tab in background): the socket may be dead, so redial

// ---------- helpers ----------

const $ = (sel) => document.querySelector(sel);
const enc = encodeURIComponent;

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k in el) el[k] = v;
    else el.setAttribute(k, v);
  }
  return kids.length ? fill(el, ...kids) : el;  // a `text` prop is the content otherwise
}

// replaceChildren that skips null/false, so `cond && h(...)` reads naturally.
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}

function badge(cls, text) { return h("span", { class: "badge " + cls, text: text || cls }); }

function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return new Date(ts * 1000).toLocaleDateString();
}

function clip(text, n = 4000) { return text.length > n ? text.slice(0, n) + "\n…" : text; }

function json(v) { return typeof v === "string" ? v : JSON.stringify(v, null, 2); }

// A description's first paragraph on one line, cut at n characters.
function brief(text, n = 200) {
  const s = String(text || "").trim().split(/\n\s*\n/)[0].replace(/\s+/g, " ");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// "A", "A and B", "A, B and C".
function andList(xs) { return xs.length < 2 ? xs.join("") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1]; }

// An agent's name as the owner knows it (its display name), for lists of agents.
function agentName(n) { return agentTitle(S.agents.get(n)) || n; }

class ApiError extends Error {
  constructor(status, message, body) { super(message); this.status = status; this.body = body; }
}

function detailOf(body) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => e.msg || JSON.stringify(e)).join("\n");  // FastAPI 422
  if (d && typeof d.detail === "string") return d.detail;
  return "";
}

async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try { res = await fetch(path, opts); } catch {
    throw new ApiError(0, "Can't reach Valet. Check the connection.", null);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.ok) return data;
  if (res.status === 401 && path !== "/api/login") signedOut();
  throw new ApiError(res.status, detailOf(data) || `${res.status} ${res.statusText}`, data);
}

// ---------- boot, sign in ----------

async function boot() {
  let me;
  try { me = await api("GET", "/api/me"); } catch (e) { return showFatal(e); }
  if (me.owner) enter(); else showSignIn(me);
}

async function enter() {
  S.signedIn = true;
  $("#top").hidden = false;
  try {
    const [agents, runners, inbox] = await Promise.all(
      [api("GET", "/api/agents"), api("GET", "/api/runners"), api("GET", "/api/inbox")]);
    if (!S.signedIn) return;
    setAll(agents, runners, inbox);
  } catch (e) {
    if (e.status === 401) return;  // already back on the sign-in screen
  }
  connect();
  route();
}

function signedOut() {
  if (!S.signedIn) return;
  S.signedIn = false;
  closeSocket();
  clearTimeout(S.wsTimer);
  closeView();
  S.view = null;
  boot();
}

async function signOut() {
  try { await api("POST", "/api/logout"); } catch { /* the cookie is gone either way */ }
  signedOut();
}

function showSignIn(me) {
  $("#top").hidden = true;
  if (!me.setup) {
    $("#app").replaceChildren(h("section", { class: "signin" },
      h("h1", { text: "Valet" }),
      h("p", {}, "No password yet. Run ", h("code", { text: "valet-control set-password" }), " on the server."),
      h("button", { text: "Check again", onclick: boot })));
    return;
  }
  const pw = h("input", { type: "password", autocomplete: "current-password", required: true, id: "pw" });
  const btn = h("button", { class: "primary", type: "submit", text: "Sign in" });
  const err = h("p", { class: "err", hidden: true });
  const form = h("form", { class: "signin" },
    h("h1", { text: "Valet" }), h("label", { htmlFor: "pw", text: "Password" }), pw, btn, err);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    btn.disabled = true;
    err.hidden = true;
    try {
      await api("POST", "/api/login", { password: pw.value, client: "web" });
      enter();
    } catch (x) {
      err.textContent = x.status === 401 ? "Wrong password."
        : x.status === 429 ? "Too many attempts. Try again in 15 minutes." : x.message;
      err.hidden = false;
      btn.disabled = false;
      pw.select();
    }
  });
  $("#app").replaceChildren(form);
  pw.focus();
}

function showFatal(e) {
  $("#app").replaceChildren(h("section", { class: "signin" },
    h("h1", { text: "Valet" }), h("p", { class: "err", text: e.message }),
    h("button", { text: "Retry", onclick: boot })));
}

// ---------- live socket ----------

function setAll(agents, runners, inbox) {
  S.agents = new Map(agents.map((a) => [a.name, a]));
  S.runners = new Map(runners.map((r) => [r.name, r]));
  S.inbox = new Map(inbox.map((i) => [i.id, i]));
  inboxCount();
}

function connect() {
  clearTimeout(S.wsTimer);
  closeSocket();
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/client");
  S.ws = ws;
  setLink("", "Connecting");
  ws.onmessage = (m) => {
    let f;
    try { f = JSON.parse(m.data); } catch { return; }
    onFrame(f);
  };
  ws.onclose = (ev) => {
    if (S.ws !== ws) return;
    S.ws = null;
    setLink("down", "Offline, reconnecting");
    if (!S.signedIn) return;
    if (ev.code === 4401) { checkAuth(); return; }
    S.wsTimer = setTimeout(connect, S.wsDelay);
    S.wsDelay = Math.min(S.wsDelay * 2, 30000);
  };
}

function closeSocket() {
  const ws = S.ws;
  S.ws = null;
  if (ws) { ws.onclose = null; ws.onmessage = null; ws.close(); }
}

function reconnectNow() {
  if (!S.signedIn) return;
  S.wsDelay = 1000;
  connect();
}

async function checkAuth() {
  try {
    const me = await api("GET", "/api/me");
    if (!me.owner) { signedOut(); return; }
  } catch { /* offline: keep retrying */ }
  S.wsTimer = setTimeout(connect, S.wsDelay);
  S.wsDelay = Math.min(S.wsDelay * 2, 30000);
}

function setLink(cls, title) {
  const el = $("#link");
  el.className = "link " + cls;
  el.title = title;
}

function onFrame(f) {
  for (const fn of EXTRA.watchers) {
    try { fn(f); } catch (e) { console.error(e); }
  }
  switch (f.type) {
    case "hello":
      S.wsDelay = 1000;
      setLink("live", "Live");
      setAll(f.agents || [], f.runners || [], f.inbox || []);
      break;
    case "agent": S.agents.set(f.agent.name, f.agent); break;
    case "agent_deleted":   // gone everywhere: the list, and its inbox items (the server dropped them)
      S.agents.delete(f.agent);
      for (const [id, item] of S.inbox) if (item.agent === f.agent) S.inbox.delete(id);
      inboxCount();
      break;
    case "runner": S.runners.set(f.runner.name, f.runner); break;
    case "inbox": S.inbox.set(f.item.id, f.item); inboxCount(); break;
    case "session": case "event": break;
    default: if (!EXTRA.frames.has(f.type)) return;
  }
  if (S.view && S.view.frame) S.view.frame(f);
  // A deleted agent's screens (its page, sessions, settings, files...) have nothing left to show: the agents list.
  if (f.type === "agent_deleted" && (S.hash + "/").startsWith(`#/a/${enc(f.agent)}/`)) location.hash = "#/";
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { S.hiddenAt = Date.now(); return; }
  if (S.signedIn && (!S.ws || Date.now() - S.hiddenAt > SLEEP_MS)) reconnectNow();
});
window.addEventListener("online", reconnectNow);
window.addEventListener("pageshow", (e) => { if (e.persisted) reconnectNow(); });

function inboxCount() {
  const n = [...S.inbox.values()].filter((i) => i.status === "pending").length;
  const el = $("#inbox-count");
  el.textContent = String(n);
  el.hidden = n === 0;
}

function putInbox(item) {
  S.inbox.set(item.id, item);
  inboxCount();
  if (S.view && S.view.frame) S.view.frame({ type: "inbox", item });
}

// ---------- screens in other files ----------
// browser.js, shared.js and the like are listed in index.html after this file and register here as they load,
// before boot() runs (at DOMContentLoaded). addRoute(fn): fn(parts) -> a view, or null to pass; tried for any
// address no built-in screen takes, before the fallbacks (an agent's page for "a/<name>/...", else the agents
// list), so an agent's own screens can live in these files too ("a/<name>/memory"). A view may have close(): called
// when another view replaces it (a socket of its own to close). addFrameType(type): the client socket's frames of
// that type reach the view on display (unknown types are dropped otherwise). addTab(key, label, also?): a link in
// the header to #/<key>, lit while the address starts with it, or with one of the keys in `also` (["vault"]); it
// returns the link (for a count).
// addAgentAction(fn): fn(agent) -> a control (or a list of them, nulls skipped) for the agent page's row of buttons;
// called each time the row is drawn.
// addWatcher(fn): fn(frame) for every frame of the client socket, whatever is on display (a tab's count).
// addSettingsSection({key, title, after, make}): a section of an agent's settings page, after the built-in one
// called `after` (name, personality, knows, brain, computer, perms, tools, skills, secrets). make(ctx) -> {el,
// draw(), frame?(f), changed?(key)}: draw() runs whenever the form is drawn (after a load, a save, a discard);
// changed(key) after any edit of the draft (`key` as below), so a section can follow another's field; ctx has the
// agent's name and base path, `draft` and `saved` (the definition, as getters), changed(key) (a draft edit: the save
// bar), errFor(key) (an error slot a 422 for that field fills), setDirectives(list) and redraw() (the whole form).
// addAskView(kind, fn): fn(p, head, cls, result) -> the card for a pending or settled ask of that kind (an inbox
// item, or a permission_request in a session), or null for the plain one. p is as permissionEl's, with the item's
// payload under its kind's name (a browser item's p.browser) and p.text for valet.owner's message and question; head
// goes first (the inbox's who and when); cls is the card's class; result says how it was settled ("" while pending).
// addAgentLine(fn): fn(agent) -> an element or null, under an agent's name in the list and on its page.
// addAgentSection(make): make(name) -> {el, frame?(f)}, a section at the end of an agent's page.
// addAgentsPart(make): make() -> {el, frame?(f)}, a part of the agents list's page, under the agents.
// addComposerPart(make): make(ctx) -> {el?, above?, has(), body(), sent(), busy?()} for a session's composer (attach.js):
// `el` goes in the composer's row before the text box, `above` over it. ctx has the agent's name, the session id,
// the view's el, the text box (input), submit() and showErr(text). has(): it has something to send without text;
// body(): async, fields for the prompt's body (it throws to hold the message; its message is shown); sent(): the
// message went (started or queued). busy(): it isn't ready yet (Send waits).
// addBlockView(fn): fn(block, ev) -> an element for a message's ContentBlock (an image, a file), or null to pass; a
// block one of them draws goes in the log after the message's text instead of in it.
// A view's close(), if it has one, runs when another view replaces it or the owner signs out (terminal.js ends its
// shells there).
const EXTRA = { routes: [], frames: new Set(), tabs: [], actions: [], watchers: [], sections: [], asks: new Map(),
  agentLines: [], agentSections: [], agentsParts: [], composer: [], blocks: [] };
function addRoute(fn) { EXTRA.routes.push(fn); }
function addAgentAction(fn) { EXTRA.actions.push(fn); }
function addAgentLine(fn) { EXTRA.agentLines.push(fn); }
function addAgentSection(make) { EXTRA.agentSections.push(make); }
function addAgentsPart(make) { EXTRA.agentsParts.push(make); }
function agentLines(a) { return EXTRA.agentLines.map((fn) => fn(a)).filter(Boolean); }
function addFrameType(type) { EXTRA.frames.add(type); }
function addTab(key, label, also = []) {
  EXTRA.tabs.push({ key, label, also });
  const link = h("a", { href: "#/" + key, "data-nav": key, text: label });
  const nav = document.querySelector("#top nav");
  if (nav) nav.append(link);
  return link;
}
function addWatcher(fn) { EXTRA.watchers.push(fn); }
function addSettingsSection(spec) { EXTRA.sections.push(spec); }
function addAskView(kind, fn) { EXTRA.asks.set(kind, fn); }
function addComposerPart(make) { EXTRA.composer.push(make); }
function addBlockView(fn) { EXTRA.blocks.push(fn); }
function blockView(block, ev) {
  for (const fn of EXTRA.blocks) {
    const el = block && fn(block, ev);
    if (el) return el;
  }
  return null;
}
function extraView(parts) {
  for (const fn of EXTRA.routes) {
    const view = fn(parts);
    if (view) return view;
  }
  return null;
}

// ---------- routing ----------

function route() {
  if (!S.signedIn) return;
  // A view with unsaved edits can hold on: leave(hash) true means it asks the owner first, so the address goes back
  // to it (replaceState fires no hashchange) and nothing is redrawn.
  if (S.view && S.view.leave && location.hash !== S.hash && S.view.leave(location.hash)) {
    history.replaceState(history.state, "", location.pathname + location.search + S.hash);
    return;
  }
  S.hash = location.hash;
  let parts;
  try {
    parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  } catch { parts = []; }
  let view;
  if (parts[0] === "inbox") view = InboxView();
  else if (parts[0] === "secrets-tools") view = SecretsToolsView();
  else if (parts[0] === "directives") view = DirectivesView();
  else if (parts[0] === "skills") view = parts[1] ? SkillView(parts[1], null) : SkillsView();
  else if (parts[0] === "a" && parts[1] && parts[2] === "skills" && parts[3]) view = SkillView(parts[3], parts[1]);
  else if (parts[0] === "a" && parts[1] && parts[2] === "s" && parts[3]) view = SessionView(parts[1], parts[3]);
  else if (parts[0] === "a" && parts[1] && parts[2] === "settings") view = SettingsView(parts[1]);
  else view = extraView(parts) || (parts[0] === "a" && parts[1] ? AgentView(parts[1]) : AgentsView());
  closeView();
  S.view = view;
  $("#app").replaceChildren(view.el);
  const extra = EXTRA.tabs.find((t) => t.key === parts[0] || t.also.includes(parts[0]));
  const tab = parts[0] === "inbox" ? "inbox" : extra ? extra.key : "agents";
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === tab));
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// The view on display is going away (another one replaces it, or the owner signed out).
function closeView() {
  const v = S.view;
  if (v && v.close) {
    try { v.close(); } catch (e) { console.error(e); }
  }
}

// ---------- secrets (the settings page, the agent pages and the secrets screen share these) ----------
// Values go in and never come out: a value field is never filled from anywhere, and is emptied once saved.

const SECRET_RE = /^[A-Z][A-Z0-9_]{0,63}$/;
const SECRET_RULE = "Use capital letters, digits and _, starting with a letter, like GITHUB_TOKEN.";

// What the owner typed as a name -> the name: capitals, spaces and dashes as _ ("github token" -> GITHUB_TOKEN).
// Whether Valet takes it (some names are reserved) is the server's call.
function secretName(text) { return String(text || "").trim().toUpperCase().replace(/[\s-]+/g, "_"); }

// A pasted value -> what's stored: a single line loses the whitespace around it (a copied token often brings a
// newline or a space along); text of several lines is kept as it is.
function secretValue(raw) {
  const s = String(raw == null ? "" : raw);
  const t = s.trim();
  return t.includes("\n") ? s : t;
}

// The secrets a refused start names (422 {detail, missing_secrets}), else [].
function missingOf(x) {
  const m = x && x.status === 422 && x.body && x.body.missing_secrets;
  return Array.isArray(m) ? m : [];
}

// A secret's value. "vault:<entry>" (what missing_secrets says of a vault entry without a password) sets that
// entry's password instead: the vault's routes, vault.js.
async function setSecret(name, value) {
  if (!name.startsWith("vault:")) return api("PUT", `/api/secrets/${enc(name)}`, { value });
  const entry = name.slice(6);
  try { return await api("PUT", "/api/vault/" + vaultPath(entry), { password: value }); } catch (x) {
    if (x.status === 404) throw new ApiError(404, `There's no vault entry ${entry}: add it in the vault first.`, x.body);
    throw x;
  }
}

// A vault entry's name in a path: github/alice stays github/alice, each part encoded.
function vaultPath(entry) { return String(entry).split("/").map(enc).join("/"); }

// The env var a vault entry's password gets when the definition names none: github/alice -> GITHUB_ALICE_PASSWORD,
// with VAULT_ in front when that wouldn't be usable (0/alice -> VAULT_0_ALICE_PASSWORD; docker/ci: DOCKER_ is
// reserved). Mirrors valet.shared.definition.vault_env and RESERVED_PREFIXES.
const RESERVED_PREFIXES = ["VALET_", "CLAUDE_", "ANTHROPIC_", "DEEPSEEK_", "DSH_", "DOCKER_", "LD_", "DYLD_"];
function vaultEnv(entry) {
  const name = String(entry || "").toUpperCase().replace(/[^A-Z0-9]/g, "_") + "_PASSWORD";
  return /^[A-Z]/.test(name) && !RESERVED_PREFIXES.some((p) => name.startsWith(p)) ? name : "VAULT_" + name;
}

// A name missing_secrets lists, for the owner: GITHUB_TOKEN, or "github/alice's password" for "vault:github/alice".
function neededName(n) { return n.startsWith("vault:") ? `${n.slice(6)}'s password` : n; }

// A write-only field for a secret's value. Enter runs onEnter. Password managers are asked to keep out of it.
function secretInput(name, onEnter) {
  const input = h("input", { type: "password", class: "mono", autocomplete: "off", autocapitalize: "none",
    placeholder: "Paste the value", "aria-label": "Value of " + name, "data-field": "secret-value:" + name,
    "data-1p-ignore": "true", "data-lpignore": "true" });
  input.setAttribute("spellcheck", "false");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); onEnter(); }
  });
  return input;
}

// "Set value" for one secret: its field, Cancel and Save value. onSaved(Secret) once stored.
function valueEditor(name, onSaved, onCancel) {
  const err = h("p", { class: "err", hidden: true, "data-err": "secret:" + name });
  const input = secretInput(name, () => go());
  const save = h("button", { type: "button", class: "small primary", text: "Save value", onclick: () => go() });
  async function go() {
    const value = secretValue(input.value);
    err.hidden = true;
    if (!value) { err.textContent = "Paste the value first."; err.hidden = false; return; }
    save.disabled = true;
    try {
      const s = await setSecret(name, value);
      input.value = "";
      save.disabled = false;
      await onSaved(s);
    } catch (x) { err.textContent = x.message; err.hidden = false; save.disabled = false; }
  }
  const el = h("div", { class: "valuebox" }, input, err, h("div", { class: "row end" },
    h("button", { type: "button", class: "small", text: "Cancel", onclick: () => { input.value = ""; onCancel(); } }), save));
  return { el, focus() { input.focus(); } };
}

// Fields for secrets that aren't set: `lead`, then each name with its field; `submit` sets them all, then after().
// One the server refuses keeps its field and the reason; the ones it took show as saved.
function secretsForm(names, { lead, submit, after, cancel }) {
  let busy = false;
  const rows = names.map((n) => {
    const r = { n, done: false, err: h("p", { class: "err", hidden: true, "data-err": "secret:" + n }),
      input: secretInput(n, () => go()), slot: h("div") };
    fill(r.slot, r.input);
    r.el = h("div", { class: "field" }, h("label", { class: "mono", text: neededName(n) }), r.slot, r.err);
    return r;
  });
  const btn = h("button", { type: "button", class: "small primary", text: submit, onclick: () => go() });
  async function go() {
    if (busy) return;
    let ok = true;
    for (const r of rows) {
      r.err.hidden = true;
      if (!r.done && !secretValue(r.input.value)) { r.err.textContent = "Paste its value."; r.err.hidden = false; ok = false; }
    }
    if (!ok) return;
    busy = btn.disabled = true;
    for (const r of rows.filter((x) => !x.done)) {
      try {
        await setSecret(r.n, secretValue(r.input.value));
        r.input.value = "";
        r.done = true;
        fill(r.slot, h("span", { class: "ok", text: "Saved" }));
      } catch (x) { r.err.textContent = x.message; r.err.hidden = false; }
    }
    busy = btn.disabled = false;
    if (rows.every((r) => r.done)) await after();
  }
  return h("div", { class: "secretsform" }, h("div", { class: "q", text: lead }), rows.map((r) => r.el),
    h("div", { class: "row end" }, cancel && h("button", { type: "button", class: "small", text: "Cancel", onclick: cancel }), btn));
}

// What went wrong starting an agent, kept across redraws (the owner may be typing in it): the error, or, when the
// start was refused for secrets that aren't set, their fields and "Set & start", which sets them and tries again.
// onChange() after it shows or hides.
function startTrouble(onChange) {
  const el = h("div", { class: "trouble", hidden: true });
  const changed = () => { if (onChange) onChange(); };
  function clear() { el.hidden = true; el.replaceChildren(); changed(); }
  function form(names, lead, submit, after) {
    fill(el, secretsForm(names, { lead, submit, after, cancel: clear }));
    el.hidden = false;
    changed();
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  return {
    el, clear,
    // A failed start: retry() once the secrets it named are set. o: {lead, submit} for the form.
    show(x, retry, o = {}) {
      const missing = missingOf(x);
      if (!missing.length) {
        fill(el, h("p", { class: "err", text: x.message }));
        el.hidden = false;
        changed();
        return;
      }
      const lead = o.lead || (missing.length > 1 ? "It can't start until these secrets are set."
        : "It can't start until this secret is set.");
      form(missing, lead, o.submit || "Set & start", async () => { clear(); await retry(); });
    },
    // "Set" on a "Needs ..." warning: just their fields.
    offer(names) {
      form(names, "Stored encrypted. Nobody can read a value back, you included.", "Save", async () => clear());
    },
  };
}

// "Needs GITHUB_TOKEN": secrets its definition lists that aren't set, so it can't start. Set opens their fields.
function needsLine(a, trouble, cls = "needs") {
  const m = (a && a.missing_secrets) || [];
  return m.length > 0 && h("div", { class: cls + " row" }, h("span", { class: "grow", text: "Needs " + andList(m.map(neededName)) }),
    h("button", { type: "button", class: "small", text: "Set", onclick: () => trouble.offer(m) }));
}

// ---------- agents ----------

const NAME_RE = /^[a-z0-9][a-z0-9-]{0,30}$/;
const STOPS_REPLY = "It's in the middle of a reply: restarting stops it.";

function agentTitle(a) { return (a && (a.display_name || a.name)) || ""; }

// Emoji, display name, and the id (small) when it differs from the display name.
function agentLabel(a) {
  const title = agentTitle(a);
  return [a.emoji && h("span", { class: "emoji", text: a.emoji }), h("span", { text: title }),
    title !== a.name && h("span", { class: "id", text: a.name })];
}

function agentBadge(a) {
  if (a.desired === "running" && !a.connected && a.state !== "failed") return badge("offline", "runner offline");
  return badge(a.status, a.status === "waiting" ? "waiting on you" : a.status);
}

// Running (or on its way): a restart would replace it.
function isUp(a) { return !!a && a.desired === "running" && (a.state === "running" || a.state === "starting"); }

async function restartAgent(name) {
  onFrame({ type: "agent", agent: await api("POST", `/api/agents/${enc(name)}/start`) });
}

// An inline "are you sure?" (never a dialog: those block the page): ask(question, yes, onYes) shows the question
// with Cancel and `yes`; either button hides it again.
function confirmBox() {
  const el = h("div", { class: "confirm", hidden: true });
  const hide = () => { el.hidden = true; };
  return {
    el, hide,
    ask(question, yes, onYes, cls = "primary") {
      fill(el, h("div", { class: "q", text: question }), h("div", { class: "row" },
        h("button", { type: "button", class: "small", text: "Cancel", onclick: hide }),
        h("button", { type: "button", class: "small " + cls, text: yes, onclick: () => { hide(); onYes(); } })));
      el.hidden = false;
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    },
  };
}

// "Saved changes apply after a restart" with a Restart button, shown while the agent's restart_needed. Stable
// across agent frames (update() redraws it), so a confirmation or an error stays put.
function restartNote(name) {
  const trouble = startTrouble();
  const ask = confirmBox();
  const go = async () => {
    btn.disabled = true;
    trouble.clear();
    try { await restartAgent(name); } catch (x) { trouble.show(x, go, { submit: "Set & restart" }); }
    btn.disabled = false;
  };
  const btn = h("button", { type: "button", class: "small primary", text: "Restart", onclick: () => {
    const a = S.agents.get(name);
    if (a && a.status === "working") ask.ask(STOPS_REPLY, "Restart anyway", go, "danger"); else go();
  } });
  const el = h("div", { class: "note restart", hidden: true },
    h("div", { class: "row between" }, h("span", { class: "grow", text: "Saved changes apply after a restart." }), btn),
    ask.el, trouble.el);
  function update() {
    const a = S.agents.get(name);
    el.hidden = !(a && a.restart_needed);
    if (el.hidden) ask.hide();
  }
  update();
  return { el, update };
}

// Start (or Restart) and Stop. What goes wrong shows in `trouble` (startTrouble), which outlives the redraw the
// agent frame that follows brings.
function agentButtons(a, trouble) {
  const run = async (verb, btn) => {
    if (btn) btn.disabled = true;
    trouble.clear();
    try {
      const agent = await api("POST", `/api/agents/${enc(a.name)}/${verb}`);
      onFrame({ type: "agent", agent });
    } catch (x) {
      trouble.show(x, () => run(verb, null));
      if (btn) btn.disabled = false;
      try { onFrame({ type: "agent", agent: await api("GET", `/api/agents/${enc(a.name)}`) }); } catch { /* shown */ }
    }
  };
  const up = a.desired === "running" && a.state !== "failed";
  const out = [];
  if (!up) out.push(h("button", { class: "small primary", text: a.state === "failed" ? "Restart" : "Start",
    onclick: (e) => run("start", e.target) }));
  if (a.desired === "running") out.push(h("button", { class: "small", text: "Stop", onclick: (e) => run("stop", e.target) }));
  return out;
}

function agentMeta(a) {
  return [a.harness, a.level, a.model, a.runner && "on " + a.runner].filter(Boolean).join(" · ");
}

async function getCatalog() {
  S.catalog = await api("GET", "/api/catalog");
  return S.catalog;
}

// A display name -> an agent id: lowercase, accents off, spaces to dashes, anything else dropped.
function slugify(text) {
  return String(text || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[\s_]+/g, "-").replace(/[^a-z0-9-]/g, "").replace(/-+/g, "-").replace(/^-+/, "")
    .slice(0, 31).replace(/-+$/, "");
}

// The new-agent form's state -> the POST /api/agents body.
function newAgentBody(f) {
  const definition = {};
  if (f.display) definition.display_name = f.display;
  if (f.harness) definition.harness = f.harness;
  definition.model = f.model || null;
  return { name: f.id, ...(f.copyFrom ? { copy_from: f.copyFrom } : { template: f.template || "blank" }), definition };
}

function chip(key, on, text, onclick) {
  return h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": on ? "true" : "false",
    "data-choice": key, text, onclick });
}

// A big tappable card in a radio group: its title, then a line or two about it.
function choice(key, on, title, lines, onclick) {
  return h("button", { type: "button", class: "choice" + (on ? " on" : ""), role: "radio",
    "aria-checked": on ? "true" : "false", "data-choice": key, onclick }, h("span", { class: "title", text: title }), lines);
}

function readiness(x) {
  if (x.ready === true) return h("span", { class: "ok", text: "Ready" });
  if (x.ready === false) return [h("span", { class: "warn-text", text: "No key on the runner" }),
    x.key && h("span", { class: "key", text: "Needs " + x.key })];
  if (x.ready === null) return h("span", { class: "sub", text: "Unknown: no runner is connected" });
  return null;
}

const OTHER = "__other__";  // the model menu's "Other…"

// Harness cards and a model menu over state.harness and state.model (null: the harness's default). Shared by the
// settings page and the new-agent form: onChange(field) after each edit; errFor(field) makes an error slot.
function brainPicker(state, onChange, errFor) {
  const cards = h("div", { class: "choices two", role: "radiogroup", "aria-label": "Brain" });
  const menu = h("div", { class: "field" });
  let other = false;

  function drawCards() {
    const list = S.catalog ? [...S.catalog.harnesses] : [];
    if (state.harness && !list.some((x) => x.id === state.harness)) {
      list.push({ id: state.harness, name: state.harness, by: "Set in the file" });
    }
    fill(cards, list.map((x) => choice("harness:" + x.id, x.id === state.harness, x.name,
      [x.by && h("span", { class: "sub", text: x.by }), readiness(x)].flat(), () => {
        if (x.id === state.harness) return;
        state.harness = x.id;
        state.model = null;
        other = false;
        drawCards();
        drawMenu();
        onChange("harness");
      })));
  }

  function drawMenu() {
    const x = S.catalog && S.catalog.harnesses.find((y) => y.id === state.harness);
    const models = (x && x.models) || [];
    if (state.model != null && !models.some((m) => m.id === state.model)) other = true;
    const sel = h("select", { "aria-label": "Model", "data-field": "model" },
      h("option", { value: "", text: "Default" }),
      models.map((m) => h("option", { value: m.id, text: m.name + (m.note ? " — " + m.note : "") })),
      h("option", { value: OTHER, text: "Other…" }));
    sel.value = other ? OTHER : state.model || "";
    const input = other && h("input", { class: "mono", value: state.model || "", placeholder: "the model's id",
      autocapitalize: "none", autocomplete: "off", "aria-label": "Model id", "data-field": "model-other",
      oninput: (e) => { state.model = e.target.value.trim() || null; onChange("model"); } });
    sel.addEventListener("change", () => {
      other = sel.value === OTHER;
      if (!other) state.model = sel.value || null;
      drawMenu();
      onChange("model");
    });
    fill(menu, h("label", { text: "Model" }), sel, input, errFor("model"));
    return input;
  }

  drawCards();
  drawMenu();
  return { el: h("div", {}, cards, errFor("harness"), menu), redraw() { drawCards(); drawMenu(); }, redrawCards: drawCards };
}

// "New agent": a name (the id follows it), what to start from, and the brain. Create & start, then its page.
function NewAgentForm(onCancel) {
  const f = { display: "", id: "", idEdited: false, template: null, copyFrom: null, harness: null, model: null };
  const errs = new Map();
  const errFor = (k) => { const el = h("p", { class: "err", hidden: true, "data-err": k }); errs.set(k, el); return el; };
  const showErr = (k, msg) => {
    const el = errs.get(k) || errs.get("form");
    el.textContent = el.hidden ? msg : el.textContent + "\n" + msg;
    el.hidden = false;
  };
  const clearErrs = () => errs.forEach((el) => { el.hidden = true; });

  const nameIn = h("input", { placeholder: "e.g. Scout", maxLength: 40, autocomplete: "off", "aria-label": "Name",
    "data-field": "display_name" });
  const idIn = h("input", { class: "mono", maxLength: 31, autocapitalize: "none", autocomplete: "off", "aria-label": "ID",
    "data-field": "name", hidden: true });
  idIn.setAttribute("spellcheck", "false");
  idIn.setAttribute("autocorrect", "off");
  const idShown = h("span", { class: "mono", text: "…" });
  const idLine = h("div", { class: "sub idline" }, "ID ", idShown, " · ",
    h("button", { type: "button", class: "linkish", text: "change", onclick: () => {
      idLine.hidden = true;
      idIn.hidden = false;
      idIn.focus();
    } }));
  const nameErr = errFor("name");
  function checkId() {
    idShown.textContent = f.id || "…";
    if (f.id && S.agents.has(f.id)) showErr("name", `There's already an agent with the ID ${f.id}.`);
    else nameErr.hidden = true;
  }
  nameIn.addEventListener("input", () => {
    f.display = nameIn.value.trim();
    if (!f.idEdited) { f.id = slugify(f.display); idIn.value = f.id; }
    checkId();
    drawStart();
  });
  idIn.addEventListener("input", () => { f.idEdited = true; f.id = idIn.value.trim(); checkId(); });

  const chips = h("div", { class: "chips" });
  const startInfo = h("div");
  let brain = null;

  function fromAgent() {
    const a = S.agents.get(f.copyFrom);
    if (!a || !a.harness) return;
    f.harness = a.harness;
    f.model = a.model || null;
    if (brain) brain.redraw();
  }

  function drawStart() {
    const ts = S.catalog ? S.catalog.templates : [];
    const agents = [...S.agents.values()].sort((x, y) => x.name.localeCompare(y.name));
    fill(chips, ts.map((t) => chip("template:" + t.id, !f.copyFrom && f.template === t.id,
      (t.emoji ? t.emoji + " " : "") + t.name, () => { f.copyFrom = null; f.template = t.id; drawStart(); })),
    agents.length > 0 && chip("copy", !!f.copyFrom, "Copy of…", () => {
      if (!f.copyFrom) { f.copyFrom = agents[0].name; fromAgent(); }
      drawStart();
    }));
    if (f.copyFrom) {
      const sel = h("select", { "aria-label": "Agent to copy", "data-field": "copy_from" },
        agents.map((a) => h("option", { value: a.name,
          text: (a.emoji ? a.emoji + " " : "") + agentTitle(a) + (agentTitle(a) !== a.name ? ` (${a.name})` : "") })));
      sel.value = f.copyFrom;
      sel.addEventListener("change", () => { f.copyFrom = sel.value; fromAgent(); });
      fill(startInfo, sel,
        h("p", { class: "sub", text: "Its personality and settings, with a computer and sessions of its own." }));
    } else {
      const t = ts.find((x) => x.id === f.template);
      fill(startInfo, t && h("p", { class: "preview", text: t.persona.split("{who}").join(f.display || "…") }));
    }
  }

  const go = h("button", { type: "button", class: "primary", text: "Create & start", onclick: create });
  const body = h("div", {}, h("p", { class: "sub", text: "Loading…" }));
  const el = h("div", { class: "card newagent" }, h("h3", { text: "New agent" }), body);

  function draw() {
    if (!S.catalog) return;
    const hs = S.catalog.harnesses;
    if (!f.harness) f.harness = (hs.find((x) => x.ready !== false) || hs[0] || { id: "claude" }).id;
    if (!f.template && S.catalog.templates.length) f.template = S.catalog.templates[0].id;
    brain = brainPicker(f, () => { errs.get("model").hidden = true; }, errFor);
    drawStart();
    fill(body,
      h("div", { class: "field" }, h("label", { text: "Name" }), nameIn, errFor("display_name"), idLine, idIn, nameErr),
      h("div", { class: "field" }, h("label", { text: "Start from" }), chips, startInfo),
      h("div", { class: "field" }, h("label", { text: "Brain" }), brain.el),
      errFor("form"),
      h("div", { class: "row end actions" },
        h("button", { type: "button", text: "Cancel", onclick: onCancel }), go));
    nameIn.focus();
  }

  async function create() {
    clearErrs();
    if (!f.id) return showErr(f.display ? "name" : "display_name", f.display ? "Pick an ID." : "Give it a name.");
    if (!NAME_RE.test(f.id)) {
      return showErr("name", "The ID takes lowercase letters, digits and dashes, starting with a letter or digit, "
        + "31 at most.");
    }
    if (S.agents.has(f.id)) return showErr("name", `There's already an agent with the ID ${f.id}.`);
    go.disabled = true;
    go.textContent = "Creating…";
    let detail;
    try {
      detail = await api("POST", "/api/agents", newAgentBody(f));
    } catch (x) {
      go.disabled = false;
      go.textContent = "Create & start";
      const list = x.body && Array.isArray(x.body.errors) ? x.body.errors : null;
      if (x.status === 409) showErr("name", `There's already an agent with the ID ${f.id}.`);
      else if (list) list.forEach((e) => showErr(errs.has(e.field) ? e.field : "form", e.msg));
      else showErr(x.status === 422 && /^names /.test(x.message) ? "name" : "form", x.message);
      return;
    }
    onFrame({ type: "agent", agent: detail });
    go.textContent = "Starting…";
    try {
      await restartAgent(f.id);
    } catch (x) {
      S.flash = { agent: f.id, text: x.status === 503 ? `Created. It starts when a runner connects (${x.message}).`
        : `Created, but it didn't start: ${x.message}` };
    }
    location.hash = "#/a/" + enc(f.id);
  }

  if (S.catalog) draw();
  getCatalog().then(() => { if (!brain) draw(); else { brain.redrawCards(); } },
    (x) => { if (!brain) fill(body, h("p", { class: "err", text: x.message })); });
  return { el, focus() { nameIn.focus(); } };
}

function AgentsView() {
  const list = h("div");
  const runners = h("div");
  const formBox = h("div");
  let form = null;
  const toggle = () => {
    if (form) { form = null; formBox.replaceChildren(); return; }
    form = NewAgentForm(toggle);
    formBox.replaceChildren(form.el);
    form.focus();
  };

  const troubles = new Map();  // agent -> its startTrouble: cards are redrawn on every frame, these stay
  const troubleOf = (n) => troubles.get(n) || troubles.set(n, startTrouble()).get(n);

  function card(a) {
    const trouble = troubleOf(a.name);
    return h("div", { class: "card agent", "data-row": a.name },
      h("a", { href: "#/a/" + enc(a.name), class: "row between", style: "color: inherit" },
        h("strong", { class: "grow title" }, agentLabel(a)), agentBadge(a)),
      h("div", { class: "meta", text: agentMeta(a) || "definition invalid" }),
      agentLines(a),
      a.restart_needed && h("div", { class: "hint", text: "Restart to apply saved changes" }),
      needsLine(a, trouble),
      (a.error || a.definition_error) && h("p", { class: "err", text: a.definition_error || a.error }),
      h("div", { class: "row", style: "margin-top: 10px" }, agentButtons(a, trouble)), trouble.el);
  }

  function draw() {
    const agents = [...S.agents.values()].sort((x, y) => x.name.localeCompare(y.name));
    list.replaceChildren(...(agents.length ? agents.map(card)
      : [h("p", { class: "empty", text: "No agents yet. Create one to start." })]));
    const rs = [...S.runners.values()].sort((x, y) => x.name.localeCompare(y.name));
    runners.replaceChildren(...(rs.length ? rs.map((r) => h("div", { class: "card row between runner" },
      h("span", { text: r.name }),
      r.connected ? badge("idle", "connected") : badge("offline", r.last_seen ? "seen " + ago(r.last_seen) : "never seen")))
      : [h("p", { class: "sub", text: "No runners yet. Add one with valet-control add-runner NAME." })]));
  }
  draw();
  const parts = EXTRA.agentsParts.map((make) => make()).filter(Boolean);
  const el = h("section", {},
    h("div", { class: "row between", style: "margin-bottom: 12px" }, h("h1", { text: "Agents" }),
      h("button", { class: "small", text: "New agent", onclick: toggle })),
    formBox, list, parts.map((x) => x.el),
    h("a", { class: "card linkcard row", href: "#/directives" },
      h("span", { class: "grow" }, h("strong", { text: "About you & authority" }),
        h("span", { class: "sub", text: "What your agents know about you, and what they may do without asking" })),
      h("span", { class: "chev", text: "›" })),
    h("a", { class: "card linkcard row", href: "#/secrets-tools" },
      h("span", { class: "grow" }, h("strong", { text: "Secrets & tools" }),
        h("span", { class: "sub", text: "Keys your agents use, and the tool servers they can reach" })),
      h("span", { class: "chev", text: "›" })),
    h("a", { class: "card linkcard row", href: "#/skills" },
      h("span", { class: "grow" }, h("strong", { text: "Skills" }),
        h("span", { class: "sub", text: "Instructions your agents load when a task calls for them" })),
      h("span", { class: "chev", text: "›" })),
    h("a", { class: "card linkcard row", href: "#/notifications" },
      h("span", { class: "grow" }, h("strong", { text: "Notifications" }),
        h("span", { class: "sub", text: "How Valet tells you something waits in the inbox: a webhook, your devices" })),
      h("span", { class: "chev", text: "›" })),
    h("h2", { text: "Runners" }), runners,
    h("p", { style: "margin-top: 24px" }, h("button", { class: "small", text: "Sign out", onclick: signOut })));
  return {
    el,
    frame(f) {
      for (const x of parts) if (x.frame) x.frame(f);
      if (["hello", "agent", "agent_deleted", "runner"].includes(f.type)) draw();
    },
  };
}

function AgentView(name) {
  const base = `/api/agents/${enc(name)}`;
  const head = h("div");
  const note = restartNote(name);
  const flash = h("div");
  const list = h("div", {}, h("p", { class: "sub", text: "Loading…" }));
  const newErr = h("p", { class: "err", hidden: true });
  const trouble = startTrouble();
  let sessions = new Map();

  if (S.flash && S.flash.agent === name) {
    fill(flash, h("div", { class: "note", text: S.flash.text }));
    S.flash = null;
  }

  const newBtn = h("button", { class: "small primary", text: "New session", onclick: async () => {
    newBtn.disabled = true;
    newErr.hidden = true;
    try {
      const s = await api("POST", base + "/sessions");
      location.hash = `#/a/${enc(name)}/s/${enc(s.id)}`;
    } catch (x) { newErr.textContent = x.message; newErr.hidden = false; }
    newBtn.disabled = false;
  } });

  function drawHead() {
    const a = S.agents.get(name);
    note.update();
    if (!a) { head.replaceChildren(h("h1", { text: name })); return; }
    fill(head,
      h("div", { class: "row between" }, h("h1", { class: "grow" }, agentLabel(a)), agentBadge(a)),
      h("div", { class: "sub", text: agentMeta(a) }),
      agentLines(a),
      (a.error || a.definition_error) && h("p", { class: "err", text: a.definition_error || a.error }),
      needsLine(a, trouble, "note warn needs"),
      h("div", { class: "row", style: "margin-top: 10px" }, agentButtons(a, trouble),
        h("a", { class: "button small", href: `#/a/${enc(name)}/settings`, text: "Settings" }),
        EXTRA.actions.flatMap((fn) => fn(a))), trouble.el);
  }

  function drawSessions() {
    const ss = [...sessions.values()].sort((x, y) => y.updated_at - x.updated_at);
    list.replaceChildren(...(ss.length ? ss.map((s) => h("a", { class: "card session",
      href: `#/a/${enc(name)}/s/${enc(s.id)}` },
      h("div", { class: "row between" }, h("span", { class: "grow", text: s.title || "New session" }),
        s.main && badge("main", "main"), s.state === "working" && badge("working")),
      h("div", { class: "meta", text: ago(s.updated_at) + (s.queued ? ` · ${s.queued} waiting` : "") })))
      : [h("p", { class: "sub", text: "No sessions yet." })]));
  }

  async function load() {
    try {
      const [detail, ss] = await Promise.all([api("GET", base), api("GET", base + "/sessions")]);
      S.agents.set(name, detail);
      sessions = new Map(ss.map((s) => [s.id, s]));
      drawHead();
      drawSessions();
    } catch (x) {
      if (x.status === 404) el.replaceChildren(h("a", { class: "back", href: "#/", text: "‹ Agents" }),
        h("p", { class: "empty", text: `No agent named ${name}.` }));
      else list.replaceChildren(h("p", { class: "err", text: x.message }));
    }
  }

  drawHead();
  const parts = EXTRA.agentSections.map((make) => make(name)).filter(Boolean);
  const el = h("section", {}, h("a", { class: "back", href: "#/", text: "‹ Agents" }), head, note.el, flash,
    h("div", { class: "row between section-head" }, h("h2", { text: "Sessions" }), newBtn), newErr, list,
    parts.map((x) => x.el));
  load();
  return {
    el,
    async frame(f) {
      for (const x of parts) if (x.frame) x.frame(f);
      if (f.type === "agent" && f.agent.name === name) drawHead();
      else if (f.type === "session" && f.session.agent === name) { sessions.set(f.session.id, f.session); drawSessions(); }
      else if (f.type === "hello") {
        drawHead();
        try { sessions = new Map((await api("GET", base + "/sessions")).map((s) => [s.id, s])); drawSessions(); } catch { /* next hello */ }
      }
    },
  };
}

// ---------- settings ----------

function clone(v) { return JSON.parse(JSON.stringify(v)); }

// A site as typed or pasted (a URL, "*.github.com", "GitHub.com/foo") -> the domain the allow list takes. The server
// still checks it.
function siteOf(text) {
  return String(text || "").trim().toLowerCase().replace(/^[a-z][a-z0-9+.-]*:\/\//, "").split(/[/?#]/)[0]
    .replace(/^[^@]*@/, "").replace(/:\d*$/, "").replace(/^\*\./, "").replace(/\.$/, "");
}

function grow(ta) { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + 2 + "px"; }

// The PUT /definition body: the whole definition (fields the form doesn't show ride along untouched), and the
// persona only if it changed.
function settingsBody(draft, persona, savedPersona) {
  const body = { definition: draft };
  if (persona !== savedPersona) body.persona = persona;
  return body;
}

// The control a server error belongs to: the longest key that is the field or a parent of it (e.g. the mount row
// "computer.mounts.0" for "computer.mounts.0.readonly"); null if nothing on the page shows that field.
function fieldFor(field, keys) {
  let best = null;
  for (const k of keys) {
    if ((field === k || field.startsWith(k + ".")) && (!best || k.length > best.length)) best = k;
  }
  return best;
}

// One of an agent's raw files with its own Save (Settings > Advanced). set(text) shows the server's text unless
// the owner has edited this one.
function fileEditor(name, file, onSaved) {
  let loaded = "";
  const ta = h("textarea", { class: "code", autocapitalize: "none", "data-field": "file:" + file });
  ta.setAttribute("spellcheck", "false");
  const msg = h("p", { class: "sub", hidden: true });
  const err = h("p", { class: "err", hidden: true });
  const save = h("button", { type: "button", class: "small primary", text: "Save file", onclick: async () => {
    save.disabled = true;
    msg.hidden = err.hidden = true;
    try {
      const r = await api("PUT", `/api/agents/${enc(name)}/files/${file}`, { text: ta.value });
      loaded = ta.value;
      msg.textContent = `Saved (${String(r.commit).slice(0, 7)}).`;
      msg.hidden = false;
      await onSaved();
    } catch (x) { err.textContent = x.message; err.hidden = false; }
    save.disabled = false;
  } });
  return {
    el: h("div", { class: "card file" },
      h("div", { class: "row between", style: "margin-bottom: 8px" }, h("strong", { class: "mono", text: file }), save),
      ta, err, msg),
    set(text) { if (ta.value === loaded) ta.value = text; loaded = text; },
  };
}

// #/a/<name>/settings: the agent's definition as a form. It edits a copy of detail.definition and saves the whole
// object, so fields it doesn't show survive. Agent frames redraw the status and the save bar, never the form.
function SettingsView(name) {
  const base = `/api/agents/${enc(name)}`;
  let saved = null, draft = null, savedPersona = "", persona = "";
  let busy = "", flashMsg = "", flashTimer = 0, moreOpen = false, moreEl = null, brain = null;
  let lastHost = null;            // the last shared folder's host input, to focus after "Add a folder"
  let lastSite = null;            // the last allowed site's input, to focus after "Add a site"
  let keptMounts = null;          // shared folders set aside while "No computer" is picked, put back if it isn't
  const slots = new Map();        // field -> the error element under its control
  const fieldErrors = new Map();  // field -> message, so a redrawn control keeps its error

  const top = h("div");
  const form = h("div", {}, h("p", { class: "sub", text: "Loading…" }));
  const identity = h("div", { class: "card" }), personality = h("div", { class: "card" });
  const brainBox = h("div", { class: "card" }), computer = h("div", { class: "card" }), perms = h("div", { class: "card" });
  const toolsBox = h("div", { class: "card tools" }), secretsBox = h("div", { class: "card secrets" });
  const knows = h("div", { class: "card knows" });
  // What it knows: the owner profile and the presets come from their own routes (GET /api/owner, /api/authority).
  let ownerInfo = null, ownerErr = "", presetList = null, presetsErr = "";
  // The preview (POST <base>/directives/preview) of what the draft would be told, drawn in a box kept across redraws.
  let previewSeq = 0, previewShown = false;
  const previewStale = h("p", { class: "note", hidden: true, text: "You changed something since: Refresh to see it." });
  const previewBox = h("div", { class: "previewbox" });
  const previewDet = h("details", { class: "more preview-det" }, h("summary", { text: "Preview what it's told" }), previewBox);
  previewDet.addEventListener("toggle", () => { if (previewDet.open) loadPreview(); });
  const skillsBox = h("div", { class: "card skills" });
  let library = null, libraryErr = "";   // GET /api/skills: the shared skills
  let mine = null, mineErr = "";         // GET <base>/skills: its own skills, and what the saved definition gets
  let skillForm = null;                  // the open new-skill form, kept across redraws
  // Secrets and tools come from their own routes, loaded next to the definition; each draws when it arrives.
  let secretList = null;          // GET /api/secrets as a Map by name; null until it loads (or if it fails)
  let vaultList = null;           // GET /api/vault as a Map by entry name; likewise
  let lastMissing = null;         // the agent's missing_secrets last seen: a change means a secret was set or deleted
  let servers = null, serversErr = "";   // GET /api/gateway/servers
  // GET <base>/tools: what the saved definition gets, its decisions at the level picked (a preview while unsaved)
  let tools = null, toolsErr = "", toolsSeq = 0;
  const valueEds = new Map();     // secret name -> its open "Set value" editor, kept across redraws
  const openSets = new Set();     // servers whose tool list is expanded
  const toolsets = new Map();     // server -> its tool list's <details> as last drawn
  // GET /api/gateway/grants?agent=: its live "Allowed for an hour" grants; null until loaded (or if that fails)
  let grantList = null, grantTimer = 0;
  let scoped = false;             // an answer in the inbox left a grant or a rule: look again at its next agent frame
  let always = 0;                 // until when an "Always allow" answer's rule is still looked for (SCOPE_WAIT)
  const shownRules = new Set();   // "server/tool" of the rules a tool list shows; the others are listed apart
  const newSecretErr = h("p", { class: "err", hidden: true, "data-err": "secret-new" });
  const newSecret = h("input", { class: "mono", placeholder: "NEW_SECRET", maxLength: 64, autocomplete: "off",
    autocapitalize: "characters", "aria-label": "New secret's name", "data-field": "secret-new",
    oninput: () => { newSecretErr.hidden = true; },
    onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); addSecret(secretName(newSecret.value), true); } } });
  newSecret.setAttribute("spellcheck", "false");

  const isDirty = () => !!draft && (JSON.stringify(draft) !== JSON.stringify(saved) || persona !== savedPersona);
  // Sections from other files (addSettingsSection), made once; each draws with the form.
  const ctx = { name, base, get draft() { return draft; }, get saved() { return saved; },
    changed: (key) => changed(key), errFor: (key) => errFor(key), setDirectives: (list) => setDirectives(list),
    redraw: () => drawForm() };
  const extras = [];
  for (const spec of EXTRA.sections) extras.push({ spec, part: spec.make(ctx) });

  function errFor(key) {
    const el = h("p", { class: "err", "data-err": key, hidden: !fieldErrors.has(key), text: fieldErrors.get(key) || "" });
    slots.set(key, el);
    return el;
  }

  function changed(key) {
    if (key && fieldErrors.delete(key) && slots.has(key)) slots.get(key).hidden = true;
    flashMsg = "";
    if (previewShown) previewStale.hidden = false;
    drawBar();
    for (const x of extras) if (x.part.changed) x.part.changed(key);
  }

  const intro = (text) => h("p", { class: "sub intro", text });

  function drawTop() {
    const a = S.agents.get(name);
    fill(top, h("a", { class: "back", href: "#/a/" + enc(name), text: "‹ " + (agentTitle(a) || name) }),
      h("div", { class: "row between" }, h("h1", { class: "grow", text: "Settings" }), a && agentBadge(a)));
  }

  function drawIdentity() {
    fill(identity,
      h("div", { class: "ident" },
        h("div", { class: "emoji-field" }, h("label", { text: "Emoji" }),
          h("input", { class: "emoji-in", value: draft.emoji || "", placeholder: "🙂", maxLength: 16, autocomplete: "off",
            "aria-label": "Emoji", "data-field": "emoji",
            oninput: (e) => { draft.emoji = e.target.value.trim() || null; changed("emoji"); } })),
        h("div", { class: "grow" }, h("label", { text: "Name" }),
          h("input", { value: draft.display_name || "", placeholder: name, maxLength: 40, autocomplete: "off",
            "aria-label": "Name", "data-field": "display_name",
            oninput: (e) => { draft.display_name = e.target.value.trim() || null; changed("display_name"); } }))),
      errFor("emoji"), errFor("display_name"),
      h("p", { class: "sub" }, "ID ", h("span", { class: "mono", text: name }),
        " · it can't change; copy the agent for a new one"));
  }

  function drawPersonality() {
    const ta = h("textarea", { class: "persona", value: persona, rows: 8, "aria-label": "Personality",
      "data-field": "persona", oninput: () => { persona = ta.value; grow(ta); changed("persona"); } });
    const ask = confirmBox();
    const use = (t) => {
      const text = t.persona.split("{who}").join(draft.display_name || name);
      const apply = () => { ta.value = persona = text; grow(ta); changed("persona"); };
      if (persona.trim() && persona !== text) ask.ask(`Replace your text with the ${t.name} template?`, "Replace", apply, "danger");
      else apply();
    };
    const templates = (S.catalog && S.catalog.templates) || [];
    fill(personality,
      intro("Who it is and how it should work, in your words. It reads this at the start of every session."),
      ta, errFor("persona"),
      templates.length > 0 && h("div", { class: "field" }, h("label", { text: "Start from a template" }),
        h("div", { class: "chips" }, templates.map((t) =>
          chip("persona:" + t.id, false, (t.emoji ? t.emoji + " " : "") + t.name, () => use(t)))), ask.el),
      !(draft.directives || []).includes("persona.md") && h("p", { class: "note",
        text: "Its directives (set in the file) leave out persona.md, so it doesn't read this text." }));
    setTimeout(() => grow(ta), 0);
  }

  function drawBrain() {
    brain = brainPicker(draft, changed, errFor);
    fill(brainBox, intro("The AI that does the thinking."), brain.el);
  }

  // What its own computer reaches: the catalog's three choices. Its model and Valet's tools are always reachable;
  // `none` in the file means the same as `gateway`, so it shows as that.
  function internet() {
    const c = draft.computer;
    const nets = (S.catalog && S.catalog.networks) || [];
    const on = c.network === "none" ? "gateway" : c.network;
    return h("div", { class: "field" }, h("label", { text: "Internet" }),
      h("div", { class: "choices", role: "radiogroup", "aria-label": "Internet" }, nets.map((n) =>
        choice("network:" + n.id, n.id === on, n.name, [h("span", { class: "sub", text: n.summary })], () => {
          if (c.network === n.id) return;
          c.network = n.id;
          drawComputer();
          changed("computer.network");
        }))),
      errFor("computer.network"),
      on === "allowlist" && sites());
  }

  function sitesChanged() {
    for (const k of [...fieldErrors.keys()]) if (k.startsWith("computer.allowlist")) fieldErrors.delete(k);
    drawComputer();
    changed();
  }

  function addSite() {
    draft.computer.allowlist.push("");
    sitesChanged();
    if (lastSite) lastSite.focus();
  }

  function sites() {
    const c = draft.computer;
    lastSite = null;
    const rows = c.allowlist.map((d, i) => {
      const key = `computer.allowlist.${i}`;
      const input = h("input", { class: "mono", value: d, placeholder: "github.com", autocapitalize: "none",
        autocomplete: "off", inputmode: "url", "aria-label": "Site", "data-field": key,
        oninput: (e) => { c.allowlist[i] = e.target.value.trim(); changed(key); },
        onblur: (e) => {      // a pasted URL becomes its domain
          const v = siteOf(e.target.value);
          if (v !== e.target.value) { c.allowlist[i] = e.target.value = v; changed(key); }
        } });
      input.setAttribute("spellcheck", "false");
      lastSite = input;
      return h("div", { "data-row": key },
        h("div", { class: "site" }, input, h("button", { type: "button", class: "small danger", text: "Remove",
          onclick: () => { c.allowlist.splice(i, 1); sitesChanged(); } })),
        errFor(key));
    });
    return h("div", { class: "field" }, h("label", { text: "Sites it can reach" }),
      c.allowlist.length === 0 && h("p", { class: "sub", text: "None yet: it reaches only its model and Valet's tools." }),
      rows, errFor("computer.allowlist"),
      h("p", { class: "sub", text: "A site includes its subdomains: github.com covers api.github.com." }),
      h("button", { type: "button", class: "small", text: "Add a site", onclick: addSite }));
  }

  function folders() {
    const c = draft.computer;
    const mountsChanged = () => {
      for (const k of [...fieldErrors.keys()]) if (k.startsWith("computer.mounts")) fieldErrors.delete(k);
      drawComputer();
      changed();
    };
    lastHost = null;
    const rows = c.mounts.map((m, i) => {
      const key = `computer.mounts.${i}`;
      const path = h("input", { class: "mono", value: m.path, placeholder: "/work/notes", autocapitalize: "none",
        autocomplete: "off", "aria-label": "Path inside", "data-field": key + ".path",
        oninput: (e) => { m.path = e.target.value.trim(); changed(key + ".path"); } });
      const hostIn = h("input", { class: "mono", value: m.host, placeholder: "/Users/you/notes", autocapitalize: "none",
        autocomplete: "off", "aria-label": "Folder on the runner", "data-field": key + ".host",
        oninput: (e) => { m.host = e.target.value.trim(); changed(key + ".host"); },
        onblur: () => {   // suggest where it shows up inside
          if (m.path || !m.host) return;
          const last = m.host.replace(/\/+$/, "").split("/").pop();
          if (last) { m.path = path.value = "/work/" + last; changed(key + ".path"); }
        } });
      lastHost = hostIn;
      return h("div", { class: "mount" },
        h("label", { text: "Folder on the runner's machine" }), hostIn, errFor(key + ".host"),
        h("label", { text: "Shows up inside at" }), path, errFor(key + ".path"),
        h("div", { class: "row between" },
          h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!m.readonly,
            "data-field": key + ".readonly", onchange: (e) => { m.readonly = e.target.checked; changed(key + ".readonly"); } }),
          h("span", { text: "Read-only" })),
          h("button", { type: "button", class: "small danger", text: "Remove", onclick: () => {
            c.mounts.splice(i, 1);
            mountsChanged();
          } })),
        errFor(key + ".readonly"), errFor(key));
    });
    return h("div", { class: "field" }, h("label", { text: "Shared folders" }),
      c.mounts.length === 0 && h("p", { class: "sub", text: "None. Its computer only sees its own files." }),
      rows, errFor("computer.mounts"),
      h("button", { type: "button", class: "small", text: "Add a folder", onclick: () => {
        c.mounts.push({ host: "", path: "", readonly: false });
        mountsChanged();
        if (lastHost) lastHost.focus();
      } }));
  }

  function more() {
    const c = draft.computer;
    moreEl = h("details", { class: "more", open: moreOpen || !!c.image || fieldErrors.has("computer.image") },
      h("summary", { text: "More" }),
      h("div", { class: "field" }, h("label", { text: "Image" }),
        h("input", { class: "mono", value: c.image || "", placeholder: "the runner's default", autocapitalize: "none",
          autocomplete: "off", "aria-label": "Image", "data-field": "computer.image",
          oninput: (e) => { c.image = e.target.value.trim() || null; changed("computer.image"); } }),
        errFor("computer.image")));
    moreEl.addEventListener("toggle", () => { moreOpen = moreEl.open; });
    return moreEl;
  }

  // This machine: the folder it works in (computer.workdir); empty, its own folder under the runner's data.
  function workdir() {
    const c = draft.computer;
    const input = h("input", { class: "mono", value: c.workdir || "", placeholder: "its own folder, under the runner's data",
      autocapitalize: "none", autocomplete: "off", "aria-label": "Working folder", "data-field": "computer.workdir",
      oninput: (e) => { c.workdir = e.target.value.trim() || null; changed("computer.workdir"); } });
    input.setAttribute("spellcheck", "false");
    return h("div", { class: "field" }, h("label", { text: "Working folder" }), input, errFor("computer.workdir"),
      h("p", { class: "sub", text: "Where it runs and its sessions start: the absolute path of a folder that exists on "
        + "the runner's machine, like /Users/you/Code/project." }));
  }

  function drawComputer() {
    const c = draft.computer;
    const levels = S.catalog ? [...S.catalog.levels] : [];
    if (!levels.some((l) => l.id === c.level)) levels.push({ id: c.level, name: c.level, summary: "Set in the file." });
    fill(computer, intro("Where it runs and what it can reach."),
      h("div", { class: "choices", role: "radiogroup", "aria-label": "Computer" }, levels.map((l) =>
        choice("level:" + l.id, l.id === c.level, l.name, [h("span", { class: "sub", text: l.summary })], () => {
          if (c.level === l.id) return;
          if (l.id === "none" && c.mounts.length) { keptMounts = c.mounts; c.mounts = []; }
          else if (c.level === "none" && keptMounts && !c.mounts.length) { c.mounts = keptMounts; keptMounts = null; }
          c.level = l.id;
          drawComputer();
          drawPerms();
          changed("computer.level");
        }))),
      errFor("computer.level"),
      c.level === "host" && [h("div", { class: "note warn" }, h("strong", { text: "Careful: " }),
        "on this machine it isn't in a container. It runs as the runner's user and can read and change everything that "
        + "user can, and reach the network. Pick this only for an agent you trust."), workdir()],
      c.level === "none" && [h("p", { class: "note", text: "It has no files and runs no commands. It reaches its "
        + "model and the tool servers you turn on below, and nothing else." }), errFor("computer.mounts"), more()],
      c.level === "container" && [internet(), folders(), more()]);
  }

  function drawPerms() {
    const list = (S.catalog && S.catalog.permissions) || [];
    const host = draft.computer.level === "host";
    fill(perms, intro("What it may do without asking you first."),
      h("div", { class: "choices", role: "radiogroup", "aria-label": "Permissions" }, list.map((p) =>
        choice("permissions:" + p.id, p.id === draft.permissions, p.name, [h("span", { class: "sub", text: p.summary }),
          host && p.host && h("span", { class: "warn-text", text: p.host })], () => {
          draft.permissions = p.id;
          drawPerms();
          loadTools();
          drawTools();
          drawKnows();      // the authority suggested for the level
          changed("permissions");
        }))),
      errFor("permissions"));
  }

  const levelName = (id) => ((S.catalog && S.catalog.permissions.find((p) => p.id === id)) || { name: id }).name;

  // ---- What it knows: the layers it's told besides its personality, and a preview of all of it ----
  function setDirectives(list) {
    draft.directives = list;
    drawKnows();
    changed("directives");
  }

  // A layer as a row with a switch.
  function layerSwitch(key, title, sub, extra) {
    const on = (draft.directives || []).includes(key);
    const sw = h("input", { type: "checkbox", class: "switch", checked: on, "aria-label": title,
      "data-field": "directive:" + key, onchange: (e) => setDirectives(withDirective(draft.directives, key, e.target.checked)) });
    sw.setAttribute("role", "switch");
    return h("div", { class: "layer-row", "data-row": "directive:" + key },
      h("label", { class: "check" }, h("span", { class: "grow" }, h("span", { class: "name", text: title }),
        h("span", { class: "sub desc", text: sub })), sw), extra);
  }

  function drawKnows() {
    const preset = presetOf(draft.directives);
    const suggested = S.catalog && S.catalog.authority ? S.catalog.authority[draft.permissions] : null;
    const names = presetList ? presetList.map((p) => p.name).sort(presetSort) : [];
    const missing = preset && presetList && !names.includes(preset) ? preset : null;
    const ownerText = ownerInfo ? ownerInfo.text.trim() : "";
    const ownerSub = ownerErr || (!ownerInfo ? "Loading…" : ownerText ? gist(ownerText)
      : "You haven't written it yet, so it learns nothing about you from this.");
    const authority = presetsErr ? h("p", { class: "err", text: presetsErr })
      : !presetList ? h("p", { class: "sub", text: "Loading…" })
        : h("div", { class: "choices", role: "radiogroup", "aria-label": "Standing authority" },
          choice("authority:-", !preset, "None", [h("span", { class: "sub", text: "It acts on what you ask, with its own "
            + "judgment about the rest." })], () => setDirectives(withPreset(draft.directives, null))),
          [...names, ...(missing ? [missing] : [])].map((n) => {
            const p = presetList.find((x) => x.name === n);
            return choice("authority:" + n, preset === n, p ? presetLabel(p) : presetTitle(n), [
              p && h("span", { class: "sub", text: gist(p.text) }),
              n === missing && h("span", { class: "warn-text", text: "There's no such preset any more: pick another." }),
              n === suggested && h("span", { class: "ok", text: "Suggested for " + levelName(draft.permissions) })],
            () => setDirectives(withPreset(draft.directives, n)));
          }));
    fill(knows,
      intro("Besides its personality, what it's told at the start of every session. Valet joins it all in this "
        + "order: about you, its standing authority, its personality, its environment, its memory."),
      !(draft.directives || []).includes("persona.md") && h("div", { class: "note", "data-row": "directive:persona.md" },
        "It isn't told its personality: its agent.yaml leaves persona.md out of its directives. ",
        h("button", { type: "button", class: "small", "data-field": "directive:persona.md", text: "Tell it",
          onclick: () => setDirectives(withDirective(draft.directives, "persona.md", true)) })),
      layerSwitch("owner", "About you", ownerSub,
        h("a", { class: "more-link", href: "#/directives", text: ownerInfo && !ownerText ? "Write about you ›" : "Edit about you ›" })),
      h("div", { class: "field" }, h("label", { text: "Standing authority" }),
        h("p", { class: "sub intro", text: "What it may do for you without asking first. Its permission level above "
          + "still applies: Valet enforces that." }),
        authority, errFor("directives"),
        h("a", { class: "more-link", href: "#/directives", text: "Edit the presets ›" })),
      layerSwitch("environment", "Its environment", "What Valet set up for it, written at every start: its computer, "
        + "permission level, tools and what each call does, its skills, its secrets by name, and how Valet enforces "
        + "your limits. It knows what's allowed, so it acts instead of hesitating."),
      previewDet);
  }

  async function loadPreview() {
    const seq = ++previewSeq;
    previewStale.hidden = true;
    fill(previewBox, h("p", { class: "sub", text: "Loading…" }));
    let p;
    try { p = await api("POST", base + "/directives/preview", { definition: draft, persona }); } catch (x) {
      if (seq === previewSeq) fill(previewBox, h("p", { class: "err", text: x.message }));
      return;
    }
    if (seq !== previewSeq) return;
    previewShown = true;
    const where = p.harness === "claude" ? "Added to Claude Code's own system prompt, in every session."
      : p.harness === "deepseek" ? "Written to its AGENTS.md, which DeepSeek Harness reads in every session."
        : "Given to its harness in every session.";
    fill(previewBox, previewStale,
      p.error && h("p", { class: "err", text: p.error }),
      h("div", { class: "chips layers" }, p.layers.map((l) => h("span", { class: "layer " + l.state, "data-layer": l.name,
        text: layerName(l.name) + (LAYER_STATES[l.state] ? " · " + LAYER_STATES[l.state] : "") }))),
      p.text ? [h("p", { class: "sub", text: where + (isDirty() ? " With your unsaved changes, as if it started now."
        : " As if it started now.") }),
        h("pre", { class: "assembled", "data-row": "preview", text: p.text })]
        : h("p", { class: "sub", "data-row": "preview", text: "Nothing: it gets only its harness's own instructions." }),
      h("button", { type: "button", class: "small", text: "Refresh", onclick: loadPreview }));
  }

  async function loadDirectives() {
    const [o, ps] = await Promise.allSettled([api("GET", "/api/owner"), api("GET", "/api/authority")]);
    if (o.status === "fulfilled") { ownerInfo = o.value; ownerErr = ""; } else ownerErr = o.reason.message;
    if (ps.status === "fulfilled") { presetList = ps.value; presetsErr = ""; } else presetsErr = ps.reason.message;
    if (draft) drawKnows();
  }
  const mcpOf = (d) => (d && d.tools && d.tools.mcp) || [];

  // ---- Tools: the gateway's servers as switches over tools.mcp; a saved one lists its tools, what a call to each
  // does, and the rule for it (tools.rules); then any other rules, and its live hour grants ----
  function drawTools() {
    const mcp = mcpOf(draft), savedMcp = mcpOf(saved);
    // Servers that come with another setting (valet.memory with Memory) aren't switched here.
    const known = servers ? servers.servers.filter((s) => !s.comes_with) : [];
    const names = [...known.map((s) => s.name), ...mcp.filter((n) => !known.some((s) => s.name === n))];
    const shown = mcp.some((n) => savedMcp.includes(n));     // some server here lists its tools
    const stale = !!tools && tools.permissions !== draft.permissions;   // a level's decisions on their way
    // A tool list the owner opened or closed stays so. Read from the lists themselves: their toggle event can come
    // after a redraw.
    for (const [n, det] of toolsets) if (det.open) openSets.add(n); else openSets.delete(n);
    toolsets.clear();
    shownRules.clear();
    fill(toolsBox,
      intro("Tool servers it can use, like GitHub. Valet connects them for it and checks every call against its "
        + "permissions, and against your rule for the tool if you set one."),
      servers && servers.error && h("div", { class: "note warn" },
        h("strong", { text: "The servers file has an error, so its servers can't be used. " }),
        h("span", { text: servers.error })),
      serversErr && h("p", { class: "err", text: serversErr }),
      !servers && !serversErr && h("p", { class: "sub", text: "Loading…" }),
      servers && !names.length && h("p", { class: "sub", text: "No tool servers yet." }),
      shown && draft.permissions !== saved.permissions && h("p", { class: "note", text: `What each tool does `
        + `below is for ${levelName(draft.permissions)}, which isn't saved yet.` }),
      names.length > 0 && h("div", { class: "rows" },
        names.map((n) => serverRow(n, known.find((s) => s.name === n), mcp.includes(n), savedMcp.includes(n), stale))),
      otherRules(),
      grantsEl(),
      errFor("tools.mcp"), errFor("tools.rules"),
      h("a", { class: "more-link", href: "#/secrets-tools", text: "Manage tool servers ›" }));
  }

  function serverRow(n, s, on, savedOn, stale) {
    const sw = h("input", { type: "checkbox", class: "switch", checked: on, "aria-label": n, "data-field": "mcp:" + n,
      onchange: (e) => toggleServer(n, e.target.checked) });
    sw.setAttribute("role", "switch");
    return h("div", { class: "server", "data-row": n },
      h("label", { class: "check" }, h("span", { class: "grow" }, h("span", { class: "name", text: n }),
        s && s.description && h("span", { class: "sub desc", text: brief(s.description, 160) }),
        !s && servers && h("span", { class: "warn-text desc", text: "Valet has no tool server by this name." })), sw),
      on && serverTools(n, savedOn, stale));
  }

  // What the agent gets from a server it has: its tools, each with what a call does (the saved level's decisions,
  // the draft's rules, the live grants) and a rule to pick; first, a rule for all of them.
  function serverTools(n, savedOn, stale) {
    if (!savedOn) return h("p", { class: "sub", text: "Save to see its tools." });
    if (toolsErr) return h("p", { class: "err", text: toolsErr });
    const t = tools && tools.servers.find((x) => x.server === n);
    if (!t) return h("p", { class: "sub", text: "Loading its tools…" });
    if (!t.ok) return h("p", { class: "warn-text", text: "Can't use it right now: " + (t.error || "unreachable") });
    if (!t.tools.length) return h("p", { class: "sub", text: "It offers no tools." });
    const rules = rulesOf(draft)[n] || {};
    // A server that decides each call itself (valet.browser, by its site rules): only a Block here applies.
    const own = !!t.own_rules;
    const fates = t.tools.map((x) => (own ? ownFate(x, rules) : toolFate(x, rules, grantFor(n, x.tool))));
    const count = (d) => fates.filter((f) => f.decision === d).length;
    const parts = own ? [count("deny") ? `${count("deny")} blocked, the others` : "each", "decided by its own rules"]
      : [[count("allow"), "run"], [count("ask"), "ask you"], [count("deny"), "blocked"]]
        .filter(([k]) => k).map(([k, what]) => k + " " + what);
    shownRules.add(n + "/*");
    t.tools.forEach((x) => shownRules.add(n + "/" + x.tool));
    const det = h("details", { class: "toolset" + (stale ? " stale" : ""), open: openSets.has(n) },
      h("summary", { text: `${t.tools.length} tool${t.tools.length > 1 ? "s" : ""}: ${parts.join(own ? " " : ", ")}` }),
      own && h("p", { class: "sub own-rules", text: "Valet decides each call to it by its own rules"
        + (n === "valet.browser" ? ": the site rules in Browser, below" : "") + ", whatever the level. A Block here "
        + "still takes a tool away." }),
      h("div", { class: "rows" }, allToolsRow(n, rules, own), t.tools.map((x, i) => ruleRow(n, x, rules, fates[i], own))));
    toolsets.set(n, det);
    return det;
  }

  // A tool of a server that decides its own calls: blocked by a deny rule, else listed (the server decides).
  function ownFate(x, rules) {
    return (ownRule(rules, x.tool) || ownRule(rules, ALL_TOOLS)) === "deny" ? { decision: "deny", source: "rule" }
      : { decision: "allow", source: "server" };
  }

  // The server's "*" rule: every tool without one of its own.
  function allToolsRow(n, rules, own) {
    const all = ownRule(rules, ALL_TOOLS);
    return h("div", { class: "toolrow all-tools", "data-tool": n + "/*" },
      h("div", { class: "row" }, h("span", { class: "grow name", text: "All its tools" })),
      h("div", { class: "sub", text: "A rule here covers each tool below that has none of its own." }),
      ruleSelect(`rule:${n}:*`, all, own ? "Its own rules decide" : "As each one's level says",
        (v) => setRule(n, ALL_TOOLS, v), "All of " + n, own));
  }

  // One tool: its title, what a call does now (and why, if it's an hour's grant), what it says it's for, its rule.
  function ruleRow(n, x, rules, fate, own) {
    const [cls, text] = DECISION[fate.decision] || ["", String(fate.decision || "")];
    const all = ownRule(rules, ALL_TOOLS);
    const level = x.level_decision || (x.source === "level" || !x.source ? x.decision : null);
    const why = fate.source === "grant" ? `Allowed for an hour, until ${clock(fate.grant.expires_at)}`
      : null;
    const fallback = own ? (all === "deny" ? "Same as all its tools (blocked)" : "Its own rules decide")
      : all ? `Same as all its tools (${DECISION[all][1]})`
        : `As its level says${level && DECISION[level] ? " (" + DECISION[level][1] + ")" : ""}`;
    return h("div", { class: "toolrow", "data-tool": x.name || x.tool },
      h("div", { class: "row" }, h("span", { class: "grow name", text: x.title || x.tool || x.name }),
        own && fate.decision !== "deny" ? badge("", "its rules") : badge(cls, text)),
      x.description && h("div", { class: "sub", text: brief(x.description, 160) }),
      why && h("div", { class: "sub why", text: why }),
      ruleSelect(`rule:${n}:${x.tool}`, ownRule(rules, x.tool), fallback, (v) => setRule(n, x.tool, v),
        "Rule for " + (x.title || x.tool), own));
  }

  // own: a server that decides its own calls, where only Block means anything (a rule it already has stays shown).
  function ruleSelect(field, value, fallback, onPick, label, own) {
    const choices = own ? RULE_CHOICES.filter(([v]) => v === "deny" || v === value) : RULE_CHOICES;
    const sel = h("select", { class: "rule", "aria-label": label, "data-field": field },
      h("option", { value: "", text: fallback }),
      choices.map(([v, words]) => h("option", { value: v, text: words })));
    sel.value = value || "";
    sel.addEventListener("change", () => onPick(sel.value || null));
    return sel;
  }

  // Rules the tool lists don't show: for a server it no longer has, one that can't be reached, or a tool its server
  // no longer offers. They still apply; each can be removed.
  function otherRules() {
    const rest = [];
    for (const [server, list] of Object.entries(rulesOf(draft))) {
      for (const [tool, rule] of Object.entries(list)) {
        if (!shownRules.has(server + "/" + tool)) rest.push([server, tool, rule]);
      }
    }
    if (!rest.length) return null;
    return h("div", { class: "other-rules" }, h("h3", { text: "Other rules" }),
      h("p", { class: "sub", text: "For tools not listed above. They apply whenever the tool is there." }),
      h("div", { class: "rows" }, rest.map(([server, tool, rule]) => {
        const [cls, text] = DECISION[rule] || ["", rule];
        return h("div", { class: "row rule-row", "data-row": `rule:${server}/${tool}` },
          h("span", { class: "grow mono", text: server + " › " + (tool === ALL_TOOLS ? "all its tools" : tool) }),
          badge(cls, text),
          h("button", { type: "button", class: "small", text: "Remove", onclick: () => setRule(server, tool, null) }));
      })));
  }

  // Its live hour grants: "Allow for an hour" answers in the inbox. Revoking one takes effect at the next call.
  function grantsEl() {
    if (!grantList || !grantList.length) return null;
    return h("div", { class: "grants" }, h("h3", { text: "Allowed for an hour" }),
      h("div", { class: "rows" }, grantList.map((g) => {
        const err = h("p", { class: "err", hidden: true });
        const btn = h("button", { type: "button", class: "small", text: "Revoke" });
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          err.hidden = true;
          try { await api("DELETE", `/api/gateway/grants/${enc(g.id)}`); } catch (x) {
            if (x.status !== 404) { err.textContent = x.message; err.hidden = false; btn.disabled = false; return; }
          }
          loadGrants();
        });
        return h("div", { "data-row": "grant:" + g.id },
          h("div", { class: "row" }, h("span", { class: "grow mono", text: g.server + " › " + g.tool }),
            h("span", { class: "sub", text: "until " + clock(g.expires_at) }), btn), err);
      })));
  }

  const grantFor = (server, tool) => (grantList || []).find((g) => g.server === server && g.tool === tool) || null;

  // A rule picked: null puts the tool back on its level (or the server's "*"). A server left with no rules goes.
  // The draft's rules keep the saved ones' order, so a rule taken off and put back is no change.
  function setRule(server, tool, rule) {
    if (!draft.tools) draft.tools = { mcp: [], skills: [] };
    const next = clone(rulesOf(draft));
    const list = next[server] || {};
    if (rule) list[tool] = rule; else delete list[tool];
    next[server] = list;
    draft.tools.rules = orderLike(next, rulesOf(saved));
    drawTools();
    changed("tools.rules");
  }

  async function loadGrants() {
    clearTimeout(grantTimer);
    try { grantList = await api("GET", "/api/gateway/grants?agent=" + enc(name)); } catch { /* keep the last */ }
    // Redrawn when the soonest runs out, so an expired grant doesn't linger.
    const soonest = Math.min(...(grantList || []).map((g) => g.expires_at));
    if (Number.isFinite(soonest)) {
      grantTimer = setTimeout(() => { if (el.isConnected) loadGrants(); },
        Math.min(Math.max(soonest * 1000 - Date.now(), 0) + 500, 3600 * 1000));
    }
    if (draft) drawTools();
  }

  // A rule written elsewhere ("Always allow" in the inbox, another tab): the form takes it, so a save from here
  // doesn't take it back. Unedited, the whole definition is taken again; with edits, only the rules the form
  // hasn't changed itself. Resolves true if the rules had moved (and were taken).
  async function syncRules() {
    if (!saved || busy) return false;
    let d;
    try { d = await api("GET", base); } catch { return false; }
    if (!d.definition || !saved || busy) return false;
    const theirs = rulesOf(d.definition), before = rulesOf(saved);
    if (JSON.stringify(theirs) === JSON.stringify(before)) return false;
    if (!isDirty()) { take(d); return true; }
    const mine = clone(rulesOf(draft));
    for (const server of new Set([...Object.keys(theirs), ...Object.keys(before)])) {
      const t = theirs[server] || {}, b = before[server] || {};
      for (const tool of new Set([...Object.keys(t), ...Object.keys(b)])) {
        if (ownRule(mine[server] || {}, tool) !== ownRule(b, tool)) continue;   // the form changed this one
        mine[server] = mine[server] || {};
        if (ownRule(t, tool)) mine[server][tool] = t[tool]; else delete mine[server][tool];
      }
    }
    saved.tools = { ...(saved.tools || {}), rules: clone(theirs) };
    if (!draft.tools) draft.tools = { mcp: [], skills: [] };
    draft.tools.rules = orderLike(mine, theirs);
    yamlEd.set((d.files && d.files["agent.yaml"]) || "");
    loadTools(true);
    drawTools();
    drawBar();
    return true;
  }

  function toggleServer(n, on) {
    if (!draft.tools) draft.tools = { mcp: [], skills: [] };
    const savedMcp = mcpOf(saved);
    const rank = (x) => (savedMcp.includes(x) ? savedMcp.indexOf(x) : savedMcp.length);
    const rest = draft.tools.mcp.filter((x) => x !== n);
    // Back on: in its saved place, so off and on again is no change.
    draft.tools.mcp = on ? [...rest, n].sort((x, y) => rank(x) - rank(y)) : rest;
    drawTools();
    changed("tools.mcp");
  }

  // ---- Skills: shared ones as switches over tools.skills ("*": all of them); its own, listed, edited elsewhere ----
  const skillsOf = (d) => (d && d.tools && d.tools.skills) || [];

  function drawSkills() {
    const picked = skillsOf(draft), all = picked.includes("*");
    const known = library || [];
    const own = mine ? mine.own.map((s) => s.name) : [];
    const names = [...known.map((s) => s.name), ...picked.filter((n) => n !== "*" && !known.some((s) => s.name === n))];
    const allSw = h("input", { type: "checkbox", class: "switch", checked: all, "aria-label": "All shared skills",
      "data-field": "skills:*", onchange: (e) => toggleAllSkills(e.target.checked) });
    allSw.setAttribute("role", "switch");
    fill(skillsBox,
      intro("Instructions it loads when a task calls for them, like how you want code reviewed. It gets the shared "
        + "skills you switch on, and all of its own."),
      libraryErr && h("p", { class: "err", text: libraryErr }),
      !library && !libraryErr && h("p", { class: "sub", text: "Loading…" }),
      library && h("div", { class: "rows" },
        h("div", { class: "server", "data-row": "skills:*" }, h("label", { class: "check" }, h("span", { class: "grow" },
          h("span", { class: "name", text: "All shared skills" }),
          h("span", { class: "sub desc", text: "Every skill in the library, and any added later" })), allSw)),
        names.map((n) => sharedSkillRow(n, known.find((s) => s.name === n), all || picked.includes(n), all,
          own.includes(n)))),
      library && !known.length && h("p", { class: "sub", text: "The library has no skills yet." }),
      errFor("tools.skills"),
      mine && mine.problems.length > 0 && h("div", { class: "note warn" },
        h("strong", { text: "Left out, until fixed: " }), h("span", { class: "pre", text: mine.problems.join("\n") })),
      h("h3", { class: "own-head", text: "Its own skills" }),
      mineErr && h("p", { class: "err", text: mineErr }),
      !mine && !mineErr && h("p", { class: "sub", text: "Loading…" }),
      mine && (mine.own.length ? h("div", { class: "rows" }, mine.own.map(ownSkillRow))
        : h("p", { class: "sub", text: "None yet. Its own skills are only for it." })),
      skillForm ? skillForm.el : h("div", { class: "new-skill" }, h("button", { type: "button", class: "small",
        text: "New skill", onclick: () => {
          skillForm = newSkillForm(base + "/skills", "newskill inset", () => { skillForm = null; loadSkills(); },
            () => { skillForm = null; drawSkills(); });
          drawSkills();
          skillForm.focus();
        } })),
      h("a", { class: "more-link", href: "#/skills", text: "Manage the skill library ›" }));
  }

  function sharedSkillRow(n, s, on, locked, shadowed) {
    const sw = h("input", { type: "checkbox", class: "switch", checked: on, disabled: locked, "aria-label": n,
      "data-field": "skill:" + n, onchange: (e) => toggleSkill(n, e.target.checked) });
    sw.setAttribute("role", "switch");
    return h("div", { class: "server", "data-row": "skill:" + n },
      h("label", { class: "check" }, h("span", { class: "grow" }, h("span", { class: "name mono", text: n }),
        s && s.description && h("span", { class: "sub desc", text: brief(s.description, 160) }),
        !s && h("span", { class: "warn-text desc", text: "The library has no skill by this name." }),
        s && s.error && h("span", { class: "warn-text desc", text: "It has a problem, so it's left out: " + s.error }),
        shadowed && h("span", { class: "sub desc", text: "Its own skill of this name is used instead." })), sw));
  }

  function ownSkillRow(s) {
    return h("a", { class: "skill row", href: skillHash(s.name, name), "data-row": "own:" + s.name },
      h("span", { class: "grow" }, h("span", { class: "name mono", text: s.name }),
        s.description && h("span", { class: "sub desc", text: brief(s.description, 160) }),
        s.error && h("span", { class: "warn-text desc", text: "It has a problem, so it's left out: " + s.error }),
        s.replaces_shared && h("span", { class: "sub desc", text: "Used instead of the shared skill of this name." })),
      h("span", { class: "chev", text: "›" }));
  }

  // Off: back to what it picks by name (the saved list), or, if it had all of them, each skill it has now.
  function toggleAllSkills(on) {
    if (!draft.tools) draft.tools = { mcp: [], skills: [] };
    const savedPicked = skillsOf(saved);
    draft.tools.skills = on ? ["*"] : savedPicked.includes("*") ? (library || []).map((s) => s.name) : [...savedPicked];
    drawSkills();
    changed("tools.skills");
  }

  function toggleSkill(n, on) {
    if (!draft.tools) draft.tools = { mcp: [], skills: [] };
    const savedPicked = skillsOf(saved);
    const rank = (x) => (savedPicked.includes(x) ? savedPicked.indexOf(x) : savedPicked.length);
    const rest = draft.tools.skills.filter((x) => x !== n);
    draft.tools.skills = on ? [...rest, n].sort((x, y) => rank(x) - rank(y)) : rest;
    drawSkills();
    changed("tools.skills");
  }

  async function loadSkills() {
    const [lib, own] = await Promise.allSettled([api("GET", "/api/skills"), api("GET", base + "/skills")]);
    if (lib.status === "fulfilled") { library = lib.value; libraryErr = ""; } else libraryErr = lib.reason.message;
    if (own.status === "fulfilled") { mine = own.value; mineErr = ""; } else mineErr = own.reason.message;
    if (draft) drawSkills();
  }

  // ---- Secrets: the names it gets as env vars; values are set here too, but never shown ----
  function secretStatus(n) {   // true set, false not set, null unknown
    if (secretList) return !!(secretList.get(n) || {}).set;
    const a = S.agents.get(name);
    if (!a || !(saved.secrets || []).includes(n)) return null;
    return !(a.missing_secrets || []).includes(n);
  }

  function drawSecrets() {
    const list = draft.secrets || [];
    const names = list.filter((s) => typeof s === "string");
    for (const n of [...valueEds.keys()]) if (!names.includes(n)) valueEds.delete(n);
    const have = secretList ? [...secretList.values()].filter((s) => s.set && !names.includes(s.name)) : [];
    const listed = new Set(list.filter((s) => s && typeof s === "object").map((s) => s.vault));
    const logins = vaultList ? [...vaultList.values()].filter((v) => v.has_password && !listed.has(v.name)) : [];
    const fromVault = logins.length > 0 && h("select", { class: "from-vault", "aria-label": "A password from the vault",
      "data-field": "secret-vault", onchange: (e) => { if (e.target.value) addVault(e.target.value); } },
    h("option", { value: "", text: "A password from the vault…" }),
    logins.map((v) => h("option", { value: v.name, text: `${v.name} (${v.site}) as ${vaultEnv(v.name)}` })));
    fill(secretsBox,
      intro("Keys and tokens it gets as environment variables, like GITHUB_TOKEN, and passwords from the vault. A "
        + "value is stored encrypted and never shown again."),
      list.length ? h("div", { class: "rows" }, list.map((s, i) => typeof s === "string" ? secretRow(s, i) : vaultRow(s, i)))
        : h("p", { class: "sub", text: "None." }),
      errFor("secrets"),
      h("div", { class: "field" }, h("label", { text: "Add a secret" }),
        have.length > 0 && h("div", { class: "chips" },
          have.map((s) => chip("secret:" + s.name, false, s.name, () => addSecret(s.name, false)))),
        h("div", { class: "row addrow" }, h("div", { class: "grow" }, newSecret),
          h("button", { type: "button", class: "small", text: "Add",
            onclick: () => addSecret(secretName(newSecret.value), true) })),
        fromVault, newSecretErr));
  }

  function secretRow(n, i) {
    const st = secretStatus(n);
    const info = secretList && secretList.get(n);
    const others = info ? [...(info.used_by || []).filter((x) => x !== name).map(agentName),
      ...(info.servers || []).map((x) => `the ${x} server`)] : [];
    const ed = valueEds.get(n);
    const sub = [st === false ? "Not set: it can't start until it is" : info && info.updated_at && "Set " + ago(info.updated_at),
      others.length > 0 && "also used by " + andList(others)].filter(Boolean).join(" · ");
    return h("div", { class: "secret", "data-row": n },
      h("div", { class: "row" }, h("span", { class: "mono grow name", text: n }),
        st === true ? badge("idle", "set") : st === false ? badge("waiting", "not set") : null),
      sub && h("div", { class: "sub", text: sub }),
      ed ? ed.el : h("div", { class: "row acts" },
        h("button", { type: "button", class: "small" + (st === false ? " primary" : ""),
          text: st === false ? "Set value" : "Change value", onclick: () => openValue(n) }),
        h("button", { type: "button", class: "small danger", text: "Remove", onclick: () => {
          draft.secrets.splice(i, 1);
          valueEds.delete(n);
          drawSecrets();
          changed("secrets");
        } })));
  }

  // A vault entry's password, as the env var the entry names (or the one derived from its name). The entry itself
  // (and its password) is changed in the vault (vault.js); here it's listed or removed.
  function vaultRow(s, i) {
    const entry = String(s.vault || "");
    const v = vaultList && vaultList.get(entry);
    const a = S.agents.get(name);
    // true: it has a password; false: no entry, or none; null: unknown (the vault didn't load, and it isn't saved)
    let st = null;
    if (vaultList) st = !!(v && v.has_password);
    else if (a && (saved.secrets || []).some((x) => x && x.vault === entry)) {
      st = !(a.missing_secrets || []).includes("vault:" + entry);
    }
    const what = v ? [v.label, v.username && "as " + v.username, "on " + v.site].filter(Boolean).join(" ") : "";
    const sub = vaultList && !v ? `There's no vault entry ${entry}: add it in the vault, or it can't start`
      : st === false ? `${entry} has no password: it can't start until it does`
        : `The password of ${entry}` + (what ? `, ${what}` : "");
    return h("div", { class: "secret", "data-row": "vault:" + entry },
      h("div", { class: "row" }, h("span", { class: "mono grow name", text: s.env || vaultEnv(entry) }),
        st === true ? badge("idle", "vault") : st === false ? badge("waiting", v ? "no password" : "not in the vault") : null),
      h("div", { class: "sub", text: sub }),
      h("div", { class: "row acts" },
        h("a", { class: "button small", href: v ? "#/vault/" + vaultPath(entry) : "#/vault", text: v ? "Open in the vault"
          : "Vault" }),
        h("button", { type: "button", class: "small danger", text: "Remove", onclick: () => {
          draft.secrets.splice(i, 1);
          drawSecrets();
          changed("secrets");
        } })));
  }

  // "A password from the vault…": the entry, as its derived env var.
  function addVault(entry) {
    const show = (msg) => { newSecretErr.textContent = msg; newSecretErr.hidden = false; drawSecrets(); };
    newSecretErr.hidden = true;
    const env = vaultEnv(entry);
    if (!draft.secrets) draft.secrets = [];
    if (draft.secrets.some((s) => s === env || (s && typeof s === "object" && (s.env || vaultEnv(s.vault)) === env))) {
      return show(`It already has ${env}.`);
    }
    draft.secrets.push({ vault: entry, env: null });
    drawSecrets();
    changed("secrets");
  }

  function openValue(n) {
    const ed = valueEditor(n, (s) => {
      valueEds.delete(n);
      if (secretList) secretList.set(s.name, s);
      drawSecrets();
      loadSecrets();
    }, () => { valueEds.delete(n); drawSecrets(); });
    valueEds.set(n, ed);
    drawSecrets();
    ed.focus();
  }

  function addSecret(n, typed) {
    const show = (msg) => { newSecretErr.textContent = msg; newSecretErr.hidden = false; };
    newSecretErr.hidden = true;
    if (!n) return show("Type its name, like GITHUB_TOKEN.");
    if (!SECRET_RE.test(n)) return show(SECRET_RULE);
    if (!draft.secrets) draft.secrets = [];
    if (draft.secrets.some((s) => s === n || (s && typeof s === "object" && (s.env || vaultEnv(s.vault)) === n))) {
      return show(`It already has ${n}.`);
    }
    draft.secrets.push(n);
    if (typed) newSecret.value = "";
    changed("secrets");
    if (secretStatus(n) === false) openValue(n); else drawSecrets();
  }

  async function loadSecrets() {
    const [ss, vs] = await Promise.allSettled([api("GET", "/api/secrets"), api("GET", "/api/vault")]);
    if (ss.status === "fulfilled") secretList = new Map(ss.value.map((s) => [s.name, s]));    // else keep the last
    if (vs.status === "fulfilled") vaultList = new Map(vs.value.map((v) => [v.name, v]));
    if (draft) drawSecrets();
  }

  async function loadServers() {
    try { servers = await api("GET", "/api/gateway/servers"); serversErr = ""; } catch (x) { serversErr = x.message; }
    if (draft) drawTools();
  }

  // The saved definition's tools, decided at the level the form shows (?permissions= previews an unsaved one).
  // The endpoint lists the servers live, so it can be slow: the newest load wins, and until it's in, the last
  // answer stays up, dimmed. fresh: the saved definition changed, so the last answer is no good.
  async function loadTools(fresh) {
    const seq = ++toolsSeq;
    if (fresh) tools = null;
    toolsErr = "";
    if (!mcpOf(saved).length) { tools = null; return; }
    const level = draft ? draft.permissions : saved.permissions;
    let res = null, err = "";
    try {
      res = await api("GET", base + "/tools" + (level !== saved.permissions ? "?permissions=" + enc(level) : ""));
    } catch (x) { err = x.message; }
    if (seq !== toolsSeq) return;
    tools = res;
    toolsErr = err;
    if (draft) drawTools();
  }

  const yamlEd = fileEditor(name, "agent.yaml", reload);
  const personaEd = fileEditor(name, "persona.md", reload);
  const histBox = h("div", {}, h("p", { class: "sub", text: "Loading…" }));
  const adv = h("details", { class: "advanced" }, h("summary", { text: "Files and history" }),
    h("p", { class: "note", text: "The settings above live in these two files. Saving the form rewrites agent.yaml, and "
      + "saving a file here replaces what the form shows: save one before you edit the other." }),
    yamlEd.el, personaEd.el, h("h3", { text: "History" }), histBox);
  adv.addEventListener("toggle", () => { if (adv.open) loadHistory(); });

  // ---- deleting it (DELETE <base>): a confirmation that says what goes; a container agent's volumes if asked ----
  const deleteBox = h("div", { class: "danger-zone" });
  let deleting = false, volumes = false, delBusy = false, delErr = "", gone = false;
  function drawDelete() {
    const a = S.agents.get(name);
    const c = saved && saved.computer;
    const level = c ? c.level : null;     // null: its agent.yaml doesn't validate (no volumes box: it may have none)
    const where = a && a.runner ? ` on ${a.runner}` : "";
    const err = h("p", { class: "err", hidden: !delErr, text: delErr, "data-err": "delete" });
    const box = h("input", { type: "checkbox", checked: volumes, "data-field": "delete-volumes",
      onchange: (e) => { volumes = e.target.checked; drawDelete(); } });
    const btn = h("button", { type: "button", class: "small danger", disabled: delBusy,
      text: delBusy ? "Deleting…" : volumes ? "Delete with its volumes" : "Delete",
      onclick: async () => {
        delBusy = true;
        delErr = "";
        drawDelete();
        try {
          await api("DELETE", base + (volumes ? "?volumes=1" : ""));
          gone = true;
          location.hash = "#/";
        } catch (x) { delErr = x.message; }
        delBusy = false;
        drawDelete();
      } });
    fill(deleteBox, !deleting ? h("button", { type: "button", class: "small danger", text: "Delete agent…",
      onclick: () => { deleting = true; volumes = false; drawDelete(); } })
      : h("div", { class: "confirm" },
        h("div", { class: "q", text: `Delete ${agentTitle(a) || name}? It stops, and Valet forgets it: its settings, `
          + "personality, skills and memory leave the definitions repo (its history keeps them), and its sessions, "
          + "inbox items, shared notes and files, queued messages, triggers and wake-ups go for good." }),
        level === "host" && h("p", { class: "sub", text: `Its working folder${where} stays: `
          + (c.workdir ? `${c.workdir} is yours, and Valet never deletes it.` : "Valet doesn't delete your files.") }),
        // Never placed on a runner, it never ran: no volumes anywhere to offer.
        level && level !== "host" && a && a.runner && h("label", { class: "check" }, box, h("span", { text: `Also delete its volumes${where}: `
          + "its home and workspace, files and logins, for good. Without this they stay, and a new agent with this name "
          + "gets them back." })),
        err,
        h("div", { class: "row" },
          h("button", { type: "button", class: "small", text: "Cancel", disabled: delBusy,
            onclick: () => { deleting = false; delErr = ""; drawDelete(); } }),
          btn)));
  }

  function drawForm() {
    slots.clear();
    if (!draft) {
      const a = S.agents.get(name);
      fill(form, h("div", { class: "note" }, h("strong", { text: "agent.yaml has an error, so the form can't show it." }),
        h("p", { class: "err", text: (a && a.definition_error) || "" }),
        "Fix the file under Advanced, or restore an earlier version from its history."));
      if (!adv.open) { adv.open = true; loadHistory(); }
      return;
    }
    drawIdentity();
    drawPersonality();
    drawKnows();
    drawBrain();
    drawComputer();
    drawPerms();
    drawTools();
    drawSkills();
    drawSecrets();
    for (const x of extras) x.part.draw();
    const sections = [["name", "Name", identity], ["personality", "Personality", personality],
      ["knows", "What it knows", knows], ["brain", "Brain", brainBox], ["computer", "Computer", computer],
      ["perms", "Permissions", perms], ["tools", "Tools", toolsBox], ["skills", "Skills", skillsBox],
      ["secrets", "Secrets", secretsBox]];
    fill(form, sections.flatMap(([key, title, box]) => [h("h2", { text: title }), box,
      ...extras.filter((x) => x.spec.after === key).flatMap((x) => [h("h2", { text: x.spec.title }), x.part.el])]));
  }

  // A fresh AgentDetail from the server (load, save, restore): the new baseline, and the form drawn from it.
  function take(d) {
    onFrame({ type: "agent", agent: d });
    lastMissing = JSON.stringify(d.missing_secrets || []);
    saved = d.definition ? clone(d.definition) : null;
    draft = saved && clone(saved);
    savedPersona = persona = (d.files && d.files["persona.md"]) || "";
    fieldErrors.clear();
    if (saved) loadTools(true);   // what the saved definition gets: the form shows "Loading…" until it's here
    if (saved) loadSkills();  // and its skills: what it gets follows the saved tools.skills
    drawForm();
    if (previewDet.open && draft) loadPreview(); else if (previewShown) previewStale.hidden = false;
    yamlEd.set((d.files && d.files["agent.yaml"]) || "");
    personaEd.set(savedPersona);
    if (adv.open) loadHistory();
    drawDelete();
    drawBar();
  }

  async function reload() {
    try { take(await api("GET", base)); } catch (x) { showBarErr(x.message); }
  }

  async function loadHistory() {
    let list;
    try { list = await api("GET", base + "/history"); } catch (x) {
      fill(histBox, h("p", { class: "err", text: x.message }));
      return;
    }
    fill(histBox, list.length ? list.map((c, i) => histRow(c, i === 0)) : h("p", { class: "sub", text: "No changes yet." }));
  }

  function histRow(c, current) {
    const ask = confirmBox();
    const err = h("p", { class: "err", hidden: true });
    const btn = current ? badge("current") : h("button", { type: "button", class: "small", text: "Restore",
      "data-commit": c.commit, onclick: () => ask.ask(isDirty() ? "Restore this version? Your unsaved changes are discarded."
        : "Restore this version? What you have now stays in the history.", "Restore", async () => {
        btn.disabled = true;
        err.hidden = true;
        try {
          take(await api("POST", base + "/restore", { commit: c.commit }));
          flash(`Restored: ${c.message}.`);
        } catch (x) { err.textContent = x.message; err.hidden = false; btn.disabled = false; }
      }) });
    return h("div", { class: "hist" },
      h("div", { class: "row" }, h("div", { class: "grow" }, h("div", { class: "what", text: c.message }),
        h("div", { class: "sub" }, ago(c.at) + " · ", h("span", { class: "mono", text: String(c.commit).slice(0, 7) }))),
      btn), ask.el, err);
  }

  // ---- the save bar: sticks to the bottom while there's something to save, a restart to do, or news ----
  const barMsg = h("div", { class: "bar-msg" });
  const barErr = h("p", { class: "err", hidden: true, "data-err": "bar" });
  const barAsk = confirmBox();
  const guard = (go) => {  // a restart stops a reply in progress: ask first
    const a = S.agents.get(name);
    if (a && a.status === "working") barAsk.ask(STOPS_REPLY, "Restart anyway", go, "danger"); else go();
  };
  const discardBtn = h("button", { type: "button", text: "Discard", onclick: discard });
  const saveBtn = h("button", { type: "button", class: "primary", text: "Save", onclick: () => save(false) });
  const saveRestartBtn = h("button", { type: "button", text: "Save & restart", onclick: () => guard(() => save(true)) });
  const restartBtn = h("button", { type: "button", class: "primary", text: "Restart", onclick: () => guard(restartNow) });
  // Leaving with unsaved edits (route() asks leave(hash) first): Save then go, Discard & leave, or Stay.
  let leaveTo = null;
  const go = (to) => { leaveTo = null; location.hash = to; };
  const leaveSaveBtn = h("button", { type: "button", class: "primary", text: "Save", onclick: async () => {
    const to = leaveTo;
    if (await save(false)) go(to);
  } });
  const leaveDiscardBtn = h("button", { type: "button", class: "danger", text: "Discard & leave", onclick: () => {
    const to = leaveTo;
    discard();
    go(to);
  } });
  const stayBtn = h("button", { type: "button", text: "Stay", onclick: () => { leaveTo = null; drawBar(); } });
  const actions = h("div", { class: "actions" });
  // A restart refused for secrets that aren't set: their fields, right here, then the restart again.
  const trouble = startTrouble(() => drawBar());
  const bar = h("div", { class: "savebar", hidden: true }, barMsg, barErr, trouble.el, barAsk.el, actions);

  function drawBar() {
    const a = S.agents.get(name);
    const dirty = isDirty();
    if (leaveTo !== null && !dirty) leaveTo = null;
    if (leaveTo !== null) {  // they tried to go elsewhere with edits unsaved
      barMsg.textContent = busy || "You have unsaved changes";
      barMsg.hidden = false;
      fill(actions, leaveSaveBtn, leaveDiscardBtn, stayBtn);
      for (const b of [leaveSaveBtn, leaveDiscardBtn, stayBtn]) b.disabled = !!busy;
      barAsk.hide();
      bar.hidden = false;
      return;
    }
    const restart = !dirty && !!a && !!a.restart_needed;
    barMsg.textContent = busy || (dirty ? "Unsaved changes" : flashMsg || (restart ? "Saved changes apply after a restart." : ""));
    barMsg.hidden = !barMsg.textContent;
    fill(actions, dirty && discardBtn, dirty && saveBtn, dirty && isUp(a) && saveRestartBtn, restart && restartBtn);
    for (const b of [discardBtn, saveBtn, saveRestartBtn, restartBtn]) b.disabled = !!busy;
    if (!dirty && !restart) barAsk.hide();
    bar.hidden = !(dirty || busy || flashMsg || restart || !barErr.hidden || !trouble.el.hidden);
  }

  function flash(text) {
    flashMsg = text;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { flashMsg = ""; drawBar(); }, 3000);
    drawBar();
  }

  function showBarErr(text) { barErr.textContent = text; barErr.hidden = false; drawBar(); }

  function clearErrors() {
    fieldErrors.clear();
    for (const el of slots.values()) el.hidden = true;
    barErr.hidden = true;
    trouble.clear();
  }

  // 422 errors next to their controls; the ones no control shows, at the bar.
  function showErrors(errors) {
    const keys = [...slots].filter(([, el]) => el.isConnected).map(([k]) => k);
    const rest = [];
    for (const e of errors) {
      const k = fieldFor(String(e.field || ""), keys);
      if (!k) { rest.push(e); continue; }
      fieldErrors.set(k, fieldErrors.has(k) ? fieldErrors.get(k) + "\n" + e.msg : e.msg);
    }
    let first = null;
    for (const [k, msg] of fieldErrors) {
      const el = slots.get(k);
      el.textContent = msg;
      el.hidden = false;
      first = first || el;
    }
    if (fieldErrors.has("computer.image") && moreEl) moreEl.open = moreOpen = true;
    showBarErr(rest.length ? rest.map((e) => (e.field ? e.field + ": " : "") + e.msg).join("\n")
      : "Some settings need fixing: see the notes in red.");
    if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  // Resolves true once saved (and restarted, if asked).
  async function save(restart) {
    if (busy || !draft) return false;
    busy = "Saving…";
    barAsk.hide();
    clearErrors();
    drawBar();
    let ok = false;
    try {
      take(await api("PUT", base + "/definition", settingsBody(draft, persona, savedPersona)));
      ok = true;
    } catch (x) {
      busy = "";
      if (x.status === 422 && x.body && Array.isArray(x.body.errors)) showErrors(x.body.errors);
      else showBarErr(x.message);
    }
    if (ok && restart) {
      busy = "Restarting…";
      drawBar();
      try { await restartAgent(name); busy = ""; flash("Saved and restarted."); } catch (x) {
        busy = "";
        if (missingOf(x).length) {
          trouble.show(x, restartNow, { submit: "Set & restart", lead: "Saved. It can't start until "
            + (missingOf(x).length > 1 ? "these secrets are set." : "this secret is set.") });
        } else showBarErr("Saved, but it didn't restart: " + x.message);
      }
    } else if (ok) { busy = ""; flash("Saved."); }
    drawBar();
    return ok;
  }

  async function restartNow() {
    busy = "Restarting…";
    barErr.hidden = true;
    trouble.clear();
    try { await restartAgent(name); busy = ""; flash("Restarted."); } catch (x) {
      busy = "";
      if (missingOf(x).length) trouble.show(x, restartNow, { submit: "Set & restart" }); else showBarErr(x.message);
    }
  }

  function discard() {
    draft = clone(saved);
    persona = savedPersona;
    clearErrors();
    if (tools && tools.permissions !== saved.permissions) loadTools();
    drawForm();
    if (previewShown) previewStale.hidden = false;
    drawBar();
  }

  async function load() {
    loadSecrets();
    loadServers();
    loadDirectives();
    loadGrants();
    try {
      const [d] = await Promise.all([api("GET", base), getCatalog()]);
      take(d);
    } catch (x) {
      if (x.status === 404) el.replaceChildren(h("a", { class: "back", href: "#/", text: "‹ Agents" }),
        h("p", { class: "empty", text: `No agent named ${name}.` }));
      else fill(form, h("p", { class: "err", text: x.message }), h("button", { class: "small", text: "Retry", onclick: load }));
    }
  }

  drawTop();
  const el = h("section", { class: "settings" }, top, form, h("h2", { text: "Advanced" }), adv, deleteBox, bar);
  load();
  return {
    el,
    leave(to) {
      if (gone || !isDirty()) return false;     // deleted: its unsaved edits have nowhere to go
      leaveTo = to;
      drawBar();
      return true;
    },
    frame(f) {
      if (f.type === "agent_deleted" && f.agent === name) gone = true;
      if (f.type === "hello" || (f.type === "agent" && f.agent.name === name)) {
        drawTop();
        drawBar();
        drawDelete();
        // Its missing secrets changed (one was set or deleted elsewhere): there's no secret frame, so refetch.
        const missing = JSON.stringify((S.agents.get(name) || {}).missing_secrets || []);
        if (f.type === "hello" || (lastMissing !== null && missing !== lastMissing)) loadSecrets();
        lastMissing = missing;
      }
      if (f.type === "hello") { loadServers(); loadDirectives(); loadGrants(); if (saved) { loadSkills(); syncRules(); } }
      // An answer that leaves a grant or a rule: the gateway records it as the call goes on, then says so with an
      // agent frame. The answer's own agent frame can come before an "Always allow" rule is written, so for that one
      // each agent frame looks again, until the rules move (or SCOPE_WAIT runs out).
      if (f.type === "inbox" && f.item.agent === name && f.item.answer && SCOPED.has(f.item.answer.optionId)) {
        scoped = true;
        if (f.item.kind === "tool" && f.item.answer.optionId === "allow_always") always = Date.now() + SCOPE_WAIT;
      }
      if (f.type === "agent" && f.agent.name === name && (scoped || always > Date.now())) {
        scoped = false;
        loadGrants();
        syncRules().then((moved) => { if (moved) always = 0; });
      }
      if (f.type === "hello" || f.type === "runner") {
        getCatalog().then(() => { if (brain && draft) brain.redrawCards(); }, () => { /* the next one */ });
      }
      for (const x of extras) if (x.part.frame) x.part.frame(f);
    },
  };
}

// ---------- secrets & tools (#/secrets-tools) ----------

const CALLS_PAGE = 20;
const KIND = { builtin: "Built into Valet", http: "Remote (HTTP)", stdio: "A command on the control plane's machine" };
// A tool call's outcome -> [badge class, words].
const OUTCOME = { ok: ["completed", "done"], error: ["failed", "error"], denied: ["rejected", "blocked"],
  rejected: ["rejected", "rejected"], expired: ["expired", "no answer"], failed: ["failed", "failed"],
  unknown: ["expired", "unknown tool"], cancelled: ["expired", "cancelled"], interrupted: ["expired", "interrupted"] };

// What a call to a tool does at the agent's level (GET /api/agents/{name}/tools `decision`) -> [badge class, words].
const DECISION = { allow: ["runs", "runs"], ask: ["asks", "asks you"], deny: ["blocked", "blocked"] };

// tools.rules: server -> tool name, or "*" for all its tools -> allow, ask or deny, before the level.
const ALL_TOOLS = "*";
const RULE_CHOICES = [["allow", "Run"], ["ask", "Ask you"], ["deny", "Block"]];
const SCOPED = new Set(["allow_hour", "allow_always"]);    // the inbox answers that leave a grant or a rule
const SCOPE_WAIT = 30000;   // ms an open settings page keeps looking for the rule an "Always allow" answer writes
const rulesOf = (d) => (d && d.tools && d.tools.rules) || {};
const ownRule = (rules, tool) => (Object.hasOwn(rules, tool) && RULE_CHOICES.some(([v]) => v === rules[tool])
  ? rules[tool] : null);

// What a call to tool x (an entry of GET /api/agents/{name}/tools) does under a server's rules: its own rule, else
// the server's "*", else its level (level_decision); a live hour grant turns an ask into a run, never a block.
function toolFate(x, rules, grant) {
  const rule = ownRule(rules, x.tool) || ownRule(rules, ALL_TOOLS);
  const level = x.level_decision || x.decision;
  if (rule === "ask" || (!rule && level === "ask")) {
    if (grant) return { decision: "allow", source: "grant", grant };
  }
  return rule ? { decision: rule, source: "rule", grant } : { decision: level, source: "level", grant };
}

// rules with each server, and each tool in it, where it is in `like` (new ones after); no empty servers.
function orderLike(rules, like) {
  const out = {};
  const keys = (a, b) => [...Object.keys(b).filter((k) => Object.hasOwn(a, k)), ...Object.keys(a).filter((k) => !Object.hasOwn(b, k))];
  for (const server of keys(rules, like)) {
    const list = {};
    for (const tool of keys(rules[server], like[server] || {})) list[tool] = rules[server][tool];
    if (Object.keys(list).length) out[server] = list;
  }
  return out;
}

// A time today as the clock shows it ("14:05"); another day's with its date.
function clock(ts) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? t : d.toLocaleDateString() + " " + t;
}

function seconds(s) {
  if (s == null) return "";
  return s < 1 ? Math.round(s * 1000) + " ms" : s < 60 ? s.toFixed(1) + " s" : Math.round(s / 60) + " min";
}

const REGISTRY_EXAMPLE = `servers:
  github:
    description: GitHub issues and pull requests
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: Bearer \${secret:GITHUB_TOKEN}`;

// Who uses a secret (GET /api/secrets used_by and servers), for the owner: "Scout and the github server".
function secretUsers(s) {
  return [...(s.used_by || []).map(agentName), ...(s.servers || []).map((n) => `the ${n} server`)];
}

// Everything agents share: the secrets, the MCP servers (and mcp.yaml, the file that lists them), and the
// gateway's log of tool calls. There are no frames for secrets or calls: it refetches on hello and on demand.
function SecretsToolsView() {
  let secrets = null, secretsErr = "";
  let gw = null, gwErr = "";
  let calls = [], more = false, callsErr = "", callsLoaded = false;
  const valueEds = new Map();     // secret name -> its open "Set value" editor
  const rowUi = new Map();        // secret name -> {ask, err}: kept across redraws
  const uiOf = (n) => rowUi.get(n) || rowUi.set(n, { ask: confirmBox(), err: h("p", { class: "err", hidden: true }) }).get(n);

  const secretsBox = h("div"), serversBox = h("div"), callsBox = h("div");

  // ---- secrets ----
  const newName = h("input", { class: "mono", placeholder: "GITHUB_TOKEN", maxLength: 64, autocomplete: "off",
    autocapitalize: "characters", "aria-label": "Name", "data-field": "secret-new" });
  newName.setAttribute("spellcheck", "false");
  const newValue = secretInput("new", () => addNew());
  const newErr = h("p", { class: "err", hidden: true, "data-err": "secret-new" });
  const newSave = h("button", { type: "button", class: "small primary", text: "Save", onclick: () => addNew() });
  const newForm = h("div", { class: "card newsecret", hidden: true },
    h("div", { class: "field" }, h("label", { text: "Name" }), newName),
    h("div", { class: "field" }, h("label", { text: "Value" }), newValue),
    newErr,
    h("div", { class: "row end actions" }, h("button", { type: "button", class: "small", text: "Cancel",
      onclick: () => closeNew() }), newSave));
  newName.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); newValue.focus(); } });

  function closeNew() { newName.value = newValue.value = ""; newErr.hidden = true; newForm.hidden = true; }

  async function addNew() {
    const n = secretName(newName.value), value = secretValue(newValue.value);
    const show = (msg) => { newErr.textContent = msg; newErr.hidden = false; };
    newErr.hidden = true;
    if (!n) return show("Give it a name, like GITHUB_TOKEN.");
    if (!SECRET_RE.test(n)) return show(SECRET_RULE);
    const old = secrets && secrets.find((s) => s.name === n);
    if (old && old.set) return show(`${n} is already set: use Change value on it below.`);
    if (!value) return show("Paste its value.");
    newSave.disabled = true;
    try {
      await setSecret(n, value);
      closeNew();
      await loadSecrets();
    } catch (x) { show(x.message); }
    newSave.disabled = false;
  }

  function drawSecrets() {
    const list = secrets || [];
    for (const n of [...valueEds.keys()]) if (!list.some((s) => s.name === n)) valueEds.delete(n);
    fill(secretsBox, newForm,
      secretsErr && h("p", { class: "err", text: secretsErr }),
      !secrets && !secretsErr && h("p", { class: "sub", text: "Loading…" }),
      secrets && !list.length && h("p", { class: "empty", text: "No secrets yet." }),
      list.length > 0 && h("div", { class: "card" }, h("div", { class: "rows" }, list.map(secretRow))));
  }

  function secretRow(s) {
    const users = secretUsers(s);
    const agents = (s.used_by || []).length, servers = (s.servers || []).length;
    const ui = uiOf(s.name);
    const ed = valueEds.get(s.name);
    const blocked = agents > 1 ? "they can't start until it's set" : agents ? "it can't start until it's set"
      : servers > 1 ? "they can't connect until it's set" : servers ? "it can't connect until it's set" : "";
    const sub = [users.length ? "Used by " + andList(users) : "Nothing uses it",
      s.set ? s.updated_at && "set " + ago(s.updated_at) : blocked].filter(Boolean).join(" · ");
    return h("div", { class: "secret", "data-row": s.name },
      h("div", { class: "row" }, h("span", { class: "mono grow name", text: s.name }),
        s.set ? badge("idle", "set") : badge("waiting", "not set")),
      h("div", { class: "sub", text: sub }),
      ed ? ed.el : h("div", { class: "row acts" },
        h("button", { type: "button", class: "small" + (s.set ? "" : " primary"), text: s.set ? "Change value" : "Set value",
          onclick: () => openValue(s.name) }),
        s.set && h("button", { type: "button", class: "small danger", text: "Delete", onclick: () => askDelete(s) })),
      ui.ask.el, ui.err);
  }

  function openValue(n) {
    const ed = valueEditor(n, async () => { valueEds.delete(n); await loadSecrets(); },
      () => { valueEds.delete(n); drawSecrets(); });
    valueEds.set(n, ed);
    drawSecrets();
    ed.focus();
  }

  function askDelete(s) {
    const ui = uiOf(s.name);
    const used = (s.used_by || []).map(agentName);
    const servers = (s.servers || []).map((n) => `the ${n} server`);
    const many = used.length > 1;
    const agentsSay = used.length ? `${andList(used)} ${many ? "use" : "uses"} ${s.name}. ${many ? "They keep" : "It keeps"} `
      + `it until ${many ? "they stop" : "it stops"}, then can't start again until it's set. ` : "";
    const list = andList(servers);
    const serversSay = servers.length ? `${list.charAt(0).toUpperCase() + list.slice(1)} `
      + `${servers.length > 1 ? "stop" : "stops"} working until it's set again. ` : "";
    const q = agentsSay || serversSay ? agentsSay + serversSay + "Delete it?"
      : `Delete ${s.name}? Nobody can read it back, so you'd need the value again to set it.`;
    ui.err.hidden = true;
    ui.ask.ask(q, "Delete", async () => {
      try { await api("DELETE", `/api/secrets/${enc(s.name)}`); } catch (x) {
        if (x.status !== 404) { ui.err.textContent = x.message; ui.err.hidden = false; return; }
      }
      await loadSecrets();
    }, "danger");
  }

  async function loadSecrets() {
    try { secrets = await api("GET", "/api/secrets"); secretsErr = ""; } catch (x) { secretsErr = x.message; }
    drawSecrets();
    if (gw) drawServers();   // a server's secrets say which aren't set
  }

  // ---- MCP servers, and mcp.yaml ----
  function drawServers() {
    const list = gw ? gw.servers : [];
    fill(serversBox,
      gwErr && h("p", { class: "err", text: gwErr }),
      !gw && !gwErr && h("p", { class: "sub", text: "Loading…" }),
      gw && gw.error && h("div", { class: "note warn" },
        h("strong", { text: "mcp.yaml has an error, so its servers can't be used. " }), h("span", { text: gw.error })),
      list.length > 0 && h("div", { class: "card" }, h("div", { class: "rows" }, list.map(serverRow))));
  }

  function serverRow(s) {
    const used = (s.agents || []).map(agentName);
    const where = s.kind === "http" ? s.url : s.kind === "stdio" ? [s.command, ...(s.args || [])].join(" ") : "";
    const creds = (s.kind === "http" ? s.headers : s.env) || [];
    return h("div", { class: "server", "data-row": s.name },
      h("div", { class: "row" }, h("strong", { class: "grow name", text: s.name }),
        s.builtin ? badge("", "built in") : s.connected ? badge("idle", "connected") : badge("", "not connected")),
      s.description && h("div", { class: "sub", text: brief(s.description, 200) }),
      where && h("div", { class: "mono where", text: where }),
      creds.length > 0 && h("div", { class: "sub", text: (s.kind === "http" ? "Headers: " : "Environment: ") + creds.join(", ") }),
      (s.secrets || []).length > 0 && h("div", { class: "sub", text: "Secrets: " + s.secrets.map((n) => {
        const it = secrets && secrets.find((x) => x.name === n);
        return n + (it && !it.set ? " (not set)" : "");
      }).join(", ") }),
      h("div", { class: "sub", text: [KIND[s.kind] || s.kind,
        used.length ? "used by " + andList(used) : "no agent uses it yet"].join(" · ") }));
  }

  async function loadServers() {
    try { gw = await api("GET", "/api/gateway/servers"); gwErr = ""; } catch (x) { gwErr = x.message; }
    drawServers();
  }

  let regLoaded = null;           // the file's text as last loaded or saved; null until the editor first opens
  const regTa = h("textarea", { class: "code", autocapitalize: "none", "aria-label": "mcp.yaml",
    "data-field": "file:mcp.yaml" });
  regTa.setAttribute("spellcheck", "false");
  const regMsg = h("p", { class: "sub", hidden: true });
  const regErr = h("p", { class: "err", hidden: true, "data-err": "registry" });
  const regSave = h("button", { type: "button", class: "small primary", text: "Save file", onclick: async () => {
    regSave.disabled = true;
    regMsg.hidden = regErr.hidden = true;
    try {
      const r = await api("PUT", "/api/gateway/registry", { text: regTa.value });
      regLoaded = regTa.value;
      gw = { servers: r.servers || [], error: r.error || null };
      gwErr = "";
      drawServers();
      regMsg.textContent = `Saved (${String(r.commit).slice(0, 7)}). It applies to the next tool call.`;
      regMsg.hidden = false;
      loadSecrets();      // the secrets the file names, set or not
    } catch (x) { regErr.textContent = x.message; regErr.hidden = false; }
    regSave.disabled = false;
  } });
  const regBox = h("details", { class: "advanced" }, h("summary", { text: "Edit servers file" }),
    h("p", { class: "sub", text: "mcp.yaml lists the servers agents can be given. Put credentials in as "
      + "${secret:NAME}, one of the secrets above, or ${env:NAME}, a variable of the control plane's environment. "
      + "They stay in Valet: the file, the agents and their computers never see them. For example:" }),
    h("pre", { class: "example", text: REGISTRY_EXAMPLE }),
    h("div", { class: "card file" },
      h("div", { class: "row between", style: "margin-bottom: 8px" }, h("strong", { class: "mono", text: "mcp.yaml" }), regSave),
      regTa, regErr, regMsg));
  regBox.addEventListener("toggle", () => { if (regBox.open && regLoaded === null) loadRegistry(); });

  async function loadRegistry() {
    try {
      const r = await api("GET", "/api/gateway/registry");
      if (regTa.value === (regLoaded || "")) regTa.value = r.text;   // never over the owner's edits
      regLoaded = r.text;
    } catch (x) { regErr.textContent = x.message; regErr.hidden = false; }
  }

  // ---- the gateway's log ----
  function drawCalls() {
    fill(callsBox,
      callsErr && h("p", { class: "err", text: callsErr }),
      !callsLoaded && !callsErr && h("p", { class: "sub", text: "Loading…" }),
      callsLoaded && !calls.length && h("p", { class: "empty", text: "No tool calls yet." }),
      calls.length > 0 && h("div", { class: "card" }, h("div", { class: "rows" }, calls.map(callRow))),
      more && h("button", { type: "button", class: "small", text: "More", onclick: (e) => {
        e.target.disabled = true;
        loadCalls(calls[calls.length - 1].id);
      } }));
  }

  function callRow(c) {
    const [cls, text] = c.outcome === "pending" ? (c.decision === "ask" ? ["waiting", "waiting on you"] : ["working", "running"])
      : OUTCOME[c.outcome] || ["", String(c.outcome || "")];
    return h("div", { class: "call", "data-row": String(c.id) },
      h("div", { class: "row" }, h("span", { class: "grow what" }, h("span", { text: agentName(c.agent) + " · " }),
        h("span", { class: "mono", text: c.server ? c.server + " › " + c.tool : c.tool })), badge(cls, text)),
      h("div", { class: "sub", text: [ago(c.started_at), c.decision === "ask" && "asked you", seconds(c.duration)]
        .filter(Boolean).join(" · ") }),
      c.error && h("p", { class: "err", text: clip(String(c.error), 500) }),
      !isEmpty(c.arguments) && h("details", { class: "args" }, h("summary", { text: "Arguments" }),
        h("pre", { text: clip(json(c.arguments), 2000) })));
  }

  // before: the oldest id shown, to page back; none: the newest page again.
  async function loadCalls(before) {
    try {
      const r = await api("GET", `/api/gateway/calls?limit=${CALLS_PAGE}` + (before ? `&before=${before}` : ""));
      calls = before ? calls.concat(r.calls) : r.calls;
      more = !!r.more;
      callsErr = "";
    } catch (x) { callsErr = x.message; }
    callsLoaded = true;
    drawCalls();
  }

  drawSecrets();
  drawServers();
  drawCalls();
  loadSecrets();
  loadServers();
  loadCalls();
  const el = h("section", { class: "secrets-tools" },
    h("a", { class: "back", href: "#/", text: "‹ Agents" }), h("h1", { text: "Secrets & tools" }),
    h("div", { class: "row between section-head" }, h("h2", { text: "Secrets" }),
      h("button", { type: "button", class: "small", text: "New secret", onclick: () => {
        newForm.hidden = false;
        newName.focus();
      } })),
    h("p", { class: "sub intro", text: "Keys and tokens your agents get as environment variables (an agent lists the "
      + "ones it needs in its settings), and that tool servers use to sign in. Values are stored encrypted and "
      + "never shown again." }),
    secretsBox,
    h("h2", { text: "Tool servers" }),
    h("p", { class: "sub intro", text: "MCP servers your agents can use through Valet's gateway. Turn one on for an "
      + "agent in its settings." }),
    serversBox, regBox,
    h("div", { class: "row between section-head" }, h("h2", { text: "Recent tool calls" }),
      h("button", { type: "button", class: "small", text: "Refresh", onclick: () => loadCalls() })),
    callsBox);
  return {
    el,
    frame(f) {
      if (f.type === "hello") { loadSecrets(); loadServers(); loadCalls(); }
      if (f.type === "agent" && secrets) drawSecrets();   // display names in "used by"
    },
  };
}

// ---------- directives: what agents are told (the settings page's "What it knows", and #/directives) ----------
// An agent's `directives` lists the layers it's told, in order: owner (about you), authority/<preset> (standing
// authority), persona.md, environment and memory (both written by Valet at each start).

const DIRECTIVE_ORDER = ["owner", "authority/", "persona.md", "environment", "memory"];
const OWNER_PLACEHOLDER = "# About the owner\n\n- Where you live, your time zone, the languages you use\n"
  + "- What you do, and what you're working on\n- How you like to work, and how you like answers\n"
  + "- Your machines and accounts";
const LAYER_NAMES = { owner: "About you", "persona.md": "Personality", environment: "Environment", memory: "Memory" };
const LAYER_STATES = { empty: "empty", missing: "missing", off: "off" };

const isPreset = (e) => typeof e === "string" && e.startsWith("authority/");
function presetOf(list) { const e = (list || []).find(isPreset); return e ? e.slice("authority/".length) : null; }
function presetTitle(name) { const s = String(name).replace(/-/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); }
// A preset's title: what its "# Standing authority: …" heading calls it, else its name.
function presetLabel(p) {
  const m = /^#\s*Standing authority:\s*(.+)$/im.exec((p && p.text) || "");
  return m ? presetTitle(m[1].trim()) : presetTitle(p.name);
}
// Presets from the most careful to the most trusting, then the owner's own, by name.
const PRESET_ORDER = ["strict", "standard", "full"];
function presetSort(a, b) {
  const r = (n) => { const i = PRESET_ORDER.indexOf(n); return i < 0 ? PRESET_ORDER.length : i; };
  return r(a) - r(b) || (a < b ? -1 : a > b ? 1 : 0);
}
function layerName(e) { return isPreset(e) ? "Authority: " + presetTitle(e.slice(10)) : LAYER_NAMES[e] || e; }

function directiveRank(e) {
  const i = DIRECTIVE_ORDER.indexOf(isPreset(e) ? "authority/" : e);
  return i < 0 ? DIRECTIVE_ORDER.length : i;
}

// `list` with `entry` on (put in its place in DESIGN.md's order, the others staying where they are) or off.
function withDirective(list, entry, on) {
  const rest = (list || []).filter((x) => x !== entry);
  if (!on) return rest;
  const at = rest.findIndex((x) => directiveRank(x) > directiveRank(entry));
  return at < 0 ? [...rest, entry] : [...rest.slice(0, at), entry, ...rest.slice(at)];
}

// `list` with its standing authority set to the preset `name` (null: none), where the old one was.
function withPreset(list, name) {
  const cur = (list || []).find(isPreset);
  if (cur && name) return list.map((x) => (x === cur ? "authority/" + name : x));
  const rest = (list || []).filter((x) => !isPreset(x));
  return name ? withDirective(rest, "authority/" + name, true) : rest;
}

// A layer's gist: its first line that isn't a heading, on one line.
function gist(text, n = 140) {
  const line = String(text || "").split("\n").map((l) => l.trim()).find((l) => l && !l.startsWith("#")) || "";
  return brief(line.replace(/^[-*]\s+/, "").replace(/:$/, "…"), n);
}

// Who a saved layer reaches: the running ones need a restart, the others read it when they next start.
function appliesNote(used) {
  if (!used.length) return "No agent includes it yet.";
  const up = used.filter((n) => isUp(S.agents.get(n)));
  if (up.length) return `${andList(up.map(agentName))} ${up.length > 1 ? "need" : "needs"} a restart to use it.`;
  const many = used.length > 1;
  return `${andList(used.map(agentName))} ${many ? "read" : "reads"} it at ${many ? "their" : "its"} next start.`;
}

// A text with its own Save, for #/directives. set(text) shows the server's text unless the owner has edited it.
function layerEditor(key, label, placeholder, onSave) {
  let loaded = null;
  const ta = h("textarea", { class: "persona", placeholder: placeholder || "", "aria-label": label, "data-field": key,
    oninput: () => { grow(ta); msg.hidden = true; } });
  const msg = h("p", { class: "sub", hidden: true });
  const err = h("p", { class: "err", hidden: true, "data-err": key });
  const save = h("button", { type: "button", class: "small primary", text: "Save", onclick: async () => {
    save.disabled = true;
    msg.hidden = err.hidden = true;
    try {
      const text = ta.value;
      msg.textContent = await onSave(text);
      loaded = text;
      msg.hidden = false;
    } catch (x) { err.textContent = x.message; err.hidden = false; }
    save.disabled = false;
  } });
  return {
    ta, msg, err, save,
    set(text) {
      if (loaded === null || ta.value === loaded) ta.value = text;
      loaded = text;
      setTimeout(() => grow(ta), 0);
    },
  };
}

// #/directives: the owner profile and the authority presets, shared by every agent that includes them.
function DirectivesView() {
  let owner = null, ownerErr = "", presets = null, presetsErr = "";
  const ownerEd = layerEditor("owner", "About you", OWNER_PLACEHOLDER, async (text) => {
    owner = await api("PUT", "/api/owner", { text });
    drawOwner();
    return "Saved. " + appliesNote(owner.used_by || []);
  });
  const ownerBox = h("div", { class: "card" });
  const presetsBox = h("div");
  const eds = new Map();          // preset -> {ed, ask, delErr}: kept across redraws (the owner may be typing)

  function drawOwner() {
    const used = owner ? (owner.used_by || []).map(agentName) : [];
    fill(ownerBox,
      ownerErr && h("p", { class: "err", text: ownerErr }),
      !owner && !ownerErr && h("p", { class: "sub", text: "Loading…" }),
      ownerEd.ta, ownerEd.err, ownerEd.msg,
      h("div", { class: "row between acts" },
        h("span", { class: "sub grow", text: owner ? (used.length ? "Included by " + andList(used) : "No agent includes it yet")
          : "" }), ownerEd.save));
  }

  function editorOf(name) {
    if (eds.has(name)) return eds.get(name);
    const ed = layerEditor("preset:" + name, presetTitle(name), "", async (text) => {
      const p = await api("PUT", `/api/authority/${enc(name)}`, { text });
      await loadPresets();
      return "Saved. " + appliesNote(p.used_by || []);
    });
    const x = { ed, ask: confirmBox(), delErr: h("p", { class: "err", hidden: true }) };
    eds.set(name, x);
    return x;
  }

  function presetCard(p) {
    const x = editorOf(p.name);
    const used = (p.used_by || []).map(agentName);
    x.ed.set(p.text);
    const del = !used.length && h("button", { type: "button", class: "small danger", text: "Delete", onclick: () => {
      x.delErr.hidden = true;
      x.ask.ask(`Delete ${presetLabel(p)}? It stays in the definitions' history.`, "Delete", async () => {
        try { await api("DELETE", `/api/authority/${enc(p.name)}`); } catch (e) {
          if (e.status !== 404) { x.delErr.textContent = e.message; x.delErr.hidden = false; return; }
        }
        eds.delete(p.name);
        await loadPresets();
      }, "danger");
    } });
    return h("div", { class: "card preset", "data-row": "preset:" + p.name },
      h("div", { class: "row between" }, h("strong", { class: "grow", text: presetLabel(p) }),
        h("span", { class: "mono sub", text: "authority/" + p.name })),
      h("div", { class: "sub", text: used.length ? "Used by " + andList(used) : "No agent uses it" }),
      x.ed.ta, x.ed.err, x.ed.msg, x.delErr,
      h("div", { class: "row end acts" }, del, x.ed.save), x.ask.el);
  }

  const newName = h("input", { placeholder: "e.g. GitHub only", maxLength: 31, autocomplete: "off",
    "aria-label": "New preset's name", "data-field": "preset-new",
    onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); addPreset(); } } });
  const newErr = h("p", { class: "err", hidden: true, "data-err": "preset-new" });
  const newForm = h("div", { class: "card newpreset", hidden: true },
    h("div", { class: "field" }, h("label", { text: "Name" }), newName), newErr,
    h("div", { class: "row end acts" },
      h("button", { type: "button", class: "small", text: "Cancel", onclick: () => { newForm.hidden = true; newName.value = ""; } }),
      h("button", { type: "button", class: "small primary", text: "Create", onclick: () => addPreset() })));

  async function addPreset() {
    const name = slugify(newName.value);
    const show = (m) => { newErr.textContent = m; newErr.hidden = false; };
    newErr.hidden = true;
    if (!name) return show("Give it a name.");
    if (!NAME_RE.test(name)) return show("Use letters, digits and dashes.");
    if (presets && presets.some((p) => p.name === name)) return show(`There's already a preset called ${name}.`);
    try {
      await api("PUT", `/api/authority/${enc(name)}`, { text: `# Standing authority: ${presetTitle(name)}\n\n` });
    } catch (x) { return show(x.message); }
    newName.value = "";
    newForm.hidden = true;
    await loadPresets();
  }

  function drawPresets() {
    fill(presetsBox, newForm,
      presetsErr && h("p", { class: "err", text: presetsErr }),
      !presets && !presetsErr && h("p", { class: "sub", text: "Loading…" }),
      presets && !presets.length && h("p", { class: "empty", text: "No presets." }),
      (presets || []).slice().sort((a, b) => presetSort(a.name, b.name)).map(presetCard));
  }

  async function loadOwner() {
    try { owner = await api("GET", "/api/owner"); ownerErr = ""; ownerEd.set(owner.text); } catch (x) { ownerErr = x.message; }
    drawOwner();
  }

  async function loadPresets() {
    try { presets = await api("GET", "/api/authority"); presetsErr = ""; } catch (x) { presetsErr = x.message; }
    drawPresets();
  }

  drawOwner();
  drawPresets();
  loadOwner();
  loadPresets();
  const el = h("section", { class: "directives" },
    h("a", { class: "back", href: "#/", text: "‹ Agents" }), h("h1", { text: "About you & authority" }),
    h("h2", { text: "About you" }),
    h("p", { class: "sub intro", text: "Who you are and how you work. Agents that include it read it at the start of "
      + "every session, so they don't need to ask. Turn it on in an agent's settings, under What it knows." }),
    ownerBox,
    h("div", { class: "row between section-head" }, h("h2", { text: "Standing authority" }),
      h("button", { type: "button", class: "small", text: "New preset", onclick: () => {
        newForm.hidden = false;
        newName.focus();
      } })),
    h("p", { class: "sub intro", text: "What an agent may do for you without asking first. Each agent uses one preset "
      + "(or none), picked in its settings. Valet still enforces its permission level: this tells it what you want, "
      + "so it acts instead of hesitating." }),
    presetsBox);
  return {
    el,
    frame(f) {
      if (f.type === "hello") { loadOwner(); loadPresets(); }
      if (f.type === "agent") { drawOwner(); if (presets) drawPresets(); }   // names in "used by"
    },
  };
}

// ---------- skills (#/skills, #/skills/<name>, #/a/<agent>/skills/<name>) ----------
// A skill is a SKILL.md (its name, when to use it, then the instructions) and maybe other text files it points to.
// Shared ones are the library an agent picks from in its settings; an agent's own are only for it.

const SKILL_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const SKILL_RULE = "Use lowercase letters and digits, with single dashes between words, like code-review.";
const SKILL_FILE = "SKILL.md";
const PATH_RE = /^[A-Za-z0-9_][A-Za-z0-9._-]*(?:\/[A-Za-z0-9_][A-Za-z0-9._-]*)*$/;

// What the owner typed -> a skill name: trimmed, lowercase, spaces to dashes (the rule is checked after).
function skillName(text) { return String(text || "").trim().toLowerCase().replace(/[\s_]+/g, "-"); }

function skillApi(name, agent) {
  return agent ? `/api/agents/${enc(agent)}/skills/${enc(name)}` : `/api/skills/${enc(name)}`;
}

function skillHash(name, agent) { return agent ? `#/a/${enc(agent)}/skills/${enc(name)}` : `#/skills/${enc(name)}`; }

function lineCount(text) {
  const n = text ? text.replace(/\n$/, "").split("\n").length : 0;
  return n === 1 ? "1 line" : n + " lines";
}

// A new skill: its name, when to use it, and what to do. POSTs to `path`; onDone(skill) once it's made.
function newSkillForm(path, cls, onDone, onCancel) {
  const nameIn = h("input", { class: "mono", placeholder: "code-review", maxLength: 64, autocomplete: "off",
    autocapitalize: "none", "aria-label": "Name", "data-field": "skill-new-name",
    onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); descIn.focus(); } } });
  nameIn.setAttribute("spellcheck", "false");
  const descIn = h("textarea", { class: "skill-desc", rows: 2, "aria-label": "When to use it",
    placeholder: "Use when reviewing a pull request.", "data-field": "skill-new-description" });
  const bodyIn = h("textarea", { class: "persona", rows: 8, "aria-label": "Instructions",
    placeholder: "1. Read the diff.\n2. …", "data-field": "skill-new-instructions" });
  const err = h("p", { class: "err", hidden: true, "data-err": "skill-new" });
  const create = h("button", { type: "button", class: "small primary", text: "Create", onclick: submit });
  async function submit() {
    const name = skillName(nameIn.value), description = descIn.value.trim();
    const show = (msg) => { err.textContent = msg; err.hidden = false; };
    err.hidden = true;
    if (!name) return show("Give it a name, like code-review.");
    if (name.length > 64 || !SKILL_RE.test(name)) return show(SKILL_RULE);
    if (!description) return show("Say when to use it: that's what the agent goes by.");
    create.disabled = true;
    try {
      const s = await api("POST", path, { name, description, instructions: bodyIn.value });
      create.disabled = false;
      return onDone(s);
    } catch (x) { show(x.message); }
    create.disabled = false;
  }
  const el = h("div", { class: cls },
    h("div", { class: "field" }, h("label", { text: "Name" }), nameIn),
    h("div", { class: "field" }, h("label", { text: "When to use it" }), descIn,
      h("p", { class: "sub hint-line", text: "The agent sees only this until it uses the skill." })),
    h("div", { class: "field" }, h("label", { text: "Instructions" }), bodyIn),
    err,
    h("div", { class: "row end actions" }, h("button", { type: "button", class: "small", text: "Cancel", onclick: onCancel }),
      create));
  return { el, focus() { nameIn.focus(); } };
}

// #/skills: the shared library.
function SkillsView() {
  let list = null, err = "";
  const listBox = h("div"), formBox = h("div");
  let form = null;
  const toggle = () => {
    if (form) { form = null; formBox.replaceChildren(); return; }
    form = newSkillForm("/api/skills", "card newskill", async () => { toggle(); await load(); }, toggle);
    formBox.replaceChildren(form.el);
    form.focus();
  };

  function row(s) {
    const used = (s.used_by || []).map(agentName);
    const more = s.files.filter((p) => p !== SKILL_FILE).length;
    const sub = [used.length ? "Used by " + andList(used) : "No agent uses it",
      more > 0 && (more === 1 ? "1 more file" : more + " more files")].filter(Boolean).join(" · ");
    return h("a", { class: "skill row", href: skillHash(s.name), "data-row": s.name },
      h("span", { class: "grow" }, h("span", { class: "name mono", text: s.name }),
        s.description && h("span", { class: "desc", text: brief(s.description, 160) }),
        s.error && h("span", { class: "warn-text desc", text: "It has a problem, so no agent gets it: " + s.error }),
        h("span", { class: "sub desc", text: sub })),
      h("span", { class: "chev", text: "›" }));
  }

  function draw() {
    fill(listBox, err && h("p", { class: "err", text: err }),
      !list && !err && h("p", { class: "sub", text: "Loading…" }),
      list && !list.length && h("p", { class: "empty", text: "No skills yet." }),
      list && list.length > 0 && h("div", { class: "card" }, h("div", { class: "rows" }, list.map(row))));
  }

  async function load() {
    try { list = await api("GET", "/api/skills"); err = ""; } catch (x) { err = x.message; }
    draw();
  }

  draw();
  load();
  const el = h("section", { class: "skills" },
    h("a", { class: "back", href: "#/", text: "‹ Agents" }),
    h("div", { class: "row between", style: "margin-bottom: 12px" }, h("h1", { text: "Skills" }),
      h("button", { type: "button", class: "small", text: "New skill", onclick: toggle })),
    h("p", { class: "sub intro", text: "Instructions your agents load when a task calls for them. Give one to an agent "
      + "in its settings, under Skills; changes reach an agent when it restarts." }),
    formBox, listBox);
  return {
    el,
    frame(f) {
      if (f.type === "hello") load();
      else if (f.type === "agent" && list) draw();   // display names in "used by"
    },
  };
}

// #/skills/<name> (shared) or #/a/<agent>/skills/<name> (an agent's own): when to use it, the instructions and its
// other files, saved together. A SKILL.md that can't be read as name/description/instructions is shown as a file.
function SkillView(name, agent) {
  const path = skillApi(name, agent);
  const back = agent ? `#/a/${enc(agent)}/settings` : "#/skills";
  let detail = null, saved = null, draft = null, raw = false, gone = false;
  let busy = "", flashMsg = "", flashTimer = 0, leaveTo = null, newPathErr = "";
  const openFiles = new Set();     // paths whose text is shown for editing

  const top = h("div"), problem = h("div"), form = h("div", {}, h("p", { class: "sub", text: "Loading…" }));
  const filesBox = h("div", { class: "card" }), dangerBox = h("div");
  const fileInputs = new Map();    // path -> its textarea, while open
  const note = agent ? restartNote(agent) : null;
  const ask = confirmBox();
  const deleteErr = h("p", { class: "err", hidden: true });

  const isDirty = () => !!draft && !gone && JSON.stringify(draft) !== JSON.stringify(saved);

  function drawTop() {
    const used = detail ? (detail.used_by || []).map(agentName) : [];
    const sub = agent ? "Only for " + agentName(agent)
      : "Shared skill · " + (used.length ? "used by " + andList(used) : "no agent uses it");
    fill(top, h("a", { class: "back", href: back, text: agent ? "‹ Settings" : "‹ Skills" }),
      h("h1", { class: "mono skill-title", text: name }), h("p", { class: "sub", text: sub }));
  }

  function drawProblem() {
    fill(problem, detail && detail.error && h("div", { class: "note warn" },
      h("strong", { text: agent ? "It has a problem, so the agent doesn't get it: " : "It has a problem, so no agent gets it: " }),
      h("span", { text: detail.error })));
  }

  function drawForm() {
    if (raw) {
      fill(form, h("p", { class: "note", text: "Its SKILL.md doesn't start with a name and description, so it's shown "
        + "as a file below: fix it there." }));
      return;
    }
    const desc = h("textarea", { class: "skill-desc", rows: 2, value: draft.description, "aria-label": "When to use it",
      "data-field": "description", oninput: () => { draft.description = desc.value; grow(desc); changed(); } });
    const body = h("textarea", { class: "persona", rows: 10, value: draft.instructions, "aria-label": "Instructions",
      "data-field": "instructions", oninput: () => { draft.instructions = body.value; grow(body); changed(); } });
    const extra = Object.keys((detail && detail.extra) || {});
    fill(form, h("div", { class: "card" },
      h("div", { class: "field" }, h("label", { text: "When to use it" }), desc,
        h("p", { class: "sub hint-line", text: "The agent sees only this until it uses the skill." })),
      h("div", { class: "field" }, h("label", { text: "Instructions" }), body,
        h("p", { class: "sub hint-line", text: "What to do, in Markdown. It reads this when it uses the skill." })),
      extra.length > 0 && h("p", { class: "sub hint-line", text: `Its SKILL.md also sets ${andList(extra)}: kept as `
        + "they are." })));
    setTimeout(() => { grow(desc); grow(body); }, 0);
  }

  function drawFiles() {
    const paths = Object.keys(draft.files).sort((x, y) => (x !== SKILL_FILE) - (y !== SKILL_FILE) || x.localeCompare(y));
    fileInputs.clear();
    const pathIn = h("input", { class: "mono", placeholder: "reference.md", maxLength: 200, autocomplete: "off",
      autocapitalize: "none", "aria-label": "New file's path", "data-field": "file-new",
      oninput: () => { newPathErr = ""; pathErr.hidden = true; },
      onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); addFile(pathIn.value); } } });
    pathIn.setAttribute("spellcheck", "false");
    const pathErr = h("p", { class: "err", hidden: !newPathErr, text: newPathErr, "data-err": "file-new" });
    fill(filesBox,
      h("p", { class: "sub intro", text: raw ? "Its files." : "Other files the instructions point to, like a "
        + "reference or a script. Text only." }),
      paths.length ? h("div", { class: "rows" }, paths.map(fileRow)) : h("p", { class: "sub", text: "None." }),
      h("div", { class: "field" }, h("label", { text: "Add a file" }),
        h("div", { class: "row addrow" }, h("div", { class: "grow" }, pathIn),
          h("button", { type: "button", class: "small", text: "Add", onclick: () => addFile(pathIn.value) })),
        pathErr));
  }

  function fileRow(p) {
    const open = openFiles.has(p);
    const ta = open && h("textarea", { class: "code", value: draft.files[p], autocapitalize: "none",
      "aria-label": p, "data-field": "file:" + p, oninput: () => { draft.files[p] = ta.value; changed(); } });
    if (ta) { ta.setAttribute("spellcheck", "false"); fileInputs.set(p, ta); }
    return h("div", { class: "skillfile", "data-row": "file:" + p },
      h("div", { class: "row" }, h("span", { class: "mono grow name", text: p }),
        !(p in saved.files) && badge("", "new")),
      h("div", { class: "sub", text: lineCount(draft.files[p]) }),
      ta,
      h("div", { class: "row acts" },
        h("button", { type: "button", class: "small", text: open ? "Done" : "Edit", onclick: () => {
          if (open) openFiles.delete(p); else openFiles.add(p);
          drawFiles();
        } }),
        !(raw && p === SKILL_FILE) && h("button", { type: "button", class: "small danger", text: "Remove", onclick: () => {
          delete draft.files[p];
          openFiles.delete(p);
          drawFiles();
          changed();
        } })));
  }

  function addFile(text) {
    const p = String(text || "").trim();
    const show = (msg) => { newPathErr = msg; drawFiles(); };
    if (!p) return show("Type its path, like reference.md.");
    if (p === SKILL_FILE) return show("SKILL.md is the description and instructions above.");
    if (p in draft.files) return show(`It already has ${p}.`);
    if (!PATH_RE.test(p) || p.split("/").some((s) => s.startsWith("."))) {
      return show("Use a relative path like reference.md or scripts/check.sh: letters, digits, . _ - and /.");
    }
    newPathErr = "";
    draft.files[p] = "";
    openFiles.add(p);
    drawFiles();
    changed();
    if (fileInputs.has(p)) fileInputs.get(p).focus();
  }

  function drawDanger() {
    fill(dangerBox, h("div", { class: "row" }, h("button", { type: "button", class: "small danger", text: "Delete skill",
      onclick: askDelete })), ask.el, deleteErr);
  }

  function askDelete() {
    const used = agent ? [] : (detail.used_by || []).map(agentName);
    const many = used.length > 1;
    const q = agent ? `Delete ${agentName(agent)}'s skill ${name}? It keeps it until it restarts.`
      : used.length ? `${andList(used)} ${many ? "use" : "uses"} it. ${many ? "They keep" : "It keeps"} it until `
        + `${many ? "they restart" : "it restarts"}. Delete it?`
        : "Delete this skill? It stays in the definitions repo's history.";
    ask.ask(q, "Delete", async () => {
      deleteErr.hidden = true;
      try {
        await api("DELETE", path);
        gone = true;
        location.hash = back;
      } catch (x) { deleteErr.textContent = x.message; deleteErr.hidden = false; }
    }, "danger");
  }

  // ---- the save bar, as on the settings page ----
  const barMsg = h("div", { class: "bar-msg" });
  const barErr = h("p", { class: "err", hidden: true, "data-err": "bar" });
  const actions = h("div", { class: "actions" });
  const bar = h("div", { class: "savebar", hidden: true }, barMsg, barErr, actions);
  const go = (to) => { leaveTo = null; location.hash = to; };
  const discardBtn = h("button", { type: "button", text: "Discard", onclick: discard });
  const saveBtn = h("button", { type: "button", class: "primary", text: "Save", onclick: () => save() });
  const leaveSaveBtn = h("button", { type: "button", class: "primary", text: "Save", onclick: async () => {
    const to = leaveTo;
    if (await save()) go(to);
  } });
  const leaveDiscardBtn = h("button", { type: "button", class: "danger", text: "Discard & leave", onclick: () => {
    const to = leaveTo;
    discard();
    go(to);
  } });
  const stayBtn = h("button", { type: "button", text: "Stay", onclick: () => { leaveTo = null; drawBar(); } });

  function drawBar() {
    const dirty = isDirty();
    if (leaveTo !== null && !dirty) leaveTo = null;
    if (leaveTo !== null) {
      barMsg.textContent = busy || "You have unsaved changes";
      fill(actions, leaveSaveBtn, leaveDiscardBtn, stayBtn);
    } else {
      barMsg.textContent = busy || (dirty ? "Unsaved changes" : flashMsg);
      fill(actions, dirty && discardBtn, dirty && saveBtn);
    }
    for (const b of [discardBtn, saveBtn, leaveSaveBtn, leaveDiscardBtn, stayBtn]) b.disabled = !!busy;
    barMsg.hidden = !barMsg.textContent;
    bar.hidden = !(dirty || busy || flashMsg || !barErr.hidden);
  }

  function changed() { flashMsg = ""; barErr.hidden = true; drawBar(); }

  function flash(text) {
    flashMsg = text;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { flashMsg = ""; drawBar(); }, 3000);
    drawBar();
  }

  // The PUT body: what changed. SKILL.md as a file only when it's shown as one.
  function editBody() {
    const out = {};
    if (!raw && draft.description !== saved.description) out.description = draft.description.trim();
    if (!raw && draft.instructions !== saved.instructions) out.instructions = draft.instructions;
    const files = {};
    for (const [p, t] of Object.entries(draft.files)) if (saved.files[p] !== t) files[p] = t;
    for (const p of Object.keys(saved.files)) if (!(p in draft.files)) files[p] = null;
    if (Object.keys(files).length) out.files = files;
    return out;
  }

  async function save() {
    if (busy || !draft) return false;
    busy = "Saving…";
    barErr.hidden = true;
    drawBar();
    let ok = false;
    try {
      take(await api("PUT", path, editBody()));
      ok = true;
    } catch (x) { barErr.textContent = x.message; barErr.hidden = false; }
    busy = "";
    if (ok) flash("Saved."); else drawBar();
    return ok;
  }

  function discard() {
    draft = clone(saved);
    barErr.hidden = true;
    drawForm();
    drawFiles();
    drawBar();
  }

  function take(d) {
    detail = d;
    const content = d.content || {};
    raw = SKILL_FILE in content && typeof d.description !== "string";
    const files = Object.fromEntries(Object.entries(content).filter(([p]) => raw || p !== SKILL_FILE));
    saved = { description: d.description || "", instructions: d.instructions || "", files };
    draft = clone(saved);
    for (const p of [...openFiles]) if (!(p in files)) openFiles.delete(p);
    if (raw) openFiles.add(SKILL_FILE);
    drawTop();
    drawProblem();
    drawForm();
    drawFiles();
    drawDanger();
    rest.hidden = false;
    drawBar();
  }

  async function load() {
    try { take(await api("GET", path)); } catch (x) {
      if (x.status === 404) {
        el.replaceChildren(h("a", { class: "back", href: back, text: agent ? "‹ Settings" : "‹ Skills" }),
          h("p", { class: "empty", text: agent ? `${agentName(agent)} has no skill named ${name}.` : `No skill named ${name}.` }));
      } else fill(form, h("p", { class: "err", text: x.message }), h("button", { class: "small", text: "Retry", onclick: load }));
    }
  }

  const rest = h("div", { hidden: true }, h("h2", { text: "Files" }), filesBox, dangerBox);
  drawTop();
  const el = h("section", { class: "skill-view" }, top, note && note.el, problem, form, rest, bar);
  load();
  return {
    el,
    leave(to) {
      if (!isDirty()) return false;
      leaveTo = to;
      drawBar();
      return true;
    },
    frame(f) {
      if (note && (f.type === "hello" || (f.type === "agent" && f.agent.name === agent))) note.update();
      if (f.type === "hello" && !isDirty()) load();
      else if (f.type === "agent" && detail) drawTop();
    },
  };
}

// ---------- permissions (shared by the session and the inbox) ----------

// What an item asks: its kind ("permission" for a harness's own request, "tool" for a gateway ask, others as
// Valet adds them). An inbox item has it; a session's permission_request carries it as data.kind, or else it's the
// inbox's copy of the item, or a gateway ask by its toolCallId ("gateway-<id>").
function askKind(p) {
  if (p.kind) return p.kind;
  const item = S.inbox.get(p.id);
  if (item && item.kind) return item.kind;
  return isToolAsk(p) ? "tool" : "permission";
}

// An ask's payload, for the kinds another file draws (addAskView): Valet puts it under the kind's name, in the inbox
// item (a browser item's `browser`) and in its permission_request's data. -> {[kind]: payload}, or {}.
function payloadOf(kind, src) {
  return kind && EXTRA.asks.has(kind) && src && src[kind] != null ? { [kind]: src[kind] } : {};
}

// The answers to offer on a pending item p: {id, kind?, options}. Every option it carries, in its order, except on
// a harness's own permission request: its "always" choices are the harness's (Claude Code's last as long as its
// session), so once and reject are shown when it offers them, and the rest only when it offers nothing else.
function shownOptions(p) {
  const options = p.options || [];
  if (askKind(p) !== "permission") return options;
  const two = options.filter((o) => o.kind === "allow_once" || o.kind === "reject_once");
  return two.length ? two : options;
}

// The gateway's scoped answers, in words once given.
const ANSWERED = { allow_hour: "Allowed for an hour", allow_always: "Always allowed" };

// {status, outcome} of a permission -> {cls, text} once it's settled, else null.
function answerOf(st, options) {
  if (st.status === "expired") return { cls: "expired", text: "Expired" };
  if (st.status !== "answered") return null;
  const o = st.outcome;
  if (!o || o.outcome === "cancelled") return { cls: "expired", text: "Cancelled" };
  const id = String(o.optionId || "");
  const opt = options.find((x) => x.optionId === o.optionId);
  const kind = (opt && opt.kind) || (Object.hasOwn(ANSWERED, id) ? "allow" : "");
  if (kind.startsWith("allow")) {
    const text = Object.hasOwn(ANSWERED, id) ? ANSWERED[id] : kind === "allow_always" ? "Always allowed" : "Allowed";
    return { cls: "allowed", text };
  }
  if (kind.startsWith("reject")) return { cls: "rejected", text: "Rejected" };
  return { cls: "answered", text: opt ? opt.name : id };
}

// An option's button: the first allowing one is the main answer, a rejecting one is red, the rest plain.
function optionClass(o, opts) {
  const kind = o.kind || "";
  if (kind.startsWith("reject")) return "danger";
  return kind.startsWith("allow") && opts.find((x) => (x.kind || "").startsWith("allow")) === o ? "primary" : "";
}

async function answer(id, optionId, buttons, errEl) {
  buttons.forEach((b) => { b.disabled = true; });
  errEl.hidden = true;
  try {
    putInbox(await api("POST", `/api/inbox/${enc(id)}/answer`, { option_id: optionId }));
  } catch (x) {
    const body = x.body || {};
    const item = body.item || (body.detail && body.detail.item);
    if (item) { putInbox(item); return; }  // 410: the runner let go of it
    if (x.status === 409) {
      try { putInbox(await api("GET", `/api/inbox/${enc(id)}`)); return; } catch { /* fall through */ }
    }
    errEl.textContent = x.message;
    errEl.hidden = false;
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// The answer buttons of a pending item p: {id, kind?, options, toolCall?}.
function askEl(p, question) {
  const errEl = h("p", { class: "err", hidden: true });
  const opts = shownOptions(p);
  const buttons = opts.map((o) => h("button", { class: optionClass(o, opts), text: o.name, "data-option": o.optionId }));
  buttons.forEach((b, i) => b.addEventListener("click", () => answer(p.id, opts[i].optionId, buttons, errEl)));
  return h("div", { class: "ask" }, question && h("div", { class: "q", text: question }),
    h("div", { class: "options" }, buttons), errEl);
}

// A gateway ask: an inbox item of kind "tool", or the permission_request it puts in a session (its toolCallId is
// "gateway-<id>"). p.tool is {server, name, title, description, arguments}, from the item or the event; an event
// recorded before it carried one has only its title ("<server>: <tool>") and rawInput, shown as they are.
function isToolAsk(p) {
  return !!p.tool || p.kind === "tool" || /^gateway-/.test(String((p.toolCall && p.toolCall.toolCallId) || ""));
}

function toolAskOf(p) {
  const t = p.tool;
  if (t) return { server: t.server || "", title: t.title || t.name || "", description: t.description, args: t.arguments };
  return { server: "", title: String(p.title || (p.toolCall && p.toolCall.title) || ""), description: null,
    args: p.toolCall && p.toolCall.rawInput };
}

// "Scout wants to use github › Create issue", what the tool says it does, the arguments (folded), the answers.
function toolAskEl(p, extra, cls, result) {
  const t = toolAskOf(p);
  const who = (p.agent && agentName(p.agent)) || "The agent";
  return h("div", { class: cls + " toolask" }, extra,
    h("div", { class: "title" }, who + " wants to use ",
      h("span", { class: "tool-name", text: (t.server ? t.server + " › " : "") + (t.title || "a tool") })),
    t.description && h("div", { class: "sub desc", text: brief(t.description, 300) }),
    !isEmpty(t.args) && h("details", { class: "args" }, h("summary", { text: "Arguments" }),
      h("pre", { text: clip(json(t.args), 2000) })),
    p.status === "pending" && askEl(p),
    result && h("div", { class: "result", text: result }));
}

// A permission on its own card: p: {id, title, toolCall, options, status, outcome, kind?, tool?, agent?};
// details: show the tool's input (the inbox, and a request with no tool card in the session).
function permissionEl(p, extra, details) {
  const pending = p.status === "pending";
  const ans = answerOf(p, p.options);
  const result = p.status === "expired" ? "Expired: the agent stopped waiting"
    : ans ? ans.text : pending ? "" : "No longer waiting";
  const own = EXTRA.asks.get(askKind(p));       // a kind another file draws (addAskView)
  const card = own && own(p, extra, "perm" + (pending ? "" : " closed"), result);
  if (card) return card;
  if (isToolAsk(p)) return toolAskEl(p, extra, "perm" + (pending ? "" : " closed"), result);
  return h("div", { class: "perm" + (pending ? "" : " closed") }, extra,
    h("div", { class: "title", text: p.title || p.toolCall.title || "Permission request" }),
    details && toolDetails(p.toolCall),
    pending && askEl(p),
    result && h("div", { class: "result", text: result }));
}

function InboxView() {
  const list = h("div", { class: "inbox-list" });
  // id -> {key, el}: an item's card is kept while the item doesn't change, so what's typed in it (a question's
  // answer), its buttons while an answer is on its way and its error survive other items' frames and a hello.
  let cards = new Map();
  function card(i, next) {
    const key = JSON.stringify(i), was = cards.get(i.id);
    const el = was && was.key === key ? was.el : permissionEl(
      { id: i.id, title: i.title, toolCall: i.tool_call || {}, options: i.options || [], status: i.status, outcome: i.answer,
        kind: i.kind, tool: i.tool, agent: i.agent, text: i.text, created_at: i.created_at, ...payloadOf(i.kind, i) },
      h("div", { class: "row between agent-name" }, h("span", { text: `${i.agent} · ${ago(i.created_at)}` }),
        i.session_id && h("a", { href: `#/a/${enc(i.agent)}/s/${enc(i.session_id)}`, text: "Open session ›" })),
      true);
    next.set(i.id, { key, el });
    return el;
  }
  function draw() {
    const items = [...S.inbox.values()].filter((i) => i.status === "pending")
      .sort((x, y) => y.created_at - x.created_at);
    const next = new Map();
    const els = items.length ? items.map((i) => card(i, next))
      : [h("p", { class: "empty", text: "Nothing is waiting on you." })];
    cards = next;
    // Put back only if the list changed: a card taken out and put back loses the focus.
    const same = els.length === list.children.length && els.every((el, k) => list.children[k] === el);
    if (!same) list.replaceChildren(...els);
  }
  draw();
  api("GET", "/api/inbox").then((items) => { items.forEach((i) => S.inbox.set(i.id, i)); inboxCount(); draw(); },
    () => { /* the socket's hello covers it */ });
  return {
    el: h("section", {}, h("h1", { text: "Inbox", style: "margin-bottom: 12px" }), list),
    frame(f) { if (f.type === "hello" || f.type === "inbox" || f.type === "agent_deleted") draw(); },
  };
}

// ---------- the conversation ----------

function blockText(c) {
  if (!c) return "";
  switch (c.type) {
    case "text": return c.text || "";
    case "image": return "[image]";
    case "audio": return "[audio]";
    case "resource_link": return `[${c.name || c.uri}]`;
    case "resource": return (c.resource && (c.resource.text || `[${c.resource.uri}]`)) || "";
    default: return "";
  }
}

const TOOL_STATUS = { pending: "pending", in_progress: "running", completed: "done", failed: "failed" };
const DIFF_LINES = 400;

function isEmpty(v) { return v == null || v === "" || (typeof v === "object" && !Object.keys(v).length); }

// A line diff that's cheap and good enough for a tool's edit: the common head and tail are context, the middle is
// the change. (Claude's Edit sends snippets; a new file has no old text, so it's all added.)
function diffLines(oldText, newText) {
  const a = oldText == null ? [] : String(oldText).split("\n"), b = String(newText || "").split("\n");
  let p = 0, s = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  const ctx = 3, out = [];
  if (p > ctx) out.push(["…", ""]);
  for (let k = Math.max(0, p - ctx); k < p; k++) out.push([" ", a[k]]);
  for (let k = p; k < a.length - s; k++) out.push(["-", a[k]]);
  for (let k = p; k < b.length - s; k++) out.push(["+", b[k]]);
  for (let k = b.length - s; k < Math.min(b.length, b.length - s + ctx); k++) out.push([" ", b[k]]);
  if (s > ctx) out.push(["…", ""]);
  return out;
}

function diffEl(c) {
  const lines = diffLines(c.oldText, c.newText);
  const cls = { "+": "add", "-": "del" };
  return [h("div", { class: "loc mono", text: c.path || "" }),
    h("pre", { class: "diff" }, lines.slice(0, DIFF_LINES).map(([t, s]) =>
      h("span", { class: cls[t] || "ctx", text: (t === "…" ? "…" : t + " " + s) + "\n" })),
    lines.length > DIFF_LINES && h("span", { class: "ctx", text: `… ${lines.length - DIFF_LINES} more lines` }))];
}

// A ContentBlock a tool produced: text is markdown (Claude fences command output as ```console).
function contentEl(c) {
  if (c && c.type === "text") return h("div", { class: "md out" }, markdown(clip(c.text || "", 20000)));
  return h("pre", { text: clip(blockText(c)) });
}

// What a tool does, from its ACP ToolCall fields: the command, its content (diffs, output), else its raw input
// and output. Shared by tool cards and the inbox.
function toolDetails(d) {
  const out = [];
  const content = Array.isArray(d.content) ? d.content : [];
  const cmd = d.rawInput && typeof d.rawInput.command === "string" ? d.rawInput.command : null;
  if (cmd) out.push(h("pre", { class: "cmd", text: "$ " + clip(cmd) }));
  if (!content.some((c) => c.type === "diff")) {
    for (const l of d.locations || []) out.push(h("div", { class: "loc mono", text: l.path + (l.line ? ":" + l.line : "") }));
  }
  for (const c of content) {
    if (c.type === "diff") out.push(...diffEl(c));
    else if (c.type === "content") out.push(contentEl(c.content));
    else if (c.type === "terminal") out.push(h("div", { class: "loc", text: "Terminal output isn't shown here." }));
  }
  if (!content.length) {
    if (!cmd && !isEmpty(d.rawInput)) out.push(h("pre", { text: clip(json(d.rawInput)) }));
    if (!isEmpty(d.rawOutput)) out.push(h("pre", { text: clip(json(d.rawOutput)) }));
  }
  return out;
}

// Builds the log from events, in id order: chunks of one message are joined, tool calls merge their updates, and
// a permission request shows on its tool's card. Call flush() after a batch: it draws the messages that grew.
// Where a prompt came from, over its bubble, unless the owner typed it: "Schedule · Every day at 08:00", "Webhook
// deploy", "From alpha", "Wake-up", "The owner's answer" (the labels the control plane gave), and "Sent again" on a Retry.
const SOURCE_NAMES = { schedule: "Schedule", wakeup: "Wake-up", answer: "Your answer" };
function sourceLine(d) {
  const source = String(d.source || "owner");
  const kind = source.split(":")[0];
  const parts = [];
  if (kind !== "owner") {
    const label = d.label || "";
    const name = kind === "webhook" ? "Webhook" : kind === "agent" ? "From an agent" : SOURCE_NAMES[kind] || source;
    parts.push(kind === "schedule" && label ? `${name} · ${label}` : label || name);
  }
  if (d.retry_of) parts.push("Sent again");
  return parts.length ? h("div", { class: "from " + kind, "data-source": source, text: parts.join(" · ") }) : null;
}

// opts.retry(turnId): the owner's Retry of an interrupted or failed turn (a promise; its rejection's message is
// shown); without it, no Retry buttons.
function Renderer(log, opts = {}) {
  let last = null;           // the message being streamed: {kind, turn, text, body}
  const retries = new Map(); // turn id -> its Retry button's row, gone once a turn says it retries it
  const dirty = new Set();   // messages whose markdown is out of date
  const tools = new Map();   // toolCallId -> {data, turn, el, perm, auto, ended}
  const running = new Set(); // tools whose turn hasn't ended
  const perms = new Map();   // requestId -> {data, answer, el, tool}
  let plan = null;

  function add(el) { log.append(el); last = null; }

  function message(ev, d) {
    if (ev.kind === "user_message" && (!last || last.kind !== ev.kind || last.turn !== ev.turn_id)) {
      if (d.retry_of && retries.has(d.retry_of)) { retries.get(d.retry_of).remove(); retries.delete(d.retry_of); }
      const from = sourceLine(d);
      if (from) add(from);
    }
    if (ev.kind === "user_message" && d.handoff) {   // a hand-off (handoff.js): the earlier conversation, folded
      add(h("details", { class: "thought handoff" }, h("summary", { text: "Handed off: " + (d.handoff.source || "an "
        + "earlier conversation") }), h("div", { class: "msg user", text: blockText(d.content) })));
      if (d.handoff.note) add(h("div", { class: "msg user", text: d.handoff.note }));
      return;
    }
    const view = ev.kind !== "agent_thought" && blockView(d.content, ev);
    if (view) {     // an image, a file: after the message's text, which goes on above it if more comes
      log.append(h("div", { class: "blocks " + (ev.kind === "user_message" ? "user" : "agent") }, view));
      return;
    }
    if (!last || last.kind !== ev.kind || last.turn !== ev.turn_id) {
      const user = ev.kind === "user_message";
      const body = h("div", { class: user ? "msg user" : ev.kind === "agent_thought" ? "md" : "msg agent md" });
      add(ev.kind === "agent_thought" ? h("details", { class: "thought" }, h("summary", { text: "Thinking" }), body) : body);
      last = { kind: ev.kind, turn: ev.turn_id, text: "", body };
    }
    last.text += blockText(d.content);
    if (ev.kind === "user_message") last.body.textContent = last.text;  // the owner's text, as typed
    else dirty.add(last);
  }

  // Re-rendering the whole message keeps the markdown right as it streams (a fence closes, a list grows); doing
  // it once per batch rather than per chunk keeps a long history cheap to load.
  function flush() {
    for (const m of dirty) fill(m.body, markdown(m.text));
    dirty.clear();
  }

  function permState(p) {
    const item = S.inbox.get(p.data.requestId);
    return p.answer || (item ? { status: item.status, outcome: item.answer } : { status: "closed" });
  }

  // Open while it runs or asks; closed once done (or its turn ended). Only a change of that state moves it, so
  // a card the owner opened or closed stays that way.
  function drawTool(t) {
    const d = t.data;
    const status = d.status || "pending";
    const options = t.perm ? t.perm.data.options || [] : [];
    const st = t.perm && permState(t.perm);
    const asking = !!st && st.status === "pending";
    const ans = st && answerOf(st, options);
    const open = asking || (!t.ended && status !== "completed" && status !== "failed");
    if (open !== t.auto) { t.auto = open; t.el.open = open; }
    t.el.className = "tool" + (asking ? " asking" : "");
    const body = toolDetails(d);
    // DeepSeek Harness titles a call with the bare tool name ("bash") and describes it in rawInput.description.
    const desc = d.rawInput && typeof d.rawInput.description === "string" ? d.rawInput.description : "";
    const name = (d.title || "Tool call") + (desc && desc !== d.title ? " · " + desc : "");
    fill(t.el,
      h("summary", {}, h("span", { class: "name", text: name }),
        d.kind && d.kind !== "other" && h("span", { class: "kind", text: d.kind }),
        asking ? badge("waiting", "needs you") : ans && badge(ans.cls, ans.text),
        badge(status, TOOL_STATUS[status] || status)),
      h("div", { class: "body" }, body.length ? body : h("div", { class: "sub", text: "No details." }),
        asking && askEl({ id: t.perm.data.requestId, kind: t.perm.data.kind, options,
          toolCall: t.perm.data.toolCall }, "Allow this?")));
  }

  function tool(ev, d, isUpdate) {
    let t = tools.get(d.toolCallId);
    if (!t) {
      t = { data: {}, turn: ev.turn_id, el: h("details", { class: "tool" }), perm: null, auto: null, ended: false };
      tools.set(d.toolCallId, t);
      running.add(t);
      add(t.el);
    }
    for (const [k, v] of Object.entries(d)) if (v != null && (!isUpdate || k !== "toolCallId")) t.data[k] = v;
    drawTool(t);
  }

  function drawPerm(p) {
    if (p.tool) { drawTool(p.tool); return; }
    const state = permState(p);
    const el = permissionEl({ id: p.data.requestId, title: (p.data.toolCall || {}).title, toolCall: p.data.toolCall || {},
      options: p.data.options || [], status: state.status, outcome: state.outcome, agent: p.agent,
      tool: p.data.tool || null, kind: p.data.kind, text: p.data.text, ...payloadOf(p.data.kind, p.data) }, null,
      true);
    if (p.el) p.el.replaceWith(el);
    p.el = el;
  }

  function permission(d, agent) {
    const tc = d.toolCall || {};
    const t = tools.get(tc.toolCallId) || null;
    const p = { data: d, answer: null, el: null, tool: t, agent };
    perms.set(d.requestId, p);
    if (!t) { drawPerm(p); add(p.el); return; }
    t.perm = p;  // the request's ToolCallUpdate is the tool's latest state
    for (const [k, v] of Object.entries(tc)) if (v != null && k !== "toolCallId" && k !== "status") t.data[k] = v;
    drawTool(t);
  }

  function ended(ev, d) {
    last = null;
    for (const t of running) {
      if (t.turn !== ev.turn_id) continue;
      running.delete(t);
      t.ended = true;
      drawTool(t);
    }
    const why = d.error ? (d.error === "interrupted" ? "Interrupted" : "Error: " + d.error)
      : { cancelled: "Cancelled", refusal: "The agent refused", max_tokens: "Stopped: output limit reached",
        max_turn_requests: "Stopped: too many steps" }[d.stopReason];
    if (why) add(h("div", { class: "ended" + (d.error || d.stopReason === "refusal" ? " bad" : ""), text: why }));
    if (d.error && opts.retry && ev.turn_id) {
      const err = h("p", { class: "err", hidden: true });
      const btn = h("button", { class: "small", text: "Retry", "data-retry": ev.turn_id, onclick: async () => {
        btn.disabled = true;
        err.hidden = true;
        try { await opts.retry(ev.turn_id); } catch (x) {
          err.textContent = x.message;
          err.hidden = false;
          btn.disabled = false;
        }
      } });
      const row = h("div", { class: "retry" }, btn, err);
      retries.set(ev.turn_id, row);
      add(row);
    }
  }

  function apply(ev) {
    const d = ev.data || {};
    switch (ev.kind) {
      case "user_message": case "agent_message": case "agent_thought": message(ev, d); break;
      case "tool_call": tool(ev, d, false); break;
      case "tool_call_update": tool(ev, d, true); break;
      case "plan": {
        if (plan) plan.remove();
        plan = h("div", { class: "card plan" }, h("strong", { text: "Plan" }), h("ol", {},
          (d.entries || []).map((e) => h("li", { class: e.status || "", text: e.content || "" }))));
        add(plan);
        break;
      }
      case "permission_request": permission(d, ev.agent); break;
      case "permission_answer": {
        const p = perms.get(d.requestId);
        if (p) { p.answer = { status: d.status, outcome: d.outcome }; drawPerm(p); }
        break;
      }
      case "turn_end": ended(ev, d); break;
      default: break;  // kinds this client doesn't render yet
    }
  }

  function refresh(id) { const p = perms.get(id); if (p && !p.answer) drawPerm(p); }

  return { apply, flush, refresh };
}

function atBottom() { return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 80; }
function scrollEnd() { window.scrollTo(0, document.documentElement.scrollHeight); }

function SessionView(name, sid) {
  const base = `/api/agents/${enc(name)}/sessions/${enc(sid)}`;
  const log = h("div", { class: "log" });
  const r = Renderer(log, { retry: async (turnId) => {
    const res = await api("POST", `${base}/turns/${enc(turnId)}/retry`);
    if (res && res.queued != null) showQueued();
  } });
  let session = null;
  let cursor = 0;            // highest event id applied
  let syncing = false, again = false, queue = [], sending = false;

  const title = h("h1", { class: "grow", text: "Session" });
  const mainBadge = h("span", { class: "badge main", hidden: true, text: "Main session",
    title: "Where its schedules, other agents and your answers arrive" });
  const queueBox = h("div", { class: "queue", hidden: true });
  const notice = h("div");
  const trouble = startTrouble();
  const cancel = h("button", { class: "small", text: "Cancel", onclick: async () => {
    cancel.disabled = true;
    try { await api("POST", base + "/cancel"); } catch (x) { if (x.status !== 409) showErr(x.message); }
    cancel.disabled = false;
  } });
  const working = h("div", { class: "working", hidden: true }, h("span", { class: "pulse", text: "Working…" }), cancel);
  const input = h("textarea", { rows: 1, placeholder: "Message", enterKeyHint: "send", "data-field": "prompt" });
  const send = h("button", { class: "primary", type: "submit", text: "Send" });
  const err = h("p", { class: "err", hidden: true });
  const el = h("section", { class: "session-view" });
  const parts = EXTRA.composer.map((make) => make({ name, sid, el, input, submit: () => submit(), showErr,
    changed: () => drawState() }));
  const form = h("form", { class: "composer" }, parts.map((p) => p.el), input, send);
  const queued = h("p", { class: "sub queued-note", hidden: true });
  fill(el, h("a", { class: "back", href: "#/a/" + enc(name), text: "‹ " + (agentTitle(S.agents.get(name)) || name) }),
    h("div", { class: "row between" }, title, mainBadge),
    notice, log,
    h("div", { class: "dock" }, queueBox, working, err, queued, parts.map((p) => p.above), form));

  function showErr(text) { err.textContent = text; err.hidden = !text; }

  let queuedTimer = 0;
  function showQueued() {
    queued.textContent = "Queued: it goes when the agent finishes what it's doing.";
    queued.hidden = false;
    clearTimeout(queuedTimer);
    queuedTimer = setTimeout(() => { queued.hidden = true; }, 6000);
  }

  // The prompts waiting for this session's turn (Session.queue), each with Remove.
  function drawQueue() {
    const q = (session && session.queue) || [];
    queueBox.hidden = q.length === 0;
    if (!q.length) { queueBox.replaceChildren(); return; }
    fill(queueBox, h("div", { class: "sub", text: q.length === 1 ? "1 prompt waiting" : `${q.length} prompts waiting` }),
      q.map((item) => {
        const btn = h("button", { class: "small", text: "Remove", onclick: async () => {
          btn.disabled = true;
          try { await api("DELETE", `${base}/queue/${item.id}`); } catch (x) { if (x.status !== 404) showErr(x.message); }
          await refreshSession();
        } });
        const from = item.source && item.source !== "owner" ? (item.label || item.source) + ": " : "";
        const att = (item.attachments || []).map((x) => x.name).join(", ");
        return h("div", { class: "queued row", "data-row": "queued-" + item.id },
          h("span", { class: "grow", text: from + brief(item.text, 160) + (att ? (item.text ? " + " : "") + att : "") }),
          btn);
      }));
  }

  function autosize() { input.style.height = "auto"; input.style.height = input.scrollHeight + 2 + "px"; }

  function drawState() {
    if (!session) return;
    title.textContent = session.title || "New session";
    mainBadge.hidden = !session.main;
    working.hidden = session.state !== "working";
    send.disabled = sending || parts.some((p) => p.busy && p.busy());
    input.placeholder = session.state === "working" ? "Queue a message" : "Message";   // one line at 390px wide
    drawQueue();
  }

  function drawNotice() {
    const a = S.agents.get(name);
    if (!a || (a.state === "running" && a.connected)) { trouble.clear(); notice.replaceChildren(); return; }
    const what = a.desired === "running" && !a.connected ? "Its runner is offline."
      : a.state === "starting" ? "The agent is starting."
      : a.state === "failed" ? "The agent failed." : "The agent is stopped.";
    fill(notice, h("div", { class: "note" },
      h("div", { class: "row between" }, h("span", { class: "grow", text: what }),
        a.state === "starting" ? null : agentButtons(a, trouble)),
      a.state === "failed" && a.error && h("p", { class: "err", text: a.error }),
      needsLine(a, trouble)), trouble.el);
  }

  // Applies events in order, once each, then draws: stays at the bottom if the reader was there.
  function applyAll(evs) {
    const stick = atBottom();
    for (const ev of evs) {
      if (ev.id <= cursor) continue;
      cursor = ev.id;
      r.apply(ev);
    }
    r.flush();
    if (stick) scrollEnd();
  }

  // History first, then whatever arrived live meanwhile: the socket's events are queued while a fetch runs so
  // nothing applies out of order. Called on open and after every reconnect (from the cursor). The queue is
  // applied only after the history is complete: applying it after a failed fetch would move the cursor past
  // the events that fetch was for, and they'd never be fetched again. So a failed fetch is retried.
  async function sync() {
    if (syncing) { again = true; return; }
    syncing = true;
    let failed = false;
    for (let delay = 1000; ; delay = Math.min(delay * 2, 30000)) {
      try {
        do {
          again = false;
          for (let more = true; more;) {
            const res = await api("GET", `${base}/events?after=${cursor}&limit=500`);
            applyAll(res.events);
            more = res.more && res.events.length > 0;
          }
        } while (again);
        break;
      } catch (x) {
        if (x.status === 401 || x.status === 404 || !el.isConnected) {
          syncing = false;      // signed out or gone: drop the queue unapplied; the next sync refetches it
          queue.length = 0;
          return;
        }
        failed = true;
        showErr(x.message);
        await new Promise((resolve) => setTimeout(resolve, delay));
      }
    }
    syncing = false;
    if (failed) showErr("");
    applyAll(queue.splice(0));
  }

  async function refreshSession() {
    try { session = await api("GET", base); drawState(); } catch (x) {
      if (x.status === 404) el.replaceChildren(h("a", { class: "back", href: "#/a/" + enc(name), text: "‹ " + name }),
        h("p", { class: "empty", text: "No such session." }));
    }
  }

  async function submit() {
    const text = input.value;
    if ((!text.trim() && !parts.some((p) => p.has())) || sending || parts.some((p) => p.busy && p.busy())) return;
    sending = true;
    drawState();
    showErr("");
    try {
      const body = { text };
      for (const p of parts) Object.assign(body, await p.body());
      const res = await api("POST", base + "/prompt", body);
      input.value = "";
      for (const p of parts) p.sent();
      if (res && res.queued != null) showQueued();
    } catch (x) {
      // A 502/503 was recorded, and the turn ended with the error; unless nothing was sent (an attachment couldn't
      // be placed): then the message stays as it is, to send again.
      if ((x.status === 502 || x.status === 503) && !(x.body && x.body.sent === false)) {
        input.value = "";
        for (const p of parts) p.sent();
      }
      showErr(x.message);
    }
    autosize();
    sending = false;
    await refreshSession();
    scrollEnd();
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    if (e.metaKey || e.ctrlKey || matchMedia("(pointer: fine)").matches) { e.preventDefault(); submit(); }
  });

  drawNotice();
  sync().then(scrollEnd);  // marks the view as syncing at once, so early socket events queue behind history
  refreshSession();
  return {
    el,
    frame(f) {
      if (f.type === "event") {
        const ev = f.event;
        if (ev.agent !== name || ev.session_id !== sid) return;
        if (syncing) { queue.push(ev); return; }
        applyAll([ev]);
      } else if (f.type === "session") {
        if (f.session.agent === name && f.session.id === sid) { session = f.session; drawState(); }
      } else if (f.type === "agent") {
        if (f.agent.name === name) drawNotice();
      } else if (f.type === "inbox") {
        r.refresh(f.item.id);
      } else if (f.type === "hello") {
        drawNotice();
        refreshSession();
        sync();
      }
    },
  };
}

document.addEventListener("DOMContentLoaded", () => boot());  // after every script in index.html
