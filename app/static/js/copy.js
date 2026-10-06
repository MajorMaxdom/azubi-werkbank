// Copy-to-clipboard buttons: <button data-copy="#input-id" data-copied="…">.
(function () {
  "use strict";
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    var label = button.textContent;
    button.addEventListener("click", function () {
      var input = document.querySelector(button.getAttribute("data-copy"));
      if (!input) return;
      var done = function () {
        button.textContent = button.getAttribute("data-copied") || label;
        setTimeout(function () { button.textContent = label; }, 2000);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(input.value).then(done, function () {
          input.select();
        });
      } else {
        input.select();
        try {
          if (document.execCommand("copy")) done();
        } catch (err) {
          /* the link stays selected for manual copying */
        }
      }
    });
  });
})();
