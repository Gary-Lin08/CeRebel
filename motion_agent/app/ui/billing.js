/* Plans, paywall, checkout and product events for the consumer beta.
   Loaded after the main inline script; uses its globals (authToken, authHeaders,
   refreshLibrary, openAccountPanel, showToast) only at call time. */
(function () {
  "use strict";

  var VISITOR_KEY = "cerebel_visitor_v1";
  var visitor = "";
  try {
    visitor = localStorage.getItem(VISITOR_KEY) || "";
    if (!visitor) {
      visitor = Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(VISITOR_KEY, visitor);
    }
  } catch (_) {}

  function headers(extra) {
    try { return authHeaders(extra); } catch (_) { return Object.assign({}, extra || {}); }
  }
  function toast(msg) {
    try { showToast(msg); } catch (_) { window.alert(msg); }
  }
  function signedIn() {
    try { return !!authToken; } catch (_) { return false; }
  }

  function track(event, props) {
    try {
      fetch("/api/events", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ event: event, props: props || {}, visitor: visitor }),
        keepalive: true,
      }).catch(function () {});
    } catch (_) {}
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "onclick") node.addEventListener("click", attrs[k]);
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  var lastBilling = null;

  async function fetchBilling() {
    var r = await fetch("/api/billing", { headers: headers() });
    var data = await r.json();
    lastBilling = data.billing || null;
    return data;
  }

  async function checkout(plan, button) {
    if (!signedIn()) {
      closePaywall();
      try { openAccountPanel(false); } catch (_) {}
      toast("Create an account first, then upgrade.");
      return;
    }
    track("checkout_click", { plan: plan });
    if (button) { button.disabled = true; button.textContent = "Opening checkout…"; }
    try {
      var r = await fetch("/api/billing/checkout", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ plan: plan }),
      });
      var data = await r.json();
      if (!r.ok || !data.url) throw new Error(data.message || "Checkout is not available right now.");
      window.location.href = data.url;
    } catch (err) {
      toast(err.message || "Checkout failed.");
      if (button) { button.disabled = false; button.textContent = "Choose"; }
    }
  }

  async function portal(button) {
    if (button) button.disabled = true;
    try {
      var r = await fetch("/api/billing/portal", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: "{}",
      });
      var data = await r.json();
      if (!r.ok || !data.url) throw new Error(data.message || "Could not open subscription settings.");
      window.location.href = data.url;
    } catch (err) {
      toast(err.message);
      if (button) button.disabled = false;
    }
  }

  function closePaywall() {
    var m = document.getElementById("cbPaywall");
    if (m) m.remove();
  }

  async function openPaywall(reason) {
    closePaywall();
    var billing = lastBilling;
    if (!billing) {
      try { billing = (await fetchBilling()).billing; } catch (_) { billing = null; }
    }
    track("paywall_view", { reason: reason && reason.quota ? "quota" : "button" });
    var plans = (billing && billing.plans) || [];
    var list = el("div", { class: "cb-plans" });
    if (!billing || !billing.checkout_enabled || !plans.length) {
      list.appendChild(el("p", { class: "cb-muted", text: "Pro is opening soon. We'll email you when upgrades are live." }));
    }
    plans.forEach(function (p) {
      var btn = el("button", { type: "button", class: p.id === "founding" ? "primary" : "", text: "Choose" });
      btn.addEventListener("click", function () { checkout(p.id, btn); });
      list.appendChild(
        el("div", { class: "cb-plan" + (p.id === "founding" ? " cb-plan--hi" : "") }, [
          el("div", {}, [
            el("strong", { text: p.label }),
            el("div", { class: "cb-price", text: p.price }),
            el("div", { class: "cb-muted", text: p.detail || "" }),
          ]),
          btn,
        ])
      );
    });
    var free = (billing && billing.free_monthly) || 3;
    var pro = (billing && billing.pro_monthly) || 60;
    var questions = (billing && billing.pro_daily_questions) || 100;
    var modal = el("div", { id: "cbPaywall", class: "cb-modal", role: "dialog", "aria-modal": "true", "aria-label": "Upgrade to Pro" }, [
      el("div", { class: "cb-card" }, [
        el("button", { type: "button", class: "cb-close", "aria-label": "Close", text: "×", onclick: closePaywall }),
        el("h3", { text: (reason && reason.title) || (reason && reason.message ? "You're out of free analyses" : "CeRebel Pro") }),
        el("p", { class: "cb-muted", text: (reason && reason.message) || "" }),
        el("ul", { class: "cb-features" }, [
          el("li", { text: "Up to " + pro + " analyses a month (free plan: " + free + ")" }),
          el("li", { text: "Track the same metrics across every swing" }),
          el("li", { text: "Up to " + questions + " questions a day to the coach about your swings" }),
          el("li", { text: "Less than the price of one lesson. Cancel anytime." }),
        ]),
        list,
        el("p", { class: "cb-fine", text: "Secure checkout by Stripe. Beta: numbers are estimates from one phone video." }),
      ]),
    ]);
    modal.addEventListener("click", function (e) { if (e.target === modal) closePaywall(); });
    document.body.appendChild(modal);
  }

  function paint(data) {
    if (data && data.video_consent) setConsent(data.video_consent);
    var box = document.getElementById("libraryBox");
    if (!box || !data) return;
    wireFilmingTips();
    if (data.billing) lastBilling = data.billing;
    var host = document.getElementById("cbPlanBox");
    if (!host) {
      host = el("div", { id: "cbPlanBox", class: "cb-planbox" });
      box.insertBefore(host, box.firstChild);
    }
    host.innerHTML = "";
    var q = data.quota || {};
    var sub = data.subscription || {};
    var label = sub.pro ? "Pro" : "Free";
    var line = (q.remaining != null)
      ? q.remaining + " of " + q.monthly_limit + " analyses left this month"
      : "";
    host.appendChild(el("span", { class: "cb-badge" + (sub.pro ? " cb-badge--pro" : ""), text: label }));
    host.appendChild(el("span", { class: "cb-muted", text: line }));
    if (sub.pro && sub.cancel_at_period_end) {
      host.appendChild(el("span", { class: "cb-muted", text: "· ends at period end" }));
    }
    if (sub.can_manage) {
      var mb = el("button", { type: "button", text: "Manage subscription" });
      mb.addEventListener("click", function () { portal(mb); });
      host.appendChild(mb);
    }
    if (!sub.pro) {
      host.appendChild(el("button", { type: "button", class: "primary", text: "Upgrade", onclick: function () { openPaywall(null); } }));
    }
  }

  function wireTrust() {
    var hero = document.getElementById("fixHero");
    if (!hero || document.getElementById("cbTrust")) return;
    var main = hero.querySelector(".fix-hero__main") || hero;
    var row = el("div", { id: "cbTrust", class: "cb-trust" }, [
      el("span", { class: "cb-muted", text: "Does this match what you feel, or what your coach says?" }),
    ]);
    function answer(ok) {
      var sid = null;
      try { sid = activeSessionId; } catch (_) {}
      track(ok ? "trust_yes" : "trust_no", { session_id: sid || "" });
      row.innerHTML = "";
      row.appendChild(el("span", { class: "cb-muted", text: ok ? "Thanks — noted." : "Thanks. That helps us improve the analysis." }));
    }
    row.appendChild(el("button", { type: "button", text: "Yes", onclick: function () { answer(true); } }));
    row.appendChild(el("button", { type: "button", text: "Not really", onclick: function () { answer(false); } }));
    main.appendChild(row);
    var seen = false;
    new MutationObserver(function () {
      if (!hero.hidden && !seen) { seen = true; track("report_view", {}); }
    }).observe(hero, { attributes: true, attributeFilter: ["hidden"] });
  }

  function handleReturn() {
    var params = new URLSearchParams(window.location.search);
    var state = params.get("checkout");
    if (!state) return;
    params.delete("checkout");
    var clean = window.location.pathname + (params.toString() ? "?" + params.toString() : "") + window.location.hash;
    window.history.replaceState(null, "", clean);
    if (state === "success") {
      toast("Welcome to Pro! It can take a few seconds to show up.");
      var tries = 0;
      var timer = window.setInterval(function () {
        tries += 1;
        try { refreshLibrary(); } catch (_) {}
        if (tries >= 5) window.clearInterval(timer);
      }, 2500);
    } else if (state === "cancel") {
      toast("Checkout cancelled. You're still on the free plan.");
    }
  }

  window.cerebelBilling = { track: track, paint: paint, openPaywall: openPaywall, checkout: checkout, portal: portal };

  /* Separate, explicit consent to video processing before the first upload.
     The text comes from the server (/api/me -> video_consent) so the page and the record match. */
  var consentState = null;
  var consentToken = null;
  var consentPending = null;

  function currentToken() {
    try { return authToken || ""; } catch (_) { return ""; }
  }
  function setConsent(c) {
    if (!c) return;
    consentState = c;
    consentToken = currentToken();
  }
  async function fetchConsent() {
    try {
      var r = await fetch("/api/me", { headers: headers() });
      var d = await r.json();
      if (d && d.video_consent) setConsent(d.video_consent);
    } catch (_) {}
    return consentState;
  }
  function showConsent(c) {
    return new Promise(function (resolve) {
      var box = el("input", { type: "checkbox", id: "cbVideoConsentBox" });
      var agree = el("button", { type: "button", class: "primary", text: "Agree and continue" });
      var cancel = el("button", { type: "button", text: "Not now" });
      var msg = el("p", { class: "cb-muted", role: "status" });
      var textP = el("p", { text: c.text || "" });
      var procs = el("ul", { class: "cb-features" });
      (c.processors || []).forEach(function (p) {
        procs.appendChild(el("li", { text: p.name + " (" + p.country + "): " + p.role }));
      });
      agree.disabled = true;
      box.addEventListener("change", function () { agree.disabled = !box.checked; });
      var modal = el("div", { id: "cbVideoConsent", class: "cb-modal", role: "dialog", "aria-modal": "true", "aria-label": "Consent to video processing" }, [
        el("div", { class: "cb-card" }, [
          el("h3", { text: "Before your first upload" }),
          textP,
          el("p", { class: "cb-muted", text: "Who processes your video and data:" }),
          procs,
          el("label", { class: "cb-consent", for: "cbVideoConsentBox" }, [
            box,
            el("span", { text: "I have read this and agree to my videos being processed this way." }),
          ]),
          el("p", { class: "cb-fine" }, [
            document.createTextNode("Details in the "),
            el("a", { href: c.privacy_url || "/privacy", target: "_blank", text: "Privacy Policy" }),
            document.createTextNode(". You can delete any video, or your whole account, at any time."),
          ]),
          msg,
          el("div", { class: "account-panel__actions" }, [agree, cancel]),
        ]),
      ]);
      function close(ok) {
        modal.remove();
        resolve(ok);
      }
      cancel.addEventListener("click", function () { close(false); });
      agree.addEventListener("click", async function () {
        if (!box.checked) return;
        agree.disabled = true;
        msg.textContent = "Saving…";
        try {
          var r = await fetch("/api/account/video-consent", {
            method: "POST",
            headers: headers({ "Content-Type": "application/json" }),
            body: JSON.stringify({ accept: true, version: c.version }),
          });
          var d = await r.json();
          if (d && d.video_consent) setConsent(d.video_consent);
          if (!r.ok) {
            if (r.status === 409 && d.video_consent) {
              c = d.video_consent;
              textP.textContent = c.text || "";
              box.checked = false;
            }
            msg.textContent = (d && d.message) || "Could not save your consent. Try again.";
            agree.disabled = !box.checked;
            return;
          }
          close(true);
        } catch (_) {
          msg.textContent = "Could not save your consent. Check your connection and try again.";
          agree.disabled = false;
        }
      });
      document.body.appendChild(modal);
    });
  }
  async function ensureConsent() {
    if (!signedIn()) return false;
    if (consentPending) return consentPending;
    var c = consentToken === currentToken() ? consentState : null;
    if (!c) c = await fetchConsent();
    if (c && !c.required) return true;
    if (!c) {
      toast("Could not load the video consent. Reload the page and try again.");
      return false;
    }
    consentPending = showConsent(c).then(function (ok) { consentPending = null; return ok; });
    return consentPending;
  }
  window.cerebelConsent = {
    ensure: ensureConsent,
    reset: function (c) { consentState = null; consentToken = null; if (c) setConsent(c); },
  };

  function wireFilmingTips() {
    var box = document.getElementById("libraryBox");
    if (!box || document.getElementById("cbTips")) return;
    var label = box.querySelector(".upload-label");
    var tips = el("details", { id: "cbTips", class: "cb-tips" }, [
      el("summary", { text: "How to film for the best analysis" }),
      el("ul", {}, [
        el("li", { text: "Side-on (down the line) or face-on, the same view every time." }),
        el("li", { text: "Whole body and the club in frame, from address to finish." }),
        el("li", { text: "Phone still, on a stand or propped up, about hip height, 8–10 ft away." }),
        el("li", { text: "Slow-motion (120 or 240 fps) if your phone has it; good light." }),
        el("li", { text: "One swing per clip, under 30 seconds. Only you in the shot." }),
      ]),
    ]);
    box.insertBefore(tips, label || box.firstChild);
  }

  function wireHero() {
    var row = document.querySelector("body.pg-consumer .pg-brand-row");
    if (!row || document.getElementById("cbHero")) return;
    var free = 3;
    var hero = el("div", { id: "cbHero" }, [
      el("p", { class: "cb-hero", text: "See your swing as a full 3D body, and get the one fix that matters." }),
      el("p", {
        class: "cb-hero-sub",
        text: "Film one swing on your phone. CeRebel rebuilds your skeleton, measures every joint, and shows the numbers behind every tip. Free for " + free + " swings a month.",
      }),
    ]);
    row.parentNode.insertBefore(hero, row.nextSibling);
  }

  function openDeepLink() {
    var sid = new URLSearchParams(window.location.search).get("session");
    if (!sid || !/^[A-Za-z0-9-]{8,64}$/.test(sid) || !signedIn()) return;
    try {
      activeSessionId = sid;
      loadSession({ announce: true });
    } catch (_) {}
  }

  function init() {
    wireHero();
    window.setTimeout(openDeepLink, 600);
    // The app page (/app) is not the landing; it reports its own report and sample views.
    if (!document.body.classList.contains("cx")) track("landing_view", { signed_in: signedIn() });
    wireTrust();
    handleReturn();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
