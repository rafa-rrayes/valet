// Drives app.js's screens (settings page, new-agent form, agent list and page, inbox, secrets & tools, skills,
// directives) under the stand-in DOM (fakedom.js), against a fake API. Usage: node settings.js <static dir>, with a
// JSON list of jobs on stdin; prints a JSON list of results.
//   {"view": "settings"|"new"|"agents"|"agent"|"inbox"|"secrets"|"skills"|"skill"|"directives"|"session"|"route",
//    "name"?: str, "agent"?: str (skill: an agent's own), "session"?: str (session: its id), "hash"?: str (route),
//    "routes": {"METHOD /path": {status, body}, or a list of them answered in turn}, "agents"?: [Agent],
//    "inbox"?: [InboxItem], "catalog"?: Catalog, "actions": [Action]}
//       -> {requests: [{method, path, body}], controls: {field: {value, checked, disabled}}, chosen: [data-choice keys on],
//           errors: {key: text of the visible error slots}, rows: {data-row or data-tool: that element's HTML},
//           text, seen (the text without hidden elements), html, hash}
//   {"call": fn, "args": [...]} -> {result}: a plain function of app.js (slugify, newAgentBody, fieldFor, ...)
// Actions: {"click": data-choice}, {"button": text, "within"?: class of an ancestor, "row"?: data-row of an
// ancestor}, {"input"|"change": data-field, "value"}, {"check": data-field, "checked"}, {"blur": data-field},
// {"enter": data-field} (the Enter key), {"frame": a socket frame}, {"open": class of a <details>}, {"go": a hash,
// as if a link was followed}, {"files": data-field, "names": [file names]} (files picked in a file input).
// A request whose body isn't JSON (FormData) is recorded as {form: [[field, file name or value], ...]}. A route's
// body that's a string answers res.text() as is. "eval": an expression run in the page after the actions -> {value}.
// Also {"submit": data-field} (its form is submitted, as the Send button does), {"wait": ms}
// (for a screen's own timers, like a preview a moment after typing).
"use strict";
const fs = require("fs");
const vm = require("vm");
const { html, load } = require("./fakedom");

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function walk(n, out = []) {
  if (n.tag !== "#text") out.push(n);
  for (const c of n.children) walk(c, out);
  return out;
}

// The text a reader sees: textContent without what's hidden.
function seen(n) { return n.hidden ? "" : n._text + n.children.map(seen).join(""); }

function shown(n) {
  for (let x = n; x; x = x.parent) if (x.hidden) return false;
  return true;
}

async function fire(el, type, extra) {
  for (const fn of el.listeners[type] || []) await fn({ target: el, preventDefault() {}, ...extra });
}

function within(e, test) {
  for (let x = e; x; x = x.parent) if (test(x)) return true;
  return false;
}

async function run(job) {
  if (job.call) {
    const ctx = load(process.argv[2]);
    return { result: ctx[job.call](...job.args) };
  }
  const requests = [];
  const routes = job.routes || {};
  async function fetch(path, opts) {
    const method = (opts && opts.method) || "GET";
    const raw = opts && opts.body;
    const body = !raw ? null : typeof raw === "string" ? JSON.parse(raw) : raw.entries ? { form: raw.entries } : {};
    requests.push({ method, path, body });
    // A list answers in turn (its last answer repeats): a start refused, then one that goes through.
    let r = routes[`${method} ${path}`];
    if (Array.isArray(r)) r = r.length > 1 ? r.shift() : r[0];
    r = r || { status: 404, body: { detail: "not found" } };
    return { ok: r.status < 300, status: r.status, statusText: "", json: async () => r.body,
      text: async () => (typeof r.body === "string" ? r.body : JSON.stringify(r.body)) };
  }
  const ctx = load(process.argv[2], fetch);
  const S = ctx.S;
  S.signedIn = true;
  S.agents = new Map((job.agents || []).map((a) => [a.name, a]));
  if (job.catalog) S.catalog = job.catalog;
  if (job.inbox) S.inbox = new Map(job.inbox.map((i) => [i.id, i]));
  // "route": the app's own router from job.hash; setting location.hash in an action stands for a hashchange, and
  // history.replaceState puts the hash back without one, as in a browser.
  const replaced = [];
  let routed = null;
  const hashchange = () => { while (ctx.location.hash !== routed) { routed = ctx.location.hash; ctx.route(); } };
  let view = null;
  if (job.view === "route") {
    Object.assign(ctx.location, { hash: job.hash, pathname: "/", search: "" });
    ctx.history = { state: null, replaceState(st, title, url) {
      replaced.push(url);
      ctx.location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
      routed = ctx.location.hash;
    } };
    hashchange();
  } else {
    view = job.view === "settings" ? ctx.SettingsView(job.name)
      : job.view === "new" ? ctx.NewAgentForm(() => {})
      : job.view === "agent" ? ctx.AgentView(job.name)
      : job.view === "inbox" ? ctx.InboxView()
      : job.view === "secrets" ? ctx.SecretsToolsView()
      : job.view === "skills" ? ctx.SkillsView()
      : job.view === "skill" ? ctx.SkillView(job.name, job.agent || null)
      : job.view === "directives" ? ctx.DirectivesView()
      : job.view === "session" ? ctx.SessionView(job.name, job.session) : ctx.AgentsView();
    S.view = view.frame ? view : null;
  }
  const cur = () => view || S.view;
  for (let i = 0; i < 5; i++) await tick();

  const all = () => walk(cur().el);
  const byField = (f) => {
    const el = all().find((e) => e.attrs["data-field"] === f);
    if (!el) throw new Error("no control " + f);
    return el;
  };
  for (const a of job.actions || []) {
    if (a.click) {
      const el = all().find((e) => e.attrs["data-choice"] === a.click);
      if (!el) throw new Error("no choice " + a.click);
      await fire(el, "click");
    } else if (a.button) {
      const inside = (e) => (!a.within || within(e, (x) => x.className.split(" ").includes(a.within)))
        && (!a.row || within(e, (x) => x.attrs["data-row"] === a.row));
      const el = all().find((e) => e.tag === "button" && e.textContent === a.button && shown(e) && inside(e));
      if (!el) throw new Error("no button " + a.button + (a.row ? " in " + a.row : ""));
      await fire(el, "click");
    } else if (a.enter) {
      await fire(byField(a.enter), "keydown", { key: "Enter" });
    } else if (a.input || a.change) {
      const el = byField(a.input || a.change);
      el.value = a.value;
      await fire(el, a.input ? "input" : "change");
    } else if (a.check) {
      const el = byField(a.check);
      el.checked = a.checked;
      await fire(el, "change");
    } else if (a.blur) {
      await fire(byField(a.blur), "blur");
    } else if (a.frame) {
      ctx.onFrame(a.frame);
    } else if (a.open) {
      const el = all().find((e) => e.tag === "details" && e.className.split(" ").includes(a.open)
        && (!a.row || within(e, (x) => x.attrs["data-row"] === a.row)));
      el.open = true;
      await fire(el, "toggle");
    } else if (a.go != null) {
      ctx.location.hash = a.go;
    } else if (a.files) {
      const el = byField(a.files);
      el.files = a.names.map((name) => ({ name, size: 1, type: "" }));
      await fire(el, "change");
    } else if (a.submit) {
      let form = byField(a.submit);
      while (form && form.tag !== "form") form = form.parent;
      await fire(form, "submit");
    } else if (a.wait) {
      await new Promise((resolve) => setTimeout(resolve, a.wait));
    }
    for (let i = 0; i < 5; i++) {
      await tick();
      if (job.view === "route") hashchange();
    }
  }

  const controls = {}, errors = {}, chosen = [], rows = {};
  for (const e of all()) {
    const key = e.attrs["data-row"] || e.attrs["data-tool"];
    if (key && !(key in rows)) rows[key] = html(e);
    const f = e.attrs["data-field"];
    if (f && shown(e)) controls[f] = { value: e.tag === "div" ? e.textContent : e.value, checked: e.checked, disabled: e.disabled };
    if (e.attrs["data-err"] && shown(e) && e.textContent) errors[e.attrs["data-err"]] = e.textContent;
    if (e.attrs["data-choice"] && (e.attrs["aria-checked"] === "true" || e.attrs["aria-pressed"] === "true")) {
      chosen.push(e.attrs["data-choice"]);
    }
  }
  return { requests, controls, chosen, errors, rows, text: cur().el.textContent, seen: seen(cur().el),
           html: html(cur().el), hash: ctx.location.hash,
           flash: S.flash, agent: S.agents.get(job.name) || null, replaced, viewClass: cur().el.className,
           value: job.eval ? vm.runInContext(job.eval, ctx) : undefined };
}

(async () => {
  const jobs = JSON.parse(fs.readFileSync(0, "utf8"));
  const out = [];
  for (const job of jobs) out.push(await run(job));
  console.log(JSON.stringify(out));
  process.exit(0);  // the save bar's "Saved." timer would keep node up for seconds
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
