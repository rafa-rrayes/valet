"use strict";
// The agent's terminal (docs/API.md, "The agent's computer"): #/a/<name>/terminal, shells in its computer (its own
// container, or its working folder on a host agent's runner), each on its own /ws/terminal/<name> socket. Several can
// be open (tabs); closing one, or leaving the screen, ends its shell. xterm.js (static/vendor/xterm, see VERSIONS)
// draws them and is loaded only here. On a phone, a key bar gives the keys a phone keyboard lacks.

const TERM_MAX = 8;                 // the runner's limit per agent
const TERM_VENDOR = "/static/vendor/xterm/";
const TERM_KEYS = [                 // [label, what it sends] for the key bar; "ctrl" is a modifier for the next key
  ["Esc", "\x1b"], ["Tab", "\t"], ["Ctrl", "ctrl"], ["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"],
  ["→", "\x1b[C"], ["|", "|"], ["~", "~"], ["/", "/"], ["-", "-"],
];
const TERM_THEMES = {
  light: { background: "#ffffff", foreground: "#14161a", cursor: "#1f8a5b", cursorAccent: "#ffffff",
    selectionBackground: "#cfe9dc" },
  dark: { background: "#0b0d11", foreground: "#e8eaee", cursor: "#5cc994", cursorAccent: "#0b0d11",
    selectionBackground: "#2a4a3a" },
};

let termLoading = null;

// xterm.js and its fit addon, once, the first time a terminal is opened.
function termLoad() {
  if (globalThis.Terminal && globalThis.FitAddon) return Promise.resolve();
  if (!termLoading) {
    termLoading = new Promise((resolve, reject) => {
      document.head.append(h("link", { rel: "stylesheet", href: TERM_VENDOR + "xterm.css" }));
      let left = 2;
      const fail = () => { termLoading = null; reject(new Error("The terminal couldn't load. Reload the page.")); };
      for (const file of ["xterm.js", "addon-fit.js"]) {
        document.head.append(h("script", { src: TERM_VENDOR + file, onload: () => { if (--left === 0) resolve(); },
          onerror: fail }));
      }
    });
  }
  return termLoading;
}

function termDark() { return matchMedia("(prefers-color-scheme: dark)").matches; }

function termSocketUrl(name, cols, rows) {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host
    + `/ws/terminal/${enc(name)}?cols=${cols}&rows=${rows}`;
}

addRoute((parts) => (parts[0] === "a" && parts[1] && parts[2] === "terminal" ? TerminalView(parts[1]) : null));

addAgentAction((a) => a.level && a.level !== "none"
  && h("a", { class: "button small", href: `#/a/${enc(a.name)}/terminal`, text: "Terminal" }));

function TerminalView(name) {
  const terms = [];             // {n, el, term, fit, ws, state: "connecting"|"open"|"ended", status, tab}
  let current = null, counter = 0, ctrl = false, loadErr = "";
  const tabs = h("div", { class: "term-tabs", role: "tablist" });
  const screens = h("div", { class: "term-screens" });
  const notice = h("div");
  const status = h("div", { class: "term-status sub", "data-row": "term-status" });
  // A key bar button never takes the focus: the shell keeps it, and a phone keeps its keyboard up.
  const keyBtn = ([label, seq]) => h("button", { type: "button", class: "small", text: label, "data-key": label,
    onpointerdown: (e) => e.preventDefault(), onclick: () => key(seq) });
  const keyBtns = TERM_KEYS.map(keyBtn);
  const ctrlBtn = keyBtns[TERM_KEYS.findIndex(([, seq]) => seq === "ctrl")];
  const keys = h("div", { class: "term-keys", "aria-label": "Keys" }, keyBtns);
  const newBtn = h("button", { type: "button", class: "small", text: "New terminal", onclick: () => open() });
  const where = h("p", { class: "sub where" });
  const a0 = S.agents.get(name);
  const el = h("section", { class: "terminal-view" },
    h("a", { class: "back", href: "#/a/" + enc(name), text: "‹ " + (agentTitle(a0) || name) }),
    h("div", { class: "row between" }, h("h1", { class: "grow", text: "Terminal" }), newBtn),
    where, notice, h("div", { class: "term-bar row" }, tabs), screens, status, keys);

  function agent() { return S.agents.get(name); }
  function up() { const a = agent(); return !!a && a.level !== "none" && a.state === "running" && a.connected; }

  function drawHead() {
    const a = agent();
    where.textContent = !a ? "" : a.level === "container" ? "A shell in its container, as its user, in /work."
      : a.level === "host" ? `A shell on ${a.runner || "its runner"}'s machine, in its working folder.` : "";
    if (a && a.level === "none") {
      fill(notice, h("p", { class: "note", text: "It has no computer (level none): no terminal." }));
    } else if (!up()) {
      fill(notice, h("div", { class: "note row between" },
        h("span", { class: "grow", text: a && a.desired === "running" && !a.connected ? "Its runner is offline."
          : "It isn't running: start it to open a terminal." }),
        a && a.state !== "starting" && agentButtons(a, startTrouble())));
    } else notice.replaceChildren();
    if (loadErr) notice.append(h("p", { class: "err", text: loadErr }));
    newBtn.disabled = !up() || terms.filter((t) => t.state !== "ended").length >= TERM_MAX;
  }

  function drawTabs() {
    fill(tabs, terms.map((t) => {
      const label = h("button", { type: "button", class: "tab" + (t === current ? " on" : "")
        + (t.state === "ended" ? " ended" : ""), role: "tab", "aria-selected": String(t === current),
        text: `Shell ${t.n}`, "data-row": "term-tab:" + t.n, onclick: () => show(t) });
      const x = h("button", { type: "button", class: "tab-x", "aria-label": `Close shell ${t.n}`, text: "×",
        "data-row": "term-close:" + t.n, onclick: () => closeTerm(t) });
      return h("span", { class: "term-tab" }, label, x);
    }));
    drawStatus();
    drawHead();
  }

  function drawStatus() {
    const t = current;
    status.textContent = !t ? (terms.length ? "" : "No shell open.") : t.status;
  }

  function show(t) {
    current = t;
    for (const x of terms) x.el.hidden = x !== t;
    drawTabs();
    if (t && t.term) { fitTerm(t); t.term.focus(); }
  }

  function fitTerm(t) {
    if (!t.fit || t.el.hidden) return;
    try { t.fit.fit(); } catch { /* not laid out yet */ }
  }

  function send(t, data) {
    if (t.ws && t.ws.readyState === 1) t.ws.send(typeof data === "string" ? new TextEncoder().encode(data) : data);
  }

  // The key bar: a key straight to the shell; Ctrl applies to the next key typed or tapped.
  function key(seq) {
    const t = current;
    if (!t || t.state !== "open") return;
    if (seq === "ctrl") {
      setCtrl(!ctrl);
      return;
    }
    send(t, ctrl && seq.length === 1 ? ctrlOf(seq) : seq);
    setCtrl(false);
  }

  function setCtrl(on) {
    ctrl = on;
    ctrlBtn.className = "small" + (on ? " on" : "");
    ctrlBtn.setAttribute("aria-pressed", String(on));
  }

  function ctrlOf(ch) {
    const c = ch.toUpperCase().charCodeAt(0);
    return c >= 64 && c <= 95 ? String.fromCharCode(c - 64) : ch;
  }

  function ended(t, text) {
    if (t.state === "ended") return;
    t.state = "ended";
    t.status = text;
    if (t.term) t.term.options.cursorBlink = false;
    drawTabs();
  }

  async function open() {
    if (!up() || terms.filter((t) => t.state !== "ended").length >= TERM_MAX) return;
    const t = { n: ++counter, el: h("div", { class: "term-screen" }), term: null, fit: null, ws: null,
      state: "connecting", status: "Connecting…" };
    terms.push(t);
    screens.append(t.el);
    show(t);
    try {
      await termLoad();
    } catch (x) {
      loadErr = x.message;
      ended(t, x.message);
      return;
    }
    if (t.state === "ended" || !el.isConnected) return;
    const term = new globalThis.Terminal({ cursorBlink: true, fontSize: 13, scrollback: 5000, allowProposedApi: false,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', theme: TERM_THEMES[termDark() ? "dark" : "light"] });
    const fit = new globalThis.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(t.el);
    t.term = term;
    t.fit = fit;
    fitTerm(t);
    const ws = new WebSocket(termSocketUrl(name, term.cols, term.rows));
    ws.binaryType = "arraybuffer";
    t.ws = ws;
    term.onData((d) => {
      if (ctrl && d.length === 1) { d = ctrlOf(d); setCtrl(false); }
      send(t, d);
    });
    term.onBinary((d) => send(t, Uint8Array.from(d, (c) => c.charCodeAt(0))));
    term.onResize(({ cols, rows }) => {
      if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols, rows }));
    });
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") { term.write(new Uint8Array(e.data)); return; }
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      if (m.type === "ready") {
        t.state = "open";
        t.status = "";
        drawTabs();
        if (t === current) term.focus();
      } else if (m.type === "exit") {
        ended(t, m.reason ? "Closed: " + m.reason + "." : `The shell exited (${m.code}).`);
      } else if (m.type === "error") {
        ended(t, m.message);
      }
    };
    ws.onclose = (e) => {
      ended(t, e.code === 4401 ? "Sign in again." : e.code === 4403 ? "Refused: this page isn't Valet's own address."
        : t.state === "connecting" ? "Couldn't connect." : "Disconnected.");
    };
  }

  function closeTerm(t) {
    if (t.ws) { t.ws.onclose = null; try { t.ws.close(1000); } catch { /* closed */ } }
    if (t.term) t.term.dispose();
    t.el.remove();
    const i = terms.indexOf(t);
    terms.splice(i, 1);
    if (current === t) current = terms[Math.min(i, terms.length - 1)] || null;
    if (current) show(current); else drawTabs();
  }

  const onResize = () => { if (current) fitTerm(current); };
  const scheme = matchMedia("(prefers-color-scheme: dark)");
  const onScheme = () => {
    for (const t of terms) if (t.term) t.term.options.theme = TERM_THEMES[termDark() ? "dark" : "light"];
  };
  window.addEventListener("resize", onResize);
  if (scheme.addEventListener) scheme.addEventListener("change", onScheme);

  drawTabs();
  if (up()) open();
  return {
    el,
    frame(f) {
      if (f.type === "agent" && f.agent.name === name) {
        const was = !!terms.length;
        drawHead();
        if (up() && !was) open();
      } else if (f.type === "hello") drawHead();
    },
    close() {
      window.removeEventListener("resize", onResize);
      if (scheme.removeEventListener) scheme.removeEventListener("change", onScheme);
      for (const t of [...terms]) closeTerm(t);
    },
  };
}
