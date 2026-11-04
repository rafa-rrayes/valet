"use strict";
// The vault (docs/API.md, "The vault"): saved logins for the browser's profiles. login() fills one into a page, so
// an agent signs in without seeing the password; an agent that lists one under its secrets gets the password as an
// env var. Values go in and never come out: a password or 2FA field is never filled from anywhere, and is emptied
// once saved. This file draws
// - #/vault: the saved logins by profile, and a new one;
// - #/vault/<entry>: one: its details, a new password or 2FA secret, Clear, Delete.
// Loaded after browser.js (saveBar, loginRow, sitePattern, BROWSER).

const VAULT_RE = /^(?=.{1,64}$)[a-z0-9][a-z0-9._-]*(\/[a-z0-9][a-z0-9._-]*){0,2}$/;
const VAULT_RULE = "Lowercase letters, digits, dots, dashes and _, in up to three parts separated by / (like "
  + "github/alice), 64 at most.";
const TOTP_HINT = "The secret the site shows when you turn on two-step sign-in (letters and digits), or the "
  + "otpauth:// link its QR code holds. login() types the current code.";

addRoute((parts) => {
  if (parts[0] !== "vault") return null;
  return parts[1] ? VaultEntryView(parts.slice(1).join("/")) : VaultView();
});

// A site and a username -> a name for the entry: github.com and alice@x.com -> github/alice.
function vaultName(site, username) {
  const host = sitePattern(site).split("/")[0].replace(/^\*\./, "").replace(/^www\./, "");
  const labels = host.split(".").filter(Boolean);
  const base = labels.length > 1 ? labels[labels.length - 2] : labels[0] || "";
  const clean = (t) => String(t || "").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+/, "")
    .replace(/-+$/, "");
  const user = clean(String(username || "").split("@")[0]);
  const name = clean(base) + (base && user ? "/" + user : "");
  return name.slice(0, 64).replace(/[/._-]+$/, "");
}

// A write-only field: a password or a 2FA secret. `has`: one is saved (then Clear marks it to go at the next save).
// value(): undefined (leave it), "" (clear it) or the new value.
function secretField({ key, label, placeholder, hint, onChange, fresh }) {
  let has = false, clear = false;
  const input = h("input", { type: "password", autocomplete: "new-password", autocapitalize: "none",
    "aria-label": label, "data-field": "vault-" + key, "data-1p-ignore": "", "data-lpignore": "true",
    "data-bwignore": "", oninput: () => onChange() });
  input.setAttribute("spellcheck", "false");
  const state = h("span", { class: "secret-state" });
  const btn = h("button", { type: "button", class: "small", "data-clear": key, onclick: () => {
    clear = !clear;
    input.value = "";
    draw();
    onChange();
  } });
  const el = h("div", { class: "field secret-field", "data-row": "secret:" + key },
    h("div", { class: "row between" }, h("label", { text: label }), h("span", { class: "row" }, state, btn)),
    input, hint && h("p", { class: "sub hint-line", text: hint }));
  function draw() {
    state.textContent = clear ? "Cleared when you save" : has ? "Saved" : "Not set";
    state.className = "secret-state " + (clear ? "going" : has ? "set" : "unset");
    state.hidden = !!fresh;            // a new login's: nothing to say yet
    btn.textContent = clear ? "Keep it" : "Clear";
    btn.hidden = !has;
    input.disabled = clear;
    input.placeholder = clear ? "" : has ? "Type a new one to replace it" : placeholder;
  }
  draw();
  return {
    el,
    value() { return clear ? "" : input.value ? input.value : undefined; },
    // saved: whether one is saved now; keep: leave what the owner typed or cleared (a refresh, not a save).
    set(saved, keep) {
      has = !!saved;
      if (!keep || !has) clear = false;
      if (!keep) input.value = "";
      draw();
    },
  };
}

// The profile select, from /api/browser/profiles (a profile that's gone stays listed while an entry has it).
function profileSelect(field, onChange) {
  const sel = h("select", { "aria-label": "Profile", "data-field": field, onchange: () => onChange() });
  return {
    el: sel,
    draw(profiles, value) {
      const list = profiles || [];
      const names = list.map((p) => p.name);
      fill(sel, !value && h("option", { value: "", text: list.length ? "Pick a profile" : "No profiles yet" }),
        list.map((p) => h("option", { value: p.name, text: profileTitle(p) + (p.title ? ` (${p.name})` : "") })),
        value && !names.includes(value) && h("option", { value, text: profiles ? `${value} (no such profile)` : value }));
      sel.value = value || "";
    },
  };
}

// ---------- #/vault ----------

function VaultView() {
  let entries = null, err = "", profiles = null, profilesErr = "", open = !!BROWSER.newLogin, flashMsg = "";
  let preset = BROWSER.newLogin, nameEdited = false, busy = false;
  BROWSER.newLogin = null;
  const listBox = h("div"), newBox = h("div");
  const flash = h("p", { class: "note ok-note", hidden: true, "data-row": "vault-flash" });

  const profile = profileSelect("vault-profile", () => { newErr.hidden = true; });
  const text = (field, label, attrs = {}) => {
    const i = h("input", { autocomplete: "off", autocapitalize: "none", "aria-label": label, "data-field": field, ...attrs });
    i.setAttribute("spellcheck", "false");
    return i;
  };
  const suggest = () => { if (!nameEdited) nameIn.value = vaultName(siteIn.value, userIn.value); newErr.hidden = true; };
  const siteIn = text("vault-site", "Site", { class: "mono", placeholder: "github.com", inputmode: "url", oninput: suggest });
  const userIn = text("vault-username", "Username", { placeholder: "alice@example.com", oninput: suggest });
  const nameIn = text("vault-name", "Name", { class: "mono", placeholder: "github/alice", maxLength: 64,
    oninput: () => { nameEdited = true; newErr.hidden = true; } });
  const labelIn = text("vault-label", "Label", { placeholder: "e.g. Work account", maxLength: 80, autocapitalize: "sentences" });
  const password = secretField({ key: "password", label: "Password", placeholder: "Its password",
    fresh: true,
    onChange: () => { newErr.hidden = true; } });
  const totp = secretField({ key: "totp", label: "2FA secret (optional)", placeholder: "Base32 secret or otpauth:// link",
    hint: TOTP_HINT, fresh: true,
    onChange: () => { newErr.hidden = true; } });
  const newErr = h("p", { class: "err", hidden: true, "data-err": "vault-new" });

  function reset() {
    for (const i of [siteIn, userIn, nameIn, labelIn]) i.value = "";
    password.set(false);
    totp.set(false);
    nameEdited = false;
    newErr.hidden = true;
  }

  function drawNew() {
    profile.draw(profiles, preset || profile.el.value || (profiles && profiles.length === 1 ? profiles[0].name : ""));
    preset = null;
    fill(newBox, open && h("form", { class: "card newlogin", autocomplete: "off", onsubmit: (e) => e.preventDefault() },
      h("h3", { text: "New login" }),
      h("div", { class: "field" }, h("label", { text: "Profile" }), profile.el,
        h("p", { class: "sub hint-line", text: "The browser it signs in." })),
      h("div", { class: "field" }, h("label", { text: "Site" }), siteIn,
        h("p", { class: "sub hint-line", text: "Where login() may fill it: github.com covers its subdomains too." })),
      h("div", { class: "field" }, h("label", { text: "Username" }), userIn),
      password.el, totp.el,
      h("div", { class: "field" }, h("label", { text: "Name" }), nameIn,
        h("p", { class: "sub hint-line", text: "How agents and settings name it. " + VAULT_RULE })),
      h("div", { class: "field" }, h("label", { text: "Label (optional)" }), labelIn),
      newErr,
      h("div", { class: "row end actions" },
        h("button", { type: "button", class: "small", text: "Cancel", onclick: () => { open = false; reset(); drawNew(); } }),
        h("button", { type: "submit", class: "small primary", text: "Save", disabled: busy,
          onclick: (e) => { e.preventDefault(); create(); } }))));     // Enter in a field clicks it too
  }

  async function create() {
    const show = (m) => { newErr.textContent = m; newErr.hidden = false; };
    const name = nameIn.value.trim();
    if (!profile.el.value) return show("Pick the profile it's for.");
    if (!siteIn.value.trim()) return show("Type the site it's for, like github.com.");
    if (!name) return show("Give it a name.");
    if (!VAULT_RE.test(name)) return show(VAULT_RULE);
    const body = { name, profile: profile.el.value, site: siteIn.value.trim() };
    if (labelIn.value.trim()) body.label = labelIn.value.trim();
    if (userIn.value.trim()) body.username = userIn.value.trim();
    if (password.value()) body.password = password.value();
    if (totp.value()) body.totp = totp.value();
    busy = true;
    drawNew();
    try {
      await api("POST", "/api/vault", body);
      reset();
      open = false;
      flashMsg = `Saved ${name}.` + (body.password ? "" : " It has no password yet: add one on its page.");
      await load();
    } catch (x) { show(x.message); }
    busy = false;
    drawNew();
    drawFlash();
  }

  function drawFlash() { flash.textContent = flashMsg; flash.hidden = !flashMsg; }

  function drawList() {
    const known = new Map((profiles || []).map((p) => [p.name, p]));
    const groups = new Map();
    for (const e of entries || []) {
      if (!groups.has(e.profile)) groups.set(e.profile, []);
      groups.get(e.profile).push(e);
    }
    const order = [...(profiles || []).map((p) => p.name).filter((n) => groups.has(n)),
      ...[...groups.keys()].filter((n) => !known.has(n)).sort()];
    fill(listBox, err && h("p", { class: "err", text: err }),
      profilesErr && h("p", { class: "err", text: profilesErr }),
      !entries && !err && h("p", { class: "sub", text: "Loading…" }),
      entries && !entries.length && h("p", { class: "empty", text: profiles && !profiles.length
        ? "No saved logins. Add a browser profile first: a login belongs to one." : "No saved logins yet." }),
      order.map((n) => {
        const p = known.get(n);
        return h("div", { class: "vault-group", "data-row": "vault-profile:" + n },
          h("div", { class: "row between section-head" }, h("h2", { text: p ? profileTitle(p) : n + " (no such profile)" }),
            p && h("a", { class: "small-link", href: profileHash(n), text: "Profile ›" })),
          h("div", { class: "card" }, h("div", { class: "rows" }, groups.get(n).map(loginRow))));
      }));
  }

  async function load() {
    const [es, ps] = await Promise.allSettled([api("GET", "/api/vault"), api("GET", "/api/browser/profiles")]);
    if (es.status === "fulfilled") { entries = es.value; err = ""; } else err = es.reason.message;
    if (ps.status === "fulfilled") {
      profiles = ps.value.profiles;
      profilesErr = ps.value.error ? "browser.yaml has an error, so there are no profiles until it's fixed (Browser › "
        + "Advanced)." : "";
    } else profilesErr = ps.reason.message;
    drawList();
    drawNew();
  }

  drawNew();
  drawList();
  load();
  const el = h("section", { class: "vault" },
    h("a", { class: "back", href: "#/browser", text: "‹ Browser" }),
    h("div", { class: "row between" }, h("h1", { text: "Vault" }),
      h("button", { type: "button", class: "small", text: "New login", onclick: () => {
        open = !open;
        flashMsg = "";
        drawFlash();
        drawNew();
        if (open) siteIn.focus();
      } })),
    h("p", { class: "sub intro", text: "Saved logins for the browser's profiles. An agent signs in with login(), which "
      + "fills the page without showing it the password. What you type here is never shown again, here or anywhere." }),
    flash, newBox, listBox);
  if (open) setTimeout(() => siteIn.focus(), 0);
  return {
    el,
    frame(f) { if (f.type === "hello") load(); },
  };
}

// ---------- #/vault/<entry> ----------

function VaultEntryView(name) {
  // A name no entry can have (an address like #/vault/../skills/x) is never put in a path: vaultPath keeps "..".
  const valid = VAULT_RE.test(name);
  const path = "/api/vault/" + vaultPath(name);
  let saved = null, gone = false, loadErr = "", profiles = null, deleting = false;
  const head = h("div"), form = h("div"), deleteBox = h("div", { class: "danger-zone" });
  const edited = () => bar.draw();
  const profile = profileSelect("vault-profile", edited);
  const text = (field, label, attrs = {}) => {
    const i = h("input", { autocomplete: "off", autocapitalize: "none", "aria-label": label, "data-field": field,
      oninput: edited, ...attrs });
    i.setAttribute("spellcheck", "false");
    return i;
  };
  const siteIn = text("vault-site", "Site", { class: "mono", inputmode: "url" });
  const userIn = text("vault-username", "Username");
  const labelIn = text("vault-label", "Label", { maxLength: 80, autocapitalize: "sentences" });
  const password = secretField({ key: "password", label: "Password", placeholder: "Its password", onChange: edited });
  const totp = secretField({ key: "totp", label: "2FA secret", placeholder: "Base32 secret or otpauth:// link",
    hint: TOTP_HINT, onChange: edited });

  // What the form would change: only the fields that differ, so a save never sends what wasn't touched.
  function changes() {
    if (!saved) return {};
    const out = {};
    if (profile.el.value && profile.el.value !== saved.profile) out.profile = profile.el.value;
    if (siteIn.value.trim() !== saved.site) out.site = siteIn.value.trim();
    if (labelIn.value.trim() !== (saved.label || "")) out.label = labelIn.value.trim();
    if (userIn.value.trim() !== (saved.username || "")) out.username = userIn.value.trim();
    if (password.value() !== undefined) out.password = password.value();
    if (totp.value() !== undefined) out.totp = totp.value();
    return out;
  }
  const isDirty = () => Object.keys(changes()).length > 0;
  const bar = saveBar({
    isDirty,
    async save() {
      const body = changes();
      if (!body.site && "site" in body) throw new Error("Type the site it's for.");
      const e = await api("PUT", path, body);
      take(e, true);
      const users = e.used_by.map(agentName);
      return "password" in body && body.password && users.length
        ? `Saved. ${andList(users)} ${users.length > 1 ? "get" : "gets"} the new password at the next restart.` : "Saved.";
    },
    discard() { take(saved, true); },
  });

  function drawHead() {
    const e = saved;
    const p = e && (profiles || []).find((x) => x.name === e.profile);
    const facts = e && [e.used_at ? "Last used " + ago(e.used_at) : "Never used", "changed " + ago(e.updated_at)];
    fill(head, h("a", { class: "back", href: "#/vault", text: "‹ Vault" }),
      h("h1", { class: "mono", text: name }),
      e && h("p", { class: "sub" }, `For ${e.site}, in `, h("a", { href: profileHash(e.profile), text: p ? profileTitle(p) : e.profile }),
        " · " + facts.join(" · ")),
      e && e.used_by.length > 0 && h("p", { class: "sub", "data-row": "vault-used-by", text: `${andList(e.used_by.map(agentName))} `
        + `${e.used_by.length > 1 ? "get" : "gets"} its password as an env var (their Secrets).` }));
  }

  function drawForm() {
    if (gone) { fill(form, h("p", { class: "empty", text: `There's no saved login named ${name}.` })); return; }
    if (!saved) { fill(form, loadErr ? h("p", { class: "err", text: loadErr }) : h("p", { class: "sub", text: "Loading…" })); return; }
    fill(form,
      h("form", { class: "card", autocomplete: "off", onsubmit: (e) => { e.preventDefault(); bar.save(); } },
        h("div", { class: "field" }, h("label", { text: "Profile" }), profile.el),
        h("div", { class: "field" }, h("label", { text: "Site" }), siteIn,
          h("p", { class: "sub hint-line", text: "Where login() may fill it: github.com covers its subdomains too." })),
        h("div", { class: "field" }, h("label", { text: "Username" }), userIn),
        h("div", { class: "field" }, h("label", { text: "Label" }), labelIn)),
      h("h2", { text: "Secrets" }),
      h("form", { class: "card secrets-card", autocomplete: "off", onsubmit: (e) => { e.preventDefault(); bar.save(); } },
        h("p", { class: "sub intro", text: "Never shown: type a new value to replace one, or clear it." }),
        password.el, totp.el));
  }

  // The entry as saved -> the form (all of it with `reset`, else only what the owner hasn't edited).
  function take(e, reset) {
    const before = saved;
    const keep = (input, was) => !reset && before && input.value.trim() !== (was || "");
    saved = e;
    if (!keep(siteIn, before && before.site)) siteIn.value = e.site;
    if (!keep(userIn, before && before.username)) userIn.value = e.username || "";
    if (!keep(labelIn, before && before.label)) labelIn.value = e.label || "";
    const p = reset || !before || profile.el.value === before.profile ? e.profile : profile.el.value;
    profile.draw(profiles, p);
    password.set(e.has_password, !reset && !!before);
    totp.set(e.has_totp, !reset && !!before);
    drawHead();
    drawForm();
    drawDelete();
    bar.draw();
  }

  function drawDelete() {
    if (!saved) { fill(deleteBox); return; }
    const err = h("p", { class: "err", hidden: true, "data-err": "delete" });
    const users = saved.used_by.map(agentName);
    fill(deleteBox, !deleting ? h("button", { type: "button", class: "small danger", text: "Delete login",
      onclick: () => { deleting = true; drawDelete(); } })
      : h("div", { class: "confirm" },
        h("div", { class: "q", text: `Delete ${name}? Its password and 2FA secret go for good, and login() can't use it.` }),
        users.length > 0 && h("p", { class: "warn-text", text: `${andList(users)} ${users.length > 1 ? "list" : "lists"} it `
          + "under secrets and won't start without it: take it out of their settings too." }),
        err,
        h("div", { class: "row" },
          h("button", { type: "button", class: "small", text: "Cancel", onclick: () => { deleting = false; drawDelete(); } }),
          h("button", { type: "button", class: "small danger", text: "Delete", onclick: async () => {
            err.hidden = true;
            try {
              await api("DELETE", path);
              saved = null;
              location.hash = "#/vault";
            } catch (x) { err.textContent = x.message; err.hidden = false; }
          } }))));
  }

  async function load() {
    if (!valid) { gone = true; drawForm(); return; }
    const [es, ps] = await Promise.allSettled([api("GET", path), api("GET", "/api/browser/profiles")]);
    if (ps.status === "fulfilled") profiles = ps.value.profiles;
    if (es.status === "fulfilled") { loadErr = ""; take(es.value, false); return; }
    if (es.reason.status === 404) { gone = true; saved = null; drawHead(); drawDelete(); } else loadErr = es.reason.message;
    drawForm();
    bar.draw();
  }

  drawHead();
  drawForm();
  load();
  const el = h("section", { class: "vault-entry" }, head, form, deleteBox, bar.el);
  return {
    el,
    close() { bar.close(); },
    leave: bar.leave,
    frame(f) {
      if (f.type === "hello") load();
      if (f.type === "agent" && saved) drawHead();
    },
  };
}
