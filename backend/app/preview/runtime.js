/* The mockup's logic runtime — shipped by the platform, never written by a model.
 *
 * A 7B model asked to write a hash router, a store and form validation writes a
 * framework-shaped bug. So it writes markup and `data-*` bindings instead, and this
 * file is the framework they bind to. It is inlined into every generated mockup and
 * must never throw: the done-bar for a mockup is zero console errors on load.
 *
 * Bindings (the whole contract the section prompts describe):
 *   routes      [data-route="/path"]            one page; the first is home
 *               a[href="#/path"], [data-to]     navigation (aria-current on the active link)
 *   lists       [data-list="c"] > template      one clone per record, filtered + sorted
 *               [data-field="f"]                record value (formatted by field type)
 *               [data-limit="n"]                show the first n only
 *               [data-filter="c"]               search input, or select/button with
 *                                               data-filter-field (+ data-filter-value)
 *               [data-sort="c"]                 select of "field:asc|desc", or a button
 *                                               with data-sort-field
 *               [data-empty="c"] [data-count="c"]
 *               [data-action="remove"] [data-toggle="f"]   per-record actions
 *   stats       [data-stat="c.count" | "c.sum.f" | "c.avg.f" | "c.min.f" | "c.max.f"]
 *               [data-stat-where="f=value"]
 *   forms       form[data-form="c"]             validates, stores, re-renders, resets
 *               [data-error-for="f"] [data-success] [data-redirect="/path"]
 *   overlays    [data-modal="id"] [data-open="id"] [data-close] [data-toast="message"]
 *   misc        [data-year]
 */
(function () {
  "use strict";

  var doc = document;
  var win = window;

  function safe(fn) {
    return function () {
      try {
        return fn.apply(this, arguments);
      } catch (_) {
        /* A mockup that throws is a mockup with console errors; swallow and carry on. */
        return undefined;
      }
    };
  }

  function all(root, selector) {
    try {
      return Array.prototype.slice.call((root || doc).querySelectorAll(selector));
    } catch (_) {
      return [];
    }
  }

  function closest(el, selector) {
    return el && el.closest ? el.closest(selector) : null;
  }

  function post(message) {
    try {
      if (win.parent && win.parent !== win) {
        message.__preview = true;
        win.parent.postMessage(message, "*");
      }
    } catch (_) {
      /* the parent is gone or refuses — the page works without it */
    }
  }

  // ── data ──────────────────────────────────────────────────────────────────
  var app = { product: "", currency: "USD", routes: [], collections: {} };
  try {
    var dataEl = doc.getElementById("app-data");
    if (dataEl) {
      var parsed = JSON.parse(dataEl.textContent || "{}");
      for (var key in parsed) {
        if (Object.prototype.hasOwnProperty.call(parsed, key)) app[key] = parsed[key];
      }
    }
  } catch (_) {
    /* no data is a valid mockup — every binding below degrades to nothing */
  }

  var seq = 0;
  var store = {};
  Object.keys(app.collections || {}).forEach(function (name) {
    var c = app.collections[name] || {};
    store[name] = {
      label: c.label || name,
      fields: Array.isArray(c.fields) ? c.fields : [],
      rows: (Array.isArray(c.rows) ? c.rows : []).map(function (row) {
        var copy = { _id: "r" + ++seq };
        for (var k in row) {
          if (Object.prototype.hasOwnProperty.call(row, k)) copy[k] = row[k];
        }
        return copy;
      }),
    };
  });

  function fieldOf(collection, name) {
    var c = store[collection];
    if (!c) return null;
    for (var i = 0; i < c.fields.length; i++) {
      if (c.fields[i] && c.fields[i].name === name) return c.fields[i];
    }
    return null;
  }

  // ── formatting ────────────────────────────────────────────────────────────
  function format(value, type, hint) {
    var kind = hint || type || "text";
    if (value === null || value === undefined || value === "") return "—";
    try {
      if (kind === "currency") {
        return new Intl.NumberFormat(undefined, {
          style: "currency",
          currency: app.currency || "USD",
          maximumFractionDigits: Number(value) % 1 === 0 ? 0 : 2,
        }).format(Number(value));
      }
      if (kind === "number") return Number(value).toLocaleString();
      if (kind === "percent") return Math.round(Number(value)) + "%";
      if (kind === "date" || kind === "datetime") {
        var d = new Date(value);
        if (isNaN(d.getTime())) return String(value);
        var opts = { month: "short", day: "numeric", year: "numeric" };
        if (kind === "datetime") {
          opts.hour = "numeric";
          opts.minute = "2-digit";
        }
        return d.toLocaleDateString(undefined, opts);
      }
      if (kind === "boolean") return value === true || value === "true" ? "Yes" : "No";
    } catch (_) {
      /* fall through to the plain string */
    }
    return String(value);
  }

  function slug(value) {
    return String(value === undefined || value === null ? "" : value)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");
  }

  // ── querying: search, equality filters, sort — scoped per section ─────────
  var states = {};

  function scopeOf(el) {
    return closest(el, "[data-section]") || doc.body;
  }

  function scopeKey(scope, collection) {
    var id = scope && scope.getAttribute ? scope.getAttribute("data-section") : "";
    return (id || "page") + "::" + collection;
  }

  function stateFor(scope, collection) {
    var key = scopeKey(scope, collection);
    if (!states[key]) states[key] = { q: "", eq: {}, sort: null };
    return states[key];
  }

  function compare(a, b, type) {
    if (a === b) return 0;
    if (a === undefined || a === null || a === "") return 1;
    if (b === undefined || b === null || b === "") return -1;
    if (type === "number" || type === "currency" || type === "percent") {
      return Number(a) - Number(b);
    }
    if (type === "date" || type === "datetime") {
      return new Date(a).getTime() - new Date(b).getTime();
    }
    if (typeof a === "number" && typeof b === "number") return a - b;
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  }

  function query(collection, state) {
    var c = store[collection];
    if (!c) return [];
    var q = (state.q || "").trim().toLowerCase();
    var rows = c.rows.filter(function (row) {
      for (var f in state.eq) {
        if (!Object.prototype.hasOwnProperty.call(state.eq, f)) continue;
        var want = state.eq[f];
        if (want === "" || want === null || want === undefined) continue;
        if (slug(row[f]) !== slug(want)) return false;
      }
      if (!q) return true;
      for (var k in row) {
        if (k === "_id" || !Object.prototype.hasOwnProperty.call(row, k)) continue;
        if (String(row[k]).toLowerCase().indexOf(q) !== -1) return true;
      }
      return false;
    });
    if (state.sort && state.sort.field) {
      var field = fieldOf(collection, state.sort.field);
      var dir = state.sort.dir === "desc" ? -1 : 1;
      rows = rows.slice().sort(function (a, b) {
        return dir * compare(a[state.sort.field], b[state.sort.field], field && field.type);
      });
    }
    return rows;
  }

  // ── rendering ─────────────────────────────────────────────────────────────
  function bind(node, row, collection) {
    var targets = [node].concat(all(node, "[data-field]"));
    targets.forEach(function (el) {
      var name = el.getAttribute && el.getAttribute("data-field");
      if (!name) return;
      var field = fieldOf(collection, name);
      el.textContent = format(row[name], field && field.type, el.getAttribute("data-format"));
      el.setAttribute("data-value", slug(row[name]));
    });
    [node].concat(all(node, "[data-toggle]")).forEach(function (el) {
      var name = el.getAttribute && el.getAttribute("data-toggle");
      if (!name) return;
      var on = row[name] === true || row[name] === "true";
      if ("checked" in el) el.checked = on;
      el.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function templateOf(list) {
    for (var i = 0; i < list.children.length; i++) {
      if (list.children[i].tagName === "TEMPLATE") return list.children[i];
    }
    return list.querySelector("template");
  }

  function renderList(list) {
    var name = list.getAttribute("data-list");
    if (!store[name]) return;
    var tpl = templateOf(list);
    if (!tpl || !tpl.content) return;
    var host = tpl.parentNode || list;

    all(host, "[data-row]").forEach(function (el) {
      if (el.parentNode === host) host.removeChild(el);
    });

    var scope = scopeOf(list);
    var rows = query(name, stateFor(scope, name));
    var total = rows.length;
    var limit = parseInt(list.getAttribute("data-limit") || "0", 10);
    if (limit > 0) rows = rows.slice(0, limit);

    rows.forEach(function (row) {
      var frag = tpl.content.cloneNode(true);
      // Elements only: the template's whitespace would otherwise pile up beside the
      // rows on every re-render, since only [data-row] elements are cleared.
      Array.prototype.slice.call(frag.children).forEach(function (el) {
        el.setAttribute("data-row", row._id);
        bind(el, row, name);
        host.appendChild(el);
      });
    });

    all(scope, '[data-empty="' + name + '"]').forEach(function (el) {
      el.hidden = total > 0;
    });
    all(scope, '[data-count="' + name + '"]').forEach(function (el) {
      el.textContent = String(total);
    });
  }

  function renderStats() {
    all(doc, "[data-stat]").forEach(
      safe(function (el) {
        var parts = (el.getAttribute("data-stat") || "").split(".");
        var name = parts[0];
        var op = parts[1] || "count";
        var fieldName = parts[2];
        var c = store[name];
        if (!c) return;
        var rows = c.rows;
        var where = el.getAttribute("data-stat-where");
        if (where && where.indexOf("=") > 0) {
          var f = where.split("=")[0].trim();
          var v = where.split("=").slice(1).join("=").trim();
          rows = rows.filter(function (row) {
            return slug(row[f]) === slug(v);
          });
        }
        var result;
        if (op === "count" || !fieldName) {
          result = rows.length;
          el.textContent = format(result, "number", el.getAttribute("data-format"));
          return;
        }
        var numbers = rows
          .map(function (row) {
            return Number(row[fieldName]);
          })
          .filter(function (n) {
            return !isNaN(n);
          });
        if (!numbers.length) {
          el.textContent = "—";
          return;
        }
        var sum = numbers.reduce(function (a, b) {
          return a + b;
        }, 0);
        if (op === "sum") result = sum;
        else if (op === "avg") result = sum / numbers.length;
        else if (op === "min") result = Math.min.apply(null, numbers);
        else if (op === "max") result = Math.max.apply(null, numbers);
        else result = rows.length;
        var field = fieldOf(name, fieldName);
        var type = field && field.type;
        if (op === "avg" && type !== "currency" && type !== "percent") {
          result = Math.round(result * 10) / 10;
        }
        el.textContent = format(result, type === "text" ? "number" : type, el.getAttribute("data-format"));
      })
    );
  }

  function renderAll(collection) {
    all(doc, "[data-list]").forEach(
      safe(function (list) {
        if (!collection || list.getAttribute("data-list") === collection) renderList(list);
      })
    );
    renderStats();
  }

  function fillFilterOptions() {
    all(doc, "select[data-filter][data-filter-field]").forEach(
      safe(function (select) {
        if (select.options.length > 1) return;
        var name = select.getAttribute("data-filter");
        var fieldName = select.getAttribute("data-filter-field");
        var c = store[name];
        if (!c) return;
        var field = fieldOf(name, fieldName);
        var values = field && Array.isArray(field.options) && field.options.length
          ? field.options
          : c.rows
              .map(function (row) {
                return row[fieldName];
              })
              .filter(function (v, i, arr) {
                return v !== undefined && v !== null && v !== "" && arr.indexOf(v) === i;
              });
        if (!select.options.length) {
          var any = doc.createElement("option");
          any.value = "";
          any.textContent = "All";
          select.appendChild(any);
        }
        values.forEach(function (v) {
          var opt = doc.createElement("option");
          opt.value = String(v);
          opt.textContent = String(v);
          select.appendChild(opt);
        });
      })
    );
  }

  // ── routing ───────────────────────────────────────────────────────────────
  var current = "";

  function normalisePath(path) {
    var p = String(path || "/").trim();
    if (p.charAt(0) === "#") p = p.slice(1);
    if (p.charAt(0) !== "/") p = "/" + p;
    if (p.length > 1 && p.charAt(p.length - 1) === "/") p = p.slice(0, -1);
    return p;
  }

  function routeEls() {
    return all(doc, "[data-route]");
  }

  function go(path, quiet) {
    var target = normalisePath(path);
    var pages = routeEls();
    if (!pages.length) return;
    var match = null;
    pages.forEach(function (el) {
      if (normalisePath(el.getAttribute("data-route")) === target) match = el;
    });
    if (!match) {
      if (!quiet) toast("That page isn't part of this mockup.");
      match = pages[0];
      target = normalisePath(match.getAttribute("data-route"));
    }
    pages.forEach(function (el) {
      var on = el === match;
      el.hidden = !on;
      if (el.classList) el.classList.toggle("is-active", on);
    });
    all(doc, 'a[href^="#/"], [data-to]').forEach(function (el) {
      var to = el.getAttribute("data-to") || el.getAttribute("href");
      var here = normalisePath(to) === target;
      if (here) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
      if (el.classList) el.classList.toggle("is-active", here);
    });
    var title = match.getAttribute("data-route-title") || "";
    try {
      doc.title = title && app.product ? title + " · " + app.product : app.product || title;
    } catch (_) {
      /* title is cosmetic */
    }
    if (current && current !== target) {
      try {
        win.scrollTo(0, 0);
      } catch (_) {
        /* scrolling is cosmetic */
      }
    }
    current = target;
    post({ type: "route", path: target, title: title });
  }

  function routeOf(el) {
    var page = closest(el, "[data-route]");
    return page ? normalisePath(page.getAttribute("data-route")) : null;
  }

  function reveal(el) {
    var path = routeOf(el);
    if (path && path !== current) go(path, true);
    try {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    } catch (_) {
      /* cosmetic */
    }
  }

  // ── overlays ──────────────────────────────────────────────────────────────
  var toastHost = null;

  function toast(message) {
    if (!message) return;
    if (!toastHost) {
      toastHost = doc.createElement("div");
      toastHost.className = "app-toasts";
      toastHost.setAttribute("role", "status");
      toastHost.setAttribute("aria-live", "polite");
      doc.body.appendChild(toastHost);
    }
    var item = doc.createElement("div");
    item.className = "app-toast";
    item.textContent = message;
    toastHost.appendChild(item);
    setTimeout(function () {
      if (item.classList) item.classList.add("is-leaving");
    }, 2600);
    setTimeout(function () {
      if (item.parentNode) item.parentNode.removeChild(item);
    }, 3000);
  }

  var lastFocus = null;

  function openModal(id) {
    var modal = doc.querySelector('[data-modal="' + String(id).replace(/"/g, "") + '"]');
    if (!modal) return;
    lastFocus = doc.activeElement;
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    var focusable = modal.querySelector("input, select, textarea, button");
    if (focusable) focusable.focus();
  }

  function closeModal(modal) {
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  // ── forms ─────────────────────────────────────────────────────────────────
  function errorSlot(form, input) {
    var name = input.getAttribute("name");
    var slot = name ? form.querySelector('[data-error-for="' + name + '"]') : null;
    if (slot) return slot;
    var next = input.nextElementSibling;
    if (next && next.hasAttribute && next.hasAttribute("data-generated-error")) return next;
    slot = doc.createElement("p");
    slot.className = "form-error";
    slot.setAttribute("data-generated-error", "");
    slot.setAttribute("role", "alert");
    if (input.parentNode) input.parentNode.insertBefore(slot, input.nextSibling);
    return slot;
  }

  function showError(form, input, message) {
    var slot = errorSlot(form, input);
    slot.textContent = message || "";
    slot.hidden = !message;
    if (message) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }

  function prepareForms() {
    all(doc, "form").forEach(
      safe(function (form) {
        form.noValidate = true;
        form.removeAttribute("action");
        var name = form.getAttribute("data-form");
        var c = store[name];
        if (!c) return;
        c.fields.forEach(function (field) {
          if (!field || !field.name) return;
          var input = form.querySelector('[name="' + field.name + '"]');
          if (!input) return;
          if (field.required && !input.hasAttribute("required")) input.setAttribute("required", "");
          if (input.tagName === "SELECT" && input.options.length === 0 && Array.isArray(field.options)) {
            field.options.forEach(function (option) {
              var opt = doc.createElement("option");
              opt.value = String(option);
              opt.textContent = String(option);
              input.appendChild(opt);
            });
          }
        });
      })
    );
  }

  function coerce(value, field) {
    var type = field && field.type;
    if (type === "number" || type === "currency" || type === "percent") {
      var n = parseFloat(value);
      return isNaN(n) ? null : n;
    }
    return value;
  }

  function defaultFor(field) {
    var type = field.type;
    if (type === "date" || type === "datetime") return new Date().toISOString().slice(0, 10);
    if (type === "boolean") return false;
    if (type === "select" && Array.isArray(field.options) && field.options.length) return field.options[0];
    if (type === "number" || type === "currency" || type === "percent") return 0;
    return "";
  }

  var submitForm = safe(function (form) {
    var invalid = [];
    all(form, "input, select, textarea").forEach(function (input) {
      if (!input.name || input.disabled || input.type === "hidden") return;
      if (input.checkValidity && !input.checkValidity()) {
        invalid.push(input);
        showError(form, input, input.validationMessage || "Check this field.");
      } else {
        showError(form, input, "");
      }
    });
    if (invalid.length) {
      invalid[0].focus();
      return;
    }

    var name = form.getAttribute("data-form");
    var c = store[name];
    if (c) {
      var row = { _id: "r" + ++seq };
      c.fields.forEach(function (field) {
        if (field && field.name) row[field.name] = defaultFor(field);
      });
      all(form, "input, select, textarea").forEach(function (input) {
        if (!input.name) return;
        var field = fieldOf(name, input.name);
        if (input.type === "checkbox") row[input.name] = !!input.checked;
        else if (input.type === "radio") {
          if (input.checked) row[input.name] = input.value;
        } else row[input.name] = coerce(input.value, field);
      });
      c.rows.unshift(row);
      renderAll(name);
    }

    form.reset();
    toast(form.getAttribute("data-success") || (c ? "Saved to " + c.label + "." : "Sent — this is a preview."));
    var modal = closest(form, "[data-modal]");
    if (modal) closeModal(modal);
    var redirect = form.getAttribute("data-redirect");
    if (redirect) go(redirect);
  });

  // ── events ────────────────────────────────────────────────────────────────
  doc.addEventListener(
    "submit",
    safe(function (e) {
      e.preventDefault();
      if (e.target && e.target.tagName === "FORM") submitForm(e.target);
    })
  );

  doc.addEventListener(
    "input",
    safe(function (e) {
      var t = e.target;
      if (!t || !t.getAttribute) return;
      if (t.getAttribute("aria-invalid") === "true") {
        var form = closest(t, "form");
        if (form && t.checkValidity && t.checkValidity()) showError(form, t, "");
      }
      var name = t.getAttribute("data-filter");
      if (name && t.tagName === "INPUT" && t.type !== "checkbox") {
        stateFor(scopeOf(t), name).q = t.value || "";
        renderScope(scopeOf(t), name);
      }
    })
  );

  doc.addEventListener(
    "change",
    safe(function (e) {
      var t = e.target;
      if (!t || !t.getAttribute) return;
      var scope = scopeOf(t);
      if (t.tagName === "SELECT" && t.hasAttribute("data-filter")) {
        var name = t.getAttribute("data-filter");
        var state = stateFor(scope, name);
        var field = t.getAttribute("data-filter-field");
        if (field) state.eq[field] = t.value;
        else state.q = t.value;
        renderScope(scope, name);
        return;
      }
      if (t.tagName === "SELECT" && t.hasAttribute("data-sort")) {
        var sortName = t.getAttribute("data-sort");
        var parts = String(t.value || "").split(":");
        stateFor(scope, sortName).sort = parts[0] ? { field: parts[0], dir: parts[1] || "asc" } : null;
        renderScope(scope, sortName);
        return;
      }
      var toggle = t.getAttribute("data-toggle");
      if (toggle) flip(t, toggle);
    })
  );

  function renderScope(scope, name) {
    all(scope, '[data-list="' + name + '"]').forEach(safe(renderList));
  }

  function rowFor(el) {
    var holder = closest(el, "[data-row]");
    if (!holder) return null;
    // Rows are appended beside their template, which is the list itself or sits
    // inside it — `closest` covers both, a <tbody data-list> included.
    var list = closest(holder.parentNode, "[data-list]");
    var name = list ? list.getAttribute("data-list") : null;
    if (!name || !store[name]) return null;
    var id = holder.getAttribute("data-row");
    var rows = store[name].rows;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i]._id === id) return { name: name, index: i, row: rows[i] };
    }
    return null;
  }

  function flip(el, field) {
    var hit = rowFor(el);
    if (!hit) return;
    hit.row[field] = !(hit.row[field] === true || hit.row[field] === "true");
    renderAll(hit.name);
  }

  doc.addEventListener(
    "click",
    safe(function (e) {
      var t = e.target;
      if (!t || !t.closest) return;

      var to = closest(t, "[data-to]");
      if (to) {
        e.preventDefault();
        go(to.getAttribute("data-to"));
        return;
      }

      var opener = closest(t, "[data-open]");
      if (opener) {
        e.preventDefault();
        openModal(opener.getAttribute("data-open"));
        return;
      }

      var closer = closest(t, "[data-close]");
      if (closer) {
        e.preventDefault();
        closeModal(closest(closer, "[data-modal]"));
        return;
      }

      if (t.hasAttribute && t.hasAttribute("data-modal")) {
        closeModal(t); // a click on the backdrop itself
        return;
      }

      var remover = closest(t, '[data-action="remove"]');
      if (remover) {
        e.preventDefault();
        var hit = rowFor(remover);
        if (hit) {
          store[hit.name].rows.splice(hit.index, 1);
          renderAll(hit.name);
          toast("Removed.");
        }
        return;
      }

      var toggler = closest(t, "[data-toggle]");
      if (toggler && !("checked" in toggler)) {
        e.preventDefault();
        flip(toggler, toggler.getAttribute("data-toggle"));
        return;
      }

      var filterBtn = closest(t, "button[data-filter], [role=button][data-filter]");
      if (filterBtn) {
        e.preventDefault();
        var fname = filterBtn.getAttribute("data-filter");
        var ffield = filterBtn.getAttribute("data-filter-field");
        var fvalue = filterBtn.getAttribute("data-filter-value") || "";
        var fscope = scopeOf(filterBtn);
        var fstate = stateFor(fscope, fname);
        if (ffield) fstate.eq[ffield] = fvalue;
        else fstate.q = fvalue;
        all(fscope, '[data-filter="' + fname + '"][data-filter-value]').forEach(function (b) {
          var same = (b.getAttribute("data-filter-field") || "") === (ffield || "");
          if (same) b.setAttribute("aria-pressed", b === filterBtn ? "true" : "false");
        });
        renderScope(fscope, fname);
        return;
      }

      var sortBtn = closest(t, "button[data-sort], [role=button][data-sort]");
      if (sortBtn) {
        e.preventDefault();
        var sname = sortBtn.getAttribute("data-sort");
        var sfield = sortBtn.getAttribute("data-sort-field");
        if (!sfield) return;
        var sscope = scopeOf(sortBtn);
        var sstate = stateFor(sscope, sname);
        var dir =
          sstate.sort && sstate.sort.field === sfield && sstate.sort.dir === "asc" ? "desc" : "asc";
        sstate.sort = { field: sfield, dir: dir };
        all(sscope, '[data-sort="' + sname + '"][data-sort-field]').forEach(function (b) {
          if (b === sortBtn) b.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
          else b.removeAttribute("aria-sort");
        });
        renderScope(sscope, sname);
        return;
      }

      var toaster = closest(t, "[data-toast]");
      if (toaster) {
        e.preventDefault();
        toast(toaster.getAttribute("data-toast"));
        return;
      }

      var a = closest(t, "a[href]");
      if (a) {
        var href = a.getAttribute("href") || "";
        if (href.indexOf("#/") === 0) {
          e.preventDefault();
          go(href.slice(1));
          return;
        }
        if (href === "#" || href === "") {
          e.preventDefault();
          return;
        }
        if (href.charAt(0) === "#") {
          e.preventDefault();
          var anchor = doc.getElementById(href.slice(1));
          if (anchor) reveal(anchor);
          return;
        }
        // The preview never navigates away from itself: in a sandboxed srcdoc frame
        // a relative link resolves against the *parent* app and loads it in here.
        e.preventDefault();
        toast("Links out of the mockup are switched off in the preview.");
      }
    })
  );

  doc.addEventListener(
    "keydown",
    safe(function (e) {
      if (e.key !== "Escape") return;
      all(doc, "[data-modal].is-open").forEach(closeModal);
    })
  );

  win.addEventListener(
    "message",
    safe(function (e) {
      var d = e.data;
      if (d && d.__preview && d.type === "go" && d.path) go(d.path, true);
    })
  );

  // ── boot ──────────────────────────────────────────────────────────────────
  var boot = safe(function () {
    all(doc, "[data-year]").forEach(function (el) {
      el.textContent = String(new Date().getFullYear());
    });
    all(doc, "[data-modal]").forEach(function (el) {
      el.setAttribute("role", "dialog");
      el.setAttribute("aria-modal", "true");
      el.setAttribute("aria-hidden", "true");
    });
    fillFilterOptions();
    prepareForms();
    renderAll();
    var first = routeEls()[0];
    go(first ? first.getAttribute("data-route") : "/", true);
    post({
      type: "ready",
      routes: routeEls().map(function (el) {
        return {
          path: normalisePath(el.getAttribute("data-route")),
          title: el.getAttribute("data-route-title") || "",
        };
      }),
    });
  });

  win.__app = {
    go: safe(go),
    reveal: safe(reveal),
    routeOf: safe(routeOf),
    store: store,
    get current() {
      return current;
    },
  };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
