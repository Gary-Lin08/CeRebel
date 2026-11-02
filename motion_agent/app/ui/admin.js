/* CeRebel developer dashboard. Every value from the server is written with textContent. */
(function () {
  "use strict";

  var USER_KEY = "cerebel_admin_user_token";
  var ADMIN_KEY = "cerebel_admin_token";
  var EMAIL_KEY = "cerebel_admin_email";
  var userToken = "";
  var adminToken = "";
  try {
    userToken = sessionStorage.getItem(USER_KEY) || "";
    adminToken = sessionStorage.getItem(ADMIN_KEY) || "";
  } catch (_) {}

  var $ = function (id) { return document.getElementById(id); };
  var state = { tab: "live", users: [], sortKey: "created_at", sortDir: -1, userId: null, trafficDays: "30", liveTimer: null, backersSeen: null };
  var LIVE_EVERY_MS = 5000;

  // ------------------------------------------------------------ helpers

  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") node.textContent = String(v);
      else if (k === "class") node.className = v;
      else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return node;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    var d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString("zh-CN", { hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function fmtAgo(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    var s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    if (s < 86400 * 60) return Math.floor(s / 86400) + " 天前";
    return fmtTime(iso);
  }
  function fmtDur(sec) {
    sec = Number(sec) || 0;
    if (sec < 60) return "<1 分钟";
    var m = Math.round(sec / 60);
    if (m < 60) return m + " 分钟";
    return Math.floor(m / 60) + " 小时 " + (m % 60) + " 分";
  }
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (!n) return "—";
    if (n < 1e6) return Math.round(n / 1e3) + " KB";
    return (n / 1e6).toFixed(1) + " MB";
  }
  function fmtMoney(cents, currency) {
    if (cents === null || cents === undefined) return "—";
    return (Number(cents) / 100).toFixed(2) + " " + String(currency || "").toUpperCase();
  }
  function pct(x) { return (Math.round((Number(x) || 0) * 1000) / 10) + "%"; }

  var STATUS = {
    ready: ["完成", "good"], processing: ["处理中", "warn"], uploading: ["上传中", "warn"], extracting: ["处理中", "warn"],
    error: ["失败", "bad"], rejected: ["质检拒绝", "bad"], no_swing: ["未识别挥杆", "warn"], deleted: ["已删除", ""],
  };
  function statusChip(status) {
    var s = STATUS[status] || [status || "—", ""];
    return h("span", { class: "chip" + (s[1] ? " chip--" + s[1] : ""), text: s[0] });
  }

  var EVENT_LABELS = {
    signup: "注册", login: "登录", password_reset_requested: "申请重置密码", password_reset: "重置密码",
    password_changed: "修改密码", video_consent: "同意视频处理", upload_needs_consent: "上传前需同意",
    upload_saved: "上传", upload_rejected: "上传被拒", upload_trimmed: "自动裁剪", upload_unavailable: "上传时 GPU 不可用",
    capture_finalized: "分析完成", pipeline_error: "处理失败", email_sent: "发送邮件", capture_deleted: "删除挥杆",
    practice_logged: "记录练习", account_deleted: "删除账户", chat_question: "问教练", chat_error: "教练出错",
    checkout_created: "发起付款", paywall_hit: "额度用完", compare_view: "对比挥杆", feedback: "文字反馈",
    client_trust_yes: "反馈：符合", client_trust_no: "反馈：不符合", client_landing_view: "看落地页",
    client_sample_report_view: "看示例报告", client_signup_view: "打开注册", client_upload_click: "点上传",
    client_report_view: "看报告", client_fix_expand: "展开修正", client_chat_open: "打开教练",
    client_paywall_view: "看付费墙", client_checkout_click: "点付款", client_share_click: "点分享",
    client_progress_card_view: "看进步卡片", client_progress_open: "打开进步对比",
  };
  function eventLabel(kind) {
    kind = String(kind || "");
    if (EVENT_LABELS[kind]) return EVENT_LABELS[kind];
    if (kind.indexOf("billing_") === 0) return "Stripe：" + kind.slice(8);
    return kind;
  }
  var HIDDEN_PROPS = { ts: 1, kind: 1, user_id: 1, email: 1, visitor: 1 };
  function eventProps(e) {
    return Object.keys(e)
      .filter(function (k) { return !HIDDEN_PROPS[k] && e[k] !== null && e[k] !== "" && e[k] !== undefined; })
      .map(function (k) { return k + "=" + (typeof e[k] === "object" ? JSON.stringify(e[k]) : String(e[k])); })
      .join("  ");
  }

  function setStatus(msg, isError) {
    var el = $("appStatus");
    el.textContent = msg || "";
    el.classList.toggle("is-error", !!isError);
  }

  function table(columns, rows, opts) {
    opts = opts || {};
    if (!rows.length) return h("p", { class: "empty", text: opts.empty || "暂无数据" });
    var head = h("tr", {}, columns.map(function (c) { return h("th", { text: c.label }); }));
    var body = h("tbody", {}, rows.map(function (r) {
      var tr = h("tr", {}, columns.map(function (c) {
        var v = c.render ? c.render(r) : r[c.key];
        var cell = h("td", { class: c.cls || null });
        if (v instanceof Node) cell.appendChild(v);
        else cell.textContent = v === null || v === undefined || v === "" ? "—" : String(v);
        return cell;
      }));
      return tr;
    }));
    return h("div", { class: "table-wrap" }, [h("table", {}, [h("thead", {}, [head]), body])]);
  }

  function card(title, children) {
    return h("section", { class: "card" }, [h("h3", { text: title })].concat(children));
  }

  // ------------------------------------------------------------ API

  async function api(path, opts) {
    opts = opts || {};
    var headers = { Authorization: "Bearer " + userToken, "X-Admin-Token": adminToken };
    if (opts.body) headers["Content-Type"] = "application/json";
    var r = await fetch(path, {
      method: opts.method || (opts.body ? "POST" : "GET"),
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: "no-store",
    });
    var data = {};
    try { data = await r.json(); } catch (_) {}
    if (r.status === 401 && data.relogin) {
      signOut(false);
      throw new Error(data.message || "请重新登录");
    }
    if (!r.ok) throw new Error(data.message || "请求失败（" + r.status + "）");
    return data;
  }

  // ------------------------------------------------------------ sign-in

  function showLogin(message) {
    $("app").hidden = true;
    $("login").hidden = false;
    $("loginStatus").textContent = message || "";
    $("loginEmail").focus();
  }

  $("loginForm").addEventListener("submit", async function (ev) {
    ev.preventDefault();
    var status = $("loginStatus");
    status.classList.remove("is-error");
    status.textContent = "登录中…";
    $("loginSubmit").disabled = true;
    try {
      var r = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: $("loginEmail").value.trim(), password: $("loginPassword").value }),
      });
      var data = await r.json();
      if (!r.ok) throw new Error(data.message || "登录失败");
      userToken = data.token;
      var r2 = await fetch("/api/admin/session", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + userToken },
        body: JSON.stringify({ password: $("loginAdminPassword").value }),
      });
      var data2 = await r2.json();
      if (!r2.ok) throw new Error(data2.message || "后台验证失败");
      adminToken = data2.admin_token;
      try {
        sessionStorage.setItem(USER_KEY, userToken);
        sessionStorage.setItem(ADMIN_KEY, adminToken);
        sessionStorage.setItem(EMAIL_KEY, data2.email || "");
      } catch (_) {}
      $("loginPassword").value = "";
      $("loginAdminPassword").value = "";
      status.textContent = "";
      start(data2.email);
    } catch (err) {
      status.textContent = err.message;
      status.classList.add("is-error");
    } finally {
      $("loginSubmit").disabled = false;
    }
  });

  async function signOut(callServer) {
    if (callServer) {
      try { await api("/api/admin/logout", { body: {} }); } catch (_) {}
      try {
        await fetch("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + userToken }, body: "{}" });
      } catch (_) {}
    }
    stopLive();
    userToken = "";
    adminToken = "";
    try {
      sessionStorage.removeItem(USER_KEY);
      sessionStorage.removeItem(ADMIN_KEY);
      sessionStorage.removeItem(EMAIL_KEY);
    } catch (_) {}
    showLogin(callServer ? "已退出。" : "登录已过期，请重新登录。");
  }
  $("logout").addEventListener("click", function () { signOut(true); });

  // ------------------------------------------------------------ tabs

  document.querySelectorAll(".tabs button").forEach(function (btn) {
    btn.addEventListener("click", function () { showTab(btn.getAttribute("data-tab")); });
  });
  $("refresh").addEventListener("click", function () {
    if (state.tab === "user" && state.userId) openUser(state.userId);
    else showTab(state.tab);
  });

  function showTab(name) {
    state.tab = name;
    document.querySelectorAll(".tab").forEach(function (s) { s.hidden = s.id !== "tab-" + name; });
    document.querySelectorAll(".tabs button").forEach(function (b) {
      b.classList.toggle("is-active", b.getAttribute("data-tab") === (name === "user" ? "users" : name));
    });
    setStatus("");
    stopLive();
    var loaders = { live: startLive, traffic: loadTraffic, backers: loadBackers, overview: loadOverview, users: loadUsers, feedback: loadFeedback, questions: loadQuestions, audit: loadAudit };
    if (loaders[name]) loaders[name]().catch(function (err) { setStatus(err.message, true); });
  }

  // ------------------------------------------------------------ live

  var SITE_LABELS = { main: "主站", playground: "Playground" };
  var DEVICE_LABELS = { desktop: "电脑", mobile: "手机", tablet: "平板" };

  function visitorName(r) {
    return r.email || "匿名访客 " + String(r.visitor || "").slice(0, 6);
  }
  function whoCell(r) {
    if (r.user_id) return userCell(r);
    return h("span", { class: "muted", text: visitorName(r) });
  }
  function clock(iso) {
    var d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  }
  function isToday(iso) {
    return new Date(iso).toDateString() === new Date().toDateString();
  }
  function feedText(e) {
    var who = e.email || (e.visitor ? "匿名访客 " + String(e.visitor).slice(0, 6) : "匿名");
    var from = [e.country, e.source ? "来自 " + e.source : ""].filter(Boolean).join(" · ");
    var tail = from ? "（" + from + "）" : "";
    var site = SITE_LABELS[e.site] || "";
    switch (e.type) {
      case "backer": return [h("b", { text: e.email }), " 成为 backer" + tail];
      case "page_view": return [who + " 打开 " + site + " " + (e.path || "/") + tail];
      case "cta_click": return [who + " 点了「" + (e.label || "按钮") + "」"];
      case "form_start": return [who + " 开始填表单"];
      case "billing_checkout.session.completed": return [h("b", { text: who }), " 完成付款"];
      case "checkout_created": return [who + " 发起付款" + (e.plan ? "（" + e.plan + "）" : "")];
      default: return [who + " " + eventLabel(e.type)];
    }
  }

  async function startLive() {
    var box = $("tab-live");
    var nodes = {
      dot: h("span", { class: "live-dot" }),
      count: h("strong", { text: "—" }),
      note: h("span", { class: "live-note", text: "连接中…" }),
      banner: h("p", { class: "banner", hidden: true }),
      kpis: h("div", { class: "kpis" }),
      minutes: h("div", {}),
      online: h("div", {}),
      feed: h("div", {}),
    };
    box.replaceChildren(
      h("div", { class: "live-head" }, [
        h("div", { class: "live-count" }, [nodes.dot, nodes.count, h("span", { text: "人正在线上" })]),
        nodes.note,
      ]),
      nodes.banner,
      nodes.kpis,
      card("正在线上的人", [nodes.online]),
      h("div", { class: "grid2" }, [
        card("最近 30 分钟每分钟在线人数", [nodes.minutes]),
        card("动态", [nodes.feed]),
      ])
    );
    state.backersSeen = null;
    var lastOk = 0;
    async function tick() {
      try {
        var d = await api("/api/admin/live");
        lastOk = Date.now();
        paintLive(d, nodes);
      } catch (err) {
        if (state.tab === "live") {
          nodes.dot.classList.add("is-stale");
          nodes.note.textContent = "连接中断：" + err.message + (lastOk ? "（数字停留在 " + clock(new Date(lastOk).toISOString()) + "）" : "");
        }
      }
    }
    await tick();
    if (state.tab === "live" && userToken) state.liveTimer = window.setInterval(function () {
      if (document.visibilityState === "visible") tick();
    }, LIVE_EVERY_MS);
  }

  function stopLive() {
    if (state.liveTimer) window.clearInterval(state.liveTimer);
    state.liveTimer = null;
  }

  function paintLive(d, nodes) {
    nodes.dot.classList.remove("is-stale");
    nodes.count.textContent = d.online.length;
    nodes.note.textContent = "每 5 秒刷新，最近 " + clock(d.now) + "。90 秒内有心跳算在线。";
    if (d.backer_store_error) nodes.note.textContent += " Backer 数字可能不是最新：读取 Supabase 失败。";
    if (!d.backer_store_error && state.backersSeen !== null && d.backers_total > state.backersSeen && d.latest_backer) {
      nodes.banner.textContent = "新 backer：" + d.latest_backer;
      nodes.banner.hidden = false;
    }
    // A failed read must not look like a drop in backers, or set off the "new backer" notice later.
    if (!d.backer_store_error) state.backersSeen = d.backers_total;
    var t = d.today;
    nodes.kpis.replaceChildren(
      kpi("今天访客", t.visitors, "打开页面 " + t.page_views + " 次（UTC）"),
      kpi("今天注册", t.signups),
      kpi("今天新 backer", d.backer_store_error ? "—" : t.backers, d.backer_store_error ? "读取失败" : "累计 " + d.backers_total),
      kpi("今天付款", t.payments, "当前 Pro " + d.pro_total)
    );
    nodes.online.replaceChildren(table([
      { label: "谁", render: whoCell },
      { label: "在哪", render: function (r) { return (SITE_LABELS[r.site] || r.site || "") + " " + (r.path || "/"); } },
      { label: "来源", render: function (r) { return r.utm_source || r.ref || "直接访问"; } },
      { label: "地区", cls: "nowrap", render: function (r) { return r.country || r.tz || "未知"; } },
      { label: "设备", cls: "nowrap", render: function (r) { return (DEVICE_LABELS[r.device] || r.device || "") + " · " + (r.browser || ""); } },
      { label: "已停留", cls: "num nowrap", render: function (r) { return fmtDur(r.seconds); } },
    ], d.online, { empty: "现在没有人在线。有人打开网站后，30 秒内会出现在这里。" }));
    nodes.minutes.replaceChildren(window.adminCharts.bars({
      labels: d.per_minute.map(function (m) { return m.minute; }),
      values: d.per_minute.map(function (m) { return m.count; }),
      name: "人在线",
      label: "最近 30 分钟每分钟在线人数",
      height: 150,
      noTable: true,
      tickFormat: clock,
      tipTitle: clock,
      empty: "最近 30 分钟没有访客（服务重启后从零开始算）",
    }));
    var list = h("ul", { class: "feed" }, d.feed.slice(0, 30).map(function (e) {
      return h("li", {}, [
        h("span", { class: "feed__time", text: isToday(e.ts) ? clock(e.ts) : fmtTime(e.ts).slice(5, 10) }),
        h("span", { class: "feed__text" }, feedText(e)),
      ]);
    }));
    nodes.feed.replaceChildren(d.feed.length ? list : h("p", { class: "empty", text: "还没有动态" }));
  }

  // ------------------------------------------------------------ traffic

  async function loadTraffic() {
    setStatus("加载中…");
    var d = await api("/api/admin/traffic?days=" + state.trafficDays);
    setStatus("");
    var C = window.adminCharts;
    var range = h("div", { class: "segmented", role: "group", "aria-label": "时间范围" }, ["7", "30", "90"].map(function (n) {
      return h("button", {
        type: "button",
        class: state.trafficDays === n ? "is-active" : null,
        "aria-pressed": state.trafficDays === n ? "true" : "false",
        text: n + " 天",
        onclick: function () { state.trafficDays = n; loadTraffic().catch(function (err) { setStatus(err.message, true); }); },
      });
    }));
    var conv = d.total_visitors ? pct(d.backers_in_window / d.total_visitors) : "—";
    $("tab-traffic").replaceChildren(
      h("div", { class: "toolbar" }, [range, h("span", { class: "muted", text: "日期按 UTC 计。访客按浏览器算，同一个人换设备算两个。" })]),
      h("div", { class: "kpis" }, [
        kpi("访客", d.total_visitors, "打开页面 " + d.total_page_views + " 次"),
        kpi("注册", d.funnel[2].count),
        kpi("新 backer", d.backers_in_window, "占访客 " + conv),
        kpi("新付费", d.funnel[4].count),
      ]),
      card("每日访客", [C.line({
        labels: d.labels,
        series: [{ name: "主站", values: d.visitors_main }, { name: "Playground", values: d.visitors_playground }],
        label: "每日访客，主站和 Playground 各一条线",
        empty: "还没有访客数据。主站和 Playground 部署新代码后才开始记录。",
      })]),
      h("div", { class: "grid2" }, [
        card("每日注册", [C.bars({ labels: d.labels, values: d.signups, name: "人注册", label: "每日注册人数", empty: "这段时间没有新注册" })]),
        card("转化（人数）", [
          h("p", { class: "note", text: "各步骤分别统计，不是同一批人的追踪：访客是匿名的，注册之后才知道是谁。" }),
          C.ranks(d.funnel, { empty: "还没有数据" }),
        ]),
      ]),
      h("div", { class: "grid2" }, [
        card("Backer 累计", [C.line({ labels: d.labels, series: [{ name: "backer", values: d.backers_running }], label: "backer 累计人数", height: 180, empty: "还没有 backer。主站的表单接到这个后台后才会记录。" })]),
        card("付费用户累计", [C.line({ labels: d.labels, series: [{ name: "付费用户", values: d.paying_running }], label: "付费用户累计人数", height: 180, empty: "还没有完成付款的用户" })]),
      ]),
      h("div", { class: "grid2" }, [
        card("来源", [C.ranks(d.sources, { total: d.total_visitors })]),
        card("地区", [C.ranks(d.countries, { total: d.total_visitors, empty: "还没有数据" })]),
        card("设备", [C.ranks(d.devices.map(function (r) { return { name: DEVICE_LABELS[r.name] || r.name, count: r.count }; }), { total: d.total_visitors })]),
        card("浏览器", [C.ranks(d.browsers, { total: d.total_visitors })]),
        card("打开最多的页面", [C.ranks(d.pages)]),
        card("点击最多的按钮", [C.ranks(d.clicks, { empty: "还没有点击" })]),
      ])
    );
  }

  // ------------------------------------------------------------ backers

  function csvCell(value) {
    var s = String(value === null || value === undefined ? "" : value);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // a spreadsheet must not run a signup's name as a formula
    return '"' + s.replace(/"/g, '""') + '"';
  }

  async function exportBackers(rows) {
    var cols = ["email", "name", "use", "created_at", "country", "ref", "utm_source", "utm_campaign", "list"];
    var csv = [cols.join(",")].concat(rows.map(function (r) {
      return cols.map(function (c) { return csvCell(r[c]); }).join(",");
    })).join("\n");
    await api("/api/admin/backers/exported", { body: { rows: rows.length } });
    var url = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
    var a = h("a", { href: url, download: "cerebel-backers.csv" });
    document.body.append(a);
    a.click();
    a.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  async function loadBackers() {
    setStatus("加载中…");
    var d = await api("/api/admin/backers");
    setStatus("");
    var box = $("tab-backers");
    var query = h("input", { type: "search", placeholder: "搜索邮箱、名字", "aria-label": "搜索 backer" });
    var listBox = h("div", {});
    var cols = [
      { label: "邮箱", render: function (b) { return b.email; } },
      { label: "名字", render: function (b) { return b.name; } },
      { label: "用途", render: function (b) { return b.use; } },
      { label: "加入时间", cls: "nowrap", render: function (b) { return fmtTime(b.created_at); } },
      { label: "来源", render: function (b) { return b.utm_source || b.ref || "直接访问"; } },
      { label: "地区", cls: "nowrap", render: function (b) { return b.country; } },
      { label: "账号", cls: "nowrap", render: function (b) { return h("span", { class: "chip" + (b.has_account ? " chip--good" : ""), text: b.has_account ? "已注册" : "未注册" }); } },
      { label: "", cls: "nowrap", render: function (b) {
        return h("button", { type: "button", text: "移除", "aria-label": "移除 " + b.email, onclick: function () {
          if (!window.confirm("把 " + b.email + " 从 backer 名单中永久移除？这步无法撤销。")) return;
          api("/api/admin/backers/remove", { body: { id: b.id } })
            .then(loadBackers)
            .catch(function (err) { setStatus(err.message, true); });
        } });
      } },
    ];
    function paint() {
      var q = query.value.trim().toLowerCase();
      var rows = d.backers.filter(function (b) { return !q || (b.email + " " + (b.name || "")).toLowerCase().indexOf(q) >= 0; });
      listBox.replaceChildren(table(cols, rows, { empty: q ? "没有匹配的 backer" : "还没有 backer。主站的表单接到这个后台后，新的报名会出现在这里。" }));
    }
    query.addEventListener("input", paint);
    paint();
    var pro = d.paying.filter(function (p) { return p.pro; });
    setStatus(d.backer_store_error ? "读取 Supabase 失败，下面的名单可能不是最新，甚至是空的。报名本身不受影响。" : "", !!d.backer_store_error);
    box.replaceChildren(
      h("div", { class: "kpis" }, [
        kpi("Backer（预售名单）", d.backer_store_error ? "—" : d.backers.length, "只留了邮箱，没有付款"),
        kpi("付费 Pro", pro.length, "创始会员名额已用 " + d.founding_used),
      ]),
      h("section", { class: "card" }, [
        h("div", { class: "card__head" }, [
          h("h3", { text: "Backer 名单（" + d.backers.length + "）" }),
          h("button", { type: "button", text: "导出 CSV", disabled: !d.backers.length, onclick: function () {
            exportBackers(d.backers).catch(function (err) { setStatus(err.message, true); });
          } }),
        ]),
        h("div", { class: "toolbar" }, [query]),
        h("div", {}, [listBox]),
      ]),
      card("付费用户（" + d.paying.length + "）", [table([
        { label: "用户", render: userCell },
        { label: "方案", render: function (p) { return p.plan; } },
        { label: "状态", render: function (p) { return h("span", { class: "chip" + (p.pro ? " chip--pro" : ""), text: p.status || "—" }); } },
        { label: "开始付费", cls: "nowrap", render: function (p) { return fmtTime(p.since); } },
        { label: "当前周期结束", cls: "nowrap", render: function (p) { return p.current_period_end ? fmtTime(p.current_period_end) : "—"; } },
        { label: "期末取消", render: function (p) { return p.cancel_at_period_end ? "是" : "否"; } },
      ], d.paying, { empty: "还没有付费用户" })])
    );
  }

  // ------------------------------------------------------------ overview

  function kpi(label, value, sub) {
    return h("div", { class: "kpi" }, [
      h("div", { class: "kpi__label", text: label }),
      h("div", { class: "kpi__value", text: value }),
      sub ? h("div", { class: "kpi__sub", text: sub }) : null,
    ]);
  }

  async function loadOverview() {
    var box = $("tab-overview");
    setStatus("加载中…");
    var d = await api("/api/admin/overview");
    setStatus("");
    var a = d.accounts, b = d.billing, u = d.uploads, f = d.feedback;
    var statuses = u.by_status || {};
    var trustTotal = (f.trust_yes || 0) + (f.trust_no || 0);
    box.replaceChildren(
      h("div", { class: "kpis" }, [
        kpi("注册用户", a.total, "7 天新增 " + a.new_7d + " · 30 天新增 " + a.new_30d),
        kpi("活跃用户", a.active_7d, "今天 " + a.active_1d + " · 30 天 " + a.active_30d),
        kpi("Pro 用户", b.pro, Object.keys(b.plans || {}).map(function (k) { return k + " " + b.plans[k]; }).join(" · ") || "—"),
        kpi("创始会员名额已用", b.founding_used, "发起付款 " + b.checkouts_started + " 次 · 额度用完 " + b.paywall_hits + " 次"),
        kpi("上传总数", u.total, "完成 " + (statuses.ready || 0) + " · 失败 " + ((statuses.error || 0) + (statuses.rejected || 0) + (statuses.no_swing || 0))),
        kpi("处理失败率", pct(u.failure_rate), u.pipeline_errors + " / " + u.saved_events + " 次上传"),
        kpi("报告「符合」比例", trustTotal ? pct(f.trust_yes / trustTotal) : "—", "符合 " + f.trust_yes + " · 不符合 " + f.trust_no + " · 文字 " + f.written),
        kpi("30 天提问数", d.questions_30d, "已同意视频处理 " + a.video_consent + " 人"),
      ]),
      card("最近 30 天每日注册（UTC）", [window.adminCharts.bars({
        labels: d.signups_by_day.map(function (x) { return x.day; }),
        values: d.signups_by_day.map(function (x) { return x.count; }),
        name: "人注册",
        label: "最近 30 天每日注册人数",
        empty: "最近 30 天没有新注册",
      })]),
      card("最近事件", [eventTable(d.recent_events, true)])
    );
  }

  function eventTable(rows, withUser) {
    var cols = [
      { label: "时间", cls: "nowrap", render: function (e) { return fmtTime(e.ts); } },
      { label: "事件", cls: "nowrap", render: function (e) { return eventLabel(e.kind); } },
    ];
    if (withUser) {
      cols.push({ label: "用户", render: function (e) {
        if (!e.user_id) return "—";
        return h("button", { type: "button", class: "linkish", text: e.email || e.user_id, onclick: function () { openUser(e.user_id); } });
      } });
    }
    cols.push({ label: "详情", render: function (e) { return h("span", { class: "props", text: eventProps(e) || "—" }); } });
    return table(cols, rows || [], { empty: "还没有事件" });
  }

  // ------------------------------------------------------------ users

  var USER_COLS = [
    { key: "email", label: "邮箱" },
    { key: "created_at", label: "注册时间", cls: "nowrap", render: function (u) { return fmtTime(u.created_at); } },
    { key: "last_login_at", label: "最后登录", cls: "nowrap", render: function (u) { return fmtTime(u.last_login_at); } },
    { key: "last_seen_at", label: "最后活跃", cls: "nowrap", render: function (u) { return fmtAgo(u.last_seen_at); } },
    { key: "logins", label: "登录次数", cls: "num" },
    { key: "visits", label: "访问次数", cls: "num" },
    { key: "total_seconds", label: "总时长", cls: "num nowrap", render: function (u) { return u.visits ? fmtDur(u.total_seconds) : "—"; } },
    { key: "uploads", label: "上传", cls: "num nowrap", render: function (u) { return u.uploads + (u.failed ? "（失败 " + u.failed + "）" : ""); } },
    { key: "questions", label: "提问", cls: "num" },
    { key: "feedback", label: "反馈", cls: "num" },
    { key: "plan", label: "套餐", render: function (u) {
      return h("span", { class: "chip" + (u.pro ? " chip--pro" : ""), text: u.pro ? "Pro · " + (u.plan || "") : "免费" });
    } },
    { key: "coach_code", label: "教练码" },
  ];

  async function loadUsers() {
    setStatus("加载中…");
    var d = await api("/api/admin/users");
    setStatus("");
    state.users = d.users || [];
    paintUsers();
  }

  function paintUsers() {
    var q = $("userSearch").value.trim().toLowerCase();
    var rows = state.users.filter(function (u) {
      if (!q) return true;
      return [u.email, u.user_id, u.coach_code].some(function (v) { return String(v || "").toLowerCase().indexOf(q) >= 0; });
    });
    var key = state.sortKey, dir = state.sortDir;
    rows.sort(function (a, b) {
      var x = a[key], y = b[key];
      if (x === y) return 0;
      if (x === null || x === undefined || x === "") return 1;
      if (y === null || y === undefined || y === "") return -1;
      return (x > y ? 1 : -1) * dir;
    });
    $("userCount").textContent = rows.length + " / " + state.users.length + " 个用户";
    var head = h("tr", {}, USER_COLS.map(function (c) {
      var arrow = state.sortKey === c.key ? (state.sortDir > 0 ? " ↑" : " ↓") : "";
      return h("th", {
        class: "sortable",
        text: c.label + arrow,
        "aria-sort": state.sortKey === c.key ? (state.sortDir > 0 ? "ascending" : "descending") : "none",
        onclick: function () {
          if (state.sortKey === c.key) state.sortDir = -state.sortDir;
          else { state.sortKey = c.key; state.sortDir = c.key === "email" ? 1 : -1; }
          paintUsers();
        },
      });
    }));
    var body = h("tbody", {}, rows.map(function (u) {
      var tr = h("tr", { class: "clickable" }, USER_COLS.map(function (c) {
        var v = c.render ? c.render(u) : u[c.key];
        var td = h("td", { class: c.cls || null });
        if (v instanceof Node) td.appendChild(v);
        else td.textContent = v === null || v === undefined || v === "" ? "—" : String(v);
        return td;
      }));
      tr.addEventListener("click", function () { openUser(u.user_id); });
      return tr;
    }));
    $("userTable").replaceChildren(h("thead", {}, [head]), body);
    if (!rows.length) $("userTable").replaceChildren(h("tbody", {}, [h("tr", {}, [h("td", { class: "empty", text: "没有匹配的用户" })])]));
  }
  $("userSearch").addEventListener("input", paintUsers);

  // ------------------------------------------------------------ one user

  async function openUser(userId) {
    state.userId = userId;
    showTab("user");
    var box = $("tab-user");
    box.replaceChildren(h("p", { class: "muted", text: "加载中…" }));
    var d;
    try {
      d = await api("/api/admin/user?user_id=" + encodeURIComponent(userId));
    } catch (err) {
      box.replaceChildren(h("button", { type: "button", text: "← 返回用户列表", onclick: function () { showTab("users"); } }), h("p", { class: "status is-error", text: err.message }));
      return;
    }
    var acc = d.account, sub = d.subscription;
    var resetBox = h("div", { class: "reset-box" });
    var head = h("div", { class: "detail-head" }, [
      h("button", { type: "button", text: "← 返回", onclick: function () { showTab("users"); } }),
      h("h2", { text: acc.email }),
      h("span", { class: "chip" + (sub.pro ? " chip--pro" : ""), text: sub.pro ? "Pro · " + (sub.plan || "") : "免费" }),
      h("span", { class: "spacer" }),
      h("button", { type: "button", text: "生成重置密码链接", onclick: function () { makeResetLink(acc.user_id, resetBox); } }),
    ]);

    var facts = h("dl", { class: "facts" });
    [
      ["用户 ID", acc.user_id],
      ["注册时间", fmtTime(acc.created_at)],
      ["最后登录", fmtTime(acc.last_login_at)],
      ["最后活跃", fmtTime(acc.last_active_at)],
      ["访问次数 / 总时长", d.visits.length ? d.visits.length + " 次 / " + fmtDur(d.total_seconds) : "0 次"],
      ["教练码", acc.coach_code || "—"],
      ["同意条款", (acc.terms_version || "—") + "（" + fmtTime(acc.terms_accepted_at) + "）"],
      ["同意视频处理", acc.video_consent_at ? (acc.video_consent_version + "（" + fmtTime(acc.video_consent_at) + "）") : "还没有"],
      ["上次改密码", fmtTime(acc.password_changed_at)],
    ].forEach(function (p) { facts.append(h("dt", { text: p[0] }), h("dd", { text: p[1] })); });

    var payBox = h("div", {}, [h("p", { class: "muted", text: "正在读取 Stripe…" })]);
    var subFacts = h("dl", { class: "facts" });
    [
      ["状态", sub.status || "没有订阅"],
      ["方案", sub.plan || "—"],
      ["当前周期结束", sub.current_period_end ? fmtTime(sub.current_period_end) : "—"],
      ["期末取消", sub.cancel_at_period_end ? "是" : "否"],
      ["Stripe 客户", sub.customer || "—"],
    ].forEach(function (p) { subFacts.append(h("dt", { text: p[0] }), h("dd", { text: p[1] })); });

    box.replaceChildren(
      head,
      resetBox,
      h("div", { class: "grid2" }, [card("账户", [facts]), card("订阅与付款", [subFacts, payBox])]),
      card("上传的视频（" + d.captures.length + "）", [captureTable(d.captures)]),
      h("div", { class: "grid2" }, [
        card("访问记录（间隔 30 分钟以上算新的一次）", [table([
          { label: "开始", cls: "nowrap", render: function (v) { return fmtTime(v.start); } },
          { label: "结束", cls: "nowrap", render: function (v) { return fmtTime(v.end); } },
          { label: "时长", cls: "num nowrap", render: function (v) { return fmtDur(v.seconds); } },
        ], d.visits.slice(0, 200), { empty: "还没有访问记录（这个功能上线后才开始记录）" })]),
        card("登录与密码记录", [table([
          { label: "时间", cls: "nowrap", render: function (e) { return fmtTime(e.ts); } },
          { label: "事件", render: function (e) { return eventLabel(e.kind); } },
        ], d.logins, { empty: "还没有登录记录（这个功能上线后才开始记录）" })]),
      ]),
      card("反馈（" + d.feedback.length + "）", [feedbackTable(d.feedback, false)]),
      card("问教练的问题（保留 " + d.question_retention_days + " 天，" + d.questions.length + " 条）", [questionTable(d.questions, false)]),
      card("全部事件（最近 300 条）", [eventTable(d.events, false)])
    );
    loadPayments(userId, payBox);
  }

  function captureTable(rows) {
    return table([
      { label: "上传时间", cls: "nowrap", render: function (c) { return fmtTime(c.created_at); } },
      { label: "文件名", render: function (c) { return c.filename; } },
      { label: "状态", render: function (c) { return statusChip(c.deleted ? "deleted" : c.status); } },
      { label: "识别", render: function (c) {
        return c.sport ? (c.sport_label || c.sport) + (c.sport_confidence ? "（" + Math.round(c.sport_confidence * 100) + "%）" : "") : "—";
      } },
      { label: "大小", cls: "num nowrap", render: function (c) { return fmtBytes(c.bytes); } },
      { label: "说明", cls: "text", render: function (c) {
        var parts = [c.detect_message || c.message || c.progress_message, c.failed_at ? "失败环节：" + c.failed_at : "", c.error ? "错误：" + c.error : ""];
        return parts.filter(Boolean).join("\n") || "—";
      } },
      { label: "原视频", cls: "nowrap", render: function (c) {
        if (c.deleted) return "已随挥杆删除";
        if (c.raw_video_deleted) return "已按保留期删除";
        if (!c.has_video) return "无";
        return h("span", {}, [
          h("button", { type: "button", text: "播放", onclick: function () { playVideo(c); } }),
          h("span", { class: "muted small", text: " 保留到 " + fmtTime(c.raw_video_expires_at).slice(0, 10) }),
        ]);
      } },
    ], rows, { empty: "还没有上传" });
  }

  async function loadPayments(userId, box) {
    try {
      var d = await api("/api/admin/payments?user_id=" + encodeURIComponent(userId));
      var parts = [];
      if (d.note) parts.push(h("p", { class: "muted", text: d.note }));
      if (d.invoices && d.invoices.length) {
        parts.push(table([
          { label: "日期", cls: "nowrap", render: function (i) { return fmtTime(i.created); } },
          { label: "金额", cls: "num nowrap", render: function (i) { return fmtMoney(i.status === "paid" ? i.amount_paid : i.amount_due, i.currency); } },
          { label: "状态", render: function (i) { return statusChip(i.status === "paid" ? "ready" : i.status); } },
          { label: "发票", render: function (i) {
            return i.hosted_invoice_url ? h("a", { href: i.hosted_invoice_url, target: "_blank", rel: "noopener noreferrer", text: i.number || "查看" }) : (i.number || "—");
          } },
        ], d.invoices));
      } else if (!d.note) {
        parts.push(h("p", { class: "muted", text: "Stripe 上没有发票。" }));
      }
      if (d.dashboard_url) parts.push(h("p", {}, [h("a", { href: d.dashboard_url, target: "_blank", rel: "noopener noreferrer", text: "在 Stripe 后台打开这个客户 ↗" })]));
      box.replaceChildren.apply(box, parts);
    } catch (err) {
      box.replaceChildren(h("p", { class: "status is-error", text: err.message }));
    }
  }

  async function makeResetLink(userId, box) {
    if (!window.confirm("给这个用户生成一个 24 小时内有效、只能用一次的重置密码链接？\n生成后要由你发给对方本人。")) return;
    try {
      var d = await api("/api/admin/reset-link", { body: { user_id: userId } });
      var input = h("input", { type: "text", readonly: true, value: d.link, "aria-label": "重置密码链接" });
      box.replaceChildren(
        h("p", { class: "muted", text: "链接 " + d.expires_hours + " 小时内有效、只能用一次。请只发给 " + d.email + " 本人。用户设置新密码后，所有设备都会退出登录。" }),
        input,
        h("div", {}, [h("button", { type: "button", text: "复制", onclick: function () {
          input.select();
          try { navigator.clipboard.writeText(d.link); } catch (_) { document.execCommand("copy"); }
        } })])
      );
    } catch (err) {
      box.replaceChildren(h("p", { class: "status is-error", text: err.message }));
    }
  }

  // ------------------------------------------------------------ video

  async function playVideo(capture) {
    var dlg = $("videoDialog");
    var player = $("videoPlayer");
    $("videoTitle").textContent = capture.filename || capture.session_id;
    $("videoMeta").textContent = "加载中…";
    $("videoSources").replaceChildren();
    player.removeAttribute("src");
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    try {
      var d = await api("/api/admin/video?session_id=" + encodeURIComponent(capture.session_id));
      var m = d.meta || {};
      $("videoMeta").textContent = [
        m.duration ? m.duration + " 秒" : "", m.fps ? m.fps + " fps" : "",
        m.width ? m.width + "×" + m.height : "", "挥杆 ID " + d.session_id,
      ].filter(Boolean).join(" · ");
      var labels = { playback: "播放版", original: "原始上传" };
      d.files.forEach(function (f, i) {
        $("videoSources").append(h("button", {
          type: "button",
          text: (labels[f.kind] || f.kind) + "（" + fmtBytes(f.bytes) + "）",
          onclick: function () { player.src = f.url; player.play().catch(function () {}); },
        }));
        if (i === 0) player.src = f.url;
      });
    } catch (err) {
      $("videoMeta").textContent = err.message;
    }
  }
  $("videoClose").addEventListener("click", function () { $("videoDialog").close(); });
  $("videoDialog").addEventListener("close", function () {
    var p = $("videoPlayer");
    p.pause();
    p.removeAttribute("src");
    p.load();
  });

  // ------------------------------------------------------------ feedback, questions, audit

  function userCell(r) {
    if (!r.user_id) return "—";
    return h("button", { type: "button", class: "linkish", text: r.email || r.user_id, onclick: function () { openUser(r.user_id); } });
  }

  function feedbackTable(rows, withUser) {
    var cols = [{ label: "时间", cls: "nowrap", render: function (r) { return fmtTime(r.ts); } }];
    if (withUser) cols.push({ label: "用户", render: userCell });
    cols.push(
      { label: "评价", render: function (r) {
        if (r.rating === "yes") return h("span", { class: "chip chip--good", text: "符合" });
        if (r.rating === "no") return h("span", { class: "chip chip--bad", text: "不符合" });
        return "—";
      } },
      { label: "内容", cls: "text", render: function (r) { return r.text || "—"; } },
      { label: "挥杆", cls: "nowrap", render: function (r) { return r.session_id ? String(r.session_id).slice(0, 8) : "—"; } }
    );
    return table(cols, rows, { empty: "还没有反馈" });
  }

  function questionTable(rows, withUser) {
    var cols = [{ label: "时间", cls: "nowrap", render: function (r) { return fmtTime(r.ts); } }];
    if (withUser) cols.push({ label: "用户", render: userCell });
    cols.push(
      { label: "问题", cls: "text", render: function (r) { return r.question; } },
      { label: "挥杆", cls: "nowrap", render: function (r) { return r.session_id ? String(r.session_id).slice(0, 8) : "—"; } }
    );
    return table(cols, rows, { empty: "还没有记录到提问（这个功能上线后才开始记录）" });
  }

  async function loadFeedback() {
    setStatus("加载中…");
    var d = await api("/api/admin/feedback");
    setStatus("");
    $("tab-feedback").replaceChildren(card("全部反馈（最近 1000 条）", [feedbackTable(d.feedback, true)]));
  }

  async function loadQuestions() {
    setStatus("加载中…");
    var d = await api("/api/admin/questions");
    setStatus("");
    $("tab-questions").replaceChildren(card("问教练的问题（最近 1000 条，只存登录用户的问题）", [questionTable(d.questions, true)]));
  }

  var AUDIT_LABELS = { admin_login: "登录后台", admin_denied: "后台登录被拒", video_view: "播放视频", reset_link: "生成重置链接", backers_export: "导出 backer 名单", backer_removed: "移除 backer" };
  async function loadAudit() {
    setStatus("加载中…");
    var d = await api("/api/admin/audit");
    setStatus("");
    $("tab-audit").replaceChildren(card("审计日志（谁在什么时候看了什么）", [table([
      { label: "时间", cls: "nowrap", render: function (r) { return fmtTime(r.ts); } },
      { label: "管理员", render: function (r) { return r.admin; } },
      { label: "操作", render: function (r) { return AUDIT_LABELS[r.action] || r.action; } },
      { label: "对象", render: function (r) {
        return [r.user_id ? "用户 " + r.user_id : "", r.session_id ? "挥杆 " + String(r.session_id).slice(0, 8) : "", r.reason ? "原因 " + r.reason : "", r.rows !== undefined ? r.rows + " 行" : "", r.backer_id ? "记录 " + r.backer_id : ""].filter(Boolean).join(" · ") || "—";
      } },
    ], d.rows, { empty: "还没有记录" })]));
  }

  // ------------------------------------------------------------ start

  function start(email) {
    $("login").hidden = true;
    $("app").hidden = false;
    $("meEmail").textContent = email || "";
    showTab("live");
  }

  if (userToken && adminToken) {
    api("/api/admin/overview")
      .then(function () {
        var email = "";
        try { email = sessionStorage.getItem(EMAIL_KEY) || ""; } catch (_) {}
        start(email);
      })
      .catch(function () { showLogin(""); });
  } else {
    showLogin("");
  }
})();
