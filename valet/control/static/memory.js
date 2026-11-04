"use strict";
// An agent's memory (docs/API.md, "Memory"): the Memory section of its settings (the switch, which sets `memory` and
// its `memory` layer together, and its files), and a file's own screen, #/a/<name>/memory/<file>: the Markdown,
// editing it, deleting it, and its history with older versions to look at and restore. The agent changes these
// files as it works; `memory` frames keep both screens current.

const MEM_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const MEM_RULE = "Lowercase letters and digits, words joined by dashes, like project-notes.";

// What the owner typed as a file name -> a memory file's name: "Project Notes.md" -> "project-notes".
function memSlug(text) {
  return String(text || "").trim().toLowerCase().replace(/\.md$/, "").replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 64).replace(/-+$/, "");
}

function memBytes(text) {
  let n = 0;
  for (const ch of String(text)) {
    const c = ch.codePointAt(0);
    n += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
  }
  return n;
}

function memSize(n) { return n < 1024 ? n + " B" : (n / 1024).toFixed(1) + " KiB"; }

function memHash(agent, file) { return `#/a/${enc(agent)}/memory/${enc(file)}`; }

addFrameType("memory");

// ---------- Settings › Memory ----------

addSettingsSection({ key: "memory", title: "Memory", after: "knows", make: memorySection });

function memorySection(ctx) {
  const base = ctx.base + "/memory";
  const el = h("div", { class: "card memory" });
  let info = null, err = "", newOpen = false, newErr = "";
  const newName = h("input", { class: "mono", placeholder: "project-notes", maxLength: 64, autocomplete: "off",
    autocapitalize: "none", "aria-label": "New memory file's name", "data-field": "memory-new",
    oninput: () => { newErr = ""; draw(); newName.focus(); },
    onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); create(); } } });
  newName.setAttribute("spellcheck", "false");

  async function load() {
    try { info = await api("GET", base); err = ""; } catch (x) { err = x.message; }
    draw();
  }

  function toggle(on) {
    const d = ctx.draft;
    d.memory = on;
    ctx.setDirectives(withDirective(d.directives, "memory", on));
    ctx.changed("memory");
    draw();
  }

  function create() {
    const n = memSlug(newName.value);
    if (!n || !MEM_NAME_RE.test(n)) { newErr = MEM_RULE; draw(); return; }
    if (info && info.files.some((f) => f.name === n)) { newErr = `There's already a file named ${n}.`; draw(); return; }
    newName.value = "";
    newOpen = false;
    location.hash = memHash(ctx.name, n);
  }

  function fileRow(f) {
    return h("a", { class: "memfile row", href: memHash(ctx.name, f.name), "data-row": "memory:" + f.name },
      h("span", { class: "grow" }, h("span", { class: "name mono", text: f.name }),
        h("span", { class: "sub desc", text: [f.title, memSize(f.size), "updated " + ago(f.updated_at)]
          .filter(Boolean).join(" · ") })),
      h("span", { class: "chev", text: "›" }));
  }

  function draw() {
    const d = ctx.draft;
    if (!d) return;
    const on = !!d.memory, layer = (d.directives || []).includes("memory");
    const sw = h("input", { type: "checkbox", class: "switch", checked: on, "aria-label": "Memory",
      "data-field": "memory", onchange: (e) => toggle(e.target.checked) });
    sw.setAttribute("role", "switch");
    const files = info ? info.files : [];
    const desc = !on ? "Off: no memory tools, and nothing about memory in what it's told."
      : layer ? "On: it reads and writes these files with its memory tools, never waiting for your approval, and "
        + "every session starts with them in what it's told."
        : "On, but what it's told at the start leaves them out: it has to look them up.";
    fill(el,
      h("p", { class: "sub intro", text: "Notes it keeps for itself across sessions and restarts, as Markdown files, "
        + "one per subject. You can read and change them too." }),
      h("div", { class: "layer-row", "data-row": "memory" },
        h("label", { class: "check" }, h("span", { class: "grow" }, h("span", { class: "name", text: "Memory" }),
          h("span", { class: "sub desc", text: desc })), sw)),
      on && !layer && h("div", { class: "note" }, "Its directives leave the memory layer out. ",
        h("button", { type: "button", class: "small", "data-field": "directive:memory", text: "Tell it",
          onclick: () => { ctx.setDirectives(withDirective(ctx.draft.directives, "memory", true)); draw(); } })),
      ctx.errFor("memory"),
      err ? h("p", { class: "err", text: err })
        : !info ? h("p", { class: "sub", text: "Loading…" })
          : [h("h3", { class: "mem-head", text: files.length ? `${files.length} file${files.length > 1 ? "s" : ""}, `
            + `${memSize(info.size)} of ${info.limits.total / 1048576} MiB` : "No memory files yet" }),
          files.length > 0 && h("div", { class: "rows" }, files.map(fileRow))],
      newOpen ? h("div", { class: "newmem" },
        h("div", { class: "field" }, h("label", { text: "New file" }), newName,
          h("p", { class: "err", hidden: !newErr, text: newErr, "data-err": "memory-new" })),
        h("div", { class: "row end" },
          h("button", { type: "button", class: "small", text: "Cancel", onclick: () => {
            newOpen = false; newErr = ""; newName.value = ""; draw(); } }),
          h("button", { type: "button", class: "small primary", text: "Create", onclick: create })))
        : h("button", { type: "button", class: "small new-mem", text: "New file", onclick: () => {
          newOpen = true; draw(); newName.focus(); } }));
  }

  load();
  return {
    el, draw,
    frame(f) {
      if (f.type === "hello") { load(); return; }
      if (f.type !== "memory" || f.agent !== ctx.name || !info) return;
      const rest = info.files.filter((x) => x.name !== f.name);
      info.files = f.file ? [...rest, f.file].sort((a, b) => (a.name < b.name ? -1 : 1)) : rest;
      info.size = info.files.reduce((n, x) => n + x.size, 0);
      draw();
    },
  };
}

// ---------- a memory file: #/a/<name>/memory/<file> ----------

addRoute((parts) => (parts[0] === "a" && parts[1] && parts[2] === "memory" && parts[3]
  ? MemoryView(parts[1], parts[3].replace(/\.md$/, "")) : null));

function MemoryView(agent, file) {
  const path = `/api/agents/${enc(agent)}/memory/${enc(file)}`;
  const back = `#/a/${enc(agent)}/settings`;
  let text = null, meta = null, exists = false, editing = false, draft = "", busy = false;
  let news = "", leaveTo = null, history = null, histErr = "", older = null;   // older: {commit, message, text}
  const top = h("div"), body = h("div"), histBox = h("div");
  const ask = confirmBox();
  const err = h("p", { class: "err", hidden: true, "data-err": "memory" });
  const hist = h("details", { class: "more mem-history" }, h("summary", { text: "History" }), histBox);
  hist.addEventListener("toggle", () => { if (hist.open) loadHistory(); });
  const valid = MEM_NAME_RE.test(file);
  const dirty = () => editing && draft !== (text || "");

  function drawTop() {
    const sub = !exists ? "A new memory file for " + agentName(agent)
      : [meta.title, memSize(meta.size), "updated " + ago(meta.updated_at)].filter(Boolean).join(" · ");
    fill(top, h("a", { class: "back", href: back, text: "‹ " + agentName(agent) + " · Memory" }),
      h("h1", { class: "mono skill-title", text: file + ".md" }), h("p", { class: "sub", text: sub }));
  }

  function showErr(x) { err.textContent = x; err.hidden = !x; }

  function draw() {
    drawTop();
    if (!valid) { fill(body, h("p", { class: "err", text: `${file} isn't a memory file's name. ${MEM_RULE}` })); return; }
    if (text === null && !editing) { fill(body, h("p", { class: "sub", text: "Loading…" })); return; }
    const leaving = leaveTo !== null && h("div", { class: "confirm" },
      h("div", { class: "q", text: "You have unsaved changes to this file." }),
      h("div", { class: "row" },
        h("button", { type: "button", class: "small", text: "Stay", onclick: () => { leaveTo = null; draw(); } }),
        h("button", { type: "button", class: "small danger", text: "Discard & leave", onclick: () => {
          const to = leaveTo;
          leaveTo = null; editing = false; location.hash = to; } })));
    if (editing) {
      const box = h("textarea", { class: "persona mem-text", rows: 14, value: draft, "aria-label": "Text",
        "data-field": "memory-text", oninput: () => { draft = box.value; drawCount(); } });
      const count = h("p", { class: "sub hint-line" });
      const drawCount = () => {
        const n = memBytes(draft);
        count.textContent = `Markdown. ${memSize(n)} of 64 KiB.`;
        count.className = n > 65536 ? "warn-text hint-line" : "sub hint-line";
      };
      drawCount();
      fill(body, news && h("div", { class: "note" }, news), h("div", { class: "card" }, box, count, err,
        h("div", { class: "row end mem-actions" },
          h("button", { type: "button", class: "small", text: "Cancel", onclick: cancel }),
          h("button", { type: "button", class: "small primary", text: exists ? "Save" : "Create", disabled: busy,
            onclick: save }))), leaving);
      setTimeout(() => grow(box), 0);
      return;
    }
    if (!exists) {
      fill(body, h("p", { class: "empty", text: news || "There's no such memory file." }),
        h("div", { class: "row" }, h("button", { type: "button", class: "small", text: "Write it",
          onclick: () => { editing = true; draft = ""; news = ""; draw(); } })));
      return;
    }
    fill(body, news && h("div", { class: "note" }, news),
      h("div", { class: "card md mem-md", "data-row": "memory-text" }, markdown(text)),
      h("div", { class: "row mem-actions" },
        h("button", { type: "button", class: "small primary", text: "Edit", onclick: () => {
          editing = true; draft = text; news = ""; draw(); } }),
        h("button", { type: "button", class: "small danger", text: "Delete", onclick: () => ask.ask(
          `Delete ${file}.md? ${agentName(agent)} won't remember what's in it. It stays in the history.`,
          "Delete", remove, "danger") })),
      ask.el, err, hist);
  }

  function cancel() { editing = false; draft = ""; showErr(""); news = ""; draw(); }

  async function load() {
    if (!valid) { draw(); return; }
    try {
      const r = await api("GET", path);
      text = r.text; meta = r.file; exists = true;
    } catch (x) {
      if (x.status === 404) {
        text = ""; meta = null; exists = false;
        if (!editing) { editing = true; draft = ""; }     // a new file: straight to writing it
      } else { text = ""; showErr(x.message); }
    }
    draw();
    if (hist.open) loadHistory();
  }

  async function save() {
    if (busy) return;
    if (!draft.trim()) { showErr("Write something first; to remove the file, delete it."); return; }
    busy = true;
    showErr("");
    try {
      const r = await api("PUT", path, { text: draft });
      meta = r.file; text = draft.endsWith("\n") ? draft : draft + "\n"; exists = true; editing = false; news = "";
      busy = false;
      draw();
      if (hist.open) loadHistory();
    } catch (x) { busy = false; showErr(x.message); draw(); }
  }

  async function remove() {
    try {
      await api("DELETE", path);
      location.hash = back;
    } catch (x) { showErr(x.message); }
  }

  async function loadHistory() {
    try { history = (await api("GET", path + "/history")).history; histErr = ""; } catch (x) { histErr = x.message; }
    drawHistory();
  }

  async function showOlder(c) {
    try {
      const r = await api("GET", path + "?at=" + enc(c.commit));
      older = { commit: c.commit, message: c.message, text: r.text };
    } catch (x) { older = { commit: c.commit, message: c.message, text: null, err: x.message }; }
    drawHistory();
  }

  function drawHistory() {
    const rows = (history || []).map((c, i) => h("div", { class: "hist", "data-row": "mem-commit:" + c.commit },
      h("div", { class: "row" }, h("div", { class: "grow" }, h("div", { class: "what", text: c.message }),
        h("div", { class: "sub" }, ago(c.at) + " · ", h("span", { class: "mono", text: String(c.commit).slice(0, 7) }))),
      i === 0 ? badge("current")
        : !c.message.includes(" deleted ") && h("button", { type: "button", class: "small", text: "View",
          onclick: () => showOlder(c) })),
      older && older.commit === c.commit && h("div", { class: "mem-older" },
        older.err ? h("p", { class: "err", text: older.err })
          : [h("pre", { class: "mem-old-text", text: older.text }),
            h("div", { class: "row end" },
              h("button", { type: "button", class: "small", text: "Close", onclick: () => { older = null; drawHistory(); } }),
              h("button", { type: "button", class: "small primary", text: "Restore this version", onclick: () => {
                editing = true; draft = older.text; older = null; news = "Restored into the editor: Save to keep it.";
                draw(); } }))])));
    fill(histBox, histErr ? h("p", { class: "err", text: histErr })
      : !history ? h("p", { class: "sub", text: "Loading…" })
        : rows.length ? rows : h("p", { class: "sub", text: "No changes yet." }));
  }

  const el = h("section", { class: "memory-view" }, top, body);
  draw();
  load();
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
      if (f.type !== "memory" || f.agent !== agent || f.name !== file) return;
      if (editing && exists) {
        news = f.file ? "It was changed elsewhere since you started editing: saving replaces that."
          : "It was deleted elsewhere: saving writes it again.";
        if (!f.file) exists = false;
        draw();
        return;
      }
      if (!editing) {
        news = f.file ? "" : "It was just deleted.";
        if (f.file) load(); else { exists = false; text = ""; meta = null; draw(); }
      }
    },
  };
}
