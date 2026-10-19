// Loaded via <script type="text/babel"> after the React/ReactDOM UMD builds —
// no bundler here, so pull hooks off the global React instead of `import`.
const { useState, useRef, useEffect } = React;

/* =========================================================
   Cerebel Motion Lab — full prototype
   Covers Flows A-F from the UX spec.
   Visual system: near-black + lime accent (matches deck).
========================================================= */

const COLORS = {
  void: "#0A0B0A",
  panel: "#121410",
  panelAlt: "#15170F",
  line: "#262920",
  lineSoft: "#1A1C15",
  ink: "#F1F3EA",
  inkDim: "#878C7C",
  signal: "#C6F24E",
  signalDim: "#7C8F3E",
  caution: "#FF9B57",
  cautionDim: "#4A3822",
  danger: "#FF8D9A",
  dangerDim: "#4A1F26",
};

const SPORTS = ["Snowboarding", "Basketball", "Running", "Skiing", "Other"];

/* ---------------- real backend API (CerebelServer/web_api.py) ---------------- */

const API_BASE = window.CEREBEL_API_BASE || "";

async function apiCreateSession(blob, sport, movement) {
  const qs = `sport=${encodeURIComponent(sport)}&movement=${encodeURIComponent(movement || "")}`;
  const res = await fetch(`${API_BASE}/api/sessions?${qs}`, {
    method: "POST",
    headers: { "Content-Type": blob.type || "video/mp4" },
    body: blob,
  });
  if (!res.ok) throw new Error(`upload failed (${res.status})`);
  return res.json();
}

async function apiSessionStatus(sessionId) {
  const res = await fetch(`${API_BASE}/api/sessions/${sessionId}/status`);
  return res.json();
}

async function apiSessionResults(sessionId) {
  const res = await fetch(`${API_BASE}/api/sessions/${sessionId}/results`);
  if (!res.ok) throw new Error(`results not ready (${res.status})`);
  return res.json();
}

async function apiListSessions() {
  const res = await fetch(`${API_BASE}/api/sessions`);
  return res.json();
}

const PROCESSING_STAGES = [
  { key: "uploading", label: "上传视频" },
  { key: "pose_estimation", label: "识别动作（GEM-X）" },
  { key: "biomechanics_ik", label: "生成生物力学模型（OpenSim IK）" },
  { key: "ready", label: "生成分析结果" },
];

const PHASES = [
  { key: "approach", label: "Approach", start: 0, end: 0.25 },
  { key: "takeoff", label: "Takeoff", start: 0.25, end: 0.42 },
  { key: "air", label: "Air", start: 0.42, end: 0.74 },
  { key: "landing", label: "Landing", start: 0.74, end: 1.0 },
];

const METRIC_DEFS = [
  { key: "knee", label: "Knee flexion", unit: "\u00B0", confidence: "Reconstructed",
    keyframes: [[0, 12], [0.25, 28], [0.42, 46], [0.6, 51], [0.74, 63], [0.85, 39], [1, 22]] },
  { key: "hip", label: "Hip rotation speed", unit: "\u00B0/s", confidence: "Reconstructed",
    keyframes: [[0, 10], [0.25, 140], [0.42, 310], [0.6, 250], [0.74, 90], [1, 20]] },
  { key: "com", label: "Center-of-mass offset", unit: " cm", confidence: "Estimated",
    keyframes: [[0, 0], [0.42, 2.1], [0.6, 4.0], [0.74, 6.4], [0.85, 3.2], [1, 1.0]], scalable: true },
];

const POSE_KEYFRAMES = [
  { frac: 0.0, pts: { head: [0.5, 0.12], sh: [0.5, 0.24], hip: [0.5, 0.48], kneeL: [0.44, 0.68], kneeR: [0.56, 0.68], ankL: [0.42, 0.92], ankR: [0.58, 0.92] } },
  { frac: 0.25, pts: { head: [0.5, 0.1], sh: [0.5, 0.22], hip: [0.5, 0.44], kneeL: [0.4, 0.6], kneeR: [0.6, 0.62], ankL: [0.35, 0.9], ankR: [0.62, 0.9] } },
  { frac: 0.42, pts: { head: [0.52, 0.16], sh: [0.52, 0.28], hip: [0.5, 0.42], kneeL: [0.4, 0.5], kneeR: [0.6, 0.5], ankL: [0.36, 0.62], ankR: [0.64, 0.6] } },
  { frac: 0.58, pts: { head: [0.55, 0.1], sh: [0.53, 0.22], hip: [0.48, 0.34], kneeL: [0.42, 0.4], kneeR: [0.58, 0.42], ankL: [0.4, 0.48], ankR: [0.62, 0.5] } },
  { frac: 0.74, pts: { head: [0.44, 0.14], sh: [0.46, 0.26], hip: [0.5, 0.48], kneeL: [0.42, 0.66], kneeR: [0.6, 0.68], ankL: [0.38, 0.9], ankR: [0.66, 0.92] } },
  { frac: 1.0, pts: { head: [0.5, 0.12], sh: [0.5, 0.24], hip: [0.5, 0.46], kneeL: [0.44, 0.66], kneeR: [0.56, 0.66], ankL: [0.42, 0.9], ankR: [0.58, 0.9] } },
];

function lerp(a, b, t) { return a + (b - a) * t; }
function interpKeyframes(keyframes, frac) {
  if (frac <= keyframes[0][0]) return keyframes[0][1];
  for (let i = 0; i < keyframes.length - 1; i++) {
    const [f0, v0] = keyframes[i], [f1, v1] = keyframes[i + 1];
    if (frac >= f0 && frac <= f1) { const t = f1 === f0 ? 0 : (frac - f0) / (f1 - f0); return lerp(v0, v1, t); }
  }
  return keyframes[keyframes.length - 1][1];
}
function interpPose(frac) {
  const kfs = POSE_KEYFRAMES;
  if (frac <= kfs[0].frac) return kfs[0].pts;
  for (let i = 0; i < kfs.length - 1; i++) {
    const a = kfs[i], b = kfs[i + 1];
    if (frac >= a.frac && frac <= b.frac) {
      const t = b.frac === a.frac ? 0 : (frac - a.frac) / (b.frac - a.frac);
      const out = {};
      for (const k of Object.keys(a.pts)) out[k] = [lerp(a.pts[k][0], b.pts[k][0], t), lerp(a.pts[k][1], b.pts[k][1], t)];
      return out;
    }
  }
  return kfs[kfs.length - 1].pts;
}
function phaseAt(frac) { return PHASES.find((p) => frac >= p.start && frac <= p.end) || PHASES[PHASES.length - 1]; }
function fmtTime(s) { if (!isFinite(s)) return "0:00.0"; const m = Math.floor(s / 60); const rem = (s % 60).toFixed(1); return `${m}:${rem.padStart(4, "0")}`; }
function uid() { return Math.random().toString(36).slice(2, 9); }

/* ---------------- shared UI atoms ---------------- */

const ConfidenceTag = ({ level }) => {
  const isCaution = level === "Estimated" || level === "AI interpretation" || level === "Sample";
  return (
    <span style={{
      fontFamily: "'IBM Plex Mono', monospace", fontSize: "10px", letterSpacing: "0.02em",
      padding: "2px 6px", borderRadius: "3px",
      color: isCaution ? COLORS.caution : COLORS.signalDim,
      border: `1px solid ${isCaution ? COLORS.cautionDim : "#33391E"}`,
      background: isCaution ? "rgba(255,155,87,0.06)" : "rgba(198,242,78,0.05)",
      whiteSpace: "nowrap",
    }}>{level}</span>
  );
};

const StatusBadge = ({ status }) => {
  const map = {
    uploading: { label: "正在上传", color: COLORS.inkDim, border: COLORS.line },
    analyzing: { label: "正在分析", color: COLORS.signal, border: "#33391E" },
    complete: { label: "分析完成", color: COLORS.signal, border: "#33391E" },
    insufficient: { label: "视频条件不足", color: COLORS.caution, border: COLORS.cautionDim },
    uploadFailed: { label: "上传失败", color: COLORS.danger, border: COLORS.dangerDim },
    analysisFailed: { label: "分析失败", color: COLORS.danger, border: COLORS.dangerDim },
  };
  const s = map[status] || map.complete;
  return (
    <span style={{ fontSize: "11px", padding: "2px 8px", borderRadius: "3px", border: `1px solid ${s.border}`, color: s.color }}>
      {s.label}
    </span>
  );
};

const PrimaryButton = ({ children, onClick, disabled, style }) => (
  <button onClick={onClick} disabled={disabled} style={{
    width: "100%", padding: "12px", borderRadius: "5px", border: "none",
    background: disabled ? "#3a3f2c" : COLORS.signal, color: disabled ? "#7a7f6c" : COLORS.void,
    fontWeight: 600, fontSize: "14px", cursor: disabled ? "not-allowed" : "pointer", ...style,
  }}>{children}</button>
);

const SecondaryButton = ({ children, onClick, style, danger }) => (
  <button onClick={onClick} style={{
    padding: "10px 14px", borderRadius: "5px", border: `1px solid ${danger ? COLORS.dangerDim : COLORS.line}`,
    background: "transparent", color: danger ? COLORS.danger : COLORS.ink, fontSize: "13px", cursor: "pointer", ...style,
  }}>{children}</button>
);

const TopBar = ({ title, onBack, right }) => (
  <div className="flex items-center justify-between" style={{ padding: "16px 20px", borderBottom: `1px solid ${COLORS.line}` }}>
    <div className="flex items-center gap-3">
      {onBack && (
        <button onClick={onBack} style={{ background: "none", border: "none", color: COLORS.inkDim, fontSize: "18px", cursor: "pointer", padding: 0 }}>
          {"\u2190"}
        </button>
      )}
      <span style={{ fontSize: "15px", fontWeight: 600 }}>{title}</span>
    </div>
    <div>{right}</div>
  </div>
);

const BottomNav = ({ current, onNav }) => {
  const items = [
    { key: "home", label: "首页" },
    { key: "history", label: "训练记录" },
    { key: "settings", label: "设置" },
  ];
  return (
    <div className="flex" style={{ borderTop: `1px solid ${COLORS.line}`, background: COLORS.void }}>
      {items.map((it) => (
        <button key={it.key} onClick={() => onNav(it.key)} style={{
          flex: 1, padding: "12px 0", background: "none", border: "none", cursor: "pointer",
          color: current === it.key ? COLORS.signal : COLORS.inkDim, fontSize: "12px",
        }}>{it.label}</button>
      ))}
    </div>
  );
};

const Modal = ({ children, onClose }) => (
  <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", display: "flex", alignItems: "flex-end", justifyContent: "center", zIndex: 50 }} onClick={onClose}>
    <div onClick={(e) => e.stopPropagation()} style={{ background: COLORS.panel, borderTop: `1px solid ${COLORS.line}`, borderRadius: "10px 10px 0 0", padding: "20px", width: "100%", maxWidth: "420px" }}>
      {children}
    </div>
  </div>
);

const Toast = ({ message }) => !message ? null : (
  <div style={{ position: "absolute", bottom: "76px", left: "50%", transform: "translateX(-50%)", background: COLORS.panelAlt, border: `1px solid ${COLORS.line}`, borderRadius: "6px", padding: "8px 16px", fontSize: "13px", zIndex: 60 }}>
    {message}
  </div>
);

function StickFigure({ pts, dim }) {
  const seg = (p1, p2) => (
    <line x1={pts[p1][0] * 100} y1={pts[p1][1] * 100} x2={pts[p2][0] * 100} y2={pts[p2][1] * 100}
      stroke={COLORS.signal} strokeWidth="1.4" strokeLinecap="round" opacity={dim ? 0.35 : 0.9} />
  );
  return (
    <svg viewBox="0 0 100 100" style={{ width: "100%", height: "100%" }}>
      <line x1="0" y1="97" x2="100" y2="97" stroke={COLORS.line} strokeWidth="0.5" />
      {seg("hip", "sh")}{seg("sh", "kneeL")}{seg("sh", "kneeR")}{seg("hip", "kneeL")}{seg("hip", "kneeR")}{seg("kneeL", "ankL")}{seg("kneeR", "ankR")}
      <circle cx={pts.head[0] * 100} cy={pts.head[1] * 100} r="4" fill="none" stroke={COLORS.signal} strokeWidth="1.4" opacity={dim ? 0.35 : 1} />
      {[pts.sh, pts.hip, pts.kneeL, pts.kneeR, pts.ankL, pts.ankR].map((p, i) => (
        <circle key={i} cx={p[0] * 100} cy={p[1] * 100} r="1.3" fill={COLORS.ink} opacity={dim ? 0.35 : 1} />
      ))}
    </svg>
  );
}

function EvidenceOverlay({ pose, toggles, phaseLabel }) {
  const hip = pose.hip, ankMidX = (pose.ankL[0] + pose.ankR[0]) / 2;
  return (
    <svg viewBox="0 0 100 100" style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}>
      {toggles.centerline && <line x1={ankMidX * 100} y1="0" x2={ankMidX * 100} y2="100" stroke={COLORS.signal} strokeWidth="0.6" strokeDasharray="2,2" opacity="0.8" />}
      {toggles.positionMarkers && [pose.sh, pose.hip, pose.kneeL, pose.kneeR, pose.ankL, pose.ankR].map((p, i) => (
        <circle key={i} cx={p[0] * 100} cy={p[1] * 100} r="1.1" fill={COLORS.signal} opacity="0.85" />
      ))}
      {toggles.hipMarker && <circle cx={hip[0] * 100} cy={hip[1] * 100} r="2.4" fill="none" stroke={COLORS.caution} strokeWidth="1" />}
      {toggles.direction && (
        <line x1={hip[0] * 100} y1={hip[1] * 100} x2={hip[0] * 100 + (ankMidX - 0.5) * 60} y2={hip[1] * 100 - 8}
          stroke={COLORS.caution} strokeWidth="1" markerEnd="url(#arrow)" />
      )}
      <defs>
        <marker id="arrow" markerWidth="6" markerHeight="6" refX="3" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill={COLORS.caution} />
        </marker>
      </defs>
      {toggles.keyframeRange && (
        <text x="4" y="8" fontSize="4" fill={COLORS.signal} style={{ fontFamily: "IBM Plex Mono, monospace" }}>{phaseLabel}</text>
      )}
    </svg>
  );
}

/* ---------------- mock data seed ---------------- */

function makeSeedHistory() {
  return [
    { id: "h1", movement: "Backside 360", sport: "Snowboarding", date: "Sep 10", status: "complete", videoURL: null, duration: 8, comScale: 0.63, limitation: null, nextFocus: "Keep hips centered through impact." },
    { id: "h2", movement: "Backside 360", sport: "Snowboarding", date: "Sep 6", status: "complete", videoURL: null, duration: 8, comScale: 1.33, limitation: "Estimated from one camera view.", nextFocus: "Land with a wider stance for balance." },
    { id: "h3", movement: "Jump shot", sport: "Basketball", date: "Sep 3", status: "insufficient", videoURL: null, reason: "落地阶段部分被遮挡，画面中人物被场边观众挡住。", suggestion: "建议更换拍摄角度，确保落地全程不被遮挡。" },
    { id: "h4", movement: "Sprint start", sport: "Running", date: "Sep 1", status: "analysisFailed", videoURL: null, reason: "服务端处理超时。" },
    { id: "h5", movement: "Backside 360", sport: "Snowboarding", date: "Aug 28", status: "uploadFailed", videoURL: null, reason: "网络中断，上传未完成。" },
  ];
}

function buildFinding(record) {
  const comScale = record.comScale ?? 1;
  const peak = (6.4 * comScale).toFixed(1);
  return `On landing, the center of mass drifts roughly ${peak} cm left of the base of support.`;
}

/* ---------------- main app ---------------- */

function App() {
  const [screen, setScreen] = useState("home");
  const [prevScreen, setPrevScreen] = useState("home");
  const [history, setHistory] = useState(makeSeedHistory);
  const [session, setSession] = useState(null);
  const [activeRecordId, setActiveRecordId] = useState(null);
  const [toast, setToast] = useState("");
  const [cameraPermission, setCameraPermission] = useState("unrequested");
  const [showPermModal, setShowPermModal] = useState(false);
  const [pendingAfterPerm, setPendingAfterPerm] = useState(null);
  const [trainingFocus, setTrainingFocus] = useState(null);
  const [compareSel, setCompareSel] = useState([]);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [feedbackCtx, setFeedbackCtx] = useState(null);
  const [notifOpen, setNotifOpen] = useState(false);

  const showToast = (msg) => { setToast(msg); setTimeout(() => setToast(""), 2200); };
  const goto = (s) => { setPrevScreen(screen); setScreen(s); };

  const notifications = [
    { id: "n1", label: "分析完成 · Backside 360", recordId: "h1" },
    { id: "n2", label: "可以开始下一次训练", recordId: "h1" },
    { id: "n3", label: "分析失败 · Sprint start", recordId: "h4" },
  ];

  const videoStreamRef = useRef(null);
  const requestCamera = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      videoStreamRef.current = stream;
      setCameraPermission("granted");
      return true;
    } catch (e) {
      setCameraPermission("denied");
      return false;
    }
  };

  const startCaptureFlow = () => {
    if (cameraPermission === "granted") { goto("guidance"); return; }
    setPendingAfterPerm("guidance");
    setShowPermModal(true);
  };

  const confirmPermission = async () => {
    setShowPermModal(false);
    const ok = await requestCamera();
    if (ok && pendingAfterPerm) goto(pendingAfterPerm);
    else if (!ok) showToast("摄像头权限被拒绝，你仍然可以导入视频");
  };

  const finalizeRecord = (sess, results) => {
    const id = uid();
    const record = {
      id, movement: sess.movement || "Untitled movement", sport: sess.sport, date: "Today",
      status: "complete",
      videoURL: (results && results.video_url) ? `${API_BASE}${results.video_url}` : sess.videoURL,
      duration: (results && results.duration) || sess.duration || 8,
      limitation: (results && results.limitation) || sess.limitation || "Estimated from one camera view.",
      nextFocus: (results && results.finding)
        ? "Address the finding above before your next attempt."
        : "Keep hips square through impact to reduce lateral drift on landing.",
      metrics: results ? results.metrics : undefined,
      keyMoments: results ? results.keyMoments || results.key_moments : undefined,
      finding: results ? results.finding : undefined,
      serverSessionId: sess.serverSessionId,
    };
    setHistory((h) => [record, ...h]);
    setActiveRecordId(id);
    setTrainingFocus(null);
    return record;
  };

  return (
    <div style={{
      background: COLORS.void, color: COLORS.ink, fontFamily: "'IBM Plex Sans', sans-serif",
      minHeight: "700px", maxWidth: "420px", margin: "0 auto", position: "relative",
      border: `1px solid ${COLORS.line}`, borderRadius: "14px", overflow: "hidden", display: "flex", flexDirection: "column",
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');
        * { box-sizing: border-box; }
        input[type=range] { -webkit-appearance: none; background: transparent; width: 100%; }
        input[type=range]::-webkit-slider-runnable-track { height: 3px; background: ${COLORS.line}; border-radius: 2px; }
        input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; margin-top: -5px; width: 12px; height: 12px; border-radius: 50%; background: ${COLORS.signal}; border: none; cursor: pointer; }
        button { font-family: inherit; }
        ::-webkit-scrollbar { width: 4px; }
      `}</style>

      <div style={{ flex: 1, overflowY: "auto", position: "relative" }}>
        {screen === "home" && (
          <HomeScreen
            history={history}
            onCapture={startCaptureFlow}
            onImport={() => goto("import")}
            onOpenRecord={(id) => { setActiveRecordId(id); goto("results"); }}
            onHistory={() => goto("history")}
            cameraPermission={cameraPermission}
            notifications={notifications}
            notifOpen={notifOpen}
            setNotifOpen={setNotifOpen}
            onNotifClick={(n) => {
              setActiveRecordId(n.recordId); setNotifOpen(false);
              const rec = history.find((h) => h.id === n.recordId);
              goto(rec?.status === "complete" ? "results" : "history");
            }}
          />
        )}

        {screen === "guidance" && (
          <GuidanceScreen trainingFocus={trainingFocus} onBack={() => goto("home")} onStart={() => goto("capture")} />
        )}

        {screen === "capture" && (
          <CaptureScreen
            onBack={() => goto("guidance")}
            onUse={(videoURL, duration, blob) => { setSession({ videoURL, duration, blob, source: "capture" }); goto("reviewSport"); }}
          />
        )}

        {screen === "import" && (
          <ImportScreen
            trainingFocus={trainingFocus}
            onBack={() => goto(prevScreen === "trainTask" ? "trainTask" : "home")}
            onUse={(videoURL, duration, blob) => { setSession({ videoURL, duration, blob, source: "import" }); goto("reviewSport"); }}
          />
        )}

        {screen === "reviewSport" && session && (
          <ReviewSportScreen
            session={session}
            onBack={() => goto("home")}
            onNext={async (sport, movement) => {
              const updated = { ...session, sport, movement };
              setSession({ ...updated, uploading: true });
              try {
                const result = await apiCreateSession(updated.blob, sport, movement);
                if (result.error) {
                  setSession({ ...updated, uploading: false, checkResult: { status: "blocked", reason: result.error, suggestion: "请稍后重试。" } });
                } else {
                  setSession({
                    ...updated, uploading: false,
                    serverSessionId: result.session_id,
                    checkResult: { status: result.status, reason: result.reason, suggestion: result.suggestion },
                    pipelineError: result.pipeline_error,
                  });
                }
              } catch (e) {
                setSession({ ...updated, uploading: false, checkResult: { status: "blocked", reason: "上传失败：" + e.message, suggestion: "请检查网络连接后重试。" } });
              }
              goto("check");
            }}
          />
        )}

        {screen === "check" && session && (
          <CheckScreen
            session={session}
            onContinue={(limitation) => { setSession((s) => ({ ...s, limitation })); goto("processing"); }}
            onReshoot={() => goto("guidance")}
            onChooseOther={() => goto("import")}
            onGuidance={() => goto("guidance")}
          />
        )}

        {screen === "processing" && session && (
          <ProcessingScreen
            session={session}
            onDone={async () => {
              try {
                const results = await apiSessionResults(session.serverSessionId);
                finalizeRecord(session, results);
              } catch (e) {
                showToast("获取结果失败：" + e.message);
              }
              setSession(null);
              goto("results");
            }}
            onError={(msg) => { showToast(msg); setSession(null); goto("home"); }}
            onCancel={() => { setSession(null); goto("home"); }}
          />
        )}

        {screen === "results" && activeRecordId && (
          <ResultsScreen
            record={history.find((h) => h.id === activeRecordId)}
            onBack={() => goto(prevScreen === "compareSelect" ? "history" : "home")}
            onTrainNext={(focus) => { setTrainingFocus(focus); goto("trainTask"); }}
            onFeedback={(ctx) => { setFeedbackCtx(ctx); goto("feedback"); }}
            onDelete={() => setConfirmDelete({ type: "record", id: activeRecordId })}
            onShare={() => showToast("链接已复制")}
          />
        )}

        {screen === "trainTask" && trainingFocus && (
          <TrainTaskScreen
            focus={trainingFocus}
            record={history.find((h) => h.id === activeRecordId)}
            onBack={() => goto("results")}
            onRecord={startCaptureFlow}
            onImport={() => goto("import")}
            onMarkPracticed={() => { showToast("已标记为已练习"); goto("home"); }}
          />
        )}

        {screen === "history" && (
          <HistoryScreen
            history={history}
            compareSel={compareSel}
            onToggleSel={(id) => setCompareSel((sel) => {
              if (sel.includes(id)) return sel.filter((s) => s !== id);
              if (sel.length >= 2) { showToast("最多同时对比2条"); return sel; }
              return [...sel, id];
            })}
            onOpen={(rec) => {
              setActiveRecordId(rec.id);
              if (rec.status === "complete") goto("results");
              else goto("historyDetail");
            }}
            onCompare={() => goto("compare")}
            onRetry={(id) => {
              setHistory((h) => h.map((r) => (r.id === id ? { ...r, status: "analyzing" } : r)));
              setTimeout(() => {
                setHistory((h) => h.map((r) => (r.id === id ? { ...r, status: "complete", comScale: 1, limitation: "Estimated from one camera view.", nextFocus: "Keep hips square through impact." } : r)));
                showToast("重新提交分析完成");
              }, 2200);
            }}
          />
        )}

        {screen === "historyDetail" && activeRecordId && (
          <HistoryDetailBlocked
            record={history.find((h) => h.id === activeRecordId)}
            onBack={() => goto("history")}
            onReshoot={() => goto("guidance")}
            onChooseOther={() => goto("import")}
            onGuidance={() => goto("guidance")}
          />
        )}

        {screen === "compare" && compareSel.length === 2 && (
          <CompareScreen
            recordA={history.find((h) => h.id === compareSel[0])}
            recordB={history.find((h) => h.id === compareSel[1])}
            onBack={() => { setPrevScreen("compareSelect"); goto("history"); }}
          />
        )}

        {screen === "feedback" && feedbackCtx && (
          <FeedbackScreen
            ctx={feedbackCtx}
            record={history.find((h) => h.id === feedbackCtx.recordId)}
            onBack={() => goto(prevScreen)}
            onSubmit={() => goto("feedbackDone")}
          />
        )}

        {screen === "feedbackDone" && <FeedbackDoneScreen onBack={() => goto("results")} />}

        {screen === "settings" && <SettingsScreen onDeleteAllRecords={() => setConfirmDelete({ type: "allRecords" })} />}

        <Toast message={toast} />

        {showPermModal && (
          <Modal onClose={() => setShowPermModal(false)}>
            <div style={{ fontSize: "15px", fontWeight: 600, marginBottom: "8px" }}>需要访问摄像头</div>
            <p style={{ fontSize: "13px", color: COLORS.inkDim, lineHeight: 1.6, marginBottom: "16px" }}>
              Cerebel 需要使用摄像头来录制你的训练视频。视频仅用于生成动作分析，你可以随时在设置中删除。
            </p>
            <PrimaryButton onClick={confirmPermission}>继续</PrimaryButton>
            <div style={{ height: "8px" }} />
            <SecondaryButton onClick={() => setShowPermModal(false)} style={{ width: "100%" }}>暂不</SecondaryButton>
          </Modal>
        )}

        {confirmDelete && (
          <Modal onClose={() => setConfirmDelete(null)}>
            <div style={{ fontSize: "15px", fontWeight: 600, marginBottom: "8px" }}>
              {confirmDelete.type === "allRecords" ? "删除所有训练记录？" : "删除此次分析？"}
            </div>
            <p style={{ fontSize: "13px", color: COLORS.inkDim, lineHeight: 1.6, marginBottom: "16px" }}>
              此操作无法撤销，相关视频和分析结论都会被永久删除。
            </p>
            <SecondaryButton danger onClick={() => {
              if (confirmDelete.type === "allRecords") { setHistory([]); showToast("已删除所有训练记录"); goto("home"); }
              else { setHistory((h) => h.filter((r) => r.id !== confirmDelete.id)); showToast("已删除此次分析"); goto("home"); }
              setConfirmDelete(null);
            }} style={{ width: "100%", marginBottom: "8px" }}>确认删除</SecondaryButton>
            <SecondaryButton onClick={() => setConfirmDelete(null)} style={{ width: "100%" }}>取消</SecondaryButton>
          </Modal>
        )}
      </div>

      {["home", "history", "settings"].includes(screen) && <BottomNav current={screen} onNav={goto} />}
    </div>
  );
}

/* ---------------- screens ---------------- */

function HomeScreen({ history, onCapture, onImport, onOpenRecord, onHistory, cameraPermission, notifications, notifOpen, setNotifOpen, onNotifClick }) {
  const recent = history.find((h) => h.status === "complete");
  const isFirstUse = history.length === 0;
  return (
    <div style={{ padding: "20px" }}>
      <div className="flex items-center justify-between" style={{ marginBottom: "20px" }}>
        <span style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: "18px" }}>Cerebel</span>
        <div style={{ position: "relative" }}>
          <button onClick={() => setNotifOpen(!notifOpen)} style={{ background: "none", border: `1px solid ${COLORS.line}`, borderRadius: "5px", padding: "6px 10px", color: COLORS.ink, cursor: "pointer", fontSize: "13px" }}>
            {"\uD83D\uDD14"} {notifications.length}
          </button>
          {notifOpen && (
            <div style={{ position: "absolute", right: 0, top: "36px", background: COLORS.panel, border: `1px solid ${COLORS.line}`, borderRadius: "6px", width: "220px", zIndex: 40 }}>
              {notifications.map((n) => (
                <div key={n.id} onClick={() => onNotifClick(n)} style={{ padding: "10px 12px", fontSize: "12px", borderBottom: `1px solid ${COLORS.lineSoft}`, cursor: "pointer" }}>{n.label}</div>
              ))}
            </div>
          )}
        </div>
      </div>

      {isFirstUse && (
        <div style={{ padding: "24px", border: `1px solid ${COLORS.line}`, borderRadius: "8px", marginBottom: "24px", textAlign: "center" }}>
          <p style={{ fontSize: "13px", color: COLORS.inkDim, lineHeight: 1.6 }}>
            拍一段动作视频，Cerebel 找出具体问题发生的时刻，并给出一项可执行的训练重点。
          </p>
        </div>
      )}

      <div className="flex gap-3" style={{ marginBottom: "24px" }}>
        <div onClick={onCapture} style={{ flex: 1, border: `1px solid ${cameraPermission === "denied" ? COLORS.cautionDim : COLORS.line}`, borderRadius: "8px", padding: "18px 12px", cursor: "pointer", textAlign: "center" }}>
          <div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "4px" }}>拍摄新视频</div>
          {cameraPermission === "denied" && <div style={{ fontSize: "11px", color: COLORS.caution }}>需要摄像头权限</div>}
        </div>
        <div onClick={onImport} style={{ flex: 1, border: `1px solid ${COLORS.line}`, borderRadius: "8px", padding: "18px 12px", cursor: "pointer", textAlign: "center" }}>
          <div style={{ fontSize: "13px", fontWeight: 600 }}>从相册导入</div>
        </div>
      </div>

      {recent && (
        <div onClick={() => onOpenRecord(recent.id)} style={{ border: `1px solid ${COLORS.line}`, borderRadius: "8px", padding: "14px", marginBottom: "20px", cursor: "pointer" }}>
          <div style={{ fontSize: "11px", color: COLORS.inkDim, marginBottom: "6px" }}>最近一次分析</div>
          <div className="flex items-center justify-between">
            <span style={{ fontSize: "13px", fontWeight: 600 }}>{recent.movement}</span>
            <StatusBadge status={recent.status} />
          </div>
        </div>
      )}

      {!isFirstUse && (
        <div>
          <div className="flex items-center justify-between" style={{ marginBottom: "10px" }}>
            <span style={{ fontSize: "13px", fontWeight: 600 }}>训练记录</span>
            <button onClick={onHistory} style={{ background: "none", border: "none", color: COLORS.inkDim, fontSize: "12px", cursor: "pointer" }}>查看全部</button>
          </div>
          {history.slice(0, 3).map((r) => (
            <div key={r.id} onClick={() => onOpenRecord(r.id)} className="flex items-center justify-between" style={{ padding: "10px 0", borderBottom: `1px solid ${COLORS.lineSoft}`, cursor: "pointer" }}>
              <div>
                <div style={{ fontSize: "13px" }}>{r.movement}</div>
                <div style={{ fontSize: "11px", color: COLORS.inkDim }}>{r.date}</div>
              </div>
              <StatusBadge status={r.status} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function GuidanceScreen({ onBack, onStart, trainingFocus }) {
  const items = ["保持滑雪者/运动员全身可见", "尽量使用固定机位", "确保落地过程没有被遮挡", "保留起跳、旋转和落地全过程", "避免距离过远或画面严重晃动"];
  return (
    <div>
      <TopBar title="拍摄指导" onBack={onBack} />
      <div style={{ padding: "20px" }}>
        {trainingFocus && (
          <div style={{ background: "rgba(198,242,78,0.06)", border: "1px solid #33391E", borderRadius: "6px", padding: "10px 12px", marginBottom: "20px", fontSize: "12px" }}>
            本次训练重点：{trainingFocus}
          </div>
        )}
        {items.map((it, i) => (
          <div key={i} className="flex items-start gap-2" style={{ marginBottom: "12px" }}>
            <span style={{ color: COLORS.signal, fontSize: "13px" }}>{"\u2713"}</span>
            <span style={{ fontSize: "13px", color: COLORS.ink }}>{it}</span>
          </div>
        ))}
        <div style={{ marginTop: "24px" }}>
          <PrimaryButton onClick={onStart}>开始录制</PrimaryButton>
        </div>
      </div>
    </div>
  );
}

function CaptureScreen({ onBack, onUse }) {
  const videoRef = useRef(null);
  const [stream, setStream] = useState(null);
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [previewURL, setPreviewURL] = useState(null);
  const [cameraOk, setCameraOk] = useState(true);
  const recorderRef = useRef(null);
  const chunksRef = useRef([]);
  const timerRef = useRef(null);
  const blobRef = useRef(null);

  useEffect(() => {
    let active = true;
    navigator.mediaDevices?.getUserMedia?.({ video: true, audio: false })
      .then((s) => { if (!active) return; setStream(s); if (videoRef.current) videoRef.current.srcObject = s; })
      .catch(() => setCameraOk(false));
    return () => { active = false; stream?.getTracks().forEach((t) => t.stop()); clearInterval(timerRef.current); };
    // eslint-disable-next-line
  }, []);

  const startRec = () => {
    if (!stream) return;
    chunksRef.current = [];
    try {
      const rec = new MediaRecorder(stream);
      rec.ondataavailable = (e) => chunksRef.current.push(e.data);
      rec.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: "video/webm" });
        blobRef.current = blob;
        setPreviewURL(URL.createObjectURL(blob));
      };
      rec.start();
      recorderRef.current = rec;
      setRecording(true);
      setSeconds(0);
      timerRef.current = setInterval(() => setSeconds((s) => s + 1), 1000);
    } catch (e) { setCameraOk(false); }
  };
  const stopRec = () => { recorderRef.current?.stop(); setRecording(false); clearInterval(timerRef.current); };

  if (previewURL) {
    return (
      <div>
        <TopBar title="预览" onBack={() => setPreviewURL(null)} />
        <div style={{ padding: "20px" }}>
          <video src={previewURL} controls style={{ width: "100%", borderRadius: "6px", marginBottom: "16px" }} />
          <PrimaryButton onClick={() => onUse(previewURL, seconds || 8, blobRef.current)}>使用此视频</PrimaryButton>
          <div style={{ height: "8px" }} />
          <SecondaryButton style={{ width: "100%" }} onClick={() => setPreviewURL(null)}>重新拍摄</SecondaryButton>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <TopBar title="录制" onBack={onBack} />
      <div style={{ flex: 1, background: "#000", position: "relative", minHeight: "360px" }}>
        {cameraOk ? (
          <video ref={videoRef} autoPlay muted playsInline style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        ) : (
          <div className="flex items-center justify-center" style={{ height: "100%", color: COLORS.inkDim, fontSize: "13px", padding: "24px", textAlign: "center" }}>
            相机预览在此环境不可用 — 请改用"从相册导入"
          </div>
        )}
        {recording && (
          <div style={{ position: "absolute", top: "12px", left: "12px", display: "flex", alignItems: "center", gap: "6px", background: "rgba(0,0,0,0.5)", padding: "4px 8px", borderRadius: "4px" }}>
            <span style={{ width: "8px", height: "8px", borderRadius: "50%", background: COLORS.danger }} />
            <span style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: "12px", color: COLORS.ink }}>{fmtTime(seconds)}</span>
          </div>
        )}
      </div>
      <div className="flex items-center justify-center" style={{ padding: "24px" }}>
        <button onClick={recording ? stopRec : startRec} disabled={!cameraOk} style={{
          width: "60px", height: "60px", borderRadius: "50%", border: `3px solid ${COLORS.ink}`,
          background: recording ? COLORS.danger : "transparent", cursor: cameraOk ? "pointer" : "not-allowed",
        }} />
      </div>
    </div>
  );
}

function ImportScreen({ onBack, onUse, trainingFocus }) {
  const inputRef = useRef(null);
  const [preview, setPreview] = useState(null);
  const [dur, setDur] = useState(8);
  const [err, setErr] = useState(null);
  const fileRef = useRef(null);

  const handleFile = (file) => {
    if (!file) return;
    if (!file.type.startsWith("video/")) { setErr("不支持的格式，请选择 MP4/MOV/WebM 视频。"); return; }
    setErr(null);
    fileRef.current = file;
    setPreview(URL.createObjectURL(file));
  };

  return (
    <div>
      <TopBar title="导入视频" onBack={onBack} />
      <div style={{ padding: "20px" }}>
        {trainingFocus && (
          <div style={{ background: "rgba(198,242,78,0.06)", border: "1px solid #33391E", borderRadius: "6px", padding: "10px 12px", marginBottom: "20px", fontSize: "12px" }}>
            本次训练重点：{trainingFocus}
          </div>
        )}
        {!preview && (
          <div onClick={() => inputRef.current?.click()} style={{ border: `1px solid ${COLORS.line}`, borderRadius: "6px", padding: "40px 20px", textAlign: "center", cursor: "pointer", background: COLORS.panel }}>
            <div style={{ fontSize: "14px", marginBottom: "6px" }}>点击选择相册中的视频</div>
            <div style={{ fontSize: "12px", color: COLORS.inkDim }}>5–15 秒效果最佳 · MP4 / MOV / WebM</div>
          </div>
        )}
        {err && <div style={{ marginTop: "12px", fontSize: "12px", color: COLORS.danger }}>{err}</div>}
        <input ref={inputRef} type="file" accept="video/*" style={{ display: "none" }} onChange={(e) => handleFile(e.target.files?.[0])} />
        {preview && (
          <div style={{ marginTop: "16px" }}>
            <video src={preview} controls onLoadedMetadata={(e) => setDur(e.target.duration)} style={{ width: "100%", borderRadius: "6px", marginBottom: "16px" }} />
            <PrimaryButton onClick={() => onUse(preview, dur || 8, fileRef.current)}>使用此视频</PrimaryButton>
            <div style={{ height: "8px" }} />
            <SecondaryButton style={{ width: "100%" }} onClick={() => setPreview(null)}>选择其他视频</SecondaryButton>
          </div>
        )}
      </div>
    </div>
  );
}

function ReviewSportScreen({ session, onBack, onNext }) {
  const [sport, setSport] = useState("Snowboarding");
  const [movement, setMovement] = useState("");
  const [submitting, setSubmitting] = useState(false);
  return (
    <div>
      <TopBar title="确认信息" onBack={onBack} />
      <div style={{ padding: "20px" }}>
        <video src={session.videoURL} controls style={{ width: "100%", borderRadius: "6px", marginBottom: "20px" }} />
        <div style={{ marginBottom: "16px" }}>
          <div style={{ fontSize: "13px", color: COLORS.inkDim, marginBottom: "8px" }}>运动</div>
          <div className="flex flex-wrap gap-2">
            {SPORTS.map((s) => (
              <button key={s} onClick={() => setSport(s)} style={{
                padding: "7px 14px", borderRadius: "5px", fontSize: "13px", cursor: "pointer",
                border: `1px solid ${sport === s ? COLORS.signal : COLORS.line}`,
                background: sport === s ? "rgba(198,242,78,0.08)" : "transparent",
                color: sport === s ? COLORS.signal : COLORS.ink,
              }}>{s}</button>
            ))}
          </div>
        </div>
        <div style={{ marginBottom: "24px" }}>
          <div style={{ fontSize: "13px", color: COLORS.inkDim, marginBottom: "8px" }}>动作名称</div>
          <input value={movement} onChange={(e) => setMovement(e.target.value)} placeholder="例如 Backside 360" style={{
            width: "100%", background: COLORS.panel, border: `1px solid ${COLORS.line}`, borderRadius: "5px",
            padding: "10px 12px", color: COLORS.ink, fontSize: "14px", outline: "none",
          }} />
        </div>
        <PrimaryButton
          disabled={submitting}
          onClick={async () => { setSubmitting(true); await onNext(sport, movement); }}
        >{submitting ? "正在上传视频…" : "继续"}</PrimaryButton>
      </div>
    </div>
  );
}

function CheckScreen({ session, onContinue, onReshoot, onChooseOther, onGuidance }) {
  const result = session.checkResult;
  if (!result) return null;
  if (result.status === "blocked") {
    return (
      <div style={{ padding: "20px" }}>
        <TopBar title="视频检查" />
        <div style={{ border: `1px solid ${COLORS.dangerDim}`, borderRadius: "8px", padding: "18px", margin: "20px 0" }}>
          <div style={{ color: COLORS.danger, fontWeight: 600, fontSize: "14px", marginBottom: "8px" }}>无法分析</div>
          <p style={{ fontSize: "13px", color: COLORS.ink, marginBottom: "6px" }}>{result.reason}</p>
          <p style={{ fontSize: "12px", color: COLORS.inkDim }}>{result.suggestion}</p>
        </div>
        <SecondaryButton style={{ width: "100%", marginBottom: "8px" }} onClick={onReshoot}>重新拍摄</SecondaryButton>
        <SecondaryButton style={{ width: "100%", marginBottom: "8px" }} onClick={onChooseOther}>选择其他视频</SecondaryButton>
        <SecondaryButton style={{ width: "100%" }} onClick={onGuidance}>查看拍摄指导</SecondaryButton>
      </div>
    );
  }
  if (result.status === "limited") {
    return (
      <div style={{ padding: "20px" }}>
        <TopBar title="视频检查" />
        <div style={{ border: `1px solid ${COLORS.cautionDim}`, borderRadius: "8px", padding: "18px", margin: "20px 0" }}>
          <div style={{ color: COLORS.caution, fontWeight: 600, fontSize: "14px", marginBottom: "8px" }}>可以分析，但有限制</div>
          <p style={{ fontSize: "13px", color: COLORS.ink }}>{result.reason}</p>
        </div>
        <PrimaryButton onClick={() => onContinue(result.reason)}>继续</PrimaryButton>
        <div style={{ height: "8px" }} />
        <SecondaryButton style={{ width: "100%" }} onClick={onChooseOther}>更换视频</SecondaryButton>
      </div>
    );
  }
  return (
    <div style={{ padding: "20px" }}>
      <TopBar title="视频检查" />
      <div style={{ border: "1px solid #33391E", borderRadius: "8px", padding: "18px", margin: "20px 0" }}>
        <div style={{ color: COLORS.signal, fontWeight: 600, fontSize: "14px" }}>可以分析</div>
      </div>
      {session.pipelineError && (
        <div style={{ border: `1px solid ${COLORS.dangerDim}`, borderRadius: "6px", padding: "10px 12px", marginBottom: "16px", fontSize: "12px", color: COLORS.danger }}>
          后端处理管线不可用：{session.pipelineError}
        </div>
      )}
      <PrimaryButton onClick={() => onContinue(null)}>继续</PrimaryButton>
    </div>
  );
}

function ProcessingScreen({ session, onDone, onError, onCancel }) {
  const [idx, setIdx] = useState(0);
  const [errorMsg, setErrorMsg] = useState(null);
  const stages = PROCESSING_STAGES;

  useEffect(() => {
    const sessionId = session?.serverSessionId;
    if (!sessionId) {
      setErrorMsg("未找到服务端 session（可能后端不可用）。");
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const st = await apiSessionStatus(sessionId);
        if (cancelled) return;
        if (st.stage === "error") {
          setErrorMsg(st.error || "处理失败。");
          return;
        }
        const stageIdx = stages.findIndex((s) => s.key === st.stage);
        if (stageIdx >= 0) setIdx(stageIdx);
        if (st.stage === "ready") {
          onDone();
          return;
        }
        setTimeout(poll, 1500);
      } catch (e) {
        if (!cancelled) setTimeout(poll, 2500);
      }
    };
    poll();
    return () => { cancelled = true; };
    // eslint-disable-next-line
  }, [session?.serverSessionId]);

  if (errorMsg) {
    return (
      <div className="flex flex-col items-center justify-center" style={{ minHeight: "500px", padding: "24px" }}>
        <div style={{ border: `1px solid ${COLORS.dangerDim}`, borderRadius: "8px", padding: "18px", marginBottom: "16px", width: "100%" }}>
          <div style={{ color: COLORS.danger, fontWeight: 600, fontSize: "14px", marginBottom: "8px" }}>分析失败</div>
          <p style={{ fontSize: "13px", color: COLORS.ink, lineHeight: 1.6 }}>{errorMsg}</p>
        </div>
        <SecondaryButton style={{ width: "100%" }} onClick={() => onError(errorMsg)}>返回首页</SecondaryButton>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center" style={{ minHeight: "500px", padding: "24px" }}>
      <div style={{ width: "100%" }}>
        {stages.map((s, i) => {
          const label = s.label;
          const done = i < idx, active = i === idx;
          return (
            <div key={label} className="flex items-center" style={{ padding: "12px 0", borderBottom: i < stages.length - 1 ? `1px solid ${COLORS.lineSoft}` : "none" }}>
              <div style={{
                width: "18px", height: "18px", borderRadius: "50%", marginRight: "14px",
                border: `1px solid ${done || active ? COLORS.signal : COLORS.line}`,
                display: "flex", alignItems: "center", justifyContent: "center", fontSize: "10px",
                color: done ? COLORS.void : COLORS.signal, background: done ? COLORS.signal : "transparent",
              }}>{done ? "\u2713" : active ? "\u25CF" : ""}</div>
              <span style={{ fontSize: "14px", color: done || active ? COLORS.ink : COLORS.inkDim }}>{label}</span>
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: "24px", width: "100%" }}>
        <SecondaryButton style={{ width: "100%" }} onClick={onCancel}>取消</SecondaryButton>
      </div>
    </div>
  );
}

function ResultsScreen({ record, onBack, onTrainNext, onFeedback, onDelete, onShare }) {
  const [scrubTime, setScrubTime] = useState(0);
  const [view, setView] = useState("original");
  const [overlay, setOverlay] = useState({ centerline: true, positionMarkers: true, hipMarker: false, direction: false, keyframeRange: false });
  const videoRef = useRef(null);
  const duration = record.duration || 8;
  const frac = duration > 0 ? Math.min(1, scrubTime / duration) : 0;
  const pose = interpPose(frac);
  const phase = phaseAt(frac);
  const comScale = record.comScale ?? 1;
  // Real records (uploaded through the actual pipeline) carry `metrics` /
  // `keyMoments` / `finding` computed from real OpenSim output — see
  // web_api.py session_results(). Seed/demo history entries fall back to the
  // illustrative interpolated curves so the screen still renders.
  const metrics = record.metrics || METRIC_DEFS.map((m) => ({ ...m, value: interpKeyframes(m.keyframes, frac) * (m.scalable ? comScale : 1) }));
  const keyMoments = record.keyMoments || [{ label: "Takeoff", time: 0.25 * duration }, { label: "Landing", time: 0.74 * duration }];
  const finding = record.finding || buildFinding(record);

  const onScrub = (t) => { setScrubTime(Number(t)); if (videoRef.current) videoRef.current.currentTime = Number(t); };

  return (
    <div>
      <TopBar title={record.movement} onBack={onBack} right={<StatusBadge status={record.status} />} />
      <div style={{ padding: "16px 20px" }}>
        <div className="flex gap-1" style={{ marginBottom: "10px" }}>
          {["original", "reconstruction"].map((t) => (
            <button key={t} onClick={() => setView(t)} style={{
              padding: "6px 12px", fontSize: "12px", borderRadius: "4px", cursor: "pointer",
              border: `1px solid ${COLORS.line}`, background: view === t ? COLORS.lineSoft : "transparent",
              color: view === t ? COLORS.signal : COLORS.inkDim,
            }}>{t === "original" ? "Original" : "Reconstruction"}</button>
          ))}
        </div>

        <div style={{ position: "relative", background: COLORS.panel, borderRadius: "6px", aspectRatio: "4/5", overflow: "hidden" }}>
          {view === "original" ? (
            record.videoURL ? (
              <>
                <video ref={videoRef} src={record.videoURL} onTimeUpdate={(e) => setScrubTime(e.target.currentTime)} muted
                  style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                <EvidenceOverlay pose={pose} toggles={overlay} phaseLabel={phase.label} />
              </>
            ) : (
              <div className="flex items-center justify-center" style={{ height: "100%", padding: "20px", textAlign: "center", fontSize: "12px", color: COLORS.inkDim }}>
                证据视频不可用（可能已达到保存期限或已被删除）。以下为该次分析的重建视图。
              </div>
            )
          ) : (
            <div style={{ width: "70%", height: "90%", margin: "5% auto" }}>
              <StickFigure pts={pose} />
            </div>
          )}
        </div>

        {view === "original" && record.videoURL && (
          <div className="flex flex-wrap gap-2" style={{ marginTop: "10px" }}>
            {[["centerline", "中心线"], ["positionMarkers", "位置标记"], ["hipMarker", "髋部位置"], ["direction", "方向"], ["keyframeRange", "关键帧范围"]].map(([k, label]) => (
              <button key={k} onClick={() => setOverlay((o) => ({ ...o, [k]: !o[k] }))} style={{
                fontSize: "11px", padding: "4px 8px", borderRadius: "4px", cursor: "pointer",
                border: `1px solid ${overlay[k] ? COLORS.signal : COLORS.line}`,
                color: overlay[k] ? COLORS.signal : COLORS.inkDim, background: "transparent",
              }}>{label}</button>
            ))}
          </div>
        )}

        <div style={{ marginTop: "12px" }}>
          <div className="flex" style={{ height: "5px", borderRadius: "3px", overflow: "hidden", marginBottom: "6px" }}>
            {PHASES.map((p) => <div key={p.key} style={{ width: `${(p.end - p.start) * 100}%`, background: phase.key === p.key ? COLORS.signal : COLORS.line }} />)}
          </div>
          <input type="range" min={0} max={duration} step={0.01} value={scrubTime} onChange={(e) => onScrub(e.target.value)} />
          <div className="flex justify-between" style={{ fontSize: "11px", color: COLORS.inkDim, fontFamily: "'IBM Plex Mono', monospace" }}>
            <span>{fmtTime(scrubTime)}</span><span>{phase.label}</span><span>{fmtTime(duration)}</span>
          </div>
        </div>

        <div style={{ marginTop: "18px", borderTop: `1px solid ${COLORS.line}`, paddingTop: "14px" }}>
          <div style={{ fontSize: "12px", color: COLORS.inkDim, marginBottom: "8px" }}>关键时刻</div>
          {keyMoments.map((k) => (
            <button key={k.label} onClick={() => onScrub(k.time)} className="flex items-center justify-between" style={{
              width: "100%", background: "none", border: "none", padding: "6px 0", cursor: "pointer", color: COLORS.ink,
            }}>
              <span style={{ fontSize: "13px" }}>{k.label}</span>
              <span style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: "12px", color: COLORS.signal }}>{k.time.toFixed(2)}s</span>
            </button>
          ))}
        </div>

        <div style={{ marginTop: "14px", borderTop: `1px solid ${COLORS.line}`, paddingTop: "14px" }}>
          <div className="flex items-center justify-between" style={{ marginBottom: "6px" }}>
            <span style={{ fontSize: "13px", fontWeight: 600 }}>主要发现</span>
          </div>
          <p onClick={() => onScrub(0.74 * duration)} style={{ fontSize: "13px", color: COLORS.ink, lineHeight: 1.6, cursor: "pointer", textDecoration: "underline dotted" }}>
            {finding}
          </p>
        </div>

        <div style={{ marginTop: "14px", border: `1px solid ${COLORS.line}`, borderRadius: "6px", padding: "10px 12px" }}>
          <div className="flex items-center justify-between" style={{ marginBottom: "4px" }}>
            <span style={{ fontSize: "12px", color: COLORS.inkDim }}>分析限制</span>
            <ConfidenceTag level="Estimated" />
          </div>
          <p style={{ fontSize: "12px", color: COLORS.inkDim }}>{record.limitation || "Estimated from one camera view."}</p>
        </div>

        <div style={{ marginTop: "14px", borderTop: `1px solid ${COLORS.line}`, paddingTop: "14px" }}>
          <div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "6px" }}>Next Focus</div>
          <p style={{ fontSize: "13px", color: COLORS.ink, lineHeight: 1.6, marginBottom: "14px" }}>{record.nextFocus}</p>
          <PrimaryButton onClick={() => onTrainNext(record.nextFocus)}>Train this next</PrimaryButton>
          <div className="flex gap-2" style={{ marginTop: "10px" }}>
            <SecondaryButton style={{ flex: 1 }} onClick={() => onFeedback({ recordId: record.id, time: 0.74 * duration })}>Something looks wrong</SecondaryButton>
            <SecondaryButton style={{ flex: 1 }} onClick={onShare}>分享</SecondaryButton>
          </div>
          <div style={{ marginTop: "10px" }}>
            <SecondaryButton danger style={{ width: "100%" }} onClick={onDelete}>删除此次分析</SecondaryButton>
          </div>
        </div>

        <div style={{ marginTop: "14px", borderTop: `1px solid ${COLORS.line}`, paddingTop: "14px" }}>
          {metrics.map((m) => (
            <div key={m.key} className="flex items-center justify-between" style={{ padding: "6px 0" }}>
              <span style={{ fontSize: "12px", color: COLORS.inkDim }}>{m.label}</span>
              <div className="flex items-center gap-2">
                <span style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: "12px" }}>{m.value.toFixed(1)}{m.unit}</span>
                <ConfidenceTag level={m.confidence} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function TrainTaskScreen({ focus, record, onBack, onRecord, onImport, onMarkPracticed }) {
  return (
    <div>
      <TopBar title="训练任务" onBack={onBack} />
      <div style={{ padding: "20px" }}>
        <div style={{ fontSize: "12px", color: COLORS.inkDim, marginBottom: "6px" }}>当前训练重点</div>
        <p style={{ fontSize: "14px", marginBottom: "20px", lineHeight: 1.6 }}>{focus}</p>
        {record?.videoURL && <video src={record.videoURL} controls style={{ width: "100%", borderRadius: "6px", marginBottom: "20px" }} />}
        <div style={{ fontSize: "12px", color: COLORS.inkDim, marginBottom: "24px" }}>
          动作提示：落地时视线和髋部朝向下坡方向，保持稳定。
        </div>
        <PrimaryButton onClick={onRecord}>开始下一次录制</PrimaryButton>
        <div style={{ height: "8px" }} />
        <SecondaryButton style={{ width: "100%" }} onClick={onImport}>从相册导入下一次尝试</SecondaryButton>
        <div style={{ height: "8px" }} />
        <SecondaryButton style={{ width: "100%" }} onClick={onMarkPracticed}>标记为已练习</SecondaryButton>
      </div>
    </div>
  );
}

function HistoryScreen({ history, compareSel, onToggleSel, onOpen, onCompare, onRetry }) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const statuses = ["all", "complete", "insufficient", "uploadFailed", "analysisFailed"];
  const filtered = history.filter((r) => (filter === "all" || r.status === filter) && r.movement.toLowerCase().includes(query.toLowerCase()));
  return (
    <div style={{ paddingBottom: compareSel.length === 2 ? "70px" : 0, position: "relative" }}>
      <TopBar title="训练记录" />
      <div style={{ padding: "16px 20px" }}>
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索动作名称"
          style={{ width: "100%", background: COLORS.panel, border: `1px solid ${COLORS.line}`, borderRadius: "5px", padding: "8px 12px", color: COLORS.ink, fontSize: "13px", marginBottom: "10px", outline: "none" }} />
        <div className="flex flex-wrap gap-2" style={{ marginBottom: "16px" }}>
          {statuses.map((s) => (
            <button key={s} onClick={() => setFilter(s)} style={{
              fontSize: "11px", padding: "4px 10px", borderRadius: "4px", cursor: "pointer",
              border: `1px solid ${filter === s ? COLORS.signal : COLORS.line}`,
              color: filter === s ? COLORS.signal : COLORS.inkDim, background: "transparent",
            }}>{s === "all" ? "全部" : s}</button>
          ))}
        </div>

        {filtered.map((r) => (
          <div key={r.id} className="flex items-center gap-2" style={{ padding: "10px 0", borderBottom: `1px solid ${COLORS.lineSoft}` }}>
            <input type="checkbox" checked={compareSel.includes(r.id)} onChange={() => onToggleSel(r.id)} style={{ accentColor: COLORS.signal }} />
            <div onClick={() => onOpen(r)} style={{ flex: 1, cursor: "pointer" }}>
              <div className="flex items-center justify-between">
                <span style={{ fontSize: "13px" }}>{r.movement}</span>
                <StatusBadge status={r.status} />
              </div>
              <div style={{ fontSize: "11px", color: COLORS.inkDim }}>{r.date}{r.nextFocus ? ` · ${r.nextFocus}` : ""}</div>
            </div>
            {(r.status === "uploadFailed" || r.status === "analysisFailed") && (
              <button onClick={() => onRetry(r.id)} style={{ fontSize: "11px", color: COLORS.signal, background: "none", border: "none", cursor: "pointer" }}>重试</button>
            )}
          </div>
        ))}
        {filtered.length === 0 && <div style={{ fontSize: "13px", color: COLORS.inkDim, textAlign: "center", padding: "40px 0" }}>没有匹配的记录</div>}
      </div>

      {compareSel.length === 2 && (
        <div style={{ position: "absolute", bottom: "8px", left: "20px", right: "20px" }}>
          <PrimaryButton onClick={onCompare}>对比这两次训练</PrimaryButton>
        </div>
      )}
    </div>
  );
}

function HistoryDetailBlocked({ record, onBack, onReshoot, onChooseOther, onGuidance }) {
  return (
    <div style={{ padding: "20px" }}>
      <TopBar title={record.movement} onBack={onBack} />
      <div style={{ border: `1px solid ${record.status === "insufficient" ? COLORS.cautionDim : COLORS.dangerDim}`, borderRadius: "8px", padding: "18px", margin: "20px 0" }}>
        <div style={{ marginBottom: "8px" }}><StatusBadge status={record.status} /></div>
        <p style={{ fontSize: "13px", color: COLORS.ink, marginBottom: "6px" }}>{record.reason}</p>
        {record.suggestion && <p style={{ fontSize: "12px", color: COLORS.inkDim }}>{record.suggestion}</p>}
      </div>
      <SecondaryButton style={{ width: "100%", marginBottom: "8px" }} onClick={onReshoot}>重新拍摄</SecondaryButton>
      <SecondaryButton style={{ width: "100%", marginBottom: "8px" }} onClick={onChooseOther}>选择其他视频</SecondaryButton>
      <SecondaryButton style={{ width: "100%" }} onClick={onGuidance}>查看拍摄指导</SecondaryButton>
    </div>
  );
}

function CompareScreen({ recordA, recordB, onBack }) {
  const poseLanding = interpPose(0.74);
  const peakA = (6.4 * (recordA.comScale ?? 1)).toFixed(1);
  const peakB = (6.4 * (recordB.comScale ?? 1)).toFixed(1);
  const focusChanged = recordA.nextFocus !== recordB.nextFocus;
  return (
    <div>
      <TopBar title="前后对比" onBack={onBack} />
      <div style={{ padding: "20px" }}>
        <div className="flex gap-3" style={{ marginBottom: "16px" }}>
          {[{ r: recordA, label: "较新" }, { r: recordB, label: "较早" }].map(({ r, label }) => (
            <div key={r.id} style={{ flex: 1 }}>
              <div style={{ fontSize: "11px", color: COLORS.inkDim, marginBottom: "6px" }}>{label} · {r.date}</div>
              <div style={{ background: COLORS.panel, borderRadius: "6px", aspectRatio: "4/5", overflow: "hidden" }}>
                <div style={{ width: "70%", height: "90%", margin: "5% auto" }}>
                  <StickFigure pts={poseLanding} />
                </div>
              </div>
            </div>
          ))}
        </div>

        <div style={{ border: `1px solid ${COLORS.line}`, borderRadius: "6px", padding: "12px", marginBottom: "16px" }}>
          <div className="flex items-center justify-between" style={{ marginBottom: "6px" }}>
            <span style={{ fontSize: "12px", color: COLORS.inkDim }}>落地重心偏移变化</span>
            <ConfidenceTag level="Estimated" />
          </div>
          <p style={{ fontSize: "13px", color: COLORS.ink }}>
            落地时的重心偏移从约 {peakB}cm 变化到约 {peakA}cm（基于两次记录的重建估算）。
          </p>
        </div>

        <div style={{ marginBottom: "10px" }}>
          <div style={{ fontSize: "12px", color: COLORS.inkDim, marginBottom: "4px" }}>较新一次的主要发现</div>
          <p style={{ fontSize: "13px" }}>{buildFinding(recordA)}</p>
        </div>
        <div style={{ marginBottom: "16px" }}>
          <div style={{ fontSize: "12px", color: COLORS.inkDim, marginBottom: "4px" }}>较早一次的主要发现</div>
          <p style={{ fontSize: "13px" }}>{buildFinding(recordB)}</p>
        </div>

        <div style={{ border: `1px solid ${COLORS.line}`, borderRadius: "6px", padding: "12px" }}>
          <div style={{ fontSize: "12px", color: COLORS.inkDim, marginBottom: "4px" }}>Next Focus</div>
          {focusChanged ? (
            <p style={{ fontSize: "13px" }}>从 "{recordB.nextFocus}" 变成了 "{recordA.nextFocus}"</p>
          ) : (
            <p style={{ fontSize: "13px" }}>Next Focus 未变，仍建议继续练习。</p>
          )}
        </div>
      </div>
    </div>
  );
}

function FeedbackScreen({ ctx, record, onBack, onSubmit }) {
  const [issue, setIssue] = useState(null);
  const [text, setText] = useState("");
  const [includeVideo, setIncludeVideo] = useState(false);
  const issues = ["关键时间点不正确", "身体位置识别不正确", "分析结论不准确", "动作类型识别错误", "视频播放存在问题", "其他问题"];
  return (
    <div>
      <TopBar title="报告问题" onBack={onBack} />
      <div style={{ padding: "20px" }}>
        <div style={{ fontSize: "12px", color: COLORS.inkDim, marginBottom: "14px" }}>
          关联记录：{record?.movement || "未知"}{ctx.time != null ? ` · ${ctx.time.toFixed(2)}s` : ""}
        </div>
        {issues.map((it) => (
          <div key={it} onClick={() => setIssue(it)} className="flex items-center gap-2" style={{ padding: "8px 0", cursor: "pointer" }}>
            <span style={{
              width: "14px", height: "14px", borderRadius: "50%", border: `1px solid ${issue === it ? COLORS.signal : COLORS.line}`,
              background: issue === it ? COLORS.signal : "transparent",
            }} />
            <span style={{ fontSize: "13px" }}>{it}</span>
          </div>
        ))}
        <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="补充说明（可选）" rows={3}
          style={{ width: "100%", background: COLORS.panel, border: `1px solid ${COLORS.line}`, borderRadius: "5px", padding: "10px", color: COLORS.ink, fontSize: "13px", marginTop: "12px", outline: "none", resize: "none" }} />
        <div className="flex items-center gap-2" style={{ marginTop: "12px" }}>
          <input type="checkbox" disabled={!record?.videoURL} checked={includeVideo} onChange={(e) => setIncludeVideo(e.target.checked)} style={{ accentColor: COLORS.signal }} />
          <span style={{ fontSize: "12px", color: record?.videoURL ? COLORS.ink : COLORS.inkDim }}>
            {record?.videoURL ? "同意提交视频用于问题调查" : "该记录没有可用视频"}
          </span>
        </div>
        <div style={{ marginTop: "20px" }}>
          <PrimaryButton disabled={!issue} onClick={onSubmit}>提交</PrimaryButton>
        </div>
      </div>
    </div>
  );
}

function FeedbackDoneScreen({ onBack }) {
  return (
    <div className="flex flex-col items-center justify-center" style={{ minHeight: "400px", padding: "20px", textAlign: "center" }}>
      <div style={{ fontSize: "15px", fontWeight: 600, marginBottom: "10px" }}>反馈已提交</div>
      <p style={{ fontSize: "13px", color: COLORS.inkDim, marginBottom: "20px", lineHeight: 1.6 }}>感谢反馈，我们会用它来改进分析质量。</p>
      <PrimaryButton onClick={onBack} style={{ width: "auto", padding: "10px 24px" }}>返回</PrimaryButton>
    </div>
  );
}

function SettingsScreen({ onDeleteAllRecords }) {
  const rows = ["账户信息", "通知设置", "视频上传设置", "视频保存期限", "隐私说明", "数据授权", "删除本地视频"];
  return (
    <div>
      <TopBar title="设置" />
      <div style={{ padding: "0 20px" }}>
        {rows.map((r) => (
          <div key={r} className="flex items-center justify-between" style={{ padding: "14px 0", borderBottom: `1px solid ${COLORS.lineSoft}`, cursor: "pointer" }}>
            <span style={{ fontSize: "13px" }}>{r}</span>
            <span style={{ color: COLORS.inkDim }}>{"\u203A"}</span>
          </div>
        ))}
        <div onClick={onDeleteAllRecords} className="flex items-center justify-between" style={{ padding: "14px 0", borderBottom: `1px solid ${COLORS.lineSoft}`, cursor: "pointer" }}>
          <span style={{ fontSize: "13px", color: COLORS.danger }}>删除所有训练记录</span>
        </div>
        <div className="flex items-center justify-between" style={{ padding: "14px 0", borderBottom: `1px solid ${COLORS.lineSoft}`, cursor: "pointer" }}>
          <span style={{ fontSize: "13px", color: COLORS.danger }}>删除账户</span>
        </div>
        <div className="flex items-center justify-between" style={{ padding: "14px 0", cursor: "pointer" }}>
          <span style={{ fontSize: "13px" }}>帮助与反馈</span>
        </div>
      </div>
    </div>
  );
}
