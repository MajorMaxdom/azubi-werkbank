// Show a banner when the catalog of the open workbook changes on the server.
// The page is never reloaded automatically, so unsaved input is not lost.
(function () {
  "use strict";
  var sheet = document.querySelector("[data-workbook]");
  var workbook = sheet && sheet.getAttribute("data-workbook");
  var banner = document.querySelector("[data-update-banner]");
  if (!workbook || !banner || !window.EventSource) return;

  var reload = banner.querySelector("[data-reload]");
  if (reload) {
    reload.addEventListener("click", function () {
      window.location.reload();
    });
  }

  var source = new EventSource("/events");
  source.addEventListener("catalog", function (event) {
    var data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (data.workbooks && data.workbooks.indexOf(workbook) !== -1) {
      banner.hidden = false;
    }
  });
})();
