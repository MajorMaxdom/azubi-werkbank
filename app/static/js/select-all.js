// "Alle auswählen" for checkbox lists (progressive enhancement: hidden without JS).
(function () {
  "use strict";
  document.querySelectorAll("[data-select-all]").forEach(function (wrapper) {
    var master = wrapper.querySelector("input");
    var scope = wrapper.closest("fieldset") || document;
    var boxes = Array.prototype.filter.call(
      scope.querySelectorAll('input[type="checkbox"]'),
      function (box) { return box !== master; }
    );
    if (!master || !boxes.length) return;
    function sync() {
      var checked = boxes.filter(function (b) { return b.checked; }).length;
      master.checked = checked === boxes.length;
      master.indeterminate = checked > 0 && checked < boxes.length;
    }
    master.addEventListener("change", function () {
      boxes.forEach(function (b) { b.checked = master.checked; });
    });
    boxes.forEach(function (b) { b.addEventListener("change", sync); });
    wrapper.hidden = false;
    sync();
  });
})();
