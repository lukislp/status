// Keyboard support for the uptime bars: each bar list is a single tab stop (the latest day),
// arrow keys / Home / End move between days and the focused day's tooltip shows via :focus.
// The page works without this script; only the per-day keyboard navigation needs it.
(function () {
  "use strict";
  function move(list, from, to) {
    var bars = Array.prototype.filter.call(list.children, function (el) {
      return el.offsetParent !== null; // skip days hidden on narrow screens
    });
    var index = bars.indexOf(from);
    if (index < 0) return;
    var next = to === "home" ? 0 : to === "end" ? bars.length - 1 : Math.min(bars.length - 1, Math.max(0, index + to));
    from.tabIndex = -1;
    bars[next].tabIndex = 0;
    bars[next].focus();
  }
  document.addEventListener("keydown", function (event) {
    var bar = event.target;
    if (!bar.classList || !bar.classList.contains("bar")) return;
    var keys = { ArrowLeft: -1, ArrowRight: 1, Home: "home", End: "end" };
    if (!(event.key in keys)) return;
    event.preventDefault();
    move(bar.parentElement, bar, keys[event.key]);
  });
})();
