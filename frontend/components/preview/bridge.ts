/**
 * The selector that runs inside the mockup, for the Preview tab's Edit mode.
 *
 * The frame is sandboxed without same-origin, so the Preview tab cannot reach into
 * it. Everything goes through `postMessage`: this script reports what is hovered,
 * selected and typed, and applies the changes the inspector makes so they show at
 * once, before they are saved.
 *
 * It draws in a shadow root on <html>, never on the page's own elements. The old
 * inline outline sat under full-bleed children and changed the page it described;
 * a fixed overlay with `pointer-events: none` does neither.
 *
 * Every element a person can pick carries a `data-oid` from the server. Elements
 * cloned from a list's <template> share their template's id, and are reported as
 * templated: the inspector sends changes to them to the model instead.
 *
 * Plain ES5 in a string, because it is injected into a document this app does not
 * build: no bundler helper can reach it there.
 */
export const BRIDGE_SCRIPT = String.raw`
<script>
(function(){
  if (window.__pvBridge) return; window.__pvBridge = true;
  var doc = document, win = window;
  var mode = 'use';
  var selected = [];       // elements; the last one is primary
  var hoverEl = null;
  var editing = null;      // element being typed into
  var editingBefore = '';
  var menu = null;
  var ACCENT = '#f5a524', HOVER = '#22b8cf';
  var BLOCK = /^(DIV|SECTION|ARTICLE|UL|OL|LI|NAV|HEADER|FOOTER|MAIN|FORM|TABLE|IMG|SVG|BUTTON|A|INPUT|SELECT|TEXTAREA)$/;
  var NOTEXT = /^(IMG|SVG|INPUT|SELECT|TEXTAREA|HR|BR|MAIN|UL|OL|TABLE|FORM|NAV|HEADER|FOOTER|SECTION)$/;

  function post(m){ m.__preview = true; try { parent.postMessage(m, '*'); } catch(_){} }
  function oidOf(el){ return el && el.getAttribute ? el.getAttribute('data-oid') : null; }
  function pickable(t){
    if (!t || !t.closest) return null;
    if (host && (t === host || host.contains(t))) return null;
    var el = t.closest('[data-oid]');
    if (!el || el === doc.body) return null;
    return el;
  }
  function parentPick(el){ var p = el && el.parentElement; return p ? p.closest('[data-oid]') : null; }
  function firstChildPick(el){
    if (!el) return null;
    var kids = el.querySelectorAll('[data-oid]');
    for (var i = 0; i < kids.length; i++) { if (visible(kids[i])) return kids[i]; }
    return null;
  }
  function visible(el){ var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
  function words(el, n){
    var t = (el.getAttribute('aria-label') || el.getAttribute('alt') || el.textContent || '').replace(/\s+/g, ' ').trim();
    n = n || 24; return t.length > n ? t.slice(0, n - 1) + '…' : t;
  }
  function q(t){ return t ? ' “' + t + '”' : ''; }
  function isLogo(el){
    if (el.tagName !== 'A') return false;
    var bar = el.closest('header, nav'); if (!bar) return false;
    var first = bar.querySelector('a[href]');
    return first === el && /^#\/?$/.test(el.getAttribute('href') || '');
  }
  function name(el){
    var tag = el.tagName, cls = ' ' + (el.getAttribute('class') || '') + ' ';
    var label = el.getAttribute('data-label');
    if (el.hasAttribute('data-route')) return 'Page' + q(el.getAttribute('data-route-title') || el.getAttribute('data-route'));
    if (el.hasAttribute('data-section')) {
      if (tag === 'HEADER' || tag === 'NAV' || el.getAttribute('data-kind') === 'nav') return label || 'Navbar';
      if (tag === 'FOOTER') return label || 'Footer';
      return label || 'Section';
    }
    if (tag === 'NAV') return 'Navigation';
    if (tag === 'HEADER') return 'Header';
    if (tag === 'FOOTER') return 'Footer';
    if (tag === 'MAIN') return 'Main content';
    if (isLogo(el)) return 'Logo';
    if (/^H[1-6]$/.test(tag)) return 'Heading' + q(words(el));
    if (tag === 'BUTTON' || cls.indexOf(' btn ') > -1) return 'Button' + q(words(el));
    if (tag === 'A') return 'Link' + q(words(el));
    if (tag === 'IMG') return 'Image' + q(words(el));
    if (tag === 'svg' || tag === 'SVG') return 'Icon';
    if (tag === 'P') return 'Text' + q(words(el));
    if (tag === 'SPAN' || tag === 'STRONG' || tag === 'EM') return 'Text' + q(words(el));
    if (tag === 'LI') return 'List item' + q(words(el, 18));
    if (tag === 'UL' || tag === 'OL') return 'List';
    if (tag === 'FORM') return 'Form';
    if (tag === 'LABEL') return 'Label' + q(words(el));
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return 'Field' + q(el.getAttribute('placeholder') || el.getAttribute('name') || '');
    if (tag === 'TABLE') return 'Table';
    if (tag === 'TR') return 'Row';
    if (tag === 'TD' || tag === 'TH') return 'Cell' + q(words(el, 16));
    if (tag === 'TEMPLATE') return 'Template';
    if (cls.indexOf(' card ') > -1) return 'Card';
    if (cls.indexOf(' badge ') > -1 || cls.indexOf(' chip ') > -1) return 'Badge' + q(words(el, 16));
    if (cls.indexOf(' container-page ') > -1) return 'Container';
    if (cls.indexOf(' grid ') > -1) return 'Grid';
    if (cls.indexOf(' flex ') > -1) return 'Row';
    return 'Block';
  }
  function sectionOf(el){ return el.closest('[data-section]'); }
  function chipLabel(el){
    var s = sectionOf(el);
    return s && s !== el ? name(s) + ' › ' + name(el) : name(el);
  }
  function templated(el){ return !!el.closest('[data-row]'); }
  function filled(el){ return el.hasAttribute('data-field') || el.hasAttribute('data-stat') || el.hasAttribute('data-count') || el.hasAttribute('data-year'); }
  function textEditable(el){
    if (NOTEXT.test(el.tagName.toUpperCase()) || templated(el) || filled(el)) return false;
    var kids = el.children;
    for (var i = 0; i < kids.length; i++) { if (BLOCK.test(kids[i].tagName.toUpperCase())) return false; }
    return (el.textContent || '').trim().length > 0;
  }
  function crumbs(el){
    var out = [], cur = el;
    while (cur) { out.unshift({ oid: oidOf(cur), name: name(cur) }); cur = parentPick(cur); }
    return out;
  }
  function info(el){
    var cs = getComputedStyle(el);
    return {
      oid: oidOf(el), tag: el.tagName.toLowerCase(), name: name(el), label: chipLabel(el),
      crumbs: crumbs(el), classes: el.getAttribute('class') || '',
      text: textEditable(el) ? (el.textContent || '').replace(/\s+/g, ' ').trim() : null,
      templated: templated(el), filled: filled(el),
      section: (sectionOf(el) || {}).getAttribute ? sectionOf(el).getAttribute('data-section') : null,
      hasChildren: !!firstChildPick(el),
      attrs: { href: el.getAttribute('href'), src: el.tagName === 'IMG' ? (el.getAttribute('src') || '').slice(0, 120) : null, alt: el.getAttribute('alt') },
      style: {
        fontSize: cs.fontSize, fontWeight: cs.fontWeight, lineHeight: cs.lineHeight, fontFamily: cs.fontFamily,
        color: cs.color, background: cs.backgroundColor, textAlign: cs.textAlign,
        padding: [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].join(' '),
        margin: [cs.marginTop, cs.marginRight, cs.marginBottom, cs.marginLeft].join(' '),
        display: cs.display, flexDirection: cs.flexDirection, gap: cs.gap, radius: cs.borderTopLeftRadius,
        shadow: cs.boxShadow, border: cs.borderTopWidth
      }
    };
  }

  // ── the overlay ──────────────────────────────────────────────────────────
  var host = doc.createElement('div');
  host.setAttribute('data-preview-overlay', '');
  host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
  var shadow = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;
  shadow.innerHTML =
    '<style>' +
    ':host{all:initial}' +
    '.box{position:fixed;box-sizing:border-box;pointer-events:none;border-radius:2px}' +
    '.hover{outline:1.5px solid ' + HOVER + ';outline-offset:0}' +
    '.sel{outline:2px solid ' + ACCENT + ';outline-offset:1px;box-shadow:0 0 0 4px rgba(245,165,36,.18)}' +
    '.sel.alt{outline-style:dashed}' +
    '.pad{background:rgba(61,214,140,.16)}' +
    '.mar{background:rgba(255,138,61,.16)}' +
    '.chip{position:fixed;pointer-events:none;font:600 11px/1 ui-sans-serif,system-ui,-apple-system,sans-serif;letter-spacing:.01em;' +
      'padding:5px 7px;border-radius:5px;white-space:nowrap;max-width:min(420px,80vw);overflow:hidden;text-overflow:ellipsis;box-shadow:0 4px 12px -2px rgba(0,0,0,.35)}' +
    '.chip.h{background:#0b1f24;color:#bdf3fb;border:1px solid ' + HOVER + '}' +
    '.chip.s{background:' + ACCENT + ';color:#1a1204}' +
    '.chip small{font-weight:500;opacity:.75;margin-left:6px}' +
    '.menu{position:fixed;pointer-events:auto;min-width:200px;max-width:320px;max-height:60vh;overflow:auto;padding:4px;border-radius:9px;' +
      'background:#12151c;border:1px solid #2a303d;box-shadow:0 16px 40px -12px rgba(0,0,0,.7);font:500 12px/1.3 ui-sans-serif,system-ui,sans-serif;color:#e9ecf2}' +
    '.menu p{margin:4px 8px 6px;font-size:11px;color:#838b9c;font-weight:600}' +
    '.menu button{display:block;width:100%;text-align:left;border:0;background:none;color:inherit;font:inherit;padding:7px 8px;border-radius:6px;cursor:pointer}' +
    '.menu button:hover,.menu button:focus-visible{background:#191d26;outline:none}' +
    '.menu button[aria-current=true]{color:' + ACCENT + '}' +
    '</style><div id="layer"></div>';
  var layer = shadow.getElementById ? shadow.getElementById('layer') : shadow.querySelector('#layer');
  (doc.documentElement || doc.body).appendChild(host);

  function box(r, cls){
    var d = doc.createElement('div'); d.className = 'box ' + cls;
    d.style.left = r.left + 'px'; d.style.top = r.top + 'px'; d.style.width = Math.max(r.width, 1) + 'px'; d.style.height = Math.max(r.height, 1) + 'px';
    layer.appendChild(d);
  }
  function chip(r, text, cls, extra){
    var d = doc.createElement('div'); d.className = 'chip ' + cls;
    d.textContent = text;
    if (extra) { var s = doc.createElement('small'); s.textContent = extra; d.appendChild(s); }
    layer.appendChild(d);
    var h = d.offsetHeight || 22, w = d.offsetWidth || 80;
    var top = r.top - h - 4; if (top < 2) top = Math.min(r.bottom + 4, win.innerHeight - h - 2);
    var left = Math.min(Math.max(r.left, 2), Math.max(2, win.innerWidth - w - 2));
    d.style.top = top + 'px'; d.style.left = left + 'px';
  }
  function px(v){ return parseFloat(v) || 0; }
  function spacing(el, r){
    var cs = getComputedStyle(el);
    var mt = px(cs.marginTop), mr = px(cs.marginRight), mb = px(cs.marginBottom), ml = px(cs.marginLeft);
    var pt = px(cs.paddingTop), pr = px(cs.paddingRight), pb = px(cs.paddingBottom), pl = px(cs.paddingLeft);
    if (mt) box({ left: r.left - ml, top: r.top - mt, width: r.width + ml + mr, height: mt }, 'mar');
    if (mb) box({ left: r.left - ml, top: r.bottom, width: r.width + ml + mr, height: mb }, 'mar');
    if (ml) box({ left: r.left - ml, top: r.top, width: ml, height: r.height }, 'mar');
    if (mr) box({ left: r.right, top: r.top, width: mr, height: r.height }, 'mar');
    if (pt) box({ left: r.left, top: r.top, width: r.width, height: pt }, 'pad');
    if (pb) box({ left: r.left, top: r.bottom - pb, width: r.width, height: pb }, 'pad');
    if (pl) box({ left: r.left, top: r.top + pt, width: pl, height: r.height - pt - pb }, 'pad');
    if (pr) box({ left: r.right - pr, top: r.top + pt, width: pr, height: r.height - pt - pb }, 'pad');
  }
  function size(r){ return Math.round(r.width) + ' × ' + Math.round(r.height); }
  var queued = false;
  function draw(){
    queued = false;
    while (layer.firstChild) layer.removeChild(layer.firstChild);
    if (mode !== 'edit') return;
    selected = selected.filter(function(el){ return el.isConnected; });
    if (hoverEl && selected.indexOf(hoverEl) === -1 && hoverEl.isConnected && !editing) {
      var hr = hoverEl.getBoundingClientRect();
      spacing(hoverEl, hr); box(hr, 'hover'); chip(hr, chipLabel(hoverEl), 'h', size(hr));
    }
    for (var i = 0; i < selected.length; i++) {
      var el = selected[i], r = el.getBoundingClientRect();
      var primary = i === selected.length - 1;
      box(r, 'sel' + (primary ? '' : ' alt'));
      if (primary && !editing) chip(r, chipLabel(el), 's', selected.length > 1 ? '+' + (selected.length - 1) : size(r));
    }
    if (menu) layer.appendChild(menu);
  }
  function redraw(){ if (!queued) { queued = true; (win.requestAnimationFrame || setTimeout)(draw); } }
  win.addEventListener('scroll', redraw, true);
  win.addEventListener('resize', redraw);
  if (win.ResizeObserver) { try { new ResizeObserver(redraw).observe(doc.body); } catch(_){} }

  // ── selection ────────────────────────────────────────────────────────────
  function report(){
    var p = selected[selected.length - 1];
    if (!p) { post({ type: 'deselect' }); return; }
    post({ type: 'select', info: info(p), others: selected.slice(0, -1).map(function(e){ return { oid: oidOf(e), name: name(e), templated: templated(e), classes: e.getAttribute('class') || '' }; }) });
  }
  function select(el, additive, reveal){
    closeMenu();
    if (!el) { selected = []; report(); redraw(); return; }
    if (additive) {
      var at = selected.indexOf(el);
      if (at > -1) { selected.splice(at, 1); } else { selected.push(el); }
    } else { selected = [el]; }
    if (reveal) {
      if (win.__app && win.__app.reveal) win.__app.reveal(el);
      var r = el.getBoundingClientRect();
      if (r.bottom < 0 || r.top > win.innerHeight) el.scrollIntoView({ block: 'center' });
    }
    report(); redraw();
  }
  function byOid(oid){
    if (!oid) return null;
    var all = doc.querySelectorAll('[data-oid="' + String(oid).replace(/"/g, '') + '"]');
    for (var i = 0; i < all.length; i++) { if (visible(all[i])) return all[i]; }
    return all[0] || null;
  }

  doc.addEventListener('mousemove', function(e){
    if (mode !== 'edit' || editing) return;
    var el = pickable(e.target);
    if (el !== hoverEl) { hoverEl = el; redraw(); }
  }, true);
  doc.addEventListener('mouseleave', function(){ hoverEl = null; redraw(); });
  function swallow(e){ if (mode === 'edit' && !(editing && editing.contains(e.target)) && !(menu && e.composedPath && e.composedPath().indexOf(menu) > -1)) { e.preventDefault(); e.stopPropagation(); } }
  ['mousedown', 'mouseup', 'pointerdown', 'submit', 'input', 'change'].forEach(function(t){ win.addEventListener(t, function(e){ if (t === 'mousedown' && menu && !(e.composedPath && e.composedPath().indexOf(menu) > -1)) closeMenu(); swallow(e); }, true); });
  win.addEventListener('click', function(e){
    if (mode !== 'edit') return;
    if (menu && e.composedPath && e.composedPath().indexOf(menu) > -1) return;
    if (editing && editing.contains(e.target)) return;
    e.preventDefault(); e.stopPropagation();
    if (editing) commitText();
    if (e.altKey) { layers(e); return; }
    var el = pickable(e.target);
    select(el, e.shiftKey || e.metaKey || e.ctrlKey, false);
  }, true);
  win.addEventListener('contextmenu', function(e){ if (mode === 'edit') { e.preventDefault(); layers(e); } }, true);
  win.addEventListener('dblclick', function(e){
    if (mode !== 'edit') return;
    e.preventDefault(); e.stopPropagation();
    var el = pickable(e.target);
    if (el) { select(el, false, false); startText(el); }
  }, true);

  // ── pick from layers: everything stacked under the pointer ───────────────
  function closeMenu(){ if (menu) { menu = null; redraw(); } }
  function layers(e){
    var stack = (doc.elementsFromPoint ? doc.elementsFromPoint(e.clientX, e.clientY) : []);
    var seen = [], items = [];
    stack.forEach(function(n){ var el = pickable(n); while (el) { if (seen.indexOf(el) === -1) { seen.push(el); items.push(el); } el = parentPick(el); } });
    if (!items.length) return;
    var m = doc.createElement('div'); m.className = 'menu'; m.setAttribute('role', 'menu');
    var h = doc.createElement('p'); h.textContent = 'Layers here'; m.appendChild(h);
    items.slice(0, 14).forEach(function(el){
      var b = doc.createElement('button'); b.type = 'button'; b.setAttribute('role', 'menuitem');
      b.textContent = name(el);
      if (selected.indexOf(el) > -1) b.setAttribute('aria-current', 'true');
      b.addEventListener('mouseenter', function(){ hoverEl = el; draw(); });
      b.addEventListener('click', function(ev){ ev.preventDefault(); ev.stopPropagation(); menu = null; select(el, false, false); });
      m.appendChild(b);
    });
    m.style.left = Math.min(e.clientX, win.innerWidth - 240) + 'px';
    m.style.top = Math.min(e.clientY, win.innerHeight - 40) + 'px';
    menu = m; draw();
    var first = m.querySelector('button'); if (first) first.focus();
  }

  // ── typing straight into the page ────────────────────────────────────────
  function startText(el){
    if (!textEditable(el)) { post({ type: 'notice', text: templated(el) ? 'This is drawn from a list, so its words come from the records. Ask the crew to change the pattern.' : 'Double-click the words themselves to type over them.' }); return; }
    editing = el; editingBefore = (el.textContent || '').replace(/\s+/g, ' ').trim();
    el.setAttribute('contenteditable', 'plaintext-only');
    if (el.contentEditable !== 'plaintext-only') el.setAttribute('contenteditable', 'true');
    el.focus();
    try { var range = doc.createRange(); range.selectNodeContents(el); var s = win.getSelection(); s.removeAllRanges(); s.addRange(range); } catch(_){}
    redraw();
  }
  function commitText(cancel){
    var el = editing; if (!el) return;
    editing = null;
    el.removeAttribute('contenteditable');
    var now = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (cancel) { el.textContent = editingBefore; }
    else if (now !== editingBefore) { el.textContent = now; post({ type: 'textEdited', oid: oidOf(el), text: now, prev: editingBefore }); }
    report(); redraw();
  }
  doc.addEventListener('focusout', function(e){ if (editing && e.target === editing) commitText(); }, true);

  // ── keys ─────────────────────────────────────────────────────────────────
  function typing(t){ return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)); }
  function key(e){
    var mod = e.metaKey || e.ctrlKey;
    if (editing) {
      if (e.key === 'Escape') { e.preventDefault(); commitText(true); }
      else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commitText(); }
      return;
    }
    if (mod && (e.key === 'i' || e.key === 'I')) { e.preventDefault(); post({ type: 'toggleMode' }); return; }
    if (mode !== 'edit') return;
    if (menu && e.key === 'Escape') { e.preventDefault(); closeMenu(); return; }
    if (mod && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); post({ type: e.shiftKey ? 'redo' : 'undo' }); return; }
    if (typing(e.target)) return;
    if ((e.key === 's' || e.key === 'S') && !mod && !e.altKey) { e.preventDefault(); post({ type: 'toggleMode' }); return; }
    var p = selected[selected.length - 1];
    if (e.key === 'ArrowUp' || e.key === 'Escape') {
      if (!p) return;
      e.preventDefault();
      var up = parentPick(p);
      // Esc climbs to the section, then lets go; the arrow climbs as far as it can.
      if (e.key === 'Escape' && (p.hasAttribute('data-section') || !up)) { select(null); return; }
      if (up) select(up, false, false);
    } else if (e.key === 'Enter' || e.key === 'ArrowDown') {
      if (!p) return;
      e.preventDefault();
      var down = firstChildPick(p); if (down) select(down, false, false);
    } else if (e.key === 'F2') {
      if (p) { e.preventDefault(); startText(p); }
    }
  }
  doc.addEventListener('keydown', key, true);

  // ── what the Preview tab sends ───────────────────────────────────────────
  function applyOps(ops){
    (ops || []).forEach(function(op){
      var all = doc.querySelectorAll('[data-oid="' + String(op.oid).replace(/"/g, '') + '"]');
      for (var i = 0; i < all.length; i++) {
        var el = all[i];
        if (op.kind === 'classes') {
          (op.remove || []).forEach(function(c){ el.classList.remove(c); });
          (op.add || []).forEach(function(c){ el.classList.add(c); });
        } else if (op.kind === 'text') {
          el.textContent = op.text;
        } else if (op.kind === 'attr') {
          if (op.value === null || op.value === undefined) el.removeAttribute(op.name); else el.setAttribute(op.name, op.value);
        }
      }
    });
    report();
    // Tailwind's CDN writes the CSS for a new class on its next tick.
    redraw(); setTimeout(function(){ report(); redraw(); }, 120);
  }
  win.addEventListener('message', function(e){
    var d = e.data; if (!d || !d.__preview) return;
    if (d.type === 'mode') {
      mode = d.mode === 'edit' ? 'edit' : 'use';
      if (mode === 'use') { if (editing) commitText(); hoverEl = null; closeMenu(); }
      doc.documentElement.style.cursor = mode === 'edit' ? 'default' : '';
      redraw();
    } else if (d.type === 'select') {
      select(byOid(d.oid), false, d.reveal !== false);
    } else if (d.type === 'highlight') {
      var s = null;
      try { s = d.id ? doc.querySelector('[data-section="' + String(d.id).replace(/"/g, '') + '"]') : null; } catch(_){}
      select(s, false, true);
    } else if (d.type === 'clear') {
      select(null);
    } else if (d.type === 'ops') {
      applyOps(d.ops);
    } else if (d.type === 'editText') {
      var p = selected[selected.length - 1]; if (p) startText(p);
    } else if (d.type === 'route?') {
      post({ type: 'route', path: win.__app && win.__app.current ? win.__app.current : '/' });
    }
  });
  post({ type: 'bridge' });
})();
</script>`;
