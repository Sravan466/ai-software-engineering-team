// The app preview's only way in or out (#78). Runs inside the preview sandbox, which
// has no network: no port is published, nothing listens beyond the container's own
// loopback. The backend talks to this over the container's stdin and stdout — one JSON
// object per line each way — and this passes each request to the app's server inside.
//
//   host -> relay  {id, method, path, headers, body}      a request (body base64)
//                  {type: "start-backend", entry, cwd, env} run a Node backend here
//                  {type: "backend", status, why}         the backend's state, from outside
//   relay -> host  {type: "ready"} | {type: "failed", why} | {type: "backend", status, why}
//                  {id, status, headers, body}             the answer (body base64)
//
// The frontend is `next start` on 127.0.0.1:3000, or the built `dist/` served from
// here. `/__api/*` — and `/api/*` when the frontend has no API routes of its own — goes
// to the backend: a Node one on 127.0.0.1:3001 in this container, or a Python one on a
// unix socket in the shared volume, run in a sibling container.
//
// No dependencies: node's standard library only. Plain CommonJS.
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { spawn } = require('child_process');

const cfg = JSON.parse(process.env.AITEAM_PREVIEW || '{}');
const ROOT = cfg.root || '/work/frontend';
const FRONT_PORT = 3000;
const BACK_PORT = 3001;
const MAX_BODY = 25 * 1024 * 1024;

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}
function log(line) {
  process.stderr.write(String(line).replace(/\s+$/, '') + '\n');
}

const children = [];
function run(name, cmd, args, opts) {
  const child = spawn(cmd, args, Object.assign({ stdio: ['ignore', 'pipe', 'pipe'] }, opts));
  children.push(child);
  const prefix = `[${name}] `;
  child.stdout.on('data', (d) => log(prefix + d));
  child.stderr.on('data', (d) => log(prefix + d));
  return child;
}
function shutdown(code) {
  for (const c of children) {
    try { c.kill('SIGTERM'); } catch (_) {}
  }
  setTimeout(() => process.exit(code), 200);
}
process.on('SIGTERM', () => shutdown(0));
process.on('uncaughtException', (e) => log(`[relay] ${e && e.stack ? e.stack : e}`));
process.stdin.on('end', () => shutdown(0));

// ── the frontend ─────────────────────────────────────────────────────────────
const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.ico': 'image/x-icon',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json', '.webmanifest': 'application/manifest+json', '.avif': 'image/avif',
};

function serveStatic(dir) {
  const base = path.resolve(ROOT, dir);
  return http.createServer((req, res) => {
    let rel;
    try {
      rel = decodeURIComponent((req.url || '/').split('?')[0]);
    } catch (_) {
      // A malformed address is the request's fault: answer it, don't fall over.
      res.writeHead(400, { 'content-type': 'text/plain; charset=utf-8' });
      return res.end('Bad request');
    }
    let file = path.resolve(base, '.' + rel);
    if (!file.startsWith(base)) {
      res.writeHead(403);
      return res.end();
    }
    const tryFiles = [file, path.join(file, 'index.html')];
    // A single-page app routes in the browser: any path without an extension is its page.
    if (!path.extname(rel)) tryFiles.push(path.join(base, 'index.html'));
    for (const f of tryFiles) {
      try {
        if (fs.statSync(f).isFile()) {
          res.writeHead(200, { 'content-type': TYPES[path.extname(f).toLowerCase()] || 'application/octet-stream' });
          return fs.createReadStream(f).pipe(res);
        }
      } catch (_) {}
    }
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('Not found');
  });
}

function startFrontend() {
  if (cfg.mode === 'static') {
    serveStatic(cfg.dir || 'dist').listen(FRONT_PORT, '127.0.0.1');
    return;
  }
  const next = run('next', process.execPath, ['node_modules/next/dist/bin/next', 'start', '-p', String(FRONT_PORT), '-H', '127.0.0.1'], {
    cwd: ROOT,
    env: Object.assign({}, process.env, cfg.env || {}, { NODE_ENV: 'production', PORT: String(FRONT_PORT) }),
  });
  next.on('exit', (code) => {
    send({ type: 'failed', why: `next start stopped (exit ${code}).` });
    shutdown(1);
  });
}

// ── the backend ──────────────────────────────────────────────────────────────
let backend = { status: 'none', why: '' };
let backendTarget = null; // {port} | {socketPath}

function setBackend(status, why) {
  backend = { status, why: why || '' };
  send({ type: 'backend', status, why: why || '' });
}

function probe(target, timeoutMs) {
  return new Promise((resolve) => {
    const req = http.request(Object.assign({ host: '127.0.0.1', method: 'GET', path: '/', timeout: 1500 }, target), (res) => {
      res.resume();
      resolve(true);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.end();
  });
}

async function waitFor(target, seconds, alive) {
  const until = Date.now() + seconds * 1000;
  while (Date.now() < until) {
    if (await probe(target)) return true;
    if (alive && !alive()) return false;
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

function startNodeBackend(msg) {
  let exited = null;
  let tail = '';
  const child = run('backend', process.execPath, [msg.entry], {
    cwd: msg.cwd || '/work/backend',
    env: Object.assign({}, process.env, msg.env || {}, { PORT: String(BACK_PORT), NODE_ENV: 'production' }),
  });
  const keep = (d) => { tail = (tail + d).slice(-600); };
  child.stdout.on('data', keep);
  child.stderr.on('data', keep);
  child.on('exit', (code) => {
    exited = code;
    if (backend.status === 'up') setBackend('down', `The backend stopped (exit ${code}).`);
  });
  backendTarget = { port: BACK_PORT };
  setBackend('starting');
  waitFor(backendTarget, 30, () => exited === null).then((ok) => {
    if (ok) setBackend('up');
    else setBackend('down', exited !== null ? lastLine(tail) || `It exited with code ${exited}.` : 'It didn\'t answer within 30 seconds.');
  });
}

function lastLine(text) {
  const lines = String(text).split('\n').map((l) => l.trim()).filter(Boolean);
  return lines.length ? lines[lines.length - 1].slice(0, 240) : '';
}

// ── requests ─────────────────────────────────────────────────────────────────
const HOP = new Set(['host', 'connection', 'keep-alive', 'accept-encoding', 'transfer-encoding', 'upgrade', 'proxy-connection', 'te', 'trailer', 'content-length']);

function toBackend(p) {
  if (p === '/__api' || p.startsWith('/__api/') || p.startsWith('/__api?')) return { path: p.slice('/__api'.length) || '/' };
  if (cfg.apiFallback && (p === '/api' || p.startsWith('/api/') || p.startsWith('/api?'))) return { path: p };
  return null;
}

function forward(msg) {
  const back = toBackend(msg.path || '/');
  if (back && !(backendTarget && backend.status === 'up')) {
    const why = backend.status === 'starting' ? 'The backend is still starting.' : backend.why || 'This preview has no backend running.';
    const body = Buffer.from(JSON.stringify({ detail: `This preview runs the frontend only. ${why}` }));
    send({ id: msg.id, status: 503, headers: { 'content-type': 'application/json' }, body: body.toString('base64') });
    return;
  }
  const headers = {};
  for (const [k, v] of Object.entries(msg.headers || {})) if (!HOP.has(k.toLowerCase())) headers[k] = v;
  const body = msg.body ? Buffer.from(msg.body, 'base64') : null;
  if (body && body.length) headers['content-length'] = String(body.length);
  headers.host = 'localhost';
  const target = back ? backendTarget : { port: FRONT_PORT };
  const req = http.request(
    Object.assign({ host: '127.0.0.1', method: msg.method || 'GET', path: back ? back.path : msg.path || '/', headers, timeout: 60000 }, target),
    (res) => {
      const chunks = [];
      let size = 0;
      res.on('data', (d) => {
        size += d.length;
        if (size <= MAX_BODY) chunks.push(d);
      });
      res.on('end', () => {
        if (size > MAX_BODY) {
          send({ id: msg.id, status: 502, headers: {}, body: '', error: 'The response was too large to preview.' });
          return;
        }
        const out = {};
        for (const [k, v] of Object.entries(res.headers)) if (!HOP.has(k.toLowerCase())) out[k] = v;
        send({ id: msg.id, status: res.statusCode || 502, headers: out, body: Buffer.concat(chunks).toString('base64') });
      });
    },
  );
  req.on('timeout', () => req.destroy(new Error('timed out')));
  req.on('error', (e) => send({ id: msg.id, status: 502, headers: {}, body: '', error: String(e.message || e) }));
  if (body && body.length) req.write(body);
  req.end();
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch (_) { return; }
  if (msg.type === 'start-backend') return startNodeBackend(msg);
  if (msg.type === 'backend-socket') {
    backendTarget = { socketPath: msg.socket };
    setBackend('starting');
    waitFor(backendTarget, msg.seconds || 45).then((ok) => {
      if (ok) setBackend('up');
      else if (backend.status !== 'down') setBackend('down', msg.why || "It didn't answer within the time it was given.");
    });
    return;
  }
  if (msg.type === 'backend') return setBackend(msg.status, msg.why);
  if (msg.id !== undefined) forward(msg);
});

startFrontend();
waitFor({ port: FRONT_PORT }, cfg.readySeconds || 90).then((ok) => {
  if (ok) send({ type: 'ready' });
  else {
    send({ type: 'failed', why: "The app's server didn't answer within the time it was given." });
    shutdown(1);
  }
});
