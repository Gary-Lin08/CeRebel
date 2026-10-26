/* Progress view (thin version for the current page): this swing against an earlier one.
   Loaded after the main inline script; uses its globals (authToken, authHeaders, showToast,
   selectFix) only at call time. The page calls onLibrary(data) after /api/library and
   onSession(session) after /api/session. All numbers come from /api/compare. */
(function () {
  "use strict";

  const PHASES = [
    ["address", "Address"],
    ["top", "Top"],
    ["impact", "Impact"],
    ["finish", "Finish"],
  ];
  const VERDICT = {
    improved: ["Better", "is-better"],
    worse: ["Worse", "is-worse"],
    no_clear_change: ["No clear change", "is-same"],
    not_comparable: ["Can't compare", "is-off"],
    info: ["", "is-info"],
  };
  const STATUS_CLASS = {
    resolved: "is-better",
    improving: "is-better",
    borderline: "is-same",
    unchanged: "is-same",
    worse: "is-worse",
    not_comparable: "is-off",
  };

  let library = null;
  let currentSession = null;
  let cardToken = 0;
  let sheet = null;
  let syncStop = null;

  function headers(extra) {
    try { return authHeaders(extra); } catch (_) { return Object.assign({}, extra || {}); }
  }
  function signedIn() {
    try { return !!authToken; } catch (_) { return false; }
  }
  function track(event, props) {
    if (window.cerebelBilling) window.cerebelBilling.track(event, props);
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((k) => {
      const v = attrs[k];
      if (v == null || v === false) return;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k === "onclick") node.addEventListener("click", v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    (children || []).forEach((c) => { if (c) node.appendChild(c); });
    return node;
  }

  function fmtDate(iso, withTime) {
    const d = new Date(iso || "");
    if (isNaN(d.getTime())) return "Earlier swing";
    const opts = { month: "short", day: "numeric" };
    if (withTime) { opts.hour = "numeric"; opts.minute = "2-digit"; }
    return d.toLocaleString(undefined, opts);
  }
  function sameDay(a, b) {
    return String(a || "").slice(0, 10) === String(b || "").slice(0, 10);
  }

  function isReady(row) {
    const stage = (row.progress && row.progress.stage) || row.status;
    return stage === "ready";
  }
  function readyCaptures() {
    const rows = ((library && library.captures) || []).filter(isReady);
    return rows.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
  }

  async function ensureLibrary(sessionId) {
    const known = library && (library.captures || []).some((r) => r.session_id === sessionId);
    if (known) return library;
    const r = await fetch("/api/library", { headers: headers() });
    if (!r.ok) return null;
    library = await r.json();
    return library;
  }

  async function fetchCompare(before, after) {
    const q = "?before=" + encodeURIComponent(before) + "&after=" + encodeURIComponent(after);
    const r = await fetch("/api/compare" + q, { headers: headers() });
    let data = {};
    try { data = await r.json(); } catch (_) {}
    return { status: r.status, data };
  }

  // ------------------------------------------------------------ card on the result page

  function cardHost() {
    let card = document.getElementById("progressCard");
    if (card) return card;
    const main = document.querySelector("#fixHero .fix-hero__main");
    if (!main) return null;
    card = el("div", { id: "progressCard", class: "cp-mini", hidden: true });
    main.insertBefore(card, main.querySelector(".fix-hero__actions"));
    return card;
  }

  function hideCard() {
    const card = document.getElementById("progressCard");
    if (card) { card.hidden = true; card.innerHTML = ""; }
  }

  function renderCard(data) {
    const card = cardHost();
    if (!card) return;
    card.innerHTML = "";
    const issue = data.main_issue;
    const since = "Since " + fmtDate(data.before.created_at, sameDay(data.before.created_at, data.after.created_at));
    let line;
    if (issue && issue.status !== "not_comparable") {
      line = el("p", { class: "cp-mini__line" }, [
        document.createTextNode("Last time's fix: "),
        el("strong", { class: "cp-pill " + (STATUS_CLASS[issue.status] || ""), text: issue.status_label }),
        issue.display ? el("span", { class: "cp-mini__figure", text: issue.display }) : null,
      ]);
    } else {
      line = el("p", { class: "cp-mini__line", text: "See what changed since your last swing." });
    }
    card.appendChild(el("p", { class: "cp-mini__kicker", text: since }));
    card.appendChild(line);
    card.appendChild(
      el("button", {
        type: "button",
        class: "cp-mini__open",
        text: "See progress",
        onclick: () => { track("progress_open", { from: "card" }); openSheet(data.before.session_id, data.after.session_id, data); },
      })
    );
    card.hidden = false;
    track("progress_card_view", { status: issue ? issue.status : "none" });
  }

  async function onSession(session) {
    currentSession = session || null;
    const token = ++cardToken;
    hideCard();
    if (!signedIn() || !session || session.empty || session.processing || session.error) return;
    if (session.no_swing || session.demo || (session.sport && session.sport !== "golf")) return;
    const sid = session.session_id;
    if (!sid) return;
    try {
      if (!(await ensureLibrary(sid)) || token !== cardToken) return;
      const ready = readyCaptures();
      const i = ready.findIndex((r) => r.session_id === sid);
      if (i <= 0) return;
      const res = await fetchCompare(ready[i - 1].session_id, sid);
      if (token !== cardToken || res.status !== 200) return;
      if (res.data.comparability && res.data.comparability.level === "none") return;
      renderCard(res.data);
    } catch (_) {}
  }

  function onLibrary(data) {
    if (!data || !data.ok) return;
    library = data;
    const box = document.getElementById("libraryBox");
    if (!box) return;
    let btn = document.getElementById("progressLibraryBtn");
    const pair = data.compare && data.compare.default;
    if (!pair) { if (btn) btn.remove(); return; }
    if (!btn) {
      btn = el("button", { type: "button", id: "progressLibraryBtn", class: "cp-library-btn", text: "See your progress" });
      const list = document.getElementById("libraryList");
      box.insertBefore(btn, list || null);
    }
    btn.onclick = () => {
      const p = library.compare.default;
      track("progress_open", { from: "library" });
      openSheet(p.before, p.after);
    };
  }

  // ------------------------------------------------------------ progress sheet

  function closeSheet() {
    stopSync();
    if (sheet) sheet.remove();
    sheet = null;
    document.removeEventListener("keydown", onKey);
    document.documentElement.classList.remove("cp-locked");
  }
  function onKey(e) { if (e.key === "Escape") closeSheet(); }

  function optionLabel(row, all) {
    const clash = all.some((o) => o !== row && sameDay(o.created_at, row.created_at));
    const name = String(row.filename || "Swing").slice(0, 28);
    return fmtDate(row.created_at, clash) + " · " + name;
  }

  function pairPicker(before, after) {
    const ready = readyCaptures();
    const mk = (id, selected) => {
      const s = el("select", { id, class: "cp-select", "aria-label": id === "cpBefore" ? "Earlier swing" : "Later swing" });
      ready.forEach((row) => {
        const o = el("option", { value: row.session_id, text: optionLabel(row, ready) });
        if (row.session_id === selected) o.selected = true;
        s.appendChild(o);
      });
      s.addEventListener("change", () => {
        const b = sheet.querySelector("#cpBefore").value;
        const a = sheet.querySelector("#cpAfter").value;
        if (b && a && b !== a) load(b, a);
      });
      return s;
    };
    return el("div", { class: "cp-pair" }, [mk("cpBefore", before), el("span", { class: "cp-arrow", "aria-hidden": "true", text: "→" }), mk("cpAfter", after)]);
  }

  async function openSheet(before, after, preloaded) {
    closeSheet();
    if (!library) { try { await ensureLibrary(after); } catch (_) {} }
    const body = el("div", { class: "cp-body", "aria-live": "polite" });
    sheet = el("div", { class: "cp-sheet", role: "dialog", "aria-modal": "true", "aria-label": "Your progress" }, [
      el("div", { class: "cp-card", "data-lenis-prevent": true }, [
        el("div", { class: "cp-head" }, [
          el("h3", { text: "Your progress" }),
          el("button", { type: "button", class: "cp-close", "aria-label": "Close", text: "×", onclick: closeSheet }),
        ]),
        pairPicker(before, after),
        body,
      ]),
    ]);
    sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });
    document.addEventListener("keydown", onKey);
    document.documentElement.classList.add("cp-locked");
    document.body.appendChild(sheet);
    if (preloaded) render(preloaded);
    else load(before, after);
  }

  async function load(before, after) {
    if (!sheet) return;
    stopSync();
    const body = sheet.querySelector(".cp-body");
    body.innerHTML = "";
    body.appendChild(el("p", { class: "cp-muted", text: "Lining up your swings…" }));
    let res;
    try { res = await fetchCompare(before, after); } catch (_) { res = { status: 0, data: {} }; }
    if (!sheet) return;
    body.innerHTML = "";
    if (res.status === 402) {
      body.appendChild(el("div", { class: "cp-upsell" }, [
        el("p", { text: res.data.message || "Comparing any two swings is part of Pro." }),
        el("div", { class: "cp-row-actions" }, [
          el("button", {
            type: "button",
            class: "primary",
            text: "See Pro",
            onclick: () => {
              if (window.cerebelBilling) {
                window.cerebelBilling.openPaywall({ title: "Compare any two swings", message: res.data.message });
              }
            },
          }),
          res.data.default
            ? el("button", { type: "button", text: "Back to your last two", onclick: () => resetPicker(res.data.default) })
            : null,
        ]),
      ]));
      return;
    }
    if (res.status !== 200) {
      body.appendChild(el("p", { class: "cp-note is-warn", text: res.data.message || "We couldn't compare these swings right now." }));
      return;
    }
    render(res.data);
  }

  function resetPicker(pair) {
    const b = sheet && sheet.querySelector("#cpBefore");
    const a = sheet && sheet.querySelector("#cpAfter");
    if (b) b.value = pair.before;
    if (a) a.value = pair.after;
    load(pair.before, pair.after);
  }

  function render(data) {
    const body = sheet && sheet.querySelector(".cp-body");
    if (!body) return;
    body.innerHTML = "";
    const b = sheet.querySelector("#cpBefore");
    const a = sheet.querySelector("#cpAfter");
    if (b) b.value = data.before.session_id;
    if (a) a.value = data.after.session_id;

    const comp = data.comparability || {};
    if (comp.message) {
      body.appendChild(el("p", { class: "cp-note" + (comp.level === "full" ? "" : " is-warn"), text: comp.message }));
    }
    body.appendChild(issueBlock(data));
    if (comp.level !== "none") {
      body.appendChild(stageBlock(data));
      body.appendChild(metricsBlock(data));
    }
    const next = data.next_issue;
    if (next) {
      const isCurrent = currentSession && currentSession.session_id === data.after.session_id;
      body.appendChild(el("div", { class: "cp-section" }, [
        el("p", { class: "cp-kicker", text: next.same_as_before ? "Still the main fix" : "Next focus" }),
        el("p", { class: "cp-next", text: next.headline || "" }),
        isCurrent && typeof selectFix === "function"
          ? el("button", {
              type: "button",
              text: "Open this fix",
              onclick: () => { closeSheet(); try { selectFix(next.finding_id, { play: true }); } catch (_) {} },
            })
          : null,
      ]));
    }
    body.appendChild(el("p", { class: "cp-fine", text: data.disclosure || "" }));
  }

  function issueBlock(data) {
    const issue = data.main_issue;
    const section = el("div", { class: "cp-section cp-issue" }, [el("p", { class: "cp-kicker", text: "Last time's main fix" })]);
    if (!issue) {
      section.appendChild(el("p", { class: "cp-muted", text: "Nothing was flagged as a main fix last time." }));
      return section;
    }
    section.appendChild(el("p", { class: "cp-issue__headline", text: issue.headline || "" }));
    if (issue.label) section.appendChild(el("p", { class: "cp-muted", text: issue.label }));
    section.appendChild(el("div", { class: "cp-issue__row" }, [
      issue.display ? el("span", { class: "cp-issue__figure", text: issue.display }) : null,
      el("span", { class: "cp-pill " + (STATUS_CLASS[issue.status] || ""), text: issue.status_label }),
    ]));
    const metric = (data.metrics || []).find((m) => m.id === issue.metric_id);
    const meta = [];
    if (issue.goal) meta.push("Goal " + issue.goal);
    if (metric) meta.push("estimate " + metric.margin);
    if (meta.length) section.appendChild(el("p", { class: "cp-fine", text: meta.join(" · ") }));
    // The figure and pill already say it; spell it out only when the verdict needs a reason.
    if (issue.message && (issue.status === "borderline" || issue.status === "not_comparable")) {
      section.appendChild(el("p", { class: "cp-issue__msg", text: issue.message }));
    }
    return section;
  }

  // Two videos, one phase at a time.
  function pane(side, label) {
    const wrap = el("figure", { class: "cp-pane" });
    wrap.appendChild(el("figcaption", { text: label }));
    const url = side.media && side.media.video_url;
    if (url) {
      const v = el("video", { muted: true, playsinline: true, preload: "metadata" });
      v.muted = true;
      v.playsInline = true;
      v.src = url;
      wrap.appendChild(v);
      if (window.cerebelSkeleton) {
        window.cerebelSkeleton.attach(v).setSession(side.session_id).setStatic(side.highlight);
      }
    } else {
      wrap.appendChild(el("div", {
        class: "cp-novideo",
        text: side.media && side.media.video_removed
          ? "Video removed after 30 days for privacy. The measurements are kept."
          : "No video for this swing.",
      }));
    }
    return wrap;
  }

  function seek(video, t) {
    if (!video || t == null) return;
    video.pause();
    const go = () => { try { video.currentTime = Math.max(0, Number(t)); } catch (_) {} };
    if (video.readyState >= 1) go();
    else video.addEventListener("loadedmetadata", go, { once: true });
  }

  function stageBlock(data) {
    const sameDate = sameDay(data.before.created_at, data.after.created_at);
    const left = pane(data.before, fmtDate(data.before.created_at, sameDate));
    const right = pane(data.after, fmtDate(data.after.created_at, sameDate));
    const vb = left.querySelector("video");
    const va = right.querySelector("video");
    const chips = el("div", { class: "cp-chips", role: "group", "aria-label": "Swing phase" });
    const pick = (id) => {
      stopSync();
      chips.querySelectorAll("button").forEach((c) => c.classList.toggle("is-on", c.dataset.id === id));
      seek(vb, data.before.events[id]);
      seek(va, data.after.events[id]);
    };
    PHASES.forEach(([id, label]) => {
      if (data.before.events[id] == null || data.after.events[id] == null) return;
      const c = el("button", { type: "button", text: label, onclick: () => pick(id) });
      c.dataset.id = id;
      chips.appendChild(c);
    });
    const section = el("div", { class: "cp-section" }, [chips, el("div", { class: "cp-stage" }, [left, right])]);
    if (vb && va) {
      section.appendChild(el("button", {
        type: "button",
        class: "cp-play",
        text: "Play both, half speed",
        onclick: () => playSynced(vb, va, data.before.events, data.after.events),
      }));
    }
    window.setTimeout(() => pick("top"), 0);
    sheet._videos = [vb, va];
    return section;
  }

  // Play the earlier swing at half speed and keep the later one on the same phase:
  // between two phase moments, map time linearly from one swing onto the other.
  function playSynced(vb, va, eb, ea) {
    stopSync();
    const keys = PHASES.map(([id]) => id).filter((id) => eb[id] != null && ea[id] != null);
    if (keys.length < 2) return;
    const tb = keys.map((k) => Number(eb[k]));
    const ta = keys.map((k) => Number(ea[k]));
    const rate = 0.5;
    vb.playbackRate = rate;
    let running = true;
    let raf = 0;
    const tick = () => {
      if (!running) return;
      const t = vb.currentTime;
      if (t >= tb[tb.length - 1]) { stopSync(); return; }
      let i = 0;
      while (i < tb.length - 2 && t > tb[i + 1]) i++;
      const segB = Math.max(tb[i + 1] - tb[i], 1e-3);
      const segA = Math.max(ta[i + 1] - ta[i], 1e-3);
      va.playbackRate = Math.min(4, Math.max(0.0625, rate * (segA / segB)));
      const target = ta[i] + ((t - tb[i]) / segB) * segA;
      if (Math.abs(va.currentTime - target) > 0.12) {
        try { va.currentTime = target; } catch (_) {}
      }
      raf = window.requestAnimationFrame(tick);
    };
    syncStop = () => {
      running = false;
      window.cancelAnimationFrame(raf);
      vb.pause();
      va.pause();
      syncStop = null;
    };
    seek(vb, tb[0]);
    seek(va, ta[0]);
    Promise.all([vb.play(), va.play()]).then(() => { raf = window.requestAnimationFrame(tick); }).catch(() => stopSync());
  }
  function stopSync() { if (syncStop) syncStop(); }

  function metricsBlock(data) {
    const list = el("ul", { class: "cp-metrics" });
    (data.metrics || []).forEach((m) => {
      const [word, cls] = VERDICT[m.verdict] || ["", ""];
      const row = el("button", { type: "button", class: "cp-metric" }, [
        el("span", { class: "cp-metric__label" }, [
          el("span", { text: m.label }),
          el("small", { text: "estimate " + m.margin + (m.verdict === "not_comparable" && m.view_sensitive ? " · different camera angle" : "") }),
        ]),
        el("span", { class: "cp-metric__side" }, [
          el("span", { class: "cp-metric__value", text: m.display }),
          word ? el("span", { class: "cp-pill " + cls, text: word }) : null,
        ]),
      ]);
      if (m.verdict === "not_comparable") row.classList.add("is-dim");
      row.addEventListener("click", () => {
        const [vb, va] = (sheet && sheet._videos) || [];
        stopSync();
        if (sheet) sheet.querySelectorAll(".cp-chips button").forEach((c) => c.classList.toggle("is-on", c.dataset.id === m.event));
        seek(vb, m.time_before);
        seek(va, m.time_after);
      });
      list.appendChild(el("li", {}, [row]));
    });
    return el("div", { class: "cp-section" }, [el("p", { class: "cp-kicker", text: "What changed" }), list]);
  }

  window.cerebelCompare = { onLibrary, onSession, open: openSheet, close: closeSheet };
})();
