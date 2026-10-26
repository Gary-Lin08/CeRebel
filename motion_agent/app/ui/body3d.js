/* 3D body for one swing: the OpenSim bones and muscles from /api/smplx_mesh_clip, posed at the
   time the page gives it (the report video's), with the bones of the current fix in amber.

   The app page imports this file only when someone opens the 3D view, and three.js only loads
   inside mount(), from the same local files the old page's close-up uses. Usage:

     const view = await mount(container, { sessionId, finding, getTime, t0, t1 });
     view.setFinding(f); view.setMuscles(false); view.setCloseUp(true);
     dispose();   // or view.dispose(): frees every geometry, material and the WebGL context

   mount() rejects with an Error whose userMessage can be shown as is; a mount cut short by
   dispose() rejects with error.aborted set, which the caller can ignore. */

const FIX = "#F2A541";
const BONE = "#E3E0D4";
const MUSCLE = "#C29A90";
const GROUND = 0x0a1512;
const FOV = 35;
const MAX_FRAMES = 180;

let libs = null;
let current = null;

// The page's login header: app.js (and the old page) define authHeaders() globally.
function authHeaders() {
  return typeof window.authHeaders === "function" ? window.authHeaders() : {};
}

function loadThree() {
  if (!libs) {
    libs = Promise.all([
      import("/static/three/three.module.js"),
      import("/static/three/OrbitControls.js"),
      import("/static/three/GLTFLoader.js"),
    ]).then(([THREE, controls, gltf]) => ({ THREE, OrbitControls: controls.OrbitControls, GLTFLoader: gltf.GLTFLoader }));
    libs.catch(() => { libs = null; });
  }
  return libs;
}

function failure(userMessage) {
  const err = new Error(userMessage);
  err.userMessage = userMessage;
  return err;
}

function aborted() {
  const err = new Error("The 3D view was closed while it loaded.");
  err.aborted = true;
  return err;
}

// ------------------------------------------------------------------ pure helpers (tested in Node)

// Index of the frame nearest to t; frames are sorted by time.
function frameIndexAt(times, t) {
  const n = times.length;
  if (!n) return -1;
  if (!(t > times[0])) return 0;
  if (t >= times[n - 1]) return n - 1;
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t) lo = mid;
    else hi = mid;
  }
  return t - times[lo] <= times[hi] - t ? lo : hi;
}

// What the view marks for a fix: the bones in amber, the body part it zooms to, the camera side.
function focusOf(finding) {
  const focus = (finding && finding.focus) || {};
  const bones = Array.isArray(focus.bones) && focus.bones.length ? focus.bones.map(String) : ["pelvis"];
  const yaw = Number(focus.view_yaw);
  const start = Number(finding && finding.start_time);
  const end = Number(finding && finding.end_time);
  return {
    anchor: String(focus.anchor || bones[0]),
    bones,
    yaw: Number.isFinite(yaw) ? yaw : 0,
    label: String(focus.label || (finding && finding.title) || "Body"),
    window: Number.isFinite(start) && Number.isFinite(end) && end > start ? [start, end] : null,
  };
}

// Camera distance that fits a box of height h and width w in view, with some room around it.
function fitDistance(h, w, aspect, fovDeg) {
  const half = (Math.max(fovDeg, 1) * Math.PI) / 360;
  const tanV = Math.tan(half);
  const tanH = tanV * Math.max(aspect, 0.1);
  return Math.max((h * 0.52) / tanV, (w * 0.52) / tanH);
}

function dedupePoints(points, minDist) {
  const out = [];
  (points || []).forEach((p) => {
    const x = Number(p && p[0]) || 0;
    const y = Number(p && p[1]) || 0;
    const z = Number(p && p[2]) || 0;
    const prev = out[out.length - 1];
    if (prev) {
      const dx = x - prev[0];
      const dy = y - prev[1];
      const dz = z - prev[2];
      if (dx * dx + dy * dy + dz * dz < minDist * minDist) return;
    }
    out.push([x, y, z]);
  });
  return out;
}

// Tube resolution for a muscle path, fixed per muscle so its buffers are reused frame to frame.
function tubeSegments(pointCount) {
  return Math.max(16, Math.min(48, (Math.max(pointCount, 2) - 1) * 8));
}

// ------------------------------------------------------------------ muscles

const RADIAL = 7;

// A tube with room for `segments` rings; writeMuscle() fills it in place for every frame.
function muscleGeometry(THREE, segments) {
  const ring = RADIAL + 1;
  const count = (segments + 1) * ring;
  const geometry = new THREE.BufferGeometry();
  const pos = new THREE.BufferAttribute(new Float32Array(count * 3), 3);
  const nrm = new THREE.BufferAttribute(new Float32Array(count * 3), 3);
  pos.setUsage(THREE.DynamicDrawUsage);
  nrm.setUsage(THREE.DynamicDrawUsage);
  geometry.setAttribute("position", pos);
  geometry.setAttribute("normal", nrm);
  const index = [];
  for (let i = 0; i < segments; i += 1) {
    for (let j = 0; j < RADIAL; j += 1) {
      const a = i * ring + j;
      const b = (i + 1) * ring + j;
      index.push(a, b, a + 1, b, b + 1, a + 1);
    }
  }
  geometry.setIndex(index);
  geometry.userData.segments = segments;
  return geometry;
}

// Fusiform belly along the muscle's path: thick in the middle, tapered to tendons at both ends
// (the same shape as the old page's close-up). Returns false when the path is too short to draw.
function writeMuscle(THREE, geometry, points, scratch) {
  const cleaned = dedupePoints(points, 0.002);
  if (cleaned.length < 2) return false;
  const vectors = cleaned.map((p) => new THREE.Vector3(p[0], p[1], p[2]));
  if (vectors.length === 2) vectors.splice(1, 0, vectors[0].clone().lerp(vectors[1], 0.5));
  const curve = new THREE.CatmullRomCurve3(vectors, false, "centripetal", 0.5);
  const segments = geometry.userData.segments;
  let frames;
  try {
    frames = curve.computeFrenetFrames(segments, false);
  } catch (_) {
    return false;
  }
  const length = Math.max(curve.getLength(), 0.02);
  const belly = THREE.MathUtils.clamp(0.01 + length * 0.028, 0.012, 0.024);
  const tip = belly * 0.22;
  const pos = geometry.attributes.position.array;
  const nrm = geometry.attributes.normal.array;
  const p = scratch.p;
  const n = scratch.n;
  for (let i = 0; i <= segments; i += 1) {
    const u = i / segments;
    const radius = tip + (belly - tip) * Math.pow(Math.sin(Math.PI * u), 1.25);
    curve.getPointAt(u, p);
    const N = frames.normals[i];
    const B = frames.binormals[i];
    for (let j = 0; j <= RADIAL; j += 1) {
      const angle = (j / RADIAL) * Math.PI * 2;
      const cos = -Math.cos(angle);
      const sin = Math.sin(angle);
      n.set(cos * N.x + sin * B.x, cos * N.y + sin * B.y, cos * N.z + sin * B.z).normalize();
      const k = (i * (RADIAL + 1) + j) * 3;
      pos[k] = p.x + radius * n.x;
      pos[k + 1] = p.y + radius * n.y;
      pos[k + 2] = p.z + radius * n.z;
      nrm[k] = n.x;
      nrm[k + 1] = n.y;
      nrm[k + 2] = n.z;
    }
  }
  geometry.attributes.position.needsUpdate = true;
  geometry.attributes.normal.needsUpdate = true;
  return true;
}

// ------------------------------------------------------------------ loading

async function fetchClip(sessionId, t0, t1, signal) {
  const url =
    "/api/smplx_mesh_clip?video_id=" + encodeURIComponent(sessionId) +
    "&t0=" + encodeURIComponent(String(t0)) +
    "&t1=" + encodeURIComponent(String(t1)) +
    "&max_frames=" + MAX_FRAMES + "&muscles=1";
  let r;
  try {
    r = await fetch(url, { headers: authHeaders(), signal });
  } catch (err) {
    if (signal.aborted) throw aborted();
    throw failure("We couldn't load the 3D body. Check your connection and try again.");
  }
  if (r.status === 401 || r.status === 403) throw failure("Sign in to see this swing in 3D.");
  const data = await r.json().catch(() => null);
  if (signal.aborted) throw aborted();
  if (!r.ok || !data || data.error || !Array.isArray(data.frames) || !data.frames.length || !data.glb_url) {
    throw failure("The 3D body isn't ready for this swing yet.");
  }
  data.frames.sort((a, b) => Number(a.time) - Number(b.time));
  return data;
}

async function fetchModel(GLTFLoader, url, signal) {
  let buffer;
  try {
    const r = await fetch(url, { headers: authHeaders(), signal });
    if (!r.ok) throw new Error(String(r.status));
    buffer = await r.arrayBuffer();
  } catch (err) {
    if (signal.aborted) throw aborted();
    throw failure("We couldn't load the 3D body. Check your connection and try again.");
  }
  if (signal.aborted) throw aborted();
  return new GLTFLoader().parseAsync(buffer, "");
}

function disposeMaterial(material) {
  (Array.isArray(material) ? material : [material]).forEach((m) => {
    if (!m) return;
    Object.keys(m).forEach((key) => {
      if (m[key] && m[key].isTexture) m[key].dispose();
    });
    m.dispose();
  });
}

// ------------------------------------------------------------------ the viewer

export async function mount(container, opts) {
  dispose();
  const viewer = { abort: new AbortController(), cleanups: [], renderer: null, leftover: null, disposed: false };
  viewer.dispose = () => freeViewer(viewer);
  current = viewer;
  try {
    return await build(viewer, container, opts || {});
  } catch (err) {
    freeViewer(viewer);
    throw err;
  }
}

function memoryOf(renderer) {
  const info = renderer.info;
  return { geometries: info.memory.geometries, textures: info.memory.textures, programs: (info.programs || []).length };
}

// Frees in reverse order of creation, then the renderer last, so its counters show what was left.
function freeViewer(viewer) {
  if (viewer.disposed) return viewer.leftover;
  viewer.disposed = true;
  viewer.abort.abort();
  while (viewer.cleanups.length) {
    try { viewer.cleanups.pop()(); } catch (_) { /* keep freeing the rest */ }
  }
  const renderer = viewer.renderer;
  if (renderer) {
    viewer.leftover = memoryOf(renderer);
    renderer.dispose();
    // Hands the WebGL context back right away; browsers cap how many can stay open.
    renderer.forceContextLoss();
    renderer.domElement.remove();
    viewer.renderer = null;
  }
  if (current === viewer) current = null;
  return viewer.leftover;
}

async function build(viewer, container, o) {
  const abort = viewer.abort;
  const cleanups = viewer.cleanups;
  if (!container || !o.sessionId) throw failure("Open a swing first.");
  const getTime = typeof o.getTime === "function" ? o.getTime : () => 0;
  const t0 = Math.max(0, Number(o.t0) || 0);
  const t1 = Number(o.t1) > t0 ? Number(o.t1) : t0 + 600;

  const [lib, clip] = await Promise.all([
    loadThree().catch(() => { throw failure("We couldn't load the 3D viewer. Check your connection and try again."); }),
    fetchClip(o.sessionId, t0, t1, abort.signal),
  ]);
  if (viewer.disposed) throw aborted();
  const { THREE, OrbitControls, GLTFLoader } = lib;
  const gltf = await fetchModel(GLTFLoader, clip.glb_url, abort.signal);
  if (viewer.disposed) {
    gltf.scene.traverse((obj) => {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) disposeMaterial(obj.material);
    });
    throw aborted();
  }

  // Everything below registers how to free itself, so dispose() can run at any point.
  const scene = new THREE.Scene();
  cleanups.push(() => {
    scene.traverse((obj) => { if (obj.geometry) obj.geometry.dispose(); });
    scene.clear();
  });

  const materials = {
    bone: new THREE.MeshStandardMaterial({ color: new THREE.Color(BONE), roughness: 0.5, metalness: 0.04 }),
    fix: new THREE.MeshStandardMaterial({
      color: new THREE.Color(FIX), roughness: 0.42, metalness: 0.04,
      emissive: new THREE.Color(FIX), emissiveIntensity: 0.3,
    }),
    muscle: new THREE.MeshStandardMaterial({
      color: new THREE.Color(MUSCLE), roughness: 0.55, metalness: 0,
      transparent: true, opacity: 0.5, depthWrite: false, side: THREE.DoubleSide,
    }),
  };
  cleanups.push(() => Object.values(materials).forEach((m) => m.dispose()));

  // Bones: each mesh hangs under the body (pelvis, femur_r, ...) the clip moves.
  const model = gltf.scene;
  const bodyNames = (clip.body_names || []).map(String);
  const bodySet = new Set(bodyNames);
  const bodies = new Map();
  const boneMeshes = [];
  model.traverse((obj) => {
    if (bodySet.has(obj.name) && !bodies.has(obj.name)) bodies.set(obj.name, obj);
    if (!obj.isMesh) return;
    let up = obj;
    while (up && !bodySet.has(up.name)) up = up.parent;
    disposeMaterial(obj.material);
    obj.material = materials.bone;
    boneMeshes.push({ mesh: obj, body: up ? up.name : "" });
  });
  scene.add(model);

  const muscles = new THREE.Group();
  muscles.visible = o.muscles !== false;
  const muscleMeshes = new Map();
  const scratch = { p: new THREE.Vector3(), n: new THREE.Vector3() };
  scene.add(muscles);

  const frames = clip.frames;
  const times = frames.map((f) => Number(f.time) || 0);

  function poseBones(frame) {
    const src = (frame && frame.bodies) || {};
    bodies.forEach((obj, name) => {
      const b = src[name];
      if (!b) return;
      const p = b.p || [0, 0, 0];
      const q = b.q || [0, 0, 0, 1];
      obj.position.set(p[0], p[1], p[2]);
      obj.quaternion.set(q[0], q[1], q[2], q[3]);
    });
    model.updateMatrixWorld(true);
  }

  function poseMuscles(frame) {
    const list = (frame && frame.muscles) || [];
    const seen = new Set();
    list.forEach((m) => {
      const pts = m && m.points;
      if (!m || !m.name || !Array.isArray(pts) || pts.length < 2) return;
      let mesh = muscleMeshes.get(m.name);
      if (!mesh) {
        mesh = new THREE.Mesh(muscleGeometry(THREE, tubeSegments(pts.length)), materials.muscle);
        mesh.name = m.name;
        mesh.frustumCulled = false;
        mesh.renderOrder = 1;
        muscles.add(mesh);
        muscleMeshes.set(m.name, mesh);
      }
      mesh.visible = writeMuscle(THREE, mesh.geometry, pts, scratch);
      if (mesh.visible) seen.add(m.name);
    });
    muscleMeshes.forEach((mesh, name) => { if (!seen.has(name)) mesh.visible = false; });
  }

  // Fit the camera to the whole swing: union of the body's box over a few frames.
  const box = new THREE.Box3();
  const step = Math.max(1, Math.floor(frames.length / 6));
  for (let i = 0; i < frames.length; i += step) {
    poseBones(frames[i]);
    box.union(new THREE.Box3().setFromObject(model));
  }
  const size = box.getSize(new THREE.Vector3());
  const floorY = Number.isFinite(box.min.y) ? box.min.y : 0;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
  } catch (_) {
    throw failure("This browser can't draw the 3D body. Try another browser or device.");
  }
  viewer.renderer = renderer;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(GROUND, 1);
  const canvas = renderer.domElement;
  canvas.style.display = "block";
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.setAttribute("aria-hidden", "true");
  container.appendChild(canvas);

  const grid = new THREE.GridHelper(4, 16, 0x2f4a40, 0x1b2f28);
  grid.position.y = floorY;
  grid.material.transparent = true;
  grid.material.opacity = 0.7;
  scene.add(grid);
  cleanups.push(() => grid.material.dispose());

  scene.add(new THREE.HemisphereLight(0xf4f1e6, 0x0d1c18, 1.1));
  const key = new THREE.DirectionalLight(0xfff6e8, 2.2);
  key.position.set(1.6, 3.2, 2.2);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xe8c98a, 0.9);
  rim.position.set(-1.8, 1.4, -1.6);
  scene.add(rim);

  const camera = new THREE.PerspectiveCamera(FOV, 1, 0.02, 60);
  const controls = new OrbitControls(camera, canvas);
  cleanups.push(() => controls.dispose());
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.enablePan = false;
  controls.minDistance = 0.45;
  controls.maxDistance = 8;
  controls.rotateSpeed = 0.8;

  let focus = focusOf(o.finding);
  let closeUp = false;
  let touched = false;
  let zoomTo = null;
  let shown = -1;
  let musclesOn = o.muscles !== false;
  let musclesStale = true;
  controls.addEventListener("start", () => { touched = true; });
  // Turning keeps an animated zoom going; zooming by hand (wheel, pinch) takes over from it.
  const stopZoom = (e) => { if (e.type === "wheel" || (e.touches && e.touches.length > 1)) zoomTo = null; };
  canvas.addEventListener("wheel", stopZoom, { passive: true });
  canvas.addEventListener("touchstart", stopZoom, { passive: true });
  cleanups.push(() => {
    canvas.removeEventListener("wheel", stopZoom);
    canvas.removeEventListener("touchstart", stopZoom);
  });

  function paintFocus() {
    const lit = new Set(focus.bones);
    boneMeshes.forEach(({ mesh, body }) => { mesh.material = lit.has(body) ? materials.fix : materials.bone; });
  }

  function bodyDistance() {
    return fitDistance(Math.max(size.y, 1.2), Math.max(size.x, size.z, 0.8), camera.aspect, FOV);
  }
  // Close enough to fill the view with the fixed part: hips and trunk are bigger than a knee or a hand.
  function closeDistance() {
    const part = focus.anchor === "pelvis" || focus.anchor === "torso" ? 0.62 : 0.46;
    return fitDistance(part, part, camera.aspect, FOV);
  }

  const goal = new THREE.Vector3();
  const delta = new THREE.Vector3();
  function goalPoint(out) {
    const node = (closeUp && bodies.get(focus.anchor)) || bodies.get("pelvis") || model;
    node.getWorldPosition(out);
    if (!closeUp) out.y = floorY + size.y * 0.5;
    return out;
  }

  // Start side-on to the fix (the same yaw the old close-up used), whole body in view.
  function placeCamera() {
    goalPoint(goal);
    const yaw = (focus.yaw * Math.PI) / 180;
    const d = closeUp ? closeDistance() : bodyDistance();
    controls.target.copy(goal);
    camera.position.set(goal.x + Math.sin(yaw) * d, goal.y + d * 0.16, goal.z + Math.cos(yaw) * d);
    controls.update();
  }

  function resize() {
    const w = Math.max(container.clientWidth, 1);
    const h = Math.max(container.clientHeight, 1);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
    if (!touched && !closeUp) placeCamera();
  }
  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
  if (ro) {
    ro.observe(container);
    cleanups.push(() => ro.disconnect());
  } else {
    window.addEventListener("resize", resize);
    cleanups.push(() => window.removeEventListener("resize", resize));
  }

  function clampedTime() {
    const t = Number(getTime());
    return Number.isFinite(t) ? t : times[0];
  }

  let raf = 0;
  cleanups.push(() => window.cancelAnimationFrame(raf));
  function loop() {
    raf = window.requestAnimationFrame(loop);
    const t = clampedTime();
    const i = frameIndexAt(times, t);
    if (i !== shown) {
      shown = i;
      poseBones(frames[i]);
      musclesStale = true;
    }
    if (musclesStale && musclesOn) {
      poseMuscles(frames[shown]);
      musclesStale = false;
    }
    // The target follows the body (or the fixed part in close-up) and the camera keeps its angle.
    goalPoint(goal);
    delta.copy(goal).sub(controls.target).multiplyScalar(0.15);
    controls.target.add(delta);
    camera.position.add(delta);
    if (zoomTo != null) {
      const off = camera.position.clone().sub(controls.target);
      const d = off.length();
      const next = d + (zoomTo - d) * 0.14;
      camera.position.copy(controls.target).add(off.setLength(next));
      if (Math.abs(zoomTo - next) < 0.01) zoomTo = null;
    }
    // The fixed bones glow a little brighter during the moment to fix.
    const live = focus.window && t >= focus.window[0] && t <= focus.window[1];
    const glow = live ? 0.75 : 0.3;
    materials.fix.emissiveIntensity += (glow - materials.fix.emissiveIntensity) * 0.2;
    controls.update();
    renderer.render(scene, camera);
  }

  paintFocus();
  poseBones(frames[frameIndexAt(times, clampedTime())]);
  resize();
  placeCamera();
  raf = window.requestAnimationFrame(loop);

  return {
    setFinding(finding) {
      focus = focusOf(finding);
      paintFocus();
      if (closeUp) zoomTo = closeDistance();
    },
    setMuscles(on) {
      musclesOn = !!on;
      muscles.visible = musclesOn;
      musclesStale = musclesOn;
    },
    setCloseUp(on) {
      closeUp = !!on;
      zoomTo = closeUp ? closeDistance() : bodyDistance();
    },
    hasMuscles: frames.some((f) => Array.isArray(f.muscles) && f.muscles.length > 0),
    // For checks: what the GPU holds now, or what was left when the view was disposed (all zero).
    memory: () => (viewer.renderer ? memoryOf(viewer.renderer) : viewer.leftover),
    dispose: viewer.dispose,
  };
}

// Frees the mounted view, or stops one that is still loading.
export function dispose() {
  if (current) current.dispose();
  current = null;
}

export const _test = { frameIndexAt, focusOf, fitDistance, dedupePoints, tubeSegments };
