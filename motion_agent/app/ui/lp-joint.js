/* Landing "Every joint": old Swing-analysis board (line graphs + fix windows + skeleton close-up)
   in the dusk landing tokens. Data: /static/hero/swing-board.json (series, findings, phases) and
   /static/hero/swing-sample.json + opensim-bones.glb for the close-up.
   Finding.focus.{anchor,bones,view_yaw} drives camera zoom and lit bodies — same contract as the
   product sport board close-up. Selecting a fix window (or a Fix-tab finding via cerebel:lp-fix)
   seeks the scrubber and re-focuses the skeleton. */
const THREE = await import("/static/three/three.module.js");
const { GLTFLoader } = await import("/static/three/GLTFLoader.js");

const root = document.querySelector("[data-lp-joint]");
if (!root) {
  // Panel may be absent on signed-in chrome-only views.
} else {
  boot(root).catch((err) => console.warn("lp-joint", err));
}

async function boot(root) {
  const deck = document.getElementById("lpJointDeck");
  const phasesEl = document.getElementById("lpJointPhases");
  const scrub = document.getElementById("lpJointScrub");
  const playBtn = document.getElementById("lpJointPlay");
  const liveEl = document.getElementById("lpJointLive");
  const noteEl = document.getElementById("lpJointNote");
  const closeup = document.getElementById("lpJointCloseup");
  const closeupStatus = document.getElementById("lpJointCloseupStatus");
  const closeupTarget = document.getElementById("lpJointCloseupTarget");
  if (!deck || !scrub || !closeup) return;

  const [board, sample] = await Promise.all([
    fetch("/static/hero/swing-board.json").then((r) => r.json()),
    fetch("/static/hero/swing-sample.json").then((r) => r.json()),
  ]);

  const t0 = Number(board.start_time) || 0;
  const t1 = Number(board.end_time) || (sample.frames && sample.frames.length
    ? sample.frames[sample.frames.length - 1].t
    : 1);
  const span = Math.max(t1 - t0, 1e-6);
  const findings = Array.isArray(board.findings) ? board.findings.slice() : [];
  findings.sort((a, b) => Number(b.severity || 0) - Number(a.priority || 0));

  scrub.min = String(t0);
  scrub.max = String(t1);
  scrub.step = "0.001";
  scrub.value = String(t0);

  if (noteEl) {
    const d = board.disclosure || {};
    noteEl.textContent = [d.motion, d.coaching, board.note].filter(Boolean).join(" ");
  }

  // ---- phases ----
  const phaseItems = [];
  (board.phases || []).forEach((phase) => {
    const li = document.createElement("li");
    const start = Number(phase.start_time);
    const end = Number(phase.end_time);
    li.style.setProperty("--span", String(Math.max(0.15, end - start)));
    li.textContent = phase.label || phase.id || "Phase";
    li.title = li.textContent;
    li.addEventListener("click", () => seek(start));
    phasesEl.appendChild(li);
    phaseItems.push({ el: li, start, end });
  });

  // ---- line graphs + fix window annotations ----
  function seriesGeometry(times, values) {
    if (!times || !times.length) return { d: "", y: () => 50, min: 0, max: 1 };
    const a = times[0];
    const b = times[times.length - 1];
    const s = Math.max(b - a, 1e-6);
    let min = Math.min.apply(null, values);
    let max = Math.max.apply(null, values);
    if (min === max) { min -= 1; max += 1; }
    const pad = (max - min) * 0.08;
    min -= pad;
    max += pad;
    const y = (v) => 8 + (1 - (v - min) / (max - min)) * 84;
    const x = (t) => ((t - a) / s) * 100;
    const d = times.map((t, i) => (i ? "L" : "M") + x(t).toFixed(2) + " " + y(values[i]).toFixed(2)).join(" ");
    return { d, y, min, max };
  }

  function formatValue(value, unit) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    const n = Number(value);
    const abs = Math.abs(n);
    const text = abs >= 100 ? n.toFixed(0) : abs >= 10 ? n.toFixed(1) : n.toFixed(2);
    return text + (unit ? " " + unit : "");
  }

  function valueAt(trace, time) {
    const times = trace.times || [];
    const values = trace.values || [];
    if (!times.length) return null;
    let best = 0;
    let bestDist = Infinity;
    times.forEach((t, i) => {
      const dist = Math.abs(t - time);
      if (dist < bestDist) { bestDist = dist; best = i; }
    });
    return values[best];
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  const tracks = [];
  (board.series || []).forEach((trace) => {
    const geo = seriesGeometry(trace.times, trace.values);
    const wrap = document.createElement("div");
    wrap.className = "lp-joint-track";
    wrap.dataset.series = trace.id;
    const windows = findings.filter((note) => note.series === trace.id);
    const buttons = windows.map((note) => {
      const left = ((Number(note.start_time) - t0) / span) * 100;
      const width = ((Number(note.end_time) - Number(note.start_time)) / span) * 100;
      return `<button type="button" data-id="${escapeHtml(note.id)}" data-seek="${note.start_time}" style="--accent:${note.accent || "#F2A541"};left:${left}%;width:${Math.max(width, 1.2)}%" aria-label="${escapeHtml(note.title || note.headline || "Fix")}"></button>`;
    }).join("");
    const zero = (geo.min < 0 && geo.max > 0)
      ? `<line class="lp-joint-track__zero" x1="0" x2="100" y1="${geo.y(0).toFixed(2)}" y2="${geo.y(0).toFixed(2)}"></line>`
      : "";
    const goals = windows.filter((note) => note.goal && note.goal.value != null).map((note) => {
      const y = geo.y(Number(note.goal.value)).toFixed(2);
      const x1 = ((Number(note.start_time) - t0) / span) * 100;
      const x2 = ((Number(note.end_time) - t0) / span) * 100;
      return `<line class="lp-joint-track__goal" style="--accent:${note.accent || "#F2A541"}" x1="${x1}" x2="${x2}" y1="${y}" y2="${y}"></line>`;
    }).join("");
    wrap.innerHTML =
      `<div class="lp-joint-track__head">` +
        `<span>${escapeHtml(trace.label)}</span>` +
        `<output data-readout="${escapeHtml(trace.id)}">${formatValue((trace.values || [])[0], trace.unit)}</output>` +
      `</div>` +
      `<div class="lp-joint-track__lane">` +
        buttons +
        `<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">${zero}${goals}<path d="${geo.d}"></path></svg>` +
        `<i class="lp-joint-track__playhead" aria-hidden="true"></i>` +
      `</div>`;
    wrap.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        const note = findings.find((f) => f.id === btn.dataset.id);
        selectFinding(note, { seekTo: Number(btn.dataset.seek) });
      });
    });
    deck.appendChild(wrap);
    tracks.push({ wrap, trace, readout: wrap.querySelector("output"), playhead: wrap.querySelector(".lp-joint-track__playhead") });
  });

  // ---- skeleton close-up (static sample GLB + frames; focus zooms like ensureSportCloseup) ----
  const css = getComputedStyle(document.documentElement);
  const token = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();
  const boneColor = new THREE.Color(token("--bone", "#EEF0EA"));
  const quietColor = new THREE.Color("#5d6d66");
  const accentColor = new THREE.Color(token("--fix", "#F2A541"));
  const lampColor = new THREE.Color(token("--lamp", "#E8C98A"));

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x08130f, 1);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  closeup.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.02, 40);
  camera.position.set(0.4, 1.3, 2.4);
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const keyLight = new THREE.DirectionalLight(0xf4f0fa, 1.15);
  keyLight.position.set(1.6, 3.2, 2.2);
  scene.add(keyLight);
  const fill = new THREE.DirectionalLight(0xe8c98a, 0.5);
  fill.position.set(-1.8, 1.2, -1.2);
  scene.add(fill);

  const gltf = await new GLTFLoader().loadAsync(
    (board.skeleton && board.skeleton.glb) || "/static/hero/opensim-bones.glb"
  );
  const model = gltf.scene;
  const boneMat = new THREE.MeshStandardMaterial({
    color: boneColor.clone(), roughness: 0.48, metalness: 0.06, emissive: new THREE.Color(0x000000),
  });
  model.traverse((o) => {
    if (!o.isMesh) return;
    o.material = boneMat.clone();
    o.castShadow = false;
  });
  scene.add(model);

  const bodies = sample.bodies || [];
  const nodes = bodies.map((name) => model.getObjectByName(name));
  const frames = sample.frames || [];

  // Muscles: same fusiform tubes as the reel (paths in sample frames).
  const muscleMat = new THREE.MeshStandardMaterial({
    color: 0xa9776d, roughness: 0.5, metalness: 0, transparent: true, opacity: 0.72,
  });
  const muscleMeshes = (sample.muscles || []).map(() => {
    const m = new THREE.Mesh(new THREE.BufferGeometry(), muscleMat);
    m.userData.overlay = true;
    model.add(m);
    return m;
  });
  function muscleGeometry(points) {
    const v = points.map((p) => new THREE.Vector3(p[0], p[1], p[2]));
    if (v.length === 2) v.splice(1, 0, v[0].clone().lerp(v[1], 0.5));
    const curve = new THREE.CatmullRomCurve3(v, false, "centripetal", 0.5);
    const len = Math.max(curve.getLength(), 0.02);
    const belly = THREE.MathUtils.clamp(0.011 + len * 0.03, 0.013, 0.026);
    const tip = belly * 0.25;
    const seg = 14, rad = 6;
    const fr = curve.computeFrenetFrames(seg, false);
    const pos = new Float32Array((seg + 1) * (rad + 1) * 3);
    const nrm = new Float32Array(pos.length);
    const index = [];
    const p = new THREE.Vector3(), n = new THREE.Vector3();
    for (let i = 0; i <= seg; i++) {
      const u = i / seg;
      const r = tip + (belly - tip) * Math.pow(Math.sin(Math.PI * u), 1.2);
      curve.getPointAt(u, p);
      for (let j = 0; j <= rad; j++) {
        const a = (j / rad) * Math.PI * 2;
        n.copy(fr.normals[i]).multiplyScalar(-Math.cos(a)).addScaledVector(fr.binormals[i], Math.sin(a)).normalize();
        const k = (i * (rad + 1) + j) * 3;
        pos[k] = p.x + r * n.x; pos[k + 1] = p.y + r * n.y; pos[k + 2] = p.z + r * n.z;
        nrm[k] = n.x; nrm[k + 1] = n.y; nrm[k + 2] = n.z;
      }
    }
    for (let i = 0; i < seg; i++) {
      for (let j = 0; j < rad; j++) {
        const a = i * (rad + 1) + j, b = a + rad + 1;
        index.push(a, b, a + 1, b, b + 1, a + 1);
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setAttribute("normal", new THREE.BufferAttribute(nrm, 3));
    g.setIndex(index);
    return g;
  }

  const qa = new THREE.Quaternion(), qb = new THREE.Quaternion();
  function sampleFrame(t) {
    if (!frames.length) return { a: null, b: null, k: 0 };
    if (t <= frames[0].t) return { a: frames[0], b: frames[0], k: 0 };
    if (t >= frames[frames.length - 1].t) {
      const last = frames[frames.length - 1];
      return { a: last, b: last, k: 0 };
    }
    let lo = 0, hi = frames.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (frames[mid].t <= t) lo = mid; else hi = mid;
    }
    const a = frames[lo], b = frames[hi];
    return { a, b, k: (t - a.t) / Math.max(1e-6, b.t - a.t) };
  }

  function poseAt(t) {
    const { a, b, k } = sampleFrame(t);
    if (!a) return;
    nodes.forEach((node, n) => {
      if (!node) return;
      const A = a.b[n], Bv = b.b[n];
      node.position.set(A[0] + (Bv[0] - A[0]) * k, A[1] + (Bv[1] - A[1]) * k, A[2] + (Bv[2] - A[2]) * k);
      qa.set(A[3], A[4], A[5], A[6]); qb.set(Bv[3], Bv[4], Bv[5], Bv[6]);
      node.quaternion.copy(qa.slerp(qb, k));
    });
    const src = k < 0.5 ? a : b;
    muscleMeshes.forEach((mesh, n) => {
      const pts = src.m && src.m[n];
      mesh.geometry.dispose();
      mesh.geometry = pts && pts.length >= 2 ? muscleGeometry(pts) : new THREE.BufferGeometry();
    });
    model.updateMatrixWorld(true);
  }

  let focus = { anchor: "pelvis", bones: ["pelvis", "torso"], view_yaw: 0, label: "Body" };
  let activeId = null;
  const goal = new THREE.Vector3(0, 1, 0);
  const look = new THREE.Vector3(0, 1, 0);
  const desired = new THREE.Vector3();

  function setFocus(note) {
    const next = (note && note.focus) || focus;
    focus = next;
    accentColor.set((note && note.accent) || token("--fix", "#F2A541"));
    if (closeupTarget) {
      closeupTarget.innerHTML = "<span>Fix</span>" + escapeHtml(next.label || (note && (note.title || note.headline)) || "Body");
    }
  }

  function resizeCloseup() {
    const w = closeup.clientWidth || 320;
    const h = closeup.clientHeight || 360;
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }
  new ResizeObserver(resizeCloseup).observe(closeup);
  resizeCloseup();

  let currentT = t0;
  let playing = false;
  let raf = 0;
  let lastTick = 0;

  function findingAt(t) {
    // Prefer the selected finding while the scrub is inside its window; else the densest overlap.
    if (activeId) {
      const sel = findings.find((f) => f.id === activeId);
      if (sel && t >= Number(sel.start_time) - 1e-3 && t <= Number(sel.end_time) + 1e-3) return sel;
    }
    let best = null;
    findings.forEach((f) => {
      if (t < Number(f.start_time) || t > Number(f.end_time)) return;
      if (!best || Number(f.priority || 0) > Number(best.priority || 0)) best = f;
    });
    return best;
  }

  function paintUi(t) {
    const pct = ((t - t0) / span) * 100;
    tracks.forEach(({ wrap, trace, readout, playhead }) => {
      if (playhead) playhead.style.setProperty("--playhead", pct + "%");
      if (readout) readout.textContent = formatValue(valueAt(trace, t), trace.unit);
      wrap.querySelectorAll("button").forEach((btn) => {
        btn.classList.toggle("is-active", btn.dataset.id === activeId);
      });
    });
    phaseItems.forEach(({ el, start, end }) => {
      el.classList.toggle("is-active", t >= start - 1e-6 && t < end - 1e-6);
    });
    const live = findingAt(t) || findings.find((f) => f.id === activeId) || findings[0];
    if (liveEl) {
      if (live) {
        const metric = (live.metric && live.metric.value) || "";
        liveEl.innerHTML = escapeHtml(metric) + "<small>" + escapeHtml(live.headline || live.title || "") + "</small>";
      } else {
        liveEl.textContent = "";
      }
    }
    if (live) setFocus(live);
  }

  function seek(t, { fromFix } = {}) {
    currentT = Math.min(t1, Math.max(t0, Number(t)));
    scrub.value = String(currentT);
    poseAt(currentT);
    paintUi(currentT);
    if (!fromFix && activeId) {
      // Keep window selection; do not clear.
    }
  }

  function selectFinding(note, { seekTo, fromFix } = {}) {
    if (!note) return;
    activeId = note.id;
    setFocus(note);
    const t = seekTo != null ? seekTo : Number(note.start_time);
    seek(t, { fromFix });
    if (!fromFix) {
      window.dispatchEvent(new CustomEvent("cerebel:lp-joint-fix", { detail: { id: note.id, finding: note } }));
    }
  }

  scrub.addEventListener("input", () => {
    playing = false;
    if (playBtn) playBtn.textContent = "▶";
    seek(Number(scrub.value));
  });

  if (playBtn) {
    playBtn.addEventListener("click", () => {
      playing = !playing;
      playBtn.textContent = playing ? "❚❚" : "▶";
      playBtn.setAttribute("aria-label", playing ? "Pause traces" : "Play traces");
      lastTick = performance.now();
    });
  }

  function tick(now) {
    raf = requestAnimationFrame(tick);
    if (playing) {
      const dt = Math.min(0.05, (now - lastTick) / 1000);
      lastTick = now;
      currentT += dt;
      if (currentT >= t1) {
        currentT = t1;
        playing = false;
        if (playBtn) {
          playBtn.textContent = "▶";
          playBtn.setAttribute("aria-label", "Play traces");
        }
      }
      scrub.value = String(currentT);
      poseAt(currentT);
      paintUi(currentT);
    }
    // Camera + bone lighting always lerp toward the active finding's focus
    // (focus.view_yaw / focus.bones — same contract as the product sport close-up).
    const node = model.getObjectByName(focus.anchor) || model.getObjectByName("pelvis");
    if (node) node.getWorldPosition(goal);
    const yaw = ((focus.view_yaw || 0) * Math.PI) / 180;
    const tight = focus.anchor && focus.anchor !== "pelvis" && focus.anchor !== "torso";
    const dist = tight ? 0.78 : 1.25;
    desired.set(goal.x + Math.sin(yaw) * dist, goal.y + dist * 0.18, goal.z + Math.cos(yaw) * dist);
    camera.position.lerp(desired, 0.08);
    look.lerp(goal, 0.1);
    camera.lookAt(look);
    const lit = new Set(focus.bones || []);
    model.traverse((object) => {
      if (!object.isMesh || !object.material || object.userData.overlay) return;
      let body = object;
      let name = "";
      while (body) {
        if (bodies.indexOf(body.name) >= 0) { name = body.name; break; }
        body = body.parent;
      }
      const on = lit.has(name);
      object.material.color.copy(on ? accentColor : boneColor).lerp(quietColor, on ? 0 : 0.55);
      object.material.emissive.copy(on ? accentColor : quietColor).multiplyScalar(on ? 0.42 : 0);
    });
    renderer.render(scene, camera);
  }

  // Sync from Fix tab indexer.
  window.addEventListener("cerebel:lp-fix", (ev) => {
    const id = ev.detail && ev.detail.id;
    const note = findings.find((f) => f.id === id) || (ev.detail && ev.detail.finding);
    if (!note) return;
    // Prefer board copy (has series/accent/focus) when ids match.
    const local = findings.find((f) => f.id === note.id) || note;
    selectFinding(local, { seekTo: Number(local.start_time), fromFix: true });
  });

  // Lazy start when the panel becomes visible (hidden tabs have no size).
  let started = false;
  function startWhenVisible() {
    if (started) return;
    if (root.closest("[hidden]") || root.hasAttribute("hidden")) return;
    if (closeup.clientWidth < 40) return;
    started = true;
    if (closeupStatus) closeupStatus.hidden = true;
    const primary = findings[0] || null;
    if (primary) selectFinding(primary, { seekTo: Number(primary.start_time) });
    else {
      poseAt(t0);
      paintUi(t0);
      setFocus(null);
    }
    resizeCloseup();
    raf = requestAnimationFrame(tick);
  }
  startWhenVisible();
  window.addEventListener("cerebel:landing", () => requestAnimationFrame(startWhenVisible));
  // Tab switches toggle [hidden] on the panel.
  const mo = new MutationObserver(() => requestAnimationFrame(startWhenVisible));
  mo.observe(root, { attributes: true, attributeFilter: ["hidden"] });
  document.getElementById("lpTabJoint")?.addEventListener("click", () => {
    requestAnimationFrame(() => requestAnimationFrame(startWhenVisible));
  });
}
