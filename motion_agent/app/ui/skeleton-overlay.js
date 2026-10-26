/* Skeleton lines over the player's own video, with the current fix marked in amber.
   Keypoints come from /api/swing_keypoints (the body estimate projected into the picture);
   how each fix is marked comes from finding.overlay (/api/sport_analysis). Loaded after the
   main inline script; uses authHeaders only at call time. The page calls onSession(session)
   and onNote(note, analysisPayload); compare.js calls attach(video).setStatic(...). */
(function () {
  "use strict";

  const BONE = "rgba(238, 240, 234, 0.82)";
  const FIX = "#F2A541";
  const SHADOW = "rgba(5, 12, 10, 0.75)";
  const STORE_KEY = "cerebel_skeleton_on_v1";
  const FADE_IN_S = 0.15;
  const FADE_OUT_S = 0.25;
  const COMPACT_WIDTH = 220;
  const cache = new Map();
  const overlays = new WeakMap();
  let mainOverlay = null;

  function headers() {
    try { return authHeaders(); } catch (_) { return {}; }
  }
  function enabled() {
    try { return localStorage.getItem(STORE_KEY) !== "0"; } catch (_) { return true; }
  }
  function remember(on) {
    try { localStorage.setItem(STORE_KEY, on ? "1" : "0"); } catch (_) {}
  }

  function loadKeypoints(sid) {
    if (!sid) return Promise.resolve(null);
    if (!cache.has(sid)) {
      const job = fetch("/api/swing_keypoints?session_id=" + encodeURIComponent(sid), { headers: headers() })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (!d || !d.available) return null;
          const index = {};
          d.names.forEach((n, i) => { index[n] = i; });
          return { fps: Number(d.fps) || 30, scale: Number(d.scale) || 10000, index, bones: d.bones, frames: d.frames, note: d.note };
        })
        .catch(() => { cache.delete(sid); return null; });
      cache.set(sid, job);
    }
    return cache.get(sid);
  }

  function seriesValue(series, t) {
    if (!series || !series.times || series.times.length < 2) return null;
    const ts = series.times;
    const vs = series.values;
    if (t <= ts[0]) return vs[0];
    if (t >= ts[ts.length - 1]) return vs[vs.length - 1];
    let i = 1;
    while (i < ts.length - 1 && ts[i] < t) i++;
    const f = (t - ts[i - 1]) / Math.max(ts[i] - ts[i - 1], 1e-6);
    return vs[i - 1] + f * (vs[i] - vs[i - 1]);
  }

  function windowAlpha(t, start, end) {
    if (start == null || end == null) return 1;
    if (t < start - FADE_IN_S || t > end + FADE_OUT_S) return 0;
    if (t < start) return 1 - (start - t) / FADE_IN_S;
    if (t > end) return 1 - (t - end) / FADE_OUT_S;
    return 1;
  }

  // ---------------------------------------------------------------- geometry helpers

  const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
  const add = (a, b) => [a[0] + b[0], a[1] + b[1]];
  const mul = (a, k) => [a[0] * k, a[1] * k];
  const len = (a) => Math.hypot(a[0], a[1]);
  const unit = (a) => { const l = len(a) || 1; return [a[0] / l, a[1] / l]; };
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];

  class Overlay {
    constructor(video, opts) {
      this.video = video;
      this.opts = Object.assign({ toggle: false, caption: false }, opts || {});
      this.kp = null;
      this.sid = null;
      this.note = null;
      this.series = {};
      this.staticLabel = null;
      this.bendDir = null;
      this.host = video.parentElement;
      // A host not in the page yet reports no position at all; either way the canvas needs a positioned parent.
      const position = getComputedStyle(this.host).position;
      if (!position || position === "static") this.host.style.position = "relative";
      this.canvas = document.createElement("canvas");
      this.canvas.className = "sk-canvas";
      this.canvas.setAttribute("aria-hidden", "true");
      this.host.appendChild(this.canvas);
      if (this.opts.toggle) {
        this.toggle = document.createElement("button");
        this.toggle.type = "button";
        this.toggle.className = "sk-toggle";
        this.toggle.textContent = "Skeleton";
        this.toggle.hidden = true;
        this.toggle.addEventListener("click", () => {
          remember(!enabled());
          this.sync();
        });
        this.host.appendChild(this.toggle);
      }
      if (this.opts.caption) {
        this.caption = document.createElement("p");
        this.caption.className = "sk-caption";
        this.caption.hidden = true;
        this.host.insertAdjacentElement("afterend", this.caption);
      }
      const redraw = () => this.draw(this.video.currentTime);
      ["seeked", "loadeddata", "pause", "timeupdate"].forEach((ev) => video.addEventListener(ev, redraw));
      if (window.ResizeObserver) new ResizeObserver(redraw).observe(this.host);
      this.loop();
    }

    loop() {
      const v = this.video;
      if (typeof v.requestVideoFrameCallback === "function") {
        const onFrame = (_now, meta) => {
          this.draw(meta && Number.isFinite(meta.mediaTime) ? meta.mediaTime : v.currentTime);
          v.requestVideoFrameCallback(onFrame);
        };
        v.requestVideoFrameCallback(onFrame);
      } else {
        const tick = () => {
          if (!v.paused) this.draw(v.currentTime);
          window.requestAnimationFrame(tick);
        };
        window.requestAnimationFrame(tick);
      }
    }

    setSession(sid) {
      if (sid === this.sid) return this;
      this.sid = sid;
      this.kp = null;
      this.bendDir = null;
      this.sync();
      loadKeypoints(sid).then((kp) => {
        if (this.sid !== sid) return;
        this.kp = kp;
        this.bendDir = null;
        this.sync();
      });
      return this;
    }

    setNote(note, payload) {
      this.note = note && note.overlay ? note : null;
      this.series = {};
      ((payload && payload.series) || []).forEach((s) => { this.series[s.id] = s; });
      this.bendDir = null;
      this.staticLabel = null;
      this.draw(this.video.currentTime);
      return this;
    }

    // Compare panes: no traces, so the number is the measured value for this swing.
    setStatic(highlight) {
      this.note = highlight && highlight.overlay ? highlight : null;
      this.series = {};
      this.staticLabel = (highlight && highlight.label) || null;
      this.bendDir = null;
      this.draw(this.video.currentTime);
      return this;
    }

    sync() {
      const on = enabled();
      if (this.toggle) {
        this.toggle.hidden = !this.kp;
        this.toggle.setAttribute("aria-pressed", on ? "true" : "false");
        this.toggle.classList.toggle("is-off", !on);
      }
      if (this.caption) {
        this.caption.hidden = !(this.kp && on);
        this.caption.textContent = (this.kp && this.kp.note) || "";
      }
      this.draw(this.video.currentTime);
    }

    frameAt(t) {
      const kp = this.kp;
      const n = kp.frames.length;
      const i = Math.max(0, Math.min(n - 1, Math.round(t * kp.fps)));
      for (let d = 0; d <= 2; d++) {
        if (kp.frames[i - d]) return kp.frames[i - d];
        if (kp.frames[i + d]) return kp.frames[i + d];
      }
      return null;
    }

    // Where the picture actually sits inside the canvas (object-fit contain or cover).
    contentRect(dpr) {
      const v = this.video;
      const hr = this.host.getBoundingClientRect();
      const vr = v.getBoundingClientRect();
      const bw = vr.width;
      const bh = vr.height;
      const iw = v.videoWidth || bw;
      const ih = v.videoHeight || bh;
      const fit = getComputedStyle(v).objectFit;
      const s = fit === "cover" ? Math.max(bw / iw, bh / ih) : Math.min(bw / iw, bh / ih);
      return {
        x: (vr.left - hr.left + (bw - iw * s) / 2) * dpr,
        y: (vr.top - hr.top + (bh - ih * s) / 2) * dpr,
        w: iw * s * dpr,
        h: ih * s * dpr,
      };
    }

    draw(t) {
      const c = this.canvas;
      const dpr = window.devicePixelRatio || 1;
      const w = Math.round(this.host.clientWidth * dpr);
      const h = Math.round(this.host.clientHeight * dpr);
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      const ctx = c.getContext("2d");
      ctx.clearRect(0, 0, w, h);
      if (!this.kp || !enabled() || !this.video.videoWidth) return;
      const raw = this.frameAt(t);
      if (!raw) return;
      const r = this.contentRect(dpr);
      this.bounds = r;
      const scale = this.kp.scale;
      const P = (name) => {
        const i = this.kp.index[name];
        if (i == null) return null;
        return [r.x + (raw[2 * i] / scale) * r.w, r.y + (raw[2 * i + 1] / scale) * r.h];
      };
      const lw = Math.max(1.5 * dpr, Math.min(4 * dpr, r.h * 0.0065));
      ctx.lineCap = "round";
      ctx.lineJoin = "round";

      // Plain skeleton.
      ctx.strokeStyle = BONE;
      ctx.lineWidth = lw;
      ctx.beginPath();
      this.kp.bones.forEach(([a, b]) => {
        const pa = P(a);
        const pb = P(b);
        if (!pa || !pb) return;
        ctx.moveTo(pa[0], pa[1]);
        ctx.lineTo(pb[0], pb[1]);
      });
      ctx.stroke();
      const ls = P("left_shoulder");
      const rs = P("right_shoulder");
      const nose = P("nose");
      if (nose && ls && rs) {
        const neck = mid(ls, rs);
        ctx.beginPath();
        ctx.moveTo(neck[0], neck[1]);
        ctx.lineTo(nose[0], nose[1]);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(nose[0], nose[1], Math.max(lw * 2.2, len(sub(ls, rs)) * 0.18), 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.fillStyle = BONE;
      Object.keys(this.kp.index).forEach((name) => {
        if (name === "nose") return;
        const p = P(name);
        if (!p) return;
        ctx.beginPath();
        ctx.arc(p[0], p[1], lw * 1.1, 0, Math.PI * 2);
        ctx.fill();
      });

      const note = this.note;
      if (!note) return;
      const alpha = windowAlpha(t, note.start_time, note.end_time);
      if (alpha <= 0) return;
      ctx.save();
      ctx.globalAlpha = alpha;
      this.drawFix(ctx, note.overlay, P, t, lw, dpr);
      ctx.restore();
    }

    // Labels stay inside the picture: shift them in rather than letting the frame cut them off.
    label(ctx, text, at, dpr, color, align, wording) {
      if (!text) return;
      const b = this.bounds;
      const cssWidth = b ? b.w / dpr : 400;
      // Small pictures (the side-by-side panes) keep the numbers and drop the sentences.
      if (wording && cssWidth < COMPACT_WIDTH) return;
      const size = Math.round(Math.max(10, Math.min(13, cssWidth / 16)) * dpr);
      ctx.font = `600 ${size}px Inter, -apple-system, "Helvetica Neue", Arial, sans-serif`;
      const width = ctx.measureText(text).width;
      let left = align === "right" ? at[0] - width : align === "center" ? at[0] - width / 2 : at[0];
      let y = at[1];
      if (b) {
        const pad = 6 * dpr;
        left = Math.max(b.x + pad, Math.min(left, b.x + b.w - width - pad));
        y = Math.max(b.y + size, Math.min(y, b.y + b.h - size));
      }
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.lineWidth = 3.5 * dpr;
      ctx.strokeStyle = SHADOW;
      ctx.strokeText(text, left, y);
      ctx.fillStyle = color || FIX;
      ctx.fillText(text, left, y);
    }

    arrow(ctx, from, to, lw) {
      const d = unit(sub(to, from));
      const head = Math.max(lw * 3.2, 7);
      const left = add(to, add(mul(d, -head), mul([-d[1], d[0]], head * 0.6)));
      const right = add(to, add(mul(d, -head), mul([d[1], -d[0]], head * 0.6)));
      ctx.beginPath();
      ctx.moveTo(from[0], from[1]);
      ctx.lineTo(to[0], to[1]);
      ctx.moveTo(left[0], left[1]);
      ctx.lineTo(to[0], to[1]);
      ctx.lineTo(right[0], right[1]);
      ctx.stroke();
    }

    readout(seriesId, t, absolute, unitText) {
      if (this.staticLabel) return this.staticLabel;
      const v = seriesValue(this.series[seriesId], t);
      if (v == null || !Number.isFinite(v)) return null;
      return Math.round(absolute ? Math.abs(v) : v) + (unitText || "°");
    }

    drawFix(ctx, spec, P, t, lw, dpr) {
      ctx.strokeStyle = FIX;
      ctx.fillStyle = FIX;
      ctx.lineWidth = lw * 1.9;
      ctx.beginPath();
      (spec.segments || []).forEach(([a, b]) => {
        const pa = P(a);
        const pb = P(b);
        if (!pa || !pb) return;
        ctx.moveTo(pa[0], pa[1]);
        ctx.lineTo(pb[0], pb[1]);
      });
      ctx.stroke();
      (spec.segments || []).flat().forEach((name) => {
        const p = P(name);
        if (!p) return;
        ctx.beginPath();
        ctx.arc(p[0], p[1], lw * 1.7, 0, Math.PI * 2);
        ctx.fill();
      });

      if (spec.angle) this.drawAngle(ctx, spec.angle, P, t, lw, dpr);
      if (spec.arrow) this.drawArrow(ctx, spec.arrow, P, lw, dpr);
      if (spec.lines) this.drawLines(ctx, spec, P, t, lw, dpr);
      if (spec.path) this.drawPath(ctx, spec.path, P, t, lw, dpr);
    }

    drawAngle(ctx, a, P, t, lw, dpr) {
      const j = P(a.at);
      const p1 = P(a.from);
      const p2 = P(a.to);
      if (!j || !p1 || !p2) return;
      const radius = 0.32 * Math.min(len(sub(p1, j)), len(sub(p2, j)));
      if (radius < 4) return;
      const a1 = Math.atan2(p1[1] - j[1], p1[0] - j[0]);
      const a2 = Math.atan2(p2[1] - j[1], p2[0] - j[0]);
      let delta = a2 - a1;
      while (delta > Math.PI) delta -= 2 * Math.PI;
      while (delta < -Math.PI) delta += 2 * Math.PI;
      ctx.lineWidth = lw * 1.1;
      ctx.beginPath();
      ctx.arc(j[0], j[1], radius, a1, a1 + delta, delta < 0);
      ctx.stroke();
      const text = this.readout(a.series, t, false, "°");
      // The number sits inside the joint's angle; arrows point outward, so the two never collide.
      const bis = a1 + delta / 2;
      this.label(ctx, text, [j[0] + Math.cos(bis) * radius * 1.8, j[1] + Math.sin(bis) * radius * 1.8], dpr, FIX, "center");
    }

    drawArrow(ctx, a, P, lw, dpr) {
      ctx.lineWidth = lw * 1.2;
      if (a.kind === "bend") {
        const k = P(a.at);
        const h = P(a.from);
        const an = P(a.to);
        if (!k || !h || !an) return;
        const axis = sub(an, h);
        const along = mul(unit(axis), (sub(k, h)[0] * unit(axis)[0] + sub(k, h)[1] * unit(axis)[1]));
        const off = sub(sub(k, h), along);
        // Keep the direction the knee bent at the start of the fix, even once it has straightened.
        if (!this.bendDir && len(off) > 2) this.bendDir = unit(off);
        if (!this.bendDir) return;
        const size = len(axis) * 0.28;
        const from = add(k, mul(this.bendDir, size * 0.25));
        const to = add(k, mul(this.bendDir, size));
        this.arrow(ctx, from, to, lw);
        this.label(ctx, a.label, add(to, mul(this.bendDir, 10 * dpr)), dpr, FIX, this.bendDir[0] < 0 ? "right" : "left", true);
      } else if (a.kind === "extend") {
        const s = P(a.from);
        const w = P(a.to);
        if (!s || !w) return;
        const d = unit(sub(w, s));
        const size = len(sub(w, s)) * 0.45;
        const from = add(w, mul(d, size * 0.2));
        const to = add(w, mul(d, size));
        this.arrow(ctx, from, to, lw);
        this.label(ctx, a.label, add(to, mul(d, 10 * dpr)), dpr, FIX, d[0] < 0 ? "right" : "left", true);
      }
    }

    drawLines(ctx, spec, P, t, lw, dpr) {
      let fixMid = null;
      spec.lines.forEach((line) => {
        const a = P(line.a);
        const b = P(line.b);
        if (!a || !b) return;
        const d = sub(b, a);
        const ea = sub(a, mul(d, 0.35));
        const eb = add(b, mul(d, 0.35));
        ctx.save();
        if (line.role === "fix") {
          ctx.strokeStyle = FIX;
          ctx.lineWidth = lw * 1.5;
          fixMid = mid(a, b);
        } else {
          ctx.strokeStyle = BONE;
          ctx.lineWidth = lw;
          ctx.setLineDash([6 * dpr, 6 * dpr]);
        }
        ctx.beginPath();
        ctx.moveTo(ea[0], ea[1]);
        ctx.lineTo(eb[0], eb[1]);
        ctx.stroke();
        ctx.restore();
      });
      const r = spec.readout;
      if (r && fixMid) {
        const value = this.readout(r.series, t, r.absolute, "°");
        const compact = this.bounds && this.bounds.w / dpr < COMPACT_WIDTH;
        if (value) this.label(ctx, compact ? value : `${r.label} ${value}`, [fixMid[0], fixMid[1] - 22 * dpr], dpr, FIX, "center");
      }
    }

    drawPath(ctx, path, P, t, lw, dpr) {
      const note = this.note;
      const start = Number(note.start_time);
      if (!Number.isFinite(start) || t < start) return;
      const kp = this.kp;
      const centre = (frame) => {
        if (!frame) return null;
        const pts = path.points.map((n) => kp.index[n]).filter((i) => i != null);
        if (!pts.length) return null;
        const r = this.contentRect(window.devicePixelRatio || 1);
        let x = 0;
        let y = 0;
        pts.forEach((i) => { x += frame[2 * i]; y += frame[2 * i + 1]; });
        return [r.x + (x / pts.length / kp.scale) * r.w, r.y + (y / pts.length / kp.scale) * r.h];
      };
      const i0 = Math.max(0, Math.round(start * kp.fps));
      const i1 = Math.min(kp.frames.length - 1, Math.round(t * kp.fps));
      const ref = centre(kp.frames[i0]);
      if (!ref) return;
      const la = P("left_ankle");
      const ra = P("right_ankle");
      const ls = P("left_shoulder");
      const top = ls ? ls[1] : ref[1] - 120 * dpr;
      const bottom = la && ra ? Math.max(la[1], ra[1]) : ref[1] + 160 * dpr;
      ctx.save();
      ctx.strokeStyle = BONE;
      ctx.lineWidth = lw;
      ctx.setLineDash([6 * dpr, 6 * dpr]);
      ctx.beginPath();
      ctx.moveTo(ref[0], top);
      ctx.lineTo(ref[0], bottom);
      ctx.stroke();
      ctx.restore();
      this.label(ctx, path.label, [ref[0], top - 12 * dpr], dpr, BONE, "center", true);
      const trail = [];
      for (let i = i0; i <= i1; i++) {
        const p = centre(kp.frames[i]);
        if (p) trail.push(p);
      }
      if (trail.length < 2) return;
      ctx.lineWidth = lw * 1.2;
      ctx.save();
      ctx.setLineDash([2 * dpr, 5 * dpr]);
      ctx.beginPath();
      trail.forEach((p, k) => (k ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.stroke();
      ctx.restore();
      const now = trail[trail.length - 1];
      if (len(sub(now, ref)) > 4 * dpr) {
        this.arrow(ctx, add(ref, mul(unit(sub(now, ref)), 2 * dpr)), now, lw);
        const value = this.staticLabel || (note.metric && note.metric.value) || "";
        this.label(ctx, value, add(now, [0, 18 * dpr]), dpr, FIX, "center");
      }
    }
  }

  function attach(video, opts) {
    if (!video) return null;
    let overlay = overlays.get(video);
    if (!overlay) {
      overlay = new Overlay(video, opts);
      overlays.set(video, overlay);
    }
    return overlay;
  }

  function onSession(session) {
    const video = document.getElementById("fixVideo");
    if (!video) return;
    mainOverlay = attach(video, { toggle: true, caption: true });
    const ok = session && session.session_id && !session.empty && !session.processing && !session.error && !session.fake_egocentric;
    mainOverlay.setNote(null, null);
    mainOverlay.setSession(ok ? session.session_id : null);
  }

  function onNote(note, payload) {
    if (!mainOverlay) return;
    let marked = note;
    // A fix rewritten by the coach keeps the marking of the measured fix it came from.
    if (marked && !marked.overlay && payload && payload.findings) {
      const prior = payload.findings.find((f) => f.id === marked.id)
        || payload.findings.find((f) => f.series && f.series === marked.series);
      if (prior && prior.overlay) marked = Object.assign({}, marked, { overlay: prior.overlay });
    }
    mainOverlay.setNote(marked, payload);
  }

  window.cerebelSkeleton = { attach, onSession, onNote };
})();
