/* CeRebel app page (/app): home, swing report, coach sheet, upload and account for customers.
   Routes live in the hash: #/ (home), #/swing/<id>[/drill|/data], #/sample, plus #signin / #signup.
   The shared modules loaded after this file (skeleton-overlay.js, compare.js, billing.js,
   swing_trim.js) call the globals defined at the top at run time, the same names the old page
   gave them. Every number on screen comes from the analysis endpoints, never from this file. */

// ------------------------------------------------------------------ globals for shared modules

const AUTH_KEY = "cerebel_user_token_v1";
let authToken = "";
try { authToken = localStorage.getItem(AUTH_KEY) || ""; } catch (_) {}
let activeSessionId = null;
let session = null;

function authHeaders(extra) {
  const headers = Object.assign({}, extra || {});
  if (authToken) headers.Authorization = "Bearer " + authToken;
  return headers;
}
function escapeHtml(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function showToast(message) { CxApp.toast(message); }
function refreshLibrary() { return CxApp.refreshLibrary(); }
function loadSession() { if (activeSessionId) CxApp.go("#/swing/" + activeSessionId); }
function openAccountPanel(focusUpload) { CxApp.openAccountPanel(!!focusUpload); }
function selectFix(id, opts) { CxApp.selectFix(id, opts || {}); }
function showConsumerChat() { CxApp.openSheet("coach"); }
function addBubble(who, html, cls) { return CxApp.addBubble(who, html, cls); }

// An email link (/?session=<id>, forwarded here by index.html) becomes a report route.
(function adoptEmailLink() {
  const params = new URLSearchParams(window.location.search);
  const sid = params.get("session");
  if (!sid) return;
  params.delete("session");
  const query = params.toString();
  const route = /^[A-Za-z0-9_-]{4,80}$/.test(sid) ? "#/swing/" + sid : window.location.hash;
  window.history.replaceState(null, "", window.location.pathname + (query ? "?" + query : "") + route);
})();

const CxApp = (function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const VIEWS = ["vHome", "vReport", "vWait"];
  const SHEETS = { coach: "sCoach", film: "sFilm", auth: "sAuth", account: "sAccount" };
  const DONE = new Set(["ready", "error", "no_swing", "deleted"]);
  const STAGE_LABEL = {
    uploading: "Receiving", extracting: "Preparing", queued: "Waiting in line", gemx: "Rebuilding your body",
    ik: "Measuring", processing: "Working", ready: "Ready", no_swing: "No clear swing", error: "Needs another clip",
  };
  // Waiting screen steps; "Waiting in line" only shows when there is a line.
  const WAIT_STEPS = [
    ["received", "Upload received", ["uploading"]],
    ["preparing", "Preparing frames", ["extracting", "processing"]],
    ["queued", "Waiting in line", ["queued"]],
    ["body", "Rebuilding your body", ["gemx"]],
    ["measure", "Measuring angles", ["ik"]],
  ];
  const SPEEDS = [1, 0.5, 0.25];
  const MOMENT_RATE = 0.3;

  const state = {
    library: null,
    libraryPoll: 0,
    latest: {},          // session id -> { video, finding } for the home card
    view: null,
    viewToken: 0,
    waitTimer: 0,
    report: null,        // { sid, sample, session, analysis, findings, fi, tab }
    overlay: null,
    speed: 0.5,
    moment: null,
    sparks: [],
    sheet: null,
    authMode: "register",
    afterAuth: null,
    asking: false,
    dictation: null,
    call: null,          // a live voice call: { msgs } = coach chat length when it started
  };

  // ---------------------------------------------------------------- small helpers

  let toastTimer = 0;
  function toast(message) {
    const el = $("toast");
    if (!el || !message) return;
    el.textContent = message;
    el.classList.add("is-on");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => el.classList.remove("is-on"), 3200);
  }
  function track(event, props) {
    try { if (window.cerebelBilling) window.cerebelBilling.track(event, props || {}); } catch (_) {}
  }
  function stageOf(row) { return (row && ((row.progress && row.progress.stage) || row.status)) || "processing"; }
  function whenLabel(iso) {
    const d = new Date(iso || "");
    if (Number.isNaN(d.getTime())) return "Swing";
    const now = new Date();
    const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return "Today, " + time;
    if (d.toDateString() === new Date(now.getTime() - 864e5).toDateString()) return "Yesterday, " + time;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + time;
  }
  function sortedCaptures() {
    const rows = ((state.library && state.library.captures) || []).filter((r) => stageOf(r) !== "deleted");
    return rows.slice().sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  }
  // "Chair drill. Set a chair…" -> name "Chair drill", steps ["Set a chair…", …]
  function drillParts(text) {
    const raw = String(text || "").trim();
    const cut = raw.indexOf(". ");
    if (cut < 0 || cut > 60) return { name: raw, steps: [] };
    const steps = raw.slice(cut + 2).split(/\.\s+/).filter(Boolean).map((s) => s.replace(/\.?$/, "."));
    return { name: raw.slice(0, cut), steps };
  }
  function seriesValue(series, t) {
    const ts = series.times || [];
    const vs = series.values || [];
    if (ts.length < 2) return vs.length ? vs[0] : null;
    if (t <= ts[0]) return vs[0];
    if (t >= ts[ts.length - 1]) return vs[vs.length - 1];
    let i = 1;
    while (i < ts.length - 1 && ts[i] < t) i++;
    const f = (t - ts[i - 1]) / Math.max(ts[i] - ts[i - 1], 1e-6);
    return vs[i - 1] + f * (vs[i] - vs[i - 1]);
  }
  function formatValue(v, unit) {
    if (v == null || !Number.isFinite(v)) return "—";
    if (unit === "°") return Math.round(v) + "°";
    const digits = Math.abs(v) >= 10 ? 0 : 1;
    return v.toFixed(digits) + (unit ? " " + unit : "");
  }
  function renderMarkdown(text) {
    const raw = String(text || "");
    let html;
    try {
      html = window.marked && window.marked.parse ? window.marked.parse(raw, { breaks: true, gfm: true }) : "<p>" + escapeHtml(raw).replace(/\n/g, "<br>") + "</p>";
    } catch (_) {
      html = "<p>" + escapeHtml(raw).replace(/\n/g, "<br>") + "</p>";
    }
    if (window.DOMPurify) return window.DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
    return "<p>" + escapeHtml(raw).replace(/\n/g, "<br>") + "</p>";
  }
  // The coach can append a machine-readable block for the old page; the app shows the prose only.
  function stripAnalysis(text) {
    return String(text || "").replace(/```cerebel-analysis[\s\S]*?```/gi, "").trim();
  }

  // ---------------------------------------------------------------- views, routes, sheets

  function show(view) {
    state.view = view;
    $("boot").hidden = true;
    VIEWS.forEach((id) => { $(id).hidden = id !== view; });
    if (view !== "vReport") pauseVideo();
    window.scrollTo(0, 0);
  }

  function parseRoute() {
    const hash = window.location.hash.replace(/^#/, "");
    if (hash === "signin" || hash === "signup") return { name: authToken ? "home" : "sample", auth: hash };
    if (hash === "sample" || hash === "/sample") return { name: "sample" };
    const m = hash.match(/^\/swing\/([A-Za-z0-9_-]{4,80})(?:\/(fix|drill|data))?$/);
    if (m) return { name: "swing", sid: m[1], tab: m[2] || "fix" };
    return { name: authToken ? "home" : "sample" };
  }

  function go(hash) {
    if (window.location.hash === hash) route();
    else window.location.hash = hash;
  }

  async function route() {
    state.viewToken += 1;
    window.clearTimeout(state.waitTimer);
    // A call is about the swing on screen; leaving it ends the call (the chat is cleared too).
    if (state.call) {
      state.call = null;
      if (window.cerebelVoice) window.cerebelVoice.hangUp();
    }
    closeSheet();
    const r = parseRoute();
    if (r.auth) window.history.replaceState(null, "", window.location.pathname + window.location.search + (authToken ? "#/" : "#/sample"));
    if (r.name === "home") {
      await renderHome();
    } else if (r.name === "sample") {
      await openReport({ sample: true });
    } else if (!authToken) {
      state.afterAuth = "#/swing/" + r.sid;
      await openReport({ sample: true });
      openAuth("login", "Sign in to see this swing.");
      return;
    } else {
      await openReport({ sid: r.sid, tab: r.tab });
    }
    if (r.auth && !authToken) openAuth(r.auth === "signin" ? "login" : "register");
  }

  function openSheet(name) {
    const id = SHEETS[name];
    if (!id) return;
    closeSheet();
    state.sheet = name;
    $(id).hidden = false;
    $("backdrop").hidden = false;
    document.body.classList.add("is-locked");
    if (name === "coach") {
      track("chat_open", { sample: !!(state.report && state.report.sample) });
      paintCallCard();
      window.setTimeout(() => { if (window.matchMedia("(pointer: fine)").matches) $("q").focus(); }, 60);
    }
  }
  function closeSheet() {
    if (!state.sheet) return;
    const id = SHEETS[state.sheet];
    state.sheet = null;
    if (id) $(id).hidden = true;
    $("backdrop").hidden = true;
    document.body.classList.remove("is-locked");
  }

  // ---------------------------------------------------------------- account and library

  function signOutLocal() {
    authToken = "";
    try { localStorage.removeItem(AUTH_KEY); } catch (_) {}
    state.library = null;
    state.latest = {};
    activeSessionId = null;
    window.clearTimeout(state.libraryPoll);
  }

  async function refreshLibraryData() {
    if (!authToken) return null;
    let r;
    try {
      r = await fetch("/api/library", { headers: authHeaders() });
    } catch (_) {
      return null;
    }
    if (r.status === 401) {
      signOutLocal();
      toast("You were signed out. Sign in again to see your swings.");
      return null;
    }
    const data = await r.json().catch(() => null);
    if (!r.ok || !data) return null;
    state.library = data;
    if (window.cerebelBilling) window.cerebelBilling.paint(data);
    if (window.cerebelCompare) window.cerebelCompare.onLibrary(data);
    paintAccount();
    schedulePoll();
    return data;
  }

  // While a swing is being analysed, refresh the list so home shows its progress and the report.
  function schedulePoll() {
    window.clearTimeout(state.libraryPoll);
    const pending = sortedCaptures().some((row) => !DONE.has(stageOf(row)));
    if (!pending) return;
    state.libraryPoll = window.setTimeout(async () => {
      await refreshLibraryData();
      if (state.view === "vHome") paintHome();
    }, 4000);
  }

  function paintAccount() {
    const data = state.library;
    const email = (data && data.user && data.user.email) || "";
    $("accountBtn").textContent = email ? email.charAt(0) : "?";
    $("accountEmail").textContent = email;
    const sub = (data && data.subscription) || {};
    const quota = (data && data.quota) || {};
    $("accountPlan").textContent = sub.pro ? "Pro" : "Free";
    $("accountPlan").classList.toggle("cx-chip--fix", !!sub.pro);
    $("accountQuota").textContent = quota.remaining != null ? quota.remaining + " of " + quota.monthly_limit + " analyses left this month" : "";
    $("accountUpgrade").hidden = !!sub.pro;
    $("accountManage").hidden = !sub.can_manage;
    const line = quotaLine();
    $("homeQuota").textContent = line;
    $("filmQuota").textContent = line;
  }

  function quotaLine() {
    const data = state.library;
    if (!data || !data.quota || data.quota.remaining == null) return "";
    const q = data.quota;
    const pro = data.subscription && data.subscription.pro;
    const word = pro ? "analyses" : "free swings";
    return q.remaining + " of " + q.monthly_limit + " " + word + " left this month";
  }

  function openAccountPanel(focusUpload) {
    if (!authToken) return openAuth("register");
    if (focusUpload) return openFilm();
    openSheet("account");
  }

  // ---------------------------------------------------------------- home

  async function renderHome() {
    if (!authToken) return go("#/sample");
    const token = state.viewToken;
    show("vHome");
    if (state.library) paintHome();
    const data = await refreshLibraryData();
    if (token !== state.viewToken) return;
    if (!data) {
      if (!authToken) return go("#/sample");
      $("homeQuota").textContent = "We couldn't load your swings. Check your connection and reload.";
      return;
    }
    paintHome();
  }

  function paintHome() {
    const rows = sortedCaptures();
    const ready = rows.filter((r) => stageOf(r) === "ready");
    const pending = rows.filter((r) => !DONE.has(stageOf(r)));
    $("homeFirst").hidden = rows.length > 0;
    paintPending(pending);
    paintLatest(ready[0] || null);
    paintProgress(ready);
    paintAllSwings(rows);
  }

  function paintPending(rows) {
    const box = $("homePending");
    box.textContent = "";
    rows.forEach((row) => {
      const p = row.progress || {};
      const a = document.createElement("a");
      a.className = "cx-card cx-pending";
      a.href = "#/swing/" + row.session_id;
      const title = document.createElement("strong");
      title.className = "cx-card__title";
      title.textContent = "Working on your swing";
      const line = document.createElement("p");
      line.textContent = (STAGE_LABEL[stageOf(row)] || "Working") + (p.queue_message ? " · " + p.queue_message : "");
      const bar = document.createElement("div");
      bar.className = "cx-pending__bar";
      const fill = document.createElement("i");
      const pct = Number(p.percent);
      fill.style.width = (Number.isFinite(pct) ? Math.max(6, Math.min(100, pct)) : 8) + "%";
      bar.appendChild(fill);
      a.append(title, line, bar);
      box.appendChild(a);
    });
  }

  function paintLatest(row) {
    $("homeLatestWrap").hidden = !row;
    $("homeDrillWrap").hidden = true;
    if (!row) return;
    const sid = row.session_id;
    $("homeLatest").href = "#/swing/" + sid;
    $("homeLatestDate").textContent = whenLabel(row.created_at);
    $("homeDrillOpen").href = "#/swing/" + sid + "/drill";
    const cached = state.latest[sid];
    if (cached) return paintLatestDetail(row, cached);
    $("homeLatestTitle").textContent = "Open your report";
    $("homeLatestChip").hidden = true;
    loadLatestDetail(sid).then((detail) => {
      if (!detail || state.view !== "vHome") return;
      state.latest[sid] = detail;
      const current = sortedCaptures().find((r) => r.session_id === sid);
      if (current) paintLatestDetail(current, detail);
    });
  }

  async function loadLatestDetail(sid) {
    try {
      const r = await fetch("/api/session?session_id=" + encodeURIComponent(sid), { headers: authHeaders() });
      if (!r.ok) return null;
      const s = await r.json();
      const detail = { video: s.video_url_path || null, finding: null };
      if (s.no_swing || s.sport === "general") return detail;
      const a = await fetch(
        "/api/sport_analysis?sport=" + encodeURIComponent(s.sport || "golf") + "&video_id=" + encodeURIComponent(sid),
        { headers: authHeaders() }
      ).then((res) => res.json()).catch(() => null);
      const findings = mainFindings(a);
      detail.finding = findings[0] || null;
      return detail;
    } catch (_) {
      return null;
    }
  }

  function paintLatestDetail(row, detail) {
    const f = detail.finding;
    $("homeLatestTitle").textContent = f ? f.headline || f.title : "Open your report";
    $("homeLatestChip").hidden = !f;
    const thumb = $("homeThumb");
    if (detail.video && thumb.dataset.sid !== row.session_id) {
      thumb.dataset.sid = row.session_id;
      thumb.src = detail.video + "#t=0.5";
    }
    if (!f || !f.drill) return;
    const drill = drillParts(f.drill);
    $("homeDrillWrap").hidden = false;
    $("homeDrillName").textContent = drill.name;
    $("homeDrillText").textContent = drill.steps.join(" ");
    const n = (row.practice || []).filter((p) => p.finding_id === f.id).length;
    $("homeDrillCount").textContent = n ? "Practiced " + n + (n === 1 ? " time" : " times") : "Not practiced yet";
  }

  async function paintProgress(ready) {
    const wrap = $("homeProgressWrap");
    const box = $("homeProgress");
    const pair = state.library && state.library.compare && state.library.compare.default;
    if (!ready.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    if (!pair) {
      box.dataset.pair = "";
      box.innerHTML = "<p>Film one more swing and we'll put the same moments side by side, so you can see what changed.</p>";
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cx-btn cx-btn--small";
      b.dataset.act = "film";
      b.textContent = "Film a swing";
      box.appendChild(b);
      return;
    }
    const key = pair.before + ":" + pair.after;
    if (box.dataset.pair === key) return;
    box.dataset.pair = key;
    box.innerHTML = "<p>Comparing your last two swings…</p>";
    let data = null;
    try {
      const q = "?before=" + encodeURIComponent(pair.before) + "&after=" + encodeURIComponent(pair.after);
      const r = await fetch("/api/compare" + q, { headers: authHeaders() });
      data = r.ok ? await r.json() : null;
    } catch (_) {}
    if (box.dataset.pair !== key) return;
    box.textContent = "";
    const issue = data && data.main_issue;
    const p = document.createElement("p");
    if (issue && issue.status !== "not_comparable") {
      p.append("Last time's fix: ");
      const pill = document.createElement("span");
      pill.className = "cx-chip " + (issue.status === "resolved" || issue.status === "improving" ? "cx-chip--good" : issue.status === "worse" ? "cx-chip--fix" : "");
      pill.textContent = issue.status_label || "";
      p.appendChild(pill);
      if (issue.display) p.append(" " + issue.display);
    } else {
      p.textContent = "See what changed between your last two swings.";
    }
    const b = document.createElement("button");
    b.type = "button";
    b.className = "cx-btn cx-btn--small";
    b.textContent = "See progress";
    b.addEventListener("click", () => {
      track("progress_open", { from: "home" });
      if (window.cerebelCompare) window.cerebelCompare.open(pair.before, pair.after, data && data.ok !== false ? data : null);
    });
    box.append(p, b);
  }

  function paintAllSwings(rows) {
    $("homeAllWrap").hidden = rows.length < 2;
    const list = $("homeAll");
    list.textContent = "";
    rows.forEach((row) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = "#/swing/" + row.session_id;
      const name = document.createElement("span");
      name.textContent = whenLabel(row.created_at);
      const stage = stageOf(row);
      const chip = document.createElement("span");
      chip.className = "cx-chip" + (stage === "ready" ? "" : " cx-chip--fix");
      chip.textContent = STAGE_LABEL[stage] || "Working";
      a.append(name, chip);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "cx-icon-btn";
      del.setAttribute("aria-label", "Delete the swing from " + whenLabel(row.created_at));
      del.textContent = "×";
      del.addEventListener("click", () => deleteSwing(row.session_id));
      li.append(a, del);
      list.appendChild(li);
    });
  }

  async function deleteSwing(sid) {
    if (!window.confirm("Delete this swing? The video and its analysis are removed from our servers.")) return;
    try {
      const r = await fetch("/api/library/delete", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ session_id: sid }),
      });
      if (!r.ok) throw new Error();
      delete state.latest[sid];
      toast("Swing deleted.");
    } catch (_) {
      toast("Couldn't delete that swing. Try again.");
    }
    await refreshLibraryData();
    if (state.view === "vHome") paintHome();
    else go("#/");
  }

  // ---------------------------------------------------------------- report

  function mainFindings(analysis) {
    const all = (analysis && Array.isArray(analysis.findings)) ? analysis.findings : [];
    const fixes = all.filter((f) => !f.strength);
    return fixes.length ? fixes : all;
  }
  function finding() {
    const rep = state.report;
    return rep && rep.findings ? rep.findings[rep.fi] || null : null;
  }

  function paintReportChrome() {
    const rep = state.report;
    const visitor = !authToken;
    $("reportBack").hidden = visitor;
    $("reportLogo").hidden = !visitor;
    $("reportTitle").hidden = visitor;
    $("reportSignup").hidden = !visitor;
    $("sampleBanner").hidden = !rep.sample;
    $("sampleBannerBtn").textContent = visitor ? "Upload yours, free" : "Film yours";
    $("sampleBannerBtn").dataset.act = visitor ? "signup" : "film";
    $("reportDate").textContent = "";
    paintMic();
  }

  function resetReport(message) {
    $("fixKicker").textContent = message || "";
    $("fixHeadline").textContent = "";
    $("fixPlain").textContent = "";
    $("fixMetricRow").hidden = true;
    $("momentBtn").hidden = true;
    $("reportTabs").hidden = true;
    $("reportNotice").hidden = true;
    $("trackFix").style.width = "0";
    $("stageHint").hidden = true;
    $("stageGone").hidden = true;
    $("phaseNow").textContent = "";
    $("msgs").textContent = "";
    $("chips").textContent = "";
    state.sparks = [];
  }

  function paintReportDate() {
    const rep = state.report;
    const row = rep && sortedCaptures().find((c) => c.session_id === rep.sid);
    $("reportDate").textContent = row ? whenLabel(row.created_at) : rep && rep.sample ? "Sample" : "";
  }

  function notice(text) {
    $("fixKicker").textContent = "";
    $("reportNotice").hidden = false;
    $("reportNoticeText").textContent = text;
  }

  async function openReport({ sid, sample, tab }) {
    const token = state.viewToken;
    state.report = { sid: sid || null, sample: !!sample, session: null, analysis: null, findings: [], fi: 0, tab: tab || "fix" };
    session = null;
    show("vReport");
    paintReportChrome();
    resetReport("Opening your swing…");
    const query = sample ? "&sample=1" : "&session_id=" + encodeURIComponent(sid);
    let r;
    try {
      r = await fetch("/api/session?t=" + Date.now() + query, { headers: authHeaders() });
    } catch (_) {
      notice("We couldn't reach CeRebel. Check your connection and try again.");
      return;
    }
    if (token !== state.viewToken) return;
    if (r.status === 410) { toast("That swing was deleted."); return go("#/"); }
    if (r.status === 403) {
      // An expired sign-in looks the same as someone else's swing; the library call tells them apart.
      if (authToken) await refreshLibraryData();
      if (token !== state.viewToken) return;
      if (!authToken && sid) {
        state.afterAuth = "#/swing/" + sid;
        notice("Sign in to see this swing.");
        openAuth("login", "Your sign-in expired. Sign in again to see this swing.");
        return;
      }
      notice("This swing belongs to another account.");
      return;
    }
    const s = await r.json().catch(() => null);
    if (token !== state.viewToken) return;
    if (!s || s.error) { notice((s && s.message) || "We couldn't open this swing."); return; }
    if (s.processing) return openWait(s);
    if (s.empty) { notice(s.message || "There's no swing to show yet."); return; }
    session = s;
    activeSessionId = s.session_id;
    state.report.sid = s.session_id;
    state.report.session = s;
    paintReportDate();
    if (authToken && !state.library) {
      // Opened straight from a link: the date and the practice count come from the library.
      refreshLibraryData().then(() => {
        if (token !== state.viewToken) return;
        paintReportDate();
        if (state.report.analysis) paintDrill();
      });
    }
    paintVideo(s);
    paintChips(s.coach_briefs);
    if (s.no_swing || s.sport === "general") {
      notice(s.detect_message || "We couldn't find a clear swing in this clip. Try a side-on take that shows the whole swing.");
      return;
    }
    $("fixKicker").textContent = "Measuring your swing…";
    let a = null;
    try {
      const url = "/api/sport_analysis?sport=" + encodeURIComponent(s.sport || "golf") + "&video_id=" + encodeURIComponent(s.session_id);
      a = await fetch(url, { headers: authHeaders() }).then((res) => res.json());
    } catch (_) {}
    if (token !== state.viewToken) return;
    const findings = mainFindings(a);
    if (!a || a.error || !findings.length) {
      notice((a && a.message) || "We measured this swing but couldn't name a fix. Try another clip, side-on with your whole body in frame.");
      return;
    }
    state.report.analysis = a;
    state.report.findings = findings;
    renderReport();
    track(sample ? "sample_report_view" : "report_view", {});
  }

  function renderReport() {
    const rep = state.report;
    const f = finding();
    if (!f) return;
    $("reportNotice").hidden = true;
    $("fixKicker").textContent = rep.fi === 0 ? "The one thing to fix" : "Also in this swing";
    $("fixHeadline").textContent = f.headline || f.title || "";
    $("fixPlain").textContent = f.plain || f.detail || "";
    const metric = f.metric && f.metric.value;
    $("fixMetricRow").hidden = !metric;
    $("fixMetric").textContent = metric || "";
    $("fixMetricCap").textContent = (f.metric && f.metric.caption) || "";
    const timed = Number.isFinite(Number(f.start_time)) && Number.isFinite(Number(f.end_time));
    $("momentBtn").hidden = !timed || !video().getAttribute("src");
    $("fixWhyBlock").hidden = !f.why;
    $("fixWhy").textContent = f.why || "";
    $("fixCueBlock").hidden = !f.cue;
    $("fixCue").textContent = f.cue || "";
    paintTrust();
    paintOthers();
    paintDrill();
    paintSeries();
    paintTrack();
    paint3dEntry();
    if (state.overlay) state.overlay.setNote(f, rep.analysis);
    $("reportTabs").hidden = false;
    setTab(rep.tab);
  }

  function selectFinding(index, { play = false } = {}) {
    const rep = state.report;
    if (!rep || !rep.findings[index]) return;
    rep.fi = index;
    renderReport();
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (play) playMoment();
  }
  function selectFix(id, opts) {
    const rep = state.report;
    if (!rep) return;
    const i = rep.findings.findIndex((f) => f.id === id);
    if (i >= 0) selectFinding(i, { play: !!(opts && opts.play) });
  }

  function paintTrust() {
    const box = $("trust");
    box.textContent = "";
    const label = document.createElement("span");
    label.textContent = "Does this match what you feel?";
    box.appendChild(label);
    [["Yes", true], ["Not really", false]].forEach(([text, ok]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cx-btn";
      b.textContent = text;
      b.addEventListener("click", () => {
        track(ok ? "trust_yes" : "trust_no", { session_id: (state.report && state.report.sid) || "" });
        box.innerHTML = "<span>" + (ok ? "Thanks. Noted." : "Thanks. That helps us improve the analysis.") + "</span>";
      });
      box.appendChild(b);
    });
  }

  function paintOthers() {
    const rep = state.report;
    const box = $("others");
    box.textContent = "";
    rep.findings.forEach((o, i) => {
      if (i === rep.fi) return;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cx-other";
      const title = document.createElement("strong");
      title.textContent = o.headline || o.title;
      const line = document.createElement("span");
      line.textContent = o.metric && o.metric.value ? o.metric.value + " · " + (o.metric.caption || "") : (o.title || "");
      const chev = document.createElement("span");
      chev.className = "cx-chev";
      chev.setAttribute("aria-hidden", "true");
      chev.textContent = "›";
      b.append(title, line, chev);
      b.addEventListener("click", () => selectFinding(i, { play: true }));
      box.appendChild(b);
    });
    $("othersBlock").hidden = rep.findings.length < 2;
  }

  function ownRow() {
    const rep = state.report;
    if (!rep || rep.sample || !authToken) return null;
    return sortedCaptures().find((c) => c.session_id === rep.sid) || null;
  }

  function paintDrill() {
    const f = finding();
    const drill = drillParts(f.drill);
    $("drillName").textContent = drill.name || "No drill for this fix yet.";
    const steps = $("drillSteps");
    steps.textContent = "";
    drill.steps.forEach((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      steps.appendChild(li);
    });
    $("drillCheckRow").hidden = !f.check;
    $("drillCheck").textContent = f.check || "";
    const row = ownRow();
    $("practiceRow").hidden = !row || !f.drill;
    if (!row) return;
    const n = (row.practice || []).filter((p) => p.finding_id === f.id).length;
    $("practiceCount").textContent = n ? "Practiced " + n + (n === 1 ? " time" : " times") : "Not practiced yet";
  }

  async function markPracticed() {
    const f = finding();
    const row = ownRow();
    if (!f || !row) return;
    const btn = $("practicedBtn");
    btn.disabled = true;
    try {
      const r = await fetch("/api/library/practice", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ session_id: row.session_id, finding_id: f.id }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.message || "Couldn't save that. Try again.");
      row.practice = (row.practice || []).concat({ finding_id: f.id, at: new Date().toISOString() });
      paintDrill();
      toast("Marked as practiced. Film again when you're ready to check it.");
    } catch (err) {
      toast(err.message || "Couldn't save that. Try again.");
    } finally {
      btn.disabled = false;
    }
  }

  function setTab(name) {
    const tab = ["fix", "drill", "data"].includes(name) ? name : "fix";
    if (state.report) state.report.tab = tab;
    document.querySelectorAll(".cx-tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    $("panFix").hidden = tab !== "fix";
    $("panDrill").hidden = tab !== "drill";
    $("panData").hidden = tab !== "data";
  }

  // ---------------------------------------------------------------- video, timeline, overlay

  function video() { return $("fixVideo"); }
  function duration() {
    const a = state.report && state.report.analysis;
    const v = video();
    const fromAnalysis = a && Number(a.end_time);
    if (Number.isFinite(fromAnalysis) && fromAnalysis > 0) return fromAnalysis;
    return Number.isFinite(v.duration) && v.duration > 0 ? v.duration : 1;
  }

  function paintVideo(s) {
    const v = video();
    state.moment = null;
    if (s.video_url_path) {
      // The path carries a signed token (a video tag cannot send the login header).
      const url = new URL(s.video_url_path, window.location.origin);
      url.searchParams.set("t", String(Date.now()));
      v.src = url.pathname + url.search;
      v.loop = true;
      v.playbackRate = state.speed;
      v.load();
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    } else {
      v.removeAttribute("src");
      v.load();
      $("stageGone").hidden = !s.source_video_removed;
    }
    if (window.cerebelSkeleton && !s.fake_egocentric) {
      state.overlay = window.cerebelSkeleton.attach(v, { toggle: false, caption: false });
      state.overlay.setNote(null, null);
      state.overlay.setSession(s.session_id);
    }
    paintLines();
  }

  function pauseVideo() {
    const v = video();
    if (v && !v.paused) v.pause();
    state.moment = null;
  }

  function paintTrack() {
    const a = state.report.analysis;
    const f = finding();
    const track = $("track");
    track.querySelectorAll(".cx-track__tick").forEach((n) => n.remove());
    const end = duration();
    (a.phases || []).slice(1).forEach((p) => {
      const tick = document.createElement("i");
      tick.className = "cx-track__tick";
      tick.style.left = (Number(p.start_time) / end) * 100 + "%";
      track.insertBefore(tick, $("trackHead"));
    });
    const fx = $("trackFix");
    const start = Number(f.start_time);
    const stop = Number(f.end_time);
    if (Number.isFinite(start) && Number.isFinite(stop) && stop > start) {
      fx.style.left = (start / end) * 100 + "%";
      fx.style.width = ((stop - start) / end) * 100 + "%";
    } else {
      fx.style.width = "0";
    }
  }

  function phaseAt(t) {
    const phases = (state.report && state.report.analysis && state.report.analysis.phases) || [];
    return phases.find((p) => t >= p.start_time && t < p.end_time) || phases[phases.length - 1] || null;
  }

  function playMoment() {
    const f = finding();
    const v = video();
    if (!f || !v.getAttribute("src")) { toast("The video isn't available for this swing."); return; }
    const start = Math.max(0, Number(f.start_time) - 0.35);
    const stop = Math.min(duration(), Number(f.end_time) + 0.2);
    if (!Number.isFinite(start) || !Number.isFinite(stop)) return;
    state.moment = { start, stop };
    v.loop = false;
    v.playbackRate = MOMENT_RATE;
    try { v.currentTime = start; } catch (_) {}
    const p = v.play();
    if (p && p.catch) p.catch(() => {});
    const phase = phaseAt(Number(f.start_time) + 0.01);
    $("stageHint").textContent = "Slow motion" + (phase ? " · " + phase.label : "");
    $("stageHint").hidden = false;
    $("momentBtn").hidden = true;
    track("fix_expand", { kind: "moment" });
  }
  function endMoment() {
    const v = video();
    state.moment = null;
    v.pause();
    v.loop = true;
    v.playbackRate = state.speed;
    window.setTimeout(() => {
      if (state.moment) return;
      $("stageHint").hidden = true;
      $("momentBtn").hidden = false;
    }, 1400);
  }

  function seekFromPointer(e) {
    const r = $("track").getBoundingClientRect();
    const k = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const v = video();
    state.moment = null;
    v.pause();
    try { v.currentTime = k * duration(); } catch (_) {}
  }

  function paintLines() {
    let on = true;
    try { on = localStorage.getItem("cerebel_skeleton_on_v1") !== "0"; } catch (_) {}
    $("linesBtn").setAttribute("aria-pressed", String(on));
  }
  function toggleLines() {
    let on = true;
    try { on = localStorage.getItem("cerebel_skeleton_on_v1") !== "0"; } catch (_) {}
    try { localStorage.setItem("cerebel_skeleton_on_v1", on ? "0" : "1"); } catch (_) {}
    if (state.overlay) state.overlay.sync();
    paintLines();
  }

  function paintPlayIcon() {
    const v = video();
    $("playIcon").setAttribute("d", v.paused ? "M4 2.5v11l9-5.5z" : "M3.5 2.5h3v11h-3zM9.5 2.5h3v11h-3z");
    $("playBtn").setAttribute("aria-label", v.paused ? "Play" : "Pause");
  }

  function paintSpeed() {
    $("speedBtn").textContent = state.speed === 1 ? "1×" : state.speed === 0.5 ? "½×" : "¼×";
  }

  // All-data tab: one row per measured trace, the fix's trace first and in amber.
  function paintSeries() {
    const a = state.report.analysis;
    const f = finding();
    const list = $("seriesList");
    list.textContent = "";
    state.sparks = [];
    const series = (a.series || []).filter((s) => (s.values || []).length > 1);
    series.sort((x, y) => (y.id === f.series) - (x.id === f.series));
    const end = duration();
    const ns = "http://www.w3.org/2000/svg";
    series.forEach((s) => {
      const isFix = s.id === f.series;
      const box = document.createElement("div");
      box.className = "cx-series" + (isFix ? " cx-series--fix" : "");
      const head = document.createElement("div");
      head.className = "cx-series__head";
      const name = document.createElement("span");
      name.textContent = s.label;
      if (isFix) {
        const tag = document.createElement("span");
        tag.className = "cx-series__tag";
        tag.textContent = "This fix";
        name.appendChild(tag);
      }
      const val = document.createElement("span");
      val.className = "cx-num";
      head.append(name, val);
      const W = 300;
      const H = 40;
      const values = s.values.filter((v) => Number.isFinite(v));
      const min = Math.min.apply(null, values);
      const max = Math.max.apply(null, values);
      const x = (t) => (Math.max(0, Math.min(end, t)) / end) * W;
      const y = (v) => H - 3 - ((v - min) / Math.max(max - min, 1e-6)) * (H - 6);
      let d = "";
      s.times.forEach((t, i) => {
        if (!Number.isFinite(s.values[i])) return;
        d += (d ? "L" : "M") + x(t).toFixed(1) + " " + y(s.values[i]).toFixed(1);
      });
      const svg = document.createElementNS(ns, "svg");
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("aria-hidden", "true");
      if (isFix && Number.isFinite(Number(f.start_time)) && Number.isFinite(Number(f.end_time))) {
        const r = document.createElementNS(ns, "rect");
        r.setAttribute("x", x(Number(f.start_time)));
        r.setAttribute("y", 0);
        r.setAttribute("width", Math.max(0, x(Number(f.end_time)) - x(Number(f.start_time))));
        r.setAttribute("height", H);
        r.setAttribute("fill", "rgba(242,165,65,0.14)");
        svg.appendChild(r);
      }
      (a.phases || []).slice(1).forEach((p) => {
        const l = document.createElementNS(ns, "line");
        l.setAttribute("x1", x(p.start_time));
        l.setAttribute("x2", x(p.start_time));
        l.setAttribute("y1", 0);
        l.setAttribute("y2", H);
        l.setAttribute("stroke", "rgba(238,240,234,0.1)");
        l.setAttribute("vector-effect", "non-scaling-stroke");
        svg.appendChild(l);
      });
      const path = document.createElementNS(ns, "path");
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", isFix ? "#F2A541" : "rgba(238,240,234,0.75)");
      path.setAttribute("stroke-width", isFix ? 2 : 1.4);
      path.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(path);
      const head2 = document.createElementNS(ns, "line");
      head2.setAttribute("y1", 0);
      head2.setAttribute("y2", H);
      head2.setAttribute("stroke", "#EEF0EA");
      head2.setAttribute("stroke-width", 1.5);
      head2.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(head2);
      box.append(head, svg);
      list.appendChild(box);
      state.sparks.push({ s, val, head: head2, x });
    });
  }

  // One loop for the playhead, the phase name, the all-data values and the end of a slow-motion moment.
  function tick() {
    window.requestAnimationFrame(tick);
    if (state.view !== "vReport" || !state.report || !state.report.analysis) return;
    const v = video();
    const end = duration();
    const t = Math.min(v.currentTime || 0, end);
    if (state.moment && t >= state.moment.stop) endMoment();
    const k = end ? t / end : 0;
    $("trackHead").style.left = k * 100 + "%";
    $("track").setAttribute("aria-valuenow", String(Math.round(k * 100)));
    const f = finding();
    const phase = phaseAt(t);
    const inFix = f && t >= Number(f.start_time) && t <= Number(f.end_time);
    const html = (phase ? escapeHtml(phase.label) : "") + (inFix ? " · <b>the moment to fix</b>" : "");
    const el = $("phaseNow");
    if (el.innerHTML !== html) el.innerHTML = html;
    if (state.report.tab === "data") {
      state.sparks.forEach((sp) => {
        const text = formatValue(seriesValue(sp.s, t), sp.s.unit);
        if (sp.val.textContent !== text) sp.val.textContent = text;
        const xx = sp.x(t);
        sp.head.setAttribute("x1", xx);
        sp.head.setAttribute("x2", xx);
      });
    }
  }

  // ---------------------------------------------------------------- 3D body, full screen

  // body3d.js (and three.js behind it) load on the first open, never with the report. The view
  // follows the report video; once the video was deleted it runs on its own clock (t, playing).
  const b3d = { open: false, token: 0, mod: null, view: null, pushed: false, t: 0, playing: false, last: 0 };

  function has3dVideo() { return !!video().getAttribute("src"); }
  function time3d() { return has3dVideo() ? video().currentTime || 0 : b3d.t; }

  // From renderReport: the entry row, the fix named in the header, and the amber bones.
  function paint3dEntry() {
    const rep = state.report;
    const f = finding();
    $("body3dRow").hidden = !rep.session || rep.session.has_opensim === false;
    const focus = (f && f.focus) || {};
    $("b3dFix").textContent = (focus.label || (f && f.title) || "The fix") + ", the part to fix";
    if (b3d.view) b3d.view.setFinding(f);
  }

  // The same ticks and fix window as the report's timeline.
  function paint3dTrack() {
    const track = $("b3dTrack");
    track.querySelectorAll(".cx-track__tick").forEach((n) => n.remove());
    $("track").querySelectorAll(".cx-track__tick").forEach((n) => track.insertBefore(n.cloneNode(), $("b3dTrackHead")));
    $("b3dTrackFix").style.cssText = $("trackFix").style.cssText;
  }

  function loop3d(now) {
    if (!b3d.open) return;
    window.requestAnimationFrame(loop3d);
    const end = duration();
    const dt = b3d.last ? Math.min(0.1, (now - b3d.last) / 1000) : 0;
    b3d.last = now;
    if (b3d.playing && !has3dVideo()) b3d.t = (b3d.t + dt * state.speed) % end;
    const t = Math.min(time3d(), end);
    const k = end ? t / end : 0;
    $("b3dTrackHead").style.left = k * 100 + "%";
    $("b3dTrack").setAttribute("aria-valuenow", String(Math.round(k * 100)));
    const f = finding();
    const phase = phaseAt(t);
    const inFix = f && t >= Number(f.start_time) && t <= Number(f.end_time);
    const html = (phase ? escapeHtml(phase.label) : "") + (inFix ? " · <b>the moment to fix</b>" : "");
    if ($("b3dPhase").innerHTML !== html) $("b3dPhase").innerHTML = html;
    const paused = has3dVideo() ? video().paused : !b3d.playing;
    $("b3dPlayIcon").setAttribute("d", paused ? "M4 2.5v11l9-5.5z" : "M3.5 2.5h3v11h-3zM9.5 2.5h3v11h-3z");
    $("b3dPlay").setAttribute("aria-label", paused ? "Play" : "Pause");
    $("b3dSpeed").textContent = $("speedBtn").textContent;
  }

  // Same as the report's play button, or the 3D view's own clock when there is no video.
  function play3d() {
    const v = video();
    if (!has3dVideo()) { b3d.playing = !b3d.playing; return; }
    if (v.paused) {
      state.moment = null;
      v.loop = true;
      v.playbackRate = state.speed;
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    } else {
      v.pause();
    }
  }
  function seek3d(t) {
    const time = Math.max(0, Math.min(duration(), t));
    if (!has3dVideo()) { b3d.t = time; b3d.playing = false; return; }
    const v = video();
    state.moment = null;
    v.pause();
    try { v.currentTime = time; } catch (_) {}
  }

  async function open3d() {
    const rep = state.report;
    const f = finding();
    if (!rep || !rep.sid || !rep.analysis || !f || b3d.open) return;
    b3d.open = true;
    b3d.token += 1;
    const token = b3d.token;
    $("v3d").hidden = false;
    $("vReport").inert = true;
    document.body.classList.add("is-locked");
    $("b3dStatus").hidden = false;
    $("b3dStatus").textContent = "Loading the 3D body…";
    $("b3dZoom").setAttribute("aria-pressed", "false");
    $("b3dMuscles").setAttribute("aria-pressed", "true");
    $("b3dMuscles").hidden = false;
    $("b3dHint").textContent = window.matchMedia("(pointer: coarse)").matches ? "Drag to turn, pinch to zoom" : "Drag to turn, scroll to zoom";
    paint3dEntry();
    paint3dTrack();
    b3d.last = 0;
    window.requestAnimationFrame(loop3d);
    // The phone's back gesture closes the view instead of leaving the report. After a reload the
    // entry from last time is still there, so reuse it.
    try {
      if (!(window.history.state && window.history.state.cx3d)) window.history.pushState({ cx3d: true }, "");
      b3d.pushed = true;
    } catch (_) {}
    $("b3dDone").focus({ preventScroll: true });
    track("fix_expand", { kind: "body3d", sample: !!rep.sample });
    try {
      b3d.mod = b3d.mod || (await import("/static/body3d.js"));
      if (token !== b3d.token) return;
      const view = await b3d.mod.mount($("b3dStage"), {
        sessionId: rep.sid,
        finding: f,
        getTime: time3d,
        t0: Number(rep.analysis.start_time) || 0,
        t1: duration(),
      });
      if (token !== b3d.token) { view.dispose(); return; }
      b3d.view = view;
      // The switches may have been flipped while it loaded.
      if ($("b3dZoom").getAttribute("aria-pressed") === "true") view.setCloseUp(true);
      if ($("b3dMuscles").getAttribute("aria-pressed") !== "true") view.setMuscles(false);
      $("b3dStatus").hidden = true;
      $("b3dMuscles").hidden = !view.hasMuscles;
    } catch (err) {
      if (token !== b3d.token || (err && err.aborted)) return;
      $("b3dStatus").textContent = (err && err.userMessage) || "We couldn't open the 3D body. Check your connection and try again.";
    }
  }

  // navigated: the history entry already moved (back gesture, a new route), so don't go back again.
  function close3d(opts) {
    if (!b3d.open) return;
    const navigated = !!(opts && opts.navigated);
    b3d.open = false;
    b3d.token += 1;
    b3d.playing = false;
    if (b3d.mod) b3d.mod.dispose();
    b3d.view = null;
    $("v3d").hidden = true;
    $("vReport").inert = false;
    if (!state.sheet) document.body.classList.remove("is-locked");
    const pushed = b3d.pushed;
    b3d.pushed = false;
    if (pushed && !navigated && window.history.state && window.history.state.cx3d) window.history.back();
    if (!navigated) $("body3dRow").focus({ preventScroll: true });
  }

  function toggle3dOption(btn, apply) {
    const on = btn.getAttribute("aria-pressed") !== "true";
    btn.setAttribute("aria-pressed", String(on));
    if (b3d.view) apply(b3d.view, on);
  }

  function init3d() {
    document.addEventListener("click", (e) => {
      const el = e.target.closest("[data-act]");
      if (el && el.dataset.act === "body3d") open3d();
      else if (el && el.dataset.act === "close-3d") close3d();
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && b3d.open) close3d(); });
    window.addEventListener("popstate", (e) => { if (b3d.open && !(e.state && e.state.cx3d)) close3d({ navigated: true }); });
    window.addEventListener("hashchange", () => close3d({ navigated: true }));
    $("b3dPlay").addEventListener("click", play3d);
    $("b3dSpeed").addEventListener("click", () => {
      state.speed = SPEEDS[(SPEEDS.indexOf(state.speed) + 1) % SPEEDS.length];
      if (!state.moment) video().playbackRate = state.speed;
      paintSpeed();
    });
    $("b3dZoom").addEventListener("click", (e) => toggle3dOption(e.currentTarget, (view, on) => view.setCloseUp(on)));
    $("b3dMuscles").addEventListener("click", (e) => toggle3dOption(e.currentTarget, (view, on) => view.setMuscles(on)));
    const t = $("b3dTrack");
    let dragging = false;
    const at = (e) => {
      const r = t.getBoundingClientRect();
      seek3d(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * duration());
    };
    t.addEventListener("pointerdown", (e) => { dragging = true; t.setPointerCapture(e.pointerId); at(e); });
    t.addEventListener("pointermove", (e) => { if (dragging) at(e); });
    t.addEventListener("pointerup", () => { dragging = false; });
    t.addEventListener("pointercancel", () => { dragging = false; });
    t.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      seek3d(time3d() + (e.key === "ArrowRight" ? 0.1 : -0.1));
    });
  }

  // ---------------------------------------------------------------- waiting screen

  function openWait(s) {
    const token = state.viewToken;
    show("vWait");
    const stage = String(s.stage || s.status || "processing");
    const queued = stage === "queued" || Number(s.jobs_ahead) > 0;
    const steps = WAIT_STEPS.filter(([id]) => id !== "queued" || queued);
    let current = steps.findIndex(([, , stages]) => stages.includes(stage));
    if (current < 0) current = 1;
    paintWaitSteps(steps.map(([, label]) => label), current);
    const pct = Number(s.percent);
    $("waitBar").style.width = (Number.isFinite(pct) ? Math.max(6, Math.min(100, pct)) : 8) + "%";
    $("waitTitle").textContent = "Working on your swing";
    $("waitMsg").textContent = s.queue_message || s.message || "Rebuilding your body from the video…";
    $("waitNote").textContent = s.product_subtitle || "";
    $("waitActions").hidden = true;
    window.clearTimeout(state.waitTimer);
    state.waitTimer = window.setTimeout(() => {
      if (token !== state.viewToken || !s.session_id) return;
      openReportAgain(s.session_id, token);
    }, 4000);
  }

  async function openReportAgain(sid, token) {
    try {
      const r = await fetch("/api/session?t=" + Date.now() + "&session_id=" + encodeURIComponent(sid), { headers: authHeaders() });
      const s = await r.json();
      if (token !== state.viewToken) return;
      if (s && s.processing) return openWait(s);
    } catch (_) {
      if (token !== state.viewToken) return;
      state.waitTimer = window.setTimeout(() => openReportAgain(sid, token), 6000);
      return;
    }
    state.library = null;
    refreshLibraryData();
    openReport({ sid });
    toast("Your report is ready.");
  }

  function paintWaitSteps(labels, current) {
    const list = $("waitSteps");
    list.textContent = "";
    labels.forEach((label, i) => {
      const li = document.createElement("li");
      li.textContent = label;
      if (i < current) li.className = "is-done";
      if (i === current) {
        li.className = "is-current";
        li.setAttribute("aria-current", "step");
      }
      list.appendChild(li);
    });
  }

  function waitError(message) {
    show("vWait");
    $("waitTitle").textContent = "That clip didn't go through";
    $("waitSteps").textContent = "";
    $("waitBar").style.width = "0";
    $("waitMsg").textContent = message;
    $("waitNote").textContent = "";
    $("waitActions").hidden = false;
  }

  // ---------------------------------------------------------------- upload

  function openFilm() {
    if (!authToken) {
      state.afterAuth = "#/";
      return openAuth("register", "Create a free account to upload your swing.");
    }
    paintAccount();
    openSheet("film");
  }

  function uploadWithProgress(body, onProgress) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      const headers = authHeaders();
      Object.keys(headers).forEach((k) => xhr.setRequestHeader(k, headers[k]));
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (_) {}
        resolve({ status: xhr.status, ok: xhr.status >= 200 && xhr.status < 300, data });
      };
      xhr.onerror = () => resolve({ status: 0, ok: false, data: { message: "The upload stopped. Check your connection and try again." } });
      xhr.send(body);
    });
  }

  async function handleFile(input) {
    const file = input.files && input.files[0];
    input.value = "";
    if (!file || !authToken) return;
    closeSheet();
    if (window.cerebelConsent && !(await window.cerebelConsent.ensure())) {
      toast("Upload cancelled. We need your consent to process the video.");
      return;
    }
    state.viewToken += 1;
    show("vWait");
    $("waitTitle").textContent = "Uploading your swing";
    $("waitActions").hidden = true;
    $("waitNote").textContent = "Keep this page open until the upload finishes.";
    paintWaitSteps(["Checking the clip", "Uploading", "Rebuilding your body", "Measuring angles"], 0);
    $("waitBar").style.width = "4%";
    $("waitMsg").textContent = "Checking the clip…";
    track("upload_click", { mb: Math.round(file.size / 1e6) });
    let prep = null;
    if (window.cerebelSwingTrim) {
      try {
        prep = await window.cerebelSwingTrim.analyze(file, (msg) => { $("waitMsg").textContent = msg; });
      } catch (_) {
        prep = null;
      }
      if (prep && prep.block) return waitError(prep.warning + " This didn't use one of your analyses.");
      if (prep && prep.warning && !window.confirm(prep.warning + "\n\nUpload anyway?")) return go("#/");
    }
    const body = new FormData();
    body.append("file", file);
    if (prep && prep.window) {
      body.append("trim_start", String(prep.window[0]));
      body.append("trim_end", String(prep.window[1]));
    }
    paintWaitSteps(["Checking the clip", "Uploading", "Rebuilding your body", "Measuring angles"], 1);
    $("waitMsg").textContent = "Uploading…";
    const res = await uploadWithProgress(body, (k) => {
      $("waitBar").style.width = Math.max(4, Math.round(k * 100)) + "%";
      $("waitMsg").textContent = "Uploading… " + Math.round(k * 100) + "%";
    });
    const data = res.data || {};
    if (res.status === 402) {
      waitError(data.message || "You're out of free analyses this month.");
      if (data.paywall && window.cerebelBilling) window.cerebelBilling.openPaywall(data);
      return;
    }
    if (res.status === 428 && window.cerebelConsent) {
      window.cerebelConsent.reset(data.video_consent);
      waitError((data.message || "We need your consent first.") + " Choose the clip again after agreeing.");
      window.cerebelConsent.ensure();
      return;
    }
    if (!res.ok || !data.session_id) return waitError(data.message || "The upload failed. Try again.");
    if (data.warnings && data.warnings.length) toast(data.warnings.join(" "));
    state.library = null;
    refreshLibraryData();
    go("#/swing/" + data.session_id);
  }

  // ---------------------------------------------------------------- sign up / sign in

  function setAuthMode(mode) {
    state.authMode = mode === "login" ? "login" : "register";
    const isNew = state.authMode === "register";
    $("authNew").setAttribute("aria-selected", String(isNew));
    $("authOld").setAttribute("aria-selected", String(!isNew));
    $("authConsentRow").hidden = !isNew;
    $("authCoachRow").hidden = !isNew;
    $("authSubmit").textContent = isNew ? "Create account" : "Sign in";
    $("authPassword").setAttribute("autocomplete", isNew ? "new-password" : "current-password");
    $("authTitle").textContent = isNew ? "See your own swing" : "Welcome back";
    $("authSub").textContent = isNew ? "Free for 3 swings a month. No credit card." : "Sign in to see your swings.";
  }

  function openAuth(mode, message) {
    setAuthMode(mode);
    $("authStatus").textContent = message || "";
    openSheet("auth");
    if (mode === "register") track("signup_view", {});
    window.setTimeout(() => { if (window.matchMedia("(pointer: fine)").matches) $("authEmail").focus(); }, 60);
  }

  async function submitAuth(e) {
    e.preventDefault();
    const email = $("authEmail").value.trim();
    const password = $("authPassword").value;
    const isNew = state.authMode === "register";
    const status = $("authStatus");
    if (!email || !email.includes("@")) { status.textContent = "Enter your email address."; return; }
    if (password.length < 8) { status.textContent = "Use at least 8 characters for the password."; return; }
    if (isNew && !$("authConsent").checked) { status.textContent = "Tick the box to accept the Terms and Privacy Policy."; return; }
    const payload = { email, password };
    if (isNew) {
      payload.accept_terms = true;
      const coach = $("authCoach").value.trim();
      if (coach) payload.coach_code = coach;
    }
    const btn = $("authSubmit");
    btn.disabled = true;
    status.textContent = "";
    try {
      const r = await fetch(isNew ? "/api/auth/register" : "/api/auth/login", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.token) throw new Error(data.message || (isNew ? "Couldn't create the account." : "Couldn't sign in."));
      authToken = data.token;
      try { localStorage.setItem(AUTH_KEY, authToken); } catch (_) {}
      $("authPassword").value = "";
      state.library = null;
      state.latest = {};
      closeSheet();
      const next = state.afterAuth || "#/";
      state.afterAuth = null;
      toast(isNew ? "Account created. Film your first swing." : "Signed in.");
      go(next);
      if (window.cerebelVoice && window.cerebelVoice.refresh) window.cerebelVoice.refresh();
    } catch (err) {
      status.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function signOut() {
    try {
      await fetch("/api/auth/logout", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: "{}" });
    } catch (_) {}
    signOutLocal();
    closeSheet();
    toast("Signed out.");
    go("#/sample");
  }

  async function deleteAccount() {
    if (!window.confirm("Delete your account and every swing stored with it? This can't be undone.")) return;
    try {
      const r = await fetch("/api/account/delete", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: "{}" });
      if (!r.ok) throw new Error();
    } catch (_) {
      toast("Couldn't delete the account. Try again.");
      return;
    }
    signOutLocal();
    closeSheet();
    toast("Your account and videos were deleted.");
    go("#/sample");
  }

  // ---------------------------------------------------------------- coach

  function paintChips(briefs) {
    const box = $("chips");
    box.textContent = "";
    (Array.isArray(briefs) ? briefs : []).filter((b) => b && b.question && b.answer).slice(0, 4).forEach((b) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = b.question;
      btn.addEventListener("click", () => ask(b.question));
      box.appendChild(btn);
    });
  }

  function addMsg(who, content, opts) {
    const o = opts || {};
    const m = document.createElement("div");
    m.className = "cx-msg cx-msg--" + who + (o.plain ? " is-plain" : "") + (o.wait ? " cx-msg--wait" : "");
    if (o.html) m.innerHTML = content;
    else m.textContent = content;
    if (o.note) {
      const small = document.createElement("small");
      small.textContent = o.note;
      m.appendChild(small);
    }
    $("msgs").appendChild(m);
    const box = $("chat");
    box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
    return m;
  }

  // voice-coach.js writes the call's captions into the chat through this.
  function addBubble(who, html, cls) {
    const mine = /user/i.test(String(cls || who || ""));
    return addMsg(mine ? "me" : "coach", html, { html: true });
  }

  function matchBrief(question) {
    const q = String(question || "").trim().toLowerCase();
    const briefs = (session && session.coach_briefs) || [];
    return briefs.find((b) => b && b.answer && String(b.question || "").trim().toLowerCase() === q) || null;
  }

  async function readSse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const raw = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
        if (!raw || raw === "[DONE]") continue;
        let ev = null;
        try { ev = JSON.parse(raw); } catch (_) { continue; }
        onEvent(ev);
        if ((ev.type || ev.kind) === "done") {
          try { await reader.cancel(); } catch (_) {}
          return;
        }
      }
    }
  }

  async function ask(text) {
    const question = String(text || "").trim();
    if (!question || state.asking) return;
    if (state.sheet !== "coach") openSheet("coach");
    $("q").value = "";
    addMsg("me", question);
    const brief = matchBrief(question);
    if (brief) {
      addMsg("coach", brief.answer, { plain: true, note: "From this swing's measurements" });
      if (brief.finding_id) selectFix(brief.finding_id, {});
      return;
    }
    if (!session || !session.session_id) {
      addMsg("coach", "Open a swing first, then ask about it.", { plain: true });
      return;
    }
    state.asking = true;
    $("send").disabled = true;
    const pending = addMsg("coach", "Looking at your swing…", { wait: true });
    let answered = false;
    try {
      const payload = {
        question,
        video_id: session.session_id,
        attach_video: true,
        prompt_mode: session.default_prompt_mode || "golf",
      };
      const headers = authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" });
      const r = await fetch("/api/chat/stream", { method: "POST", headers, body: JSON.stringify(payload) });
      if (!r.ok) {
        let message = "";
        try { message = (await r.json()).message || ""; } catch (_) {}
        pending.remove();
        if (r.status === 429) return limitReached(message);
        throw new Error(message || "The coach couldn't answer just now. Try again in a moment.");
      }
      await readSse(r, (ev) => {
        const type = ev.type || ev.kind;
        const final = type === "final" ? ev.content : type === "done" ? ev.answer : "";
        if (final && !answered) {
          answered = true;
          pending.remove();
          addMsg("coach", renderMarkdown(stripAnalysis(final)), { html: true });
        } else if (type === "error" && !answered) {
          answered = true;
          pending.remove();
          addMsg("coach", ev.message || "Something went wrong. Try again.", { plain: true });
        }
      });
      if (!answered) {
        pending.remove();
        addMsg("coach", "The coach didn't answer. Try asking again.", { plain: true });
      }
    } catch (err) {
      if (pending.isConnected) pending.remove();
      addMsg("coach", err.message || "The coach couldn't answer just now.", { plain: true });
    } finally {
      state.asking = false;
      $("send").disabled = false;
    }
  }

  function limitReached(message) {
    const m = addMsg("coach", message || "You've reached today's question limit.", { plain: true });
    const b = document.createElement("button");
    b.type = "button";
    b.className = "cx-btn cx-btn--small cx-btn--primary";
    if (authToken) {
      b.textContent = "See Pro";
      b.addEventListener("click", () => { if (window.cerebelBilling) window.cerebelBilling.openPaywall({ message }); });
    } else {
      b.textContent = "Create a free account";
      b.dataset.act = "signup";
    }
    m.appendChild(b);
  }

  // ---------------------------------------------------------------- talking to the coach

  // A live call needs voice-coach.js with a start() and a way to carry the voice in this browser;
  // otherwise the mic dictates a question.
  function liveVoice() {
    const v = window.cerebelVoice;
    if (!v || typeof v.start !== "function") return null;
    return !v.state || v.state().available ? v : null;
  }
  function voiceMode() {
    if (liveVoice()) return "live";
    return window.SpeechRecognition || window.webkitSpeechRecognition ? "dictate" : "";
  }
  function paintMic() {
    const mode = voiceMode();
    $("micBtn").hidden = !mode;
    if (!state.dictation) $("micBtn").setAttribute("aria-label", mode === "live" ? "Talk to your coach" : "Ask out loud");
  }
  function paintCallCard() {
    const live = liveVoice();
    const next = live && live.state ? live.state() : {};
    $("callCard").hidden = !live;
    $("callNote").textContent = !authToken ? "Create a free account to try it."
      : next.need === "upgrade" ? "Part of Pro. Hands free, with your swing on screen."
      : next.kind === "trial" ? "One free try. Hands free, with your swing on screen."
      : "Hands free, with your swing on screen.";
  }

  // voice-coach.js reports here when a call can start, starts or ends.
  function onVoice(e) {
    const live = !!(e.detail && e.detail.live);
    if (live && !state.call) {
      state.call = { msgs: $("msgs").childElementCount };
      // The call panel takes the bottom of the screen; the swing stays in view above it.
      if (state.sheet === "coach") closeSheet();
    } else if (!live && state.call) {
      const said = $("msgs").childElementCount > state.call.msgs;
      state.call = null;
      if (said && state.sheet !== "coach") toast("Your call's transcript is in the coach chat.");
    }
    if (state.view === "vReport") paintMic();
    if (state.sheet === "coach") paintCallCard();
  }

  function micPressed() {
    const live = liveVoice();
    if (live) {
      if (!authToken) return openAuth("register", "Create a free account to talk to the coach.");
      return live.start();
    }
    dictate();
  }

  function dictate() {
    const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
    const mic = $("micBtn");
    if (!Rec) return openSheet("coach");
    if (state.dictation) { state.dictation.stop(); return; }
    const rec = new Rec();
    rec.lang = navigator.language || "en-US";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    state.dictation = rec;
    mic.classList.add("is-listening");
    mic.setAttribute("aria-label", "Listening. Tap to stop.");
    toast("Listening… ask your question.");
    rec.onresult = (e) => {
      const text = e.results && e.results[0] && e.results[0][0] ? e.results[0][0].transcript : "";
      if (text) ask(text);
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") toast("Microphone access is blocked. Type your question instead.");
    };
    rec.onend = () => {
      state.dictation = null;
      mic.classList.remove("is-listening");
      mic.setAttribute("aria-label", "Ask out loud");
    };
    try { rec.start(); } catch (_) { rec.onend(); }
  }

  // ---------------------------------------------------------------- wiring

  function onClick(e) {
    const tab = e.target.closest("[data-tab]");
    if (tab) { setTab(tab.dataset.tab); return; }
    const el = e.target.closest("[data-act]");
    if (!el) return;
    switch (el.dataset.act) {
      case "film": openFilm(); break;
      case "account": openSheet("account"); break;
      case "signup": openAuth("register"); break;
      case "close": closeSheet(); break;
      case "coach": openSheet("coach"); break;
      case "mic": micPressed(); break;
      case "call": micPressed(); break;
      case "moment": playMoment(); break;
      case "practiced": markPracticed(); break;
      case "upgrade": if (window.cerebelBilling) window.cerebelBilling.openPaywall(null); break;
      case "manage": if (window.cerebelBilling) window.cerebelBilling.portal(el); break;
      case "signout": signOut(); break;
      case "delete-account": deleteAccount(); break;
      default: break;
    }
  }

  function bindTrack() {
    const t = $("track");
    let dragging = false;
    t.addEventListener("pointerdown", (e) => { dragging = true; t.setPointerCapture(e.pointerId); seekFromPointer(e); });
    t.addEventListener("pointermove", (e) => { if (dragging) seekFromPointer(e); });
    t.addEventListener("pointerup", () => { dragging = false; });
    t.addEventListener("pointercancel", () => { dragging = false; });
    t.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const v = video();
      v.pause();
      try { v.currentTime = Math.max(0, Math.min(duration(), v.currentTime + (e.key === "ArrowRight" ? 0.1 : -0.1))); } catch (_) {}
    });
  }

  function init() {
    document.addEventListener("click", onClick);
    $("backdrop").addEventListener("click", closeSheet);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.sheet) closeSheet(); });
    $("composer").addEventListener("submit", (e) => { e.preventDefault(); ask($("q").value); });
    $("authForm").addEventListener("submit", submitAuth);
    $("authNew").addEventListener("click", () => setAuthMode("register"));
    $("authOld").addEventListener("click", () => setAuthMode("login"));
    $("fileCamera").addEventListener("change", (e) => handleFile(e.target));
    $("fileLibrary").addEventListener("change", (e) => handleFile(e.target));
    const v = video();
    v.addEventListener("play", paintPlayIcon);
    v.addEventListener("pause", paintPlayIcon);
    $("playBtn").addEventListener("click", () => {
      if (v.paused) {
        state.moment = null;
        v.loop = true;
        v.playbackRate = state.speed;
        const p = v.play();
        if (p && p.catch) p.catch(() => {});
      } else {
        v.pause();
      }
    });
    $("speedBtn").addEventListener("click", () => {
      state.speed = SPEEDS[(SPEEDS.indexOf(state.speed) + 1) % SPEEDS.length];
      if (!state.moment) v.playbackRate = state.speed;
      paintSpeed();
    });
    $("linesBtn").addEventListener("click", toggleLines);
    window.addEventListener("cerebel-voice", onVoice);
    bindTrack();
    paintSpeed();
    setAuthMode("register");
    window.addEventListener("hashchange", route);
    init3d();
    window.requestAnimationFrame(tick);
    route();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  return {
    toast,
    go,
    refreshLibrary: async () => {
      await refreshLibraryData();
      if (state.view === "vHome") paintHome();
    },
    openAccountPanel,
    openSheet,
    selectFix,
    addBubble,
    // Pure helpers, exposed for the tests.
    _test: { drillParts, formatValue, seriesValue, stripAnalysis, whenLabel, parseRoute, voiceMode },
  };
})();
