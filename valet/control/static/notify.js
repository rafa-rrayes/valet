"use strict";
// #/notifications: how Valet tells you something waits in the inbox. A webhook (a Slack or Discord incoming
// webhook, or anything that takes a JSON POST) and the devices push goes to, which Valet's iPhone and Mac apps
// register themselves. It speaks GET|PUT /api/notifications, POST /api/notifications/test, GET /api/devices and
// DELETE /api/devices/{id} (docs/API.md, "Notifications"). Loaded after app.js, whose helpers it uses.

const DEVICE_KIND = { ios: "iPhone or iPad", macos: "Mac" };

function NotificationsView() {
  let info = null, infoErr = "";          // GET /api/notifications
  let devices = null, devicesErr = "";    // GET /api/devices
  let busy = false;
  let warned = false;                     // leaving with a URL typed and not saved: said so once
  const hookBox = h("div", { class: "card hook" }), pushBox = h("div", { class: "card push" });

  const url = h("input", { class: "mono", type: "url", placeholder: "https://hooks.slack.com/services/…",
    autocomplete: "off", autocapitalize: "none", "aria-label": "Webhook URL", "data-field": "webhook_url",
    oninput: () => { hookErr.hidden = true; hookMsg.hidden = true; drawButtons(); },
    onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); saveHook(); } } });
  url.setAttribute("spellcheck", "false");
  const hookErr = h("p", { class: "err", hidden: true, "data-err": "webhook_url" });
  const hookMsg = h("p", { class: "sub", hidden: true });
  const saveBtn = h("button", { type: "button", class: "small primary", text: "Save", onclick: () => saveHook() });
  const testBtn = h("button", { type: "button", class: "small", text: "Send test", onclick: () => sendTest() });
  const testOut = h("div", { class: "test-out" });

  const savedUrl = () => (info && info.webhook_url) || "";
  const typed = () => url.value.trim();

  function drawButtons() {
    saveBtn.disabled = busy || !info || typed() === savedUrl();
    testBtn.disabled = busy || !info;
  }

  function drawHook() {
    fill(hookBox,
      h("p", { class: "sub intro", text: "A URL Valet posts to when something new waits for you: a Slack or Discord "
        + "incoming webhook, or anything that takes JSON. The message names the agent and what it wants, never the "
        + "details, and links to the inbox." }),
      infoErr && h("p", { class: "err", text: infoErr }),
      !info && !infoErr && h("p", { class: "sub", text: "Loading…" }),
      h("label", { text: "Webhook URL" }), url, hookErr, hookMsg,
      h("div", { class: "row acts" }, saveBtn, testBtn),
      testOut);
    drawButtons();
  }

  // What the test did, each way it was sent: {webhook: null|{ok, error}, push: null|{sent, failed, removed, error}}.
  function showTest(r) {
    const lines = [];
    if (r.webhook) lines.push(r.webhook.ok ? ["ok", "Webhook: sent."] : ["bad", "Webhook: " + r.webhook.error]);
    else lines.push(["", "Webhook: none set."]);
    const p = r.push;
    if (!p) lines.push(["", "Push: not set up."]);
    else if (p.error) lines.push(["bad", "Push: " + p.error]);
    else if (!p.sent && !p.failed && !p.removed) lines.push(["", "Push: no devices yet."]);
    else {
      const parts = [p.sent && `sent to ${p.sent} device${p.sent > 1 ? "s" : ""}`,
        p.failed && `${p.failed} failed`,
        p.removed && `${p.removed} no longer registered, removed`].filter(Boolean);
      lines.push([p.failed && !p.sent ? "bad" : "ok", "Push: " + parts.join(", ") + "."]);
    }
    fill(testOut, lines.map(([cls, text]) => h("p", { class: "test-line " + cls, text })));
  }

  async function saveHook() {
    if (busy || !info) return false;
    busy = true;
    hookErr.hidden = hookMsg.hidden = true;
    drawButtons();
    let ok = false;
    try {
      info = await api("PUT", "/api/notifications", { webhook_url: typed() || null });
      url.value = savedUrl();
      hookMsg.textContent = savedUrl() ? "Saved." : "Removed: no webhook.";
      hookMsg.hidden = false;
      ok = true;
    } catch (x) {
      hookErr.textContent = x.message;
      hookErr.hidden = false;
    }
    busy = false;
    drawButtons();
    return ok;
  }

  // A URL typed but not saved is saved first: the test goes where notifications will.
  async function sendTest() {
    if (busy || !info) return;
    if (typed() !== savedUrl() && !(await saveHook())) return;
    busy = true;
    testBtn.textContent = "Sending…";
    drawButtons();
    fill(testOut);
    try { showTest(await api("POST", "/api/notifications/test")); } catch (x) {
      fill(testOut, h("p", { class: "err", text: x.message }));
    }
    busy = false;
    testBtn.textContent = "Send test";
    drawButtons();
  }

  function drawPush() {
    const p = info && info.push;
    const status = !p ? null
      : p.error ? h("div", { class: "note warn" }, h("strong", { text: "Push is off: " }), h("span", { text: p.error }))
        : p.configured ? h("p", { class: "sub" }, "Push is on, through Apple's ",
          p.sandbox ? "sandbox (development builds)" : "production service", " for ",
          h("span", { class: "mono", text: p.topic }), ".")
          : h("p", { class: "note" }, "Push isn't set up: the control plane needs your APNs key. Set ",
            h("span", { class: "mono", text: "VALET_APNS_KEY_FILE" }), ", ",
            h("span", { class: "mono", text: "VALET_APNS_KEY_ID" }), " and ",
            h("span", { class: "mono", text: "VALET_APNS_TEAM_ID" }), " where it runs, and restart it.");
    fill(pushBox,
      h("p", { class: "sub intro", text: "Valet's iPhone and Mac apps add themselves here when you let them notify "
        + "you. Each new item in the inbox is pushed to every device." }),
      status,
      devicesErr && h("p", { class: "err", text: devicesErr }),
      !devices && !devicesErr && h("p", { class: "sub", text: "Loading…" }),
      devices && !devices.length && h("p", { class: "sub", text: "No devices yet." }),
      devices && devices.length > 0 && h("div", { class: "rows" }, devices.map(deviceRow)));
  }

  function deviceRow(d) {
    const err = h("p", { class: "err", hidden: true });
    const btn = h("button", { type: "button", class: "small", text: "Remove" });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      err.hidden = true;
      try { await api("DELETE", `/api/devices/${enc(d.id)}`); } catch (x) {
        if (x.status !== 404) { err.textContent = x.message; err.hidden = false; btn.disabled = false; return; }
      }
      await loadDevices();
    });
    const seen = [DEVICE_KIND[d.platform] || d.platform, "added " + ago(d.created_at),
      d.last_push ? "last notified " + ago(d.last_push) : "not notified yet"].join(" · ");
    return h("div", { class: "device", "data-row": "device:" + d.id },
      h("div", { class: "row" }, h("span", { class: "grow" }, h("span", { class: "name", text: d.name }),
        h("span", { class: "sub", text: seen })), btn), err);
  }

  async function loadInfo() {
    const before = savedUrl();
    try {
      info = await api("GET", "/api/notifications");
      infoErr = "";
      if (typed() === before) url.value = savedUrl();     // unless the owner is typing another
    } catch (x) { infoErr = x.message; }
    drawHook();
    drawPush();
  }

  async function loadDevices() {
    try { devices = await api("GET", "/api/devices"); devicesErr = ""; } catch (x) { devicesErr = x.message; }
    drawPush();
  }

  drawHook();
  drawPush();
  loadInfo();
  loadDevices();
  const el = h("section", { class: "notifications" },
    h("a", { class: "back", href: "#/", text: "‹ Agents" }), h("h1", { text: "Notifications" }),
    h("h2", { text: "Webhook" }), hookBox,
    h("h2", { text: "Push" }), pushBox);
  return {
    el,
    leave() {
      // A URL typed and not saved: say so once; leaving again drops it.
      if (!info || warned || typed() === savedUrl()) return false;
      warned = true;
      hookErr.textContent = "This URL isn't saved: Save it, or leave again to drop it.";
      hookErr.hidden = false;
      return true;
    },
    frame(f) { if (f.type === "hello") { loadInfo(); loadDevices(); } },
  };
}

addRoute((parts) => (parts[0] === "notifications" ? NotificationsView() : null));
