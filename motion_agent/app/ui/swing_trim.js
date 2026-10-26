/* Find the swing in the browser before uploading.
   Runs MediaPipe Pose Landmarker on the user's device (no server or GPU cost), then:
   - proposes a 3–4 s window around the fastest hand movement (the strike), which the
     server cuts before sending anything to the GPU;
   - checks that one whole person is visible, so bad clips are caught before upload.
   Everything here is best-effort: if the model can't load or the clip can't be read,
   the upload simply goes ahead untrimmed. */
(function () {
  "use strict";

  var CFG = Object.assign(
    {
      // Served from our own origin (see ui/vendor/pose/README.md): no third party sees the visit.
      bundle: "/static/vendor/pose/0.10.14/vision_bundle.mjs",
      wasm: "/static/vendor/pose/0.10.14/wasm",
      model: "/static/vendor/pose/pose_landmarker_lite_f16_v1.task",
      maxSeconds: 25, // give up and upload untrimmed after this long
      before: 2.2, // seconds kept before the fastest hand movement
      after: 1.3, // seconds kept after it
    },
    window.CEREBEL_MEDIAPIPE || {}
  );

  var landmarkerPromise = null;
  // MediaPipe VIDEO mode needs timestamps that only ever increase, across all clips.
  var clock = 0;

  function loadLandmarker() {
    if (landmarkerPromise) return landmarkerPromise;
    landmarkerPromise = (async function () {
      var vision = await import(CFG.bundle);
      var fileset = await vision.FilesetResolver.forVisionTasks(CFG.wasm);
      var opts = function (delegate) {
        return {
          baseOptions: { modelAssetPath: CFG.model, delegate: delegate },
          runningMode: "VIDEO",
          numPoses: 2,
        };
      };
      try {
        return await vision.PoseLandmarker.createFromOptions(fileset, opts("GPU"));
      } catch (_) {
        return await vision.PoseLandmarker.createFromOptions(fileset, opts("CPU"));
      }
    })();
    landmarkerPromise.catch(function () { landmarkerPromise = null; });
    return landmarkerPromise;
  }

  function seek(video, t) {
    return new Promise(function (resolve, reject) {
      var done = false;
      var timer = setTimeout(function () { if (!done) { done = true; reject(new Error("seek timeout")); } }, 4000);
      video.addEventListener("seeked", function onSeek() {
        if (done) return;
        done = true;
        clearTimeout(timer);
        video.removeEventListener("seeked", onSeek);
        resolve();
      });
      video.currentTime = t;
    });
  }

  function openVideo(file) {
    return new Promise(function (resolve, reject) {
      var v = document.createElement("video");
      var url = URL.createObjectURL(file);
      var timer = setTimeout(function () {
        URL.revokeObjectURL(url);
        reject(new Error("video load timeout"));
      }, 8000);
      v.muted = true;
      v.playsInline = true;
      v.preload = "auto";
      v.addEventListener("loadeddata", function () { clearTimeout(timer); resolve(v); }, { once: true });
      v.addEventListener("error", function () {
        clearTimeout(timer);
        URL.revokeObjectURL(url);
        reject(new Error("unreadable video"));
      }, { once: true });
      v.src = url;
      // iOS Safari stops at metadata for a video that isn't on the page (no "loadeddata",
      // seeks never finish) unless load() is called.
      v.load();
    });
  }

  var VIS = 0.5;
  var HEAD = [0, 7, 8]; // nose, ears
  var PAIRS = [[15, 16], [27, 28]]; // wrists, ankles

  function inFrame(p) {
    return !!p && p.x > -0.02 && p.x < 1.02 && p.y > -0.02 && p.y < 1.02;
  }
  function seen(p) {
    return !!p && (p.visibility == null || p.visibility > VIS);
  }

  /* Head to feet in the picture. Joints get a position even when hidden, so "in frame" is read
     from the position; visibility only has to hold for one side of each pair, because the grip
     hides one wrist (and the body often one ankle) for most of a swing. */
  function fullBody(lm) {
    if (!HEAD.some(function (i) { return inFrame(lm[i]) && seen(lm[i]); })) return false;
    return PAIRS.every(function (pair) {
      var a = lm[pair[0]];
      var b = lm[pair[1]];
      return inFrame(a) && inFrame(b) && (seen(a) || seen(b));
    });
  }

  /* Returns {window:[s,e]|null, block:bool, warning:string|null, stats} or null on failure. */
  async function analyze(file, onStatus) {
    var started = Date.now();
    var say = typeof onStatus === "function" ? onStatus : function () {};
    var video;
    try {
      say("Checking your clip…");
      var landmarker = await loadLandmarker();
      video = await openVideo(file);
      var duration = video.duration;
      if (!isFinite(duration) || duration <= 0) return null;
      var step = Math.max(1 / 6, duration / 90);
      var samples = [];
      var multi = 0;
      var body = 0;
      var seen = 0;
      var processed = 0;
      for (var t = 0; t < duration; t += step) {
        if ((Date.now() - started) / 1000 > CFG.maxSeconds) break;
        await seek(video, t);
        clock += Math.max(1, Math.round(step * 1000));
        var res = landmarker.detectForVideo(video, clock);
        processed += 1;
        var poses = (res && res.landmarks) || [];
        if (poses.length > 1) multi += 1;
        if (poses.length) {
          seen += 1;
          var lm = poses[0];
          if (fullBody(lm)) body += 1;
          samples.push({ t: t, x: (lm[15].x + lm[16].x) / 2, y: (lm[15].y + lm[16].y) / 2 });
        }
      }
      var total = Math.max(1, processed);
      var stats = {
        duration: duration,
        processed: processed,
        coverage: Math.min(1, (processed * step) / duration),
        detected: seen / total,
        fullBody: seen ? body / seen : 0,
        multiple: multi / total,
      };
      if (processed < 10) return null; // too little evidence either way: upload untrimmed
      if (stats.detected < 0.3) {
        return {
          block: true,
          warning: "We couldn't find a person in this clip. Film your whole body from the side or front, then try again.",
          window: null,
          stats: stats,
        };
      }
      var warning = null;
      if (stats.fullBody < 0.5) warning = "Your whole body (head to feet) isn't in frame for most of the clip, so some angles may be off.";
      else if (stats.multiple > 0.3) warning = "There seems to be more than one person in the clip. We'll analyse the most visible one.";

      // Fastest hand movement ≈ the strike. Smooth over 3 samples to ignore jitter.
      var speeds = [];
      for (var i = 1; i < samples.length; i++) {
        var dt = samples[i].t - samples[i - 1].t || step;
        var dx = samples[i].x - samples[i - 1].x;
        var dy = samples[i].y - samples[i - 1].y;
        speeds.push({ t: samples[i].t, v: Math.sqrt(dx * dx + dy * dy) / dt });
      }
      var best = null;
      for (var j = 1; j + 1 < speeds.length; j++) {
        var v = (speeds[j - 1].v + speeds[j].v + speeds[j + 1].v) / 3;
        if (!best || v > best.v) best = { t: speeds[j].t, v: v };
      }
      var win = null;
      // Only trim when the whole clip was scanned; otherwise the strike may lie in the unscanned part.
      if (best && stats.coverage > 0.95 && duration > CFG.before + CFG.after + 1.5) {
        var s = Math.max(0, best.t - CFG.before);
        var e = Math.min(duration, best.t + CFG.after);
        if (e - s >= 2.5) win = [Number(s.toFixed(2)), Number(e.toFixed(2))];
      }
      return { block: false, warning: warning, window: win, stats: stats };
    } catch (err) {
      return null; // never block an upload because the helper failed
    } finally {
      if (video) {
        try { URL.revokeObjectURL(video.src); } catch (_) {}
      }
    }
  }

  /* The check must never hold up an upload: past this limit the clip goes up as it is. */
  function analyzeWithin(file, onStatus) {
    return Promise.race([
      analyze(file, onStatus),
      new Promise(function (resolve) { setTimeout(function () { resolve(null); }, (CFG.maxSeconds + 20) * 1000); }),
    ]);
  }

  window.cerebelSwingTrim = { analyze: analyzeWithin, preload: loadLandmarker, config: CFG };
})();
