// Autosave for workbook pages: debounced PATCH requests with a status indicator,
// retry with backoff when offline, live progress bar and port LEDs.
(function () {
  "use strict";

  // Print: hints and bonus sections are printed expanded.
  var openedForPrint = [];
  window.addEventListener("beforeprint", function () {
    document.querySelectorAll("details:not([open])").forEach(function (d) {
      d.open = true;
      openedForPrint.push(d);
    });
  });
  window.addEventListener("afterprint", function () {
    openedForPrint.forEach(function (d) { d.open = false; });
    openedForPrint = [];
  });

  var root = document.querySelector("[data-autosave]");
  if (!root || !window.fetch) return;

  var DEBOUNCE_MS = 800;
  var MAX_RETRY_MS = 30000;
  var workbook = root.getAttribute("data-workbook");
  var statusEl = document.querySelector("[data-save-status]");
  var pending = {};   // key -> {url, body}
  var timers = {};    // key -> debounce timer
  var inflight = {};  // key -> request body being sent
  var retryDelay = 0;
  var retryTimer = null;
  var fatal = null;

  function text(name) {
    return root.getAttribute("data-text-" + name) || "";
  }

  function setStatus(kind) {
    if (!statusEl || (fatal && kind !== fatal)) return;
    statusEl.textContent = kind ? text(kind) : "";
    statusEl.className = "save-status" + (kind ? " is-" + kind : "");
  }

  function busy() {
    return Object.keys(pending).length > 0 || Object.keys(inflight).length > 0 ||
      Object.keys(timers).length > 0;
  }

  // ------------------------------------------------------------------ collect values

  function answerValue(container) {
    var type = container.getAttribute("data-type");
    var inputs = container.querySelectorAll("input");
    if (type === "checklist" || (type === "choice" && container.hasAttribute("data-multiple"))) {
      return Array.prototype.filter.call(inputs, function (i) { return i.checked; })
        .map(function (i) { return i.value; });
    }
    if (type === "choice") {
      var checked = container.querySelector("input:checked");
      return checked ? checked.value : "";
    }
    var field = container.querySelector("textarea, input");
    return field ? field.value : "";
  }

  function merge(target, patch) {
    Object.keys(patch).forEach(function (k) {
      if (k === "answers") {
        target.answers = target.answers || {};
        Object.keys(patch.answers).forEach(function (a) { target.answers[a] = patch.answers[a]; });
      } else {
        target[k] = patch[k];
      }
    });
    return target;
  }

  function queue(key, url, patch, delay) {
    var entry = pending[key] || { url: url, body: {} };
    merge(entry.body, patch);
    pending[key] = entry;
    clearTimeout(timers[key]);
    timers[key] = setTimeout(function () {
      delete timers[key];
      send(key);
    }, delay);
    setStatus("saving");
  }

  // ------------------------------------------------------------------ sending

  function send(key, keepalive) {
    if (fatal || inflight[key] || !pending[key]) return;
    var entry = pending[key];
    delete pending[key];
    inflight[key] = entry;
    setStatus("saving");
    fetch(entry.url, {
      method: "PATCH",
      credentials: "same-origin",
      keepalive: !!keepalive,
      headers: { "Content-Type": "application/json", "X-Workbook": "1" },
      body: JSON.stringify(entry.body)
    }).then(function (response) {
      delete inflight[key];
      if (response.ok) {
        retryDelay = 0;
        return response.json().then(function (data) { saved(key, data); });
      }
      if (response.status === 401) {
        restore(key, entry);
        fail("logged-out");
      } else if (response.status >= 500 || response.status === 429) {
        restore(key, entry);
        offline();
      } else {
        restore(key, entry);
        fail("error");
      }
    }, function () {
      delete inflight[key];
      restore(key, entry);
      offline();
    });
  }

  function restore(key, entry) {
    // Older values first, newer edits made during the request win.
    var newer = pending[key];
    pending[key] = { url: entry.url, body: newer ? merge(entry.body, newer.body) : entry.body };
  }

  function saved(key, data) {
    if (data && data.progress) updateProgress(data.progress.done, data.progress.total);
    if (key.indexOf("task:") === 0 && data && data.task && !data.task.changed) {
      var task = root.querySelector('[data-task="' + key.slice(5) + '"]');
      var note = task && task.querySelector("[data-changed-note]");
      if (note) note.hidden = true;
    }
    if (pending[key]) {
      send(key);
    } else if (!busy()) {
      setStatus("saved");
    }
  }

  function offline() {
    setStatus("offline");
    retryDelay = Math.min(Math.max(1000, retryDelay * 2), MAX_RETRY_MS);
    clearTimeout(retryTimer);
    retryTimer = setTimeout(flushAll, retryDelay);
  }

  function fail(kind) {
    fatal = kind;
    setStatus(kind);
  }

  function flushAll(keepalive) {
    Object.keys(timers).forEach(function (key) {
      clearTimeout(timers[key]);
      delete timers[key];
    });
    Object.keys(pending).forEach(function (key) { send(key, keepalive === true); });
  }

  // ------------------------------------------------------------------ live progress

  function updateProgress(done, total) {
    var bar = document.querySelector(".topbar .progress-bar");
    var label = document.querySelector("[data-progress-text]");
    if (bar) bar.value = done;
    if (label) {
      var percent = total ? Math.round(done * 100 / total) : 0;
      label.textContent = text("progress").replace("{done}", done)
        .replace("{total}", total).replace("{percent}", percent);
    }
  }

  function localProgress() {
    var boxes = root.querySelectorAll("[data-done]");
    var done = Array.prototype.filter.call(boxes, function (b) { return b.checked; }).length;
    updateProgress(done, boxes.length);
  }

  function updatePort(taskId, done) {
    var port = document.querySelector('[data-port="' + taskId + '"]');
    if (port && !port.classList.contains("is-redo")) port.classList.toggle("is-lit", done);
  }

  // ------------------------------------------------------------------ events

  function handle(event) {
    var el = event.target;
    var base = "/api/progress/" + workbook;
    if (el.hasAttribute("data-header")) {
      var patch = {};
      patch[el.getAttribute("data-header")] = el.value;
      queue("header", base + "/header", patch, DEBOUNCE_MS);
      return;
    }
    var task = el.closest("[data-task]");
    if (!task) return;
    var taskId = task.getAttribute("data-task");
    var url = base + "/tasks/" + taskId;
    if (el.hasAttribute("data-done")) {
      if (event.type !== "change") return;
      queue("task:" + taskId, url, { done: el.checked }, 0);
      updatePort(taskId, el.checked);
      localProgress();
      return;
    }
    var container = el.closest("[data-answer]");
    if (!container) return;
    var isChoice = el.type === "checkbox" || el.type === "radio";
    if (isChoice && event.type !== "change") return;
    var answers = {};
    answers[container.getAttribute("data-answer")] = answerValue(container);
    queue("task:" + taskId, url, { answers: answers }, isChoice ? 0 : DEBOUNCE_MS);
  }

  root.addEventListener("input", handle);
  root.addEventListener("change", handle);

  window.addEventListener("online", function () {
    if (!fatal) flushAll();
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden" && !fatal) flushAll(true);
  });
  window.addEventListener("beforeunload", function (event) {
    if (busy()) {
      event.preventDefault();
      event.returnValue = text("unsaved");
      return event.returnValue;
    }
  });
})();
