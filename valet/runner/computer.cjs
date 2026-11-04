// The runner's helper inside an agent's container, for the owner's file browser and terminals (runner/files.py,
// runner/terminal.py): `docker exec -i <container> node -e <this file> <op> <json args>`. Every agent image has
// node: both harnesses are Node programs. It prints one JSON line, the result or {"error": text}; files.read's bytes
// follow that line. The runner checked the path's spelling already; here it's resolved (links followed) where it
// lives and checked against the roots again, and nothing is ever written through a link.
"use strict";
const fs = require("fs");
const path = require("path");

const MAX_ENTRIES = 2000;
const UPLOAD_RE = /^[0-9a-f]{8,64}$/;
const C = fs.constants;

class Refused extends Error {}
function fail(msg) { throw new Refused(msg); }

const CODES = {
  ENOENT: "no such file or folder", ENOTDIR: "not a folder", EISDIR: "it's a folder", EACCES: "permission denied",
  EPERM: "permission denied", ELOOP: "it's a link", EROFS: "read-only", ENOSPC: "no space left", EEXIST: "exists",
  EDQUOT: "no space left", ENAMETOOLONG: "the name is too long",
};
function why(e) { return e instanceof Refused ? e.message : CODES[e.code] || String(e.message || e); }

function norm(p) {
  if (typeof p !== "string" || !p.startsWith("/") || p.includes("\0")) fail("a path must be absolute");
  const parts = p.split("/").filter(Boolean);
  if (parts.some((x) => x === "." || x === "..")) fail("a path can't have . or .. in it");
  return "/" + parts.join("/");
}

function within(p, root) { return root === "/" || p === root || p.startsWith(root + "/"); }

// The root (with its real path) that holds a resolved path: the deepest one.
function rootOf(real, roots) {
  let best = null;
  for (const r of roots) if (r.real && within(real, r.real) && (!best || r.real.length > best.real.length)) best = r;
  return best;
}

function resolve(p, roots) {
  const n = norm(p);
  if (!roots.some((r) => within(n, r.path))) fail(`${n} is outside its folders`);
  let real;
  try { real = fs.realpathSync(n); } catch (e) { fail(why(e)); }
  const root = rootOf(real, roots);
  if (!root) fail(`${n} leads outside its folders`);
  return { n, real, root };
}

function typeOf(st) { return st.isDirectory() ? "dir" : st.isFile() ? "file" : "other"; }

function entry(dir, name, roots) {
  const full = path.join(dir, name);
  let st;
  try { st = fs.lstatSync(full); } catch { return null; }
  const link = st.isSymbolicLink();
  let type = typeOf(st);
  if (link) {
    type = "other";
    try {
      const real = fs.realpathSync(full);
      if (rootOf(real, roots)) { st = fs.statSync(real); type = typeOf(st); }
    } catch { /* dangling, or nowhere it may go: "other" */ }
  }
  return { name, type, link, size: st.size, mtime: st.mtimeMs / 1000 };
}

function byName(a, b) {
  const x = a.toLowerCase(), y = b.toLowerCase();
  return x < y ? -1 : x > y ? 1 : a < b ? -1 : a > b ? 1 : 0;
}

function list(args, roots) {
  const { n, real } = resolve(args.path, roots);
  const st = fs.statSync(real);
  const out = { path: n, type: typeOf(st), size: st.size, mtime: st.mtimeMs / 1000, entries: [], truncated: false };
  if (!st.isDirectory()) return out;
  let names = fs.readdirSync(real).filter((x) => !x.startsWith(".valet-upload-"));
  if (names.length > MAX_ENTRIES) {
    names = names.sort(byName).slice(0, MAX_ENTRIES);
    out.truncated = true;
  }
  out.entries = names.map((x) => entry(real, x, roots)).filter(Boolean)
    .sort((a, b) => ((a.type === "dir") === (b.type === "dir") ? byName(a.name, b.name) : a.type === "dir" ? -1 : 1));
  return out;
}

function read(args, roots) {
  const { real } = resolve(args.path, roots);
  const fd = fs.openSync(real, C.O_RDONLY | C.O_NOFOLLOW);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile()) fail("not a file");
    const buf = Buffer.alloc(Math.max(0, Math.min(args.length, st.size - args.offset)));
    let got = 0;
    while (got < buf.length) {
      const k = fs.readSync(fd, buf, got, buf.length - got, args.offset + got);
      if (!k) break;
      got += k;
    }
    return [{ size: st.size, eof: args.offset + got >= st.size, length: got }, buf.subarray(0, got)];
  } finally { fs.closeSync(fd); }
}

function checkName(name) {
  if (typeof name !== "string" || !name || name.includes("/") || name.includes("\0") || name === "."
      || name === ".." || Buffer.byteLength(name) > 255) fail("not a file name");
  if (name.startsWith(".valet-upload-")) fail("that name is Valet's own");
}

function lexists(p) {
  try { fs.lstatSync(p); return true; } catch { return false; }
}

function place(dir, tmp, name, exists) {
  if (exists === "rename") {
    const dot = name.lastIndexOf(".");
    const stem = dot > 0 ? name.slice(0, dot) : name, ext = dot > 0 ? name.slice(dot) : "";
    let candidate = name;
    for (let k = 2; lexists(path.join(dir, candidate)); k++) candidate = `${stem}-${k}${ext}`;
    fs.renameSync(tmp, path.join(dir, candidate));
    return candidate;
  }
  const target = path.join(dir, name);
  if (lexists(target)) {
    if (exists !== "replace") fail("exists");
    if (!fs.lstatSync(target).isFile()) fail(`${name} is a folder or a link: it can't be replaced`);
  }
  fs.renameSync(tmp, target);   // rename(2) replaces the entry itself: never writes through a link
  return name;
}

function write(args, roots) {
  const { n, real, root } = resolve(args.dir, roots);
  if (!fs.statSync(real).isDirectory()) fail("not a folder");
  if (!root.writable) fail(`${root.path} is read-only`);
  checkName(args.name);
  if (!UPLOAD_RE.test(args.upload)) fail("not an upload id");
  const tmp = path.join(real, ".valet-upload-" + args.upload);
  const data = fs.readFileSync(0);
  const fd = args.offset === 0 ? fs.openSync(tmp, C.O_WRONLY | C.O_CREAT | C.O_EXCL | C.O_NOFOLLOW, 0o644)
    : fs.openSync(tmp, C.O_WRONLY | C.O_APPEND | C.O_NOFOLLOW);
  try {
    if (fs.fstatSync(fd).size !== args.offset) fail("the upload is out of step");
    for (let off = 0; off < data.length;) off += fs.writeSync(fd, data, off, data.length - off);
  } finally { fs.closeSync(fd); }
  const size = args.offset + data.length;
  if (!args.final) return { size };
  try {
    return { path: (n === "/" ? "" : n) + "/" + place(real, tmp, args.name, args.exists), size };
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch { /* gone */ }
    throw e;
  }
}

function cancel(args, roots) {
  const { real } = resolve(args.dir, roots);
  if (!UPLOAD_RE.test(args.upload)) fail("not an upload id");
  try { fs.unlinkSync(path.join(real, ".valet-upload-" + args.upload)); } catch (e) { if (e.code !== "ENOENT") throw e; }
  return {};
}

// Each missing folder is made inside its parent's real (resolved, checked) path.
function mkdir(args, roots) {
  const n = norm(args.path);
  let top = null;
  for (const r of roots) if (within(n, r.path) && (!top || r.path.length > top.path.length)) top = r;
  if (!top) fail(`${n} is outside its folders`);
  let cur = top.path;
  let { real, root } = resolve(cur, roots);
  for (const part of n.slice(top.path.length).split("/").filter(Boolean)) {
    if (!root.writable) fail(`${root.path} is read-only`);
    try { fs.mkdirSync(path.join(real, part), 0o755); } catch (e) { if (e.code !== "EEXIST") throw e; }
    cur = (cur === "/" ? "" : cur) + "/" + part;
    ({ real, root } = resolve(cur, roots));
    if (!fs.statSync(real).isDirectory()) fail(`${cur} is not a folder`);
  }
  return { path: n };
}

// A terminal's processes: every one whose environment carries VALET_TERMINAL=<id> (its shell and what that started)
// gets SIGHUP, then SIGKILL if it's still there a second and a half later.
async function kill(args) {
  const mark = Buffer.from("VALET_TERMINAL=" + args.terminal + "\0");
  const marked = () => {
    const out = [];
    for (const pid of fs.readdirSync("/proc").filter((x) => /^\d+$/.test(x))) {
      if (Number(pid) === process.pid) continue;
      try {
        const env = Buffer.concat([fs.readFileSync(`/proc/${pid}/environ`), Buffer.from("\0")]);
        if (env.indexOf(mark) === 0 || env.indexOf(Buffer.concat([Buffer.from("\0"), mark])) >= 0) out.push(Number(pid));
      } catch { /* gone, or not ours to read */ }
    }
    return out;
  };
  const pids = marked();
  for (const pid of pids) { try { process.kill(pid, "SIGHUP"); } catch { /* gone */ } }
  for (let i = 0; i < 15 && marked().length; i++) await new Promise((r) => setTimeout(r, 100));
  for (const pid of marked()) { try { process.kill(pid, "SIGKILL"); } catch { /* gone */ } }
  return { killed: pids.length };
}

async function main() {
  const op = process.argv[1], args = JSON.parse(process.argv[2] || "{}");
  const roots = (args.roots || []).map((r) => {
    let real = null;
    try { real = fs.realpathSync(norm(r.path)); } catch { /* a root that isn't there holds nothing */ }
    return { ...r, path: norm(r.path), real };
  });
  let out, bytes = null;
  try {
    if (op === "list") out = list(args, roots);
    else if (op === "read") [out, bytes] = read(args, roots);
    else if (op === "write") out = write(args, roots);
    else if (op === "cancel") out = cancel(args, roots);
    else if (op === "mkdir") out = mkdir(args, roots);
    else if (op === "kill") out = await kill(args);
    else fail("unknown op " + op);
  } catch (e) {
    out = { error: why(e) };
  }
  process.stdout.write(JSON.stringify(out) + "\n");
  if (bytes && bytes.length) process.stdout.write(bytes);
}

main();
