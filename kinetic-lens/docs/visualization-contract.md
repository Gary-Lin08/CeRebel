# Kinetic Lens visualization contract

## Purpose and evidence

- Claim: one short action clip can be inspected on a shared timeline across source pixels, an OpenSim model state, and derived joint-angle curves.
- Audience: biomechanics and motion-intelligence teams evaluating the workflow.
- Source: supplied 91-frame MOV, monocular 3D pose inference, OpenSim official `RajagopalLaiUhlrich2023` model.
- Measured: source video pixels and supplied subject height/mass.
- Inferred: camera-relative 3D landmarks, fixed-segment virtual markers, uniform anthropometric scaling, OpenSim IK coordinates.
- Not present: calibrated optical markers, segment-specific anthropometry, force plates, pressure insoles, measured centre of pressure, muscle activation, or clinical validation.
- Estimated dynamics: whole-body COM acceleration and video-derived foot contact generate left/right external loads for OpenSim Inverse Dynamics and Joint Reaction Analysis.
- Truth invariant: every external-load-dependent value is continuously labeled **ESTIMATED GRF** and never presented as measured or clinical.
- Confidence boundary: the current result is capped at 61% because both the kinematics and external loads originate from one uncalibrated monocular clip.

## Renderer ownership

- One Three.js / React Three Fiber canvas owns the 3D focal scene.
- Official bone meshes are rendered from a GLB; 80 official OpenSim musculotendon paths are rendered as anatomical polylines.
- HTML owns controls and exact readouts. React/SVG owns the angle curve and annotations.
- The HTML video element is the only animation clock. Frame index is `round(currentTime * fps)`.
- Render-ready signal: `data-model-ready="true"` after the GLB and motion JSON produce a first frame.
- WebGL fallback: a still image and explanatory status appear only if the live scene fails.
- DPR is capped at 1.75 on desktop and 1.25 on mobile; the canvas pauses when the tab is hidden.

## Coordinate frames

| Layer | Coordinate system | Transform owner | Alignment check |
| --- | --- | --- | --- |
| OpenSim bones | X forward, Y up, Z right | Height-scaled OpenSim IK body transforms | pelvis, knees, ankles |
| Muscle paths | OpenSim ground coordinates | OpenSim exported path points | origin/insertion stay attached |
| Mechanics arrows | OpenSim ground coordinates | adjacent IK frames + model mass centre | joint origins, COM, source-plane sign |
| Estimated GRF | OpenSim ground coordinates | COM acceleration + video foot-contact distribution | contact phase, zero during flight, support-side origin |
| Joint reaction | OpenSim ground coordinates | OpenSim JointReaction using ID actuator forces | selected joint, N/BW consistency, direction components |
| Three.js scene | Y up | scene root | front, right, reset camera |
| Video/keypoints | camera-relative image/world coordinates | monocular 3D preprocessing | source overlay spot check |

## Encoding

| Data or state | Visual channel | Must not imply |
| --- | --- | --- |
| Bone geometry | warm ivory surface | subject-specific CT anatomy |
| Musculotendon path | coral line | activation or force |
| Joint-centre velocity | cyan straight arrow + m/s label | force or impulse |
| Flexion / extension direction | violet curved arrow | joint torque or moment |
| Gravity direction | lime downward arrow at whole-body COM | calibrated magnitude or ground-reaction force |
| ESTIMATED GRF | amber arrow + N/BW label | force-plate measurement |
| Selected joint reaction | indigo arrow + inspector | directly measured internal load |
| Inverse-dynamics joint moment | purple/neutral numeric readout in Nm | isolated muscle moment |
| Selected side | electric blue highlight | better/worse |
| Current time | vertical curve cursor | uncertainty bound |
| Video events | labeled ticks | instrumented event timing |

## Interaction and layout

- Desktop: original video / 3D model / angle curve in a single evidence row.
- Mobile portrait: 3D view first with tabs for original and curves; transport remains persistent below.
- Front/source view is the default for direct comparison. Drag rotates, wheel/pinch zooms, and Reset restores the source-aligned camera.
- Mechanics is visible by default and muscles are optional. Six hip/knee/ankle targets are selectable in 3D and through keyboard-accessible buttons. Desktop shows direct force labels; mobile keeps arrows and the compact inspector.
- Space toggles play/pause; left/right arrows step one frame; Escape exits expanded view.
- Reduced motion disables automatic camera settling and keeps a static first frame until playback is explicitly started.
- Reset clears camera and returns playback to the start. No ephemeral hover state is persisted.

## QA

- Run `npm run build` and `npm run test`.
- Verify one desktop and one mobile portrait screenshot.
- Check a nonblank WebGL canvas after model-ready.
- Check all 13 major-joint markers, velocity arrows, rotation arcs, the COM gravity arrow, amber ESTIMATED GRF, and selected indigo joint-reaction arrow at frames 29 and 42.
- Confirm GRF is zero during the flight window, appears at the active foot during support, stays below 3 BW, and the six joint reactions remain finite with a plausible reviewed peak range.
- Confirm each hip/knee/ankle selector updates N, BW, Nm, angle, angular velocity, direction, and confidence without breaking playback synchronization.
- Smoke-test play/pause, seek, one-frame stepping, view tabs, metric tabs, camera drag/zoom/reset, and reduced-motion mode.
- Confirm no fallback duplicates the live scene and no error is logged during one full clip.
- Compare frames 29 and 42 against the video: the lead-leg swing, lifted right knee, trunk inclination, and two-arm ball posture must remain recognizable.
