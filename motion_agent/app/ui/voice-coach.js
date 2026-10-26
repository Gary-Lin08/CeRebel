/* Live voice coach: a continuous call with the coach, spoken replies, and barge-in.
   Loaded after the main inline script; uses its globals (authToken, session, addBubble,
   escapeHtml, showConsumerChat, showToast, openAccountPanel, cerebelBilling) only at call time.
   Protocol: motion_agent/app/voice.py. */
(function () {
  "use strict";

  var WS_PATH = "/api/voice/live";
  var CONSENT_KEY = "cerebel_voice_consent_v1";
  var IN_RATE = 16000;
  var OUT_RATE = 24000;
  var FRAME = 800; // 50 ms of 16 kHz audio
  var PREROLL_FRAMES = 6; // audio kept from just before a barge-in, so the first word is not lost
  var BARGE_FRAMES = 4; // 200 ms of speech over the coach stops the coach

  var status = null;
  var call = null;

  var WORKLET =
    "class CbCapture extends AudioWorkletProcessor{constructor(){super();this.step=sampleRate/" + IN_RATE +
    ";this.pos=0;this.sum=0;this.cnt=0;this.out=new Int16Array(" + FRAME + ");this.n=0}" +
    "process(inputs){const ch=inputs[0]&&inputs[0][0];if(!ch)return true;for(let i=0;i<ch.length;i++){" +
    "this.sum+=ch[i];this.cnt++;this.pos+=1;if(this.pos>=this.step){this.pos-=this.step;let s=this.sum/this.cnt;" +
    "this.sum=0;this.cnt=0;s=s<-1?-1:s>1?1:s;this.out[this.n++]=s<0?s*32768:s*32767;if(this.n===this.out.length){" +
    "this.port.postMessage(this.out.buffer,[this.out.buffer]);this.out=new Int16Array(" + FRAME + ");this.n=0}}}return true}}" +
    "registerProcessor('cb-capture',CbCapture);";

  function token() { try { return authToken || ""; } catch (_) { return ""; } }
  function currentSession() { try { return session; } catch (_) { return null; } }
  function esc(s) { try { return escapeHtml(s); } catch (_) { return String(s || "").replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); } }
  function toast(msg) { try { showToast(msg); } catch (_) { window.alert(msg); } }
  function track(name, props) { try { window.cerebelBilling.track(name, props || {}); } catch (_) {} }
  function bubble(who, html, cls) { try { return addBubble(who, html, cls, { interim: false }); } catch (_) { return null; } }
  function backToTyping() { try { showConsumerChat(); } catch (_) {} }

  function wsUrl() {
    if (/^https?:/i.test(WS_PATH)) return WS_PATH.replace(/^http/i, "ws");
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + WS_PATH;
  }

  async function refreshStatus() {
    try {
      var headers = {};
      if (token()) headers.Authorization = "Bearer " + token();
      var r = await fetch("/api/voice/status", { headers: headers });
      status = await r.json();
    } catch (_) {
      status = { enabled: false };
    }
    paintButtons();
    return status;
  }

  function paintButtons() {
    var on = !!(status && status.enabled);
    document.querySelectorAll("[data-voice-live]").forEach(function (btn) {
      btn.hidden = !on;
      btn.classList.toggle("is-live", !!call);
      btn.setAttribute("aria-pressed", call ? "true" : "false");
      var hint = status && status.kind === "trial" && !call ? " · free try" : "";
      btn.textContent = call ? "End call" : btn.getAttribute("data-label") + hint;
    });
    // The live call replaces the old hold-to-talk button where it is available.
    var hold = document.getElementById("fixVoice");
    if (hold && on) hold.hidden = true;
  }

  function addButtons() {
    var send = document.getElementById("send");
    if (send && !document.getElementById("voiceLive")) {
      var b = document.createElement("button");
      b.type = "button";
      b.id = "voiceLive";
      b.className = "vc-btn";
      b.setAttribute("data-voice-live", "");
      b.setAttribute("data-label", "Talk live");
      b.hidden = true;
      send.insertAdjacentElement("afterend", b);
    }
    var hold = document.getElementById("fixVoice");
    if (hold && !document.getElementById("voiceLiveHero")) {
      var h = document.createElement("button");
      h.type = "button";
      h.id = "voiceLiveHero";
      h.className = "vc-btn";
      h.setAttribute("data-voice-live", "");
      h.setAttribute("data-label", "Talk to coach");
      h.hidden = true;
      hold.insertAdjacentElement("afterend", h);
    }
    document.querySelectorAll("[data-voice-live]").forEach(function (btn) {
      btn.addEventListener("click", function () { call ? hangUp() : begin(); });
    });
  }

  // ------------------------------------------------------------------ start

  async function begin() {
    var s = currentSession();
    if (s && (s.empty || s.processing || s.no_swing)) {
      toast(s.no_swing ? "Upload a clearer swing before calling the coach." : "Upload a swing before calling the coach.");
      return;
    }
    await refreshStatus();
    if (!status || !status.enabled) return;
    if (status.need === "signin") {
      toast("Create a free account to try live coaching.");
      try { openAccountPanel(); } catch (_) {}
      return;
    }
    if (status.need === "upgrade") {
      track("voice_paywall");
      try { window.cerebelBilling.openPaywall({ voice: true }); } catch (_) { toast("Live coaching is part of Pro."); }
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.WebSocket) {
      toast("This browser can't use the microphone here. Type your question instead.");
      backToTyping();
      return;
    }
    var agreed = false;
    try { agreed = localStorage.getItem(CONSENT_KEY) === "1"; } catch (_) {}
    if (!agreed) {
      showConsent();
      return;
    }
    startCall();
  }

  function showConsent() {
    var wrap = document.createElement("div");
    wrap.className = "vc-consent";
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-modal", "true");
    wrap.setAttribute("aria-label", "Live coach");
    var minutes = status && status.kind === "trial" ? Math.round((status.seconds || 0) / 60) : 0;
    wrap.innerHTML =
      '<div class="vc-consent__card">' +
      "<h3>Talk with your coach</h3>" +
      "<p>Live coach listens through your microphone only while the call is on. Your voice is sent to our speech service to turn it into text, and the coach's reply is turned into speech. We don't record or keep the audio.</p>" +
      (minutes ? "<p class=\"vc-muted\">Your free try lasts about " + minutes + " minutes. After that, live coaching is part of Pro.</p>" : "") +
      '<p class="vc-muted">Headphones work best. You can talk over the coach to interrupt.</p>' +
      '<div class="vc-consent__actions"><button type="button" class="vc-ghost" data-act="no">Not now</button><button type="button" class="vc-primary" data-act="yes">Start call</button></div>' +
      "</div>";
    document.body.appendChild(wrap);
    wrap.addEventListener("click", function (e) {
      var act = e.target && e.target.getAttribute("data-act");
      if (!act && e.target !== wrap) return;
      wrap.remove();
      if (act === "yes") {
        try { localStorage.setItem(CONSENT_KEY, "1"); } catch (_) {}
        track("voice_consent", { version: CONSENT_KEY });
        startCall();
      }
    });
  }

  async function startCall() {
    var c = {
      ws: null, ctx: null, stream: null, node: null, sink: null,
      sources: [], playEnd: 0, ignoreTurn: 0, turn: 0, waiting: false,
      muted: false, floor: 0.01, loud: 0, preroll: [], ready: false,
      seconds: 0, startedAt: 0, ended: false, sheet: null, timer: null,
    };
    call = c;
    paintButtons();
    buildSheet(c);
    setState(c, "connecting", "Connecting…");
    try {
      // Created inside the click so iOS lets it play.
      var Ctx = window.AudioContext || window.webkitAudioContext;
      c.ctx = new Ctx();
      await c.ctx.resume();
      c.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (err) {
      finish(c, err && err.name === "NotAllowedError"
        ? "Microphone access is blocked. Allow it in your browser settings, or type your question instead."
        : "Couldn't start the microphone. Type your question instead.");
      return;
    }
    if (c.ended) return;
    try {
      await startCapture(c);
    } catch (_) {
      finish(c, "This browser can't stream the microphone. Type your question instead.");
      return;
    }
    openSocket(c);
  }

  async function startCapture(c) {
    var source = c.ctx.createMediaStreamSource(c.stream);
    c.sink = c.ctx.createGain();
    c.sink.gain.value = 0; // keeps the capture node running without playing the mic back
    c.sink.connect(c.ctx.destination);
    if (c.ctx.audioWorklet && window.AudioWorkletNode) {
      var url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
      await c.ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      c.node = new AudioWorkletNode(c.ctx, "cb-capture");
      c.node.port.onmessage = function (e) { onFrame(c, new Int16Array(e.data)); };
    } else {
      // Older Safari: ScriptProcessor with the same averaging resampler.
      var step = c.ctx.sampleRate / IN_RATE, pos = 0, sum = 0, cnt = 0, out = new Int16Array(FRAME), n = 0;
      c.node = c.ctx.createScriptProcessor(4096, 1, 1);
      c.node.onaudioprocess = function (e) {
        var ch = e.inputBuffer.getChannelData(0);
        for (var i = 0; i < ch.length; i++) {
          sum += ch[i]; cnt++; pos += 1;
          if (pos >= step) {
            pos -= step;
            var s = Math.max(-1, Math.min(1, sum / cnt)); sum = 0; cnt = 0;
            out[n++] = s < 0 ? s * 32768 : s * 32767;
            if (n === FRAME) { onFrame(c, out); out = new Int16Array(FRAME); n = 0; }
          }
        }
      };
    }
    source.connect(c.node);
    c.node.connect(c.sink);
  }

  function openSocket(c) {
    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (_) {
      finish(c, "Live coaching isn't available right now. You can keep typing your questions.");
      return;
    }
    c.ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = function () {
      var s = currentSession();
      ws.send(JSON.stringify({ type: "start", token: token(), video_id: (s && s.session_id) || null }));
    };
    ws.onmessage = function (e) {
      if (typeof e.data === "string") {
        var msg = null;
        try { msg = JSON.parse(e.data); } catch (_) { return; }
        onMessage(c, msg);
      } else {
        onAudio(c, e.data);
      }
    };
    ws.onclose = function () {
      if (!c.ended) finish(c, c.ready ? "The call dropped. You can keep typing your questions." : "Live coaching isn't available right now. You can keep typing your questions.");
    };
  }

  // ------------------------------------------------------------------ audio in

  function rms(pcm) {
    var sum = 0;
    for (var i = 0; i < pcm.length; i++) { var v = pcm[i] / 32768; sum += v * v; }
    return Math.sqrt(sum / (pcm.length || 1));
  }

  function coachSpeaking(c) {
    return !!c.ctx && c.ctx.currentTime < c.playEnd - 0.02;
  }

  function send(c, pcm) {
    if (c.ws && c.ws.readyState === 1 && c.ready) c.ws.send(pcm.buffer.byteLength === pcm.length * 2 ? pcm.buffer : pcm.slice().buffer);
  }

  function onFrame(c, pcm) {
    if (c.ended) return;
    var level = c.muted ? 0 : rms(pcm);
    // Slow-moving noise floor: falls fast, rises slowly.
    c.floor = level < c.floor ? c.floor * 0.8 + level * 0.2 : c.floor * 0.995 + level * 0.005;
    if (c.muted) { send(c, new Int16Array(pcm.length)); return; }
    if (!coachSpeaking(c)) {
      c.loud = 0;
      c.preroll = [];
      send(c, pcm);
      return;
    }
    // While the coach talks, send silence unless the user clearly talks over it (echo guard).
    c.preroll.push(pcm);
    if (c.preroll.length > PREROLL_FRAMES) c.preroll.shift();
    c.loud = level > Math.max(c.floor * 4, 0.03) ? c.loud + 1 : 0;
    if (c.loud >= BARGE_FRAMES) {
      bargeIn(c);
      c.preroll.forEach(function (f) { send(c, f); });
      c.preroll = [];
      return;
    }
    send(c, new Int16Array(pcm.length));
  }

  function bargeIn(c) {
    stopPlayback(c);
    c.ignoreTurn = c.turn;
    c.loud = 0;
    if (c.ws && c.ws.readyState === 1) c.ws.send(JSON.stringify({ type: "interrupt" }));
    setState(c, "listening", "Listening…");
  }

  // ------------------------------------------------------------------ audio out

  function onAudio(c, buf) {
    if (!c.ctx || buf.byteLength < 6) return;
    var turn = new DataView(buf).getUint32(0, true);
    if (turn <= c.ignoreTurn) return;
    var count = Math.floor((buf.byteLength - 4) / 2);
    var pcm = new Int16Array(buf, 4, count);
    var audio = c.ctx.createBuffer(1, count, OUT_RATE);
    var ch = audio.getChannelData(0);
    for (var i = 0; i < count; i++) ch[i] = pcm[i] / 32768;
    var src = c.ctx.createBufferSource();
    src.buffer = audio;
    src.connect(c.ctx.destination);
    var at = Math.max(c.ctx.currentTime + 0.04, c.playEnd);
    src.start(at);
    c.playEnd = at + audio.duration;
    c.sources.push(src);
    src.onended = function () {
      var k = c.sources.indexOf(src);
      if (k >= 0) c.sources.splice(k, 1);
    };
  }

  function stopPlayback(c) {
    c.sources.forEach(function (s) { try { s.stop(); } catch (_) {} });
    c.sources = [];
    c.playEnd = 0;
  }

  // ------------------------------------------------------------------ messages

  function onMessage(c, msg) {
    switch (msg.type) {
      case "ready":
        c.ready = true;
        c.seconds = msg.seconds || 0;
        c.startedAt = Date.now();
        setState(c, "listening", "Listening…");
        caption(c, "Ask anything about your swing.", true);
        c.timer = window.setInterval(function () { tick(c); }, 200);
        break;
      case "partial":
        caption(c, msg.text, true);
        break;
      case "user":
        c.turn = msg.turn;
        c.waiting = true;
        caption(c, msg.text, false);
        numbers(c, []);
        bubble("You", esc(msg.text), "user");
        break;
      case "coach":
        caption(c, msg.say, false);
        numbers(c, msg.screen || []);
        bubble("Coach", coachHtml(msg), "agent");
        break;
      case "audio_end":
        if (msg.turn === c.turn) c.waiting = false;
        break;
      case "end":
        finish(c, msg.message || "", msg.reason);
        break;
    }
  }

  function coachHtml(msg) {
    var html = "<p>" + esc(msg.say || "") + "</p>";
    var lines = msg.screen || [];
    if (lines.length) {
      html += '<div class="vc-numbers"><span>From your swing</span><ul>' +
        lines.map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("") + "</ul></div>";
    }
    return html;
  }

  // ------------------------------------------------------------------ sheet

  function buildSheet(c) {
    var sheet = document.createElement("div");
    sheet.className = "vc-sheet";
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-label", "Live coach");
    sheet.innerHTML =
      '<button type="button" class="vc-orb" aria-label="Tap to interrupt"><span></span></button>' +
      '<div class="vc-body"><p class="vc-state" aria-live="polite"></p><p class="vc-caption"></p>' +
      '<ul class="vc-sheet-numbers" aria-label="From your swing" hidden></ul></div>' +
      '<div class="vc-actions"><span class="vc-timer"></span>' +
      '<button type="button" class="vc-ghost" data-act="mute" aria-pressed="false">Mute</button>' +
      '<button type="button" class="vc-end" data-act="end">End</button></div>';
    document.body.appendChild(sheet);
    // Show the chat with the transcript, without focusing the text box (that would raise the
    // keyboard on phones).
    document.body.classList.add("vc-in-call", "pg-consumer-chat-open");
    c.sheet = sheet;
    sheet.querySelector(".vc-orb").addEventListener("click", function () {
      if (coachSpeaking(c)) bargeIn(c);
    });
    sheet.querySelector('[data-act="end"]').addEventListener("click", hangUp);
    var mute = sheet.querySelector('[data-act="mute"]');
    mute.addEventListener("click", function () {
      c.muted = !c.muted;
      mute.setAttribute("aria-pressed", c.muted ? "true" : "false");
      mute.textContent = c.muted ? "Unmute" : "Mute";
    });
  }

  function setState(c, name, label) {
    if (!c.sheet) return;
    c.sheet.setAttribute("data-state", name);
    var el = c.sheet.querySelector(".vc-state");
    if (el && el.textContent !== label) el.textContent = label;
  }

  function caption(c, text, live) {
    if (!c.sheet) return;
    var el = c.sheet.querySelector(".vc-caption");
    el.textContent = text || "";
    el.classList.toggle("is-live", !!live);
  }

  function numbers(c, lines) {
    if (!c.sheet) return;
    var ul = c.sheet.querySelector(".vc-sheet-numbers");
    ul.innerHTML = lines.slice(0, 2).map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("");
    ul.hidden = !lines.length;
  }

  function tick(c) {
    if (c.ended || !c.ready) return;
    if (coachSpeaking(c)) setState(c, "speaking", "Speaking · talk or tap to interrupt");
    else if (c.waiting) setState(c, "thinking", "Thinking…");
    else setState(c, c.muted ? "muted" : "listening", c.muted ? "Muted" : "Listening…");
    var left = Math.max(0, c.seconds - Math.floor((Date.now() - c.startedAt) / 1000));
    var t = c.sheet && c.sheet.querySelector(".vc-timer");
    if (t) t.textContent = Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0") + " left";
  }

  // ------------------------------------------------------------------ end

  function hangUp() {
    var c = call;
    if (!c) return;
    try { if (c.ws && c.ws.readyState === 1) c.ws.send(JSON.stringify({ type: "hangup" })); } catch (_) {}
    finish(c, "");
  }

  function finish(c, message, reason) {
    if (c.ended) return;
    c.ended = true;
    if (c.timer) window.clearInterval(c.timer);
    stopPlayback(c);
    try { if (c.ws) c.ws.close(); } catch (_) {}
    try { if (c.stream) c.stream.getTracks().forEach(function (t) { t.stop(); }); } catch (_) {}
    try { if (c.node) c.node.disconnect(); } catch (_) {}
    try { if (c.ctx) c.ctx.close(); } catch (_) {}
    if (c.sheet) c.sheet.remove();
    document.body.classList.remove("vc-in-call");
    if (call === c) call = null;
    paintButtons();
    if (reason === "upgrade" || (reason === "time" && status && status.kind === "trial")) {
      try { window.cerebelBilling.openPaywall({ voice: true }); } catch (_) {}
    }
    if (reason === "signin") {
      try { openAccountPanel(); } catch (_) {}
    }
    if (message) {
      toast(message);
      backToTyping();
    }
    refreshStatus();
  }

  function init() {
    addButtons();
    refreshStatus();
    // Sign-in state changes the answer; re-check when the tab comes back.
    window.addEventListener("focus", function () { if (!call) refreshStatus(); });
  }

  window.cerebelVoice = { refresh: refreshStatus, hangUp: hangUp };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
