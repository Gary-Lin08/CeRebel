/* CeRebel swing reel: one real swing, driven by scroll.
   Data: /static/hero/swing-sample.json (OpenSim IK body transforms, muscle paths, findings, and the clip)
   and /static/hero/opensim-bones.glb (Rajagopal-Lai-Uhlrich 2023 bones, one node per body).
   Scrolling plays the full swing: phone video, then the OpenSim body with bones + muscles. The copy
   on the right tracks identified issues by time (independent of golf-phase labels). The featured
   fix lights the related bones near its timestamp. Phase buttons jump the page to a phase.
   With reduced motion the reel is a still at the featured fix with everything shown. */
const THREE = await import("/static/three/three.module.js");
const { GLTFLoader } = await import("/static/three/GLTFLoader.js");

const root = document.querySelector("[data-swing-hero]");
let started = false;
function boot() {
  // A hidden landing (signed in, or ?dev=1) has no size to render into, so wait until it shows.
  if (!root || started || root.closest("[hidden]")) return;
  started = true;
  start(root).catch((err) => {
    root.classList.add("is-failed");
    console.warn("swing hero", err);
  });
}
boot();
window.addEventListener("cerebel:landing", boot);

async function start(root) {
  const stage = root.querySelector(".swing__stage");
  const phaseList = root.querySelector(".swing__phases");
  const numEl = root.querySelector(".reel__num");
  const readout = root.querySelector(".swing__readout");
  const tag = root.querySelector(".swing__tag");
  const video = root.querySelector(".swing__video video");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const css = getComputedStyle(document.documentElement);
  const color = (name, fallback) => new THREE.Color((css.getPropertyValue(name) || fallback).trim());

  const [data, gltf] = await Promise.all([
    fetch("/static/hero/swing-sample.json").then((r) => r.json()),
    new GLTFLoader().loadAsync("/static/hero/opensim-bones.glb"),
  ]);
  const frames = data.frames;
  const tEnd = frames[frames.length - 1].t;
  const tFix = data.fix.t;
  // The highlighted measurement: frame.v when the data names one, else the lead elbow.
  const fixLabel = data.fix.label || "Lead elbow";
  const fixAnchor = data.fix.anchor || "ulna_l";
  const valueOf = (fr) => (fr.v != null ? fr.v : fr.elbow);
  const findings = Array.isArray(data.findings) ? data.findings.slice() : [];
  findings.sort((a, b) => Number(a.start_time) - Number(b.start_time));
  const findingItems = []; // filled once the acts list is built; safe if pose runs early
  let introItem = null;
  if (video && data.video && data.video.src) {
    video.dataset.offset = String(data.video.offset || 0);
    video.src = data.video.src;
    video.hidden = false;
    // iOS Safari paints nothing for a video that has never played, even after a seek; until then
    // the card shows its poster. A muted play-then-pause makes it decode frames for the scrub.
    video.addEventListener("loadedmetadata", () => {
      const primed = video.play();
      if (primed) primed.then(() => { video.pause(); pose(current); }).catch(() => {});
    }, { once: true });
    // Seeks are skipped while one is in flight; syncVideo() catches up on "seeked".
    video.addEventListener("loadeddata", () => pose(current));
  }

  // Scene
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  (stage.querySelector(".swing__gl") || stage).prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(24, 1, 0.1, 50);

  scene.add(new THREE.HemisphereLight(0xdfe9e4, 0x0a1512, 0.95));
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(2.2, 4.2, 3.2);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.left = -1.5; key.shadow.camera.right = 1.5;
  key.shadow.camera.top = 2.5; key.shadow.camera.bottom = -0.5;
  key.shadow.radius = 6;
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xe8c98a, 1.6); // floodlight from behind
  rim.position.set(-3, 2.5, -2.5);
  scene.add(rim);

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(8, 4), new THREE.ShadowMaterial({ opacity: 0.38 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  // Bones
  const model = gltf.scene;
  const boneMat = new THREE.MeshStandardMaterial({ color: 0xe9e4d8, roughness: 0.55, metalness: 0, transparent: true });
  const fixMat = new THREE.MeshStandardMaterial({ color: color("--fix", "#F2A541"), emissive: 0x6a3a00, roughness: 0.45, metalness: 0, transparent: true });
  const fixBones = new Set(data.fix.bones);
  model.traverse((o) => {
    if (o.isMesh) {
      o.material = boneMat;
      o.castShadow = true;
      o.receiveShadow = true;
    }
  });
  const nodes = data.bodies.map((name) => model.getObjectByName(name));
  scene.add(model);

  // Muscles: fusiform tubes along each OpenSim path, rebuilt per frame (paths change length).
  const muscleMat = new THREE.MeshStandardMaterial({
    color: 0xa9776d, roughness: 0.5, metalness: 0, transparent: true, opacity: 0.8, // muted, so the amber highlight reads
  });
  const muscleMeshes = data.muscles.map(() => {
    const m = new THREE.Mesh(new THREE.BufferGeometry(), muscleMat);
    m.castShadow = true;
    m.userData.overlay = true;
    model.add(m); // muscle paths are in the same frame as the bones, so they share its offset
    return m;
  });
  const curvePts = [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()];
  function muscleGeometry(points) {
    const v = points.map((p) => new THREE.Vector3(p[0], p[1], p[2]));
    if (v.length === 2) v.splice(1, 0, v[0].clone().lerp(v[1], 0.5));
    const curve = new THREE.CatmullRomCurve3(v, false, "centripetal", 0.5);
    const len = Math.max(curve.getLength(), 0.02);
    const belly = THREE.MathUtils.clamp(0.011 + len * 0.03, 0.013, 0.026);
    const tip = belly * 0.25;
    const seg = 18, rad = 8;
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

  // Path of the hands so far, like the light trail in a multiple-exposure swing photo.
  const handIdx = [data.bodies.indexOf("hand_l"), data.bodies.indexOf("hand_r")];
  const trailGeo = new THREE.BufferGeometry();
  const trail = new THREE.Line(trailGeo, new THREE.LineDashedMaterial({
    color: color("--lamp", "#E8C98A"), dashSize: 0.012, gapSize: 0.03, transparent: true, opacity: 0.9,
  }));
  trail.userData.overlay = true;
  model.add(trail);

  // Frame sampling with interpolation between stored frames (search by timestamp, not index ratio).
  const qa = new THREE.Quaternion(), qb = new THREE.Quaternion();
  function sample(t) {
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
    const span = Math.max(1e-6, b.t - a.t);
    return { a, b, k: (t - a.t) / span };
  }
  function handsAt(fr) {
    const L = fr.b[handIdx[0]], R = fr.b[handIdx[1]];
    return new THREE.Vector3((L[0] + R[0]) / 2, (L[1] + R[1]) / 2, (L[2] + R[2]) / 2);
  }

  let fixShown = 0;
  let current = 0;
  let wantVideoT = 0;
  const elbowWorld = new THREE.Vector3();
  function syncVideo(t) {
    if (!video) return;
    wantVideoT = t + (parseFloat(video.dataset.offset || "0") || 0);
    if (video.readyState < 1) return;
    if (video.seeking) return;
    if (Math.abs(video.currentTime - wantVideoT) > 0.04) video.currentTime = wantVideoT;
  }
  if (video) {
    video.addEventListener("seeked", () => {
      if (Math.abs(video.currentTime - wantVideoT) > 0.04) video.currentTime = wantVideoT;
    });
  }
  function pose(t) {
    current = t;
    syncVideo(t);
    const { a, b, k } = sample(t);
    nodes.forEach((node, n) => {
      if (!node) return;
      const A = a.b[n], Bv = b.b[n];
      node.position.set(A[0] + (Bv[0] - A[0]) * k, A[1] + (Bv[1] - A[1]) * k, A[2] + (Bv[2] - A[2]) * k);
      qa.set(A[3], A[4], A[5], A[6]); qb.set(Bv[3], Bv[4], Bv[5], Bv[6]);
      node.quaternion.copy(qa.slerp(qb, k));
    });
    const src = k < 0.5 ? a : b;
    muscleMeshes.forEach((mesh, n) => {
      const pts = src.m[n];
      mesh.geometry.dispose();
      mesh.geometry = pts && pts.length >= 2 ? muscleGeometry(pts) : new THREE.BufferGeometry();
    });
    const upto = frames.filter((fr) => fr.t <= t).map(handsAt);
    upto.push(handsAt(src));
    trailGeo.setFromPoints(upto);
    trail.computeLineDistances();

    const live = markFinding(t);
    const showFix = fixShown > 0;
    model.traverse((o) => {
      if (!o.isMesh || o.userData.overlay) return;
      let body = o;
      while (body && !fixBones.has(body.name) && body !== model) body = body.parent;
      o.material = showFix && body && fixBones.has(body.name) ? fixMat : boneMat;
    });
    const value = Math.round(valueOf(a) + (valueOf(b) - valueOf(a)) * k);
    const metricLabel = (live && live.metric && live.metric.caption) || (live && live.title) || fixLabel;
    const metricText = (live && live.metric && live.metric.value) || (String(value) + "°");
    if (readout) readout.textContent = metricLabel;
    if (numEl) {
      const raw = String(metricText).replace(/°/g, "");
      numEl.textContent = raw;
    }
    const anchorName = (live && live.focus && live.focus.anchor) || fixAnchor;
    const anchor = model.getObjectByName(anchorName);
    if (anchor) anchor.getWorldPosition(elbowWorld);
    if (tag) {
      tag.textContent = metricText;
      tag.style.opacity = String(fixShown);
    }
    markPhase(t);
    render();
  }

  // Framing: fit the whole swing's extent, three-quarter face-on view.
  const target = new THREE.Vector3(0, 0.95, 0);
  function fit() {
    const w = stage.clientWidth, h = stage.clientHeight || w * 1.05;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // One body only; portrait screens get a tighter margin so it stays large.
    const span = 1.05 * (camera.aspect < 1 ? 0.7 : 1);
    const dist = Math.max(4.9, (4.9 * span) / Math.min(span, camera.aspect * 1.1));
    // On phones the video sits in the top corner, so look a little higher and the body drops below it.
    target.y = camera.aspect < 1 ? 1.2 : 0.95;
    const yaw = 0.42;
    camera.position.set(target.x + Math.sin(yaw) * dist, 1.25, target.z + Math.cos(yaw) * dist);
    camera.lookAt(target);
    camera.updateProjectionMatrix();
    render();
  }
  const v = new THREE.Vector3();
  function render() {
    renderer.render(scene, camera);
    if (tag) {
      v.copy(elbowWorld).project(camera);
      const r = renderer.domElement.getBoundingClientRect();
      const s = (tag.offsetParent || stage).getBoundingClientRect();
      tag.style.transform = `translate(${((v.x + 1) / 2) * r.width + (r.left - s.left) - 64}px, ${((1 - v.y) / 2) * r.height + (r.top - s.top) - 14}px)`;
    }
  }
  new ResizeObserver(fit).observe(stage);

  // Center the muscle-equipped OpenSim body on the ground plane.
  const p0 = frames[0].b[data.bodies.indexOf("pelvis")];
  const yaw0 = 0.42;
  model.position.set(0, 0, 0);
  target.set(p0[0], 0.92, p0[2]);
  ground.position.set(p0[0], 0.001, p0[2]);
  ground.rotation.z = yaw0;

  // Full-swing phase rail (address through follow-through).
  const rail = (data.phases || []).map((p) => [p[0], p[1], p[2]]);
  if (!rail.some((p) => Math.abs(p[1] - tFix) < 1e-3)) {
    rail.push(["Top", tFix, tFix]);
    rail.sort((a, b) => a[1] - b[1]);
  }
  const phaseAt = (t) => {
    let n = rail[0] ? rail[0][0] : "";
    rail.forEach((p) => { if (t >= p[1] - 1e-6) n = p[0]; });
    return n;
  };
  const items = [];
  rail.forEach((p) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = p[0];
    b.addEventListener("click", () => scrollToTime(p[2] === p[1] ? p[1] : (p[1] + p[2]) / 2));
    phaseList.appendChild(b);
    items.push([p[0], b]);
  });
  function markPhase(t) {
    const cur = phaseAt(t);
    items.forEach(([name, b]) => {
      b.classList.toggle("is-current", name === cur);
      if (name === cur) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
    });
  }

  // Right-side copy: identified issues by time (same rule as the playground sport board).
  const acts = root.querySelector(".reel__acts");
  function activeFinding(t) {
    if (!findings.length) return null;
    const inside = findings.filter((note) => t >= Number(note.start_time) && t <= Number(note.end_time));
    if (inside.length) {
      return inside.reduce((best, note) => (
        Number(note.start_time) >= Number(best.start_time) ? note : best
      ));
    }
    let last = null;
    findings.forEach((note) => {
      if (Number(note.start_time) <= t && (!last || Number(note.start_time) >= Number(last.start_time))) {
        last = note;
      }
    });
    return last;
  }
  if (acts) {
    acts.innerHTML = "";
    introItem = document.createElement("li");
    introItem.dataset.finding = "intro";
    introItem.innerHTML = "<b>One phone, one swing.</b> Scroll to play — each identified issue appears when it happens.";
    acts.appendChild(introItem);
    findings.forEach((note, i) => {
      const li = document.createElement("li");
      li.dataset.finding = note.id || String(i);
      const title = note.headline || note.title || "Finding";
      const body = note.plain || (note.metric && note.metric.caption) || "";
      li.innerHTML = "<b></b> ";
      li.querySelector("b").textContent = title;
      li.appendChild(document.createTextNode(body));
      acts.appendChild(li);
      findingItems.push([note.id || String(i), li, note]);
    });
  }
  function markFinding(t) {
    const live = activeFinding(t);
    const liveId = live && (live.id || "");
    findingItems.forEach(([id, li]) => {
      const on = !!liveId && id === liveId;
      li.classList.toggle("is-on", on);
    });
    if (introItem) introItem.classList.toggle("is-on", !live);
    if (live && live.focus && Array.isArray(live.focus.bones)) {
      fixBones.clear();
      live.focus.bones.forEach((name) => fixBones.add(name));
    } else {
      fixBones.clear();
      (data.fix.bones || []).forEach((name) => fixBones.add(name));
    }
    return live;
  }

  // Scroll position through the reel (0 to 1) maps linearly across the full swing.
  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  const smooth = (x) => { x = clamp01(x); return x * x * (3 - 2 * x); };
  const SWING_FROM = 0, SWING_TO = 0.90;
  const timeAt = (p) => tEnd * clamp01((p - SWING_FROM) / (SWING_TO - SWING_FROM));
  function scrollProgress() {
    const r = root.getBoundingClientRect();
    return clamp01(-r.top / Math.max(1, r.height - window.innerHeight));
  }
  function scrollToTime(t) {
    let lo = 0, hi = 1;
    for (let k = 0; k < 30; k++) { const mid = (lo + hi) / 2; if (timeAt(mid) < t) lo = mid; else hi = mid; }
    const p = t >= tEnd - 1e-6 ? 0.95 : hi;
    const top = root.getBoundingClientRect().top + window.scrollY;
    window.scrollTo({ top: top + p * (root.offsetHeight - window.innerHeight), behavior: reduced ? "auto" : "smooth" });
  }

  let shown = -1;
  function apply(p) {
    // Visual acts stay independent of issue copy: phone → body → lit fix window.
    const act = p < 0.22 ? 1 : p < 0.55 ? 2 : 3;
    if (root.dataset.act !== String(act)) root.dataset.act = String(act);
    const o = smooth((p - 0.18) / 0.12);
    boneMat.opacity = o;
    fixMat.opacity = o;
    muscleMat.opacity = 0.8 * o;
    trail.material.opacity = 0.9 * o;
    model.visible = o > 0.01;
    const t = timeAt(p);
    // Light the featured bones near the fix timestamp (and whenever a finding window is active).
    const nearFix = 1 - clamp01(Math.abs(t - tFix) / 0.18);
    const inFinding = findings.some((note) => t >= Number(note.start_time) && t <= Number(note.end_time));
    fixShown = Math.max(nearFix, inFinding ? 0.85 : 0);
    root.classList.toggle("is-fix", fixShown > 0.5);
    root.style.setProperty("--p", p.toFixed(4));
    pose(t);
  }

  fit();
  root.classList.add("is-ready");
  if (reduced) { apply(Math.min(1, tFix / Math.max(tEnd, 1e-6))); return; }
  let cur = scrollProgress(), raf = 0;
  function tick() {
    const p = scrollProgress();
    root.classList.toggle("is-moved", p > 0.02);
    // Track scroll closely so phase labels and pose stay on the same stage.
    cur += (p - cur) * 0.35;
    if (Math.abs(p - cur) < 0.0004) cur = p;
    if (cur !== shown) { shown = cur; apply(cur); }
    raf = requestAnimationFrame(tick);
  }
  apply(cur);
  // Only run while the reel is on screen.
  new IntersectionObserver((entries) => {
    const on = entries.some((x) => x.isIntersecting);
    if (on && !raf) raf = requestAnimationFrame(tick);
    if (!on && raf) { cancelAnimationFrame(raf); raf = 0; }
  }).observe(root);
}
