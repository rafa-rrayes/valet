"use strict";
// The browser (docs/API.md, "The browser" and "The live view"): Chrome with the owner's logins, one per profile, which
// agents use through valet.browser. This file draws
// - #/browser: the profiles (running or not, stop, the live view), a new profile, the live hour grants, the activity
//   log, and browser.yaml as text (Advanced);
// - #/browser/<profile>: one profile: its title, note and site rules, its tabs, its saved logins, deleting it;
// - #/browser/<profile>/live[/<tab>]: the live view, a full-screen layer over the page;
// - Settings › Browser: the profiles an agent uses and its own site rules (browser.profiles, browser.rules);
// - browser asks (inbox items of kind `browser`) in the inbox and in the session.
// The vault, saved logins for the profiles, is vault.js. Loaded after app.js, whose helpers it uses.

const BROWSER_SERVER = "valet.browser";
const PROFILE_RE = /^[a-z0-9][a-z0-9-]{0,30}$/;
const PROFILE_RULE = "Lowercase letters, digits and dashes, starting with a letter or digit, 31 at most.";
// The kinds of action a site rule sets: [key, name, what it covers].
const BROWSER_KINDS = [["read", "Read", "open and read pages, scroll"], ["interact", "Interact", "click, type, choose"],
  ["commit", "Commit", "save, send, pay, delete"], ["login", "Sign in", "fill a saved login"]];
const KIND_WORDS = { read: "Read", interact: "Interact", commit: "Commit", login: "Sign in", human: "Take over" };
const AUDIT_PAGE = 30;
const AUDIT_OUTCOME = { ok: ["completed", "done"], failed: ["failed", "failed"], denied: ["rejected", "blocked"],
  rejected: ["rejected", "rejected"], expired: ["expired", "no answer"], cancelled: ["expired", "cancelled"] };
const AUDIT_ACTION = { open_tab: "opened a tab", navigate: "went to", snapshot: "read the page",
  screenshot: "took a screenshot", click: "clicked", type_text: "typed into", select_option: "chose in",
  press_key: "pressed", scroll: "scrolled", back: "went back", close_tab: "closed a tab", login: "signed in",
  request_human: "asked you to take over", live_view: "opened the live view", live_navigate: "went to",
  live_open: "opened a tab" };
const BROWSER = { newLogin: null };   // vault.js: the profile a "New login" from a profile's screen is for

addTab("browser", "Browser", ["vault"]);
addRoute((parts) => {
  if (parts[0] !== "browser") return null;
  if (!parts[1]) return BrowserView();
  return parts[2] === "live" ? LiveView(parts[1], parts[3] || null) : ProfileView(parts[1]);
});
addSettingsSection({ key: "browser", title: "Browser", after: "tools", make: browserSection });
addAskView("browser", (p, head, cls, result) => (p.browser ? browserAskCard(p, { head, cls, result }) : null));

// ---------- small helpers ----------

function profileTitle(p) { return (p && (p.title || p.name)) || ""; }
function profileHash(name) { return "#/browser/" + enc(name); }
function liveHash(profile, tab) { return `#/browser/${enc(profile)}/live` + (tab ? "/" + enc(tab) : ""); }
function shotUrl(name) { return "/api/browser/shots/" + enc(name); }
function plural(n, one, many) { return `${n} ${n === 1 ? one : many || one + "s"}`; }

// A site as typed or pasted -> a pattern as Valet stores it: lowercase, no scheme, query or trailing slash
// ("https://GitHub.com/foo/?a=1" -> "github.com/foo"). The server still checks it.
function sitePattern(text) {
  return String(text || "").trim().toLowerCase().replace(/^[a-z][a-z0-9+.-]*:\/\//, "").split(/[?#]/)[0]
    .replace(/\/+$/, "");
}

// Repeats fn every ms while el is on the page (stops by itself once it isn't); stop() to end it sooner.
function every(ms, fn, el) {
  let t = 0, done = false;
  const next = () => { t = setTimeout(() => { if (done || !el.isConnected) return; fn(); next(); }, ms); };
  next();
  return () => { done = true; clearTimeout(t); };
}

// ---------- site rules: a profile's `sites`, an agent's `browser.rules` ----------
// Rows of a site and, per kind, Not set / Run / Ask you / Block. A row with no site or nothing set isn't a rule
// yet: rules() leaves it out (and says so on the row). set(rules) redraws from rules unless the rows already say
// that (so typing isn't interrupted). onChange(rules) after every edit. `key` prefixes data-field names.
function sitesEditor({ key, onChange, empty }) {
  let rows = [];          // [{site, rule: {kind: mode}, order: [kind] as it came, warn: element}]
  let lastInput = null;
  const el = h("div", { class: "sites-ed" });

  function rules() {
    const out = {};
    for (const r of rows) {
      const site = r.site.trim();
      if (!site || !Object.keys(r.rule).length || Object.hasOwn(out, site)) continue;
      out[site] = {};     // the kinds in the order they came in, so setting one back to what it was is no change
      for (const k of [...r.order, ...BROWSER_KINDS.map(([x]) => x)]) if (r.rule[k] && !out[site][k]) out[site][k] = r.rule[k];
    }
    return out;
  }

  function warnings() {
    const seen = new Set();
    for (const r of rows) {
      const site = r.site.trim();
      const msg = !site ? (Object.keys(r.rule).length ? "Type the site it's for." : "")
        : seen.has(site) ? "This site is listed twice: only the first counts."
          : !Object.keys(r.rule).length ? "Pick at least one: a site with nothing set isn't a rule." : "";
      seen.add(site);
      r.warn.textContent = msg;
      r.warn.hidden = !msg;
    }
  }

  function edited() { warnings(); onChange(rules()); }

  function rowEl(r, i) {
    const site = h("input", { class: "mono", value: r.site, placeholder: "github.com or *.google.com",
      autocapitalize: "none", autocomplete: "off", inputmode: "url", "aria-label": "Site", "data-field": `${key}-site:${i}`,
      oninput: (e) => { r.site = e.target.value.trim(); edited(); },
      onblur: (e) => {
        const v = sitePattern(e.target.value);
        if (v !== e.target.value) { r.site = e.target.value = v; edited(); }
      } });
    site.setAttribute("spellcheck", "false");
    lastInput = site;
    r.warn = h("p", { class: "warn-text", hidden: true });
    const kinds = BROWSER_KINDS.map(([k, name, what]) => {
      const sel = h("select", { "aria-label": `${name} on this site`, "data-field": `${key}-${k}:${i}`, title: what },
        h("option", { value: "", text: "Not set" }), RULE_CHOICES.map(([v, words]) => h("option", { value: v, text: words })));
      sel.value = r.rule[k] || "";
      sel.className = "mode " + (r.rule[k] || "unset");
      sel.addEventListener("change", () => {
        if (sel.value) r.rule[k] = sel.value; else delete r.rule[k];
        sel.className = "mode " + (sel.value || "unset");
        edited();
      });
      return h("label", { class: "kind" }, h("span", { text: name }), sel);
    });
    return h("div", { class: "site-rule", "data-row": `${key}:${i}` },
      h("div", { class: "site-line" }, site, h("button", { type: "button", class: "small danger", text: "Remove",
        onclick: () => { rows.splice(i, 1); draw(); edited(); } })),
      h("div", { class: "kinds" }, kinds), r.warn);
  }

  function draw() {
    lastInput = null;
    fill(el, !rows.length && h("p", { class: "sub", text: empty }), rows.map(rowEl),
      h("button", { type: "button", class: "small add-site", text: "Add a site", onclick: () => {
        rows.push({ site: "", rule: {}, order: [] });
        draw();
        if (lastInput) lastInput.focus();
      } }));
    warnings();
  }

  draw();
  return {
    el, rules,
    set(next) {
      if (JSON.stringify(next || {}) === JSON.stringify(rules())) return;
      rows = Object.entries(next || {}).map(([site, rule]) => ({ site, rule: { ...rule }, order: Object.keys(rule) }));
      draw();
    },
  };
}

// ---------- the activity log ----------

function auditRow(a) {
  const [cls, words] = AUDIT_OUTCOME[a.outcome] || ["", String(a.outcome || "")];
  const who = a.agent ? agentName(a.agent) : "You";
  const did = AUDIT_ACTION[a.action] || a.action;
  return h("div", { class: "audit", "data-row": "audit:" + a.id },
    h("div", { class: "row" }, h("span", { class: "grow what" }, h("strong", { text: who }), " " + did,
      a.target && [" ", h("span", { class: "mono target", text: a.target })]), badge(cls, words)),
    h("div", { class: "sub", text: [clock(a.ts), a.host, a.profile, a.kind && KIND_WORDS[a.kind],
      a.decision === "ask" && "asked you", a.note].filter(Boolean).join(" · ") }),
    a.shot && h("a", { class: "shot-link", href: shotUrl(a.shot), target: "_blank", rel: "noopener" },
      h("img", { class: "shot", src: shotUrl(a.shot), alt: "The page then", loading: "lazy" })));
}

// The activity log, newest first, `filter` () -> {agent?, profile?}; More pages back with `before`.
function auditBox(filter) {
  let rows = [], more = false, err = "", loaded = false, seq = 0;
  const el = h("div", { class: "audit-box" });
  function draw() {
    fill(el, err && h("p", { class: "err", text: err }),
      !loaded && !err && h("p", { class: "sub", text: "Loading…" }),
      loaded && !rows.length && h("p", { class: "empty", text: "Nothing yet." }),
      rows.length > 0 && h("div", { class: "card" }, h("div", { class: "rows" }, rows.map(auditRow))),
      more && h("button", { type: "button", class: "small", text: "Older", onclick: (e) => {
        e.target.disabled = true;
        load(rows[rows.length - 1].id);
      } }));
  }
  async function load(before) {
    const s = ++seq;
    const f = filter();
    const q = [`limit=${AUDIT_PAGE}`, f.agent && "agent=" + enc(f.agent), f.profile && "profile=" + enc(f.profile),
      before && "before=" + before].filter(Boolean).join("&");
    try {
      const r = await api("GET", "/api/browser/audit?" + q);
      if (s !== seq) return;
      rows = before ? rows.concat(r.actions) : r.actions;
      more = !!r.more;
      err = "";
    } catch (x) { if (s === seq) err = x.message; }
    loaded = true;
    draw();
  }
  draw();
  return { el, load };
}

// ---------- live hour grants ----------

// The "Allow for an hour" answers still running, each with Revoke. `agent`: only that agent's (its settings).
function grantsBox(agent, after) {
  let list = null;
  const el = h("div", { class: "grants-box" });
  function row(g) {
    const err = h("p", { class: "err", hidden: true });
    const btn = h("button", { type: "button", class: "small", text: "Revoke" });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      err.hidden = true;
      try { await api("DELETE", `/api/browser/grants/${enc(g.id)}`); } catch (x) {
        if (x.status !== 404) { err.textContent = x.message; err.hidden = false; btn.disabled = false; return; }
      }
      load();
    });
    return h("div", { "data-row": "bgrant:" + g.id },
      h("div", { class: "row" }, h("span", { class: "grow" },
        !agent && h("strong", { text: agentName(g.agent) + " " }),
        h("span", { text: (KIND_WORDS[g.kind] || g.kind).toLowerCase() + " on " }), h("span", { class: "mono", text: g.host }),
        h("span", { class: "sub", text: ` · ${g.profile} · until ${clock(g.expires_at)}` })), btn), err);
  }
  function draw() {
    fill(el, list && list.length > 0 && [h("h3", { text: "Allowed for an hour" }),
      h("div", { class: "rows" }, list.map(row))]);
    if (after) after(list || []);
  }
  async function load() {
    try { list = await api("GET", "/api/browser/grants" + (agent ? "?agent=" + enc(agent) : "")); } catch { /* keep */ }
    draw();
  }
  return { el, load };
}

// ---------- browser.yaml, as text ----------

function browserYamlEditor(onSaved) {
  let loaded = null;
  const ta = h("textarea", { class: "code", autocapitalize: "none", "aria-label": "browser.yaml",
    "data-field": "file:browser.yaml" });
  ta.setAttribute("spellcheck", "false");
  const msg = h("p", { class: "sub", hidden: true });
  const err = h("p", { class: "err", hidden: true, "data-err": "browser-yaml" });
  const save = h("button", { type: "button", class: "small primary", text: "Save file", onclick: async () => {
    save.disabled = true;
    msg.hidden = err.hidden = true;
    try {
      const r = await api("PUT", "/api/browser/yaml", { text: ta.value });
      loaded = ta.value;
      msg.textContent = `Saved (${String(r.commit).slice(0, 7)}). Agents apply its rules at their next action.`;
      msg.hidden = false;
      onSaved(r);
    } catch (x) { err.textContent = x.message; err.hidden = false; }
    save.disabled = false;
  } });
  const el = h("details", { class: "advanced browser-yaml" }, h("summary", { text: "Edit browser.yaml" }),
    h("p", { class: "sub", text: "The profiles and their site rules live in this file at the definitions repo's root. "
      + "Saved here, it's kept as written, comments included; saving a profile above rewrites it without them." }),
    h("div", { class: "card file" },
      h("div", { class: "row between", style: "margin-bottom: 8px" }, h("strong", { class: "mono", text: "browser.yaml" }), save),
      ta, err, msg));
  async function load() {
    try {
      const r = await api("GET", "/api/browser/yaml");
      if (ta.value === (loaded || "")) ta.value = r.text;      // never over the owner's edits
      loaded = r.text;
    } catch (x) { err.textContent = x.message; err.hidden = false; }
  }
  el.addEventListener("toggle", () => { if (el.open) load(); });
  return { el, load, open() { el.open = true; load(); } };
}

// ---------- #/browser ----------

function BrowserView() {
  let profiles = null, error = null, err = "", running = new Map(), newOpen = false, busy = false;
  const listBox = h("div"), newBox = h("div");
  const grants = grantsBox(null, (list) => { grantsCard.hidden = !list.length; });
  const grantsCard = h("div", { class: "card grants-card", hidden: true }, grants.el);
  const audit = auditBox(() => ({ agent: agentSel.value || null, profile: profileSel.value || null }));
  const yaml = browserYamlEditor(() => load());

  const agentSel = h("select", { "aria-label": "Whose actions", "data-field": "audit-agent", onchange: () => audit.load() });
  const profileSel = h("select", { "aria-label": "Which profile", "data-field": "audit-profile", onchange: () => audit.load() });

  const nameIn = h("input", { placeholder: "e.g. Personal logins", maxLength: 60, autocomplete: "off", "aria-label": "Title",
    "data-field": "profile-title", oninput: () => { if (!idEdited) idIn.value = slugify(nameIn.value); newErr.hidden = true; } });
  let idEdited = false;
  const idIn = h("input", { class: "mono", placeholder: "alice", maxLength: 31, autocapitalize: "none", autocomplete: "off",
    "aria-label": "Name", "data-field": "profile-name", oninput: () => { idEdited = true; newErr.hidden = true; } });
  idIn.setAttribute("spellcheck", "false");
  const newErr = h("p", { class: "err", hidden: true, "data-err": "profile-new" });

  function drawNew() {
    fill(newBox, newOpen && h("div", { class: "card newprofile" }, h("h3", { text: "New profile" }),
      h("p", { class: "sub", text: "A Chrome of its own, with its own logins: sign in to sites in its live view, and "
        + "every agent you give it uses them." }),
      h("div", { class: "field" }, h("label", { text: "Title" }), nameIn),
      h("div", { class: "field" }, h("label", { text: "Name" }), idIn,
        h("p", { class: "sub hint-line", text: "How agents and browser.yaml name it. " + PROFILE_RULE })),
      newErr,
      h("div", { class: "row end actions" },
        h("button", { type: "button", class: "small", text: "Cancel", onclick: () => { newOpen = false; drawNew(); } }),
        h("button", { type: "button", class: "small primary", text: "Create", disabled: busy, onclick: create }))));
  }

  async function create() {
    const name = idIn.value.trim();
    const show = (m) => { newErr.textContent = m; newErr.hidden = false; };
    if (!name) return show("Give it a name.");
    if (!PROFILE_RE.test(name)) return show(PROFILE_RULE);
    busy = true;
    drawNew();
    try {
      await api("POST", "/api/browser/profiles", { name, title: nameIn.value.trim() || null });
      location.hash = profileHash(name);
    } catch (x) { show(x.message); }
    busy = false;
    drawNew();
  }

  function card(p) {
    const r = running.get(p.name);
    const up = !!r || p.running;
    const facts = [up ? (r ? plural(r.tabs, "tab") : "running") : "not running",
      r && r.busy && "an agent is acting",
      p.used_by.length ? "used by " + andList(p.used_by.map(agentName)) : "no agent uses it",
      p.vault > 0 && plural(p.vault, "saved login"),
      Object.keys(p.sites || {}).length > 0 && plural(Object.keys(p.sites).length, "site rule")].filter(Boolean);
    const err = h("p", { class: "err", hidden: true });
    return h("div", { class: "card profile", "data-row": "profile:" + p.name },
      h("a", { href: profileHash(p.name), class: "row between", style: "color: inherit" },
        h("strong", { class: "grow title" }, h("span", { text: profileTitle(p) }),
          p.title && h("span", { class: "id", text: p.name })),
        up ? badge("running", "running") : badge("", "stopped")),
      p.note && h("div", { class: "sub note-line", text: brief(p.note, 160) }),
      h("div", { class: "meta sub", text: facts.join(" · ") }),
      h("div", { class: "row acts" },
        h("a", { class: "button small primary", href: liveHash(p.name), text: "Live view" }),
        up && h("button", { type: "button", class: "small", text: "Stop", onclick: (e) => stop(p.name, e.target, err) }),
        h("a", { class: "button small", href: profileHash(p.name), text: "Settings" })), err);
  }

  async function stop(name, btn, err) {
    btn.disabled = true;
    err.hidden = true;
    try { await api("POST", `/api/browser/profiles/${enc(name)}/stop`); } catch (x) {
      err.textContent = x.message; err.hidden = false; btn.disabled = false; return;
    }
    await load();
  }

  function drawList() {
    const list = profiles || [];
    fill(listBox, err && h("p", { class: "err", text: err }),
      error && h("div", { class: "note warn" }, h("strong", { text: "browser.yaml has an error, so there are no "
        + "profiles until it's fixed (Advanced, below). " }), h("span", { text: error })),
      !profiles && !err && h("p", { class: "sub", text: "Loading…" }),
      profiles && !list.length && !error && h("p", { class: "empty", text: "No profiles yet. Add one to give your "
        + "agents a browser." }),
      list.map(card));
    const keep = profileSel.value;
    fill(profileSel, h("option", { value: "", text: "Every profile" }),
      list.map((p) => h("option", { value: p.name, text: profileTitle(p) })));
    profileSel.value = list.some((p) => p.name === keep) ? keep : "";
  }

  function drawAgents() {
    const keep = agentSel.value;
    const names = [...S.agents.keys()].sort();
    fill(agentSel, h("option", { value: "", text: "Everyone" }), names.map((n) => h("option", { value: n, text: agentName(n) })));
    agentSel.value = names.includes(keep) ? keep : "";
  }

  async function load() {
    const [ps, rs] = await Promise.allSettled([api("GET", "/api/browser/profiles"), api("GET", "/api/browser/running")]);
    if (ps.status === "fulfilled") { profiles = ps.value.profiles; error = ps.value.error; err = ""; } else err = ps.reason.message;
    if (rs.status === "fulfilled") running = new Map(rs.value.map((r) => [r.profile, r]));
    drawList();
  }

  drawNew();
  drawList();
  drawAgents();
  load();
  grants.load();
  audit.load();
  const el = h("section", { class: "browser" },
    h("div", { class: "row between" }, h("h1", { text: "Browser" }),
      h("button", { type: "button", class: "small", text: "New profile", onclick: () => {
        newOpen = !newOpen;
        drawNew();
        if (newOpen) nameIn.focus();
      } })),
    h("p", { class: "sub intro", text: "Chrome with your logins, one per profile. Agents use it through Valet's "
      + "browser tools, and every action is checked against the site rules. You can watch and take over in the live "
      + "view." }),
    newBox, listBox,
    h("a", { class: "card linkcard row", href: "#/vault" },
      h("span", { class: "grow" }, h("strong", { text: "Vault" }),
        h("span", { class: "sub", text: "Saved logins the browser fills in for your agents, without them seeing" })),
      h("span", { class: "chev", text: "›" })),
    grantsCard,
    h("div", { class: "row between section-head" }, h("h2", { text: "Activity" }),
      h("button", { type: "button", class: "small", text: "Refresh", onclick: () => audit.load() })),
    h("div", { class: "row filters" }, agentSel, profileSel),
    audit.el,
    h("h2", { text: "Advanced" }), yaml.el);
  // No frames say a browser started or stopped (or an agent opened a tab): look again now and then.
  const stopPoll = every(15000, () => { if (!document.hidden) load(); }, el);
  return {
    el,
    close() { stopPoll(); },
    frame(f) {
      if (f.type === "hello") { load(); grants.load(); audit.load(); drawAgents(); }
      if (f.type === "agent") { drawAgents(); if (profiles) drawList(); }
      if (f.type === "inbox" && f.item.kind === "browser" && f.item.status !== "pending") { grants.load(); audit.load(); }
    },
  };
}

// ---------- a save bar: a screen's unsaved edits ----------
// Like Settings': Discard / Save while there are edits; leaving with edits (route() asks the view's leave(hash))
// offers Save then go, Discard & leave, or Stay. save() returns what to say once saved, or throws.

function saveBar({ isDirty, save, discard }) {
  let busy = false, flashMsg = "", flashTimer = 0, leaveTo = null;
  const msg = h("div", { class: "bar-msg" });
  const err = h("p", { class: "err", hidden: true, "data-err": "bar" });
  const actions = h("div", { class: "actions" });
  const el = h("div", { class: "savebar", hidden: true }, msg, err, actions);
  const go = (to) => { leaveTo = null; location.hash = to; };

  async function run() {
    if (busy) return false;
    busy = true;
    err.hidden = true;
    draw();
    let ok = false;
    try {
      const said = await save();
      ok = true;
      flash(said || "Saved.");
    } catch (x) { err.textContent = x.message; err.hidden = false; }
    busy = false;
    draw();
    return ok;
  }

  function draw() {
    const dirty = isDirty();
    if (leaveTo !== null && !dirty) leaveTo = null;
    msg.textContent = busy ? "Saving…" : leaveTo !== null ? "You have unsaved changes" : dirty ? "Unsaved changes" : flashMsg;
    msg.hidden = !msg.textContent;
    const b = (text, cls, onclick) => h("button", { type: "button", class: cls, text, disabled: busy, onclick });
    fill(actions, leaveTo !== null
      ? [b("Save", "primary", async () => { const to = leaveTo; if (await run()) go(to); }),
        b("Discard & leave", "danger", () => { const to = leaveTo; err.hidden = true; discard(); go(to); }),
        b("Stay", "", () => { leaveTo = null; draw(); })]
      : dirty && [b("Discard", "", () => { err.hidden = true; discard(); draw(); }), b("Save", "primary", run)]);
    el.hidden = !(dirty || busy || flashMsg || !err.hidden);
  }

  function flash(text) {
    flashMsg = text;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { flashMsg = ""; draw(); }, 3000);
  }

  return {
    el, draw, save: run,
    leave(to) {
      if (!isDirty()) return false;
      leaveTo = to;
      draw();
      return true;
    },
    close() { clearTimeout(flashTimer); },
  };
}

// ---------- #/browser/<profile> ----------

function ProfileView(name) {
  const path = "/api/browser/profiles/" + enc(name);
  let saved = null, gone = false, loadErr = "";
  let title = "", note = "", sites = {};
  let deleting = false, wipe = false;
  let tabs = null, tabsErr = "", logins = null, loginsErr = "";
  const head = h("div"), form = h("div"), tabsBox = h("div", { class: "card tabs-card" }), loginsBox = h("div");
  const deleteBox = h("div", { class: "danger-zone" });
  const editor = sitesEditor({ key: "site", empty: "No rules yet: for each action, an agent's own rules decide, "
    + "then its permission level.", onChange: (r) => { sites = r; bar.draw(); } });
  const titleIn = h("input", { maxLength: 60, autocomplete: "off", "aria-label": "Title", "data-field": "title",
    placeholder: name, oninput: () => { title = titleIn.value; bar.draw(); } });
  const noteIn = h("textarea", { class: "note-in", rows: 3, "aria-label": "Note", "data-field": "note",
    placeholder: "What it's for, which accounts it has.",
    oninput: () => { note = noteIn.value; grow(noteIn); bar.draw(); } });
  const urlIn = h("input", { class: "mono", placeholder: "github.com", inputmode: "url", autocapitalize: "none",
    autocomplete: "off", "aria-label": "Address to open", "data-field": "open-url",
    onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); openTab(); } } });
  urlIn.setAttribute("spellcheck", "false");
  const tabErr = h("p", { class: "err", hidden: true, "data-err": "open-tab" });
  const audit = auditBox(() => ({ profile: name }));

  const isDirty = () => !!saved && (title.trim() !== (saved.title || "") || note.trim() !== (saved.note || "")
    || JSON.stringify(sites) !== JSON.stringify(saved.sites || {}));
  const bar = saveBar({
    isDirty,
    async save() {
      const p = await api("PUT", path, { title: title.trim() || null, note: note.trim() || null, sites });
      saved = null;
      take(p);
      return "Saved. Agents apply its rules at their next action.";
    },
    discard() {
      const p = saved;
      saved = null;          // take() resets the form only when it has no edits
      take(p);
    },
  });

  function drawHead() {
    const p = saved;
    const facts = p && [p.used_by.length ? "Used by " + andList(p.used_by.map(agentName)) : "No agent uses it yet",
      plural(p.vault, "saved login")];
    fill(head, h("a", { class: "back", href: "#/browser", text: "‹ Browser" }),
      h("div", { class: "row between title-row" }, h("h1", { class: "grow" }, h("span", { text: p ? profileTitle(p) : name }),
        p && p.title && h("span", { class: "id", text: name })),
      p && (p.running ? badge("running", "running") : badge("", "stopped"))),
      facts && h("p", { class: "sub", text: facts.join(" · ") }),
      p && h("div", { class: "row acts head-acts" }, h("a", { class: "button small", href: liveHash(name), text: "Live view" })));
  }

  function drawForm() {
    if (gone) { fill(form, h("p", { class: "empty", text: `There's no browser profile named ${name}.` })); return; }
    if (!saved) { fill(form, loadErr ? h("p", { class: "err", text: loadErr }) : h("p", { class: "sub", text: "Loading…" })); return; }
    fill(form,
      h("div", { class: "card" }, h("div", { class: "field" }, h("label", { text: "Title" }), titleIn),
        h("div", { class: "field" }, h("label", { text: "Note" }), noteIn,
          h("p", { class: "sub hint-line", text: "Agents that use this profile see the note." }))),
      h("h2", { text: "Site rules" }),
      h("div", { class: "card rules-card" },
        h("p", { class: "sub intro", text: "What each kind of action does on a site, for every agent that uses this "
          + "profile. An agent's own rules (in its settings) beat these; with no rule, its permission level decides. "
          + "github.com covers its subdomains too; admin.shopify.com/store only that part of the site." }),
        editor.el));
    setTimeout(() => grow(noteIn), 0);
  }

  function take(p) {
    const keep = isDirty();
    saved = p;
    if (!keep) {
      title = titleIn.value = p.title || "";
      note = noteIn.value = p.note || "";
      sites = JSON.parse(JSON.stringify(p.sites || {}));
      editor.set(sites);
    }
    drawHead();
    drawForm();
    drawTabs();
    drawDelete();
    bar.draw();
  }

  // ---- its tabs ----
  function drawTabs() {
    const up = saved && saved.running;
    fill(tabsBox,
      tabsErr && h("p", { class: "err", text: tabsErr }),
      tabs === null && !tabsErr && h("p", { class: "sub", text: "Loading…" }),
      tabs && !tabs.length && h("p", { class: "sub", text: up ? "No tabs open." : "Its browser isn't running: it starts "
        + "when an agent or you use it." }),
      tabs && tabs.length > 0 && h("div", { class: "rows" }, tabs.map((t) => h("div", { class: "tabrow row", "data-row": "tab:" + t.tab },
        h("span", { class: "grow tabtext" }, h("span", { class: "name", text: t.title || "Untitled" }),
          h("span", { class: "mono sub url", text: t.url })),
        h("a", { class: "button small", href: liveHash(name, t.tab), text: "View" })))),
      h("div", { class: "field open-tab" }, h("label", { text: "Open a tab" }),
        h("div", { class: "row addrow" }, h("div", { class: "grow" }, urlIn),
          h("button", { type: "button", class: "small", text: "Open", onclick: openTab })), tabErr),
      up && h("div", { class: "row acts" }, h("button", { type: "button", class: "small", text: "Stop its browser",
        onclick: stop }), h("span", { class: "sub", text: "Its tabs close; its logins stay." })));
  }

  async function openTab() {
    tabErr.hidden = true;
    const url = urlIn.value.trim();
    try {
      const t = await api("POST", path + "/tabs", url ? { url } : {});
      urlIn.value = "";
      location.hash = liveHash(name, t.tab);
    } catch (x) { tabErr.textContent = x.message; tabErr.hidden = false; }
  }

  async function stop() {
    tabErr.hidden = true;
    try { await api("POST", path + "/stop"); } catch (x) { tabErr.textContent = x.message; tabErr.hidden = false; }
    await loadProfile();
    loadTabs();
  }

  // ---- its saved logins ----
  function drawLogins() {
    fill(loginsBox, loginsErr && h("p", { class: "err", text: loginsErr }),
      !logins && !loginsErr && h("p", { class: "sub", text: "Loading…" }),
      logins && h("div", { class: "card" },
        logins.length ? h("div", { class: "rows" }, logins.map(loginRow))
          : h("p", { class: "sub", text: "None yet. With a saved login, agents sign in with login() and never see "
            + "the password." }),
        h("div", { class: "row acts" }, h("button", { type: "button", class: "small", text: "New login", onclick: () => {
          BROWSER.newLogin = name;
          location.hash = "#/vault";
        } }))));
  }

  // ---- deleting it ----
  function drawDelete() {
    if (!saved) { fill(deleteBox); return; }
    const err = h("p", { class: "err", hidden: true, "data-err": "delete" });
    const box = h("input", { type: "checkbox", checked: wipe, "data-field": "wipe", onchange: (e) => { wipe = e.target.checked; } });
    const first = [saved.used_by.length > 0 && `take it from ${andList(saved.used_by.map(agentName))}'s settings`,
      saved.vault > 0 && `delete its ${plural(saved.vault, "saved login")}`].filter(Boolean);
    fill(deleteBox, !deleting ? h("button", { type: "button", class: "small danger", text: "Delete profile",
      onclick: () => { deleting = true; wipe = false; drawDelete(); } })
      : h("div", { class: "confirm" },
        h("div", { class: "q", text: `Delete ${profileTitle(saved)}? It leaves browser.yaml (the history keeps it) and `
          + "its browser stops." }),
        first.length > 0 && h("p", { class: "warn-text", text: "First " + andList(first) + ": Valet won't delete a "
          + "profile that's in use." }),
        h("label", { class: "check wipe" }, box, h("span", { text: "Also wipe its logins: its cookies and sessions, so "
          + "every site it's signed in to signs it out. Without this they stay on disk, and a new profile with this "
          + "name gets them back." })),
        err,
        h("div", { class: "row" },
          h("button", { type: "button", class: "small", text: "Cancel", onclick: () => { deleting = false; drawDelete(); } }),
          h("button", { type: "button", class: "small danger", text: wipe ? "Delete and wipe" : "Delete", onclick: async () => {
            err.hidden = true;
            try {
              await api("DELETE", path + "?wipe=" + (wipe ? "true" : "false"));
              saved = null;
              location.hash = "#/browser";
            } catch (x) { err.textContent = x.message; err.hidden = false; }
          } }))));
    box.addEventListener("change", () => drawDelete());
  }

  async function loadProfile() {
    try { take(await api("GET", path)); loadErr = ""; } catch (x) {
      if (x.status === 404) { gone = true; saved = null; drawHead(); drawForm(); drawDelete(); bar.draw(); return; }
      loadErr = x.message;
      drawForm();
    }
  }

  async function loadTabs() {
    try { tabs = await api("GET", path + "/tabs"); tabsErr = ""; } catch (x) { if (x.status !== 404) tabsErr = x.message; }
    drawTabs();
  }

  async function loadLogins() {
    try { logins = await api("GET", "/api/vault?profile=" + enc(name)); loginsErr = ""; } catch (x) { loginsErr = x.message; }
    drawLogins();
  }

  drawHead();
  drawForm();
  drawTabs();
  drawLogins();
  loadProfile();
  loadTabs();
  loadLogins();
  audit.load();
  const el = h("section", { class: "browser-profile" }, head, form,
    h("h2", { text: "Tabs" }), tabsBox,
    h("h2", { text: "Saved logins" }), loginsBox,
    h("div", { class: "row between section-head" }, h("h2", { text: "Activity" }),
      h("button", { type: "button", class: "small", text: "Refresh", onclick: () => audit.load() })),
    audit.el, deleteBox, bar.el);
  const stopPoll = every(10000, () => {
    if (document.hidden || gone) return;
    loadTabs();
    if (!isDirty()) loadProfile();
  }, el);
  return {
    el,
    close() { stopPoll(); bar.close(); },
    leave: bar.leave,
    frame(f) {
      if (f.type === "hello") { loadProfile(); loadTabs(); loadLogins(); audit.load(); }
      if (f.type === "agent" && saved) drawHead();
      if (f.type === "inbox" && f.item.kind === "browser" && f.item.status !== "pending") audit.load();
    },
  };
}

// A saved login as a row: its name, what it is, a link to it (the vault's screen and a profile's).
function loginRow(v) {
  const facts = [v.label, v.username, v.site].filter(Boolean).join(" · ");
  return h("a", { class: "vault-row row", href: "#/vault/" + vaultPath(v.name), "data-row": "login:" + v.name },
    h("span", { class: "grow" }, h("span", { class: "name mono", text: v.name }),
      h("span", { class: "sub desc", text: facts })),
    !v.has_password && badge("pending", "no password"), v.has_totp && badge("", "2FA"),
    h("span", { class: "chev", text: "›" }));
}

// ---------- the live view: #/browser/<profile>/live[/<tab>] ----------
// One socket, /ws/browser/<profile>?tab=: JPEG frames drawn on a canvas (the newest wins while one decodes), and
// the owner's input as JSON. x and y are fractions of the frame. Touch: a tap is a click; at Fit a swipe scrolls
// the page, zoomed in a swipe moves the view (the ▲ ▼ buttons scroll the page). Mouse: down, move, up (drags,
// double clicks) and the wheel. Keys typed on the frame go as text or as keys; a phone types in the text field.

const LIVE_KEYS = [["Enter", "⏎"], ["Backspace", "⌫"], ["Tab", "⇥"], ["Escape", "Esc"], ["ArrowLeft", "←"],
  ["ArrowUp", "↑"], ["ArrowDown", "↓"], ["ArrowRight", "→"]];
const LIVE_KEY_NAMES = new Set(["Enter", "Backspace", "Tab", "Escape", "ArrowLeft", "ArrowUp", "ArrowRight",
  "ArrowDown", "Delete", "Home", "End", "PageUp", "PageDown"]);
const LIVE_ZOOMS = [1, 2, 3];
const LIVE_INPUT = { tap: "tap", mouse: "click", wheel: "scroll", text: "typing", key: "key", nav: "address",
  back: "Back", reload: "Reload", open: "new tab" };

function LiveView(profile, tabId) {
  let ws = null, closed = false, connected = false, retry = 0, retryTimer = 0, fatal = "", noteText = "", noteTimer = 0;
  let tabs = [], tab = tabId, page = null, busy = false, frames = 0, asksOpen = false, asksSeen = null;
  let fw = 0, fh = 0, zoom = 1, decoding = false, nextFrame = null;
  let textBuf = "", textTimer = 0, wheelDy = 0, wheelAt = null, wheelTimer = 0, moveAt = null, moveTimer = 0;
  let mouseDown = false, touch = null, lastClick = { t: 0, x: 0, y: 0, n: 0 };

  const canvas = h("canvas", { class: "lv-canvas", tabindex: "0", hidden: true,
    "aria-label": "The page: click or tap it, and type" });
  const waiting = h("p", { class: "lv-wait", text: "Starting the browser…" });
  const screen = h("div", { class: "lv-screen fit" }, canvas, waiting);
  const address = h("input", { class: "mono lv-address", placeholder: "Type an address", inputmode: "url",
    autocapitalize: "none", autocomplete: "off", enterKeyHint: "go", "aria-label": "Address", "data-field": "live-address" });
  address.setAttribute("spellcheck", "false");
  const addressForm = h("form", { class: "lv-go grow" }, address, h("button", { type: "submit", class: "small", text: "Go" }));
  addressForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const url = address.value.trim();
    if (url) { send({ type: "nav", url }); address.blur(); }
  });
  const tabSel = h("select", { class: "lv-tabs", "aria-label": "Tab", "data-field": "live-tab", onchange: () => {
    if (tabSel.value && tabSel.value !== tab) send({ type: "tab", tab: tabSel.value });
  } });
  const zoomLabel = h("span", { class: "lv-zoom", text: "Fit" });
  const status = h("div", { class: "lv-status", "data-row": "live-status" });
  const asksBox = h("div", { class: "lv-asks" });
  const textIn = h("input", { placeholder: "Type into the page", autocomplete: "off", enterKeyHint: "send",
    "aria-label": "Text to type into the page", "data-field": "live-text" });
  textIn.setAttribute("autocorrect", "off");
  const textForm = h("form", { class: "lv-type" }, textIn, h("button", { type: "submit", class: "small primary", text: "Type" }));
  textForm.addEventListener("submit", (e) => {
    e.preventDefault();
    if (textIn.value) { send({ type: "text", text: textIn.value.slice(0, 2000) }); textIn.value = ""; }
  });
  const btn = (text, label, onclick, cls = "small") => h("button", { type: "button", class: cls, text, title: label,
    "aria-label": label, onclick });
  const keys = h("div", { class: "lv-keys" },
    LIVE_KEYS.map(([k, sym]) => btn(sym, k.replace("Arrow", "") + " key", () => send({ type: "key", key: k }), "small key")),
    btn("▲", "Scroll up", () => scrollBy(-1), "small key"), btn("▼", "Scroll down", () => scrollBy(1), "small key"));

  const el = h("section", { class: "lv" },
    h("div", { class: "lv-top" }, h("a", { class: "button small lv-close", href: profileHash(profile), text: "‹",
      title: "Close the live view", "aria-label": "Close the live view" }), addressForm),
    h("div", { class: "lv-bar" },
      btn("◀", "Back", () => send({ type: "back" })), btn("⟳", "Reload", () => send({ type: "reload" })),
      tabSel, btn("+ Tab", "New tab", () => { send({ type: "open" }); address.value = ""; address.focus(); }),
      h("span", { class: "lv-zoombox" }, btn("−", "Zoom out", () => setZoom(-1)), zoomLabel,
        btn("+", "Zoom in", () => setZoom(1)))),
    status, asksBox, screen, h("div", { class: "lv-input" }, textForm, keys));

  // ---- the socket ----
  function connect() {
    if (closed) return;
    clearTimeout(retryTimer);
    fatal = "";
    const sock = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host
      + `/ws/browser/${enc(profile)}` + (tab ? "?tab=" + enc(tab) : ""));
    ws = sock;
    sock.binaryType = "blob";
    drawStatus();
    sock.onopen = () => { if (ws === sock) { connected = true; retry = 0; drawStatus(); } };
    sock.onmessage = (m) => {
      if (ws !== sock) return;
      if (typeof m.data === "string") onMessage(m.data); else showFrame(m.data);
    };
    sock.onclose = (ev) => {
      if (ws !== sock) return;
      ws = null;
      connected = false;
      onClosed(ev.code);
    };
  }

  function disconnect() {
    const sock = ws;
    ws = null;
    connected = false;
    if (sock) { sock.onclose = null; sock.onmessage = null; try { sock.close(); } catch { /* gone */ } }
  }

  function onMessage(raw) {
    let m;
    try { m = JSON.parse(raw); } catch { return; }
    if (m.type === "tabs") {
      tabs = m.tabs || [];
      tab = m.tab || null;
      drawTabs();
      remember();
    } else if (m.type === "page") {
      page = m;
      if (document.activeElement !== address) address.value = m.url === "about:blank" ? "" : m.url || "";
      drawStatus();
    } else if (m.type === "busy") {
      busy = !!m.busy;
      if (m.dropped) note(`Your ${LIVE_INPUT[m.dropped] || "input"} was dropped: an agent held the browser for more `
        + "than 5 seconds. Try again.");
      drawStatus();
    } else if (m.type === "error") {
      if (m.fatal === false) note(m.error);
      else { fatal = m.error === "the browser stopped" ? "The browser stopped." : m.error; drawStatus(); }
    }
  }

  function onClosed(code) {
    if (closed) return;
    if (code === 4401) {
      api("GET", "/api/me").then((me) => { if (!me.owner) signedOut(); else later(); }, later);
      return;
    }
    if (code === 4403) fatal = "Valet refused the live view from this address: open Valet at its VALET_PUBLIC_URL.";
    else if (code === 4404) fatal = fatal || `There's no browser profile named ${profile}.`;
    else if (code === 1011) fatal = fatal || "The browser didn't start.";
    if (fatal) { drawStatus(); return; }
    later();
  }

  // Dial again after a dropped link, backing off; not while the page is hidden (it dials when it's back).
  function later() {
    if (closed || document.hidden) { drawStatus(); return; }
    const wait = Math.min(1000 * 2 ** retry, 15000);
    retry++;
    retryTimer = setTimeout(connect, wait);
    drawStatus();
  }

  function rawSend(m) {
    if (!ws || ws.readyState !== 1) return false;
    ws.send(JSON.stringify(m));
    return true;
  }

  // Text typed on the frame goes in batches; anything else first sends the text before it, keeping the order.
  function flushText() {
    clearTimeout(textTimer);
    textTimer = 0;
    if (textBuf) { rawSend({ type: "text", text: textBuf }); textBuf = ""; }
  }

  function send(m) {
    if (m.type !== "text") flushText();
    if (!rawSend(m)) note(fatal ? "Not connected." : "Not connected yet: try again in a moment.");
  }

  function typeChar(ch) {
    textBuf += ch;
    if (textBuf.length >= 200) flushText();
    else if (!textTimer) textTimer = setTimeout(flushText, 60);
  }

  // ---- what it shows ----
  function note(text) {
    noteText = text;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => { noteText = ""; drawStatus(); }, 6000);
    drawStatus();
  }

  function drawStatus() {
    const reconnect = h("button", { type: "button", class: "small", text: fatal === "The browser stopped."
      ? "Start it again" : "Reconnect", onclick: () => { retry = 0; connect(); } });
    // What it's doing, a note for a few seconds over that (a dropped input...), or what stopped it.
    const line = fatal || noteText || (!ws ? (closed ? "" : "Reconnecting…") : !connected ? "Connecting…"
      : busy ? "An agent is using the browser: what you do here waits for it to finish." : (page && page.title) || "");
    status.className = "lv-status" + (fatal ? " bad" : noteText ? " note-line" : busy ? " busy" : "");
    const again = fatal || (!ws && !closed);
    fill(status, h("span", { class: "grow", text: line }), again && reconnect);
    status.hidden = !line && !again;
    if (fatal) waiting.textContent = fatal;
  }

  function drawTabs() {
    fill(tabSel, tabs.length ? tabs.map((t) => h("option", { value: t.tab,
      text: brief(t.title || (t.url === "about:blank" ? "New tab" : t.url), 48) }))
      : h("option", { value: "", text: "No tabs" }));
    tabSel.value = tab || "";
    tabSel.disabled = tabs.length < 2;
    drawAsks();
  }

  // The address keeps the tab it shows, so a reload comes back to it (replaceState: no new screen).
  function remember() {
    if (!tab || tab === tabId || closed) return;
    tabId = tab;
    history.replaceState(history.state, "", liveHash(profile, tab));
    S.hash = location.hash;
  }

  async function decode(blob) {
    if (typeof createImageBitmap === "function") return createImageBitmap(blob);
    const url = URL.createObjectURL(blob);
    try {
      const img = new Image();
      img.src = url;
      await img.decode();
      return img;
    } finally { URL.revokeObjectURL(url); }
  }

  function showFrame(blob) {
    if (decoding) { nextFrame = blob; return; }
    decoding = true;
    decode(blob).then((img) => {
      if (closed) return;
      const w = img.width, ht = img.height;
      if (w !== fw || ht !== fh) { canvas.width = fw = w; canvas.height = fh = ht; layout(); }
      const c = canvas.getContext && canvas.getContext("2d");
      if (c) c.drawImage(img, 0, 0);
      if (img.close) img.close();
      if (!frames++) { waiting.hidden = true; canvas.hidden = false; layout(); }
    }).catch(() => { /* a frame that doesn't decode: the next one will */ }).finally(() => {
      decoding = false;
      if (nextFrame && !closed) { const b = nextFrame; nextFrame = null; showFrame(b); }
    });
  }

  // The frame's size on screen: Fit shows all of it; zoomed is that many times the screen's width (it scrolls).
  function layout() {
    if (!fw || !fh) return;
    const bw = Math.max(screen.clientWidth - 4, 50), bh = Math.max(screen.clientHeight - 4, 50);
    const fit = Math.min(bw / fw, bh / fh);
    const scale = zoom === 1 ? fit : Math.max(fit, bw / fw) * zoom;
    const oldW = parseFloat(canvas.style.width) || fw * scale;
    const cx = (screen.scrollLeft + bw / 2) / oldW, cy = (screen.scrollTop + bh / 2) / (oldW * fh / fw);
    canvas.style.width = Math.round(fw * scale) + "px";
    canvas.style.height = Math.round(fh * scale) + "px";
    screen.className = "lv-screen " + (zoom === 1 ? "fit" : "zoomed");
    zoomLabel.textContent = zoom === 1 ? "Fit" : zoom + "×";
    screen.scrollLeft = Math.max(0, cx * fw * scale - bw / 2);      // keep what was in the middle there
    screen.scrollTop = Math.max(0, cy * fh * scale - bh / 2);
  }

  function setZoom(step) {
    const i = Math.max(0, Math.min(LIVE_ZOOMS.length - 1, LIVE_ZOOMS.indexOf(zoom) + step));
    zoom = LIVE_ZOOMS[i];
    layout();
  }

  // ---- input ----
  const clamp = (v) => Math.min(1, Math.max(0, Number.isFinite(v) ? v : 0.5));
  function at(e) {
    const r = canvas.getBoundingClientRect();
    return { x: clamp((e.clientX - r.left) / r.width), y: clamp((e.clientY - r.top) / r.height) };
  }

  function wheel(p, dy) {
    wheelDy += dy;
    wheelAt = p;
    if (!wheelTimer) wheelTimer = setTimeout(flushWheel, 50);
  }

  function flushWheel() {
    wheelTimer = 0;
    const dy = Math.max(-5000, Math.min(5000, Math.round(wheelDy)));
    wheelDy = 0;
    if (dy && wheelAt) send({ type: "wheel", x: wheelAt.x, y: wheelAt.y, dy });
  }

  function scrollBy(dir) { send({ type: "wheel", x: 0.5, y: 0.5, dy: dir * Math.round((fh || 900) * 0.7) }); }

  function flushMove() {
    clearTimeout(moveTimer);
    moveTimer = 0;
    if (moveAt) { send({ type: "mouse", action: "move", x: moveAt.x, y: moveAt.y }); moveAt = null; }
  }

  canvas.addEventListener("pointerdown", (e) => {
    if (!fw) return;
    if (e.pointerType === "mouse") {
      if (e.button !== 0) return;
      e.preventDefault();
      canvas.focus();
      const near = e.timeStamp - lastClick.t < 450 && Math.abs(e.clientX - lastClick.x) < 6 && Math.abs(e.clientY - lastClick.y) < 6;
      lastClick = { t: e.timeStamp, x: e.clientX, y: e.clientY, n: near ? Math.min(lastClick.n + 1, 3) : 1 };
      mouseDown = true;
      if (canvas.setPointerCapture) canvas.setPointerCapture(e.pointerId);
      moveAt = null;
      const p = at(e);
      send({ type: "mouse", action: "down", x: p.x, y: p.y, clicks: lastClick.n });
      return;
    }
    touch = { id: e.pointerId, x: e.clientX, y: e.clientY, lastY: e.clientY, moved: false, at: at(e) };
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!fw) return;
    if (e.pointerType === "mouse") {
      moveAt = at(e);
      if (!moveTimer) moveTimer = setTimeout(flushMove, mouseDown ? 40 : 120);
      return;
    }
    if (!touch || e.pointerId !== touch.id) return;
    if (!touch.moved && Math.hypot(e.clientX - touch.x, e.clientY - touch.y) > 10) touch.moved = true;
    if (touch.moved && zoom === 1) {            // at Fit, a swipe scrolls the page
      const r = canvas.getBoundingClientRect();
      wheel(at(e), (touch.lastY - e.clientY) * (fw / r.width));
      touch.lastY = e.clientY;
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    if (e.pointerType === "mouse") {
      if (!mouseDown) return;
      mouseDown = false;
      flushMove();
      const p = at(e);
      send({ type: "mouse", action: "up", x: p.x, y: p.y, clicks: lastClick.n });
      return;
    }
    if (touch && e.pointerId === touch.id && !touch.moved) send({ type: "tap", x: touch.at.x, y: touch.at.y });
    touch = null;
  });
  canvas.addEventListener("pointercancel", () => { touch = null; mouseDown = false; });   // the view scrolled instead
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("wheel", (e) => {
    if (!fw) return;
    e.preventDefault();
    wheel(at(e), e.deltaY * (e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 800 : 1));
  }, { passive: false });
  canvas.addEventListener("keydown", (e) => {
    if (e.isComposing || e.metaKey || e.ctrlKey || e.altKey) return;    // the browser's own shortcuts, and paste
    if (LIVE_KEY_NAMES.has(e.key)) { e.preventDefault(); send({ type: "key", key: e.key }); return; }
    if (e.key.length === 1) { e.preventDefault(); typeChar(e.key); }
  });
  canvas.addEventListener("paste", (e) => {
    const text = e.clipboardData && e.clipboardData.getData("text");
    if (text) { e.preventDefault(); send({ type: "text", text: text.slice(0, 2000) }); }
  });

  // ---- what agents ask about this profile, answered here ----
  // A bar that says how many wait (folded, so the page keeps the room), unfolding to the cards; a new one unfolds it.
  function drawAsks() {
    const items = [...S.inbox.values()].filter((i) => i.status === "pending" && i.kind === "browser" && i.browser
      && i.browser.profile === profile).sort((a, b) => a.created_at - b.created_at);
    const ids = items.map((i) => i.id);
    if (asksSeen && ids.some((id) => !asksSeen.has(id))) asksOpen = true;
    asksSeen = new Set(ids);
    if (!items.length) { fill(asksBox); return; }
    const first = items[0];
    const lead = items.length > 1 ? `${items.length} asks wait on you`
      : `${agentName(first.agent)}: ${first.browser.kind === "human" ? "take over" : first.browser.summary || first.title}`;
    fill(asksBox, h("div", { class: "lv-asks-bar row", "data-row": "live-asks" },
      h("span", { class: "grow", text: lead }),
      h("button", { type: "button", class: "small", text: asksOpen ? "Hide" : "Show", "aria-expanded": String(asksOpen),
        onclick: () => { asksOpen = !asksOpen; drawAsks(); layout(); } })),
    asksOpen && h("div", { class: "lv-asks-list" }, items.map((i) => {
      const other = i.browser.tab && i.browser.tab !== tab && tabs.some((t) => t.tab === i.browser.tab);
      return browserAskCard({ id: i.id, title: i.title, toolCall: i.tool_call || {}, options: i.options || [],
        status: i.status, outcome: i.answer, kind: i.kind, agent: i.agent, browser: i.browser },
      { cls: "perm lv-ask", live: true, head: other && h("button", { type: "button", class: "small lv-switch",
        text: "Show its tab", onclick: () => send({ type: "tab", tab: i.browser.tab }) }) });
    })));
  }

  const onVisible = () => {
    if (closed) return;
    if (document.hidden) { disconnect(); drawStatus(); return; }     // let an unwatched browser idle
    if (!ws && !fatal) { retry = 0; connect(); }
  };
  // The frame's room changes with the window, and with the asks bar folding: lay it out again.
  const onResize = () => layout();
  const sizes = typeof ResizeObserver === "function" ? new ResizeObserver(onResize) : null;
  if (sizes) sizes.observe(screen);
  document.addEventListener("visibilitychange", onVisible);
  window.addEventListener("resize", onResize);

  drawTabs();
  drawAsks();
  connect();
  document.body.classList.add("lv-open");
  return {
    el,
    close() {
      closed = true;
      document.body.classList.remove("lv-open");
      for (const t of [retryTimer, noteTimer, textTimer, wheelTimer, moveTimer]) clearTimeout(t);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("resize", onResize);
      if (sizes) sizes.disconnect();
      disconnect();
    },
    frame(f) {
      if (f.type === "inbox" || f.type === "hello") drawAsks();
    },
  };
}

// ---------- Settings › Browser ----------
// The profiles it uses (browser.profiles) and its own site rules (browser.rules), saved with the rest of the page.
// valet.browser, the tools that use them, follows: on with the first profile, off with the last.

function browserSection(ctx) {
  const el = h("div", { class: "card browser-sec" });
  let info = null, err = "", scoped = false;
  let always = 0;       // until when an "Always allow" answer's rule is still looked for (SCOPE_WAIT)
  const editor = sitesEditor({ key: "agent-site", empty: "None: the profiles' rules apply, then its permission level.",
    onChange: (rules) => { browserOf(ctx.draft).rules = rules; ctx.changed("browser.rules"); } });
  const grants = grantsBox(ctx.name);

  function browserOf(d) {
    if (!d.browser) d.browser = { profiles: [], rules: {} };
    if (!d.browser.profiles) d.browser.profiles = [];
    if (!d.browser.rules) d.browser.rules = {};
    return d.browser;
  }
  const mcpOf = (d) => (d && d.tools && d.tools.mcp) || [];
  const savedProfiles = () => ((ctx.saved && ctx.saved.browser && ctx.saved.browser.profiles) || []);

  // valet.browser on or off in tools.mcp, back in its saved place: off and on again is no change.
  function setServer(on) {
    const d = ctx.draft;
    if (!d.tools) d.tools = { mcp: [], skills: [] };
    const savedMcp = mcpOf(ctx.saved);
    const rank = (x) => (savedMcp.includes(x) ? savedMcp.indexOf(x) : savedMcp.length);
    const rest = mcpOf(d).filter((x) => x !== BROWSER_SERVER);
    d.tools.mcp = on ? [...rest, BROWSER_SERVER].sort((x, y) => rank(x) - rank(y)) : rest;
    ctx.changed("tools.mcp");
    ctx.redraw();
  }

  function toggle(name, on) {
    const d = ctx.draft, b = browserOf(d);
    const before = savedProfiles();
    const rank = (x) => (before.includes(x) ? before.indexOf(x) : before.length);
    const rest = b.profiles.filter((x) => x !== name);
    b.profiles = on ? [...rest, name].sort((x, y) => rank(x) - rank(y)) : rest;
    ctx.changed("browser.profiles");
    const has = mcpOf(d).includes(BROWSER_SERVER);
    if (b.profiles.length && !has) setServer(true);
    else if (!b.profiles.length && has) setServer(false);
    else draw();
  }

  function profileRow(name, p, on) {
    const sw = h("input", { type: "checkbox", class: "switch", checked: on, "aria-label": p ? profileTitle(p) : name,
      "data-field": "browser-profile:" + name, onchange: (e) => toggle(name, e.target.checked) });
    sw.setAttribute("role", "switch");
    const others = p ? p.used_by.filter((x) => x !== ctx.name).map(agentName) : [];
    const n = p ? Object.keys(p.sites || {}).length : 0;
    const facts = p && [n ? plural(n, "site rule") : "no site rules", p.vault ? plural(p.vault, "saved login") : "no saved logins",
      others.length > 0 && "also used by " + andList(others)].filter(Boolean).join(" · ");
    return h("div", { class: "server", "data-row": "browser-profile:" + name },
      h("label", { class: "check" }, h("span", { class: "grow" },
        h("span", { class: "name" }, p ? profileTitle(p) : name, p && p.title && h("span", { class: "id", text: name })),
        p && p.note && h("span", { class: "sub desc", text: brief(p.note, 160) }),
        facts && h("span", { class: "sub desc", text: facts }),
        !p && info && h("span", { class: "warn-text desc", text: "browser.yaml has no profile by this name: it can't be "
          + "saved or started with it. Switch it off, or add the profile." })), sw));
  }

  function draw() {
    const d = ctx.draft;
    if (!d) return;
    const b = d.browser || { profiles: [], rules: {} };
    const picked = b.profiles || [];
    const on = mcpOf(d).includes(BROWSER_SERVER);
    const list = info ? info.profiles : [];
    const names = [...list.map((p) => p.name), ...picked.filter((n) => !list.some((p) => p.name === n))];
    editor.set(b.rules || {});
    fill(el,
      h("p", { class: "sub intro", text: "Chrome with your logins, in profiles. It acts through Valet's browser tools, "
        + "and each action is checked against the site rules: its own below, then the profile's, then its permission "
        + "level. Rules apply at its next action, without a restart." }),
      err && h("p", { class: "err", text: err }),
      info && info.error && h("div", { class: "note warn" }, h("strong", { text: "browser.yaml has an error, so there "
        + "are no profiles until it's fixed. " }), h("span", { text: info.error })),
      !info && !err && h("p", { class: "sub", text: "Loading…" }),
      info && !names.length && !info.error && h("p", { class: "sub", text: "No browser profiles yet." }),
      names.length > 0 && [h("h3", { text: "Profiles it uses" }),
        h("div", { class: "rows" }, names.map((n) => profileRow(n, list.find((p) => p.name === n), picked.includes(n))))],
      picked.length > 0 && !on && h("div", { class: "note warn", "data-row": "browser-off" },
        "Its browser tools (valet.browser, in Tools) are off, so it can't use these. ",
        h("button", { type: "button", class: "small", text: "Turn them on", onclick: () => setServer(true) })),
      on && !picked.length && h("p", { class: "note", "data-row": "browser-none", text: "It has the browser tools but no "
        + "profile: pick one, or it can't open anything." }),
      ctx.errFor("browser.profiles"),
      h("h3", { class: "rules-head", text: "Its own site rules" }),
      h("p", { class: "sub", text: "They beat the profiles' rules, for this agent only." }),
      editor.el, ctx.errFor("browser.rules"),
      grants.el,
      h("a", { class: "more-link", href: "#/browser", text: "Browser profiles and activity ›" }));
  }

  async function load() {
    try { info = await api("GET", "/api/browser/profiles"); err = ""; } catch (x) { err = x.message; }
    draw();
  }

  // An "Always allow" answered elsewhere wrote a rule into agent.yaml: take it into the form, so a save from here
  // doesn't take it back. The form's own edits win where it has changed a site's rule. Resolves true if the rules
  // had moved (and were taken).
  async function syncRules() {
    let detail;
    try { detail = await api("GET", ctx.base); } catch { return false; }
    const d = detail.definition, saved = ctx.saved, draft = ctx.draft;
    if (!d || !saved || !draft) return false;
    const theirs = (d.browser && d.browser.rules) || {}, before = (saved.browser && saved.browser.rules) || {};
    if (JSON.stringify(theirs) === JSON.stringify(before)) return false;
    const mine = browserOf(draft).rules;
    for (const site of new Set([...Object.keys(theirs), ...Object.keys(before)])) {
      if (JSON.stringify(mine[site]) !== JSON.stringify(before[site])) continue;    // the form changed this one
      if (theirs[site]) mine[site] = { ...theirs[site] }; else delete mine[site];
    }
    browserOf(saved).rules = JSON.parse(JSON.stringify(theirs));
    draw();
    ctx.changed(null);
    return true;
  }

  load();
  grants.load();
  return {
    el, draw,
    changed(key) { if (key === "tools.mcp") draw(); },
    frame(f) {
      if (f.type === "hello") { load(); grants.load(); }
      const it = f.type === "inbox" && f.item;
      if (it && it.agent === ctx.name && it.kind === "browser" && it.answer
        && (it.answer.optionId === "allow_hour" || it.answer.optionId === "allow_always")) {
        scoped = true;
        if (it.answer.optionId === "allow_always") always = Date.now() + SCOPE_WAIT;
      }
      // The answer's own agent frame can come before the rule is written: for an "Always allow", each agent frame
      // looks again until the rules move (or SCOPE_WAIT, app.js, runs out).
      if (f.type === "agent" && f.agent.name === ctx.name && (scoped || always > Date.now())) {
        scoped = false;
        grants.load();
        syncRules().then((moved) => { if (moved) always = 0; });
      }
    },
  };
}

// ---------- a browser ask, in the inbox and the session ----------
// p: as permissionEl's, with p.browser (BrowserAsk). What the agent wants to do, on which site, the screenshot it
// asked with, and every option the item carries, in order. request_human (kind human) asks the owner to take over a
// tab: the live view, then Done or I can't. live: drawn in the live view itself (no screenshot, no link to it).

function browserAskCard(p, { head, cls, result, live }) {
  const b = p.browser || {};
  const d = b.detail || {};
  const human = b.kind === "human";
  const pending = p.status === "pending";
  const who = (p.agent && agentName(p.agent)) || "The agent";
  const chosen = p.outcome && p.outcome.optionId;
  const words = human && p.status === "answered" && chosen === "done" ? "Done: you took over"
    : human && p.status === "answered" && chosen === "cant" ? "You said you couldn't" : result;
  const el = d.element;
  const facts = [
    d.text != null && h("div", { class: "typed-box" }, h("div", { class: "sub", text: "What it would type"
      + (d.chars > 500 ? ` (the first 500 of ${d.chars} characters)` : "") + (d.submit ? ", then submit" : "") + ":" }),
    h("pre", { class: "typed", text: d.text })),
    Array.isArray(d.values) && h("div", { class: "sub", text: "Choosing " + d.values.join(", ") }),
    d.entry && h("div", { class: "sub", text: `With the saved login ${d.entry}` + (d.site ? ` (for ${d.site})` : "") }),
    el && (el.name || el.tag) && h("div", { class: "sub", text: "On " + [el.tag, el.type && `(${el.type})`].filter(Boolean)
      .join(" ") + (el.name ? ` "${el.name}"` : "") })];
  return h("div", { class: (cls || "perm") + " browserask", "data-row": "ask:" + p.id }, head,
    human ? h("div", { class: "title" }, `${who} needs you to take over`, d.reason && h("span", { class: "reason",
      text: ": " + d.reason }))
      : h("div", { class: "title" }, who + " wants to ", h("span", { class: "tool-name", text: b.summary || "use the browser" })),
    h("div", { class: "sub site" }, h("span", { class: "mono", text: b.host || "" }),
      ` · ${KIND_WORDS[b.kind] || b.kind || "browser"} · profile ${b.profile || "?"}`),
    !live && b.url && b.url !== "about:blank" && h("div", { class: "mono url", text: clip(b.url, 300) }),
    facts,
    !live && b.shot && h("a", { class: "shot-link", href: shotUrl(b.shot), target: "_blank", rel: "noopener" },
      h("img", { class: "shot", src: shotUrl(b.shot), alt: "The page when it asked", loading: "lazy" })),
    human && pending && h("p", { class: "sub", text: "Do what it needs in the live view, then say Done." }),
    !live && pending && b.profile && h("div", { class: "row live-link" }, h("a", { class: "button small",
      href: liveHash(b.profile, b.tab), text: human ? "Open the live view" : "Look at it live" })),
    pending && askEl(p),
    words && h("div", { class: "result", text: words }));
}
