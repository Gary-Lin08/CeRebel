/* CeRebel swing hero: one real swing as the OpenSim musculoskeletal model.
   Data: /static/hero/swing-sample.json (OpenSim IK body transforms + muscle paths, no video, no face)
   and /static/hero/opensim-bones.glb (Rajagopal-Lai-Uhlrich 2023 bones, one node per body).
   Motion happens once on load: address to the top, then the lead arm turns red and the elbow
   angle appears. After that only the scrubber, the phase buttons or Replay move it.
   With reduced motion the page shows the top of the backswing as a still. */
const THREE = await import("/static/three/three.module.js");
const { GLTFLoader } = await import("/static/three/GLTFLoader.js");

const root = document.querySelector("[data-swing-hero]");
if (root) start(root).catch((err) => {
  root.classList.add("is-failed");
  console.warn("swing hero", err);
});

async function start(root) {
  const stage = root.querySelector(".swing__stage");
  const range = root.querySelector(".swing__range");
  const phaseList = root.querySelector(".swing__phases");
  const replay = root.querySelector(".swing__replay");
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
  const tTop = data.fix.t;

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

  scene.add(new THREE.HemisphereLight(0xf4f6f1, 0x9aa79d, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(2.2, 4.2, 3.2);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.left = -1.5; key.shadow.camera.right = 1.5;
  key.shadow.camera.top = 2.5; key.shadow.camera.bottom = -0.5;
  key.shadow.radius = 6;
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xdfe8ff, 0.9);
  rim.position.set(-3, 2.5, -2.5);
  scene.add(rim);

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(8, 4), new THREE.ShadowMaterial({ opacity: 0.16 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  // Bones
  const model = gltf.scene;
  const boneMat = new THREE.MeshStandardMaterial({ color: 0xe9e4d8, roughness: 0.55, metalness: 0 });
  const fixMat = new THREE.MeshStandardMaterial({ color: color("--flag", "#D9402A"), roughness: 0.5, metalness: 0 });
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
    color: 0xb4584a, roughness: 0.45, metalness: 0, transparent: true, opacity: 0.82,
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
    color: color("--turf", "#2F6B45"), dashSize: 0.012, gapSize: 0.03, transparent: true, opacity: 0.9,
  }));
  trail.userData.overlay = true;
  model.add(trail);

  // GEM-X: the 3D joints the pose model estimates from the video, before any biomechanics.
  const gemx = data.gemx;
  const gemxGroup = new THREE.Group();
  scene.add(gemxGroup);
  // GEM-X joints use their own heading; turn them about the hips to face the same way as OpenSim.
  const gemxInner = new THREE.Group();
  gemxGroup.add(gemxInner);
  if (gemx) {
    const g0 = gemx.frames[0], o0 = frames[0].b, gi = (n) => gemx.names.indexOf(n), oi = (n) => data.bodies.indexOf(n);
    const L = g0[gi("LeftArm")], R = g0[gi("RightArm")], H = g0[gi("Hips")];
    const OL = o0[oi("humerus_l")], OR = o0[oi("humerus_r")];
    const yawG = Math.atan2(L[2] - R[2], L[0] - R[0]);
    const yawO = Math.atan2(OL[2] - OR[2], OL[0] - OR[0]);
    gemxInner.position.set(H[0], 0, H[2]);
    gemxInner.rotation.y = yawG - yawO;
    gemxInner.userData.pivot = new THREE.Vector3(H[0], 0, H[2]);
  }
  const jointGeo = new THREE.SphereGeometry(0.022, 16, 12);
  const jointMat = new THREE.MeshStandardMaterial({ color: color("--turf", "#2F6B45"), roughness: 0.4 });
  const linkGeo = new THREE.CylinderGeometry(0.011, 0.011, 1, 10);
  linkGeo.translate(0, 0.5, 0);
  const linkMat = new THREE.MeshStandardMaterial({ color: color("--ink", "#16241D"), roughness: 0.5 });
  const skip = new Set(["HeadEnd", "Jaw", "LeftEye", "RightEye", "LeftToeEnd", "RightToeEnd"]);
  const gJoints = gemx ? gemx.names.map((n) => {
    const m = new THREE.Mesh(jointGeo, jointMat);
    m.castShadow = true;
    m.visible = !skip.has(n);
    gemxInner.add(m);
    return m;
  }) : [];
  const gLinks = gemx ? gemx.parents.map((par, i) => {
    if (par < 0 || skip.has(gemx.names[i])) return null;
    const m = new THREE.Mesh(linkGeo, linkMat);
    m.castShadow = true;
    gemxInner.add(m);
    return m;
  }) : [];
  const up = new THREE.Vector3(0, 1, 0), dir = new THREE.Vector3();
  const gp = gemx ? gemx.names.map(() => new THREE.Vector3()) : [];
  function poseGemx(t) {
    if (!gemx) return;
    const f = Math.max(0, Math.min(gemx.frames.length - 1, (t / tEnd) * (gemx.frames.length - 1)));
    const i = Math.floor(f), j = Math.min(gemx.frames.length - 1, i + 1), k = f - i;
    gp.forEach((v, n) => {
      const A = gemx.frames[i][n], Bv = gemx.frames[j][n];
      v.set(A[0] + (Bv[0] - A[0]) * k, A[1] + (Bv[1] - A[1]) * k, A[2] + (Bv[2] - A[2]) * k).sub(gemxInner.userData.pivot);
      gJoints[n].position.copy(v);
    });
    gLinks.forEach((m, n) => {
      if (!m) return;
      const a = gp[gemx.parents[n]], b = gp[n];
      dir.subVectors(b, a);
      m.position.copy(a);
      m.scale.set(1, Math.max(dir.length(), 1e-4), 1);
      m.quaternion.setFromUnitVectors(up, dir.normalize());
    });
  }

  // Frame sampling with interpolation between stored frames.
  const qa = new THREE.Quaternion(), qb = new THREE.Quaternion();
  function sample(t) {
    const f = Math.max(0, Math.min(frames.length - 1, (t / tEnd) * (frames.length - 1)));
    const i = Math.floor(f), j = Math.min(frames.length - 1, i + 1), k = f - i;
    return { a: frames[i], b: frames[j], k };
  }
  function handsAt(fr) {
    const L = fr.b[handIdx[0]], R = fr.b[handIdx[1]];
    return new THREE.Vector3((L[0] + R[0]) / 2, (L[1] + R[1]) / 2, (L[2] + R[2]) / 2);
  }

  let fixShown = 0;
  let current = 0;
  const elbowWorld = new THREE.Vector3();
  function pose(t) {
    current = t;
    poseGemx(t);
    if (video && video.readyState >= 1 && !video.seeking) {
      const vt = t + (parseFloat(video.dataset.offset || "0") || 0);
      if (Math.abs(video.currentTime - vt) > 0.04) video.currentTime = vt;
    }
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

    const showFix = fixShown > 0;
    model.traverse((o) => {
      if (!o.isMesh || o.userData.overlay) return;
      let body = o;
      while (body && !fixBones.has(body.name) && body !== model) body = body.parent;
      o.material = showFix && body && fixBones.has(body.name) ? fixMat : boneMat;
    });
    const elbow = a.elbow + (b.elbow - a.elbow) * k;
    if (readout) readout.textContent = "Lead elbow " + Math.round(elbow) + "°";
    const ulna = model.getObjectByName("ulna_l");
    if (ulna) ulna.getWorldPosition(elbowWorld);
    if (tag) {
      tag.textContent = Math.round(elbow) + "°";
      tag.style.opacity = String(fixShown);
    }
    if (range) {
      range.value = t.toFixed(2);
      range.setAttribute("aria-valuetext", phaseAt(t) + ", lead elbow " + Math.round(elbow) + " degrees");
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
    const span = gemx ? 2.2 : 1.05; // how wide the lineup is, in metres
    const dist = Math.max(4.9, (4.9 * span) / Math.min(span, camera.aspect * 1.1));
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

  // Line the two models up side by side on one ground, GEM-X left and OpenSim right, as seen by the camera.
  const p0 = frames[0].b[data.bodies.indexOf("pelvis")];
  const yaw0 = 0.42;
  const right = new THREE.Vector3(Math.cos(yaw0), 0, -Math.sin(yaw0));
  const gap = gemx ? 0.62 : 0;
  model.position.copy(right).multiplyScalar(gap);
  gemxGroup.position.copy(right).multiplyScalar(-gap);
  target.set(p0[0], 0.92, p0[2]);
  ground.position.set(p0[0], 0.001, p0[2]);
  ground.rotation.z = yaw0;

  // Phases
  const phaseAt = (t) => { let n = ""; data.phases.forEach((p) => { if (t >= p[1] - 1e-6) n = p[0]; }); return n; };
  const items = [];
  data.phases.forEach((p) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = p[0];
    b.addEventListener("click", () => { stop(); fixShown = 1; pose(Math.min(tEnd, (p[1] + p[2]) / 2)); });
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
  range.min = "0";
  range.max = tEnd.toFixed(2);
  range.step = "0.01";
  range.addEventListener("input", () => { stop(); fixShown = 1; pose(parseFloat(range.value)); });

  // The one orchestrated moment.
  let raf = 0;
  const ease = (x) => (x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2);
  function stop() { if (raf) cancelAnimationFrame(raf); raf = 0; root.classList.remove("is-playing"); }
  function play() {
    stop();
    fixShown = 0;
    root.classList.add("is-playing");
    const swingMs = 2800, fixMs = 500;
    let t0 = 0;
    const step = (now) => {
      if (!t0) t0 = now;
      const e = now - t0;
      if (e < swingMs) pose(tTop * ease(e / swingMs));
      else {
        fixShown = Math.min(1, (e - swingMs) / fixMs);
        pose(tTop);
        if (fixShown >= 1) { stop(); return; }
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
  }
  replay.addEventListener("click", play);

  fit();
  root.classList.add("is-ready");
  if (reduced) { fixShown = 1; pose(tTop); return; }
  pose(0);
  const io = new IntersectionObserver((entries) => {
    if (entries.some((x) => x.isIntersecting)) { io.disconnect(); setTimeout(play, 300); }
  }, { threshold: 0.35 });
  io.observe(stage);
}
