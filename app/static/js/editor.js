// Form editor for workbook catalogs. Loads the catalog as JSON, renders forms,
// saves the whole catalog back (the server validates and writes YAML).
(function () {
  "use strict";

  var root = document.querySelector("[data-editor]");
  if (!root || !window.fetch) return;

  var workbookId = root.getAttribute("data-editor");
  var T = {};
  var state = null;        // catalog data; levels are edited as state._levels (array)
  var baseHash = null;
  var locked = {};         // task id -> [answer ids] with saved data
  var origIds = new WeakMap();
  var uids = new WeakMap();
  var uidSeq = 0;
  var openTasks = {};      // uid -> true
  var dirty = false;
  var busy = false;

  var LEVEL_COLORS = ["blue", "ochre", "green", "grey", "red"];
  var ANSWER_TYPES = ["text", "short", "checklist", "choice", "date"];
  var HEADER_TYPES = ["short", "text", "date"];
  var BLOCK_TYPES = ["text", "table", "snippet", "steps", "questions", "image", "note"];
  var COLOR_KEYS = ["paper", "card", "ink", "muted", "line", "accent", "accent_hover",
    "accent_soft", "ok", "redo", "hint_bg", "trainer_bg", "bonus_bg"];

  // ------------------------------------------------------------------ helpers

  function t(key, params) {
    var text = T[key] || key;
    Object.keys(params || {}).forEach(function (k) {
      text = text.split("{" + k + "}").join(params[k]);
    });
    return text;
  }

  function uid(obj) {
    if (!uids.has(obj)) uids.set(obj, "u" + (++uidSeq));
    return uids.get(obj);
  }

  function el(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") node.textContent = v;
      else if (k === "className") node.className = v;
      else node.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var child = arguments[i];
      if (child === null || child === undefined || child === false) continue;
      if (Array.isArray(child)) child.forEach(function (c) { if (c) node.appendChild(c); });
      else node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  function getPath(path) {
    return path.split(".").reduce(function (obj, part) {
      return obj === undefined || obj === null ? undefined : obj[part];
    }, state);
  }

  function setPath(path, value) {
    var parts = path.split(".");
    var obj = state;
    for (var i = 0; i < parts.length - 1; i++) {
      var next = parts[i + 1];
      if (obj[parts[i]] === undefined || obj[parts[i]] === null) {
        obj[parts[i]] = /^\d+$/.test(next) ? [] : {};
      }
      obj = obj[parts[i]];
    }
    var last = parts[parts.length - 1];
    if (value === undefined) delete obj[last];
    else obj[last] = value;
  }

  function api(method, url, body) {
    return fetch(url, {
      method: method,
      credentials: "same-origin",
      headers: { "X-Workbook": "1", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { status: response.status, ok: response.ok, data: data };
      });
    });
  }

  function setDirty(value) {
    dirty = value;
    status(dirty ? "unsaved" : null);
  }

  function status(key, params, kind) {
    var box = document.querySelector("[data-editor-status]");
    if (!box) return;
    box.textContent = key ? t(key, params) : "";
    box.className = "editor-status" + (kind ? " is-" + kind : (key === "unsaved" ? " is-dirty" : ""));
  }

  function slug(text) {
    return (text || "").toLowerCase()
      .replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss")
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  }

  function uniqueId(base, taken) {
    var id = base || "item";
    var n = 1;
    while (taken.indexOf(id) !== -1) { n += 1; id = base + "-" + n; }
    return id;
  }

  function allTaskIds() {
    var ids = [];
    (state.days || []).forEach(function (d) {
      (d.modules || []).forEach(function (m) {
        (m.tasks || []).forEach(function (task) { ids.push(task.id); });
      });
    });
    return ids;
  }

  function isLockedTask(task) {
    var orig = origIds.get(task);
    return !!orig && orig === task.id && Object.prototype.hasOwnProperty.call(locked, orig);
  }

  function isLockedAnswer(task, answer) {
    var orig = origIds.get(task);
    var answers = orig ? locked[orig] : null;
    return !!answers && origIds.get(answer) === answer.id && answers.indexOf(answer.id) !== -1;
  }

  // ------------------------------------------------------------------ form fields

  function field(label, path, kind, opts) {
    opts = opts || {};
    var id = "f-" + path.replace(/\./g, "-");
    var value = getPath(path);
    var input;
    if (kind === "textarea" || kind === "lines" || kind === "rows") {
      if (kind === "lines") value = (value || []).join("\n");
      if (kind === "rows") value = (value || []).map(function (r) { return r.join(" | "); }).join("\n");
      input = el("textarea", { id: id, rows: opts.rows || 3, className: opts.mono ? "mono-input" : null });
      input.value = value || "";
    } else if (kind === "select") {
      input = el("select", { id: id });
      (opts.options || []).forEach(function (o) {
        var option = el("option", { value: o[0], text: o[1] });
        if (String(value === undefined ? (opts.fallback || "") : value) === o[0]) option.selected = true;
        input.appendChild(option);
      });
    } else if (kind === "checkbox") {
      input = el("input", { id: id, type: "checkbox" });
      input.checked = value === undefined ? !!opts.fallback : !!value;
    } else {
      input = el("input", { id: id, type: kind === "number" ? "number" : "text",
        className: opts.mono ? "mono-input" : null, min: kind === "number" ? "1" : null });
      input.value = value === undefined || value === null ? "" : value;
    }
    input.setAttribute("data-path", path);
    input.setAttribute("data-kind", kind);
    if (opts.rerender) input.setAttribute("data-rerender", "");
    if (opts.mirror) input.setAttribute("data-mirror-source", opts.mirror);
    if (opts.readonly) {
      input.readOnly = true;
      input.setAttribute("aria-readonly", "true");
    }
    var wrap = el("div", { className: "field" + (kind === "checkbox" ? " field-check" : "") + (opts.wide ? " field-wide" : ""), "data-field": path });
    if (kind === "checkbox") {
      wrap.appendChild(el("label", { className: "option", "for": id }, input, el("span", { text: label })));
    } else {
      wrap.appendChild(el("label", { className: "micro-label", "for": id, text: label }));
      wrap.appendChild(input);
    }
    if (opts.hint) wrap.appendChild(el("p", { className: "field-hint", text: opts.hint }));
    return wrap;
  }

  function parse(kind, input) {
    if (kind === "checkbox") return input.checked;
    if (kind === "number") return input.value === "" ? undefined : Number(input.value);
    if (kind === "lines") {
      return input.value.split("\n").map(function (s) { return s.trim(); })
        .filter(function (s) { return s; });
    }
    if (kind === "rows") {
      return input.value.split("\n").filter(function (s) { return s.trim(); }).map(function (line) {
        return line.split("|").map(function (c) { return c.trim(); });
      });
    }
    return input.value;
  }

  function button(action, path, label, opts) {
    opts = opts || {};
    return el("button", { type: "button", className: "button " + (opts.primary ? "" : "button-quiet"),
      "data-action": action, "data-path": path, "data-value": opts.value,
      "aria-label": opts.aria || null, title: opts.aria || null, text: label });
  }

  function itemTools(listPath, index, length) {
    var path = listPath + "." + index;
    return el("span", { className: "item-tools" },
      index > 0 ? button("move-up", path, "↑", { aria: t("move_up") }) : null,
      index < length - 1 ? button("move-down", path, "↓", { aria: t("move_down") }) : null,
      button("remove", path, "×", { aria: t("remove") }));
  }

  function options(values, prefix) {
    return values.map(function (v) { return [v, t(prefix + v)]; });
  }

  function levelOptions() {
    return (state._levels || []).map(function (l) { return [l.key, (l.label || l.key) + " (" + l.key + ")"]; });
  }

  // ------------------------------------------------------------------ sections

  function metaSection() {
    var p = "workbook";
    return el("section", { className: "panel editor-section" },
      el("h2", { className: "section-title", text: t("section_meta") }),
      el("div", { className: "field-grid" },
        field(t("field_id"), p + ".id", "text", { mono: true, readonly: true, hint: t("hint_workbook_id") }),
        field(t("field_version"), p + ".version", "text", { mono: true, hint: t("hint_version") }),
        field(t("field_title"), p + ".title", "text", { wide: true }),
        field(t("field_subtitle"), p + ".subtitle", "text", { wide: true }),
        field(t("field_brand"), p + ".brand", "text"),
        field(t("field_footer"), p + ".footer", "text"),
        field(t("field_description"), p + ".description", "textarea", { wide: true, rows: 2 }),
        field(t("field_intro"), p + ".intro", "textarea", { wide: true, rows: 8, hint: t("hint_markdown") }),
        field(t("signoff_enabled"), p + ".signoff.enabled", "checkbox", { fallback: true }),
        field(t("signoff_title"), p + ".signoff.title", "text")),
      headerFields(),
      colorsSection());
  }

  function headerFields() {
    var list = getPath("workbook.header_fields") || [];
    return el("div", { className: "editor-sub" },
      el("h3", { className: "micro-label", text: t("section_header_fields") }),
      list.map(function (f, i) {
        var p = "workbook.header_fields." + i;
        return el("div", { className: "editor-row" },
          field(t("field_id"), p + ".id", "text", { mono: true }),
          field(t("header_label"), p + ".label", "text"),
          field(t("header_type"), p + ".type", "select", { options: options(HEADER_TYPES, "header_type_"), fallback: "short" }),
          field(t("answer_placeholder"), p + ".placeholder", "text"),
          itemTools("workbook.header_fields", i, list.length));
      }),
      button("add-header-field", "workbook.header_fields", "+ " + t("add_header_field")));
  }

  function colorsSection() {
    var p = "workbook.stylesheet";
    var details = el("details", { className: "editor-details" },
      el("summary", { text: t("section_colors") }),
      el("p", { className: "field-hint", text: t("hint_colors") }),
      el("div", { className: "field-grid color-grid" },
        COLOR_KEYS.map(function (k) {
          return field(t("color_" + k), p + "." + k, "text", { mono: true });
        }),
        LEVEL_COLORS.map(function (k) {
          return field(t("level_color_label", { color: t("color_name_" + k) }), p + ".level_palette." + k, "text", { mono: true });
        })));
    if (getPath(p)) details.open = true;
    return details;
  }

  function levelsSection() {
    var list = state._levels || [];
    return el("section", { className: "panel editor-section" },
      el("h2", { className: "section-title", text: t("section_levels") }),
      list.map(function (l, i) {
        var p = "_levels." + i;
        return el("div", { className: "editor-row" },
          field(t("level_key"), p + ".key", "text", { mono: true }),
          field(t("level_label"), p + ".label", "text"),
          field(t("level_color"), p + ".color", "select", { options: options(LEVEL_COLORS, "color_name_"), fallback: "blue" }),
          field(t("level_description"), p + ".description", "text"),
          itemTools("_levels", i, list.length));
      }),
      button("add-level", "_levels", "+ " + t("add_level")));
  }

  function taskNumber(day, dayIndex, position, task) {
    if (task.number) return task.number;
    var prefix = day.optional ? (day.nav_tag || String(dayIndex + 1)) : String(dayIndex + 1);
    return prefix + "." + position;
  }

  function daysSection() {
    var days = state.days || [];
    return el("section", { className: "editor-section" },
      el("h2", { className: "section-title", text: t("section_days") }),
      days.map(function (day, di) {
        var dp = "days." + di;
        var position = 0;
        return el("div", { className: "panel editor-day" },
          el("div", { className: "editor-head" },
            el("span", { className: "micro-label", text: t("day_label", { n: di + 1 }) }),
            el("strong", { "data-mirror": dp + ".title", text: day.title || "" }),
            itemTools("days", di, days.length)),
          el("div", { className: "field-grid" },
            field(t("field_id"), dp + ".id", "text", { mono: true }),
            field(t("field_title"), dp + ".title", "text", { mirror: dp + ".title" }),
            field(t("field_subtitle"), dp + ".subtitle", "text", { wide: true }),
            field(t("day_nav_label"), dp + ".nav_label", "text"),
            field(t("day_nav_tag"), dp + ".nav_tag", "text", { mono: true }),
            field(t("day_optional"), dp + ".optional", "checkbox")),
          (day.modules || []).map(function (module, mi) {
            var mp = dp + ".modules." + mi;
            return el("div", { className: "editor-module" },
              el("div", { className: "editor-head" },
                el("span", { className: "module-code", "data-mirror": mp + ".code", text: module.code || "" }),
                el("strong", { "data-mirror": mp + ".title", text: module.title || "" }),
                itemTools(dp + ".modules", mi, day.modules.length)),
              el("div", { className: "field-grid" },
                field(t("module_code"), mp + ".code", "text", { mono: true, mirror: mp + ".code" }),
                field(t("field_title"), mp + ".title", "text", { mirror: mp + ".title" }),
                field(t("module_objective"), mp + ".objective", "textarea", { wide: true, rows: 2, hint: t("hint_markdown") })),
              el("ol", { className: "editor-tasks" },
                (module.tasks || []).map(function (task, ti) {
                  position += 1;
                  return taskItem(task, mp + ".tasks", ti, module.tasks.length, taskNumber(day, di, position, task));
                })),
              button("add-task", mp + ".tasks", "+ " + t("add_task")));
          }),
          button("add-module", dp + ".modules", "+ " + t("add_module")));
      }),
      button("add-day", "days", "+ " + t("add_day"), { primary: true }));
  }

  function taskItem(task, listPath, index, length, number) {
    var tp = listPath + "." + index;
    var isOpen = !!openTasks[uid(task)];
    var level = (state._levels || []).filter(function (l) { return l.key === task.level; })[0];
    var item = el("li", { className: "editor-task" + (isOpen ? " is-open" : ""), "data-task-path": tp },
      el("div", { className: "editor-task-row" },
        el("span", { className: "task-number", text: number }),
        el("span", { className: "editor-task-title", "data-mirror": tp + ".title", text: task.title || t("untitled") }),
        level ? el("span", { className: "level level-" + (level.color || "grey"), text: level.label || level.key }) : null,
        isLockedTask(task) ? el("span", { className: "state state-waiting", text: t("has_answers") }) : null,
        el("span", { className: "item-tools" },
          button("toggle-task", tp, isOpen ? t("close") : t("edit")),
          index > 0 ? button("move-up", tp, "↑", { aria: t("move_up") }) : null,
          index < length - 1 ? button("move-down", tp, "↓", { aria: t("move_down") }) : null,
          button("remove", tp, "×", { aria: t("remove") }))));
    if (isOpen) item.appendChild(taskEditor(task, tp));
    return item;
  }

  function taskEditor(task, tp) {
    var idLocked = isLockedTask(task);
    var useBlocks = Array.isArray(task.blocks);
    return el("div", { className: "task-editor" },
      el("div", { className: "field-grid" },
        field(t("task_title"), tp + ".title", "text", { wide: true, mirror: tp + ".title" }),
        field(t("field_id"), tp + ".id", "text", { mono: true, readonly: idLocked,
          hint: idLocked ? t("id_locked") : t("hint_id") }),
        field(t("task_number"), tp + ".number", "text", { mono: true, hint: t("hint_number") }),
        field(t("task_level"), tp + ".level", "select", { options: levelOptions(), rerender: true }),
        field(t("task_duration"), tp + ".duration", "text", { mono: true, hint: t("hint_duration") })),
      el("fieldset", { className: "field layout-choice" },
        el("legend", { className: "micro-label", text: t("task_layout") }),
        el("label", { className: "option" },
          el("input", { type: "radio", name: "layout-" + tp, "data-action": "set-layout", "data-path": tp, "data-value": "fixed", checked: !useBlocks }),
          el("span", { text: t("layout_fixed") })),
        el("label", { className: "option" },
          el("input", { type: "radio", name: "layout-" + tp, "data-action": "set-layout", "data-path": tp, "data-value": "blocks", checked: useBlocks }),
          el("span", { text: t("layout_blocks") }))),
      useBlocks ? blocksEditor(task, tp) : el("div", { className: "field-grid" },
        field(t("task_requirement"), tp + ".requirement", "textarea", { wide: true, rows: 4, hint: t("hint_markdown") }),
        field(t("task_snippet"), tp + ".snippet", "textarea", { wide: true, rows: 4, mono: true, hint: t("hint_snippet") }),
        field(t("task_steps"), tp + ".steps", "lines", { wide: true, rows: 4, hint: t("hint_lines") }),
        field(t("task_questions"), tp + ".guiding_questions", "lines", { wide: true, rows: 3, hint: t("hint_lines") })),
      el("div", { className: "field-grid" },
        field(t("task_hints"), tp + ".hints", "textarea", { wide: true, rows: 5, hint: t("hint_markdown") })),
      answersEditor(task, tp),
      el("div", { className: "field-grid" },
        field(t("task_bonus"), tp + ".bonus", "textarea", { wide: true, rows: 3, hint: t("hint_markdown") })),
      el("div", { className: "trainer editor-trainer" },
        el("span", { className: "micro-label trainer-head", text: t("trainer_heading") }),
        el("div", { className: "field-grid" },
          field(t("task_expectations"), tp + ".trainer.expectations", "lines", { wide: true, rows: 3, hint: t("hint_lines") }),
          field(t("task_notes"), tp + ".trainer.notes", "textarea", { wide: true, rows: 2 }))),
      el("div", { className: "editor-preview-bar" },
        button("preview-task", tp, t("preview")),
        button("toggle-task", tp, t("close"))),
      el("div", { className: "editor-preview", "data-preview": tp }));
  }

  function answersEditor(task, tp) {
    var list = task.answers || [];
    return el("div", { className: "editor-sub" },
      el("h3", { className: "micro-label", text: t("answers") }),
      list.map(function (a, i) {
        var ap = tp + ".answers." + i;
        var type = a.type || "text";
        var aLocked = isLockedAnswer(task, a);
        return el("div", { className: "editor-answer" },
          el("div", { className: "editor-row" },
            field(t("field_id"), ap + ".id", "text", { mono: true, readonly: aLocked, hint: aLocked ? t("id_locked") : null }),
            field(t("answer_type"), ap + ".type", "select", { options: options(ANSWER_TYPES, "answer_type_"), fallback: "text", rerender: true }),
            field(t("answer_label"), ap + ".label", "text"),
            itemTools(tp + ".answers", i, list.length)),
          el("div", { className: "editor-row" },
            type === "text" || type === "short" ? field(t("answer_placeholder"), ap + ".placeholder", type === "text" ? "textarea" : "text", { rows: 2 }) : null,
            type === "text" ? field(t("answer_height"), ap + ".height", "number") : null,
            type === "text" || type === "short" ? field(t("answer_monospace"), ap + ".monospace", "checkbox") : null,
            type === "checklist" ? field(t("answer_items"), ap + ".items", "lines", { rows: 3, hint: t("hint_lines") }) : null,
            type === "choice" ? field(t("answer_options"), ap + ".options", "lines", { rows: 3, hint: t("hint_lines") }) : null,
            type === "choice" ? field(t("answer_multiple"), ap + ".multiple", "checkbox") : null));
      }),
      button("add-answer", tp + ".answers", "+ " + t("add_answer")));
  }

  function blocksEditor(task, tp) {
    var list = task.blocks || [];
    return el("div", { className: "editor-sub" },
      el("h3", { className: "micro-label", text: t("blocks") }),
      list.map(function (b, i) {
        var bp = tp + ".blocks." + i;
        var fields = [];
        if (b.type === "text") fields.push(field(t("block_content"), bp + ".content", "textarea", { wide: true, rows: 4, hint: t("hint_markdown") }));
        if (b.type === "table") {
          fields.push(field(t("block_caption"), bp + ".caption", "text"));
          fields.push(field(t("block_columns"), bp + ".columns", "lines", { rows: 3, hint: t("hint_lines") }));
          fields.push(field(t("block_rows"), bp + ".rows", "rows", { wide: true, rows: 4, mono: true, hint: t("hint_rows") }));
        }
        if (b.type === "snippet") {
          fields.push(field(t("block_caption"), bp + ".caption", "text"));
          fields.push(field(t("block_content"), bp + ".content", "textarea", { wide: true, rows: 4, mono: true }));
        }
        if (b.type === "steps" || b.type === "questions") {
          fields.push(field(t("block_items"), bp + ".items", "lines", { wide: true, rows: 4, hint: t("hint_lines") }));
        }
        if (b.type === "image") {
          fields.push(field(t("block_src"), bp + ".src", "text", { mono: true, hint: t("hint_src") }));
          fields.push(field(t("block_alt"), bp + ".alt", "text"));
          fields.push(field(t("block_caption"), bp + ".caption", "text"));
        }
        if (b.type === "note") {
          fields.push(field(t("block_variant"), bp + ".variant", "select", { options: [["info", t("variant_info")], ["warning", t("variant_warning")]], fallback: "info" }));
          fields.push(field(t("block_content"), bp + ".content", "textarea", { wide: true, rows: 3, hint: t("hint_markdown") }));
        }
        return el("div", { className: "editor-answer" },
          el("div", { className: "editor-row" },
            field(t("block_type"), bp + ".type", "select", { options: options(BLOCK_TYPES, "block_type_"), rerender: true }),
            itemTools(tp + ".blocks", i, list.length)),
          el("div", { className: "field-grid" }, fields));
      }),
      button("add-block", tp + ".blocks", "+ " + t("add_block")));
  }

  // ------------------------------------------------------------------ render

  function render() {
    var scroll = window.scrollY;
    var form = document.querySelector("[data-editor-form]");
    form.textContent = "";
    form.appendChild(metaSection());
    form.appendChild(levelsSection());
    form.appendChild(daysSection());
    window.scrollTo(0, scroll);
  }

  // ------------------------------------------------------------------ actions

  function listAndIndex(path) {
    var parts = path.split(".");
    var index = Number(parts.pop());
    return { list: getPath(parts.join(".")), index: index, listPath: parts.join(".") };
  }

  var actions = {
    "add-day": function () {
      state.days = state.days || [];
      var ids = state.days.map(function (d) { return d.id; });
      state.days.push({ id: uniqueId("day-" + (state.days.length + 1), ids), title: t("new_day_title"), modules: [] });
    },
    "add-module": function (path) {
      var list = getPath(path) || [];
      setPath(path, list);
      var codes = [];
      state.days.forEach(function (d) { (d.modules || []).forEach(function (m) { codes.push(m.code); }); });
      list.push({ code: uniqueId("MOD-" + String(codes.length + 1).padStart(2, "0"), codes), title: t("new_module_title"), tasks: [] });
    },
    "add-task": function (path) {
      var list = getPath(path) || [];
      setPath(path, list);
      var module = getPath(path.replace(/\.tasks$/, ""));
      var base = slug(module.code).replace(/-/g, "") || "task";
      var task = { id: uniqueId(base + "-" + (list.length + 1), allTaskIds()), title: t("new_task_title"),
        level: (state._levels[0] || {}).key, duration: "30m", answers: [{ id: "answer", label: "" }] };
      list.push(task);
      openTasks[uid(task)] = true;
    },
    "add-answer": function (path) {
      var list = getPath(path) || [];
      setPath(path, list);
      list.push({ id: uniqueId("answer-" + (list.length + 1), list.map(function (a) { return a.id; })), type: "text" });
    },
    "add-block": function (path) {
      var list = getPath(path) || [];
      setPath(path, list);
      list.push({ type: "text", content: "" });
    },
    "add-header-field": function (path) {
      var list = getPath(path) || [];
      setPath(path, list);
      list.push({ id: uniqueId("field-" + (list.length + 1), list.map(function (f) { return f.id; })), label: "", type: "short" });
    },
    "add-level": function () {
      state._levels.push({ key: uniqueId("level-" + (state._levels.length + 1), state._levels.map(function (l) { return l.key; })), label: "", color: "grey" });
    },
    "remove": function (path) {
      var x = listAndIndex(path);
      x.list.splice(x.index, 1);
    },
    "move-up": function (path) {
      var x = listAndIndex(path);
      var item = x.list.splice(x.index, 1)[0];
      x.list.splice(x.index - 1, 0, item);
    },
    "move-down": function (path) {
      var x = listAndIndex(path);
      var item = x.list.splice(x.index, 1)[0];
      x.list.splice(x.index + 1, 0, item);
    },
    "toggle-task": function (path) {
      var task = getPath(path);
      var key = uid(task);
      if (openTasks[key]) delete openTasks[key];
      else openTasks[key] = true;
      return "no-dirty";
    },
    "set-layout": function (path, value) {
      var task = getPath(path);
      if (value === "blocks" && !Array.isArray(task.blocks)) {
        var blocks = [];
        if (task.requirement) blocks.push({ type: "text", content: task.requirement });
        if (task.snippet) blocks.push({ type: "snippet", content: task.snippet });
        if (task.steps && task.steps.length) blocks.push({ type: "steps", items: task.steps });
        if (task.guiding_questions && task.guiding_questions.length) blocks.push({ type: "questions", items: task.guiding_questions });
        ["requirement", "snippet", "steps", "guiding_questions"].forEach(function (k) { delete task[k]; });
        task.blocks = blocks.length ? blocks : [{ type: "text", content: "" }];
      } else if (value === "fixed" && Array.isArray(task.blocks)) {
        task.blocks.forEach(function (b) {
          if (b.type === "text" && !task.requirement) task.requirement = b.content;
          if (b.type === "snippet" && !task.snippet) task.snippet = b.content;
          if (b.type === "steps" && !task.steps) task.steps = b.items;
          if (b.type === "questions" && !task.guiding_questions) task.guiding_questions = b.items;
        });
        delete task.blocks;
      }
    },
    "preview-task": function (path) {
      preview(path);
      return "no-render";
    }
  };

  root.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target || target.type === "radio") return;
    var action = actions[target.getAttribute("data-action")];
    if (!action) return;
    event.preventDefault();
    var result = action(target.getAttribute("data-path"), target.getAttribute("data-value"));
    if (result === "no-render") return;
    if (result !== "no-dirty") setDirty(true);
    render();
  });

  root.addEventListener("change", function (event) {
    var target = event.target;
    if (target.type === "radio" && target.getAttribute("data-action") === "set-layout") {
      actions["set-layout"](target.getAttribute("data-path"), target.getAttribute("data-value"));
      setDirty(true);
      render();
      return;
    }
    update(target, true);
  });
  root.addEventListener("input", function (event) { update(event.target, false); });

  function update(input, isChange) {
    var path = input.getAttribute && input.getAttribute("data-path");
    if (!path || input.readOnly) return;
    var kind = input.getAttribute("data-kind");
    var value = parse(kind, input);
    setPath(path, value);
    setDirty(true);
    var mirror = input.getAttribute("data-mirror-source");
    if (mirror) {
      document.querySelectorAll('[data-mirror="' + mirror + '"]').forEach(function (n) {
        n.textContent = value || "";
      });
    }
    var holder = input.closest("[data-field]");
    if (holder && holder.classList.contains("is-invalid")) {
      holder.classList.remove("is-invalid");
      var msg = holder.querySelector(".field-error");
      if (msg) msg.remove();
    }
    if (isChange && input.hasAttribute("data-rerender")) render();
  }

  // ------------------------------------------------------------------ preview

  function preview(path) {
    var box = document.querySelector('[data-preview="' + path + '"]');
    if (!box) return;
    box.textContent = t("loading");
    api("POST", "/api/editor/preview", { task: strip(getPath(path)), levels: levelsDict() }).then(function (res) {
      if (res.ok) {
        box.innerHTML = res.data.html;  // server-rendered, escaped catalog content
        box.insertBefore(el("p", { className: "micro-label", text: t("preview_heading") }), box.firstChild);
      } else if (res.data.errors) {
        box.textContent = "";
        box.appendChild(errorList(res.data.errors.map(function (e) {
          return { path: path + "." + e.path, message: e.message };
        })));
      } else {
        box.textContent = t("load_failed");
      }
    }, function () { box.textContent = t("load_failed"); });
  }

  // ------------------------------------------------------------------ save

  function strip(value) {
    if (Array.isArray(value)) return value.map(strip);
    if (value && typeof value === "object") {
      var out = {};
      Object.keys(value).forEach(function (k) {
        if (k.charAt(0) !== "_") out[k] = strip(value[k]);
      });
      return out;
    }
    return value;
  }

  function levelsDict() {
    var levels = {};
    (state._levels || []).forEach(function (l) {
      var copy = strip(l);
      var key = copy.key;
      delete copy.key;
      levels[key] = copy;
    });
    return levels;
  }

  function payload() {
    var data = strip(state);
    data.levels = levelsDict();
    return data;
  }

  function clientPath(path) {
    var parts = path.split(".");
    if (parts[0] === "levels" && parts.length > 1) {
      var index = (state._levels || []).map(function (l) { return l.key; }).indexOf(parts[1]);
      if (index !== -1) return ["_levels", index].concat(parts.slice(2)).join(".");
      return "_levels";
    }
    return path;
  }

  function errorList(errors) {
    return el("div", { className: "form-error", role: "alert" },
      el("strong", { text: t("errors_heading") }),
      el("ul", null, errors.map(function (e) {
        var text = (e.path ? e.path + ": " : "") + e.message;
        return el("li", null, el("a", { href: "#", "data-goto": clientPath(e.path), text: text }));
      })));
  }

  function showErrors(errors) {
    // Open every task that contains an error, then mark the fields.
    errors.forEach(function (e) {
      var m = clientPath(e.path).match(/^(days\.\d+\.modules\.\d+\.tasks\.\d+)/);
      if (m) {
        var task = getPath(m[1]);
        if (task) openTasks[uid(task)] = true;
      }
    });
    render();
    var box = document.querySelector("[data-editor-messages]");
    box.textContent = "";
    box.appendChild(errorList(errors));
    errors.forEach(function (e) {
      var target = findField(clientPath(e.path));
      if (!target) return;
      target.classList.add("is-invalid");
      target.appendChild(el("p", { className: "field-error", text: e.message }));
    });
    box.scrollIntoView({ block: "start" });
  }

  function findField(path) {
    var parts = path.split(".");
    while (parts.length) {
      var node = document.querySelector('[data-field="' + parts.join(".") + '"]');
      if (node) return node;
      parts.pop();
    }
    return null;
  }

  function save(confirmDelete) {
    if (busy) return;
    busy = true;
    status("saving");
    var messages = document.querySelector("[data-editor-messages]");
    messages.textContent = "";
    api("PUT", "/api/editor/" + workbookId, {
      data: payload(), base_hash: baseHash, confirm_delete: confirmDelete || {}
    }).then(function (res) {
      busy = false;
      if (res.ok) {
        baseHash = res.data.base_hash;
        rememberOrigIds();
        dirty = false;
        status("saved", { version: res.data.version }, "ok");
        render();
      } else if (res.status === 422 && res.data.errors) {
        status("has_errors", null, "error");
        showErrors(res.data.errors);
      } else if (res.status === 409 && res.data.confirm) {
        status("unsaved");
        showConfirm(res.data.confirm);
      } else if (res.status === 409) {
        status("conflict_short", null, "error");
        messages.appendChild(el("div", { className: "form-error", role: "alert" },
          el("p", { text: t("conflict") }),
          el("button", { type: "button", className: "button", "data-reload": "", text: t("reload") })));
      } else {
        status("save_failed", null, "error");
      }
    }, function () {
      busy = false;
      status("save_failed", null, "error");
    });
  }

  function showConfirm(confirm) {
    var box = document.querySelector("[data-editor-messages]");
    box.textContent = "";
    var items = Object.keys(confirm).map(function (taskId) {
      var answers = confirm[taskId];
      return el("li", { text: answers.length ? t("confirm_answers", { id: taskId, answers: answers.join(", ") }) : t("confirm_task", { id: taskId }) });
    });
    var panel = el("div", { className: "confirm-box", role: "alertdialog", "aria-labelledby": "confirm-title" },
      el("p", { id: "confirm-title" }, el("strong", { text: t("confirm_heading") })),
      el("ul", null, items),
      el("p", { text: t("confirm_text") }),
      el("div", { className: "editor-preview-bar" },
        el("button", { type: "button", className: "button", "data-confirm-save": "", text: t("confirm_yes") }),
        el("button", { type: "button", className: "button button-quiet", "data-confirm-cancel": "", text: t("confirm_no") })));
    box.appendChild(panel);
    panel.querySelector("[data-confirm-save]").addEventListener("click", function () {
      box.textContent = "";
      save(confirm);
    });
    panel.querySelector("[data-confirm-cancel]").addEventListener("click", function () {
      box.textContent = "";
    });
    panel.querySelector("[data-confirm-save]").focus();
  }

  document.addEventListener("click", function (event) {
    var save_ = event.target.closest("[data-editor-save]");
    if (save_) { event.preventDefault(); save(); return; }
    if (event.target.closest("[data-reload]")) { dirty = false; window.location.reload(); return; }
    var go = event.target.closest("[data-goto]");
    if (go) {
      event.preventDefault();
      var target = findField(go.getAttribute("data-goto"));
      if (target) {
        target.scrollIntoView({ block: "center" });
        var input = target.querySelector("input, textarea, select");
        if (input) input.focus();
      }
    }
  });

  window.addEventListener("beforeunload", function (event) {
    if (dirty) {
      event.preventDefault();
      event.returnValue = t("unsaved_leave");
      return event.returnValue;
    }
  });

  // ------------------------------------------------------------------ load

  function rememberOrigIds() {
    (state.days || []).forEach(function (d) {
      (d.modules || []).forEach(function (m) {
        (m.tasks || []).forEach(function (task) {
          origIds.set(task, task.id);
          (task.answers || []).forEach(function (a) { origIds.set(a, a.id); });
        });
      });
    });
  }

  Promise.all([api("GET", "/api/editor/strings"), api("GET", "/api/editor/" + workbookId)]).then(function (results) {
    if (!results[0].ok || !results[1].ok) throw new Error("load");
    T = results[0].data;
    var loaded = results[1].data;
    state = loaded.data;
    baseHash = loaded.base_hash;
    locked = loaded.locked || {};
    state._levels = Object.keys(state.levels || {}).map(function (key) {
      var level = state.levels[key] || {};
      var copy = { key: key };
      Object.keys(level).forEach(function (k) { copy[k] = level[k]; });
      return copy;
    });
    delete state.levels;
    rememberOrigIds();
    document.querySelectorAll("[data-editor-save]").forEach(function (b) { b.disabled = false; });
    status(null);
    render();
  }).catch(function () {
    var box = document.querySelector("[data-editor-messages]");
    box.textContent = root.getAttribute("data-text-failed") || "";
  });
})();
