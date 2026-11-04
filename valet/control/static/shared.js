"use strict";
// The shared space (docs/API.md, "The shared space"): each agent has its own, between you and that agent only.
// On an agent's page, its Shared section: the list, newest change first, with search, filters, a new note and
// uploads. #/shared/<id>, one item: a note's Markdown, a file's preview (images, text) and download, editing,
// replacing a file, deleting, and for an item with no agent, moving it to one. #/shared: the items from when Shared
// was one list for every agent and whose agent Valet couldn't tell (a card on the agents list leads there while there
// are any; a header tab for a list that's empty for nearly everyone would only be noise). What an agent adds or
// changes is new until you've looked: a dot on the item, and "N new in Shared" under the agent. `shared` frames keep
// it all current.

const SHARED_PAGE = 20;
const SHARED_PREVIEW_MAX = 256 * 1024;      // bytes of a text file shown in place
const SHARED_IMAGES = /^image\/(png|jpeg|gif|webp|avif|bmp)$/;
const SHARED_TEXT = /^(text\/|application\/(json|xml|yaml|x-yaml|toml|javascript|x-sh|sql)$)/;
const SHARED_MD = /\.(md|markdown)$/i;

// The counts (GET /api/shared/unseen, then every frame): unseen in all and per agent, and how many items have no
// agent. lines: each agent's "N new in Shared"; card: the agents list's card for the unassigned, while it's shown.
const SHARED = { unseen: 0, agents: {}, unassigned: 0, lines: new Map(), card: null };

function setSharedCounts(c) {
  if (!c || typeof c.unseen !== "number") return;
  SHARED.unseen = c.unseen;
  SHARED.agents = c.agents || {};
  SHARED.unassigned = c.unassigned || 0;
  for (const [name, el] of SHARED.lines) drawSharedLine(name, el);
  if (SHARED.card) SHARED.card();
}

addFrameType("shared");
addWatcher((f) => {
  // A deleted agent's items went with it (no shared frame says so): look again.
  if (f.type === "hello" || f.type === "agent_deleted") api("GET", "/api/shared/unseen").then(setSharedCounts, () => {});
  else if (f.type === "shared") setSharedCounts(f);
});
addRoute((parts) => (parts[0] !== "shared" ? null : parts[1] ? SharedItemView(parts[1]) : UnassignedView()));

// Under an agent with new items (the agents list, its page): one element per agent, kept current by the counts.
function drawSharedLine(name, el) {
  const n = SHARED.agents[name] || 0;
  el.hidden = !n;
  el.textContent = n === 1 ? "1 new in Shared" : `${n} new in Shared`;
}
addAgentLine((a) => {
  let el = SHARED.lines.get(a.name);
  if (!el) {
    el = h("a", { class: "shared-badge", href: "#/a/" + enc(a.name), "data-row": "shared-new:" + a.name });
    SHARED.lines.set(a.name, el);
  }
  drawSharedLine(a.name, el);
  return el;
});

// The agents list: a card for the unassigned items, while there are any.
addAgentsPart(() => {
  const el = h("a", { class: "card linkcard row", href: "#/shared", "data-row": "shared-unassigned" });
  function draw() {
    const n = SHARED.unassigned;
    el.hidden = !n;
    fill(el, h("span", { class: "grow" }, h("strong", { text: "Shared items with no agent" }),
      h("span", { class: "sub", text: `${n} from when Shared was one list for every agent: move each to its agent, `
        + "or delete it" })),
    h("span", { class: "chev", text: "›" }));
  }
  draw();
  SHARED.card = draw;
  return { el };
});
addAgentSection((name) => SharedList(name));

function sharedSize(n) {
  if (n == null) return "";
  return n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB";
}

function sharedWho(by) { return by === "owner" ? "You" : agentName(by); }

// "scout · 5m ago", or "Added by scout, changed by you · 1h ago".
function sharedByline(it) {
  const who = it.updated_by !== it.added_by ? `Added by ${sharedWho(it.added_by)}, changed by `
    + `${it.updated_by === "owner" ? "you" : agentName(it.updated_by)}` : sharedWho(it.added_by);
  return who + " · " + ago(it.updated_at);
}

// A body that isn't JSON (FormData, a File): fetch as api() would, the answer read as JSON.
async function sharedSend(method, path, body) {
  let res;
  try { res = await fetch(path, { method, credentials: "same-origin", body }); } catch {
    throw new ApiError(0, "Can't reach Valet. Check the connection.", null);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.ok) return data;
  if (res.status === 401) signedOut();
  throw new ApiError(res.status, detailOf(data) || `${res.status} ${res.statusText}`, data);
}

function sharedForm(files, text) {
  const form = new FormData();
  for (const f of files) form.append("file", f, f.name);
  if (text) form.append("text", text);
  return form;
}

// ---------- the list: an agent's (its page's Shared section), or the unassigned (#/shared, agent null) ----------

function SharedList(agent) {
  const base = agent ? `/api/agents/${enc(agent)}/shared` : "/api/shared/unassigned";
  let items = null, more = false, err = "", q = "", by = "", kind = "", seq = 0, searchTimer = 0;
  let panel = "", busy = false;          // panel: "", "note" or "upload"
  const dots = new Set();                // unseen when shown: the dot stays while the screen is up
  const listBox = h("div"), panelBox = h("div");
  const formErr = h("p", { class: "err", hidden: true, "data-err": "shared-form" });
  const search = h("input", { type: "search", placeholder: "Search titles and text", "aria-label": "Search",
    "data-field": "shared-q", autocomplete: "off",
    oninput: () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { q = search.value.trim(); load(); }, 250); } });
  const whoSel = h("select", { "aria-label": "Who added it", "data-field": "shared-by",
    onchange: () => { by = whoSel.value; load(); } });
  const kindSel = h("select", { "aria-label": "Kind", "data-field": "shared-kind",
    onchange: () => { kind = kindSel.value; load(); } },
  h("option", { value: "", text: "Notes and files" }), h("option", { value: "note", text: "Notes" }),
  h("option", { value: "file", text: "Files" }));
  const noteTitle = h("input", { placeholder: "Title (else its first line)", maxLength: 200, "aria-label": "Title",
    "data-field": "note-title" });
  const noteText = h("textarea", { class: "persona", rows: 6, placeholder: "Markdown", "aria-label": "Note",
    "data-field": "note-text", oninput: () => grow(noteText) });
  const fileIn = h("input", { type: "file", multiple: true, "aria-label": "Files", "data-field": "upload-files" });
  const fileText = h("input", { placeholder: "What it is (optional)", "aria-label": "Description",
    "data-field": "upload-text" });

  function drawWho() {
    fill(whoSel, h("option", { value: "", text: "Anyone" }), h("option", { value: "owner", text: "You" }),
      agent && h("option", { value: agent, text: agentName(agent) }));
    whoSel.value = by;
  }

  function open(which) {
    panel = panel === which ? "" : which;
    formErr.hidden = true;
    drawPanel();
    if (panel === "note") noteText.focus();
  }

  function drawPanel() {
    if (panel === "note") {
      fill(panelBox, h("div", { class: "card shared-new" }, h("div", { class: "field" }, h("label", { text: "New note" }),
        noteTitle), noteText, formErr, h("div", { class: "row end" },
        h("button", { type: "button", class: "small", text: "Cancel", onclick: () => open("") }),
        h("button", { type: "button", class: "small primary", text: "Add note", disabled: busy, onclick: addNote }))));
    } else if (panel === "upload") {
      fill(panelBox, h("div", { class: "card shared-new" }, h("div", { class: "field" }, h("label", { text: "Upload" }),
        fileIn), fileText, formErr, h("div", { class: "row end" },
        h("button", { type: "button", class: "small", text: "Cancel", onclick: () => open("") }),
        h("button", { type: "button", class: "small primary", text: busy ? "Uploading…" : "Upload", disabled: busy,
          onclick: upload }))));
    } else fill(panelBox);
  }

  function fail(x) { formErr.textContent = x.message || String(x); formErr.hidden = false; }

  async function addNote() {
    if (!noteText.value.trim()) { fail("Write the note first."); return; }
    busy = true; drawPanel();
    try {
      await api("POST", base + "/notes", { title: noteTitle.value, text: noteText.value });
      noteTitle.value = noteText.value = "";
      panel = "";
    } catch (x) { fail(x); }
    busy = false; drawPanel();
  }

  async function upload() {
    const files = [...(fileIn.files || [])];
    if (!files.length) { fail("Pick a file first."); return; }
    busy = true; drawPanel();
    try {
      await sharedSend("POST", base + "/files", sharedForm(files, fileText.value.trim()));
      fileIn.value = ""; fileText.value = "";
      panel = "";
    } catch (x) { fail(x); }
    busy = false; drawPanel();
  }

  function itemRow(it) {
    const dot = it.unseen || dots.has(it.id);
    return h("a", { class: "shared-item row", href: "#/shared/" + it.id, "data-row": "shared:" + it.id },
      h("span", { class: "dot" + (dot ? " on" : ""), title: dot ? "New" : "" }),
      h("span", { class: "grow" },
        h("span", { class: "name" }, it.kind === "file" && h("span", { class: "kind", text: "File" }), it.title),
        it.preview && h("span", { class: "sub desc", text: brief(it.preview, 160) }),
        h("span", { class: "sub desc by", text: sharedByline(it)
          + (it.kind === "file" ? " · " + sharedSize(it.size) : "") })),
      h("span", { class: "chev", text: "›" }));
  }

  function drawList() {
    const filtered = q || by || kind;
    fill(listBox, err ? h("p", { class: "err", text: err })
      : !items ? h("p", { class: "sub", text: "Loading…" })
        : !items.length ? h("p", { class: "empty", text: filtered ? "Nothing matches."
          : agent ? `Nothing shared yet. Leave a note or a file here for ${agentName(agent)}, and it leaves its own `
            + "here for you." : "Nothing here: every shared item is with its agent." })
          : [h("div", { class: "card rows shared-list" }, items.map(itemRow)),
            more && h("div", { class: "row" }, h("button", { type: "button", class: "small", text: "Older",
              onclick: () => load(items[items.length - 1].updated_at) }))]);
  }

  // Unseen items on screen are seen now: marked on the server, their dots kept until you leave.
  async function seeShown(list) {
    const ids = list.filter((it) => it.unseen).map((it) => it.id);
    if (!ids.length || document.hidden) return;
    for (const id of ids) dots.add(id);
    try { await api("POST", "/api/shared/seen", { ids }); } catch { /* the next look marks them */ }
  }

  async function load(before) {
    const s = ++seq;
    const params = [["q", q], ["by", by], ["kind", kind], ["limit", SHARED_PAGE], ["before", before]]
      .filter(([, v]) => v).map(([k, v]) => k + "=" + enc(v)).join("&");
    let r;
    try { r = await api("GET", base + (params ? "?" + params : "")); } catch (x) {
      if (s === seq) { err = x.message; drawList(); }
      return;
    }
    if (s !== seq) return;
    err = "";
    items = before ? [...items, ...r.items] : r.items;
    more = r.more;
    drawList();
    seeShown(r.items);
  }

  drawWho();
  const el = h("div", { class: "shared" },
    agent && [h("div", { class: "row between section-head" }, h("h2", { class: "grow", text: "Shared" }),
      h("button", { type: "button", class: "small", text: "New note", onclick: () => open("note") }),
      h("button", { type: "button", class: "small", text: "Upload", onclick: () => open("upload") })),
    h("p", { class: "sub intro", text: `Notes and files between you and ${agentName(agent)} only, newest change `
      + "first. No other agent sees them." })],
    panelBox, h("div", { class: "shared-tools" }, search, h("div", { class: "row" }, agent && whoSel, kindSel)),
    listBox);
  drawList();
  load();
  return {
    el,
    frame(f) {
      if (f.type === "hello") { drawWho(); load(); return; }
      if (f.type === "agent") { drawWho(); return; }
      if (f.type !== "shared" || !items) return;
      if (f.deleted != null) { items = items.filter((it) => it.id !== f.deleted); drawList(); return; }
      if (!f.item) return;
      if ((f.item.agent || null) !== agent) {            // another agent's, or moved away from here
        if (items.some((it) => it.id === f.item.id)) { items = items.filter((it) => it.id !== f.item.id); drawList(); }
        return;
      }
      if (q || by || kind) { load(); return; }      // whether it still matches is the server's call
      items = [f.item, ...items.filter((it) => it.id !== f.item.id)];
      drawList();
      seeShown([f.item]);
    },
  };
}

// #/shared: the unassigned items.
function UnassignedView() {
  const list = SharedList(null);
  const el = h("section", { class: "shared-page" }, h("a", { class: "back", href: "#/", text: "‹ Agents" }),
    h("h1", { text: "Shared items with no agent" }),
    h("p", { class: "sub intro", text: "From when Shared was one list for every agent, and Valet couldn't tell whose "
      + "they were. No agent sees them. Open one to move it to its agent, or delete it." }),
    list.el);
  return { el, frame: list.frame };
}

// ---------- one item: #/shared/<id> ----------

function SharedItemView(idText) {
  const id = Number(idText);
  const path = "/api/shared/" + enc(idText);
  let item = null, gone = false, editing = false, draft = null, busy = false, preview = null, leaveTo = null;
  const top = h("div"), body = h("div");
  const ask = confirmBox();
  const err = h("p", { class: "err", hidden: true, "data-err": "shared" });
  const replaceIn = h("input", { type: "file", "aria-label": "New file", "data-field": "replace-file" });
  const moveSel = h("select", { "aria-label": "Its agent", "data-field": "shared-move" });
  let replacing = false;
  const dirty = () => editing && !!item && (draft.title !== item.title || draft.text !== item.text);
  // Where it lives: its agent's page, or the unassigned list.
  const home = () => (item && item.agent ? "#/a/" + enc(item.agent) : item ? "#/shared" : "#/");

  function showErr(x) { err.textContent = x ? (x.message || String(x)) : ""; err.hidden = !x; }

  function drawTop() {
    fill(top, h("a", { class: "back", href: home(), text: "‹ " + (!item ? "Agents" : item.agent
      ? agentName(item.agent) : "Shared items with no agent") }),
      h("h1", { class: "skill-title shared-title", text: item ? item.title : gone ? "Deleted" : "…" }),
      item && h("p", { class: "sub", text: (item.kind === "file" ? [item.mime, sharedSize(item.size)].join(" · ") + " · "
        : "Note · ") + sharedByline(item) }));
  }

  function drawPreview() {
    if (item.kind !== "file") return null;
    const url = `${path}/file`;
    const parts = [];
    if (SHARED_IMAGES.test(item.mime || "")) {
      parts.push(h("img", { class: "shared-img", src: `${url}?inline=1&v=${item.updated_at}`, alt: item.title }));
    } else if (preview && preview.text != null) {
      parts.push(SHARED_MD.test(item.title) ? h("div", { class: "md", "data-row": "shared-preview" }, markdown(preview.text))
        : h("pre", { class: "shared-text", "data-row": "shared-preview", text: preview.text }));
    } else if (preview && preview.err) parts.push(h("p", { class: "err", text: preview.err }));
    return [parts.length > 0 && h("div", { class: "card shared-preview" }, parts),
      h("div", { class: "row" },
        h("a", { class: "button small primary", href: url, download: item.title, text: "Download" }),
        (item.mime === "application/pdf" || SHARED_IMAGES.test(item.mime || "")) && h("a", { class: "button small",
          href: url + "?inline=1", target: "_blank", rel: "noopener", text: "Open" }))];
  }

  function draw() {
    drawTop();
    if (gone) {
      fill(body, h("p", { class: "empty", text: "This item was deleted." }));
      return;
    }
    if (!item) { fill(body, err.hidden ? h("p", { class: "sub", text: "Loading…" }) : err); return; }
    const leaving = leaveTo !== null && h("div", { class: "confirm" },
      h("div", { class: "q", text: "You have unsaved changes." }),
      h("div", { class: "row" },
        h("button", { type: "button", class: "small", text: "Stay", onclick: () => { leaveTo = null; draw(); } }),
        h("button", { type: "button", class: "small danger", text: "Discard & leave", onclick: () => {
          const to = leaveTo;
          leaveTo = null; editing = false; location.hash = to; } })));
    if (editing) {
      const note = item.kind === "note";
      const title = h("input", { value: draft.title, maxLength: note ? 200 : 120, "aria-label": note ? "Title" : "Name",
        "data-field": "shared-title", oninput: () => { draft.title = title.value; } });
      const text = h("textarea", { class: "persona", rows: note ? 10 : 4, value: draft.text,
        "aria-label": note ? "Note" : "Description", "data-field": "shared-text",
        oninput: () => { draft.text = text.value; grow(text); } });
      fill(body, h("div", { class: "card" },
        h("div", { class: "field" }, h("label", { text: note ? "Title" : "File name" }), title),
        h("div", { class: "field" }, h("label", { text: note ? "Note" : "Description" }), text), err,
        h("div", { class: "row end" },
          h("button", { type: "button", class: "small", text: "Cancel", onclick: () => {
            editing = false; showErr(""); draw(); } }),
          h("button", { type: "button", class: "small primary", text: "Save", disabled: busy, onclick: save }))),
      leaving);
      setTimeout(() => grow(text), 0);
      return;
    }
    fill(body,
      item.kind === "note" ? h("div", { class: "card md", "data-row": "shared-text" }, markdown(item.text))
        : item.text && h("div", { class: "card md shared-desc", "data-row": "shared-text" }, markdown(item.text)),
      drawPreview(),
      replacing && h("div", { class: "card shared-new" }, h("div", { class: "field" },
        h("label", { text: "Replace the file" }), replaceIn), h("div", { class: "row end" },
        h("button", { type: "button", class: "small", text: "Cancel", onclick: () => { replacing = false; draw(); } }),
        h("button", { type: "button", class: "small primary", text: busy ? "Uploading…" : "Replace", disabled: busy,
          onclick: replace }))),
      h("div", { class: "row shared-actions" },
        h("button", { type: "button", class: "small", text: "Edit", onclick: () => {
          editing = true; draft = { title: item.title, text: item.text }; showErr(""); draw(); } }),
        item.kind === "file" && !replacing && h("button", { type: "button", class: "small", text: "Replace file",
          onclick: () => { replacing = true; draw(); } }),
        h("button", { type: "button", class: "small danger", text: "Delete", onclick: () => ask.ask(
          item.agent ? `Delete ${item.title}? It goes for ${agentName(item.agent)} too.` : `Delete ${item.title}?`,
          "Delete", remove, "danger") })),
      !item.agent && drawMove(), ask.el, err);
  }

  // An item with no agent: into one agent's space (only that agent sees it then).
  function drawMove() {
    const names = [...S.agents.keys()].sort();
    if (!names.length) return null;
    fill(moveSel, names.map((n) => h("option", { value: n, text: agentName(n) })));
    return h("div", { class: "card shared-new" }, h("div", { class: "field" },
      h("label", { text: "Move it to an agent" }), moveSel),
    h("p", { class: "sub", text: "Only that agent sees it then, and you." }),
    h("div", { class: "row end" }, h("button", { type: "button", class: "small primary", text: "Move",
      disabled: busy, onclick: move })));
  }

  async function move() {
    busy = true; showErr(""); draw();
    try { item = await api("POST", `${path}/move`, { agent: moveSel.value }); } catch (x) { showErr(x); }
    busy = false; draw();
  }

  async function loadPreview() {
    preview = null;
    const textual = SHARED_TEXT.test(item.mime || "") || SHARED_MD.test(item.title);
    if (item.kind !== "file" || !textual || item.size > SHARED_PREVIEW_MAX) return;
    try {
      const res = await fetch(`${path}/file?inline=1`, { credentials: "same-origin" });
      preview = res.ok ? { text: await res.text() } : { err: "Can't show it: " + res.status };
    } catch { preview = { err: "Can't reach Valet. Check the connection." }; }
    if (!editing) draw();
  }

  async function load() {
    try {
      item = await api("GET", path);
      showErr("");
    } catch (x) {
      if (x.status === 404) gone = true; else showErr(x);
      draw();
      return;
    }
    draw();
    loadPreview();
    if (item.unseen) {
      try { await api("POST", "/api/shared/seen", { ids: [id] }); } catch { /* the next look marks it */ }
    }
  }

  async function save() {
    busy = true; showErr(""); draw();
    const body = {};
    if (draft.title !== item.title) body.title = draft.title;
    if (draft.text !== item.text) body.text = draft.text;
    try {
      item = Object.keys(body).length ? await api("PATCH", path, body) : item;
      editing = false;
    } catch (x) { showErr(x); }
    busy = false; draw();
  }

  async function replace() {
    const files = [...(replaceIn.files || [])];
    if (files.length !== 1) { showErr("Pick one file."); return; }
    busy = true; showErr(""); draw();
    try {
      item = await sharedSend("PUT", `${path}/file`, sharedForm(files, ""));
      replaceIn.value = "";
      replacing = false;
      loadPreview();
    } catch (x) { showErr(x); }
    busy = false; draw();
  }

  async function remove() {
    const to = home();
    try { await api("DELETE", path); location.hash = to; } catch (x) { showErr(x); }
  }

  const el = h("section", { class: "shared-view" }, top, body);
  draw();
  if (Number.isInteger(id) && id > 0) load(); else { gone = true; draw(); }
  return {
    el,
    leave(to) {
      if (!dirty()) return false;
      leaveTo = to;
      draw();
      return true;
    },
    frame(f) {
      if (f.type === "hello" && !editing) { load(); return; }
      if (f.type !== "shared") return;
      if (f.deleted === id) { gone = true; item = null; editing = false; draw(); return; }
      if (f.item && f.item.id === id && !editing) load();
    },
  };
}
