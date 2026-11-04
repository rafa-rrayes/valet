"use strict";
// The agent's files (docs/API.md, "The agent's computer"): #/a/<name>/files/<path>, its folders while it runs (a
// container's /work, /home/agent and shared folders; a host agent's working folder): a folder's listing, a file's
// preview (text, images) and download, uploads (a name that's taken asks: replace, or keep both), new folders. Below
// it, a container agent's volumes, and deleting them while it's stopped. computerSend() is attach.js's too.

const FILES_IMAGE = /\.(png|jpe?g|gif|webp|avif|bmp)$/i;

function filesHash(name, path) {
  const segs = String(path || "").split("/").filter(Boolean);
  return `#/a/${enc(name)}/files` + segs.map((p) => "/" + enc(p)).join("");
}

function filesSize(n) {
  if (n == null) return "";
  return n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB"
    : n < 1073741824 ? (n / 1048576).toFixed(1) + " MB" : (n / 1073741824).toFixed(1) + " GB";
}

function filesJoin(dir, name) { return (dir === "/" ? "" : dir.replace(/\/+$/, "")) + "/" + name; }

// A request whose body is a file (sent as it is, streamed by the browser), the answer read as JSON like api().
async function computerSend(method, path, body) {
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

addRoute((parts) => (parts[0] === "a" && parts[1] && parts[2] === "files"
  ? FilesView(parts[1], parts.length > 3 ? "/" + parts.slice(3).join("/") : null) : null));

addAgentAction((a) => a.level && a.level !== "none"
  && h("a", { class: "button small", href: filesHash(a.name), text: "Files" }));

function FilesView(name, path) {
  const base = `/api/agents/${enc(name)}/computer`;
  let listing = null, preview = null, err = "", busy = "", newOpen = false, seq = 0, wasUp = null;
  const body = h("div", { class: "files-body" });
  const msg = h("p", { class: "err", hidden: true, "data-err": "files" });
  const choice = h("div", { class: "confirm", hidden: true, "data-row": "files-choice" });
  const upInput = h("input", { type: "file", multiple: true, hidden: true, "data-field": "files-upload",
    onchange: () => { const fs = [...(upInput.files || [])]; upInput.value = ""; upload(fs); } });
  const newName = h("input", { class: "mono", placeholder: "New folder's name", autocomplete: "off",
    autocapitalize: "none", "aria-label": "New folder's name", "data-field": "files-new",
    onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); mkdir(); } } });
  const vols = filesVolumes(name);
  const el = h("section", { class: "files-view" },
    h("a", { class: "back", href: "#/a/" + enc(name), text: "‹ " + (agentTitle(S.agents.get(name)) || name) }),
    h("h1", { text: "Files" }), body, upInput, vols.el);

  function agent() { return S.agents.get(name); }
  function up() { const a = agent(); return !!a && a.level !== "none" && a.state === "running" && a.connected; }
  function showMsg(text) { msg.textContent = text; msg.hidden = !text; }

  async function load() {
    const mine = ++seq;
    wasUp = up();
    if (!wasUp) { listing = preview = null; err = ""; draw(); return; }
    try {
      const out = await api("GET", base + "/files" + (path ? "?path=" + enc(path) : ""));
      if (mine !== seq) return;
      listing = out;
      err = "";
      preview = null;
      if (out.type === "file") preview = await filePreview(out.path);
    } catch (x) {
      if (mine !== seq) return;
      listing = null;
      err = x.message;
    }
    if (mine === seq) draw();
  }

  async function filePreview(p) {
    if (FILES_IMAGE.test(p)) return { image: base + "/files/download?inline=1&path=" + enc(p) };
    try { return await api("GET", base + "/files/text?path=" + enc(p)); } catch (x) { return { error: x.message }; }
  }

  // Where `p` is: its root (the deepest one holding it), then each folder below it.
  function crumbs(p) {
    const roots = listing.roots || [];
    const root = roots.filter((r) => p === r.path || p.startsWith(r.path === "/" ? "/" : r.path + "/"))
      .sort((x, y) => y.path.length - x.path.length)[0];
    if (!root) return [h("span", { class: "mono", text: p })];
    const out = [h("a", { href: filesHash(name, root.path), text: root.label })];
    let cur = root.path;
    for (const seg of p.slice(root.path.length).split("/").filter(Boolean)) {
      cur = filesJoin(cur, seg);
      out.push(h("span", { class: "sep", text: "/" }), h("a", { href: filesHash(name, cur), text: seg }));
    }
    return out;
  }

  function rootChips() {
    const roots = listing.roots || [];
    if (roots.length < 2) return null;
    return h("div", { class: "chips files-roots" }, roots.map((r) => h("a", {
      class: "button small" + (listing.path === r.path || listing.path.startsWith(r.path + "/") ? " on" : ""),
      href: filesHash(name, r.path), text: r.label, title: r.path })));
  }

  function writable() {
    const p = listing.path;
    const root = (listing.roots || []).filter((r) => p === r.path || p.startsWith(r.path + "/"))
      .sort((x, y) => y.path.length - x.path.length)[0];
    return !!root && root.writable;
  }

  function entryRow(e) {
    const full = filesJoin(listing.path, e.name);
    const sub = [e.type === "dir" ? "Folder" : e.type === "other" ? (e.link ? "A link that leads nowhere it may go"
      : "Not a file or folder") : filesSize(e.size), e.link && e.type !== "other" && "link", e.mtime && ago(e.mtime)]
      .filter(Boolean).join(" · ");
    const inner = [h("span", { class: "ftype " + e.type, "aria-hidden": "true" }),
      h("span", { class: "grow" }, h("span", { class: "name", text: e.name + (e.type === "dir" ? "/" : "") }),
        h("span", { class: "sub", text: sub }))];
    return e.type === "other" ? h("div", { class: "fentry row other", "data-row": "file:" + e.name }, inner)
      : h("a", { class: "fentry row", href: filesHash(name, full), "data-row": "file:" + e.name }, inner);
  }

  function drawFolder() {
    const canWrite = writable();
    const tools = h("div", { class: "row files-tools" },
      h("button", { type: "button", class: "small", text: "Upload", disabled: !canWrite || !!busy,
        onclick: () => upInput.click() }),
      h("button", { type: "button", class: "small", text: "New folder", disabled: !canWrite || !!busy,
        onclick: () => { newOpen = !newOpen; draw(); if (newOpen) newName.focus(); } }),
      !canWrite && h("span", { class: "sub", text: "Read-only" }),
      busy && h("span", { class: "sub pulse", text: busy }));
    const entries = listing.entries || [];
    return [tools,
      newOpen && h("div", { class: "row files-new" }, newName,
        h("button", { type: "button", class: "small primary", text: "Create", onclick: mkdir })),
      choice, msg,
      h("div", { class: "card" }, entries.length ? h("div", { class: "rows files-list" }, entries.map(entryRow))
        : h("p", { class: "empty", text: "Nothing here yet." }),
        listing.truncated && h("p", { class: "sub", text: `Showing the first ${entries.length}.` }))];
  }

  function drawFile() {
    const p = listing.path, fileName = p.split("/").pop();
    const pv = preview || {};
    let shown;
    if (pv.image) shown = h("img", { class: "files-img", src: pv.image, alt: fileName });
    else if (pv.error) shown = h("p", { class: "err", text: pv.error });
    else if (pv.binary) shown = h("p", { class: "sub", text: "No preview: it isn't text." });
    else shown = [h("pre", { class: "files-text", "data-row": "file-text", text: pv.text || "" }),
      pv.truncated && h("p", { class: "sub", text: "Showing the start of it: download it for the rest." })];
    const dir = p.slice(0, p.lastIndexOf("/")) || "/";
    return [h("p", { class: "sub", text: [filesSize(listing.size), listing.mtime && "changed " + ago(listing.mtime)]
      .filter(Boolean).join(" · ") }),
    h("div", { class: "row files-tools" },
      h("a", { class: "button small primary", href: base + "/files/download?path=" + enc(p), download: fileName,
        text: "Download" }),
      h("a", { class: "button small", href: filesHash(name, dir), text: "Back to the folder" })),
    h("div", { class: "card files-preview" }, shown)];
  }

  function draw() {
    const a = agent();
    if (!a || a.level === "none") {
      fill(body, h("p", { class: "note", text: "It has no computer (level none): no files." }));
      return;
    }
    if (!up()) {
      fill(body, h("div", { class: "note row between" },
        h("span", { class: "grow", text: a.desired === "running" && !a.connected ? "Its runner is offline."
          : "Its files are there while it runs: start it to look." }),
        a.state !== "starting" && agentButtons(a, startTrouble())));
      return;
    }
    if (err) { fill(body, h("p", { class: "err", "data-err": "files-load", text: err })); return; }
    if (!listing) { fill(body, h("p", { class: "empty", text: "Loading…" })); return; }
    fill(body, rootChips(), h("nav", { class: "crumbs", "aria-label": "Where" }, crumbs(listing.path)),
      listing.type === "dir" ? drawFolder() : listing.type === "file" ? drawFile()
        : h("p", { class: "sub", text: "Not a file or folder." }));
  }

  // "Replace", "Keep both" or "Cancel" for a name that's taken.
  function ask(fileName) {
    return new Promise((resolve) => {
      const done = (v) => { choice.hidden = true; resolve(v); };
      fill(choice, h("div", { class: "q", text: `${fileName} is here already.` }), h("div", { class: "row" },
        h("button", { type: "button", class: "small", text: "Cancel", onclick: () => done(null) }),
        h("button", { type: "button", class: "small", text: "Keep both", onclick: () => done("rename") }),
        h("button", { type: "button", class: "small danger", text: "Replace", onclick: () => done("replace") })));
      choice.hidden = false;
    });
  }

  async function upload(files) {
    if (!files.length || !listing || listing.type !== "dir") return;
    showMsg("");
    const dir = listing.path;
    for (const f of files) {
      const taken = (listing.entries || []).some((e) => e.name === f.name);
      const exists = taken ? await ask(f.name) : "fail";
      if (!exists) continue;
      busy = `Uploading ${f.name}…`;
      draw();
      try {
        await computerSend("POST", `${base}/files/upload?dir=${enc(dir)}&name=${enc(f.name)}&exists=${exists}`, f);
      } catch (x) {
        showMsg(`${f.name}: ${x.status === 409 ? "it's there already" : x.message}`);
      }
    }
    busy = "";
    await load();
  }

  async function mkdir() {
    const n = newName.value.trim();
    if (!n || n.includes("/") || n === "." || n === "..") { showMsg("A folder's name, without /."); draw(); return; }
    showMsg("");
    try {
      const out = await api("POST", base + "/files/folder", { path: filesJoin(listing.path, n) });
      newName.value = "";
      newOpen = false;
      location.hash = filesHash(name, out.path);
    } catch (x) { showMsg(x.message); draw(); }
  }

  draw();
  load();
  return {
    el,
    frame(f) {
      if (f.type === "agent" && f.agent.name === name) {
        if (up() !== wasUp) load(); else draw();
        vols.update();
      } else if (f.type === "hello") { load(); vols.load(); }
    },
  };
}

// A container agent's volumes: what they are, and deleting them (only while it's stopped).
function filesVolumes(name) {
  const base = `/api/agents/${enc(name)}/computer/volumes`;
  const el = h("div", { class: "card volumes", hidden: true, "data-row": "volumes" });
  const confirm = confirmBox();
  let info = null, err = "", done = "", deleting = false;

  async function load() {
    const a = S.agents.get(name);
    if (!a || a.level !== "container") { el.hidden = true; return; }
    try { info = await api("GET", base); err = ""; } catch (x) { info = null; err = x.message; }
    draw();
  }

  function running() { const a = S.agents.get(name); return !!a && (a.state === "running" || a.state === "starting"); }

  async function remove() {
    deleting = true;
    draw();
    try {
      const out = await api("DELETE", base);
      done = out.removed.length ? `Deleted ${andList(out.removed)}.` : "There were none to delete.";
      err = "";
    } catch (x) { err = x.message; }
    deleting = false;
    await load();
  }

  function draw() {
    const vs = (info && info.volumes) || [];
    const names = vs.map((v) => `${v.name} (${v.mount})`);
    el.hidden = false;
    fill(el, h("h2", { text: "Volumes" }),
      h("p", { class: "sub", text: "Its home (/home/agent) and workspace (/work) are Docker volumes"
        + (info && info.runner ? ` on ${info.runner}` : "") + ": they outlast restarts and stopping it." }),
      vs.length ? h("div", { class: "rows" }, vs.map((v) => h("div", { "data-row": "volume:" + v.name },
        h("div", { class: "name mono", text: v.name }), h("div", { class: "sub", text: v.mount }))))
        : info && h("p", { class: "sub", "data-row": "volumes-none", text: "It has none on its runner." }),
      done && h("p", { class: "sub ok", text: done }),
      err && h("p", { class: "err", "data-err": "volumes", text: err }),
      vs.length > 0 && h("div", { class: "row volumes-acts" },
        h("button", { type: "button", class: "small danger", text: "Delete volumes",
          disabled: deleting || running(),
          onclick: () => confirm.ask(`Delete ${andList(names)}? Everything in its home and workspace goes, for good.`,
            "Delete for good", remove, "danger") }),
        running() && h("span", { class: "sub", text: "Stop it first." })),
      confirm.el);
  }

  load();
  return { el, load, update: () => { if (info) draw(); else load(); } };
}
