// Always-on, in the web UI: Settings › Triggers (schedules and webhooks), what an agent is doing and what wakes it
// next (the agents list and its page), its recent trigger runs, and valet.owner's messages and questions in the
// inbox and in conversations. Registers with app.js's hooks (addSettingsSection, addAgentLine, addAgentSection,
// addAskView); loaded after app.js, before boot().
"use strict";

// ---------- times ----------

function pad2(n) { return String(n).padStart(2, "0"); }
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// A moment as a reader wants it, in this device's time: "today 08:00", "tomorrow 08:00", "Mon 08:00", "3 Nov 08:00".
function whenOf(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const clock = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((day(d) - day(now)) / 86400000);
  if (days === 0) return "today " + clock;
  if (days === 1) return "tomorrow " + clock;
  if (days === -1) return "yesterday " + clock;
  if (days > 1 && days < 7) return WEEKDAYS[d.getDay()] + " " + clock;
  return d.getDate() + " " + MONTHS[d.getMonth()] + " " + clock;
}

// How far off a moment is: "in 5 min", "in 3 h", "in 2 days".
function inOf(ts) {
  const s = ts - Date.now() / 1000;
  if (s < 60) return "in under a minute";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  if (s < 86400) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} days`;
}

// "Schedules follow <zone> time", and that the times shown are this device's when its zone is another.
function zoneNote(zone) {
  if (!zone) return "";
  let here = "";
  try { here = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { /* unknown: say it */ }
  return `Schedules follow ${zone} time` + (here === zone ? "." : "; the times here are this device's.");
}

// Agent.next_trigger -> "Next: Every day at 08:00, tomorrow 08:00".
function nextLine(t) {
  if (!t) return "";
  const what = t.kind === "wakeup" ? "a wake-up it set" : t.label || "a schedule";
  return `Next: ${what}, ${whenOf(t.at)}`;
}

// ---------- the agents list and page: what it's doing, what's waiting, what's next ----------

function activityText(a) {
  if (!a.activity || (a.status !== "working" && a.status !== "waiting")) return "";
  if (a.activity === "thinking") return "Thinking…";
  if (a.activity === "writing") return "Writing…";
  return a.activity;
}

addAgentLine((a) => {
  const parts = [];
  const doing = activityText(a);
  if (doing) parts.push(h("span", { class: "activity", text: doing }));
  if (a.queued) parts.push(h("span", { text: a.queued === 1 ? "1 prompt waiting" : `${a.queued} prompts waiting` }));
  if (a.next_trigger && a.desired === "running") parts.push(h("span", { class: "next", text: nextLine(a.next_trigger) }));
  if (!parts.length) return null;
  const out = [];
  parts.forEach((p, i) => { if (i) out.push(" · "); out.push(p); });
  return h("div", { class: "always-on sub", "data-row": "always-on-" + a.name }, out);
});

// ---------- the agent page: its triggers and their recent runs ----------

const OUTCOMES = { started: ["ran", "ran"], queued: ["queued", "queued"], skipped: ["skipped", "skipped"],
  missed: ["missed", "missed"], failed: ["failed", "failed"], firing: ["sending", "sending"] };

function runEl(r) {
  const [cls, text] = OUTCOMES[r.outcome] || ["", r.outcome];
  const when = r.scheduled_for || r.fired_at;
  const times = r.count > 1 ? ` · ${r.count} times, the last ${whenOf(when)}` : ` · ${whenOf(when)}`;
  return h("div", { class: "run", "data-row": "run-" + r.id },
    h("div", { class: "row between" }, h("span", { class: "grow", text: (r.label || r.trigger) + times }),
      h("span", { class: "badge run-" + cls, text })),
    r.error && h("div", { class: "sub", text: r.error }));
}

addAgentSection((name) => {
  const base = `/api/agents/${enc(name)}`;
  const box = h("div");
  const el = h("div", { class: "triggers-part", hidden: true },
    h("div", { class: "row between section-head" }, h("h2", { text: "Triggers" }),
      h("a", { class: "button small", href: `#/a/${enc(name)}/settings`, text: "Edit" })), box);
  let seen = null, timer = 0;

  async function load() {
    let info, runs;
    try {
      [info, runs] = await Promise.all([api("GET", base + "/triggers"), api("GET", base + "/trigger-runs?limit=10")]);
    } catch { return; }
    const triggers = info.triggers || [], wakeups = info.wakeups || [], list = runs.runs || [];
    const running = (S.agents.get(name) || {}).desired === "running";
    el.hidden = !triggers.length && !wakeups.length && !list.length;
    fill(box, h("div", { class: "card trigger-list" },
      triggers.map((t) => h("div", { class: "trow", "data-row": "trigger-" + t.trigger },
        h("strong", { text: t.kind === "schedule" ? t.description : `Webhook ${t.webhook}` }),
        h("div", { class: "sub", text: [!running ? "skipped while it's stopped"
          : t.kind === "schedule" && t.next && t.next.length ? "next " + whenOf(t.next[0]) : "",
          t.session === "new" ? "a new session each time" : "in its main session"].filter(Boolean).join(" · ") }))),
      wakeups.map((w) => h("div", { class: "trow", "data-row": "wakeup-" + w.id },
        h("strong", { text: "Wake-up " + whenOf(w.due_at) }), h("div", { class: "sub", text: brief(w.prompt, 160) }))),
      !triggers.length && !wakeups.length && h("p", { class: "sub", text: "No schedules, webhooks or wake-ups." }),
      running && triggers.some((t) => t.kind === "schedule") && h("p", { class: "sub hint-line",
        text: zoneNote(info.timezone) })),
      list.length > 0 && h("div", { class: "card runs" }, h("div", { class: "sub", text: "Recent runs" }), list.map(runEl)));
  }

  load();
  return {
    el,
    frame(f) {
      if (f.type === "hello") { load(); return; }
      if (f.type !== "agent" || f.agent.name !== name) return;
      // A trigger that fires moves next_trigger or starts a turn: look again then, not on every frame.
      const key = JSON.stringify([f.agent.next_trigger, f.agent.status, f.agent.desired, f.agent.queued]);
      if (key === seen) return;
      seen = key;
      clearTimeout(timer);
      timer = setTimeout(load, 500);
    },
  };
});

// ---------- Settings › Triggers ----------

const SCHEDULE_PRESETS = [["Every morning", "0 8 * * *"], ["Weekdays at 9", "0 9 * * 1-5"], ["Every hour", "0 * * * *"],
  ["Every 15 minutes", "*/15 * * * *"], ["Mondays at 9", "0 9 * * 1"]];

function copyText(text, btn) {
  const done = (ok) => {
    btn.textContent = ok ? "Copied" : "Select and copy it";
    setTimeout(() => { btn.textContent = "Copy"; }, 2000);
  };
  try {
    navigator.clipboard.writeText(text).then(() => done(true), () => done(false));
  } catch { done(false); }
}

addSettingsSection({ key: "triggers", title: "Triggers", after: "secrets", make: (ctx) => {
  const el = h("div", { class: "card triggers" });
  let info = null, infoErr = "", infoFor = null, loading = false;
  const previews = new Map();       // schedule expression -> {description, next} | {error}
  const shownKeys = new Set();      // webhooks whose key is shown in full
  const asks = new Map();           // webhook -> its confirmBox, kept across redraws
  let timers = new Map();

  const list = () => ctx.draft.triggers || (ctx.draft.triggers = []);
  const savedOf = (t) => info && info.triggers.find((x) => (t.webhook ? x.webhook === t.webhook : x.schedule === t.schedule));

  // GET <agent>/triggers: the saved triggers with their next runs, webhook URLs and keys. Again whenever the saved
  // triggers change (a save), not on every redraw.
  async function loadInfo() {
    const want = JSON.stringify((ctx.saved || {}).triggers || []);
    if (want === infoFor || loading) return;
    loading = true;
    try {
      info = await api("GET", `/api/agents/${enc(ctx.name)}/triggers`);
      infoErr = "";
      for (const t of info.triggers) if (t.schedule) previews.set(t.schedule, { description: t.description, next: t.next });
    } catch (x) { infoErr = x.message; }
    infoFor = want;
    loading = false;
    draw();
  }

  function previewEl(expr) {
    const p = previews.get(expr);
    if (!p) return h("p", { class: "sub hint-line", text: expr.trim() ? "…" : "" });
    if (p.error) return h("p", { class: "err", text: p.error });
    return h("p", { class: "sub hint-line describe" }, h("strong", { text: p.description }),
      p.next && p.next.length ? " · next " + p.next.map(whenOf).join(", ") : "");
  }

  // The schedule's meaning while it's typed (GET /api/triggers/preview), a moment after the last keystroke.
  function preview(i, slot) {
    clearTimeout(timers.get(i));
    timers.set(i, setTimeout(async () => {
      const expr = (list()[i] || {}).schedule || "";
      if (!expr.trim()) { slot.replaceChildren(); return; }
      if (!previews.has(expr)) {
        try {
          const p = await api("GET", "/api/triggers/preview?schedule=" + enc(expr));
          previews.set(expr, { description: p.description, next: p.next });
        } catch (x) {
          if (x.status !== 422) return;
          previews.set(expr, { error: x.message });
        }
      }
      if ((list()[i] || {}).schedule === expr) slot.replaceChildren(previewEl(expr));
    }, 300));
  }

  function hookEl(t, i) {
    const saved = savedOf(t);
    if (!saved || !t.webhook) return h("p", { class: "sub hint-line", text: "Save to get its address and key." });
    if (saved.key_error || !saved.key) return h("p", { class: "err", text: saved.key_error || "No key." });
    const ask = asks.get(t.webhook) || asks.set(t.webhook, confirmBox()).get(t.webhook);
    const err = h("p", { class: "err", hidden: true });
    const show = shownKeys.has(t.webhook);
    const copyUrl = h("button", { type: "button", class: "small", text: "Copy", onclick: () => copyText(saved.url, copyUrl) });
    const copyKey = h("button", { type: "button", class: "small", text: "Copy", onclick: () => copyText(saved.key, copyKey) });
    const rotate = async () => {
      try {
        const r = await api("POST", `/api/agents/${enc(ctx.name)}/webhooks/${enc(t.webhook)}/key`);
        Object.assign(saved, { key: r.key, key_created_at: r.key_created_at });
        shownKeys.add(t.webhook);
        draw();
      } catch (x) { err.textContent = x.message; err.hidden = false; }
    };
    return h("div", { class: "hook" },
      h("label", { text: "Address" }),
      h("div", { class: "row hook-line" }, h("div", { class: "mono grow", "data-field": `trigger-${i}-url`, text: saved.url }),
        copyUrl),
      h("label", { text: "Key" }),
      h("div", { class: "row hook-line" },
        h("div", { class: "mono grow", "data-field": `trigger-${i}-key`,
          text: show ? saved.key : saved.key.slice(0, 8) + "••••••••" }),
        h("button", { type: "button", class: "small", text: show ? "Hide" : "Show", onclick: () => {
          if (show) shownKeys.delete(t.webhook); else shownKeys.add(t.webhook);
          draw();
        } }),
        copyKey,
        h("button", { type: "button", class: "small", text: "New key", onclick: () => ask.ask(
          "Make a new key? The old one stops working at once, so update the sender.", "New key", rotate, "danger") })),
      ask.el, err,
      h("p", { class: "sub hint-line", text: "Send it as Authorization: Bearer <key>, as ?key=<key> in the address, or "
        + "as the secret of a GitHub webhook (it checks X-Hub-Signature-256). What the call sends comes with the "
        + "prompt, marked as data from outside." }));
  }

  function lastRunEl(t) {
    const saved = savedOf(t);
    const r = saved && saved.last_run;
    if (!r) return null;
    const [, text] = OUTCOMES[r.outcome] || ["", r.outcome];
    return h("p", { class: "sub hint-line", text: `Last: ${text} ${whenOf(r.scheduled_for || r.fired_at)}`
      + (r.error ? ` (${r.error})` : "") });
  }

  function triggerEl(t, i) {
    const key = `triggers.${i}`;
    const field = (name, props) => h("input", { autocomplete: "off", "data-field": `trigger-${i}-${name}`, ...props });
    const set = (k, v) => { list()[i][k] = v; ctx.changed(`${key}.${k}`); };
    const slot = h("div");
    let top;
    if (t.schedule != null) {
      const input = field("schedule", { class: "mono", value: t.schedule, placeholder: "0 8 * * *", maxLength: 200,
        "aria-label": "Schedule", autocapitalize: "none",
        oninput: () => { set("schedule", input.value); preview(i, slot); } });
      input.setAttribute("spellcheck", "false");
      slot.replaceChildren(previewEl(t.schedule));
      if (!previews.has(t.schedule)) preview(i, slot);
      top = [h("label", { text: "When" }), input, slot, ctx.errFor(`${key}.schedule`),
        h("div", { class: "chips presets" }, SCHEDULE_PRESETS.map(([label, expr]) =>
          chip(`trigger-${i}-preset:${expr}`, t.schedule === expr, label, () => {
            input.value = expr;
            set("schedule", expr);
            preview(i, slot);
            draw();
          })))];
    } else {
      const input = field("webhook", { class: "mono", value: t.webhook || "", placeholder: "github-push", maxLength: 31,
        "aria-label": "Webhook name", autocapitalize: "none",
        oninput: () => set("webhook", input.value.trim()) });
      input.setAttribute("spellcheck", "false");
      top = [h("label", { text: "Webhook name" }), input, ctx.errFor(`${key}.webhook`), hookEl(t, i)];
    }
    const prompt = h("textarea", { rows: 3, value: t.prompt || "", "data-field": `trigger-${i}-prompt`,
      "aria-label": "Prompt", placeholder: t.schedule != null ? "What it's asked each time"
        : "Optional: what to do with what the call sends",
      oninput: () => { set("prompt", prompt.value || null); grow(prompt); } });
    setTimeout(() => grow(prompt), 0);
    const session = t.session || "main";
    return h("div", { class: "trigger", "data-row": "trigger-" + i },
      h("div", { class: "row between" }, h("strong", { text: t.schedule != null ? "Schedule" : "Webhook" }),
        h("button", { type: "button", class: "small", text: "Remove", onclick: () => {
          list().splice(i, 1);
          ctx.changed("triggers");
          draw();
        } })),
      h("div", { class: "field" }, top),
      h("div", { class: "field" }, h("label", { text: "Prompt" }), prompt, ctx.errFor(`${key}.prompt`)),
      h("div", { class: "field" }, h("label", { text: "Where it goes" }),
        h("div", { class: "chips" },
          chip(`trigger-${i}-session:main`, session === "main", "Its main session", () => { set("session", "main"); draw(); }),
          chip(`trigger-${i}-session:new`, session === "new", "A new session each time", () => { set("session", "new"); draw(); }))),
      lastRunEl(t), ctx.errFor(key));
  }

  function draw() {
    const d = ctx.draft;
    if (!d) return;
    loadInfo();
    const ts = d.triggers || [];
    const zone = info && info.timezone ? " " + zoneNote(info.timezone) : "";
    fill(el,
      h("p", { class: "sub intro", text: "What wakes it without you. Each prompt goes to its main session (one "
        + "conversation it keeps) or a new session, and starts with a line saying where it came from. Triggers "
        + "fire only while it's running. A saved change fires at once; the agent's own description of its "
        + "triggers updates when it restarts." + zone }),
      ts.map(triggerEl), ctx.errFor("triggers"),
      infoErr && h("p", { class: "err", text: "Couldn't load the webhooks' addresses: " + infoErr }),
      h("div", { class: "row add-trigger" },
        h("button", { type: "button", class: "small", text: "Add a schedule", onclick: () => {
          list().push({ schedule: "0 8 * * *", webhook: null, prompt: "", session: "main" });
          ctx.changed("triggers");
          draw();
        } }),
        h("button", { type: "button", class: "small", text: "Add a webhook", onclick: () => {
          const names = new Set(list().map((t) => t.webhook));
          let n = 1;
          while (names.has(n === 1 ? "hook" : `hook-${n}`)) n++;
          list().push({ schedule: null, webhook: n === 1 ? "hook" : `hook-${n}`, prompt: null, session: "main" });
          ctx.changed("triggers");
          draw();
        } })));
  }

  return { el, draw };
} });

// ---------- valet.owner in the inbox and in conversations ----------

async function answerOwner(id, body, controls, errEl) {
  controls.forEach((c) => { c.disabled = true; });
  errEl.hidden = true;
  try {
    putInbox(await api("POST", `/api/inbox/${enc(id)}/answer`, body));
  } catch (x) {
    if (x.status === 409) {
      try { putInbox(await api("GET", `/api/inbox/${enc(id)}`)); return; } catch { /* fall through */ }
    }
    errEl.textContent = x.message;
    errEl.hidden = false;
    controls.forEach((c) => { c.disabled = false; });
  }
}

// What the owner answered, for a closed item.
function ownerResult(p) {
  if (p.status === "expired") return "Expired";
  if (p.status !== "answered") return p.status === "pending" ? "" : "Closed";
  const o = p.outcome || {};
  if (o.optionId === "dismiss") return "Dismissed";
  const opt = (p.options || []).find((x) => x.optionId === o.optionId);
  const parts = [opt && opt.name, o.text].filter(Boolean);
  return parts.length ? "You answered: " + parts.join(" · ") : "Answered";
}

function ownerItemEl(p, extra) {
  const pending = p.status === "pending";
  const who = (p.agent && agentName(p.agent)) || "The agent";
  const question = p.kind === "question";
  const text = p.text || p.title || (p.toolCall && p.toolCall.title) || "";
  const errEl = h("p", { class: "err", hidden: true });
  let answerBox = null;
  if (pending) {
    const opts = p.options || [];
    const words = question && h("textarea", { rows: 2, placeholder: "Answer in your own words",
      "aria-label": "Your answer", "data-field": "answer-" + p.id, oninput: () => grow(words) });
    const buttons = opts.map((o) => h("button", { type: "button", class: o.optionId === "dismiss" ? "" : "primary",
      text: o.name, "data-option": o.optionId }));
    const send = question && h("button", { type: "button", class: "primary", text: "Send" });
    const controls = [...buttons, ...(send ? [send] : []), ...(words ? [words] : [])];
    buttons.forEach((b, i) => b.addEventListener("click", () => {
      const body = { option_id: opts[i].optionId };
      const typed = words ? words.value.trim() : "";
      if (typed && opts[i].optionId !== "dismiss") body.text = typed;
      answerOwner(p.id, body, controls, errEl);
    }));
    if (send) send.addEventListener("click", () => {
      const typed = words.value.trim();
      if (!typed) { errEl.textContent = "Write an answer, or pick one of the choices."; errEl.hidden = false; return; }
      answerOwner(p.id, { text: typed }, controls, errEl);
    });
    answerBox = h("div", { class: "ask" },
      words && h("div", { class: "row answer-words" }, words, send),
      h("div", { class: "options" }, buttons), errEl);
  }
  const result = ownerResult(p);
  return h("div", { class: "perm owner-item " + p.kind + (pending ? "" : " closed"), "data-row": "item-" + p.id }, extra,
    h("div", { class: "title", text: who + (question ? " asks" : " says") }),
    h("div", { class: "md owner-text" }, markdown(text)),
    answerBox,
    result && h("div", { class: "result", text: result }));
}

addAskView("message", (p, head) => ownerItemEl(p, head));
addAskView("question", (p, head) => ownerItemEl(p, head));
