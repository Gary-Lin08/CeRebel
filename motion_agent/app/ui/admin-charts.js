/* Small SVG charts for the admin dashboard: no library, and nothing that needs inline style
   attributes, because the page's CSP forbids them (CSSOM and classes are fine).
   Text always goes in through textContent. */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";

  function svg(tag, attrs, text) {
    var node = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function html(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  /** Whole-number ticks from 0 to at least max: never 0.5 visitors. */
  function ticks(max) {
    max = Math.max(max, 1);
    var step = 1;
    var candidates = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000];
    for (var i = 0; i < candidates.length; i += 1) {
      step = candidates[i];
      if (Math.ceil(max / step) <= 4) break;
    }
    var out = [];
    for (var v = 0; v <= Math.ceil(max / step) * step; v += step) out.push(v);
    return out;
  }

  function shortDay(day) {
    var s = String(day);
    return /^\d{4}-\d{2}-\d{2}$/.test(s) ? Number(s.slice(5, 7)) + "/" + Number(s.slice(8, 10)) : s;
  }

  function dataTable(labels, series, labelTitle) {
    var details = html("details", "chart__table");
    details.append(html("summary", "", "查看数据"));
    var table = html("table");
    var head = html("tr");
    head.append(html("th", "", labelTitle || "日期"));
    series.forEach(function (s) { head.append(html("th", "", s.name)); });
    var thead = html("thead");
    thead.append(head);
    var body = html("tbody");
    labels.slice().reverse().forEach(function (label, r) {
      var i = labels.length - 1 - r;
      var tr = html("tr");
      tr.append(html("td", "nowrap", label));
      series.forEach(function (s) { tr.append(html("td", "num", s.values[i])); });
      body.append(tr);
    });
    table.append(thead, body);
    var wrap = html("div", "table-wrap");
    wrap.append(table);
    details.append(wrap);
    return details;
  }

  /** Mounts `draw(width)` and redraws when the card changes width. */
  function responsive(holder, draw) {
    var last = 0;
    function run() {
      var width = Math.round(holder.clientWidth);
      if (!width || width === last) return;
      last = width;
      draw(width);
    }
    if (typeof ResizeObserver === "function") new ResizeObserver(run).observe(holder);
    else window.requestAnimationFrame(run);
    return function () { last = 0; run(); };
  }

  function placeTip(tip, holder, x, y) {
    tip.hidden = false;
    var w = tip.offsetWidth;
    var left = x + 12;
    if (left + w > holder.clientWidth) left = Math.max(0, x - w - 12);
    tip.style.left = left + "px";
    tip.style.top = Math.max(0, y) + "px";
  }

  function fillTip(tip, title, rows) {
    tip.replaceChildren(html("div", "chart__tip-title", title));
    rows.forEach(function (r) {
      var row = html("div", "chart__tip-row");
      if (r.cls) row.append(html("i", "chart__key " + r.cls));
      row.append(html("strong", "", r.value), html("span", "", r.name));
      tip.append(row);
    });
  }

  function legend(series, cls) {
    if (series.length < 2) return null;
    var box = html("div", "chart__legend");
    series.forEach(function (s, i) {
      var item = html("span", "chart__legend-item");
      item.append(html("i", "chart__key " + cls + (i + 1)), document.createTextNode(s.name));
      box.append(item);
    });
    return box;
  }

  function isEmpty(series) {
    return series.every(function (s) { return s.values.every(function (v) { return !v; }); });
  }

  /** Change over time. opts: { labels, series: [{ name, values }], label, empty, height }. */
  function line(opts) {
    var series = opts.series, labels = opts.labels;
    var root = html("div", "chart");
    if (isEmpty(series)) {
      root.append(html("p", "empty", opts.empty || "这段时间还没有数据"));
      return root;
    }
    var leg = legend(series, "key-line-");
    if (leg) root.append(leg);
    var holder = html("div", "chart__plot");
    holder.setAttribute("tabindex", "0");
    holder.setAttribute("role", "img");
    holder.setAttribute("aria-label", opts.label || "折线图");
    var tip = html("div", "chart__tip");
    tip.hidden = true;
    holder.style.height = (opts.height || 220) + "px"; // hold the space before the first draw
    root.append(holder, dataTable(labels, series));

    responsive(holder, function (W) {
      var H = opts.height || 220;
      var direct = series.length > 1;
      var pad = { top: 12, right: direct ? 84 : 16, bottom: 24, left: 36 };
      var iw = W - pad.left - pad.right, ih = H - pad.top - pad.bottom;
      var max = Math.max.apply(null, series.map(function (s) { return Math.max.apply(null, s.values); }));
      var tk = ticks(max), top = tk[tk.length - 1];
      var n = labels.length;
      function X(i) { return pad.left + (n < 2 ? iw / 2 : (i / (n - 1)) * iw); }
      function Y(v) { return pad.top + ih - (v / top) * ih; }
      var chart = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, "aria-hidden": "true" });
      tk.forEach(function (v) {
        chart.append(svg("line", { x1: pad.left, x2: W - pad.right, y1: Y(v), y2: Y(v), class: v === 0 ? "c-base" : "c-grid" }));
        chart.append(svg("text", { x: pad.left - 8, y: Y(v) + 4, "text-anchor": "end", class: "c-tick" }, v));
      });
      [0, Math.floor((n - 1) / 2), n - 1].filter(function (v, i, a) { return a.indexOf(v) === i; }).forEach(function (i) {
        var anchor = i === 0 ? "start" : i === n - 1 ? "end" : "middle";
        chart.append(svg("text", { x: X(i), y: H - 6, "text-anchor": anchor, class: "c-tick" }, shortDay(labels[i])));
      });
      var ends = [];
      series.forEach(function (s, idx) {
        var cls = "c-s" + (idx + 1);
        var d = s.values.map(function (v, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1); }).join("");
        chart.append(svg("path", { d: d, class: "c-line " + cls }));
        chart.append(svg("circle", { cx: X(n - 1), cy: Y(s.values[n - 1]), r: 4, class: "c-dot " + cls }));
        ends.push({ y: Y(s.values[n - 1]) + 4, name: s.name });
      });
      if (direct) {
        // End labels: push apart whichever two would overlap.
        ends.sort(function (a, b) { return a.y - b.y; });
        ends.forEach(function (e, i) {
          if (i && e.y - ends[i - 1].y < 14) e.y = ends[i - 1].y + 14;
          chart.append(svg("text", { x: X(n - 1) + 9, y: e.y, class: "c-label" }, e.name));
        });
      }
      var cross = svg("line", { y1: pad.top, y2: pad.top + ih, class: "c-cross", visibility: "hidden" });
      chart.append(cross);
      var marks = series.map(function (s, idx) {
        var m = svg("circle", { r: 4, class: "c-dot c-s" + (idx + 1), visibility: "hidden" });
        chart.append(m);
        return m;
      });
      function show(i) {
        var x = X(i);
        cross.setAttribute("x1", x);
        cross.setAttribute("x2", x);
        cross.setAttribute("visibility", "visible");
        series.forEach(function (s, idx) {
          marks[idx].setAttribute("cx", x);
          marks[idx].setAttribute("cy", Y(s.values[i]));
          marks[idx].setAttribute("visibility", "visible");
        });
        fillTip(tip, labels[i], series.map(function (s, idx) { return { value: s.values[i], name: s.name, cls: series.length > 1 ? "key-line-" + (idx + 1) : "" }; }));
        placeTip(tip, holder, x, pad.top);
      }
      function hide() {
        cross.setAttribute("visibility", "hidden");
        marks.forEach(function (m) { m.setAttribute("visibility", "hidden"); });
        tip.hidden = true;
      }
      var focusIndex = n - 1;
      holder.onpointermove = function (ev) {
        var box = holder.getBoundingClientRect();
        var i = Math.round(((ev.clientX - box.left - pad.left) / (iw || 1)) * (n - 1));
        focusIndex = Math.min(n - 1, Math.max(0, i));
        show(focusIndex);
      };
      holder.onpointerleave = hide;
      holder.onblur = hide;
      holder.onfocus = function () { show(focusIndex); };
      holder.onkeydown = function (ev) {
        if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
        ev.preventDefault();
        focusIndex = Math.min(n - 1, Math.max(0, focusIndex + (ev.key === "ArrowRight" ? 1 : -1)));
        show(focusIndex);
      };
      holder.replaceChildren(chart, tip);
    });
    return root;
  }

  /** Counts per day or minute, one series. opts: { labels, values, name, label, empty, height, tipTitle, noTable }. */
  function bars(opts) {
    var labels = opts.labels, values = opts.values;
    var root = html("div", "chart");
    if (isEmpty([{ values: values }])) {
      root.append(html("p", "empty", opts.empty || "这段时间还没有数据"));
      return root;
    }
    var holder = html("div", "chart__plot");
    holder.setAttribute("role", "img");
    holder.setAttribute("aria-label", opts.label || "柱状图");
    var tip = html("div", "chart__tip");
    tip.hidden = true;
    holder.style.height = (opts.height || 180) + "px"; // the live page redraws this every 5 seconds
    root.append(holder);
    if (!opts.noTable) root.append(dataTable(labels, [{ name: opts.name, values: values }], opts.labelTitle));

    responsive(holder, function (W) {
      var H = opts.height || 180;
      var pad = { top: 8, right: 8, bottom: 24, left: 36 };
      var iw = W - pad.left - pad.right, ih = H - pad.top - pad.bottom;
      var tk = ticks(Math.max.apply(null, values)), top = tk[tk.length - 1];
      var n = values.length, slot = iw / n;
      var bw = Math.max(1, Math.min(24, slot - 2)); // 2px gap between bars
      var chart = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, "aria-hidden": "true" });
      function Y(v) { return pad.top + ih - (v / top) * ih; }
      tk.forEach(function (v) {
        chart.append(svg("line", { x1: pad.left, x2: W - pad.right, y1: Y(v), y2: Y(v), class: v === 0 ? "c-base" : "c-grid" }));
        chart.append(svg("text", { x: pad.left - 8, y: Y(v) + 4, "text-anchor": "end", class: "c-tick" }, v));
      });
      var fmt = opts.tickFormat || shortDay;
      chart.append(svg("text", { x: pad.left, y: H - 6, class: "c-tick" }, fmt(labels[0])));
      chart.append(svg("text", { x: W - pad.right, y: H - 6, "text-anchor": "end", class: "c-tick" }, fmt(labels[n - 1])));
      values.forEach(function (v, i) {
        var x = pad.left + i * slot + (slot - bw) / 2;
        if (v) {
          var y = Y(v), h = pad.top + ih - y, r = Math.min(4, bw / 2, h);
          var d = "M" + x + " " + (y + h) + "V" + (y + r) + "Q" + x + " " + y + " " + (x + r) + " " + y +
            "H" + (x + bw - r) + "Q" + (x + bw) + " " + y + " " + (x + bw) + " " + (y + r) + "V" + (y + h) + "Z";
          var bar = svg("path", { d: d, class: "c-bar" });
          chart.append(bar);
        }
        var hit = svg("rect", { x: pad.left + i * slot, y: pad.top, width: slot, height: ih, class: "c-hit" });
        hit.addEventListener("pointerenter", function () {
          fillTip(tip, opts.tipTitle ? opts.tipTitle(labels[i]) : labels[i], [{ value: v, name: opts.name }]);
          placeTip(tip, holder, x + bw, pad.top);
          if (bar) bar.classList.add("is-hover");
        });
        hit.addEventListener("pointerleave", function () {
          tip.hidden = true;
          if (bar) bar.classList.remove("is-hover");
        });
        chart.append(hit);
      });
      holder.replaceChildren(chart, tip);
    });
    return root;
  }

  /** Ranked list with its numbers always visible. rows: [{ name, count }]. */
  function ranks(rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return html("p", "empty", opts.empty || "还没有数据");
    var max = Math.max.apply(null, rows.map(function (r) { return r.count; }).concat([1]));
    var total = opts.total || 0;
    var list = html("div", "ranks");
    rows.forEach(function (r) {
      var row = html("div", "ranks__row");
      var name = html("div", "ranks__name", r.name);
      name.title = r.name;
      var track = html("div", "ranks__track");
      var fill = html("div", "ranks__fill");
      fill.style.width = Math.max(1, (r.count / max) * 100) + "%";
      track.append(fill);
      row.append(name, track, html("div", "ranks__value", total ? r.count + " · " + Math.round((r.count / total) * 100) + "%" : r.count));
      list.append(row);
    });
    return list;
  }

  window.adminCharts = { line: line, bars: bars, ranks: ranks };
})();
