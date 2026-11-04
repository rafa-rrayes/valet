// Drives the agent's computer screens (terminal.js, files.js) and the composer's attachments (attach.js) under the
// stand-in DOM (fakedom.js), against a fake API, fake WebSockets and a fake xterm.js. Usage: node computer.js <static
// dir>, with a JSON list of jobs on stdin; prints a JSON list of results.
//   {"hash": str, "routes": {"METHOD /path": {status, body}, or a list answered in turn}, "agents": [Agent],
//    "xterm"?: false (it isn't loaded: the screen injects its scripts), "actions": [Action], "eval"?: str}
//   -> {requests: [{method, path, body}], sockets: [{url, sent: [str | {bytes: str}], closed}],
//       terms: [{writes: str, opts, disposed, focused}], head: [src/href of what was added to <head>],
//       rows: {data-row: its HTML}, errors: {data-err: text}, text, seen, html, hash, value}
// Actions: {"button": text, "row"?: data-row of an ancestor}, {"socket": i, "recv": str | {bytes: str}},
// {"socket": i, "open": true}, {"socket": i, "close": code}, {"type": str} (keys typed in the current xterm),
// {"files": data-field, "names": [...], "types"?: [...]}, {"paste": [names], "types"?: [...]} (into the composer's
// text box), {"input": data-field, "value"}, {"enter": data-field}, {"submit": true} (the composer's form),
// {"frame": a client socket frame}, {"go": hash}, {"load": i} (the i-th script added to <head> loaded).
// A request whose body is a File is recorded as {file: its name}.
"use strict";
const fs = require("fs");
const vm = require("vm");
const { El, html, load } = require("./fakedom");

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function walk(n, out = []) {
  if (n.tag !== "#text") out.push(n);
  for (const c of n.children) walk(c, out);
  return out;
}
function seen(n) { return n.hidden ? "" : n._text + n.children.map(seen).join(""); }
function shown(n) {
  for (let x = n; x; x = x.parent) if (x.hidden) return false;
  return true;
}
function within(e, test) {
  for (let x = e; x; x = x.parent) if (test(x)) return true;
  return false;
}
async function fire(el, type, extra) {
  for (const fn of el.listeners[type] || []) await fn({ target: el, currentTarget: el, preventDefault() {}, ...extra });
}

async function run(job) {
  const requests = [];
  const routes = job.routes || {};
  async function fetch(path, opts) {
    const method = (opts && opts.method) || "GET";
    const raw = opts && opts.body;
    const body = !raw ? null : typeof raw === "string" ? JSON.parse(raw) : raw.isFile ? { file: raw.name } : {};
    requests.push({ method, path, body });
    let r = routes[`${method} ${path}`];
    if (Array.isArray(r)) r = r.length > 1 ? r.shift() : r[0];
    r = r || { status: 404, body: { detail: "not found" } };
    return { ok: r.status < 300, status: r.status, statusText: "", json: async () => r.body };
  }
  const ctx = load(process.argv[2], fetch);

  const sockets = [];
  class FakeSocket {
    constructor(url) {
      this.url = url; this.sent = []; this.readyState = 0; this.closed = null; this.binaryType = "blob";
      this.onmessage = this.onclose = this.onopen = null;
      sockets.push(this);
    }
    send(d) { this.sent.push(typeof d === "string" ? d : { bytes: Buffer.from(d).toString() }); }
    close(code) { this.closed = code || 1000; this.readyState = 3; }
  }
  const terms = [];
  class FakeTerminal {
    constructor(opts) {
      this.opts = opts; this.options = { ...opts }; this.writes = ""; this.cols = 80; this.rows = 24;
      this.disposed = false; this.focused = 0; this.handlers = {};
      terms.push(this);
    }
    loadAddon(a) { a.term = this; }
    open(el) { this.el = el; }
    onData(f) { this.handlers.data = f; }
    onBinary(f) { this.handlers.binary = f; }
    onResize(f) { this.handlers.resize = f; }
    write(d) { this.writes += Buffer.from(d).toString(); }
    focus() { this.focused++; }
    dispose() { this.disposed = true; }
  }
  class FakeFit { fit() { if (this.term) { this.term.cols = 100; this.term.rows = 30; } } }
  ctx.window.removeEventListener = () => {};
  const head = new El("head");
  ctx.document.head = head;
  ctx.WebSocket = FakeSocket;
  ctx.TextEncoder = TextEncoder;
  ctx.location.host = "valet.test";
  ctx.location.protocol = "https:";
  ctx.URL.createObjectURL = () => "blob:x";
  ctx.URL.revokeObjectURL = () => {};
  if (job.xterm !== false) { ctx.Terminal = FakeTerminal; ctx.FitAddon = { FitAddon: FakeFit }; }

  const S = ctx.S;
  S.signedIn = true;
  S.agents = new Map((job.agents || []).map((a) => [a.name, a]));
  let routed = null;
  Object.assign(ctx.location, { hash: job.hash, pathname: "/", search: "" });
  const hashchange = () => { while (ctx.location.hash !== routed) { routed = ctx.location.hash; ctx.route(); } };
  hashchange();
  const settle = async () => { for (let i = 0; i < 8; i++) { await tick(); hashchange(); } };
  await settle();

  const all = () => walk(S.view.el);
  const byField = (f) => {
    const el = all().find((e) => e.attrs["data-field"] === f);
    if (!el) throw new Error("no control " + f);
    return el;
  };
  const file = (name, i, a) => ({ name, size: 3, type: (a.types || [])[i] || "", isFile: true });
  for (const a of job.actions || []) {
    if (a.button) {
      const el = all().find((e) => e.tag === "button" && e.textContent === a.button && shown(e)
        && (!a.row || within(e, (x) => x.attrs["data-row"] === a.row)));
      if (!el) throw new Error("no button " + a.button);
      await fire(el, "click");
    } else if (a.socket != null) {
      const ws = sockets[a.socket];
      if (a.open) { ws.readyState = 1; if (ws.onopen) ws.onopen({}); }
      if (a.recv != null) {
        if (ws.readyState === 0) ws.readyState = 1;
        const data = typeof a.recv === "string" ? a.recv : new Uint8Array(Buffer.from(a.recv.bytes)).buffer;
        ws.onmessage({ data });
      }
      if (a.close != null) { ws.readyState = 3; if (ws.onclose) ws.onclose({ code: a.close }); }
    } else if (a.type != null) {
      terms[terms.length - 1].handlers.data(a.type);
    } else if (a.files) {
      const el = byField(a.files);
      el.files = a.names.map((n, i) => file(n, i, a));
      await fire(el, "change");
    } else if (a.paste) {
      const input = all().find((e) => e.tag === "textarea");
      await fire(input, "paste", { clipboardData: { files: a.paste.map((n, i) => file(n, i, a)) } });
    } else if (a.input) {
      const el = byField(a.input);
      el.value = a.value;
      await fire(el, "input");
    } else if (a.enter) {
      await fire(byField(a.enter), "keydown", { key: "Enter" });
    } else if (a.submit) {
      const form = all().find((e) => e.tag === "form");
      await fire(form, "submit");
    } else if (a.frame) {
      ctx.onFrame(a.frame);
    } else if (a.go != null) {
      ctx.location.hash = a.go;
    } else if (a.load != null) {
      const scripts = head.children.filter((c) => c.tag === "script");
      for (const fn of scripts[a.load].listeners.load || []) fn({});
    }
    await settle();
  }

  const rows = {}, errors = {};
  for (const e of all()) {
    const key = e.attrs["data-row"];
    if (key && !(key in rows)) rows[key] = html(e);
    if (e.attrs["data-err"] && shown(e) && e.textContent) errors[e.attrs["data-err"]] = e.textContent;
  }
  return {
    requests, rows, errors, hash: ctx.location.hash, text: S.view.el.textContent, seen: seen(S.view.el),
    html: html(S.view.el),
    sockets: sockets.map((w) => ({ url: w.url, sent: w.sent, closed: w.closed })),
    terms: terms.map((t) => ({ writes: t.writes, opts: t.opts, theme: t.options.theme, disposed: t.disposed,
      focused: t.focused })),
    head: head.children.map((c) => c.src || c.href || c.attrs.src || c.attrs.href),
    value: job.eval ? vm.runInContext(job.eval, ctx) : undefined,
  };
}

(async () => {
  const jobs = JSON.parse(fs.readFileSync(0, "utf8"));
  const out = [];
  for (const job of jobs) out.push(await run(job));
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
