// Drives the live view (browser.js's LiveView) under the stand-in DOM (fakedom.js) with a fake WebSocket, canvas and
// createImageBitmap. Usage: node live.js <static dir>, with a JSON list of jobs on stdin; prints a JSON list of
// results.
//   {"profile": str, "tab"?: str, "inbox"?: [InboxItem], "agents"?: [Agent], "routes"?: as settings.js's,
//    "rect"?: [w, h] (the canvas on screen, px), "screen"?: [w, h] (the space it has), "steps": [Step]}
//      -> {sockets: [{url, sent: [message], closed}], status (the status line's text), seen, html, replaced (hashes
//          history.replaceState was given), canvas: {width, height, style, hidden}, drawn (frames drawn),
//          tabs: [[value, text]], requests, zoom, signedOut}
// Steps: {"open": true} (the socket opens), {"msg": a JSON message from the server}, {"frame": [w, h]} (a JPEG of that
// size), {"close": code}, {"pointer": "down"|"move"|"up"|"cancel", "type": "mouse"|"touch", "x", "y" (px on the
// canvas), "id"?, "t"? (ms)}, {"wheel": dy, "x", "y", "mode"?}, {"key": key, "meta"?: bool}, {"paste": text},
// {"button": its text or title}, {"input": data-field, "value"}, {"submit": data-field of a field in the form},
// {"change": data-field, "value"}, {"wait": ms}, {"app": a /ws/client frame}, {"hidden": bool}, {"close_view": true}.
"use strict";
const fs = require("fs");
const { html, load } = require("./fakedom");

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function walk(n, out = []) {
  if (n.tag !== "#text") out.push(n);
  for (const c of n.children) walk(c, out);
  return out;
}
function seen(n) { return n.hidden ? "" : n._text + n.children.map(seen).join(""); }

async function run(job) {
  const requests = [];
  const routes = job.routes || {};
  async function fetch(path, opts) {
    const method = (opts && opts.method) || "GET";
    requests.push({ method, path });
    const r = routes[`${method} ${path}`] || { status: 404, body: { detail: "not found" } };
    return { ok: r.status < 300, status: r.status, statusText: "", json: async () => r.body,
      text: async () => JSON.stringify(r.body) };
  }
  const ctx = load(process.argv[2], fetch);
  const S = ctx.S;
  S.signedIn = true;
  S.agents = new Map((job.agents || []).map((a) => [a.name, a]));
  S.inbox = new Map((job.inbox || []).map((i) => [i.id, i]));

  // The browser's parts the stand-in DOM lacks.
  const [rw, rh] = job.rect || [640, 450];
  const [sw, sh] = job.screen || [644, 454];
  let drawn = 0;
  const make = ctx.document.createElement;
  ctx.document.createElement = (tag) => {
    const el = make(tag);
    el.clientWidth = sw;
    el.clientHeight = sh;
    el.scrollLeft = 0;
    el.scrollTop = 0;
    if (tag === "canvas") {
      el.width = 300;
      el.height = 150;
      el.getContext = () => ({ drawImage() { drawn++; } });
      el.getBoundingClientRect = () => ({ left: 0, top: 0, width: rw, height: rh });
      el.setPointerCapture = () => {};
    }
    return el;
  };
  const docListeners = {};
  ctx.document.addEventListener = (t, f) => { (docListeners[t] ||= []).push(f); };
  ctx.document.removeEventListener = (t, f) => { docListeners[t] = (docListeners[t] || []).filter((x) => x !== f); };
  ctx.window.addEventListener = () => {};
  ctx.window.removeEventListener = () => {};
  ctx.location.protocol = "http:";
  ctx.location.host = "valet.test";
  const replaced = [];
  ctx.history.replaceState = (st, title, url) => { replaced.push(url); };
  ctx.createImageBitmap = async (blob) => ({ width: blob.w, height: blob.h, close() {} });
  const sockets = [];
  ctx.WebSocket = class {
    constructor(url) { this.url = url; this.sent = []; this.readyState = 0; this.closed = false; sockets.push(this); }
    send(m) { this.sent.push(JSON.parse(m)); }
    close() { this.closed = true; this.readyState = 3; }
  };
  let signedOut = false;
  ctx.signedOut = () => { signedOut = true; };

  const view = ctx.LiveView(job.profile, job.tab || null);
  S.view = view;
  const ws = () => sockets[sockets.length - 1];
  const all = () => walk(view.el);
  const canvas = all().find((e) => e.tag === "canvas");
  const byField = (f) => {
    const el = all().find((e) => e.attrs["data-field"] === f);
    if (!el) throw new Error("no control " + f);
    return el;
  };
  const fire = async (el, type, extra) => {
    for (const fn of el.listeners[type] || []) await fn({ target: el, preventDefault() {}, ...extra });
  };
  for (let i = 0; i < 3; i++) await tick();

  for (const s of job.steps || []) {
    if (s.open) { ws().readyState = 1; ws().onopen(); }
    else if (s.msg) ws().onmessage({ data: JSON.stringify(s.msg) });
    else if (s.frame) ws().onmessage({ data: { w: s.frame[0], h: s.frame[1] } });
    else if (s.close) { const w = ws(); w.readyState = 3; if (w.onclose) w.onclose({ code: s.close }); }
    else if (s.pointer) {
      await fire(canvas, "pointer" + s.pointer, { pointerType: s.type || "mouse", clientX: s.x, clientY: s.y,
        button: 0, pointerId: s.id || 1, timeStamp: s.t || 0 });
    } else if (s.wheel != null) {
      await fire(canvas, "wheel", { clientX: s.x, clientY: s.y, deltaY: s.wheel, deltaMode: s.mode || 0 });
    } else if (s.key) {
      await fire(canvas, "keydown", { key: s.key, metaKey: !!s.meta, ctrlKey: false, altKey: false, isComposing: false });
    } else if (s.paste != null) {
      await fire(canvas, "paste", { clipboardData: { getData: () => s.paste } });
    } else if (s.button) {
      const b = all().find((e) => e.tag === "button" && (e.textContent === s.button || e.title === s.button
        || e.attrs.title === s.button) && !e.hidden);
      if (!b) throw new Error("no button " + s.button);
      await fire(b, "click");
    } else if (s.input || s.change) {
      const el = byField(s.input || s.change);
      el.value = s.value;
      await fire(el, s.input ? "input" : "change");
    } else if (s.submit) {
      let form = byField(s.submit);
      while (form && form.tag !== "form") form = form.parent;
      await fire(form, "submit");
    } else if (s.wait) await sleep(s.wait);
    else if (s.app) ctx.onFrame(s.app);
    else if (s.hidden != null) {
      ctx.document.hidden = s.hidden;
      for (const f of docListeners.visibilitychange || []) f();
    } else if (s.close_view) view.close();
    for (let i = 0; i < 5; i++) await tick();
  }

  const status = all().find((e) => e.attrs["data-row"] === "live-status");
  const tabSel = byField("live-tab");
  return {
    sockets: sockets.map((w) => ({ url: w.url, sent: w.sent, closed: w.closed })),
    status: status ? seen(status) : "", seen: seen(view.el), html: html(view.el), replaced, drawn, requests,
    canvas: { width: canvas.width, height: canvas.height, style: { width: canvas.style.width, height: canvas.style.height },
      hidden: canvas.hidden },
    tabs: tabSel.children.map((o) => [o.value, o.textContent]), tab: tabSel.value, tabsDisabled: tabSel.disabled,
    zoom: all().find((e) => e.className === "lv-zoom").textContent, signedOut,
  };
}

(async () => {
  const jobs = JSON.parse(fs.readFileSync(0, "utf8"));
  const out = [];
  for (const job of jobs) out.push(await run(job));
  console.log(JSON.stringify(out));
  process.exit(0);           // a reconnect's timer would keep node up
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
