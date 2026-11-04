// Renders with the web UI's own code under the stand-in DOM (fakedom.js). Usage: node render.js <static dir>,
// with a JSON list of jobs on stdin; prints a JSON list of results, one per job:
//   {"md": text}                     -> {html, elements, ms}: markdown(text) in a div
//   {"events": [Event], "inbox": [InboxItem], "flush": "each"|"end"}
//                                    -> {html, elements}: the session log the Renderer builds from those events
"use strict";
const fs = require("fs");
const { html, elements, load } = require("./fakedom");

const ctx = load(process.argv[2]);
const jobs = JSON.parse(fs.readFileSync(0, "utf8"));
const out = jobs.map((job) => {
  const root = ctx.document.createElement("div");
  if (job.md != null) {
    const t = Date.now();
    root.append(...ctx.markdown(job.md));
    return { html: html(root), elements: elements(root).slice(1), ms: Date.now() - t };
  }
  ctx.S.inbox = new Map((job.inbox || []).map((i) => [i.id, i]));
  const r = ctx.Renderer(root);
  for (const ev of job.events) {
    r.apply(ev);
    if (job.flush === "each") r.flush();
  }
  r.flush();
  return { html: html(root), elements: elements(root).slice(1) };
});
console.log(JSON.stringify(out));
