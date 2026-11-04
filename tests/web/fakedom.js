// A small stand-in DOM for running the web UI's scripts under node, with no dependencies: enough for markdown.js
// and app.js, plus html() to serialize what they built. Setting innerHTML throws: the UI must never use it.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const VOID = new Set(["br", "hr"]);

class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parent = null; this.attrs = {}; this.style = {}; this.listeners = {};
    this.isConnected = true; this.hidden = false; this.disabled = false; this.open = false; this.className = "";
    this.value = ""; this.checked = false; this.scrollHeight = 0; this._text = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  set innerHTML(v) { throw new Error("innerHTML was set"); }
  set data(v) { this._text = String(v); }
  get data() { return this._text; }
  appendData(t) { this._text += String(t); }
  append(...k) {
    for (const x of k) {
      const n = typeof x === "string" ? text(x) : x;
      n.parent = this;
      this.children.push(n);
    }
  }
  appendChild(k) { this.append(k); return k; }
  replaceChildren(...k) { this.children = []; this._text = ""; this.append(...k); }
  prepend(...k) { this.children.unshift(...k); }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  replaceWith(n) {
    if (!this.parent) return;
    n.parent = this.parent;
    this.parent.children[this.parent.children.indexOf(this)] = n;
    this.parent = null;
  }
  before() {} after() {}
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  removeEventListener() {}
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  querySelector() { return null; } querySelectorAll() { return []; }
  scrollIntoView() {} focus() {} blur() {} select() {}
}

function text(t) { const n = new El("#text"); n._text = String(t); return n; }

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// The node as HTML: class, attributes, `open`, and style.textAlign; text escaped.
function html(n) {
  if (n.tag === "#text") return esc(n._text);
  let a = n.className ? ` class="${esc(n.className)}"` : "";
  for (const [k, v] of Object.entries(n.attrs)) a += ` ${k}="${esc(v)}"`;
  if (n.open) a += " open";
  if (n.style && n.style.textAlign) a += ` style="text-align: ${esc(n.style.textAlign)}"`;
  if (VOID.has(n.tag)) return `<${n.tag}${a}>`;
  return `<${n.tag}${a}>${esc(n._text)}${n.children.map(html).join("")}</${n.tag}>`;
}

// Every element under n: [{tag, attrs, className}].
function elements(n, out = []) {
  if (n.tag !== "#text") out.push({ tag: n.tag, attrs: n.attrs, className: n.className });
  for (const c of n.children) elements(c, out);
  return out;
}

// Runs the page's scripts (app.js without its boot()) in a fresh context; fetch is the page's fetch.
function load(staticDir, fetch) {
  const document = {
    createElement: (t) => new El(t), createTextNode: text, createDocumentFragment: () => new El("#frag"),
    querySelector: () => new El("div"), querySelectorAll: () => [], documentElement: { scrollHeight: 0 },
    body: new El("body"), addEventListener() {}, visibilityState: "visible",
  };
  const window = { innerHeight: 0, scrollY: 0, scrollTo() {}, addEventListener() {}, location: { hash: "" } };
  // FormData as uploads build it: what was appended, a file by its name.
  class FormData {
    constructor() { this.entries = []; }
    append(k, v, name) { this.entries.push([k, name || (v && v.name) || v]); }
  }
  const ctx = vm.createContext({
    document, window, fetch: fetch || (async () => { throw new TypeError("offline"); }), console, setTimeout,
    clearTimeout, URL, location: window.location, matchMedia: () => ({ matches: false }), addEventListener() {},
    history: { replaceState() {} }, navigator: {}, WebSocket: function () {}, FormData,
  });
  // Every script index.html loads, in its order (markdown.js, app.js, then the screens in their own files).
  const page = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
  for (const [, file] of page.matchAll(/<script src="\/static\/([\w.-]+\.js)"/g)) {
    let code = fs.readFileSync(path.join(staticDir, file), "utf8");
    if (file === "app.js") code = code.replace(/\ndocument\.addEventListener\("DOMContentLoaded", \(\) => boot\(\)\);.*\s*$/, "\n");
    vm.runInContext(code, ctx);
  }
  ctx.S = vm.runInContext("S", ctx);
  return ctx;
}

module.exports = { El, text, html, elements, load };
