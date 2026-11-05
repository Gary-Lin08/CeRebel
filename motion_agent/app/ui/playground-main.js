    const chat = document.getElementById("chat");
    // The server marks which port served the page. The developer port always opens the playground,
    // the customer port never does (?dev=1 is ignored there), and a single shared port keeps ?dev=1.
    const surfaceMeta = document.querySelector('meta[name="cerebel-surface"]');
    const surface = surfaceMeta ? surfaceMeta.content : "shared";
    const isDevUi = surface === "operator" || (surface === "shared" && new URLSearchParams(window.location.search).has("dev"));
    const isConsumerUi = !isDevUi;
    const AUTH_KEY = "cerebel_user_token_v1";
    const PREFERRED_SPORT_KEY = "cerebel_preferred_sport_v1";
    let authToken = "";
    let accountMode = "login";
    let activeSessionId = null;
    let libraryCaptures = [];
    let libraryPoll = null;
    let preferredSport = "golf";
    try { authToken = localStorage.getItem(AUTH_KEY) || ""; } catch (_) {}
    // A password-reset email links to /#reset=<token> (a fragment never reaches server logs).
    let resetToken = "";
    const resetMatch = /^#reset=([A-Za-z0-9_-]{20,})$/.exec(window.location.hash || "");
    if (resetMatch) {
      resetToken = resetMatch[1];
      authToken = "";
      try { localStorage.removeItem(AUTH_KEY); } catch (_) {}
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    try {
      preferredSport = localStorage.getItem(PREFERRED_SPORT_KEY) || "golf";
    } catch (_) {}

    function setPreferredSport(id) {
      if (!id) return;
      preferredSport = String(id);
      try { localStorage.setItem(PREFERRED_SPORT_KEY, preferredSport); } catch (_) {}
    }

    function openAccountPanel(focusUpload) {
      const panel = document.getElementById("accountPanel");
      if (panel) {
        panel.hidden = false;
        panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
      if (focusUpload) {
        const input = document.getElementById("uploadFile");
        const library = document.getElementById("libraryBox");
        const email = document.getElementById("accountEmail");
        if (input && library && !library.hidden) {
          input.click();
        } else if (email) {
          email.focus();
        }
      }
    }

    function syncTocHome() {
      const home = document.getElementById("tocHome");
      if (!home || isDevUi) return;
      const hasFix = !!(document.getElementById("fixHero") && !document.getElementById("fixHero").hidden);
      const processing = !!(document.getElementById("processingState") && !document.getElementById("processingState").hidden);
      home.hidden = hasFix || processing;
      // The hero is the single way in for visitors; once you have your own swing it steps aside.
      const hero = document.getElementById("v3Hero");
      if (hero) hero.hidden = !!authToken && (hasFix || processing);
      document.body.classList.toggle("v3-has-swing", hasFix);
      // Quieter background while there is a swing to read.
      if (window.cxFluid) window.cxFluid.dim(hasFix ? 0.55 : 0);
    }

    function authHeaders(extra) {
      const headers = Object.assign({}, extra || {});
      if (authToken) headers.Authorization = "Bearer " + authToken;
      return headers;
    }
    document.body.classList.toggle("pg-consumer", !isDevUi);
    document.body.classList.toggle("pg-dev", isDevUi);

    const status = document.getElementById("status");
    const player = document.getElementById("player");
    const egoMeshStage = document.getElementById("egoMeshStage");
    const golfPhaseBoard = document.getElementById("golfPhaseBoard");
    const golfPhaseSwings = document.getElementById("golfPhaseSwings");
    const golfPhaseMeta = document.getElementById("golfPhaseMeta");
    const phasePreview = document.getElementById("phasePreview");
    const phasePreviewPlayer = document.getElementById("phasePreviewPlayer");
    const phasePreviewMesh = document.getElementById("phasePreviewMesh");
    const phasePreviewMeshStatus = document.getElementById("phasePreviewMeshStatus");
    const phasePreviewHint = document.getElementById("phasePreviewHint");
    const phasePreviewTitle = document.getElementById("phasePreviewTitle");
    const phasePreviewEyebrow = document.getElementById("phasePreviewEyebrow");
    const phasePreviewRange = document.getElementById("phasePreviewRange");
    const phasePreviewClose = document.getElementById("phasePreviewClose");
    const phaseViewToggle = document.getElementById("phaseViewToggle");
    const GOLF_EXAMPLE_QUESTIONS = [
      "What's the biggest thing I should fix in this swing?",
      "Walk me through my downswing.",
      "Is my lead arm collapsing before impact?",
      "Where does my pelvis drift after impact?",
    ];
    const BASEBALL_EXAMPLE_QUESTIONS = [
      "Read the pitch figures and tell me what to work on.",
      "How does the front knee change from landing to release?",
      "What does hip–shoulder separation look like in this pitch?",
    ];
    const sportBoard = document.getElementById("sportBoard");
    let lastSportPayload = null;
    let phasePreviewEnd = null;
    let phasePreviewLoop = true;
    let lastGolfPhasePayload = null;
    let golfPhasesSessionId = null;
    let golfPhasesLoading = false;
    let golfPhasesVideoWaitBound = false;
    const PHASE_VIEW_STORAGE_KEY = "motion_agent_golf_phase_view_v1";
    let phaseViewMode = "smplx";
    let meshViewer = null;
    let threeLibs = null;
    let egoMeshViewer = null;
    let fakeEgocentric = false;
    const qInput = document.getElementById("q");
    const sendBtn = document.getElementById("send");
    const loadNewestBtn = document.getElementById("loadNewest");
    const promptArea = document.getElementById("systemPrompt");
    const promptBadge = document.getElementById("promptBadge");
    const promptStatus = document.getElementById("promptStatus");
    const promptPanel = document.getElementById("promptPanel");
    const promptPasswordInput = document.getElementById("promptPassword");
    const promptLockStatus = document.getElementById("promptLockStatus");
    const MODE_STORAGE_KEY = "motion_agent_prompt_mode_v1";
    const PROMPT_STORAGE_PREFIX = "motion_agent_system_prompt_v1_";
    const PROMPT_UNLOCK_KEY = "motion_agent_prompt_unlock_v1";
    let session = null;
    let promptPresets = {};
    let promptMode = "golf";
    let originalSystemPrompt = "";
    let savedSystemPrompt = null;
    let promptUnlocked = false;
    let promptPassword = "";

    function promptStorageKey(mode) {
      return PROMPT_STORAGE_PREFIX + (mode || promptMode);
    }

    function normalizePrompt(text) {
      return (text || "").replace(/\r\n/g, "\n").trimEnd();
    }

    function setPromptLockedUI() {
      promptUnlocked = false;
      promptPassword = "";
      try { sessionStorage.removeItem(PROMPT_UNLOCK_KEY); } catch (_) { /* ignore */ }
      promptPanel.classList.remove("is-unlocked");
      promptBadge.textContent = "locked";
      promptBadge.className = "prompt-badge locked";
      promptArea.value = "";
      originalSystemPrompt = "";
      if (promptLockStatus) {
        promptLockStatus.textContent = "";
        promptLockStatus.className = "status";
      }
    }

    function setPromptUnlockedUI() {
      promptUnlocked = true;
      promptPanel.classList.add("is-unlocked");
      updatePromptBadge();
    }

    function currentPreset() {
      return promptPresets[promptMode] || null;
    }

    function availableModes() {
      const fromSession = session && Array.isArray(session.available_modes) ? session.available_modes : null;
      if (isDevUi) {
        const ids = Object.keys(promptPresets);
        return ids.length ? ids : ["general", "snowboarding", "golf", "baseball"];
      }
      if (fromSession && fromSession.length) return fromSession.slice();
      if (session && session.default_prompt_mode) return [session.default_prompt_mode];
      return ["golf"];
    }

    function fallbackMode() {
      const avail = availableModes();
      if (avail.length) return avail[0];
      if (promptPresets.golf) return "golf";
      const ids = Object.keys(promptPresets);
      return ids.length ? ids[0] : "golf";
    }

    function resolveSessionMode() {
      const avail = availableModes();
      const detected = (session && (session.default_prompt_mode || session.sport)) || fallbackMode();
      let stored = null;
      try { stored = localStorage.getItem(MODE_STORAGE_KEY); } catch (_) { /* ignore */ }
      if (avail.length === 1) return avail[0];
      if (stored && avail.includes(stored)) return stored;
      if (avail.includes(detected)) return detected;
      return avail[0] || detected;
    }

    function updateModeUI() {
      const avail = availableModes();
      const modeBar = document.getElementById("modeBar");
      if (modeBar) modeBar.classList.toggle("is-single", !isDevUi && avail.length <= 1);
      document.querySelectorAll(".mode-switch [data-mode]").forEach((btn) => {
        const mode = btn.getAttribute("data-mode");
        const allowed = isDevUi || avail.includes(mode);
        btn.hidden = !allowed;
        btn.disabled = !allowed;
        btn.classList.toggle("active", mode === promptMode);
      });
      const preset = currentPreset();
      const desc = document.getElementById("modeDesc");
      if (desc) {
        desc.textContent = isDevUi
          ? (preset ? preset.description : "")
          : ((session && session.product_subtitle) || "Ask about this capture. Cues show on your body.");
      }
      const hint = document.getElementById("modeSummaryHint");
      if (hint) hint.textContent = "(" + (preset ? preset.label : promptMode) + ")";
      const pill = document.getElementById("sportPill");
      if (pill) {
        const label =
          (session && (session.sport_label || session.sport))
          || (preset && preset.label)
          || promptMode;
        const conf = session && typeof session.sport_confidence === "number" ? session.sport_confidence : null;
        pill.textContent =
          !isDevUi && conf != null && conf > 0 && session.sport !== "general"
            ? label + " · " + Math.round(conf * 100) + "%"
            : label;
      }
      const title = document.getElementById("productTitle");
      if (title && session && session.product_title) title.textContent = session.product_title;
      const sub = document.getElementById("productSubtitle");
      if (sub && session && session.product_subtitle) sub.textContent = session.product_subtitle;
      updateDetectBanner();
      renderSportsCatalog();
      syncTocHome();
    }

    function updateDetectBanner() {
      const el = document.getElementById("detectBanner");
      if (!el || isDevUi) {
        if (el) el.hidden = true;
        return;
      }
      const msg = session && (session.detect_message || "").trim();
      if (msg) {
        el.hidden = false;
        el.textContent = msg;
        return;
      }
      if (session && session.sport === "general") {
        el.hidden = false;
        el.textContent =
          "No sport pack matched this clip yet — coaching general body motion. Golf unlocks when we see a confident swing.";
        return;
      }
      el.hidden = true;
      el.textContent = "";
    }

    let sportsCatalogExpanded = false;
    let toastTimer = null;

    function showToast(message) {
      const el = document.getElementById("pgToast");
      if (!el || !message) return;
      el.textContent = message;
      el.classList.add("is-on");
      if (toastTimer) window.clearTimeout(toastTimer);
      toastTimer = window.setTimeout(() => {
        el.classList.remove("is-on");
      }, 2800);
    }

    function renderSportsCatalog() {
      const grid = document.getElementById("sportsCatalogGrid");
      const note = document.getElementById("sportsCatalogNote");
      const moreBtn = document.getElementById("sportsCatalogMore");
      if (!grid || isDevUi) return;
      const rows =
        (session && Array.isArray(session.sports_catalog) && session.sports_catalog.length)
          ? session.sports_catalog
          : null;
      if (!rows) {
        if (!renderSportsCatalog._loading) {
          renderSportsCatalog._loading = true;
          fetch("/api/sports")
            .then((r) => r.json())
            .then((data) => {
              renderSportsCatalog._loading = false;
              if (data && Array.isArray(data.sports)) {
                if (!session) session = {};
                session.sports_catalog = data.sports;
                renderSportsCatalog();
              }
            })
            .catch(() => { renderSportsCatalog._loading = false; });
        }
        return;
      }
      const active = (session && session.sport) || promptMode;
      const visible = rows.filter((r) => r.id !== "general");
      const liveN = visible.filter((r) => r.status === "live").length;
      if (note) {
        note.textContent =
          liveN === 1
            ? "Golf is live. More packs unlock as we ship honest detectors."
            : liveN + " live packs · coming-soon never fake analysis.";
      }
      const collapsedLimit = 5;
      const showAll = sportsCatalogExpanded || visible.length <= collapsedLimit;
      const slice = showAll ? visible : visible.slice(0, collapsedLimit);
      grid.innerHTML = "";
      slice.forEach((row) => {
        const card = document.createElement("button");
        card.type = "button";
        const locked = !!row.locked || row.status !== "live";
        card.className =
          "sport-card"
          + (locked ? " is-locked" : " is-live")
          + (row.id === active ? " is-active" : "");
        card.setAttribute("aria-label", row.label + (locked ? " (coming soon)" : " (active pack)"));
        const statusLabel =
          row.status === "live" ? "Live" : row.status === "beta" ? "Beta" : "Coming soon";
        card.innerHTML =
          `<span class="sport-card__label">${escapeHtml(row.label)}</span>` +
          `<span class="sport-card__status">${escapeHtml(statusLabel)}</span>` +
          `<p class="sport-card__blurb">${escapeHtml(row.blurb || "")}</p>`;
        card.addEventListener("click", () => {
          if (locked) {
            showToast(
              (row.label || "This sport") +
                " isn’t live yet — we’ll unlock it when the detector can refuse the wrong sport."
            );
            return;
          }
          setPreferredSport(row.id);
          if (row.id === active) {
            showToast((row.label || "This pack") + " is coaching this capture.");
            return;
          }
          showToast(
            (row.label || "Sport") +
              " selected. Upload a clip or try the sample — we match the pack from the motion."
          );
          openAccountPanel(false);
        });
        grid.appendChild(card);
      });
      if (moreBtn) {
        const needMore = visible.length > collapsedLimit;
        moreBtn.hidden = !needMore;
        moreBtn.textContent = sportsCatalogExpanded
          ? "Show fewer sports"
          : "Show all " + visible.length + " sports";
        moreBtn.onclick = () => {
          sportsCatalogExpanded = !sportsCatalogExpanded;
          renderSportsCatalog();
        };
      }
    }

    function updatePromptBadge() {
      if (!promptUnlocked) {
        promptBadge.textContent = "locked";
        promptBadge.className = "prompt-badge locked";
        return;
      }
      const current = normalizePrompt(promptArea.value);
      const original = normalizePrompt(originalSystemPrompt);
      const saved = savedSystemPrompt != null ? normalizePrompt(savedSystemPrompt) : null;
      if (current !== original && (saved == null || current !== saved)) {
        promptBadge.textContent = "unsaved edits";
        promptBadge.className = "prompt-badge dirty";
      } else if (saved != null && saved !== original) {
        promptBadge.textContent = "custom (saved)";
        promptBadge.className = "prompt-badge custom";
      } else {
        promptBadge.textContent = "original";
        promptBadge.className = "prompt-badge";
      }
    }

    function applyPromptToEditor(text) {
      if (!promptUnlocked) return;
      promptArea.value = text;
      updatePromptBadge();
    }

    function loadSavedPrompt(mode) {
      try {
        const raw = localStorage.getItem(promptStorageKey(mode || promptMode));
        if (raw == null || raw === "") {
          savedSystemPrompt = null;
          return null;
        }
        savedSystemPrompt = raw;
        return raw;
      } catch (_) {
        savedSystemPrompt = null;
        return null;
      }
    }

    function applyMode(mode, { announce = false, skipPhases = false } = {}) {
      const known =
        !!promptPresets[mode]
        || (session && Array.isArray(session.available_modes) && session.available_modes.includes(mode))
        || availableModes().includes(mode);
      if (!known) mode = fallbackMode();
      const prevMode = promptMode;
      promptMode = mode;
      try { localStorage.setItem(MODE_STORAGE_KEY, promptMode); } catch (_) { /* ignore */ }
      const preset = currentPreset();
      if (promptUnlocked && preset && preset.prompt) {
        originalSystemPrompt = preset.prompt;
        const saved = loadSavedPrompt(promptMode);
        applyPromptToEditor(saved != null ? saved : originalSystemPrompt);
      } else {
        originalSystemPrompt = "";
        if (!promptUnlocked) promptArea.value = "";
        loadSavedPrompt(promptMode);
      }
      updateModeUI();
      renderExampleQuestions();
      if (!skipPhases) {
        if (promptMode === "golf") {
          scheduleGolfPhasesLoad({ reason: "mode" });
          scheduleSportAnalysis("golf");
        } else if (promptMode === "baseball") {
          clearGolfPhaseBoard();
          scheduleSportAnalysis("baseball");
        } else if (prevMode === "golf" || prevMode === "baseball") {
          clearGolfPhaseBoard();
          clearSportBoard();
        }
      } else if (promptMode !== "golf" && promptMode !== "baseball") {
        clearGolfPhaseBoard();
        clearSportBoard();
      }
      if (announce && promptStatus) {
        promptStatus.textContent = "Switched to " + (preset ? preset.label : mode) + ".";
        promptStatus.className = "status";
      }
    }

    function currentExampleQuestions() {
      if (session && Array.isArray(session.example_questions) && session.example_questions.length) {
        if (!isDevUi || promptMode === (session.default_prompt_mode || session.sport)) {
          return session.example_questions;
        }
      }
      if (promptMode === "golf") return GOLF_EXAMPLE_QUESTIONS;
      if (promptMode === "baseball") return BASEBALL_EXAMPLE_QUESTIONS;
      return (session && session.example_questions) || [];
    }

    function bindExampleButton(btn, question) {
      btn.type = "button";
      btn.textContent = question;
      btn.onclick = () => {
        qInput.value = question;
        ask();
      };
    }

    function renderExampleQuestions() {
      const questions = currentExampleQuestions();
      const ex = document.getElementById("examples");
      if (ex) {
        ex.innerHTML = "";
        questions.forEach((q) => {
          const b = document.createElement("button");
          bindExampleButton(b, q);
          ex.appendChild(b);
        });
      }
      const coachChips = document.getElementById("coachTryChips");
      if (coachChips) {
        coachChips.innerHTML = "";
        questions.slice(0, 4).forEach((q) => {
          const b = document.createElement("button");
          bindExampleButton(b, q);
          coachChips.appendChild(b);
        });
      }
    }

    function revealBodyAfterAnswer({ updated = false } = {}) {
      const bodyTarget =
        (sportBoard && !sportBoard.hidden && sportBoard)
        || document.getElementById("capture")
        || document.getElementById("egoMeshStage");
      if (!bodyTarget) return;
      bodyTarget.classList.add("is-body-flash");
      window.setTimeout(() => bodyTarget.classList.remove("is-body-flash"), 1800);
      const lenis = window.__cerebelLenis;
      if (lenis && typeof lenis.scrollTo === "function") {
        lenis.scrollTo(bodyTarget, { offset: -80, duration: 1.05 });
      } else {
        bodyTarget.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      if (updated) {
        status.textContent = "Body view updated — skeleton / mesh is highlighted above.";
        status.className = "status";
      }
    }

    function seedCoachWelcome() {
      if (!chat || chat.dataset.welcomed === "1") return;
      chat.dataset.welcomed = "1";
      const sport = (session && (session.product_title || session.sport)) || "your motion";
      const welcome = document.createElement("div");
      welcome.className = "bubble agent welcome";
      welcome.innerHTML =
        `<div class="label">Coach</div>` +
        (isDevUi
          ? "Play the capture, then ask what to fix — or tap a chip below. When I answer, I’ll jump you to the body view."
          : `This is <strong>${escapeHtml(String(sport))}</strong>. Start with the main fix and phase strip above — tap <em>Why?</em> for an instant explanation, or Ask coach only if you need more.`);
      chat.appendChild(welcome);
    }

    async function unlockPrompt() {
      const pw = (promptPasswordInput && promptPasswordInput.value) || "";
      if (!pw) {
        promptLockStatus.textContent = "Enter the password.";
        promptLockStatus.className = "status err";
        return;
      }
      promptLockStatus.textContent = "Checking…";
      promptLockStatus.className = "status";
      try {
        const r = await fetch("/api/system_prompt", {
          headers: { "X-Prompt-Password": pw },
        });
        const data = await r.json();
        if (!r.ok) {
          throw new Error(data.message || "Incorrect password");
        }
        promptPassword = pw;
        try { sessionStorage.setItem(PROMPT_UNLOCK_KEY, pw); } catch (_) { /* ignore */ }
        promptPresets = {};
        (data.presets || []).forEach((p) => { promptPresets[p.id] = p; });
        setPromptUnlockedUI();
        applyMode(promptMode);
        if (promptPasswordInput) promptPasswordInput.value = "";
        promptLockStatus.textContent = "";
        promptStatus.textContent = "System prompt unlocked.";
        promptStatus.className = "status";
      } catch (e) {
        setPromptLockedUI();
        promptLockStatus.textContent = String(e.message || e);
        promptLockStatus.className = "status err";
      }
    }

    function lockPrompt() {
      // Keep mode metadata (labels) from session; drop prompt bodies.
      Object.keys(promptPresets).forEach((id) => {
        const p = promptPresets[id];
        promptPresets[id] = { id: p.id, label: p.label, description: p.description };
      });
      setPromptLockedUI();
      if (promptPanel.open) promptPanel.open = false;
    }

    function savePrompt() {
      if (!promptUnlocked) return;
      const text = promptArea.value;
      try {
        localStorage.setItem(promptStorageKey(promptMode), text);
        savedSystemPrompt = text;
        promptStatus.textContent = "Saved for " + promptMode + " in this browser.";
        promptStatus.className = "status";
        updatePromptBadge();
      } catch (e) {
        promptStatus.textContent = "Save failed: " + e;
        promptStatus.className = "status err";
      }
    }

    function restoreOriginalPrompt() {
      if (!promptUnlocked) return;
      try {
        localStorage.removeItem(promptStorageKey(promptMode));
      } catch (_) { /* ignore */ }
      savedSystemPrompt = null;
      applyPromptToEditor(originalSystemPrompt);
      promptStatus.textContent = "Restored shipped " + promptMode + " prompt.";
      promptStatus.className = "status";
    }

    function activeSystemPrompt() {
      if (!promptUnlocked) return null;
      const text = promptArea.value;
      return text.trim() ? text : (originalSystemPrompt || null);
    }

    function activePromptMode() {
      return promptMode;
    }

    function activePromptPassword() {
      return promptUnlocked ? promptPassword : null;
    }

    function escapeHtml(text) {
      return String(text || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }

    function linkTimestampsInHtml(html) {
      // Only rewrite text segments outside of tags.
      return String(html || "").replace(/(^|>)([^<]*)/g, (m, prefix, text) => {
        const linked = text
          .replace(/\b(\d+\.\d+)\s*s\b/g, (mm, n) => `<span class="ts" data-t="${n}">${n} s</span>`)
          .replace(/\bat\s+(\d+\.\d+)\b/g, (mm, n) => `at <span class="ts" data-t="${n}">${n}</span>`);
        return prefix + linked;
      });
    }

    function renderMarkdown(text) {
      const raw = String(text || "");
      let html;
      try {
        if (typeof marked !== "undefined" && marked.parse) {
          marked.setOptions({ breaks: true, gfm: true });
          html = marked.parse(raw);
        } else {
          html = `<p>${escapeHtml(raw).replace(/\n/g, "<br>")}</p>`;
        }
      } catch (_) {
        html = `<p>${escapeHtml(raw).replace(/\n/g, "<br>")}</p>`;
      }
      if (typeof DOMPurify !== "undefined") {
        html = DOMPurify.sanitize(html, {
          USE_PROFILES: { html: true },
          ADD_ATTR: ["data-t", "class"],
        });
      }
      return `<div class="md-body">${linkTimestampsInHtml(html)}</div>`;
    }

    function formatPhaseTime(t) {
      const n = Number(t);
      if (!Number.isFinite(n)) return "—";
      return n.toFixed(2) + "s";
    }

    function loadPhaseViewMode() {
      try {
        const saved = localStorage.getItem(PHASE_VIEW_STORAGE_KEY);
        if (saved === "smplx" || saved === "raw_video") return saved;
      } catch (_) { /* ignore */ }
      return "smplx";
    }

    function setPhaseViewMode(mode, { persist = true } = {}) {
      phaseViewMode = mode === "raw_video" ? "raw_video" : "smplx";
      if (phaseViewToggle) {
        phaseViewToggle.querySelectorAll("button[data-view]").forEach((btn) => {
          btn.classList.toggle("is-active", btn.dataset.view === phaseViewMode);
        });
      }
      if (persist) {
        try {
          localStorage.setItem(PHASE_VIEW_STORAGE_KEY, phaseViewMode);
        } catch (_) { /* ignore */ }
      }
    }

    async function ensureThree() {
      if (threeLibs) return threeLibs;
      try {
        const THREE = await import("/static/three/three.module.js");
        const controlsMod = await import("/static/three/OrbitControls.js");
        const gltfMod = await import("/static/three/GLTFLoader.js");
        threeLibs = {
          THREE,
          OrbitControls: controlsMod.OrbitControls,
          GLTFLoader: gltfMod.GLTFLoader,
        };
        return threeLibs;
      } catch (err) {
        throw new Error("Failed to load local Three.js: " + (err && err.message ? err.message : err));
      }
    }

    function disposeEgoMeshViewer() {
      if (!egoMeshViewer) return;
      try { cancelAnimationFrame(egoMeshViewer.raf); } catch (_) { /* ignore */ }
      try { if (egoMeshViewer._removeResize) egoMeshViewer._removeResize(); } catch (_) { /* ignore */ }
      try { egoMeshViewer.controls && egoMeshViewer.controls.dispose(); } catch (_) { /* ignore */ }
      try { egoMeshViewer.geometry && egoMeshViewer.geometry.dispose(); } catch (_) { /* ignore */ }
      try { egoMeshViewer.material && egoMeshViewer.material.dispose(); } catch (_) { /* ignore */ }
      try { egoMeshViewer.renderer && egoMeshViewer.renderer.dispose(); } catch (_) { /* ignore */ }
      if (egoMeshViewer.renderer && egoMeshViewer.renderer.domElement && egoMeshViewer.renderer.domElement.parentNode) {
        egoMeshViewer.renderer.domElement.parentNode.removeChild(egoMeshViewer.renderer.domElement);
      }
      egoMeshViewer = null;
    }

    async function playEgoSomaMesh(url) {
      const { THREE, OrbitControls } = await ensureThree();
      disposeEgoMeshViewer();
      if (!egoMeshStage) return;
      const res = await fetch(url + (url.includes("?") ? "&" : "?") + "t=" + Date.now());
      if (!res.ok) throw new Error("SOMA mesh pack unavailable (" + res.status + ")");
      const buf = await res.arrayBuffer();
      const view = new DataView(buf);
      const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
      if (magic !== "SOMA" && magic !== "SOMA1") throw new Error("bad SOMA mesh magic");
      const version = view.getUint32(4, true);
      const faceCount = view.getUint32(8, true);
      const vertexCount = view.getUint32(12, true);
      const frameCount = view.getUint32(16, true);
      const fps = view.getFloat32(20, true) || 30;
      const spaceId = view.getUint32(24, true); // 0=global, 1=incam
      const facesOffset = 28;
      const faces = new Uint32Array(buf, facesOffset, faceCount * 3);
      const vertsOffset = facesOffset + faceCount * 3 * 4;
      const floatCount = frameCount * vertexCount * 3;
      let verts;
      if (version === 2) {
        verts = new Float32Array(floatCount);
        for (let i = 0; i < floatCount; i++) {
          const u16 = view.getUint16(vertsOffset + i * 2, true);
          const sign = (u16 & 0x8000) >> 15;
          const exp = (u16 & 0x7c00) >> 10;
          const frac = u16 & 0x03ff;
          let f;
          if (exp === 0) f = (sign ? -1 : 1) * Math.pow(2, -14) * (frac / 1024);
          else if (exp === 0x1f) f = frac ? NaN : (sign ? -1 : 1) * Infinity;
          else f = (sign ? -1 : 1) * Math.pow(2, exp - 15) * (1 + frac / 1024);
          verts[i] = f;
        }
      } else {
        verts = new Float32Array(buf.slice(vertsOffset, vertsOffset + floatCount * 4));
      }

      // Plant the body: strip per-frame root XZ (+ shared Y floor). Matches Kinetic Lens
      // rooted-global export so chat stays stable even if an old incam pack is served.
      {
        const frameFloorYs = new Float32Array(frameCount);
        for (let t = 0; t < frameCount; t++) {
          const base = t * vertexCount * 3;
          let sx = 0, sz = 0, yMin = Infinity;
          for (let v = 0; v < vertexCount; v++) {
            const o = base + v * 3;
            sx += verts[o];
            sz += verts[o + 2];
            if (verts[o + 1] < yMin) yMin = verts[o + 1];
          }
          const cx = sx / vertexCount;
          const cz = sz / vertexCount;
          for (let v = 0; v < vertexCount; v++) {
            const o = base + v * 3;
            verts[o] -= cx;
            verts[o + 2] -= cz;
          }
          frameFloorYs[t] = yMin;
        }
        const sorted = Array.from(frameFloorYs).sort((a, b) => a - b);
        const yFloor = sorted[Math.max(0, Math.floor(sorted.length * 0.05))] || 0;
        for (let i = 1; i < floatCount; i += 3) verts[i] -= yFloor;
      }

      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(egoMeshStage.clientWidth || 640, egoMeshStage.clientHeight || 320, false);
      renderer.setClearColor(0x0b0d0b, 1);
      egoMeshStage.appendChild(renderer.domElement);

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(40, 1, 0.05, 80);
      camera.position.set(0, 1.0, 3.0);
      const controls = new OrbitControls(camera, renderer.domElement);
      controls.target.set(0, 0.9, 0);
      controls.enablePan = true;
      controls.enableDamping = true;
      controls.dampingFactor = 0.08;
      controls.minDistance = 0.8;
      controls.maxDistance = 8;
      controls.update();
      scene.add(new THREE.AmbientLight(0xffffff, 0.55));
      const key = new THREE.DirectionalLight(0xffffff, 1.1);
      key.position.set(2.5, 4, 2);
      scene.add(key);

      const geometry = new THREE.BufferGeometry();
      const positions = new Float32Array(vertexCount * 3);
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geometry.setIndex(Array.from(faces));
      const material = new THREE.MeshStandardMaterial({
        color: 0x5aa8d4,
        roughness: 0.45,
        metalness: 0.08,
        side: THREE.DoubleSide,
        flatShading: true,
      });
      const mesh = new THREE.Mesh(geometry, material);
      scene.add(mesh);

      let playTime = 0;
      let last = performance.now();
      const scratch = new Float32Array(vertexCount * 3);
      const applyFrame = (f) => {
        const maxF = Math.max(frameCount - 1, 0);
        const i0 = Math.min(maxF, Math.max(0, Math.floor(f)));
        const i1 = Math.min(maxF, i0 + 1);
        const t = f - i0;
        const stride = vertexCount * 3;
        const aBase = i0 * stride;
        const bBase = i1 * stride;
        if (t <= 0.001 || i0 === i1) {
          scratch.set(verts.subarray(aBase, aBase + stride));
        } else if (t >= 0.999) {
          scratch.set(verts.subarray(bBase, bBase + stride));
        } else {
          for (let i = 0; i < stride; i++) {
            const a = verts[aBase + i];
            scratch[i] = a + (verts[bBase + i] - a) * t;
          }
        }
        const attr = geometry.getAttribute("position");
        attr.array.set(scratch);
        attr.needsUpdate = true;
      };
      applyFrame(0);

      const label = document.getElementById("egoMeshLabel");
      if (label) {
        label.textContent = "Egocentric view";
      }

      const onResize = () => {
        const w = egoMeshStage.clientWidth || 640;
        const h = egoMeshStage.clientHeight || 320;
        camera.aspect = w / Math.max(h, 1);
        camera.updateProjectionMatrix();
        renderer.setSize(w, h, false);
      };
      window.addEventListener("resize", onResize);
      onResize();

      const tick = (now) => {
        const dt = Math.min(0.05, (now - last) / 1000);
        last = now;
        const maxF = Math.max(frameCount - 1, 0);
        playTime += dt * Math.max(fps, 1);
        if (playTime > maxF) playTime -= maxF + 1;
        if (playTime < 0) playTime = 0;
        applyFrame(playTime);
        controls.update();
        renderer.render(scene, camera);
        egoMeshViewer.raf = requestAnimationFrame(tick);
      };
      egoMeshViewer = {
        raf: requestAnimationFrame(tick),
        renderer,
        controls,
        geometry,
        material,
        _removeResize: () => window.removeEventListener("resize", onResize),
      };
    }

    function applyFakeEgocentricUi(session) {
      fakeEgocentric = !!(session && (session.fake_egocentric || session.hide_source_video));
      if (player) {
        player.classList.toggle("is-hidden-source", fakeEgocentric);
        if (fakeEgocentric) {
          player.removeAttribute("src");
          try { player.load(); } catch (_) { /* ignore */ }
        }
      }
      if (egoMeshStage) {
        egoMeshStage.classList.toggle("is-active", fakeEgocentric);
        if (!fakeEgocentric) disposeEgoMeshViewer();
      }
      const rawBtn = document.getElementById("phaseViewRaw");
      if (rawBtn) {
        rawBtn.hidden = fakeEgocentric;
        if (fakeEgocentric && phaseViewMode === "raw_video") {
          phaseViewMode = "smplx";
          document.querySelectorAll("#phaseViewToggle button").forEach((btn) => {
            btn.classList.toggle("is-active", btn.getAttribute("data-view") === phaseViewMode);
          });
        }
      }
      if (loadNewestBtn) {
        if (isDevUi) {
          loadNewestBtn.title = fakeEgocentric
            ? "Reload the newest session (egocentric view)"
            : "Reload the newest successfully preprocessed session";
          loadNewestBtn.textContent = fakeEgocentric ? "Load newest stream" : "Load newest video";
        } else {
          loadNewestBtn.title = "Try the sample swing";
          loadNewestBtn.textContent = "Try sample";
        }
        // Playground default labeling (isDevUi true unless ?consumer=1)
        if (isDevUi && !fakeEgocentric) {
          loadNewestBtn.title = "Reload newest preprocessed session";
          loadNewestBtn.textContent = "Load newest";
        }
      }
    }

    function disposeMeshViewer() {
      if (!meshViewer) return;
      try {
        cancelAnimationFrame(meshViewer.raf);
      } catch (_) { /* ignore */ }
      try {
        if (meshViewer._removeResize) meshViewer._removeResize();
      } catch (_) { /* ignore */ }
      try {
        meshViewer.controls && meshViewer.controls.dispose();
      } catch (_) { /* ignore */ }
      try {
        disposeMuscleGroup(meshViewer.muscleGroup);
      } catch (_) { /* ignore */ }
      try {
        meshViewer.renderer && meshViewer.renderer.dispose();
      } catch (_) { /* ignore */ }
      try {
        if (meshViewer.material) meshViewer.material.dispose();
      } catch (_) { /* ignore */ }
      if (meshViewer.renderer && meshViewer.renderer.domElement && meshViewer.renderer.domElement.parentNode) {
        meshViewer.renderer.domElement.parentNode.removeChild(meshViewer.renderer.domElement);
      }
      meshViewer = null;
    }

    function closePhasePreview() {
      if (!phasePreview) return;
      phasePreview.hidden = true;
      phasePreviewEnd = null;
      try {
        phasePreviewPlayer.pause();
      } catch (_) { /* ignore */ }
      disposeMeshViewer();
      document.querySelectorAll(".golf-phase-card.is-active").forEach((el) => {
        el.classList.remove("is-active");
      });
    }

    function applyOpenSimFrame(model, bodyNames, frame, THREE) {
      if (!model || !frame || !frame.bodies) return;
      bodyNames.forEach((bodyName) => {
        const object = model.getObjectByName(bodyName);
        const transform = frame.bodies[bodyName];
        if (!object || !transform) return;
        const p = transform.p || [0, 0, 0];
        const q = transform.q || [0, 0, 0, 1];
        object.position.set(p[0], p[1], p[2]);
        object.quaternion.set(q[0], q[1], q[2], q[3]);
        object.updateMatrix();
      });
    }

    function createMuscleGroup(THREE) {
      const group = new THREE.Group();
      group.name = "opensim-muscles";
      group.userData.meshes = new Map();
      group.userData.sharedMaterial = new THREE.MeshPhysicalMaterial({
        // Muted in the consumer view so the flag-red fix bones stand out against the muscles.
        color: new THREE.Color(isDevUi ? 0xd96b5c : 0xc29a90),
        roughness: 0.42,
        metalness: 0.02,
        clearcoat: 0.22,
        clearcoatRoughness: 0.55,
        sheen: 0.35,
        sheenRoughness: 0.65,
        sheenColor: new THREE.Color(0xffc2b0),
        transparent: true,
        opacity: 0.55,
        depthWrite: false,
        side: THREE.DoubleSide,
      });
      return group;
    }

    function disposeMuscleGroup(group) {
      if (!group) return;
      const meshes = (group.userData && group.userData.meshes) || new Map();
      meshes.forEach((mesh) => {
        try { mesh.geometry && mesh.geometry.dispose(); } catch (_) { /* ignore */ }
        if (mesh.parent) mesh.parent.remove(mesh);
      });
      meshes.clear();
      try {
        group.userData.sharedMaterial && group.userData.sharedMaterial.dispose();
      } catch (_) { /* ignore */ }
    }

    function _dedupeMusclePoints(points, minDist) {
      const out = [];
      for (let i = 0; i < points.length; i += 1) {
        const p = points[i] || [0, 0, 0];
        const x = Number(p[0]) || 0;
        const y = Number(p[1]) || 0;
        const z = Number(p[2]) || 0;
        if (!out.length) {
          out.push([x, y, z]);
          continue;
        }
        const prev = out[out.length - 1];
        const dx = x - prev[0];
        const dy = y - prev[1];
        const dz = z - prev[2];
        if (dx * dx + dy * dy + dz * dz >= minDist * minDist) out.push([x, y, z]);
      }
      return out;
    }

    function buildFusiformMuscleGeometry(THREE, points) {
      const cleaned = _dedupeMusclePoints(points, 0.002);
      if (cleaned.length < 2) return null;
      const vectors = cleaned.map((p) => new THREE.Vector3(p[0], p[1], p[2]));
      if (vectors.length === 2) {
        // Insert a midpoint so Catmull-Rom has a belly.
        vectors.splice(1, 0, vectors[0].clone().lerp(vectors[1], 0.5));
      }
      const curve = new THREE.CatmullRomCurve3(vectors, false, "centripetal", 0.5);
      const length = Math.max(curve.getLength(), 0.02);
      // Visual scale: real MTU paths are thin; fatten for readable fusiform belly.
      const belly = THREE.MathUtils.clamp(0.010 + length * 0.028, 0.012, 0.024);
      const tip = belly * 0.22;
      const tubularSegments = Math.max(16, Math.min(48, (cleaned.length - 1) * 8));
      const radialSegments = 7;

      let frames;
      try {
        frames = curve.computeFrenetFrames(tubularSegments, false);
      } catch (_) {
        return null;
      }

      const ring = radialSegments + 1;
      const pos = new Float32Array((tubularSegments + 1) * ring * 3);
      const nrm = new Float32Array((tubularSegments + 1) * ring * 3);
      const uvs = new Float32Array((tubularSegments + 1) * ring * 2);
      const idx = [];
      const p = new THREE.Vector3();
      const normal = new THREE.Vector3();

      for (let i = 0; i <= tubularSegments; i += 1) {
        const u = i / tubularSegments;
        // Fusiform belly: thick mid-belly, tapered tendons at both ends.
        const bellyShape = Math.pow(Math.sin(Math.PI * u), 1.25);
        const radius = tip + (belly - tip) * bellyShape;
        curve.getPointAt(u, p);
        const N = frames.normals[i];
        const B = frames.binormals[i];
        for (let j = 0; j <= radialSegments; j += 1) {
          const v = j / radialSegments;
          const angle = v * Math.PI * 2;
          const cos = -Math.cos(angle);
          const sin = Math.sin(angle);
          normal.set(
            cos * N.x + sin * B.x,
            cos * N.y + sin * B.y,
            cos * N.z + sin * B.z
          ).normalize();
          const vi = (i * ring + j) * 3;
          pos[vi] = p.x + radius * normal.x;
          pos[vi + 1] = p.y + radius * normal.y;
          pos[vi + 2] = p.z + radius * normal.z;
          nrm[vi] = normal.x;
          nrm[vi + 1] = normal.y;
          nrm[vi + 2] = normal.z;
          const ui = (i * ring + j) * 2;
          uvs[ui] = u;
          uvs[ui + 1] = v;
        }
      }

      for (let i = 0; i < tubularSegments; i += 1) {
        for (let j = 0; j < radialSegments; j += 1) {
          const a = i * ring + j;
          const b = (i + 1) * ring + j;
          const c = (i + 1) * ring + (j + 1);
          const d = i * ring + (j + 1);
          idx.push(a, b, d, b, c, d);
        }
      }

      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      geometry.setAttribute("normal", new THREE.BufferAttribute(nrm, 3));
      geometry.setAttribute("uv", new THREE.BufferAttribute(uvs, 2));
      geometry.setIndex(idx);
      geometry.computeBoundingSphere();
      return geometry;
    }

    function applyMuscleFrame(group, frame, THREE) {
      if (!group || !THREE) return;
      const muscles = (frame && frame.muscles) || [];
      if (!muscles.length) {
        group.visible = false;
        return;
      }
      group.visible = true;
      const meshes = group.userData.meshes || new Map();
      group.userData.meshes = meshes;
      const material = group.userData.sharedMaterial;
      const seen = new Set();

      muscles.forEach((muscle) => {
        const pts = muscle.points || [];
        if (!muscle.name || pts.length < 2) return;
        seen.add(muscle.name);
        const geometry = buildFusiformMuscleGeometry(THREE, pts);
        if (!geometry) return;
        let mesh = meshes.get(muscle.name);
        if (!mesh) {
          mesh = new THREE.Mesh(geometry, material);
          mesh.name = muscle.name;
          mesh.frustumCulled = false;
          mesh.castShadow = false;
          mesh.receiveShadow = false;
          group.add(mesh);
          meshes.set(muscle.name, mesh);
          return;
        }
        const prev = mesh.geometry;
        mesh.geometry = geometry;
        if (prev) prev.dispose();
      });

      meshes.forEach((mesh, name) => {
        if (seen.has(name)) return;
        if (mesh.geometry) mesh.geometry.dispose();
        if (mesh.parent) mesh.parent.remove(mesh);
        meshes.delete(name);
      });
    }

    async function playOpenSimClip(payload) {
      const { THREE, OrbitControls, GLTFLoader } = await ensureThree();
      disposeMeshViewer();
      if (!phasePreviewMesh) return;

      const frames = payload.frames || [];
      const bodyNames = payload.body_names || [];
      const glbUrl = payload.glb_url;
      if (!frames.length || !glbUrl) {
        throw new Error("OpenSim clip missing frames or GLB URL");
      }

      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(phasePreviewMesh.clientWidth || 640, phasePreviewMesh.clientHeight || 360, false);
      renderer.setClearColor(0x0b0d0b, 1);
      phasePreviewMesh.appendChild(renderer.domElement);

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(
        35,
        (phasePreviewMesh.clientWidth || 640) / (phasePreviewMesh.clientHeight || 360),
        0.01,
        50
      );
      camera.position.set(2.25, 1.45, 2.75);

      const controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;
      controls.target.set(0, 0.98, 0);
      controls.minDistance = 1.7;
      controls.maxDistance = 6;
      controls.update();

      scene.add(new THREE.AmbientLight(0xffffff, 0.55));
      const key = new THREE.DirectionalLight(0xf2ffc8, 1.05);
      key.position.set(2.5, 4.0, 2.0);
      scene.add(key);
      const fill = new THREE.DirectionalLight(0x88a0ff, 0.35);
      fill.position.set(-2.0, 1.0, -1.5);
      scene.add(fill);

      const grid = new THREE.GridHelper(3, 12, 0x3a4032, 0x2a2e26);
      scene.add(grid);

      const loader = new GLTFLoader();
      const gltf = await new Promise((resolve, reject) => {
        loader.load(glbUrl, resolve, undefined, reject);
      });
      const model = gltf.scene.clone(true);
      const boneMaterial = new THREE.MeshPhysicalMaterial({
        color: new THREE.Color(0xf3f5ee),
        roughness: 0.62,
        metalness: 0.02,
        clearcoat: 0.08,
      });
      model.traverse((object) => {
        if (object.isMesh) {
          object.material = boneMaterial;
          object.castShadow = true;
          object.receiveShadow = true;
        }
      });
      scene.add(model);
      const muscleGroup = createMuscleGroup(THREE);
      scene.add(muscleGroup);
      applyOpenSimFrame(model, bodyNames, frames[0], THREE);
      applyMuscleFrame(muscleGroup, frames[0], THREE);

      const fps = Math.max(1, Number(payload.fps) || 30);
      const frameInterval = 1000 / fps;
      let frameIdx = 0;
      let lastTs = performance.now();
      let playing = true;

      function tick(now) {
        if (!meshViewer) return;
        meshViewer.raf = requestAnimationFrame(tick);
        if (playing && now - lastTs >= frameInterval) {
          lastTs = now;
          frameIdx += 1;
          if (frameIdx >= frames.length) {
            if (phasePreviewLoop) frameIdx = 0;
            else {
              frameIdx = frames.length - 1;
              playing = false;
            }
          }
          applyOpenSimFrame(model, bodyNames, frames[frameIdx], THREE);
          applyMuscleFrame(muscleGroup, frames[frameIdx], THREE);
        }
        controls.update();
        renderer.render(scene, camera);
      }

      function onResize() {
        if (!meshViewer || !phasePreviewMesh) return;
        const w = phasePreviewMesh.clientWidth || 640;
        const h = phasePreviewMesh.clientHeight || 360;
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h, false);
      }

      meshViewer = {
        raf: 0,
        renderer,
        controls,
        material: boneMaterial,
        muscleGroup,
        onResize,
        setPlaying: (v) => { playing = !!v; },
      };
      window.addEventListener("resize", onResize);
      meshViewer._removeResize = () => window.removeEventListener("resize", onResize);
      meshViewer.raf = requestAnimationFrame(tick);
    }

    function showPreviewChrome({ mesh }) {
      if (phasePreviewPlayer) phasePreviewPlayer.hidden = !!mesh;
      if (phasePreviewMesh) phasePreviewMesh.hidden = !mesh;
      if (phasePreviewHint) {
        phasePreviewHint.textContent = mesh
          ? "3D bones and muscles. Drag to turn it; it loops at the end of the phase."
          : "Playing this phase only — closes or loops at the end of the window.";
      }
    }

    async function openPhasePreviewMesh(phase, swing, start, end) {
      showPreviewChrome({ mesh: true });
      if (phasePreviewMeshStatus) {
        phasePreviewMeshStatus.hidden = false;
        phasePreviewMeshStatus.textContent = "Loading the 3D skeleton…";
      }
      disposeMeshViewer();
      const sid = (session && session.session_id) || golfPhasesSessionId || "";
      const url =
        "/api/smplx_mesh_clip?video_id=" + encodeURIComponent(sid) +
        "&t0=" + encodeURIComponent(String(start)) +
        "&t1=" + encodeURIComponent(String(end)) +
        "&muscles=1";
      const resp = await fetch(url, { headers: authHeaders() });
      const data = await resp.json();
      if (data.error || data.fallback === "raw_video" || !data.frames || !data.glb_url) {
        const msg = "The 3D skeleton isn't ready for this swing, so the video plays instead.";
        if (phasePreviewMeshStatus) {
          phasePreviewMeshStatus.hidden = false;
          phasePreviewMeshStatus.textContent = msg;
        }
        status.textContent = msg;
        status.className = "status warn";
        openPhasePreviewVideo(phase, swing, start, end, { softFallback: true });
        return;
      }
      if (phasePreviewMeshStatus) phasePreviewMeshStatus.hidden = true;
      await playOpenSimClip(data);
    }

    function openPhasePreviewVideo(phase, swing, start, end, { softFallback = false } = {}) {
      showPreviewChrome({ mesh: false });
      disposeMeshViewer();
      const src = player.currentSrc || player.src;
      if (!src) {
        status.textContent = "Load a session video before previewing phases.";
        status.className = "status err";
        return;
      }
      if (!softFallback) {
        /* keep status as-is for fallback note */
      }
      if (phasePreviewPlayer.src !== src) {
        phasePreviewPlayer.src = src;
      }
      phasePreviewEnd = end;
      const seekAndPlay = () => {
        try {
          phasePreviewPlayer.currentTime = start;
        } catch (_) { /* ignore */ }
        const p = phasePreviewPlayer.play();
        if (p && typeof p.catch === "function") p.catch(() => {});
      };
      if (phasePreviewPlayer.readyState >= 1) seekAndPlay();
      else phasePreviewPlayer.addEventListener("loadedmetadata", seekAndPlay, { once: true });
    }

    function openPhasePreview(phase, swing) {
      if (!phasePreview || !player) return;
      const start = Number(phase.start_time);
      const end = Number(phase.end_time);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;

      document.querySelectorAll(".golf-phase-card.is-active").forEach((el) => {
        el.classList.remove("is-active");
      });
      const active = document.querySelector(
        `.golf-phase-card[data-swing="${swing.swing_index}"][data-phase="${phase.id}"]`
      );
      if (active) active.classList.add("is-active");

      phasePreviewEyebrow.textContent =
        "Swing " + (Number(swing.swing_index) + 1) + " · golf phase";
      phasePreviewTitle.textContent = phase.label || phase.id || "Phase";
      phasePreviewRange.textContent =
        formatPhaseTime(start) + " → " + formatPhaseTime(end) +
        (phase.key_time != null ? " · key " + formatPhaseTime(phase.key_time) : "");

      phasePreview.hidden = false;
      if (phaseViewMode === "smplx") {
        openPhasePreviewMesh(phase, swing, start, end).catch((err) => {
          // Internal errors name pipeline pieces; customers get a plain message, the console keeps the detail.
          const msg = "The 3D skeleton didn't load, so the video plays instead.";
          console.error("Skeleton preview failed", err);
          if (phasePreviewMeshStatus) {
            phasePreviewMeshStatus.hidden = false;
            phasePreviewMeshStatus.textContent = msg;
          }
          status.textContent = msg;
          status.className = "status err";
          openPhasePreviewVideo(phase, swing, start, end, { softFallback: true });
        });
      } else {
        openPhasePreviewVideo(phase, swing, start, end);
      }
    }

    function onPhasePreviewTimeUpdate() {
      if (phasePreviewEnd == null || phasePreview.hidden || phasePreviewPlayer.hidden) return;
      if (phasePreviewPlayer.currentTime >= phasePreviewEnd - 0.02) {
        if (phasePreviewLoop) {
          const card = document.querySelector(".golf-phase-card.is-active");
          const start = card ? parseFloat(card.dataset.start) : NaN;
          if (Number.isFinite(start)) {
            phasePreviewPlayer.currentTime = start;
            return;
          }
        }
        phasePreviewPlayer.pause();
      }
    }

    function clearGolfPhaseBoard() {
      lastGolfPhasePayload = null;
      golfPhasesSessionId = null;
      if (golfPhaseBoard) golfPhaseBoard.hidden = true;
      if (golfPhaseSwings) golfPhaseSwings.innerHTML = "";
      if (golfPhaseMeta) golfPhaseMeta.textContent = "";
    }

    function videoReadyEnough() {
      return !!(player && player.readyState >= 2);
    }

    async function ensureGolfPhasesLoaded({ force = false } = {}) {
      if (promptMode !== "golf") return;
      if (!session || session.error || !session.session_id) return;
      const sid = session.session_id;
      if (!force && golfPhasesSessionId === sid && lastGolfPhasePayload) return;
      if (golfPhasesLoading) return;
      golfPhasesLoading = true;
      if (golfPhaseMeta) golfPhaseMeta.textContent = "Loading swing phases…";
      if (golfPhaseBoard) golfPhaseBoard.hidden = false;
      try {
        const r = await fetch(
          "/api/golf_phases?video_id=" + encodeURIComponent(sid) + "&t=" + Date.now(),
          { headers: authHeaders() }
        );
        const data = await r.json();
        if (!r.ok || data.error) {
          // Server messages can name internal files and tools; keep them in the console.
          console.error("Swing phases failed", data.message);
          if (golfPhaseMeta) {
            golfPhaseMeta.textContent = "Couldn't load the swing phases for this video.";
          }
          return;
        }
        renderGolfPhaseCards(data);
        golfPhasesSessionId = sid;
      } catch (e) {
        console.error("Swing phases failed", e);
        if (golfPhaseMeta) golfPhaseMeta.textContent = "Couldn't load the swing phases for this video.";
      } finally {
        golfPhasesLoading = false;
      }
    }

    function scheduleGolfPhasesLoad({ reason = "session" } = {}) {
      if (promptMode !== "golf") return;
      if (!session || session.error) return;
      // Switching into Golf after video is ready → load immediately.
      // Golf already selected while session/video loads → wait for video.
      const waitForVideo = !!(session.video_url_path) && !videoReadyEnough();
      if (!waitForVideo) {
        ensureGolfPhasesLoaded({ force: reason === "session" });
        return;
      }
      if (golfPhasesVideoWaitBound) return;
      golfPhasesVideoWaitBound = true;
      if (golfPhaseBoard) golfPhaseBoard.hidden = false;
      if (golfPhaseMeta) golfPhaseMeta.textContent = "Waiting for video…";
      const finish = () => {
        golfPhasesVideoWaitBound = false;
        player.removeEventListener("loadeddata", finish);
        player.removeEventListener("canplay", finish);
        player.removeEventListener("error", finish);
        if (promptMode === "golf") ensureGolfPhasesLoaded({ force: true });
      };
      player.addEventListener("loadeddata", finish);
      player.addEventListener("canplay", finish);
      player.addEventListener("error", finish);
    }

    function renderGolfPhaseCards(payload) {
      if (!golfPhaseBoard || !golfPhaseSwings) return;
      const swings = Array.isArray(payload && payload.swings) ? payload.swings : [];
      if (!swings.length) {
        golfPhaseBoard.hidden = true;
        golfPhaseSwings.innerHTML = "";
        return;
      }
      lastGolfPhasePayload = payload;
      const hand = payload.handedness ? String(payload.handedness) : "auto";
      golfPhaseMeta.textContent =
        swings.length + " swing" + (swings.length === 1 ? "" : "s") +
        " · " + hand + "-handed" +
        (payload.lead_side ? " · lead " + payload.lead_side : "");

      golfPhaseSwings.innerHTML = "";
      swings.forEach((swing) => {
        const block = document.createElement("div");
        block.className = "golf-phase-swing";
        const label = document.createElement("div");
        label.className = "golf-phase-swing__label";
        label.textContent =
          "Swing " + (Number(swing.swing_index) + 1) +
          " · " + formatPhaseTime(swing.start_time) +
          " → " + formatPhaseTime(swing.end_time) +
          (swing.impact_time != null ? " · impact " + formatPhaseTime(swing.impact_time) : "");
        block.appendChild(label);

        const row = document.createElement("div");
        row.className = "golf-phase-cards";
        (swing.phases || []).forEach((phase) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "golf-phase-card";
          btn.dataset.swing = String(swing.swing_index);
          btn.dataset.phase = String(phase.id || "");
          btn.dataset.start = String(phase.start_time);
          btn.dataset.end = String(phase.end_time);
          btn.innerHTML =
            `<span class="golf-phase-card__name">${escapeHtml(phase.label || phase.id || "Phase")}</span>` +
            `<span class="golf-phase-card__time">${formatPhaseTime(phase.start_time)} – ${formatPhaseTime(phase.end_time)}</span>`;
          btn.addEventListener("click", () => openPhasePreview(phase, swing));
          row.appendChild(btn);
        });
        block.appendChild(row);
        golfPhaseSwings.appendChild(block);
      });
      golfPhaseBoard.hidden = false;
    }

    function clearSportBoard() {
      lastSportPayload = null;
      selectedFixId = null;
      resetFixTour();
      if (sportBoard) {
        sportBoard.hidden = true;
        sportBoard.classList.remove("is-holding", "is-slow");
      }
      disposeSportSkeleton();
      paintFixHero(null);
      const phases = document.getElementById("fixPhases");
      if (phases) phases.innerHTML = "";
      const tabs = document.getElementById("fixFindingTabs");
      if (tabs) tabs.innerHTML = "";
      const drill = document.getElementById("fixDrill");
      if (drill) drill.hidden = true;
    }

    let sportSkeleton = null;
    let sportSkeletonGen = 0;

    // Fix tour: halt at each finding's peak (not slow-mo), then zoom out when all are shown.
    const FIX_HOLD_MS = 2000;
    let fixTour = {
      heldIds: new Set(),
      holdingId: null,
      holdTimer: null,
      overview: false,
      resumeAfterHold: false,
      pausingForHold: false,
      resumingFromHold: false,
    };

    function resetFixTour() {
      if (fixTour.holdTimer) {
        clearTimeout(fixTour.holdTimer);
        fixTour.holdTimer = null;
      }
      fixTour.heldIds = new Set();
      fixTour.holdingId = null;
      fixTour.overview = false;
      fixTour.resumeAfterHold = false;
      fixTour.pausingForHold = false;
      fixTour.resumingFromHold = false;
      if (sportBoard) sportBoard.classList.remove("is-holding", "is-slow");
      if (player && Math.abs(player.playbackRate - 1) > 0.02) player.playbackRate = 1;
    }

    function disposeSportSkeleton() {
      sportSkeletonGen += 1;
      if (!sportSkeleton) return;
      try { cancelAnimationFrame(sportSkeleton.raf); } catch (_) { /* ignore */ }
      try { sportSkeleton.removeResize && sportSkeleton.removeResize(); } catch (_) { /* ignore */ }
      try { disposeMuscleGroup(sportSkeleton.muscleGroup); } catch (_) { /* ignore */ }
      try { sportSkeleton.renderer && sportSkeleton.renderer.dispose(); } catch (_) { /* ignore */ }
      const canvas = sportSkeleton.renderer && sportSkeleton.renderer.domElement;
      if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
      sportSkeleton = null;
    }

    function nearestSkeletonFrame(frames, time) {
      let best = frames[0];
      let dist = Infinity;
      frames.forEach((frame) => {
        const d = Math.abs(Number(frame.time) - time);
        if (d < dist) { dist = d; best = frame; }
      });
      return best;
    }

    async function ensureSportCloseup(payload) {
      const host = document.getElementById("sportCloseup");
      const statusEl = document.getElementById("sportCloseupStatus");
      if (!host || !payload) return;
      const key = ((session && session.session_id) || "") + ":" + payload.start_time + ":" + payload.end_time;
      if (sportSkeleton && sportSkeleton.key === key) return;
      disposeSportSkeleton();
      const gen = sportSkeletonGen;
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.textContent = "Loading skeleton…";
      }
      const sid = (session && session.session_id) || "";
      const url =
        "/api/smplx_mesh_clip?video_id=" + encodeURIComponent(sid) +
        "&t0=" + encodeURIComponent(String(payload.start_time)) +
        "&t1=" + encodeURIComponent(String(payload.end_time)) +
        "&max_frames=160&muscles=1";
      let data;
      try {
        const resp = await fetch(url, { headers: authHeaders() });
        data = await resp.json();
      } catch (err) {
        if (statusEl) statusEl.textContent = "Skeleton unavailable";
        return;
      }
      if (gen !== sportSkeletonGen || !host.isConnected) return;
      if (data.error || !data.frames || !data.frames.length || !data.glb_url) {
        if (statusEl) statusEl.textContent = data.message || "Skeleton unavailable";
        return;
      }
      const { THREE, GLTFLoader } = await ensureThree();
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      // Transparent clear so the panel’s field shows through (no hard canvas box).
      renderer.setClearColor(0x000000, 0);
      host.appendChild(renderer.domElement);
      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(32, 1, 0.02, 40);
      camera.position.set(0.4, 1.3, 2.4);
      scene.add(new THREE.AmbientLight(0xffffff, 0.55));
      const keyLight = new THREE.DirectionalLight(0xf4f0fa, 1.15);
      keyLight.position.set(1.6, 3.2, 2.2);
      scene.add(keyLight);
      const fill = new THREE.DirectionalLight(isDevUi ? 0xb99bff : 0xe8c98a, 0.5);
      fill.position.set(-1.8, 1.2, -1.2);
      scene.add(fill);
      const loader = new GLTFLoader();
      const gltf = await new Promise((resolve, reject) => loader.load(data.glb_url, resolve, undefined, reject));
      if (gen !== sportSkeletonGen || !host.isConnected) {
        renderer.dispose();
        return;
      }
      const model = gltf.scene;
      const bodyNames = data.body_names || [];
      model.traverse((object) => {
        if (!object.isMesh) return;
        object.material = new THREE.MeshStandardMaterial({
          color: 0xeee9dd,
          roughness: 0.48,
          metalness: 0.06,
          emissive: new THREE.Color(0x000000),
        });
      });
      scene.add(model);
      const muscleGroup = createMuscleGroup(THREE);
      scene.add(muscleGroup);
      applyOpenSimFrame(model, bodyNames, data.frames[0], THREE);
      applyMuscleFrame(muscleGroup, data.frames[0], THREE);
      model.updateMatrixWorld(true);

      const goal = new THREE.Vector3(0, 1, 0);
      const look = new THREE.Vector3(0, 1, 0);
      const desired = new THREE.Vector3();
      // Consumer view uses the landing palette: flag red for the fix, quiet grey-green for the rest.
      const accent = new THREE.Color(isDevUi ? "#b99bff" : "#F2A541");
      const quiet = new THREE.Color(isDevUi ? "#6a6474" : "#5d6d66");
      const bone = new THREE.Color("#eee9dd");
      let focus = { anchor: "pelvis", bones: ["pelvis", "torso"], view_yaw: 0, label: "Body", overview: false };
      let lastFrame = null;

      function resize() {
        const w = host.clientWidth || 320;
        const h = host.clientHeight || 340;
        camera.aspect = w / Math.max(h, 1);
        camera.updateProjectionMatrix();
        renderer.setSize(w, h, false);
      }
      const onResize = () => resize();
      window.addEventListener("resize", onResize);
      resize();

      function setFocus(note) {
        const next = (note && note.focus) || {
          anchor: "pelvis",
          bones: ["pelvis", "torso"],
          view_yaw: 0,
          label: (note && note.title) || "Body",
        };
        focus = Object.assign({}, next, { overview: false });
        accent.set(isDevUi ? (note && note.accent) || "#b99bff" : "#F2A541");
        const label = document.getElementById("sportCloseupTarget");
        if (label) label.innerHTML = "<span>Fix</span>" + escapeHtml(focus.label || (note && note.title) || "Body");
      }

      function setOverview() {
        focus = {
          anchor: "pelvis",
          bones: bodyNames.slice(),
          view_yaw: 15,
          label: "Full body",
          overview: true,
        };
        accent.set(isDevUi ? "#b99bff" : "#E8C98A");
        const label = document.getElementById("sportCloseupTarget");
        if (label) label.innerHTML = "<span>Tour</span>Full body";
      }

      function tick() {
        if (!sportSkeleton) return;
        sportSkeleton.raf = requestAnimationFrame(tick);
        const time = player && Number.isFinite(player.currentTime) ? player.currentTime : Number(payload.start_time);
        const frame = nearestSkeletonFrame(data.frames, time);
        if (frame !== lastFrame) {
          lastFrame = frame;
          applyOpenSimFrame(model, bodyNames, frame, THREE);
          applyMuscleFrame(muscleGroup, frame, THREE);
          model.updateMatrixWorld(true);
        }
        const node = model.getObjectByName(focus.anchor) || model.getObjectByName("pelvis");
        if (node) node.getWorldPosition(goal);
        const yaw = ((focus.view_yaw || 0) * Math.PI) / 180;
        const tight = !focus.overview && focus.anchor && focus.anchor !== "pelvis" && focus.anchor !== "torso";
        const dist = focus.overview ? 2.45 : (tight ? 0.78 : 1.25);
        desired.set(goal.x + Math.sin(yaw) * dist, goal.y + dist * (focus.overview ? 0.28 : 0.18), goal.z + Math.cos(yaw) * dist);
        camera.position.lerp(desired, focus.overview ? 0.05 : 0.08);
        look.lerp(goal, 0.1);
        camera.lookAt(look);
        const lit = new Set(focus.bones || []);
        model.traverse((object) => {
          if (!object.isMesh || !object.material) return;
          let body = object;
          let name = "";
          while (body) {
            if (bodyNames.indexOf(body.name) >= 0) { name = body.name; break; }
            body = body.parent;
          }
          const on = lit.has(name);
          if (focus.overview) {
            object.material.color.copy(bone).lerp(quiet, 0.2);
            object.material.emissive.copy(quiet).multiplyScalar(0.08);
          } else {
            object.material.color.copy(on ? accent : bone).lerp(quiet, on ? 0 : 0.55);
            object.material.emissive.copy(on ? accent : quiet).multiplyScalar(on ? 0.42 : 0);
          }
        });
        renderer.render(scene, camera);
      }

      sportSkeleton = {
        key,
        raf: 0,
        renderer,
        muscleGroup,
        setFocus,
        setOverview,
        removeResize: () => window.removeEventListener("resize", onResize),
      };
      if (statusEl) statusEl.hidden = true;
      const live = activeSportFinding(player && player.currentTime) || (payload.findings || [])[0];
      setFocus(live);
      sportSkeleton.raf = requestAnimationFrame(tick);
    }

    function scheduleSportAnalysis(sport) {
      const sid = (session && session.session_id) || "";
      const q = sid ? "&video_id=" + encodeURIComponent(sid) : "";
      fetch("/api/sport_analysis?sport=" + encodeURIComponent(sport) + q + "&t=" + Date.now(), { headers: authHeaders() })
        .then((r) => r.json())
        .then((data) => {
          if (promptMode !== sport) return;
          if (!data || data.error || data.ui_hint !== "sport_motion_board") {
            clearSportBoard();
            if (data && data.type === "SPORT_MISMATCH" && status) {
              status.textContent = data.message || "This capture doesn’t match that sport.";
              status.className = "status warn";
            }
            return;
          }
          renderSportBoard(data);
        })
        .catch(() => { clearSportBoard(); });
    }

    let sportFindings = [];
    let sportOpenId = null;

    function formatTraceValue(value, unit) {
      const n = Number(value);
      if (!Number.isFinite(n)) return "—";
      if (unit === "°") return (Math.abs(n) >= 10 ? n.toFixed(0) : n.toFixed(1)) + "°";
      if (unit === "cm") return n.toFixed(1) + " cm";
      if (unit === "m/s") return n.toFixed(2) + " m/s";
      return n.toFixed(2) + (unit ? " " + unit : "");
    }

    function seriesGeometry(times, values) {
      if (!times || !times.length) return { d: "", y: () => 50, min: 0, max: 1 };
      const t0 = times[0];
      const t1 = times[times.length - 1];
      const span = Math.max(t1 - t0, 1e-6);
      let min = Math.min.apply(null, values);
      let max = Math.max.apply(null, values);
      if (min === max) { min -= 1; max += 1; }
      const pad = (max - min) * 0.08;
      min -= pad;
      max += pad;
      const y = (v) => 8 + (1 - (v - min) / (max - min)) * 84;
      const x = (t) => ((t - t0) / span) * 100;
      const d = times.map((t, i) => (i ? "L" : "M") + x(t).toFixed(2) + " " + y(values[i]).toFixed(2)).join(" ");
      return { d, y, min, max };
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

    function seekPlayer(t) {
      const time = Number(t);
      if (!Number.isFinite(time) || !player) return;
      player.currentTime = Math.max(0, time);
      const fixVideo = document.getElementById("fixVideo");
      if (fixVideo && fixVideo.src) {
        try { fixVideo.currentTime = Math.max(0, time); } catch (_) { /* ignore */ }
      }
      pruneFixTourAfterSeek(Math.max(0, time));
      syncSportPlayhead();
    }

    let explainFollowUp = false;
    let selectedFixId = null;
    let coachBriefs = [];
    let compareTimes = { a: 0, b: 0 };
    let voiceRecognition = null;
    let voiceTranscriptParts = [];
    let voiceArmed = false;
    let voiceRestartTimer = null;

    function currentFixNote() {
      if (!sportFindings || !sportFindings.length) return null;
      if (selectedFixId) {
        const hit = sportFindings.find((n) => n.id === selectedFixId);
        if (hit) return hit;
      }
      return sportFindings.find((n) => n.id === sportOpenId) || sportFindings[0] || null;
    }

    function hasSessionVideo() {
      return !!(player && (player.currentSrc || player.getAttribute("src")));
    }

    // Every control that points at the source video goes away when that video does.
    function syncVideoLinks() {
      const on = hasSessionVideo();
      document.querySelectorAll(".ts").forEach((el) => el.classList.toggle("ts--off", !on));
      ["fixCompareToggle", "fixPlayMoment"].forEach((id) => {
        const btn = document.getElementById(id);
        if (btn) btn.hidden = !on;
      });
      const compare = document.getElementById("fixCompare");
      if (compare && !on) compare.hidden = true;
      if (!on) {
        ["fixVideo", "fixCompareA", "fixCompareB", "phasePreviewPlayer"].forEach((id) => {
          const el = document.getElementById(id);
          if (el && el.getAttribute("src")) {
            el.removeAttribute("src");
            el.load();
          }
        });
      }
    }

    // Forget a swing that no longer exists: drop it from the URL and the player.
    function forgetSession(sessionId) {
      if (sessionId && activeSessionId !== sessionId) return;
      activeSessionId = null;
      const url = new URL(window.location.href);
      if (url.searchParams.has("session")) {
        url.searchParams.delete("session");
        window.history.replaceState(null, "", url.pathname + url.search + url.hash);
      }
      if (player.getAttribute("src")) {
        player.removeAttribute("src");
        player.load();
      }
      syncVideoLinks();
    }

    function syncFixVideoSrc() {
      const fixVideo = document.getElementById("fixVideo");
      if (!fixVideo || !player) return;
      const src = player.currentSrc || player.src;
      if (src && fixVideo.src !== src) {
        fixVideo.src = src;
        fixVideo.load();
      }
      ["fixCompareA", "fixCompareB"].forEach((id) => {
        const el = document.getElementById(id);
        if (!el || !src) return;
        if (el.src !== src) {
          el.src = src;
          el.load();
        }
      });
    }

    function phaseTime(payload, names, fallback) {
      const phases = (payload && payload.phases) || [];
      for (const name of names) {
        const hit = phases.find((p) => String(p.label || p.id || "").toLowerCase().includes(name));
        if (hit && Number.isFinite(Number(hit.key_time))) return Number(hit.key_time);
        if (hit && Number.isFinite(Number(hit.start_time))) return Number(hit.start_time);
      }
      return fallback;
    }

    function setupCompare(payload) {
      const t0 = Number(payload.start_time) || 0;
      const impact = payload.impact_time != null ? Number(payload.impact_time) : null;
      compareTimes = {
        a: phaseTime(payload, ["address", "setup"], t0),
        b: Number.isFinite(impact)
          ? impact
          : phaseTime(payload, ["impact"], t0 + Math.max(0.2, (Number(payload.end_time) || t0) - t0) * 0.55),
      };
      const labelA = document.getElementById("fixCompareLabelA");
      const labelB = document.getElementById("fixCompareLabelB");
      if (labelA) labelA.textContent = "Address";
      if (labelB) labelB.textContent = "Impact";
      syncFixVideoSrc();
      const seekPair = () => {
        const a = document.getElementById("fixCompareA");
        const b = document.getElementById("fixCompareB");
        if (a) {
          const go = () => { try { a.currentTime = compareTimes.a; } catch (_) {} };
          if (a.readyState >= 1) go();
          else a.addEventListener("loadeddata", go, { once: true });
        }
        if (b) {
          const go = () => { try { b.currentTime = compareTimes.b; } catch (_) {} };
          if (b.readyState >= 1) go();
          else b.addEventListener("loadeddata", go, { once: true });
        }
      };
      seekPair();
    }

    function setCompareWipe(pct) {
      const stage = document.getElementById("fixCompareStage");
      if (stage) stage.style.setProperty("--compare", Math.max(0, Math.min(100, Number(pct) || 0)) + "%");
    }

    function matchCoachBrief(question) {
      const q = String(question || "").trim().toLowerCase();
      if (!q || !coachBriefs.length) return null;
      for (const brief of coachBriefs) {
        const bq = String(brief.question || "").trim().toLowerCase();
        if (q === bq || q.includes(bq) || bq.includes(q)) return brief;
      }
      if (q.includes("pelvis") || q.includes("hip") || q.includes("drift")) {
        return coachBriefs.find((b) => String(b.id || "").includes("pelvis") || String(b.question || "").toLowerCase().includes("pelvis")) || null;
      }
      if (q.includes("lead arm") || q.includes("arm") || q.includes("collaps")) {
        return coachBriefs.find((b) => String(b.id || "").includes("arm") || String(b.question || "").toLowerCase().includes("lead arm")) || null;
      }
      if (q.includes("biggest") || q.includes("fix") || q.includes("work on")) {
        return coachBriefs.find((b) => b.id === "biggest_fix") || coachBriefs[0] || null;
      }
      if (q.includes("downswing") || q.includes("walk me") || q.includes("phase")) {
        return coachBriefs.find((b) => b.id === "downswing_walkthrough") || null;
      }
      return null;
    }

    function renderInstantChips() {
      const host = document.getElementById("fixInstantChips");
      if (!host) return;
      host.innerHTML = "";
      if (isDevUi) return;
      (coachBriefs || []).slice(0, 4).forEach((brief) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = brief.question;
        btn.addEventListener("click", () => applyInstantBrief(brief));
        host.appendChild(btn);
      });
    }

    function applyInstantBrief(brief) {
      if (!brief) return;
      if (brief.finding_id) selectFix(brief.finding_id, { play: true });
      const drawer = document.getElementById("explainDrawer");
      const backdrop = document.getElementById("explainBackdrop");
      const body = document.getElementById("explainBody");
      const title = document.getElementById("explainTitle");
      if (title) title.textContent = brief.question || "Instant answer";
      if (body) {
        body.innerHTML = String(brief.answer || "")
          .split(/\n\n+/)
          .map((p) => `<p>${escapeHtml(p)}</p>`)
          .join("");
      }
      if (drawer) {
        drawer.hidden = false;
        drawer.classList.add("is-open");
      }
      if (backdrop) {
        backdrop.hidden = false;
        backdrop.classList.add("is-open");
      }
      status.textContent = "Instant answer";
      status.className = "status";
      addBubble("You", escapeHtml(brief.question || ""), "user");
      addBubble("Coach", renderMarkdown(String(brief.answer || "").replace(/\n/g, "\n\n")), "agent", {
        interim: false,
        markdown: true,
      });
    }

    async function refreshCoachBriefs() {
      if (isDevUi) return;
      const sid = (session && session.session_id) || "";
      if (session && Array.isArray(session.coach_briefs) && session.coach_briefs.length) {
        coachBriefs = session.coach_briefs.slice();
        renderInstantChips();
      }
      try {
        const q = sid ? ("?video_id=" + encodeURIComponent(sid) + "&sport=golf") : "?sport=golf";
        const r = await fetch("/api/coach_briefs" + q + "&t=" + Date.now(), { headers: authHeaders() });
        const data = await r.json();
        if (!data.error && Array.isArray(data.briefs)) {
          coachBriefs = data.briefs;
          if (session) session.coach_briefs = data.briefs;
          renderInstantChips();
        }
      } catch (_) { /* keep session briefs */ }
    }

    function voiceAskButtons() {
      return [
        document.getElementById("fixVoice"),
        document.getElementById("coachVoice"),
      ].filter(Boolean);
    }

    function setVoiceListening(on) {
      voiceAskButtons().forEach((btn) => {
        btn.classList.toggle("is-listening", !!on);
        if (btn.id === "coachVoice") {
          btn.setAttribute("aria-pressed", on ? "true" : "false");
          btn.setAttribute("aria-label", on ? "Stop recording" : "Talk to coach");
          btn.title = on ? "Stop recording" : "Talk to coach";
        }
      });
    }

    function clearVoiceRestartTimer() {
      if (!voiceRestartTimer) return;
      window.clearTimeout(voiceRestartTimer);
      voiceRestartTimer = null;
    }

    function finishVoiceAsk(rawText) {
      const text = String(rawText || "").replace(/\s+/g, " ").trim();
      if (!text) {
        status.textContent = "Couldn’t catch that — tap the mic, speak, then tap again to send.";
        status.className = "status warn";
        return;
      }
      // Put the final transcript in the coach box and ask — never divert to instant-fix cards.
      showConsumerChat({ focusInput: false });
      if (qInput) qInput.value = text;
      ask({ skipBrief: true });
    }

    function beginVoiceRecognitionSession() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition || !voiceArmed) return;
      if (voiceRecognition) return;
      const rec = new SpeechRecognition();
      voiceRecognition = rec;
      rec.lang = "en-US";
      // Stay armed until the user toggles off. Do not paint interim text into the composer.
      rec.continuous = true;
      rec.interimResults = false;
      rec.maxAlternatives = 1;
      rec.onresult = (event) => {
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const result = event.results[i];
          if (!result || !result.isFinal) continue;
          const piece = result[0] && result[0].transcript ? String(result[0].transcript).trim() : "";
          if (piece) voiceTranscriptParts.push(piece);
        }
      };
      rec.onerror = (event) => {
        const err = event && event.error;
        if (err === "aborted" || err === "no-speech") return;
        if (err === "not-allowed" || err === "service-not-allowed") {
          voiceArmed = false;
          clearVoiceRestartTimer();
          setVoiceListening(false);
          status.textContent = "Microphone permission is blocked for this browser.";
          status.className = "status warn";
          return;
        }
        status.textContent = "Voice input failed — tap the mic and try again.";
        status.className = "status warn";
      };
      rec.onend = () => {
        voiceRecognition = null;
        if (!voiceArmed) {
          const parts = voiceTranscriptParts.slice();
          voiceTranscriptParts = [];
          setVoiceListening(false);
          finishVoiceAsk(parts.join(" "));
          return;
        }
        // Chrome forbids starting a new session synchronously inside onend.
        clearVoiceRestartTimer();
        voiceRestartTimer = window.setTimeout(() => {
          voiceRestartTimer = null;
          if (voiceArmed) beginVoiceRecognitionSession();
        }, 140);
      };
      try {
        rec.start();
      } catch (_) {
        clearVoiceRestartTimer();
        voiceRestartTimer = window.setTimeout(() => {
          voiceRestartTimer = null;
          if (voiceArmed) beginVoiceRecognitionSession();
        }, 220);
      }
    }

    function startVoiceAsk({ jumpToCoach = false } = {}) {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        status.textContent = "Voice input is not supported in this browser.";
        status.className = "status warn";
        return;
      }
      if (jumpToCoach) showConsumerChat({ focusInput: false });

      // Toggle off → stop listening, then transcribe once.
      if (voiceArmed) {
        voiceArmed = false;
        clearVoiceRestartTimer();
        if (voiceRecognition) {
          try { voiceRecognition.stop(); } catch (_) { /* ignore */ }
        } else {
          const parts = voiceTranscriptParts.slice();
          voiceTranscriptParts = [];
          setVoiceListening(false);
          finishVoiceAsk(parts.join(" "));
        }
        return;
      }

      // Toggle on → record only; transcript waits until the next tap.
      voiceArmed = true;
      voiceTranscriptParts = [];
      setVoiceListening(true);
      beginVoiceRecognitionSession();
    }

    function bindVoiceToggle(btn) {
      if (!btn || btn.dataset.voiceBound === "1") return;
      btn.dataset.voiceBound = "1";
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        startVoiceAsk();
      });
    }

    function renderFixPhases(payload) {
      const host = document.getElementById("fixPhases");
      if (!host) return;
      host.innerHTML = "";
      (payload && payload.phases || []).forEach((phase) => {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = phase.label || phase.id || "Phase";
        btn.dataset.start = String(phase.start_time);
        btn.dataset.end = String(phase.end_time);
        btn.addEventListener("click", () => {
          seekPlayer(phase.start_time);
          if (player) player.play().catch(() => {});
          const fv = document.getElementById("fixVideo");
          if (fv) fv.play().catch(() => {});
        });
        li.appendChild(btn);
        host.appendChild(li);
      });
    }

    function renderFixTabs(findings) {
      const host = document.getElementById("fixFindingTabs");
      if (!host) return;
      host.innerHTML = "";
      (findings || []).forEach((note, idx) => {
        const btn = document.createElement("button");
        btn.type = "button";
        const label = note.headline || note.title || "Fix";
        btn.textContent = String(idx + 1).padStart(2, "0") + " " + label;
        btn.dataset.id = note.id;
        btn.classList.toggle("is-active", note.id === (selectedFixId || sportOpenId));
        btn.addEventListener("click", () => selectFix(note.id, { play: true }));
        host.appendChild(btn);
      });
    }

    function paintFixHero(note) {
      const hero = document.getElementById("fixHero");
      const fab = document.getElementById("coachFab");
      if (!hero) return;
      if (window.cerebelSkeleton) window.cerebelSkeleton.onNote(isDevUi ? null : note, lastSportPayload);
      if (isDevUi || !lastSportPayload || !note) {
        hero.hidden = true;
        if (fab) fab.hidden = true;
        syncTocHome();
        return;
      }
      hero.hidden = false;
      if (fab) fab.hidden = false;
      syncTocHome();
      const headline = document.getElementById("fixHeadline");
      const plain = document.getElementById("fixPlain");
      const evidence = document.getElementById("fixEvidence");
      const cue = document.getElementById("fixCue");
      if (headline) {
        headline.textContent = note.headline || note.title || "Main fix";
      }
      if (plain) {
        plain.textContent = note.plain || note.detail || note.why || "";
      }
      if (evidence) {
        const value = note.metric && note.metric.value;
        const cap = (note.metric && note.metric.caption) || "";
        if (value) {
          evidence.hidden = false;
          evidence.innerHTML =
            "<strong>" + escapeHtml(String(value)) + "</strong>" +
            (cap ? "<span>" + escapeHtml(String(cap)) + "</span>" : "");
        } else {
          evidence.hidden = true;
          evidence.textContent = "";
        }
      }
      if (cue) {
        cue.innerHTML = note.cue
          ? "<b>Feel</b> " + escapeHtml(note.cue)
          : "";
      }
      document.querySelectorAll("#fixFindingTabs button").forEach((btn) => {
        btn.classList.toggle("is-active", btn.dataset.id === note.id);
      });
      const drill = document.getElementById("fixDrill");
      if (drill && !drill.hidden) paintDrill(note);
    }

    // Practice log: stored on the user's own capture, so only signed-in owners can mark it.
    function ownCapture(sessionId) {
      return sessionId ? libraryCaptures.find((row) => row.session_id === sessionId) || null : null;
    }

    function practiceCount(sessionId, findingId) {
      const row = ownCapture(sessionId);
      return row ? (row.practice || []).filter((p) => p.finding_id === findingId).length : 0;
    }

    function paintDrill(note) {
      const drillText = document.getElementById("fixDrillText");
      const check = document.getElementById("fixDrillCheck");
      const actions = document.getElementById("fixDrillActions");
      const count = document.getElementById("fixDrillCount");
      const refilm = document.getElementById("fixRefilm");
      if (drillText) drillText.textContent = note.drill || "No drill for this fix yet.";
      if (check) {
        check.hidden = !note.check;
        check.innerHTML = note.check ? "<b>Next swing</b> " + escapeHtml(note.check) : "";
      }
      const mine = !!(authToken && ownCapture(activeSessionId));
      if (actions) actions.hidden = !mine || !note.drill;
      const n = mine ? practiceCount(activeSessionId, note.id) : 0;
      if (count) count.textContent = n ? "Practiced " + n + (n === 1 ? " time" : " times") : "";
      if (refilm) refilm.hidden = n === 0;
    }

    async function markPracticed() {
      const note = currentFixNote();
      const button = document.getElementById("fixDrillDone");
      if (!note || !activeSessionId) return;
      if (button) button.disabled = true;
      try {
        const r = await fetch("/api/library/practice", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ session_id: activeSessionId, finding_id: note.id }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          showToast(r.status === 401 ? "Sign in to keep track of your practice." : (data.message || "Couldn't save that. Try again."));
          return;
        }
        const row = ownCapture(activeSessionId);
        if (row) row.practice = (row.practice || []).concat({ finding_id: note.id, at: new Date().toISOString() });
        paintDrill(note);
        showToast("Marked as practiced. Film again when you're ready to check it.");
      } catch (_) {
        showToast("Couldn't save that. Check your connection and try again.");
      } finally {
        if (button) button.disabled = false;
      }
    }

    function selectFix(id, { play = false } = {}) {
      const note = (sportFindings || []).find((n) => n.id === id);
      if (!note) return;
      selectedFixId = note.id;
      sportOpenId = note.id;
      paintSportCaption(note);
      paintFixHero(note);
      renderFixTabs(sportFindings);
      if (play) {
        seekPlayer(note.start_time != null ? note.start_time : (lastSportPayload && lastSportPayload.start_time));
        if (player) player.play().catch(() => {});
        const fv = document.getElementById("fixVideo");
        if (fv) fv.play().catch(() => {});
      }
      document.querySelectorAll(".ma-plan li").forEach((li) => {
        const open = li.dataset.id === note.id;
        li.classList.toggle("is-open", open);
        const body = li.querySelector(".ma-plan__body");
        if (body) body.hidden = !open;
      });
    }

    function openExplainDrawer(note, { followUp = false } = {}) {
      const drawer = document.getElementById("explainDrawer");
      const backdrop = document.getElementById("explainBackdrop");
      const body = document.getElementById("explainBody");
      const title = document.getElementById("explainTitle");
      if (!drawer || !body || !note) return;
      if (title) title.textContent = note.headline || note.title || "Why this matters";
      const metricLine = note.metric && note.metric.value
        ? `<p><strong>Measured:</strong> ${escapeHtml(String(note.metric.value || ""))}` +
          (note.metric.caption ? ` — ${escapeHtml(String(note.metric.caption))}` : "") +
          `</p>`
        : "";
      body.innerHTML =
        (note.plain ? `<p>${escapeHtml(note.plain)}</p>` : "") +
        (note.why ? `<p>${escapeHtml(note.why)}</p>` : "") +
        metricLine +
        (note.cue ? `<p><strong>What to feel:</strong> ${escapeHtml(note.cue)}</p>` : "") +
        (note.drill ? `<p><strong>Try this:</strong> ${escapeHtml(note.drill)}</p>` : "");
      drawer.hidden = false;
      drawer.classList.add("is-open");
      if (backdrop) {
        backdrop.hidden = false;
        backdrop.classList.add("is-open");
      }
      explainFollowUp = followUp;
    }

    function closeExplainDrawer() {
      const drawer = document.getElementById("explainDrawer");
      const backdrop = document.getElementById("explainBackdrop");
      if (drawer) {
        drawer.classList.remove("is-open");
        drawer.hidden = true;
      }
      if (backdrop) {
        backdrop.classList.remove("is-open");
        backdrop.hidden = true;
      }
      explainFollowUp = false;
    }

    function showConsumerChat({ focusInput = true } = {}) {
      if (isDevUi) return;
      document.body.classList.add("pg-consumer-chat-open");
      const coach = document.getElementById("coach");
      if (coach) coach.scrollIntoView({ behavior: "smooth", block: "start" });
      closeExplainDrawer();
      if (focusInput && qInput) qInput.focus();
    }

    function bindFixHeroControls() {
      const why = document.getElementById("fixWhy");
      const drillBtn = document.getElementById("fixDrillBtn");
      const playMoment = document.getElementById("fixPlayMoment");
      const fab = document.getElementById("coachFab");
      const closeBtn = document.getElementById("explainClose");
      const backdrop = document.getElementById("explainBackdrop");
      const askMore = document.getElementById("explainAskMore");
      const openChat = document.getElementById("explainOpenChat");
      if (why) {
        why.addEventListener("click", () => {
          const note = currentFixNote();
          if (note) openExplainDrawer(note);
        });
      }
      if (drillBtn) {
        drillBtn.addEventListener("click", () => {
          const note = currentFixNote();
          const drill = document.getElementById("fixDrill");
          if (!note || !drill) return;
          drill.hidden = false;
          paintDrill(note);
          drill.scrollIntoView({ behavior: "smooth", block: "nearest" });
        });
      }
      const drillDone = document.getElementById("fixDrillDone");
      if (drillDone) drillDone.addEventListener("click", markPracticed);
      const refilm = document.getElementById("fixRefilm");
      if (refilm) {
        refilm.addEventListener("click", () => {
          const input = document.getElementById("uploadFile");
          if (input) input.click();
        });
      }
      if (playMoment) {
        playMoment.addEventListener("click", () => {
          const note = currentFixNote();
          if (!note) return;
          selectFix(note.id, { play: true });
        });
      }
      if (fab) fab.addEventListener("click", showConsumerChat);
      if (closeBtn) closeBtn.addEventListener("click", closeExplainDrawer);
      if (backdrop) backdrop.addEventListener("click", closeExplainDrawer);
      if (openChat) openChat.addEventListener("click", showConsumerChat);
      if (askMore) {
        askMore.addEventListener("click", () => {
          const note = currentFixNote();
          if (!note || !qInput) return;
          const prompt =
            "Explain this fix in simple coaching language and how I should feel it: " +
            (note.title || "this issue") +
            (note.why ? ". Why: " + note.why : "") +
            (note.cue ? " Cue: " + note.cue : "");
          explainFollowUp = true;
          openExplainDrawer(note, { followUp: true });
          const body = document.getElementById("explainBody");
          if (body) {
            body.innerHTML += `<p class="status">Coach is answering…</p>`;
          }
          qInput.value = prompt;
          ask();
        });
      }
      const fixVideo = document.getElementById("fixVideo");
      if (fixVideo && player) {
        fixVideo.addEventListener("timeupdate", () => {
          if (!player || Math.abs(player.currentTime - fixVideo.currentTime) < 0.12) return;
          player.currentTime = fixVideo.currentTime;
        });
      }
      const compareToggle = document.getElementById("fixCompareToggle");
      const compareRange = document.getElementById("fixCompareRange");
      const compareBox = document.getElementById("fixCompare");
      const videoWrap = document.getElementById("fixVideoWrap");
      if (compareToggle && compareBox) {
        compareToggle.addEventListener("click", () => {
          const on = compareBox.hidden;
          compareBox.hidden = !on;
          if (videoWrap) videoWrap.classList.toggle("is-compare-hidden", on);
          compareToggle.classList.toggle("primary", on);
          if (on) {
            setupCompare(lastSportPayload || { start_time: 0, end_time: 1, phases: [] });
            setCompareWipe((compareRange && compareRange.value) || 50);
          }
        });
      }
      if (compareRange) {
        compareRange.addEventListener("input", () => setCompareWipe(compareRange.value));
        setCompareWipe(compareRange.value);
      }
      const voiceOk = !!(window.SpeechRecognition || window.webkitSpeechRecognition);
      const fixVoice = document.getElementById("fixVoice");
      const coachVoice = document.getElementById("coachVoice");
      if (!voiceOk) {
        if (fixVoice) fixVoice.hidden = true;
        if (coachVoice) coachVoice.hidden = true;
      } else {
        if (fixVoice && fixVoice.dataset.voiceBound !== "1") {
          fixVoice.dataset.voiceBound = "1";
          fixVoice.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            startVoiceAsk({ jumpToCoach: true });
          });
        }
        bindVoiceToggle(coachVoice);
      }
    }

    function activeSportFinding(time) {
      const notes = sportFindings || [];
      const inside = notes.filter((note) => time >= Number(note.start_time) && time <= Number(note.end_time));
      if (inside.length) {
        return inside.reduce((best, note) => (
          Number(note.start_time) >= Number(best.start_time) ? note : best
        ));
      }
      // After a fix's window, stay on the latest one already reached.
      let last = null;
      notes.forEach((note) => {
        if (Number(note.start_time) <= time && (!last || Number(note.start_time) >= Number(last.start_time))) {
          last = note;
        }
      });
      return last || notes[0] || null;
    }

    function fixPeakTime(note) {
      const t0 = Number(note.start_time);
      const t1 = Number(note.end_time);
      const mid = (t0 + t1) / 2;
      if (!Number.isFinite(t0) || !Number.isFinite(t1)) return mid;
      const seriesId = note.series;
      const series = ((lastSportPayload && lastSportPayload.series) || []).find((s) => s.id === seriesId);
      if (!series || !series.times || !series.values || !series.times.length) return mid;
      const goal = note.goal && note.goal.value != null ? Number(note.goal.value) : null;
      let bestT = mid;
      let bestScore = -Infinity;
      for (let i = 0; i < series.times.length; i += 1) {
        const t = Number(series.times[i]);
        if (!(t >= t0 && t <= t1)) continue;
        const v = Number(series.values[i]);
        if (!Number.isFinite(v)) continue;
        const score = goal == null ? Math.abs(v) : Math.abs(v - goal);
        if (score > bestScore) {
          bestScore = score;
          bestT = t;
        }
      }
      return bestT;
    }

    function pruneFixTourAfterSeek(time) {
      if (fixTour.holdTimer) {
        clearTimeout(fixTour.holdTimer);
        fixTour.holdTimer = null;
      }
      fixTour.holdingId = null;
      fixTour.resumeAfterHold = false;
      if (sportBoard) sportBoard.classList.remove("is-holding");
      (sportFindings || []).forEach((note) => {
        if (fixPeakTime(note) > time + 0.04) fixTour.heldIds.delete(note.id);
      });
      if (fixTour.overview && (sportFindings || []).some((n) => !fixTour.heldIds.has(n.id))) {
        fixTour.overview = false;
      }
    }

    function enterFixOverview() {
      if (fixTour.overview) return;
      fixTour.overview = true;
      if (sportSkeleton && typeof sportSkeleton.setOverview === "function") {
        sportSkeleton.setOverview();
      }
      if (sportBoard) sportBoard.classList.remove("is-holding", "is-slow");
    }

    function beginFixHold(note) {
      if (!player || !note || fixTour.holdingId === note.id) return;
      if (fixTour.holdTimer) {
        clearTimeout(fixTour.holdTimer);
        fixTour.holdTimer = null;
      }
      fixTour.holdingId = note.id;
      fixTour.heldIds.add(note.id);
      fixTour.resumeAfterHold = true;
      // Pause first, then seek — avoids seek-while-playing pause events clearing resume.
      fixTour.pausingForHold = true;
      try { player.pause(); } catch (_) { /* ignore */ }
      const fixVideo = document.getElementById("fixVideo");
      if (fixVideo) {
        try { fixVideo.pause(); } catch (_) { /* ignore */ }
      }
      // Keep the guard up through any deferred pause/seeked handlers.
      window.setTimeout(() => { fixTour.pausingForHold = false; }, 80);

      const peak = fixPeakTime(note);
      try {
        if (Math.abs(player.currentTime - peak) > 0.04) player.currentTime = peak;
      } catch (_) { /* ignore */ }
      if (fixVideo && fixVideo.src) {
        try { fixVideo.currentTime = peak; } catch (_) { /* ignore */ }
      }
      if (Math.abs(player.playbackRate - 1) > 0.02) player.playbackRate = 1;
      sportOpenId = note.id;
      paintSportCaption(note);
      if (sportBoard) {
        sportBoard.classList.add("is-holding");
        sportBoard.classList.remove("is-slow");
      }
      const sportPlayBtn = document.getElementById("sportPlay");
      if (sportPlayBtn) sportPlayBtn.textContent = "❚❚";
      const holdMs = FIX_HOLD_MS + Math.min(800, Math.max(0, Number(note.severity || 0)) * 8);
      fixTour.holdTimer = setTimeout(() => {
        const shouldResume = fixTour.resumeAfterHold;
        fixTour.holdTimer = null;
        fixTour.holdingId = null;
        if (sportBoard) sportBoard.classList.remove("is-holding");
        const allDone = fixTour.heldIds.size >= (sportFindings || []).length;
        if (allDone) enterFixOverview();
        if (!shouldResume || !player) {
          fixTour.resumeAfterHold = false;
          if (sportPlayBtn) sportPlayBtn.textContent = "▶";
          return;
        }
        // Nudge just past the peak so a near-start peak can't re-arm the same hold.
        try {
          const nudge = peak + 0.06;
          if (player.currentTime < nudge) player.currentTime = nudge;
          if (fixVideo && fixVideo.src && fixVideo.currentTime < nudge) {
            fixVideo.currentTime = nudge;
          }
        } catch (_) { /* ignore */ }
        // Skip the "restart tour near swing start" reset on this programmatic play().
        fixTour.resumingFromHold = true;
        const resume = player.play();
        const afterPlay = () => {
          fixTour.resumingFromHold = false;
          fixTour.resumeAfterHold = false;
          if (sportPlayBtn) sportPlayBtn.textContent = "❚❚";
          if (fixVideo && fixVideo.src) {
            try { fixVideo.play().catch(() => {}); } catch (_) { /* ignore */ }
          }
        };
        if (resume && typeof resume.then === "function") {
          resume.then(afterPlay).catch(() => {
            fixTour.resumingFromHold = false;
            fixTour.resumeAfterHold = false;
            if (sportPlayBtn) sportPlayBtn.textContent = "▶";
          });
        } else {
          afterPlay();
        }
      }, holdMs);
    }

    function syncSportFixTour(time) {
      if (!player || !lastSportPayload || !sportBoard || sportBoard.hidden) return;
      if (!sportFindings.length) return;
      if (Math.abs(player.playbackRate - 1) > 0.02) player.playbackRate = 1;
      if (fixTour.holdingId) return;
      if (player.paused) return;

      const pending = sportFindings
        .filter((note) => !fixTour.heldIds.has(note.id))
        .sort((a, b) => fixPeakTime(a) - fixPeakTime(b));
      for (let i = 0; i < pending.length; i += 1) {
        const note = pending[i];
        const peak = fixPeakTime(note);
        if (time + 0.02 >= peak) {
          beginFixHold(note);
          return;
        }
      }

      if (!fixTour.overview && fixTour.heldIds.size >= sportFindings.length) {
        const lastEnd = Math.max.apply(null, sportFindings.map((n) => Number(n.end_time)));
        if (time >= lastEnd - 0.05) enterFixOverview();
      }
    }

    function syncSportPlayhead() {
      if (!player) return;
      if (!lastSportPayload || !sportBoard || sportBoard.hidden) {
        if (Math.abs(player.playbackRate - 1) > 0.02) player.playbackRate = 1;
        if (sportBoard) sportBoard.classList.remove("is-slow", "is-holding");
        return;
      }
      const t0 = Number(lastSportPayload.start_time);
      const t1 = Number(lastSportPayload.end_time);
      const span = Math.max(t1 - t0, 1e-6);
      const time = player && Number.isFinite(player.currentTime) ? player.currentTime : t0;
      const clamped = Math.min(t1, Math.max(t0, time));
      const pct = ((clamped - t0) / span) * 100;
      const deck = document.getElementById("sportDeck");
      if (deck) deck.style.setProperty("--playhead", pct + "%");
      const scrub = document.getElementById("sportScrub");
      if (scrub && document.activeElement !== scrub) scrub.value = String(clamped);
      (lastSportPayload.series || []).forEach((trace) => {
        const out = document.querySelector('[data-readout="' + trace.id + '"]');
        if (out) out.textContent = formatTraceValue(valueAt(trace, clamped), trace.unit);
      });
      syncSportFixTour(time);
      const live = (fixTour.holdingId && sportFindings.find((n) => n.id === fixTour.holdingId))
        || activeSportFinding(clamped);
      if (live) sportOpenId = live.id;
      if (!fixTour.overview) paintSportCaption(live);
      document.querySelectorAll(".ma-plan li").forEach((li) => {
        const open = live && li.dataset.id === live.id;
        li.classList.toggle("is-open", !!open);
        const body = li.querySelector(".ma-plan__body");
        if (body) body.hidden = !open;
        const caret = li.querySelector(".ma-plan__caret");
        if (caret) caret.textContent = open ? "▴" : "▾";
      });
      document.querySelectorAll(".ma__phases li").forEach((li) => {
        const a = Number(li.dataset.start);
        const b = Number(li.dataset.end);
        li.classList.toggle("is-active", clamped >= a && clamped <= b);
      });
      document.querySelectorAll("#fixPhases button").forEach((btn) => {
        const a = Number(btn.dataset.start);
        const b = Number(btn.dataset.end);
        btn.classList.toggle("is-active", clamped >= a && clamped <= b);
      });
      const fv = document.getElementById("fixVideo");
      if (fv && fv.src && !fv.seeking && Math.abs(fv.currentTime - clamped) > 0.2) {
        try { fv.currentTime = clamped; } catch (_) { /* ignore */ }
      }
    }

    function paintSportCaption(note, { pinHero = false } = {}) {
      const metric = document.getElementById("sportMetric");
      const caption = document.getElementById("sportCaption");
      const cue = document.getElementById("sportCue");
      const stage = sportBoard && sportBoard.querySelector(".ma__stage");
      if (!note) return;
      if (pinHero || isDevUi) selectedFixId = note.id;
      if (stage) stage.style.setProperty("--accent", note.accent || "#b99bff");
      if (sportSkeleton) sportSkeleton.setFocus(note);
      if (metric) metric.textContent = (note.metric && note.metric.value) || "";
      if (caption) caption.textContent = (note.metric && note.metric.caption) || note.detail || "";
      if (cue) {
        cue.innerHTML = note.cue ? "<b>Fix</b> " + escapeHtml(note.cue) : escapeHtml(note.detail || "");
      }
      document.querySelectorAll(".ma__steps button").forEach((btn) => {
        btn.classList.toggle("is-active", btn.dataset.id === note.id);
        if (btn.dataset.id === note.id) btn.style.setProperty("--accent", note.accent || "#b99bff");
      });
      // Consumer hero stays on the ranked "biggest fix" (or user-picked tab),
      // not whatever window the playhead happens to be inside.
      if (!isDevUi) {
        paintFixHero(currentFixNote() || note);
      } else {
        paintFixHero(note);
      }
    }

    function renderSportFindings(findings) {
      const list = document.getElementById("sportFindings");
      const steps = document.getElementById("sportSteps");
      if (!list || !steps) return;
      sportFindings = findings || [];
      if (!sportOpenId && sportFindings[0]) sportOpenId = sportFindings[0].id;
      list.innerHTML = "";
      steps.innerHTML = "";
      sportFindings.forEach((note, index) => {
        const accent = note.accent || "#b99bff";
        const step = document.createElement("button");
        step.type = "button";
        step.dataset.id = note.id;
        step.textContent = String(index + 1).padStart(2, "0");
        step.style.setProperty("--accent", accent);
        step.addEventListener("click", () => {
          sportOpenId = note.id;
          seekPlayer(note.start_time);
          renderSportFindings(sportFindings);
        });
        steps.appendChild(step);

        const li = document.createElement("li");
        const open = sportOpenId === note.id;
        li.dataset.id = note.id;
        li.className = open ? "is-open" : "";
        li.style.setProperty("--accent", accent);
        const group = note.group;
        li.innerHTML =
          `<button type="button" class="ma-plan__head">` +
            `<span>${escapeHtml(String(index + 1).padStart(2, "0"))}</span>` +
            `<strong>${escapeHtml(note.title || "Finding")}</strong>` +
            (note.metric && note.metric.value ? `<em>${escapeHtml(note.metric.value)}</em>` : "<em></em>") +
            `<span class="ma-plan__caret" aria-hidden="true">${open ? "▴" : "▾"}</span>` +
          `</button>` +
          `<div class="ma-plan__body"${open ? "" : " hidden"}>` +
            `<dl>` +
              `<div><dt>What we saw</dt><dd>${escapeHtml(note.detail || "")}</dd></div>` +
              (note.why ? `<div><dt>Why it matters</dt><dd>${escapeHtml(note.why)}</dd></div>` : "") +
              (note.cue ? `<div class="is-action"><dt>Feel</dt><dd>${escapeHtml(note.cue)}</dd></div>` : "") +
              (note.drill ? `<div class="is-action"><dt>Drill</dt><dd>${escapeHtml(note.drill)}</dd></div>` : "") +
            `</dl>` +
            (note.check ? `<p class="ma-plan__check"><span>Next capture</span>${escapeHtml(note.check)}</p>` : "") +
            (group ? `<div class="ma-plan__muscle"><i></i><span>Strengthen</span><strong>${escapeHtml(group.label || "")}</strong><small>${escapeHtml(group.role || "")}</small></div>` : "") +
          `</div>`;
        li.querySelector(".ma-plan__head").addEventListener("click", () => {
          sportOpenId = open ? null : note.id;
          if (!open) seekPlayer(note.start_time);
          renderSportFindings(sportFindings);
        });
        list.appendChild(li);
      });
      paintSportCaption(sportFindings.find((note) => note.id === sportOpenId) || sportFindings[0]);
    }

    function renderSportBoard(payload, findingsOverride) {
      if (!sportBoard || !payload || payload.ui_hint !== "sport_motion_board") return;
      lastSportPayload = payload;
      resetFixTour();
      sportBoard.hidden = false;
      const badge = document.getElementById("sportBadge");
      const kicker = document.getElementById("sportKicker");
      const title = document.getElementById("sportTitle");
      if (badge) badge.textContent = (payload.disclosure && payload.disclosure.badge) || payload.badge || "Motion analysis";
      if (kicker) kicker.textContent = payload.sport === "baseball" ? "Pitch analysis" : "Swing analysis";
      if (title) title.textContent = payload.title || "Motion analysis";

      const t0 = Number(payload.start_time);
      const t1 = Number(payload.end_time);
      const span = Math.max(t1 - t0, 1e-6);
      const scrub = document.getElementById("sportScrub");
      if (scrub) {
        scrub.min = String(t0);
        scrub.max = String(t1);
        scrub.value = String(t0);
      }
      const phases = document.getElementById("sportPhases");
      phases.innerHTML = "";
      (payload.phases || []).forEach((phase) => {
        const li = document.createElement("li");
        li.dataset.start = String(phase.start_time);
        li.dataset.end = String(phase.end_time);
        li.style.setProperty("--span", String(Math.max(0.15, Number(phase.end_time) - Number(phase.start_time))));
        li.textContent = phase.label || phase.id || "Phase";
        li.title = li.textContent;
        li.addEventListener("click", () => seekPlayer(phase.start_time));
        phases.appendChild(li);
      });

      const deck = document.getElementById("sportDeck");
      deck.querySelectorAll(".ma-track").forEach((node) => node.remove());
      const findings = findingsOverride || payload.findings || [];
      (payload.series || []).forEach((trace) => {
        const geo = seriesGeometry(trace.times, trace.values);
        const wrap = document.createElement("div");
        wrap.className = "ma-track";
        const last = (trace.values || [])[0];
        wrap.innerHTML =
          `<div class="ma-track__head">` +
          `<span>${escapeHtml(trace.label)}</span>` +
          `<p><output data-readout="${escapeHtml(trace.id)}">${formatTraceValue(last, trace.unit)}</output></p></div>` +
          `<div class="ma-track__lane"></div>`;
        const lane = wrap.querySelector(".ma-track__lane");
        const windows = findings.filter((note) => note.series === trace.id);
        const buttons = windows.map((note) => {
          const left = ((Number(note.start_time) - t0) / span) * 100;
          const width = ((Number(note.end_time) - Number(note.start_time)) / span) * 100;
          return `<button type="button" data-seek="${note.start_time}" style="--accent:${note.accent || "#b99bff"};left:${left}%;width:${Math.max(width, 1.2)}%" aria-label="${escapeHtml(note.title || "")}"></button>`;
        }).join("");
        const zero = (geo.min < 0 && geo.max > 0)
          ? `<line class="ma-track__zero" x1="0" x2="100" y1="${geo.y(0).toFixed(2)}" y2="${geo.y(0).toFixed(2)}"></line>`
          : "";
        const goals = windows.filter((note) => note.goal && note.goal.value != null).map((note) => {
          const y = geo.y(Number(note.goal.value)).toFixed(2);
          const x1 = ((Number(note.start_time) - t0) / span) * 100;
          const x2 = ((Number(note.end_time) - t0) / span) * 100;
          return `<line class="ma-track__goal" style="--accent:${note.accent || "#b99bff"}" x1="${x1}" x2="${x2}" y1="${y}" y2="${y}"></line>`;
        }).join("");
        lane.innerHTML =
          buttons +
          `<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">${zero}${goals}<path d="${geo.d}"></path></svg>` +
          `<i class="ma-track__playhead" aria-hidden="true"></i>`;
        lane.querySelectorAll("button").forEach((btn) => {
          btn.addEventListener("click", () => seekPlayer(btn.dataset.seek));
        });
        deck.appendChild(wrap);
      });

      const stamped = findings.map((note) => {
        if (note.focus) return note;
        const prior = (payload.findings || []).find((item) => item.id === note.id || item.series === note.series);
        return Object.assign({}, note, { focus: prior && prior.focus });
      });
      sportOpenId = stamped[0] ? stamped[0].id : null;
      selectedFixId = sportOpenId;
      renderSportFindings(stamped);
      renderFixTabs(stamped);
      renderFixPhases(payload);
      syncFixVideoSrc();
      ensureSportCloseup(payload);
      const highlights = document.getElementById("sportHighlights");
      highlights.innerHTML = "";
      (payload.highlights || []).forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item;
        highlights.appendChild(li);
      });
      const trail = document.getElementById("sportTrail");
      const disclosure = document.getElementById("sportDisclosure");
      if (trail) trail.innerHTML = `<i class="ma__swatch" aria-hidden="true"></i>${escapeHtml(payload.trail_label || "Measured path")}`;
      if (disclosure) {
        const d = payload.disclosure || {};
        disclosure.textContent = [d.motion, d.coaching].filter(Boolean).join(" ");
      }
      const primary = stamped[0] || null;
      selectedFixId = primary ? primary.id : null;
      if (primary) paintSportCaption(primary, { pinHero: true });
      else paintFixHero(null);
      setupCompare(payload);
      refreshCoachBriefs();
      syncSportPlayhead();
      if (!isDevUi && sportBoard) {
        // Keep measured board under the fix hero for body + timeline.
        sportBoard.hidden = false;
      }
    }

    function splitCerebelAnalysis(text) {
      const raw = String(text || "");
      const match = raw.match(/```cerebel-analysis\s*([\s\S]*?)```/i);
      if (!match) return { prose: raw, analysis: null };
      let analysis = null;
      try { analysis = JSON.parse(match[1]); } catch (_) { analysis = null; }
      return { prose: raw.replace(match[0], "").trim(), analysis };
    }

    function applyModelAnalysis(analysis) {
      if (!analysis || !lastSportPayload) return;
      const findings = Array.isArray(analysis.findings) ? analysis.findings : null;
      if (findings && findings.length) renderSportBoard(lastSportPayload, findings);
      if (Array.isArray(analysis.highlights) && analysis.highlights.length) {
        const highlights = document.getElementById("sportHighlights");
        if (highlights) {
          highlights.innerHTML = "";
          analysis.highlights.forEach((item) => {
            const li = document.createElement("li");
            li.textContent = String(item);
            highlights.appendChild(li);
          });
        }
      }
    }

    function maybeRenderSportTool(ev) {
      if (!ev || ev.name !== "build_sport_analysis") return;
      const rs = ev.result_summary || {};
      if (rs.error || rs.ui_hint !== "sport_motion_board") return;
      renderSportBoard(rs);
    }

    function maybeRenderGolfTool(ev) {
      if (!ev || ev.name !== "segment_golf_swings") return;
      const rs = ev.result_summary || {};
      if (rs.error || rs.ui_hint !== "golf_phase_cards") return;
      renderGolfPhaseCards(rs);
    }

    function linkTimestamps(text) {
      return escapeHtml(text)
        .replace(/\b(\d+\.\d+)\s*s\b/g, (m, n) => `<span class="ts" data-t="${n}">${n} s</span>`)
        .replace(/\bat\s+(\d+\.\d+)\b/g, (m, n) => `at <span class="ts" data-t="${n}">${n}</span>`);
    }

    function addBubble(kind, html, cls, { interim = false, markdown = false } = {}) {
      const div = document.createElement("div");
      const classes = ["bubble", cls || kind.toLowerCase()];
      if (markdown) classes.push("md");
      if (interim) classes.push("interim");
      else if (cls === "agent" || kind === "Agent") classes.push("final");
      div.className = classes.join(" ");
      div.innerHTML = `<div class="label">${kind}</div>${html}`;
      chat.appendChild(div);
      chat.scrollTop = chat.scrollHeight;
      div.querySelectorAll(".ts").forEach((el) => {
        el.classList.toggle("ts--off", !hasSessionVideo());
        el.addEventListener("click", () => {
          const t = parseFloat(el.dataset.t);
          if (!Number.isNaN(t) && hasSessionVideo()) {
            player.currentTime = t;
            player.play();
          }
        });
      });
      return div;
    }

    function upsertStatusBubble(message) {
      let el = document.getElementById("liveStatusBubble");
      if (!el) {
        el = document.createElement("div");
        el.id = "liveStatusBubble";
        el.className = "bubble status-live interim";
        chat.appendChild(el);
      }
      el.innerHTML = `<div class="label">Status</div>${
        String(message || "")
          .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      }`;
      chat.scrollTop = chat.scrollHeight;
      return el;
    }

    function upsertThinkingBubble(text) {
      let el = document.getElementById("liveThinkingBubble");
      if (!el) {
        el = document.createElement("div");
        el.id = "liveThinkingBubble";
        el.className = "bubble thinking interim";
        chat.appendChild(el);
      }
      const safe = String(text || "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      el.innerHTML = `<div class="label">Thinking</div><div class="thinking-body">${safe}</div>`;
      chat.scrollTop = chat.scrollHeight;
      return el;
    }

    function clearThinkingBubble() {
      const el = document.getElementById("liveThinkingBubble");
      if (el) el.remove();
    }

    function clearStatusBubble() {
      const el = document.getElementById("liveStatusBubble");
      if (el) el.remove();
    }

    function renderStreamEvent(ev, collectedTrace) {
      const type = ev.type || ev.kind;
      if (type === "status") {
        // Keep chrome quiet; only a generic footer status.
        status.textContent = "Working…";
        status.className = "status";
        return;
      }
      if (type === "thinking" && ev.content) {
        collectedTrace.push(ev);
        upsertThinkingBubble(ev.content);
        status.textContent = "Thinking…";
        status.className = "status";
        return;
      }
      if (type === "assistant" && ev.content) {
        // Mid-loop assistant text is part of the hidden reasoning process.
        collectedTrace.push(ev);
        status.textContent = "Working…";
        return;
      }
      if (type === "tool") {
        collectedTrace.push(ev);
        maybeRenderGolfTool(ev);
        maybeRenderSportTool(ev);
        status.textContent = "Working…";
        return;
      }
      if (type === "final" && ev.content) {
        clearStatusBubble();
        clearThinkingBubble();
        const split = splitCerebelAnalysis(ev.content);
        const hadAnalysis = !!split.analysis;
        applyModelAnalysis(split.analysis);
        addBubble("Agent", renderMarkdown(split.prose), "agent", { interim: false, markdown: true });
        collectedTrace.push(ev);
        if (explainFollowUp) {
          const body = document.getElementById("explainBody");
          if (body) {
            body.querySelectorAll(".status").forEach((el) => el.remove());
            const block = document.createElement("div");
            block.className = "bubble agent final md";
            block.innerHTML = renderMarkdown(split.prose);
            body.appendChild(block);
          }
          explainFollowUp = false;
        } else if (!isDevUi) {
          // Consumer fix-first UI: stay on the body, don't yank scroll to chat.
        } else {
          revealBodyAfterAnswer({
            updated: hadAnalysis || !!(sportBoard && !sportBoard.hidden),
          });
        }
        return;
      }
      if (type === "error") {
        clearStatusBubble();
        clearThinkingBubble();
        addBubble(
          "Error",
          linkTimestamps(ev.message || "unknown error"),
          "agent",
          { interim: false }
        );
        status.className = "status err";
        status.textContent = ev.message || "Error";
        return;
      }
      if (type === "done") {
        clearStatusBubble();
        clearThinkingBubble();
        let bodyUpdated = false;
        if (Array.isArray(ev.trace) && ev.trace.length) {
          collectedTrace.length = 0;
          collectedTrace.push(...ev.trace);
          ev.trace.forEach((step) => {
            if ((step.type || step.kind) === "tool" || step.kind === "tool") {
              const toolEv = {
                name: step.name,
                result_summary: step.result_summary,
              };
              maybeRenderGolfTool(toolEv);
              maybeRenderSportTool(toolEv);
              if (step.name === "build_sport_analysis") bodyUpdated = true;
            }
            if ((step.type || step.kind) === "final" && step.content) {
              const split = splitCerebelAnalysis(step.content);
              if (split.analysis) bodyUpdated = true;
              applyModelAnalysis(split.analysis);
            }
          });
        }
        // Dev playground still jumps to the body after an answer; consumer stays on the coach.
        if (isDevUi && (bodyUpdated || (sportBoard && !sportBoard.hidden))) {
          revealBodyAfterAnswer({ updated: bodyUpdated });
        }
      }
    }

    async function readSse(response, onEvent) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finished = false;
      while (!finished) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const dataLines = chunk
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart());
          if (!dataLines.length) continue;
          const raw = dataLines.join("\n");
          if (!raw || raw === "[DONE]") continue;
          try {
            const ev = JSON.parse(raw);
            onEvent(ev);
            if ((ev.type || ev.kind) === "done") {
              finished = true;
              try { await reader.cancel(); } catch (_) { /* ignore */ }
              break;
            }
          } catch (e) {
            console.warn("bad SSE frame", e, raw.slice(0, 200));
          }
        }
      }
    }

    async function loadSession({ announce = false, sample = false } = {}) {
      const prevId = session && session.session_id;
      loadNewestBtn.disabled = true;
      if (announce) {
        status.textContent = isDevUi ? "Loading newest processed video…" : "Loading your session…";
        status.className = "status";
      }
      try {
        const sessionQuery = sample
          ? "&sample=1"
          : authToken && activeSessionId
            ? "&session_id=" + encodeURIComponent(activeSessionId)
            : "";
        const r = await fetch("/api/session?t=" + Date.now() + sessionQuery, { headers: authHeaders() });
        if (r.status === 410 && activeSessionId) {
          // An old email link or bookmark to a swing that was deleted: open the newest one instead.
          forgetSession(activeSessionId);
          await loadSession({ announce: false });
          status.textContent = "That swing was deleted, so we opened your latest one.";
          status.className = "status";
          return;
        }
        session = await r.json();
        if (window.cerebelCompare) window.cerebelCompare.onSession(session);
        if (window.cerebelSkeleton) window.cerebelSkeleton.onSession(session);
        const meta = document.getElementById("sessionMeta");
        const devMeta = document.getElementById("sessionDevMeta");
        paintCaptureChrome(session);
        if (session.empty || session.processing) {
          meta.textContent = session.message || (session.processing ? "Working…" : "Upload a swing");
          status.textContent = "";
          status.className = "status";
          const title = document.getElementById("productTitle");
          if (title) title.textContent = session.product_title || "Your swing";
          const sub = document.getElementById("productSubtitle");
          if (sub) sub.textContent = session.product_subtitle || "";
          const hero = document.getElementById("fixHero");
          if (hero) hero.hidden = true;
          const fab = document.getElementById("coachFab");
          if (fab) fab.hidden = true;
          const pill = document.getElementById("sportPill");
          if (pill) pill.textContent = session.processing ? stageLabel(session.status || "processing") : "No swing yet";
          renderSportsCatalog();
          syncTocHome();
          if (session.processing) {
            if (libraryPoll) window.clearInterval(libraryPoll);
            libraryPoll = window.setInterval(() => {
              refreshLibrary().then(() => loadSession()).catch(() => {});
            }, 4000);
          }
          return;
        }
        if (session.error) {
          meta.textContent = session.message || "No session";
          status.textContent = session.message || "No preprocessed session found";
          status.className = "status err";
          return;
        }
        const productTitle = session.product_title || "Your session";
        const dur = (session.duration_s || 0).toFixed(1);
        if (session.no_swing || session.sport === "general") {
          meta.innerHTML = `<strong>${escapeHtml(productTitle)}</strong> · ${dur}s · no clear swing`;
          const hero = document.getElementById("fixHero");
          if (hero) hero.hidden = true;
          const fab = document.getElementById("coachFab");
          if (fab) fab.hidden = true;
          updateDetectBanner();
          syncTocHome();
        } else {
          meta.innerHTML = `<strong>${escapeHtml(productTitle)}</strong> · ${dur}s · ready to coach`;
        }
        coachBriefs = Array.isArray(session.coach_briefs) ? session.coach_briefs.slice() : [];
        renderInstantChips();
        if (devMeta) {
          devMeta.hidden = !isDevUi;
          devMeta.innerHTML = `id <strong>${escapeHtml(String(session.session_id || ""))}</strong> ·
            fps <strong>${escapeHtml(String(session.fps || ""))}</strong> ·
            dynamics <strong>${session.has_dynamics ? "yes" : "no"}</strong> ·
            backend <strong>${escapeHtml(String(session.backend || ""))}</strong>`;
        }
        if (session.prompt_presets && session.prompt_presets.length) {
          const metaOnly = {};
          session.prompt_presets.forEach((p) => {
            metaOnly[p.id] = {
              id: p.id,
              label: p.label,
              description: p.description,
            };
          });
          // Keep unlocked prompt bodies if already unlocked; else metadata only.
          if (!promptUnlocked) {
            promptPresets = metaOnly;
          } else {
            Object.keys(metaOnly).forEach((id) => {
              if (!promptPresets[id] || !promptPresets[id].prompt) {
                promptPresets[id] = { ...(promptPresets[id] || {}), ...metaOnly[id] };
              } else {
                promptPresets[id] = {
                  ...promptPresets[id],
                  label: metaOnly[id].label,
                  description: metaOnly[id].description,
                };
              }
            });
          }
          let mode = resolveSessionMode();
          if (!promptPresets[mode] && !metaOnly[mode]) {
            mode = session.default_prompt_mode || fallbackMode();
          }
          applyMode(mode, { skipPhases: true });
        }
        applyFakeEgocentricUi(session);
        if (fakeEgocentric && session.soma_mesh_url_path) {
          playEgoSomaMesh(session.soma_mesh_url_path).catch((err) => {
            console.error(err);
            status.textContent = "The egocentric view didn't load. Reload the page to try again.";
            status.className = "status err";
          });
        } else if (session.video_url_path) {
          // Cache-bust so a replacement session.mp4 for the same id reloads.
          golfPhasesVideoWaitBound = false;
          const fvWrap = document.getElementById("fixVideoWrap");
          if (fvWrap) fvWrap.style.display = "";
          // The path carries a signed ?mt= token (video tags cannot send the auth header).
          const videoUrl = new URL(session.video_url_path, window.location.origin);
          videoUrl.searchParams.set("t", String(Date.now()));
          player.src = videoUrl.pathname + videoUrl.search;
          player.muted = true;
          player.playsInline = true;
          player.load();
          syncFixVideoSrc();
          syncVideoLinks();
          const tryAutoplay = () => {
            player.play().catch(() => {});
            const fv = document.getElementById("fixVideo");
            if (fv) fv.play().catch(() => {});
          };
          player.addEventListener("loadeddata", tryAutoplay, { once: true });
          tryAutoplay();
        } else {
          golfPhasesVideoWaitBound = false;
          player.removeAttribute("src");
          player.load();
          // Source video deleted on the retention schedule: keep the 3D analysis, explain the gap.
          const fvWrap = document.getElementById("fixVideoWrap");
          if (fvWrap) fvWrap.style.display = session.source_video_removed ? "none" : "";
          if (session.source_video_removed) {
            status.textContent = "Your original video was deleted on our retention schedule. The 3D body model and measurements are kept.";
            status.className = "status";
          }
          syncVideoLinks();
        }
        const ex = document.getElementById("examples");
        ex.innerHTML = "";
        renderExampleQuestions();
        if (promptMode === "golf") {
          golfPhasesSessionId = null;
          lastGolfPhasePayload = null;
          scheduleGolfPhasesLoad({ reason: "session" });
          scheduleSportAnalysis("golf");
        } else if (promptMode === "baseball") {
          clearGolfPhaseBoard();
          scheduleSportAnalysis("baseball");
        } else {
          clearGolfPhaseBoard();
          clearSportBoard();
        }
        if (announce && !isDevUi) {
          status.textContent = "Showing your newest swing.";
          status.className = "status";
        } else if (announce) {
          const changed = prevId && prevId !== session.session_id;
          status.textContent = changed
            ? `Loaded newest video: ${session.session_id} (was ${prevId})`
            : `Loaded newest video: ${session.session_id}`;
          status.className = "status";
          addBubble(
            "System",
            changed
              ? `Switched to newest session <strong>${session.session_id}</strong>.`
              : `Refreshed newest session <strong>${session.session_id}</strong>.`,
            "agent"
          );
        }
        seedCoachWelcome();
      } catch (e) {
        status.textContent = String(e);
        status.className = "status err";
      } finally {
        loadNewestBtn.disabled = false;
      }
    }

    function stageLabel(stage) {
      const labels = {
        uploading: "Receiving",
        extracting: "Preparing",
        queued: "Waiting",
        gemx: "Rebuilding body",
        ik: "Measuring",
        ready: "Ready",
        no_swing: "No clear swing",
        error: "Needs another clip",
        processing: "Working",
      };
      return labels[stage] || stage || "Working";
    }

    // Waiting screen: which step the analysis is on. "Waiting in line" only appears when there is a line.
    const PROCESSING_STEPS = [
      ["received", "Upload received", ["uploading"]],
      ["preparing", "Preparing frames", ["extracting", "processing"]],
      ["queued", "Waiting in line", ["queued"]],
      ["body", "Rebuilding your body", ["gemx"]],
      ["measure", "Measuring angles", ["ik"]],
    ];

    function paintProcessingSteps(data) {
      const list = document.getElementById("processingSteps");
      const bar = document.getElementById("processingBar");
      const stage = String(data.stage || data.status || "processing");
      const queued = stage === "queued" || Number(data.jobs_ahead) > 0;
      const steps = PROCESSING_STEPS.filter(([id]) => id !== "queued" || queued);
      let current = steps.findIndex(([, , stages]) => stages.includes(stage));
      if (current < 0) current = 1;
      if (list) {
        list.innerHTML = "";
        steps.forEach(([, label], i) => {
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
      const pct = Number(data.percent);
      if (bar) bar.style.width = (Number.isFinite(pct) ? Math.max(4, Math.min(100, pct)) : 8) + "%";
    }

    function paintCaptureChrome(data) {
      const demo = document.getElementById("demoNote");
      const empty = document.getElementById("emptyState");
      const processing = document.getElementById("processingState");
      const signed = !!(authToken && data && (data.signed_in || data.empty || data.processing));
      if (demo) demo.hidden = signed || !(data && data.demo);
      if (empty) empty.hidden = !(data && data.empty);
      if (processing) {
        processing.hidden = !(data && data.processing);
        const msg = document.getElementById("processingCopy");
        if (msg) msg.textContent = (data && (data.queue_message || data.message)) || "Working…";
        const note = document.getElementById("processingNote");
        if (note) note.textContent = (data && data.product_subtitle) || "";
        if (data && data.processing) paintProcessingSteps(data);
      }
      const title = document.getElementById("productTitle");
      const sub = document.getElementById("productSubtitle");
      const hideHeroChrome = !!(data && (data.empty || data.processing));
      if (title) title.hidden = hideHeroChrome;
      if (sub) sub.hidden = hideHeroChrome;
      const copy = document.getElementById("emptyCopy");
      if (copy && data && data.product_subtitle && data.empty) copy.textContent = data.product_subtitle;
    }

    async function ask({ skipBrief = false } = {}) {
      const question = qInput.value.trim();
      if (!question) return;
      if (session && (session.empty || session.processing || session.no_swing)) {
        status.textContent = session.no_swing
          ? "Upload a clearer swing before asking the coach."
          : "Upload a swing before asking the coach.";
        status.className = "status";
        const target = document.getElementById(session.processing ? "processingState" : "emptyState")
          || document.getElementById("detectBanner");
        if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      // Chip taps / typed near-matches can use instant briefs. Voice transcripts always go to the coach.
      if (!isDevUi && !skipBrief) {
        const brief = matchCoachBrief(question);
        if (brief) {
          qInput.value = "";
          applyInstantBrief(brief);
          sendBtn.disabled = false;
          return;
        }
      }
      sendBtn.disabled = true;
      status.textContent = "Working…";
      status.className = "status";
      addBubble("User", linkTimestamps(question), "user");
      qInput.value = "";
      const collectedTrace = [];
      let sawFinal = false;
      let donePayload = null;
      try {
        const payload = {
          question,
          video_id: session && session.session_id,
          attach_video: true,
          prompt_mode: activePromptMode(),
        };
        const customPrompt = activeSystemPrompt();
        const headers = authHeaders({
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        });
        if (isDevUi) headers["X-Motion-Dev"] = "1";
        if (customPrompt) {
          payload.system_prompt = customPrompt;
          const pw = activePromptPassword();
          if (pw) {
            payload.prompt_password = pw;
            headers["X-Prompt-Password"] = pw;
          }
        }
        const r = await fetch("/api/chat/stream", {
          method: "POST",
          headers,
          body: JSON.stringify(payload),
        });
        if (!r.ok) {
          const errText = await r.text();
          let friendly = "";
          try { friendly = (JSON.parse(errText) || {}).message || ""; } catch (_) {}
          if (r.status === 429 && friendly) throw new Error(friendly);
          throw new Error(friendly || `HTTP ${r.status}: ${errText.slice(0, 300)}`);
        }
        await readSse(r, (ev) => {
          if ((ev.type || ev.kind) === "final") sawFinal = true;
          if ((ev.type || ev.kind) === "done") {
            donePayload = ev;
            // Avoid duplicating the answer if we already rendered `final`.
            if (!sawFinal && ev.answer) {
              addBubble("Agent", renderMarkdown(ev.answer), "agent", { interim: false, markdown: true });
              sawFinal = true;
            }
          }
          renderStreamEvent(ev, collectedTrace);
        });
        status.textContent = donePayload && donePayload.error
          ? `Done with warning: ${donePayload.error}`
          : "Done";
        if (donePayload && donePayload.error && String(donePayload.answer || "").startsWith("Model unavailable")) {
          status.className = "status err";
        }
      } catch (e) {
        clearStatusBubble();
        status.textContent = String(e);
        status.className = "status err";
      } finally {
        sendBtn.disabled = false;
      }
    }

    sendBtn.addEventListener("click", ask);
    loadNewestBtn.addEventListener("click", () => loadSession({ announce: true }));
    const sportPlay = document.getElementById("sportPlay");
    const sportScrub = document.getElementById("sportScrub");
    if (sportPlay) {
      sportPlay.addEventListener("click", () => {
        if (!player) return;
        if (player.paused) {
          // User explicitly continues — allow auto-resume for later holds.
          fixTour.resumeAfterHold = true;
          player.play().catch(() => {});
          const fixVideo = document.getElementById("fixVideo");
          if (fixVideo && fixVideo.src) {
            try { fixVideo.play().catch(() => {}); } catch (_) { /* ignore */ }
          }
        } else {
          // User stop during a tour hold: do not auto-resume when the timer ends.
          if (fixTour.holdingId) fixTour.resumeAfterHold = false;
          player.pause();
          const fixVideo = document.getElementById("fixVideo");
          if (fixVideo) {
            try { fixVideo.pause(); } catch (_) { /* ignore */ }
          }
        }
        sportPlay.textContent = player.paused ? "▶" : "❚❚";
      });
    }
    if (sportScrub) {
      sportScrub.addEventListener("input", () => seekPlayer(sportScrub.value));
    }
    if (player) {
      player.addEventListener("timeupdate", syncSportPlayhead);
      player.addEventListener("play", () => {
        if (sportPlay) sportPlay.textContent = "❚❚";
        // Restarting from the top of the swing restarts the halt-at-fix tour.
        // Skip when we just resumed after an automatic HOLD (peak can sit near start).
        if (fixTour.resumingFromHold) {
          fixTour.resumingFromHold = false;
          return;
        }
        if (lastSportPayload && player.currentTime <= Number(lastSportPayload.start_time) + 0.2) {
          resetFixTour();
        }
      });
      player.addEventListener("pause", () => {
        // During an automatic HOLD the button stays as pause (tour will resume).
        if (fixTour.holdingId || fixTour.pausingForHold || fixTour.resumingFromHold) {
          if (sportPlay) sportPlay.textContent = "❚❚";
          return;
        }
        if (sportPlay) sportPlay.textContent = "▶";
      });
    }
    document.getElementById("savePrompt").addEventListener("click", savePrompt);
    document.getElementById("restorePrompt").addEventListener("click", restoreOriginalPrompt);
    document.getElementById("unlockPrompt").addEventListener("click", () => unlockPrompt());
    document.getElementById("lockPrompt").addEventListener("click", lockPrompt);
    promptPasswordInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        unlockPrompt();
      }
    });
    document.querySelectorAll(".mode-switch [data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.getAttribute("data-mode");
        if (mode) applyMode(mode, { announce: true });
      });
    });
    if (phasePreviewClose) {
      phasePreviewClose.addEventListener("click", closePhasePreview);
    }
    if (phasePreview) {
      phasePreview.addEventListener("click", (e) => {
        if (e.target && e.target.getAttribute("data-close-preview") === "1") {
          closePhasePreview();
        }
      });
    }
    if (phasePreviewPlayer) {
      phasePreviewPlayer.addEventListener("timeupdate", onPhasePreviewTimeUpdate);
    }
    if (phaseViewToggle) {
      phaseViewToggle.addEventListener("click", (e) => {
        const btn = e.target && e.target.closest("button[data-view]");
        if (!btn) return;
        setPhaseViewMode(btn.dataset.view);
      });
    }
    setPhaseViewMode(loadPhaseViewMode(), { persist: false });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && phasePreview && !phasePreview.hidden) {
        closePhasePreview();
      }
    });
    promptArea.addEventListener("input", updatePromptBadge);
    qInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") ask();
    });
    bindFixHeroControls();
    const accountBtn = document.getElementById("accountBtn");
    const accountPanel = document.getElementById("accountPanel");
    const accountForm = document.getElementById("accountForm");
    const accountModeBtn = document.getElementById("accountMode");
    const accountOut = document.getElementById("accountOut");
    const accountStatus = document.getElementById("accountStatus");
    const libraryBox = document.getElementById("libraryBox");
    const libraryList = document.getElementById("libraryList");
    const navCapture = document.getElementById("navCapture");
    if (navCapture) {
      navCapture.addEventListener("click", (e) => {
        const hero = document.getElementById("fixHero");
        const target = hero && !hero.hidden ? hero : document.getElementById("fixVideo");
        if (!target) return;
        e.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    }
    // Signed-out visitors see the landing; signing in brings back the app. Operators (?dev=1) never see it.
    function syncLanding() {
      const landing = document.getElementById("lpLanding");
      if (!landing) return;
      const show = !authToken && !isDevUi;
      landing.hidden = !show;
      document.body.classList.toggle("lp-mode", show);
      if (show) window.dispatchEvent(new Event("cerebel:landing"));
    }
    async function paintLandingPlans() {
      const box = document.getElementById("lpPlans");
      if (!box) return;
      let billing = null;
      try {
        const r = await fetch("/api/billing");
        billing = (await r.json()).billing;
      } catch (_) {}
      if (!billing) return;
      const free = Number(billing.free_monthly) || 3;
      const freeText = free + (free === 1 ? " swing a month" : " swings a month");
      const freeLine = document.getElementById("lpFreeLine");
      if (freeLine) freeLine.textContent = "Free for " + freeText + ". No credit card.";
      const freeDetail = document.getElementById("lpFreeDetail");
      if (freeDetail) freeDetail.textContent = freeText;
      (billing.plans || []).forEach((plan) => {
        const row = document.createElement("div");
        row.className = "lp-plan lp-plan--paid";
        const name = document.createElement("b");
        name.textContent = plan.label;
        const price = document.createElement("span");
        price.className = "lp-plan__price";
        price.textContent = plan.price;
        const detail = document.createElement("small");
        detail.textContent = [plan.detail, billing.pro_monthly ? billing.pro_monthly + " swings a month" : ""].filter(Boolean).join(". ");
        row.append(name, price, detail);
        box.appendChild(row);
      });
    }
    function paintAccount(user) {
      syncLanding();
      const signed = !!(user && user.email);
      if (accountBtn) accountBtn.textContent = signed ? user.email : "Sign in";
      if (accountOut) accountOut.hidden = !signed;
      if (libraryBox) libraryBox.hidden = !signed;
      if (accountForm) accountForm.hidden = signed;
      const panelTitle = document.getElementById("accountPanelTitle");
      if (panelTitle) panelTitle.textContent = signed ? "Your swings" : "Sign in";
    }
    // Uploads from some phones arrive with a random hex name; show when it was filmed instead.
    function captureName(row) {
      const name = String(row.filename || "");
      if (name && !/^[0-9a-f]{16,}(\.[a-z0-9]+)?$/i.test(name)) return name;
      const when = row.created_at ? new Date(row.created_at) : null;
      if (!when || Number.isNaN(when.getTime())) return "Swing";
      return "Swing · " + when.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    }

    async function refreshLibrary() {
      if (!authToken || !libraryList) return;
      const r = await fetch("/api/library", { headers: authHeaders() });
      const data = await r.json();
      if (!r.ok) return;
      paintAccount(data.user);
      const quota = document.getElementById("uploadQuota");
      if (quota) {
        quota.textContent = "Failed or unreadable uploads don't count toward your monthly analyses.";
      }
      if (window.cerebelBilling) window.cerebelBilling.paint(data);
      if (window.cerebelCompare) window.cerebelCompare.onLibrary(data);
      libraryCaptures = data.captures || [];
      const openDrill = document.getElementById("fixDrill");
      const openNote = openDrill && !openDrill.hidden ? currentFixNote() : null;
      if (openNote) paintDrill(openNote);
      libraryList.innerHTML = "";
      (data.captures || []).forEach((row) => {
        const li = document.createElement("li");
        const stage = (row.progress && row.progress.stage) || row.status || "processing";
        const label = document.createElement("button");
        label.type = "button";
        label.textContent = captureName(row) + " · " + stageLabel(stage);
        label.title = label.textContent;
        const queueMsg = row.progress && row.progress.queue_message;
        if (queueMsg && stage !== "ready") label.textContent += " · " + queueMsg;
        label.addEventListener("click", () => {
          activeSessionId = row.session_id;
          if (stage === "ready") loadSession();
          else if (accountStatus) {
            accountStatus.textContent = (row.progress && row.progress.message) || "Still processing.";
          }
        });
        const del = document.createElement("button");
        del.type = "button";
        del.textContent = "Delete";
        del.addEventListener("click", async () => {
          if (!window.confirm("Delete this swing from the server? The video and analysis will be removed.")) return;
          await fetch("/api/library/delete", {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ session_id: row.session_id }),
          });
          if (activeSessionId === row.session_id) {
            forgetSession(row.session_id);
            loadSession({ announce: false }).catch(() => {});
          }
          refreshLibrary();
        });
        li.appendChild(label);
        li.appendChild(del);
        libraryList.appendChild(li);
      });
      const ready = (data.captures || []).find((row) => (row.progress && row.progress.stage) === "ready" || row.status === "ready");
      if (ready && !activeSessionId) activeSessionId = ready.session_id;
      const pending = (data.captures || []).some((row) => {
        const stage = (row.progress && row.progress.stage) || row.status;
        return stage && stage !== "ready" && stage !== "error" && stage !== "deleted";
      });
      if (libraryPoll) window.clearInterval(libraryPoll);
      if (pending) libraryPoll = window.setInterval(() => refreshLibrary().catch(() => {}), 4000);
    }
    if (accountBtn && accountPanel) {
      accountBtn.addEventListener("click", () => {
        accountPanel.hidden = !accountPanel.hidden;
      });
    }
    if (accountPanel) {
      const isDrawer = () => !isDevUi && !document.body.classList.contains("lp-mode");
      const closeDrawer = () => { if (isDrawer()) accountPanel.hidden = true; };
      const closeBtn = document.getElementById("accountPanelClose");
      if (closeBtn) closeBtn.addEventListener("click", () => { accountPanel.hidden = true; });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !accountPanel.hidden && !document.querySelector(".cb-modal")) closeDrawer();
      });
      document.addEventListener("pointerdown", (e) => {
        if (accountPanel.hidden || accountPanel.contains(e.target)) return;
        // Buttons that open the drawer, and dialogs it opens (consent, upgrade), keep it open.
        if (e.target.closest("#accountBtn, #navLibrary, a[href='#library'], .cb-modal, #tocStartUpload, #emptyUpload")) return;
        closeDrawer();
      });
      // The coach button hides while the drawer covers its corner.
      const syncDrawerClass = () => document.body.classList.toggle("is-drawer-open", isDrawer() && !accountPanel.hidden);
      new MutationObserver(syncDrawerClass).observe(accountPanel, { attributes: true, attributeFilter: ["hidden"] });
      syncDrawerClass();
    }
    (function wireTocProductChrome() {
      const navLibrary = document.getElementById("navLibrary");
      if (navLibrary) {
        navLibrary.addEventListener("click", function (e) {
          e.preventDefault();
          openAccountPanel(false);
        });
      }
      const tocStart = document.getElementById("tocStartUpload");
      if (tocStart) {
        tocStart.addEventListener("click", function () {
          openAccountPanel(true);
        });
      }
      const v3Upload = document.getElementById("v3Upload");
      if (v3Upload) {
        v3Upload.addEventListener("click", () => {
          // First-time visitors came to upload, so open on "Create account", not "Sign in".
          const modeBtn = document.getElementById("accountMode");
          if (!authToken && accountMode === "login" && modeBtn) modeBtn.click();
          openAccountPanel(true);
        });
      }
      const v3Sample = document.getElementById("v3Sample");
      if (v3Sample) {
        v3Sample.addEventListener("click", () => {
          loadSession({ announce: true, sample: true }).then(() => {
            const target = document.getElementById("fixHero");
            if (target && !target.hidden) target.scrollIntoView({ behavior: "smooth", block: "start" });
          });
        });
      }
      const tocSample = document.getElementById("tocTrySample");
      if (tocSample) {
        tocSample.addEventListener("click", function () {
          loadSession({ announce: true, sample: true });
        });
      }
      const lpUpload = document.getElementById("lpUpload");
      if (lpUpload) {
        lpUpload.addEventListener("click", () => {
          const modeBtn = document.getElementById("accountMode");
          if (accountMode === "login" && modeBtn) modeBtn.click();
          openAccountPanel(true);
        });
      }
      const lpUpload2 = document.getElementById("lpUpload2");
      if (lpUpload2 && lpUpload) lpUpload2.addEventListener("click", () => lpUpload.click());
      const lpSignin = document.getElementById("lpSignin");
      if (lpSignin) {
        lpSignin.addEventListener("click", () => {
          const modeBtn = document.getElementById("accountMode");
          if (accountMode !== "login" && modeBtn) modeBtn.click();
          openAccountPanel(true);
        });
      }
      // Over the landing the account panel is a dialog: Escape or a click outside closes it.
      const closeLandingPanel = () => {
        if (!document.body.classList.contains("lp-mode") || !accountPanel || accountPanel.hidden) return;
        accountPanel.hidden = true;
        if (lpUpload) lpUpload.focus();
      };
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeLandingPanel();
      });
      document.addEventListener("pointerdown", (e) => {
        if (accountPanel && !accountPanel.contains(e.target) && !e.target.closest("#lpUpload, #lpUpload2, #lpSignin")) closeLandingPanel();
      });
      syncLanding();
      if (!isDevUi) paintLandingPlans();
      syncTocHome();
    })();
    // "login" | "register" | "forgot" (email a reset link) | "reset" (choose a new password from the link).
    let accountHintDefault = null;
    function setAccountMode(mode) {
      accountMode = mode;
      const submit = document.getElementById("accountSubmit");
      if (submit) {
        submit.textContent = { login: "Sign in", register: "Create account", forgot: "Email me a reset link", reset: "Set new password" }[mode];
      }
      if (accountModeBtn) accountModeBtn.textContent = mode === "login" ? "Create account" : mode === "register" ? "Have an account? Sign in" : "Back to sign in";
      const show = (id, on) => { const el = document.getElementById(id); if (el) el.hidden = !on; };
      show("accountCoachRow", mode === "register");
      show("accountConsentRow", mode === "register");
      show("accountEmailRow", mode !== "reset");
      show("accountPasswordRow", mode !== "forgot");
      show("accountForgotRow", mode === "login");
      const email = document.getElementById("accountEmail");
      if (email) email.required = mode !== "reset";
      const pw = document.getElementById("accountPassword");
      if (pw) {
        pw.required = mode !== "forgot";
        pw.autocomplete = mode === "register" || mode === "reset" ? "new-password" : "current-password";
      }
      const pwLabel = document.getElementById("accountPasswordLabel");
      if (pwLabel) pwLabel.textContent = mode === "reset" ? "New password" : "Password";
      const hint = document.getElementById("accountHint");
      if (hint) {
        if (!accountHintDefault) accountHintDefault = Array.from(hint.childNodes, (n) => n.cloneNode(true));
        const text = {
          forgot: "Enter the email you signed up with. We'll send a link to choose a new password.",
          reset: "Choose a new password. You'll be signed out on your other devices.",
        }[mode];
        if (text) hint.textContent = text;
        else hint.replaceChildren(...accountHintDefault.map((n) => n.cloneNode(true)));
      }
      if (accountStatus) accountStatus.textContent = "";
      if (mode === "register" && window.cerebelBilling) window.cerebelBilling.track("signup_view");
    }
    if (accountModeBtn) {
      accountModeBtn.addEventListener("click", () => {
        setAccountMode(accountMode === "login" ? "register" : "login");
      });
    }
    const accountForgot = document.getElementById("accountForgot");
    if (accountForgot) {
      accountForgot.addEventListener("click", () => {
        setAccountMode("forgot");
        const email = document.getElementById("accountEmail");
        if (email) email.focus();
      });
    }
    if (accountForm) {
      accountForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const email = document.getElementById("accountEmail").value.trim();
        const password = document.getElementById("accountPassword").value;
        const consent = document.getElementById("accountConsent");
        if (accountMode === "register" && !(consent && consent.checked)) {
          if (accountStatus) accountStatus.textContent = "Please tick the box to accept the Terms and Privacy Policy.";
          return;
        }
        if (accountStatus) accountStatus.textContent = "Working…";
        let url = "/api/auth/login";
        let payload = { email, password };
        if (accountMode === "register") {
          url = "/api/auth/register";
          payload.accept_terms = true;
          const coach = document.getElementById("accountCoach");
          if (coach && coach.value.trim()) payload.coach_code = coach.value.trim();
        } else if (accountMode === "forgot") {
          url = "/api/auth/forgot";
          payload = { email };
        } else if (accountMode === "reset") {
          url = "/api/auth/reset";
          payload = { token: resetToken, password };
        }
        let r;
        let data = {};
        try {
          r = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          data = await r.json();
        } catch (_) {
          if (accountStatus) accountStatus.textContent = "Network problem. Check your connection and try again.";
          return;
        }
        if (!r.ok) {
          if (accountStatus) accountStatus.textContent = data.message || "Could not sign in";
          return;
        }
        if (accountMode === "forgot") {
          if (accountStatus) accountStatus.textContent = data.message || "Check your inbox.";
          return;
        }
        if (accountMode === "reset") {
          resetToken = "";
          setAccountMode("login");
          showToast("Password changed. You're signed in.");
        }
        authToken = data.token || "";
        try { localStorage.setItem(AUTH_KEY, authToken); } catch (_) {}
        activeSessionId = null;
        paintAccount(data.user);
        if (accountStatus) accountStatus.textContent = "";
        await refreshLibrary();
        loadSession();
      });
    }
    if (accountOut) {
      accountOut.addEventListener("click", async () => {
        await fetch("/api/auth/logout", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: "{}" });
        authToken = "";
        libraryCaptures = [];
        activeSessionId = null;
        try { localStorage.removeItem(AUTH_KEY); } catch (_) {}
        paintAccount(null);
        if (libraryList) libraryList.innerHTML = "";
        loadSession();
      });
    }
    const uploadFile = document.getElementById("uploadFile");
    if (uploadFile) {
      uploadFile.addEventListener("change", async () => {
        const file = uploadFile.files && uploadFile.files[0];
        if (!file || !authToken) return;
        // Upload progress is written in the drawer, so show it even if the upload started elsewhere.
        if (accountPanel) accountPanel.hidden = false;
        const uploadStatus = document.getElementById("uploadStatus");
        if (window.cerebelConsent && !(await window.cerebelConsent.ensure())) {
          if (uploadStatus) uploadStatus.textContent = "Upload cancelled. We need your consent to process the video.";
          uploadFile.value = "";
          return;
        }
        if (uploadStatus) uploadStatus.textContent = "Uploading…";
        if (window.cerebelBilling) window.cerebelBilling.track("upload_click", { mb: Math.round(file.size / 1e6) });
        let prep = null;
        if (window.cerebelSwingTrim) {
          prep = await window.cerebelSwingTrim.analyze(file, (msg) => { if (uploadStatus) uploadStatus.textContent = msg; });
          if (prep && prep.block) {
            if (uploadStatus) uploadStatus.textContent = prep.warning + " This didn't use one of your analyses.";
            uploadFile.value = "";
            return;
          }
          if (prep && prep.warning && !window.confirm(prep.warning + "\n\nUpload anyway?")) {
            if (uploadStatus) uploadStatus.textContent = "";
            uploadFile.value = "";
            return;
          }
          if (uploadStatus) uploadStatus.textContent = "Uploading…";
        }
        const body = new FormData();
        body.append("file", file);
        if (prep && prep.window) {
          body.append("trim_start", String(prep.window[0]));
          body.append("trim_end", String(prep.window[1]));
        }
        const r = await fetch("/api/upload", { method: "POST", headers: authHeaders(), body });
        const data = await r.json();
        if (!r.ok) {
          if (uploadStatus) uploadStatus.textContent = data.message || "Upload failed";
          uploadFile.value = "";
          if (r.status === 402 && window.cerebelBilling) {
            if (data.paywall) window.cerebelBilling.openPaywall(data);
          }
          if (r.status === 428 && window.cerebelConsent) {
            window.cerebelConsent.reset(data.video_consent);
            if (uploadStatus) uploadStatus.textContent = (data.message || "Consent needed.") + " Choose the clip again after agreeing.";
            window.cerebelConsent.ensure();
          }
          return;
        }
        activeSessionId = data.session_id;
        if (uploadStatus) {
          uploadStatus.textContent = "Saved. Rebuilding the body — this can take a few minutes.";
          if (data.warnings && data.warnings.length) uploadStatus.textContent += " " + data.warnings.join(" ");
        }
        uploadFile.value = "";
        refreshLibrary();
        // Show the new swing right away: the waiting screen with its steps, which turns into the
        // report on its own when the analysis is ready. Without this the page stayed where it was.
        if (!isDevUi && accountPanel) accountPanel.hidden = true;
        loadSession();
        if (!isDevUi) window.scrollTo({ top: 0, behavior: "smooth" });
      });
    }
    const deleteAccountBtn = document.getElementById("deleteAccount");
    if (deleteAccountBtn) {
      deleteAccountBtn.addEventListener("click", async () => {
        if (!authToken) return;
        if (!window.confirm("Delete this account and every swing stored on this server?")) return;
        const r = await fetch("/api/account/delete", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: "{}" });
        if (!r.ok) {
          let msg = "Could not delete the account.";
          try { msg = (await r.json()).message || msg; } catch (_) {}
          if (accountStatus) accountStatus.textContent = msg;
          return;
        }
        authToken = "";
        libraryCaptures = [];
        activeSessionId = null;
        try { localStorage.removeItem(AUTH_KEY); } catch (_) {}
        paintAccount(null);
        if (libraryList) libraryList.innerHTML = "";
        loadSession();
      });
    }
    const passwordForm = document.getElementById("passwordForm");
    if (passwordForm) {
      passwordForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const status = document.getElementById("passwordStatus");
        if (status) status.textContent = "Working…";
        const r = await fetch("/api/account/password", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({
            current_password: document.getElementById("passwordCurrent").value,
            new_password: document.getElementById("passwordNew").value,
          }),
        });
        let data = {};
        try { data = await r.json(); } catch (_) {}
        if (!r.ok) {
          if (status) status.textContent = data.message || "Could not change the password.";
          return;
        }
        authToken = data.token || authToken;
        try { localStorage.setItem(AUTH_KEY, authToken); } catch (_) {}
        passwordForm.reset();
        if (status) status.textContent = "Password changed. Your other devices were signed out.";
      });
    }
    const feedbackForm = document.getElementById("feedbackForm");
    if (feedbackForm) {
      feedbackForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const status = document.getElementById("feedbackStatus");
        const text = document.getElementById("feedbackText").value.trim();
        if (!text) return;
        if (status) status.textContent = "Sending…";
        const r = await fetch("/api/feedback", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ text, session_id: activeSessionId || "" }),
        });
        let data = {};
        try { data = await r.json(); } catch (_) {}
        if (!r.ok) {
          if (status) status.textContent = data.message || "Could not send. Try again.";
          return;
        }
        feedbackForm.reset();
        if (status) status.textContent = "Thanks! We read every message.";
      });
    }
    // While the page is open and visible, tell the server once a minute so time spent reading a
    // report counts as use (the admin dashboard shows visits and their length).
    function pingActivity() {
      if (!authToken || document.visibilityState !== "visible") return;
      fetch("/api/activity/ping", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: "{}",
        keepalive: true,
      }).catch(() => {});
    }
    window.setInterval(pingActivity, 60000);
    document.addEventListener("visibilitychange", pingActivity);
    if (resetToken) {
      setAccountMode("reset");
      openAccountPanel(false);
      const pw = document.getElementById("accountPassword");
      if (pw) pw.focus();
    }
    // Drop a clip anywhere on the page to upload it.
    if (!isDevUi && uploadFile) {
      window.addEventListener("dragover", (e) => {
        if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) e.preventDefault();
      });
      window.addEventListener("drop", (e) => {
        const files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        e.preventDefault();
        if (!authToken) {
          openAccountPanel(false);
          showToast("Sign in before uploading a swing.");
          return;
        }
        uploadFile.files = files;
        uploadFile.dispatchEvent(new Event("change"));
        if (window.cxFluid) window.cxFluid.pulse(1);
      });
    }
    const emptyUpload = document.getElementById("emptyUpload");
    if (emptyUpload && uploadFile) {
      emptyUpload.addEventListener("click", () => {
        if (!authToken) {
          if (accountPanel) accountPanel.hidden = false;
          showToast("Sign in before uploading a swing.");
          return;
        }
        uploadFile.click();
      });
    }
    const coachLink = document.querySelector('a[href="#coach"]');
    if (coachLink) {
      coachLink.addEventListener("click", (e) => {
        if (isDevUi) return;
        e.preventDefault();
        if (session && session.empty) {
          showToast("Upload a swing before asking the coach.");
          const empty = document.getElementById("emptyState");
          if (empty) empty.scrollIntoView({ behavior: "smooth", block: "center" });
          return;
        }
        showConsumerChat();
      });
    }
    if (authToken) {
      // Operators always get the library open. Players see it only until they have a swing;
      // after that it stays behind the account button so the swing is the first thing on the page.
      if (accountPanel) accountPanel.hidden = !isDevUi;
      refreshLibrary()
        .then(() => {
        })
        .catch(() => {});
    }
    // Restore unlock for this browser tab only (not permanent).
    try {
      const savedPw = sessionStorage.getItem(PROMPT_UNLOCK_KEY);
      if (savedPw) {
        promptPasswordInput.value = savedPw;
        unlockPrompt().finally(() => loadSession());
      } else {
        setPromptLockedUI();
        loadSession();
      }
    } catch (_) {
      setPromptLockedUI();
      loadSession();
    }
