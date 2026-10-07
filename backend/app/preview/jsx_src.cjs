// Where each element on the app preview comes from, and how to change it there (#78).
// Run by `app.preview.source` with node, the way the compile gate runs check_js.cjs.
//
// Reads one JSON request from stdin, writes one JSON answer to stdout:
//
//   {op: "tag", typescript, files: {path: content}}
//     -> {files: {path: tagged}, tagged: n}
//     Every element the app renders from this code gets `data-src="path:line:col"`,
//     the place its opening tag starts in the file *as written* — not as tagged — so
//     the address the preview reports is an address in the code that ships.
//
//   {op: "locate", typescript, path, content, line, col}
//     -> {found, tag, start_line, end_line, text: {editable, value, why}, classes: {...}}
//
//   {op: "edit", typescript, files: {path: content}, ops: [{path, line, col, kind, …}]}
//     -> {files: {path: content}, refused: [{index, reason}]}
//     kind "text" (text), "classes" (add, remove), "attr" (name, value). Only what the
//     code states plainly is changed: a literal, never an expression. Anything else is
//     refused with the reason, so the person can ask the crew instead.
//
// Only TypeScript's parser is used — nothing is evaluated, and nothing is written to disk.
'use strict';

const fs = require('fs');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const ts = require(input.typescript);

// Elements a person can't usefully pick, or that React renders outside the page body.
const SKIP = new Set(['html', 'head', 'body', 'script', 'style', 'meta', 'link', 'title', 'base', 'noscript', 'template', 'slot']);
// The insides of an icon: the <svg> is the thing to pick, not each of its paths.
const SVG_PARTS = new Set([
  'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse', 'g', 'defs', 'use', 'stop',
  'lineargradient', 'radialgradient', 'clippath', 'mask', 'symbol', 'tspan', 'desc', 'pattern', 'filter',
]);
// Components known to pass the attribute on to the element they render.
const FORWARDING = new Set(['Link']);

function kindFor(path) {
  if (path.endsWith('.tsx')) return ts.ScriptKind.TSX;
  if (path.endsWith('.jsx') || path.endsWith('.js') || path.endsWith('.mjs')) return ts.ScriptKind.JSX;
  return null;
}

function isTest(path) {
  return /(^|\/)(__tests__|tests?|e2e)\//.test(path) || /\.(test|spec)\.[cm]?[jt]sx?$/.test(path);
}

function parse(path, content) {
  const kind = kindFor(path);
  if (kind === null) return null;
  return ts.createSourceFile(path, content, ts.ScriptTarget.Latest, true, kind);
}

function tagName(node) {
  const name = node.tagName;
  if (!name || !ts.isIdentifier(name)) return null;
  return name.text;
}

function taggable(name) {
  if (!name) return false;
  if (FORWARDING.has(name)) return true;
  if (!/^[a-z]/.test(name) || name.includes('-')) return false;
  const lower = name.toLowerCase();
  return !SKIP.has(lower) && !SVG_PARTS.has(lower);
}

function attribute(opening, wanted) {
  for (const prop of opening.attributes.properties) {
    if (ts.isJsxAttribute(prop) && prop.name && prop.name.getText() === wanted) return prop;
  }
  return null;
}

function position(sf, node) {
  const at = sf.getLineAndCharacterOfPosition(node.getStart(sf));
  return { line: at.line + 1, col: at.character + 1 };
}

function walk(node, visit) {
  visit(node);
  ts.forEachChild(node, (child) => walk(child, visit));
}

// ── tag ──────────────────────────────────────────────────────────────────────
function tag(files) {
  const out = {};
  let count = 0;
  for (const [path, content] of Object.entries(files)) {
    out[path] = content;
    if (isTest(path) || path.includes('node_modules/')) continue;
    const sf = parse(path, content);
    if (!sf) continue;
    const inserts = [];
    walk(sf, (node) => {
      if (!(ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))) return;
      const name = tagName(node);
      if (!taggable(name) || attribute(node, 'data-src')) return;
      const at = position(sf, node);
      inserts.push({ pos: node.tagName.end, text: ` data-src="${path.replace(/"/g, '')}:${at.line}:${at.col}"` });
    });
    if (!inserts.length) continue;
    inserts.sort((a, b) => b.pos - a.pos);
    let text = content;
    for (const ins of inserts) text = text.slice(0, ins.pos) + ins.text + text.slice(ins.pos);
    out[path] = text;
    count += inserts.length;
  }
  return { files: out, tagged: count };
}

// ── find one element ─────────────────────────────────────────────────────────
function find(sf, line, col) {
  let hit = null;
  walk(sf, (node) => {
    if (hit) return;
    if (!(ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))) return;
    const at = position(sf, node);
    if (at.line === line && at.col === col) hit = node;
  });
  if (!hit) return null;
  return { opening: hit, element: ts.isJsxOpeningElement(hit) ? hit.parent : hit };
}

function meaningful(children) {
  return children.filter((c) => !(ts.isJsxText(c) && c.containsOnlyTriviaWhiteSpaces));
}

function stringLiteral(node) {
  return node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) ? node : null;
}

function short(sf, node, n) {
  const text = node.getText(sf).replace(/\s+/g, ' ');
  return text.length > n ? text.slice(0, n - 1) + '…' : text;
}

// What a text edit would change, or why it can't.
function textTarget(sf, found) {
  const { element } = found;
  if (!ts.isJsxElement(element)) return { why: 'This element has no words of its own.' };
  const kids = meaningful(element.children);
  if (kids.length === 1 && ts.isJsxText(kids[0])) {
    const node = kids[0];
    const raw = node.getFullText(sf);
    const lead = raw.length - raw.trimStart().length;
    const trail = raw.length - raw.trimEnd().length;
    const start = node.getFullStart() + lead;
    const end = node.end - trail;
    return { start, end, value: raw.trim().replace(/\s+/g, ' '), form: 'text' };
  }
  if (kids.length === 1 && ts.isJsxExpression(kids[0]) && stringLiteral(kids[0].expression)) {
    const lit = kids[0].expression;
    return { start: lit.getStart(sf), end: lit.end, value: lit.text, form: 'literal' };
  }
  if (!kids.length) return { why: 'This element has no words of its own.' };
  const code = kids.find((k) => ts.isJsxExpression(k));
  if (code) {
    return {
      why: `Its words come from code (${short(sf, code, 40)}), so they can't be typed over here. Ask the crew to change them.`,
    };
  }
  return { why: 'Its words are split across other elements. Select the part you want to change, or ask the crew.' };
}

function attrTarget(sf, opening, name) {
  const attr = attribute(opening, name);
  if (!attr) return { missing: true, value: '' };
  const init = attr.initializer;
  if (!init) return { why: `\`${name}\` is set without a value here.` };
  // A JSX attribute string (`alt="…"`) is not a JS string: it has no escapes, and it
  // reads HTML entities. Written back with `jsxAttr`, never `quoted`.
  if (ts.isStringLiteral(init)) return { start: init.getStart(sf), end: init.end, value: init.text, jsx: true };
  if (ts.isJsxExpression(init) && stringLiteral(init.expression)) {
    const lit = init.expression;
    return { start: lit.getStart(sf), end: lit.end, value: lit.text, quote: lit.getText(sf)[0] };
  }
  return {
    why: `Its \`${name}\` is computed (${short(sf, init, 40)}), so it can't be changed here. Ask the crew.`,
  };
}

function quoted(value, quote) {
  if (quote === '`') return '`' + value.replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$\{/g, '\\${') + '`';
  if (quote === "'") return "'" + value.replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
  return JSON.stringify(value);
}

function jsxAttr(value) {
  // Plain when JSX would read it back as written; a string expression otherwise.
  if (/["&\n\r]/.test(value)) return `{${JSON.stringify(value)}}`;
  return `"${value}"`;
}

function written(target, value) {
  return target.jsx ? jsxAttr(value) : quoted(value, target.quote);
}

function jsxText(value) {
  // Plain words stay plain; anything JSX would read as markup or an entity is written
  // as a string expression instead.
  if (/[{}<>&]/.test(value) || value !== value.trim() || !value) return `{${JSON.stringify(value)}}`;
  return value;
}

function locate(path, content, line, col) {
  const sf = parse(path, content);
  if (!sf) return { found: false };
  const found = find(sf, line, col);
  if (!found) return { found: false };
  const { opening, element } = found;
  const start = position(sf, opening).line;
  const end = sf.getLineAndCharacterOfPosition(element.end).line + 1;
  const text = textTarget(sf, found);
  const classes = attrTarget(sf, opening, 'className');
  return {
    found: true,
    tag: tagName(opening),
    start_line: start,
    end_line: end,
    text: text.why ? { editable: false, why: text.why } : { editable: true, value: text.value },
    classes: classes.why ? { editable: false, why: classes.why } : { editable: true, value: classes.value || '' },
  };
}

// ── edit ─────────────────────────────────────────────────────────────────────
function edit(files, ops) {
  const out = Object.assign({}, files);
  const refused = [];
  const byFile = new Map();
  ops.forEach((op, index) => {
    if (!byFile.has(op.path)) byFile.set(op.path, []);
    byFile.get(op.path).push(Object.assign({ index }, op));
  });
  for (const [path, list] of byFile) {
    const content = files[path];
    if (typeof content !== 'string') {
      list.forEach((op) => refused.push({ index: op.index, reason: `${path} isn't part of the frontend the crew wrote.` }));
      continue;
    }
    const sf = parse(path, content);
    if (!sf) {
      list.forEach((op) => refused.push({ index: op.index, reason: `${path} isn't a file with markup in it.` }));
      continue;
    }
    // Every target is found in the file as it is, then all the replacements are made
    // from the end backwards — so one change never moves another's address.
    const changes = [];
    for (const op of list) {
      const found = find(sf, Number(op.line), Number(op.col));
      if (!found) {
        refused.push({ index: op.index, reason: 'That element has moved in the code since this preview was built. Wait for it to rebuild, then try again.' });
        continue;
      }
      const change = plan(sf, found, op);
      if (change.why) refused.push({ index: op.index, reason: change.why });
      else changes.push(change);
    }
    changes.sort((a, b) => b.start - a.start);
    for (let i = 1; i < changes.length; i++) {
      if (changes[i].end > changes[i - 1].start) {
        refused.push({ index: changes[i].index, reason: 'Two changes reach the same part of the code. Apply them one at a time.' });
        changes.splice(i, 1);
        i--;
      }
    }
    let text = content;
    for (const c of changes) text = text.slice(0, c.start) + c.text + text.slice(c.end);
    out[path] = text;
  }
  return { files: out, refused };
}

function plan(sf, found, op) {
  const { opening } = found;
  if (op.kind === 'text') {
    const target = textTarget(sf, found);
    if (target.why) return { why: target.why };
    const value = String(op.text == null ? '' : op.text);
    const text = target.form === 'literal' ? quoted(value, sf.text[target.start]) : jsxText(value);
    return { index: op.index, start: target.start, end: target.end, text };
  }
  if (op.kind === 'classes') {
    const target = attrTarget(sf, opening, 'className');
    if (target.why) return { why: target.why };
    const have = (target.value || '').split(/\s+/).filter(Boolean);
    const remove = (op.remove || []).filter(Boolean);
    const missing = remove.filter((c) => !have.includes(c));
    if (missing.length) {
      return {
        why: `\`${missing.slice(0, 3).join(' ')}\` isn't written on this element (it comes from somewhere else), so it can't be removed here. Ask the crew.`,
      };
    }
    let next = have.filter((c) => !remove.includes(c));
    for (const c of op.add || []) if (c && !next.includes(c)) next.push(c);
    const value = next.join(' ');
    if (target.missing) {
      return { index: op.index, start: opening.tagName.end, end: opening.tagName.end, text: ` className=${jsxAttr(value)}` };
    }
    return { index: op.index, start: target.start, end: target.end, text: written(target, value) };
  }
  if (op.kind === 'attr') {
    const name = String(op.name || '');
    if (!/^(href|alt|src)$/.test(name)) return { why: `\`${name}\` can't be changed here.` };
    const target = attrTarget(sf, opening, name);
    if (target.why) return { why: target.why };
    const value = op.value == null ? '' : String(op.value);
    if (target.missing) {
      return { index: op.index, start: opening.tagName.end, end: opening.tagName.end, text: ` ${name}=${jsxAttr(value)}` };
    }
    return { index: op.index, start: target.start, end: target.end, text: written(target, value) };
  }
  return { why: `"${op.kind}" isn't a change the code can take.` };
}

let answer;
if (input.op === 'tag') answer = tag(input.files || {});
else if (input.op === 'locate') answer = locate(input.path, input.content, Number(input.line), Number(input.col));
else if (input.op === 'edit') answer = edit(input.files || {}, input.ops || []);
else answer = { error: `unknown op ${input.op}` };
process.stdout.write(JSON.stringify(answer));
