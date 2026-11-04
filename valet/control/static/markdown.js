"use strict";
// Markdown for agent output, which is untrusted. It builds DOM nodes and never parses HTML: raw HTML in the text
// stays text, and links are made only to http(s) URLs. It covers what agents write: headings, paragraphs,
// emphasis, code spans and fenced blocks, lists, quotes, rules, tables and links. It's a pure function of the
// text, so a streamed message is simply rendered again as it grows.

const MD_DEPTH = 8;  // nesting limit (quotes, lists, emphasis), so hostile input can't recurse without end

const MD_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const MD_HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const MD_QUOTE = /^ {0,3}> ?/;
const MD_ITEM = /^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*)|$)/;
const MD_TABLE_SEP = /^ *\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/;
// Sticky (matched at lastIndex): links and images, <autolinks>, bare URLs (minus trailing punctuation).
const MD_LINK = /!?\[((?:[^[\]\\]|\\[^])*)\]\([ \t]*<?((?:[^\s()<>\\]|\\[^]|\([^\s()]*\))*)>?(?:[ \t]+(?:"[^"]*"|'[^']*'))?[ \t]*\)/y;
const MD_AUTOLINK = /<(https?:\/\/[^\s<>]+)>/y;
const MD_URL = /https?:\/\/[^\s<>]*[^\s<>.,;:!?'")\]*_~]/y;
const MD_PUNCT = /[!-/:-@[-`{-~]/;

function markdown(text) {
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n")
    .map((l) => l.replace(/^\t+/, (t) => "    ".repeat(t.length)));
  return mdBlocks(lines, 0);
}

function mdEl(tag, ...kids) {
  const el = document.createElement(tag);
  el.append(...kids);
  return el;
}

function mdIndent(line) { return line.length - line.trimStart().length; }

function mdFence(line) {
  const m = /^( {0,3})(`{3,}|~{3,})(.*)$/.exec(line);
  return m && !(m[2][0] === "`" && m[3].includes("`")) ? m : null;
}

function mdTableCells(line) {
  const s = line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "");
  return s.split(/(?<!\\)\|/).map((c) => c.trim());
}

function mdIsTable(lines, i) {
  const sep = lines[i + 1];
  return lines[i].includes("|") && sep != null && sep.includes("|") && MD_TABLE_SEP.test(sep)
    && mdTableCells(lines[i]).length === mdTableCells(sep).length;
}

function mdStartsBlock(lines, i) {
  const l = lines[i];
  return !!(mdFence(l) || MD_HEADING.test(l) || MD_HR.test(l) || MD_QUOTE.test(l) || MD_ITEM.test(l)
    || mdIsTable(lines, i));
}

// tight: paragraphs are left unwrapped (a list item with no blank lines in it).
function mdBlocks(lines, depth, tight) {
  const out = [];
  if (depth > MD_DEPTH) return [mdEl("p", document.createTextNode(lines.join("\n")))];
  let i = 0, m;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let node;
    if ((m = mdFence(line))) [node, i] = mdCode(lines, i, m);
    else if ((m = MD_HEADING.exec(line))) {
      node = mdEl("h" + m[1].length, ...mdInline(m[2] || "", 0));
      i++;
    } else if (MD_HR.test(line)) { node = mdEl("hr"); i++; }
    else if (MD_QUOTE.test(line)) {
      const body = [];
      for (; i < lines.length && MD_QUOTE.test(lines[i]); i++) body.push(lines[i].replace(MD_QUOTE, ""));
      node = mdEl("blockquote", ...mdBlocks(body, depth + 1));
    } else if (mdIsTable(lines, i)) [node, i] = mdTable(lines, i);
    else if (MD_ITEM.test(line)) [node, i] = mdList(lines, i, depth);
    else {
      const para = [line.trim()];
      for (i++; i < lines.length && lines[i].trim() && !mdStartsBlock(lines, i); i++) para.push(lines[i].trim());
      const kids = mdInline(para.join("\n"), 0);
      if (tight) { out.push(...kids); continue; }
      node = mdEl("p", ...kids);
    }
    out.push(node);
  }
  return out;
}

// An unclosed fence runs to the end, which is what a code block still streaming in looks like.
function mdCode(lines, i, m) {
  const indent = new RegExp(`^ {0,${m[1].length}}`), mark = m[2], body = [];
  for (i++; i < lines.length; i++) {
    const c = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(lines[i]);
    if (c && c[1][0] === mark[0] && c[1].length >= mark.length) { i++; break; }
    body.push(lines[i].replace(indent, ""));
  }
  return [mdEl("pre", mdEl("code", document.createTextNode(body.join("\n")))), i];
}

function mdTable(lines, i) {
  const align = mdTableCells(lines[i + 1]).map((c) =>
    c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : c.startsWith(":") ? "left" : "");
  const row = (line, tag) => mdEl("tr", ...align.map((a, k) => {
    const cell = mdEl(tag, ...mdInline(mdTableCells(line)[k] || "", 0));
    if (a) cell.style.textAlign = a;
    return cell;
  }));
  const head = mdEl("thead", row(lines[i], "th"));
  const body = mdEl("tbody");
  for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim() && !mdFence(lines[i])
    && !MD_QUOTE.test(lines[i]); i++) body.append(row(lines[i], "td"));
  const wrap = mdEl("div", mdEl("table", head, body));
  wrap.className = "table";
  return [wrap, i];
}

// An item's body is its first line plus the lines indented to its text (or deeper list items); it is parsed as
// blocks, which is how lists nest.
function mdList(lines, i, depth) {
  const first = MD_ITEM.exec(lines[i]);
  const ordered = /\d/.test(first[2]);
  const list = mdEl(ordered ? "ol" : "ul");
  if (ordered && parseInt(first[2], 10) !== 1) list.setAttribute("start", String(parseInt(first[2], 10)));
  while (i < lines.length) {
    const m = MD_ITEM.exec(lines[i]);
    if (!m || /\d/.test(m[2]) !== ordered) break;
    const col = m[1].length + m[2].length + 1;
    const body = [m[3] || ""];
    for (i++; i < lines.length; i++) {
      const l = lines[i], ind = mdIndent(l);
      if (!l.trim()) {
        let k = i + 1;
        while (k < lines.length && !lines[k].trim()) k++;
        if (k < lines.length && mdIndent(lines[k]) >= col) { body.push(""); continue; }
        break;
      }
      if (ind >= col || (MD_ITEM.test(l) && ind > m[1].length)) { body.push(l.slice(Math.min(ind, col))); continue; }
      if (mdStartsBlock(lines, i) || body[body.length - 1] === "") break;
      body.push(l.trim());  // a lazy continuation of the item's paragraph
    }
    list.append(mdEl("li", ...mdBlocks(body, depth + 1, !body.includes(""))));
    let k = i;  // blank lines between two items of the same list
    while (k < lines.length && !lines[k].trim()) k++;
    const next = k < lines.length && MD_ITEM.exec(lines[k]);
    if (k > i && next && /\d/.test(next[2]) === ordered && next[1].length <= first[1].length) i = k;
  }
  return [list, i];
}

// The URL to link to, or null: only absolute http(s) URLs, normalized by the URL parser.
function mdSafeUrl(raw) {
  const s = raw.replace(/\\([^])/g, "$1");
  if (!/^https?:\/\//i.test(s)) return null;
  try {
    const u = new URL(s);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch { return null; }
}

function mdLink(url, kids) {
  const a = mdEl("a", ...kids);
  a.setAttribute("href", url);
  a.setAttribute("target", "_blank");
  a.setAttribute("rel", "noopener noreferrer");
  return a;
}

// Index of a run of exactly n backticks at or after i, or -1.
function mdCodeEnd(s, i, n) {
  for (let j = s.indexOf("`", i); j !== -1;) {
    let k = j;
    while (s[k] === "`") k++;
    if (k - j === n) return j;
    j = s.indexOf("`", k);
  }
  return -1;
}

// Where emphasis delimiter d (opened before `from`) closes, or -1. Skips escapes and code spans; a closer needs
// no space before it, and `_` mustn't be inside a word (snake_case stays as it is).
function mdCloser(s, from, d) {
  for (let j = from; j < s.length; j++) {
    const c = s[j];
    if (c === "\\") { j++; continue; }
    if (c === "`") {
      let k = j;
      while (s[k] === "`") k++;
      const e = mdCodeEnd(s, k, k - j);
      j = e === -1 ? k - 1 : e + (k - j) - 1;
      continue;
    }
    if (c !== d[0]) continue;
    let k = j;
    while (s[k] === c) k++;
    const run = k - j;
    if (run < d.length || (d.length === 1 && run === 2)) { j = k - 1; continue; }
    const at = k - d.length;  // `***` closes `**` at its end, leaving `*` to the inner text
    if (at > from && !/\s/.test(s[j - 1]) && (c !== "_" || !/\w/.test(s[k] || ""))) return at;
    j = k - 1;
  }
  return -1;
}

function mdInline(s, depth) {
  if (depth > MD_DEPTH) return [document.createTextNode(s)];
  const out = [];
  const none = new Set();  // delimiters with no closer left: later openers can't have one either
  let buf = "";
  const flush = () => { if (buf) out.push(document.createTextNode(buf)); buf = ""; };
  const push = (node, next) => { flush(); out.push(node); return next; };
  let i = 0, m;
  const at = (re) => { re.lastIndex = i; return re.exec(s); };
  while (i < s.length) {
    const c = s[i];
    if (c === "\\" && i + 1 < s.length && MD_PUNCT.test(s[i + 1])) { buf += s[i + 1]; i += 2; continue; }
    if (c === "\n") { i = push(mdEl("br"), i + 1); continue; }
    if (c === "`") {
      let k = i;
      while (s[k] === "`") k++;
      const e = none.has("`" + (k - i)) ? -1 : mdCodeEnd(s, k, k - i);
      if (e === -1) { none.add("`" + (k - i)); buf += s.slice(i, k); i = k; continue; }
      let code = s.slice(k, e).replace(/\n/g, " ");
      if (/^ .*[^ ].* $/.test(code)) code = code.slice(1, -1);
      i = push(mdEl("code", document.createTextNode(code)), e + (k - i));
      continue;
    }
    if ((c === "[" || (c === "!" && s[i + 1] === "[")) && (m = at(MD_LINK))) {
      const url = mdSafeUrl(m[2]);
      const kids = c === "!" ? [document.createTextNode(m[1] || m[2])] : mdInline(m[1], depth + 1);
      if (url) i = push(mdLink(url, kids), i + m[0].length);
      else { flush(); out.push(...kids); i += m[0].length; }
      continue;
    }
    if (c === "<" && (m = at(MD_AUTOLINK)) && mdSafeUrl(m[1])) {
      i = push(mdLink(mdSafeUrl(m[1]), [document.createTextNode(m[1])]), i + m[0].length);
      continue;
    }
    if (c === "h" && !/\w/.test(s[i - 1] || "") && (m = at(MD_URL)) && mdSafeUrl(m[0])) {
      i = push(mdLink(mdSafeUrl(m[0]), [document.createTextNode(m[0])]), i + m[0].length);
      continue;
    }
    if (c === "*" || c === "_" || (c === "~" && s[i + 1] === "~")) {
      const d = s[i + 1] === c ? c + c : c;
      const from = i + d.length;
      const opens = from < s.length && !/\s/.test(s[from]) && (c !== "_" || !/\w/.test(s[i - 1] || ""));
      const e = opens && !none.has(d) ? mdCloser(s, from, d) : -1;
      if (opens && e === -1) none.add(d);
      if (e !== -1) {
        const tag = c === "~" ? "del" : d.length === 2 ? "strong" : "em";
        i = push(mdEl(tag, ...mdInline(s.slice(from, e), depth + 1)), e + d.length);
        continue;
      }
      buf += d;
      i = from;
      continue;
    }
    buf += c;
    i++;
  }
  flush();
  return out;
}
