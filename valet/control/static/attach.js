"use strict";
// Attachments in a conversation (docs/API.md, "Attachments"): the composer's + button, and files pasted into the text
// box or dropped on the session. Each one uploads at once (POST /api/agents/<name>/attachments; a chip shows it) and
// goes with the next message. An image reaches a harness that takes images as the image itself; anything else is
// put in the agent's workspace (attachments/), and the message names the path. In the log, an image the owner sent
// shows as a picture, a file as a link to it in the agent's files (files.js). Uses computerSend() from files.js.

const ATTACH_MAX = 10;
const ATTACH_IMAGE = /^image\/(png|jpeg|gif|webp)$/;

addComposerPart((ctx) => {
  const items = [];         // {file, id, url, state: "uploading"|"ready"|"failed", err, thumb}
  const picker = h("input", { type: "file", multiple: true, hidden: true, "data-field": "attach-files",
    onchange: () => { const fs = [...(picker.files || [])]; picker.value = ""; add(fs); } });
  const btn = h("button", { type: "button", class: "attach-btn", text: "+", title: "Attach files",
    "aria-label": "Attach files", onclick: () => picker.click() });
  const chips = h("div", { class: "attach-chips", hidden: true, "data-row": "attachments" });

  function changed() { draw(); ctx.changed(); }

  async function add(files) {
    ctx.showErr("");
    for (const f of files) {
      if (items.length >= ATTACH_MAX) { ctx.showErr(`At most ${ATTACH_MAX} attachments per message.`); break; }
      const thumb = ATTACH_IMAGE.test(f.type || "") && typeof URL.createObjectURL === "function"
        ? URL.createObjectURL(f) : null;
      const it = { file: f, id: null, url: null, state: "uploading", err: "", thumb };
      items.push(it);
      changed();
      try {
        const out = await computerSend("POST",
          `/api/agents/${enc(ctx.name)}/attachments?name=${enc(f.name || "pasted")}`, f);
        it.id = out.id;
        it.url = out.url;
        it.state = "ready";
      } catch (x) {
        it.state = "failed";
        it.err = x.message;
      }
      if (!items.includes(it) && it.id) api("DELETE", it.url).catch(() => {});   // taken off while it uploaded
      changed();
    }
  }

  function drop(it) {
    const i = items.indexOf(it);
    if (i >= 0) items.splice(i, 1);
    if (it.id) api("DELETE", it.url).catch(() => {});
    if (it.thumb) URL.revokeObjectURL(it.thumb);
    changed();
  }

  function draw() {
    chips.hidden = !items.length;
    fill(chips, items.map((it, i) => h("span", { class: "attach-chip " + it.state, "data-row": "attachment:" + i,
      title: it.err || it.file.name },
    it.thumb && h("img", { src: it.thumb, alt: "" }),
    h("span", { class: "grow" }, h("span", { class: "name", text: it.file.name || "pasted" }),
      h("span", { class: "sub", text: it.state === "uploading" ? "Uploading…" : it.state === "failed" ? it.err
        : filesSize(it.file.size) })),
    h("button", { type: "button", class: "x", text: "×", "aria-label": "Remove " + (it.file.name || "attachment"),
      onclick: () => drop(it) }))));
  }

  const hasFiles = (e) => !!e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
  ctx.input.addEventListener("paste", (e) => {
    const fs = [...((e.clipboardData && e.clipboardData.files) || [])];
    if (fs.length) { e.preventDefault(); add(fs); }
  });
  ctx.el.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    ctx.el.classList.add("dropping");
  });
  ctx.el.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget || !ctx.el.contains(e.relatedTarget)) ctx.el.classList.remove("dropping");
  });
  ctx.el.addEventListener("drop", (e) => {
    ctx.el.classList.remove("dropping");
    if (!hasFiles(e)) return;
    e.preventDefault();
    add([...e.dataTransfer.files]);
  });

  return {
    el: h("span", { class: "attach" }, btn, picker),
    above: chips,
    has: () => items.some((it) => it.state === "ready"),
    busy: () => items.some((it) => it.state === "uploading"),
    async body() {
      if (items.some((it) => it.state === "failed")) {
        throw new Error("An attachment didn't upload: take it off, or attach it again.");
      }
      return items.length ? { attachments: items.map((it) => it.id) } : {};
    },
    sent() {
      for (const it of items) if (it.thumb) URL.revokeObjectURL(it.thumb);
      items.length = 0;
      changed();
    },
  };
});

// In the log: an image (the owner's, by its uri; an agent's, by its data) and a file (a link to it in the agent's
// files when it's there).
addBlockView((b, ev) => {
  if (b.type === "image") {
    const src = typeof b.uri === "string" && b.uri.startsWith("/api/") ? b.uri
      : b.data && ATTACH_IMAGE.test(b.mimeType || "") ? `data:${b.mimeType};base64,${b.data}` : null;
    if (!src) return null;
    const img = h("img", { src, alt: b.name || "Image", loading: "lazy" });
    return src.startsWith("/api/") ? h("a", { class: "att-image", href: src, target: "_blank", rel: "noopener",
      "data-row": "att-image" }, img) : h("div", { class: "att-image", "data-row": "att-image" }, img);
  }
  if (b.type === "resource_link" && typeof b.uri === "string") {
    const path = b.uri.startsWith("file:///") ? b.uri.slice(7) : null;
    const kids = [h("span", { class: "ftype file", "aria-hidden": "true" }),
      h("span", { class: "grow" }, h("span", { class: "name", text: b.name || path || b.uri }),
        h("span", { class: "sub", text: [path, b.size != null && filesSize(b.size)].filter(Boolean).join(" · ") }))];
    return path && ev && ev.agent ? h("a", { class: "att-file row", href: filesHash(ev.agent, path),
      "data-row": "att-file" }, kids) : h("div", { class: "att-file row", "data-row": "att-file" }, kids);
  }
  return null;
});
