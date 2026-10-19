# Kinetic Lens prototype decisions

- This is a standalone demo website. It must not be embedded in or coupled to the Motionverse marketing website.
- Product name: **Kinetic Lens**. Descriptor: **OpenSim Motion Lab**.
- The core experience synchronizes three evidence views on one clock: original video, a real three-dimensional OpenSim musculoskeletal model, and joint-angle curves.
- Use the official OpenSim `RajagopalLaiUhlrich2023` model and official mesh geometry. Do not replace the middle viewport with a hand-drawn or generic skeleton.
- Current subject metadata: height 185 cm, mass 93 kg.
- Do not drive the OpenSim model with hand-authored 2D-angle heuristics. Recover temporally smoothed monocular 3D landmarks, convert them to fixed-segment virtual markers, scale the model from supplied height/mass, and run the OpenSim IK solver.
- The supplied single-view video supports monocular 3D OpenSim IK as a preview only. Do not label it as calibrated optical motion capture, validated clinical IK, measured dynamics, or muscle activation.
- Keep mechanics annotations scientifically separated: cyan straight arrows are joint-centre velocity, violet curved arrows are flexion/extension direction, and the green downward arrow is gravity direction at the OpenSim whole-body centre of mass.
- Estimate external loads from the 93 kg model’s whole-body COM acceleration and video-derived left/right foot contact. Label this continuously and exactly as **ESTIMATED GRF**; never imply force-plate or insole measurement.
- Run OpenSim Inverse Dynamics and Joint Reaction Analysis with those estimated loads. Their joint-force and joint-moment outputs inherit the estimate boundary and a confidence ceiling of 61%; they are exploratory, not clinical or performance-decision results.
- Keep vector encodings distinct: amber is ESTIMATED GRF, indigo is the selected OpenSim joint reaction, cyan remains joint-centre velocity, violet remains rotation direction, and lime remains gravity.
- Hip, knee, and ankle on both sides are selectable in the 3D model and through equivalent buttons. The inspector shows reaction direction, N, BW, inverse-dynamics joint moment, flexion/extension angle, angular velocity, and current confidence.
- The dynamics inspector is collapsible: use a visible minus control to reduce it to a compact title bar and a plus control to restore it. Keep the current joint and playback state when collapsed.
- Default the 3D camera to the source-camera plane so the model pose can be compared directly with the original video; orbit remains available for depth inspection.
- Keep the interface focused on inspecting one clip. Avoid marketing-site sections, generic dashboard card grids, and decorative sci-fi overlays.
- The source video is the master playback clock. Model state, event labels, numeric readouts, and the curve cursor must stay synchronized to the same frame.
