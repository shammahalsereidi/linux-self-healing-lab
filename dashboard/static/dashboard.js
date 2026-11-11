// Self-Healing Lab dashboard: chart hover/keyboard readout and live refresh.
// All data text is inserted with textContent (never innerHTML).
(function () {
  "use strict";

  function setupChart(svg) {
    var pts, g;
    try {
      pts = JSON.parse(svg.getAttribute("data-points"));
      g = JSON.parse(svg.getAttribute("data-geom"));
    } catch (e) { return; }
    if (!pts || !pts.length) { return; }
    var unit = svg.getAttribute("data-unit") || "";
    var cross = svg.querySelector(".crosshair");
    var dot = svg.querySelector(".hover-dot");
    var tip = svg.parentNode.querySelector(".tooltip");
    var tipValue = tip.querySelector(".tip-value");
    var tipLabel = tip.querySelector(".tip-label");
    var idx = pts.length - 1;

    function xOf(i) { return g.n > 1 ? g.x0 + (g.x1 - g.x0) * i / (g.n - 1) : g.x1; }
    function yOf(v) { return g.y0 - (g.y0 - g.y1) * v / g.ymax; }

    function show(i) {
      idx = Math.max(0, Math.min(pts.length - 1, i));
      var p = pts[idx], x = xOf(idx);
      cross.setAttribute("x1", x);
      cross.setAttribute("x2", x);
      cross.classList.add("on");
      if (p[1] === null) {
        dot.classList.remove("on");
        tipValue.textContent = "no data";
      } else {
        dot.setAttribute("cx", x);
        dot.setAttribute("cy", yOf(p[1]));
        dot.classList.add("on");
        tipValue.textContent = p[1].toFixed(g.decimals) + " " + unit;
      }
      tipLabel.textContent = p[0];
      var vb = svg.viewBox.baseVal;
      var w = svg.getBoundingClientRect().width;
      tip.style.left = (x / vb.width * w) + "px";  // CSSOM, allowed by the CSP
      tip.classList.add("on");
    }
    function hide() {
      cross.classList.remove("on");
      dot.classList.remove("on");
      tip.classList.remove("on");
    }

    svg.addEventListener("pointermove", function (ev) {
      var rect = svg.getBoundingClientRect();
      var vb = svg.viewBox.baseVal;
      var x = (ev.clientX - rect.left) * vb.width / rect.width;
      var i = g.n > 1 ? Math.round((x - g.x0) / (g.x1 - g.x0) * (g.n - 1)) : 0;
      show(i);
    });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("focus", function () { show(idx); });
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", function (ev) {
      var keys = { ArrowLeft: idx - 1, ArrowRight: idx + 1, Home: 0, End: pts.length - 1 };
      if (ev.key in keys) { show(keys[ev.key]); ev.preventDefault(); }
    });
  }

  function init(root) {
    Array.prototype.forEach.call(root.querySelectorAll("svg.chart"), setupChart);
  }

  // ---- live refresh every 5 s, keeping the frame ---------------------------
  var REFRESH_MS = 5000;
  var conn = document.getElementById("conn");

  function busy() {
    // Don't swap the page under the reader's pointer or keyboard focus.
    var a = document.activeElement;
    return document.querySelector(".chart-card:hover") !== null ||
           (a && a.closest && a.closest(".chart-card") !== null);
  }

  function refresh() {
    var live = document.getElementById("live");
    if (!live || busy()) { return schedule(); }
    fetch(window.location.pathname, { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { if (!r.ok) { throw new Error(r.status); } return r.text(); })
      .then(function (text) {
        var doc = new DOMParser().parseFromString(text, "text/html");
        var next = doc.getElementById("live");
        if (!next) { throw new Error("bad page"); }
        // keep open <details> open across refreshes
        Array.prototype.forEach.call(live.querySelectorAll("details[open][id]"), function (d) {
          var same = next.querySelector("details#" + CSS.escape(d.id));
          if (same) { same.open = true; }
        });
        live.replaceWith(document.adoptNode(next));
        init(next);
        conn.hidden = true;
      })
      .catch(function () {
        live.classList.add("refreshing");  // hold the last render, dimmed
        conn.hidden = false;
      })
      .then(schedule);
  }

  function schedule() { window.setTimeout(refresh, REFRESH_MS); }

  init(document);
  schedule();
})();
