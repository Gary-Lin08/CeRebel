import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Activity,
  BadgeInfo,
  Box,
  ChevronLeft,
  ChevronRight,
  Expand,
  Film,
  Glasses,
  HelpCircle,
  Pause,
  Play,
  RotateCcw,
  Sparkles,
} from "lucide-react";
import { AnalysisChart } from "./components/AnalysisChart";
import { OpenSimViewport } from "./components/OpenSimViewport";
import {
  loadSomaMeshSequence,
  SomaMeshViewport,
  type SomaMeshSequence,
} from "./components/SomaMeshViewport";
import { getMetricValues } from "./data/metrics";
import type {
  EstimatedDynamicsData,
  MetricKey,
  MotionData,
  OpenSimMotion,
} from "./types";

type MobileView = "video" | "model" | "curves";

function formatTime(value: number) {
  const safe = Number.isFinite(value) ? value : 0;
  const minutes = Math.floor(safe / 60);
  const seconds = safe - minutes * 60;
  return `${minutes}:${seconds.toFixed(2).padStart(5, "0")}`;
}

function LoadingScreen({ failed }: { failed: boolean }) {
  return (
    <main className="load-screen">
      <img
        className="brand-logo brand-logo--large"
        src="/assets/cerebel-logo.svg"
        width={595}
        height={194}
        alt="CeRebel"
      />
      <p className="eyebrow">Kinetic Lens</p>
      <h1>{failed ? "The local demo assets could not be loaded." : "Preparing the synchronized lab."}</h1>
      <p>
        {failed
          ? "Rebuild the OpenSim asset bundle and reload this page."
          : "Loading motion data, OpenSim geometry, and frame-level kinematics."}
      </p>
    </main>
  );
}

function App() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const workspaceRef = useRef<HTMLElement>(null);
  const rafRef = useRef<number | null>(null);
  const playOriginRef = useRef<{ wall: number; media: number } | null>(null);
  const [motionData, setMotionData] = useState<MotionData | null>(null);
  const [openSimMotion, setOpenSimMotion] = useState<OpenSimMotion | null>(null);
  const [estimatedDynamics, setEstimatedDynamics] =
    useState<EstimatedDynamicsData | null>(null);
  const [failed, setFailed] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [metric, setMetric] = useState<MetricKey>("knee");
  const [mobileView, setMobileView] = useState<MobileView>("model");
  const [showShortcuts, setShowShortcuts] = useState(false);
  const [fakeEgocentric, setFakeEgocentric] = useState(false);
  const [somaMesh, setSomaMesh] = useState<SomaMeshSequence | null>(null);
  const [somaFailed, setSomaFailed] = useState(false);

  useEffect(() => {
    let active = true;

    Promise.all([
      fetch("/assets/layup-motion-data.json").then((response) => {
        if (!response.ok) throw new Error("Motion data unavailable");
        return response.json() as Promise<MotionData>;
      }),
      fetch("/assets/model/opensim-motion.json").then((response) => {
        if (!response.ok) throw new Error("OpenSim motion unavailable");
        return response.json() as Promise<OpenSimMotion>;
      }),
      fetch("/assets/model/estimated-dynamics.json")
        .then((response) => (response.ok ? response.json() : null))
        .catch(() => null),
      fetch("/api/session")
        .then((response) => (response.ok ? response.json() : null))
        .catch(() => null),
    ])
      .then(([pose, opensim, dynamics, session]) => {
        if (!active) return;
        setMotionData(pose);
        setOpenSimMotion(opensim);
        setEstimatedDynamics(dynamics);
        const ego = Boolean(session?.fake_egocentric);
        setFakeEgocentric(ego);
        if (ego) {
          loadSomaMeshSequence()
            .then((mesh) => {
              if (active) {
                setSomaMesh(mesh);
                setSomaFailed(false);
              }
            })
            .catch((error) => {
              console.error(error);
              if (active) setSomaFailed(true);
            });
        }
      })
      .catch((error) => {
        console.error(error);
        if (active) setFailed(true);
      });

    return () => {
      active = false;
    };
  }, []);

  const fps =
    openSimMotion && openSimMotion.frames.length > 1
      ? 1 / (openSimMotion.frames[1].time - openSimMotion.frames[0].time)
      : motionData?.metadata.nominalFps ?? 30;
  const duration = motionData?.metadata.durationSeconds ?? 0;
  const frameIndex = useMemo(() => {
    if (!motionData) return 0;
    return Math.min(
      motionData.frames.length - 1,
      Math.max(0, Math.round(currentTime * fps)),
    );
  }, [currentTime, fps, motionData]);

  // Continuous frame clock for SOMA mesh interpolation (avoids integer snap jitter).
  const meshFrame = useMemo(() => {
    const meshFps = somaMesh?.fps || fps;
    const maxF = somaMesh ? Math.max(somaMesh.frameCount - 1, 0) : 0;
    return Math.min(maxF, Math.max(0, currentTime * meshFps));
  }, [currentTime, fps, somaMesh]);

  const eventLabel = useMemo(() => {
    if (!motionData) return "";
    const events = motionData.events.filter((event) => event.time <= currentTime);
    return events.at(-1)?.label ?? motionData.events[0]?.label ?? "";
  }, [currentTime, motionData]);

  const updateFromVideo = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    setCurrentTime(video.currentTime);
    if (!video.paused && !video.ended) {
      rafRef.current = requestAnimationFrame(updateFromVideo);
    }
  }, []);

  const updateSyntheticClock = useCallback(() => {
    const origin = playOriginRef.current;
    if (!origin) return;
    const elapsed = ((performance.now() - origin.wall) / 1000) * playbackRate;
    let next = origin.media + elapsed;
    if (next >= duration) {
      next = duration;
      setCurrentTime(next);
      setIsPlaying(false);
      playOriginRef.current = null;
      return;
    }
    setCurrentTime(next);
    rafRef.current = requestAnimationFrame(updateSyntheticClock);
  }, [duration, playbackRate]);

  useEffect(() => {
    if (!isPlaying) return;
    if (fakeEgocentric) {
      rafRef.current = requestAnimationFrame(updateSyntheticClock);
    } else {
      rafRef.current = requestAnimationFrame(updateFromVideo);
    }
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [isPlaying, fakeEgocentric, updateFromVideo, updateSyntheticClock]);

  const seek = useCallback(
    (seconds: number) => {
      const next = Math.min(duration, Math.max(0, seconds));
      if (!fakeEgocentric && videoRef.current) {
        videoRef.current.currentTime = next;
      }
      if (fakeEgocentric && isPlaying) {
        playOriginRef.current = { wall: performance.now(), media: next };
      }
      setCurrentTime(next);
    },
    [duration, fakeEgocentric, isPlaying],
  );

  const togglePlayback = useCallback(async () => {
    if (fakeEgocentric) {
      if (isPlaying) {
        setIsPlaying(false);
        playOriginRef.current = null;
        return;
      }
      const start = currentTime >= duration - 0.02 ? 0 : currentTime;
      if (start === 0 && currentTime !== 0) setCurrentTime(0);
      playOriginRef.current = { wall: performance.now(), media: start };
      setIsPlaying(true);
      return;
    }

    const video = videoRef.current;
    if (!video) return;

    if (video.paused) {
      if (video.currentTime >= duration - 0.02) seek(0);
      await video.play();
    } else {
      video.pause();
    }
  }, [currentTime, duration, fakeEgocentric, isPlaying, seek]);

  const stepFrame = useCallback(
    (direction: -1 | 1) => {
      if (!fakeEgocentric) videoRef.current?.pause();
      else {
        setIsPlaying(false);
        playOriginRef.current = null;
      }
      seek(currentTime + direction / fps);
    },
    [currentTime, fakeEgocentric, fps, seek],
  );

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (
        event.target instanceof HTMLInputElement ||
        event.target instanceof HTMLSelectElement ||
        event.target instanceof HTMLButtonElement
      ) {
        return;
      }
      if (event.code === "Space") {
        event.preventDefault();
        void togglePlayback();
      }
      if (event.key === "ArrowLeft") stepFrame(-1);
      if (event.key === "ArrowRight") stepFrame(1);
      if (event.key.toLowerCase() === "r") seek(0);
      if (event.key === "Escape") setShowShortcuts(false);
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [seek, stepFrame, togglePlayback]);

  useEffect(() => {
    if (!fakeEgocentric && videoRef.current) {
      videoRef.current.playbackRate = playbackRate;
    }
  }, [playbackRate, fakeEgocentric]);

  if (!motionData || !openSimMotion) {
    return <LoadingScreen failed={failed} />;
  }

  const currentOpenSimFrame = openSimMotion.frames[frameIndex];
  const kneeValues = getMetricValues(currentOpenSimFrame, "knee");
  const hipValues = getMetricValues(currentOpenSimFrame, "hip");
  const currentAngles = {
    leftKnee: kneeValues.left,
    rightKnee: kneeValues.right,
    leftHip: hipValues.left,
    rightHip: hipValues.right,
  };

  const sourceTabLabel = fakeEgocentric ? "Egocentric" : "Original";
  const SourceIcon = fakeEgocentric ? Glasses : Film;

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#workspace" aria-label="CeRebel Kinetic Lens">
          <img
            className="brand-logo"
            src="/assets/cerebel-logo.svg"
            width={595}
            height={194}
            alt="CeRebel"
          />
          <span className="brand-copy">
            <strong>Kinetic Lens</strong>
            <small>{fakeEgocentric ? "Egocentric inspection" : "Motion inspection"}</small>
          </span>
        </a>

        <div className="topbar__session">
          <span className="status-dot" aria-hidden="true" />
          Session 01
          <i />
          {fakeEgocentric ? "Device stream" : "Local analysis"}
        </div>

        <div className="topbar__actions">
          <button
            type="button"
            onClick={() => setShowShortcuts((value) => !value)}
            aria-expanded={showShortcuts}
            aria-label="Keyboard shortcuts"
          >
            <HelpCircle size={17} />
            <span>Keys</span>
          </button>
          <button
            type="button"
            onClick={() => workspaceRef.current?.requestFullscreen?.()}
            aria-label="Enter full screen"
          >
            <Expand size={17} />
            <span>Expand</span>
          </button>
        </div>

        {showShortcuts && (
          <div className="shortcut-popover" role="dialog" aria-label="Keyboard shortcuts">
            <strong>Keyboard</strong>
            <span><kbd>Space</kbd> Play / pause</span>
            <span><kbd>←</kbd><kbd>→</kbd> One frame</span>
            <span><kbd>R</kbd> Return to start</span>
          </div>
        )}
      </header>

      <main>
        <section className="intro" aria-labelledby="page-title">
          <div>
            <p className="eyebrow">
              <Sparkles size={14} aria-hidden="true" />
              {fakeEgocentric
                ? "Egocentric device → OpenSim inspection"
                : "Video-to-OpenSim inspection"}
            </p>
            <h1 id="page-title">
              One action.
              <br />
              Three synchronized readings.
            </h1>
          </div>
          <div className="intro__note">
            <BadgeInfo size={18} aria-hidden="true" />
            <p>
              {fakeEgocentric
                ? "The left panel shows the live SOMA body mesh reconstructed from the egocentric device stream. OpenSim inverse kinematics and estimated dynamics stay synchronized to the same clock."
                : "OpenSim inverse kinematics is followed by inverse dynamics and Joint Reaction Analysis using 93 kg and video-derived external loads. Every force result is explicitly estimated, not measured."}
            </p>
          </div>
        </section>

        <nav className="mobile-view-tabs" aria-label="Demo views">
          {([
            ["video", SourceIcon, sourceTabLabel],
            ["model", Box, "OpenSim 3D"],
            ["curves", Activity, "Angles"],
          ] as const).map(([value, Icon, label]) => (
            <button
              type="button"
              key={value}
              className={mobileView === value ? "is-active" : ""}
              onClick={() => setMobileView(value)}
            >
              <Icon size={15} />
              {label}
            </button>
          ))}
        </nav>

        <section
          id="workspace"
          ref={workspaceRef}
          className="workspace"
          data-mobile-view={mobileView}
          aria-label="Synchronized motion inspection workspace"
        >
          <article className="evidence-panel evidence-panel--video" data-view="video">
            <header className="panel-header">
              <div>
                <span className="panel-index">01</span>
                <div>
                  <h2>{fakeEgocentric ? "Egocentric stream" : "Original video"}</h2>
                  <p>
                    {fakeEgocentric
                      ? "GEM-X SOMA mesh · device body feed"
                      : "Supplied capture · source pixels"}
                  </p>
                </div>
              </div>
              <span
                className={
                  fakeEgocentric
                    ? "panel-badge panel-badge--device"
                    : "panel-badge panel-badge--source"
                }
              >
                {fakeEgocentric ? "DEVICE" : "SOURCE"}
              </span>
            </header>
            <div className="video-stage">
              {fakeEgocentric ? (
                <SomaMeshViewport
                  mesh={somaMesh}
                  frameIndex={meshFrame}
                  failed={somaFailed}
                />
              ) : (
                <video
                  ref={videoRef}
                  src="/assets/layup-original.mp4"
                  preload="auto"
                  playsInline
                  onPlay={() => setIsPlaying(true)}
                  onPause={() => {
                    setIsPlaying(false);
                    updateFromVideo();
                  }}
                  onEnded={() => setIsPlaying(false)}
                  onLoadedMetadata={() => {
                    const video = videoRef.current;
                    if (!video) return;
                    video.pause();
                    const revealFirstFrame = () => {
                      video.removeEventListener("seeked", revealFirstFrame);
                      video.currentTime = 0;
                      updateFromVideo();
                    };
                    video.addEventListener("seeked", revealFirstFrame);
                    video.currentTime = 0.001;
                  }}
                  onLoadedData={() => updateFromVideo()}
                  onClick={() => void togglePlayback()}
                />
              )}
              <div className="video-frame-corners" aria-hidden="true">
                <i /><i /><i /><i />
              </div>
              <div className="video-hud">
                <span>FRAME {String(frameIndex + 1).padStart(3, "0")}</span>
                <span>{eventLabel.toUpperCase()}</span>
              </div>
            </div>
            <footer className="panel-footer">
                  <span>{fakeEgocentric ? "SOMA · rooted global" : "720 × 1280"}</span>
              <span>{fakeEgocentric ? "Mesh master clock" : "Video master clock"}</span>
            </footer>
          </article>

          <article className="evidence-panel evidence-panel--model" data-view="model">
            <header className="panel-header">
              <div>
                <span className="panel-index">02</span>
                <div>
                  <h2>OpenSim 3D</h2>
                  <p>Height-scaled model · solved IK state</p>
                </div>
              </div>
              <span className="panel-badge panel-badge--estimated">ESTIMATED GRF</span>
            </header>
            <OpenSimViewport
              motion={openSimMotion}
              dynamics={estimatedDynamics}
              frameIndex={frameIndex}
            />
            <footer className="panel-footer">
              <span>Drag to orbit · pinch to zoom</span>
              <span>IK fit RMS {(currentOpenSimFrame.markerRmseM * 100).toFixed(1)} cm</span>
            </footer>
          </article>

          <article className="evidence-panel evidence-panel--curves" data-view="curves">
            <header className="panel-header">
              <div>
                <span className="panel-index">03</span>
                <div>
                  <h2>Joint angles &amp; dynamics</h2>
                  <p>OpenSim IK angles with estimated force and torque</p>
                </div>
              </div>
              <span className="panel-badge panel-badge--estimated">EST. GRF</span>
            </header>
            <AnalysisChart
              data={openSimMotion}
              dynamics={estimatedDynamics}
              duration={duration}
              events={motionData.events}
              frameIndex={frameIndex}
              metric={metric}
              onMetricChange={setMetric}
              onSeek={seek}
            />
            <footer className="panel-footer panel-footer--angles">
              <span>L knee {currentAngles.leftKnee.toFixed(1)}°</span>
              <span>R knee {currentAngles.rightKnee.toFixed(1)}°</span>
              <span>L hip {currentAngles.leftHip.toFixed(1)}°</span>
              <span>R hip {currentAngles.rightHip.toFixed(1)}°</span>
            </footer>
          </article>

          <div className="transport" aria-label="Synchronized playback controls">
            <button
              className="transport__play"
              type="button"
              onClick={() => void togglePlayback()}
              aria-label={isPlaying ? "Pause" : "Play"}
            >
              {isPlaying ? <Pause fill="currentColor" /> : <Play fill="currentColor" />}
            </button>
            <button
              className="transport__step"
              type="button"
              onClick={() => stepFrame(-1)}
              aria-label="Previous frame"
            >
              <ChevronLeft />
            </button>
            <button
              className="transport__step"
              type="button"
              onClick={() => stepFrame(1)}
              aria-label="Next frame"
            >
              <ChevronRight />
            </button>

            <span className="transport__time">{formatTime(currentTime)}</span>

            <div className="timeline">
              <input
                type="range"
                min="0"
                max={duration}
                step={1 / fps}
                value={currentTime}
                onChange={(event) => seek(Number(event.target.value))}
                aria-label="Timeline"
                style={{ "--progress": `${(currentTime / duration) * 100}%` } as React.CSSProperties}
              />
              <div className="timeline__events" aria-hidden="true">
                {motionData.events.map((event) => (
                  <i
                    key={event.label}
                    style={{ left: `${(event.time / duration) * 100}%` }}
                  />
                ))}
              </div>
            </div>

            <span className="transport__time transport__duration">{formatTime(duration)}</span>
            <span className="transport__frame">{frameIndex + 1} / {motionData.metadata.frameCount}</span>

            <label className="speed-control">
              <span className="sr-only">Playback speed</span>
              <select
                value={playbackRate}
                onChange={(event) => setPlaybackRate(Number(event.target.value))}
              >
                <option value="0.25">0.25×</option>
                <option value="0.5">0.5×</option>
                <option value="1">1×</option>
                <option value="1.5">1.5×</option>
              </select>
            </label>
            <button
              className="transport__reset"
              type="button"
              onClick={() => {
                if (!fakeEgocentric) videoRef.current?.pause();
                setIsPlaying(false);
                playOriginRef.current = null;
                seek(0);
              }}
            >
              <RotateCcw size={15} />
              Reset
            </button>
          </div>
        </section>
      </main>

      <footer className="site-footer">
        <span>CeRebel · Kinetic Lens</span>
        <span>An intelligence that sees motion.</span>
      </footer>
    </div>
  );
}

export default App;
