// Question/answer thread per task ("Rückfragen"): posts a message and appends
// the stored message to the thread without reloading the page.
(function () {
  "use strict";

  var root = document.querySelector("[data-workbook]");
  if (!root || !window.fetch || !document.querySelector("[data-thread-url]")) return;

  var MAX_CHARS = 5000;

  function text(name) {
    return root.getAttribute("data-text-comment-" + name) || "";
  }

  function showError(thread, message) {
    var el = thread.querySelector("[data-thread-error]");
    if (!el) return;
    el.textContent = message;
    el.hidden = !message;
  }

  function element(tag, className, content) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (content) el.textContent = content;  // never innerHTML: messages are user input
    return el;
  }

  function append(thread, data) {
    var comment = data.comment;
    var list = thread.querySelector("[data-thread-list]");
    var item = element("li", "thread-item thread-from-" +
      (comment.role === "trainer" ? "trainer" : "apprentice"));
    var meta = element("p", "thread-meta");
    meta.appendChild(element("strong", "", data.author_name || comment.author));
    if (comment.role === "trainer") {
      meta.appendChild(document.createTextNode(" "));
      meta.appendChild(element("span", "thread-role", text("role")));
    }
    meta.appendChild(document.createTextNode(" · "));
    var time = element("time", "", data.at_text || "");
    time.setAttribute("datetime", comment.at);
    meta.appendChild(time);
    item.appendChild(meta);
    item.appendChild(element("p", "thread-text", comment.text));
    list.appendChild(item);
    list.hidden = false;
    thread.classList.add("has-comments");
    var count = thread.querySelector("[data-thread-count]");
    if (count) count.textContent = String(list.children.length);
  }

  function send(thread, button) {
    var field = thread.querySelector("[data-thread-text]");
    var value = field ? field.value.trim() : "";
    if (!value) {
      showError(thread, text("empty"));
      return;
    }
    if (value.length > MAX_CHARS) {
      showError(thread, text("too-long"));
      return;
    }
    showError(thread, "");
    button.disabled = true;
    fetch(thread.getAttribute("data-thread-url"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Workbook": "1" },
      body: JSON.stringify({ text: value })
    }).then(function (response) {
      if (response.ok) {
        return response.json().then(function (data) {
          append(thread, data);
          field.value = "";
        });
      }
      showError(thread, response.status === 401 ? text("logged-out") : text("error"));
    }, function () {
      showError(thread, text("offline"));
    }).then(function () {
      button.disabled = false;
    }, function () {
      button.disabled = false;
      showError(thread, text("error"));
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("[data-thread-send]") : null;
    if (!button) return;
    var thread = button.closest("[data-thread]");
    if (thread && thread.hasAttribute("data-thread-url")) send(thread, button);
  });
})();
