# Human (General Motion) Sport Pack Knowledge Base

Status: content build complete (see _meta/build_report.txt)
Corpus pack id: human
Product pack id: general (HumanMotionPack in motion_agent/sports/packs/human.py)
Product: CeRebel body-first coaching when no sport-specific pack matches
Spec: motion_agent/knowledge/corpus/_templates/SPORT_PACK_KB_SPEC.txt

Scope v1:
- Sport-agnostic human movement coaching from reconstructed body motion
- Posture stack (head–rib–pelvis), balance, limb–torso coordination
- Simple movement phases: prep → action → recover
- Common posture faults, feel cues, and short drills usable across activities
- OpenSim / SMPL-X joint naming aligned with sibling corpus opensim_motion_tools.txt

Out of scope v1:
- Medical diagnosis, pathology labeling, or treatment prescription
- Sport-specific technique (use dedicated sport packs when detected)
- Ball, club, racket, or implement tracking
- Force-plate or EMG ground truth claims from monocular video alone

_index: all content dirs except _meta/
