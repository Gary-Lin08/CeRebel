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
      bundle: "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs",
      wasm: "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm",
      model:
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
      maxSeconds: 25, // give up and upload untrimmed after this long
      before: 2.2, // seconds kept before the fastest hand movement
      after: 1.3, // seconds kept after it
    },
    window.CEREBEL_MEDIAPIPE || {}
  );

  var landmarkerPromise = null;

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
      v.muted = true;
      v.playsInline = true;
      v.preload = "auto";
      v.src = URL.createObjectURL(file);
      v.addEventListener("loadeddata", function () { resolve(v); }, { once: true });
      v.addEventListener("error", function () { reject(new Error("unreadable video")); }, { once: true });
    });
  }

  var VIS = 0.5;
  var BODY = [0, 15, 16, 27, 28]; // nose, wrists, ankles

  function fullBody(lm) {
    return BODY.every(function (i) {
      var p = lm[i];
      if (!p) return false;
      var inFrame = p.x > -0.02 && p.x < 1.02 && p.y > -0.02 && p.y < 1.02;
      return inFrame && (p.visibility == null || p.visibility > VIS);
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
      var step = Math.max(1 / 8, duration / 160);
      var samples = [];
      var multi = 0;
      var body = 0;
      var seen = 0;
      for (var t = 0; t < duration; t += step) {
        if ((Date.now() - started) / 1000 > CFG.maxSeconds) break;
        await seek(video, t);
        var res = landmarker.detectForVideo(video, Math.round(t * 1000));
        var poses = (res && res.landmarks) || [];
        if (poses.length > 1) multi += 1;
        if (poses.length) {
          seen += 1;
          var lm = poses[0];
          if (fullBody(lm)) body += 1;
          samples.push({ t: t, x: (lm[15].x + lm[16].x) / 2, y: (lm[15].y + lm[16].y) / 2 });
        }
      }
      var total = Math.max(1, Math.round(duration / step));
      var stats = {
        duration: duration,
        detected: seen / total,
        fullBody: seen ? body / seen : 0,
        multiple: multi / total,
      };
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
      if (best && duration > CFG.before + CFG.after + 1.5) {
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

  window.cerebelSwingTrim = { analyze: analyze, preload: loadLandmarker, config: CFG };
})();
