# Implementation report — neck-aware retargeting v2

Isolated under `kinetic-lens/retargeting_neck_v2/`. Production SOMA→pose3d→IK files were not modified.

## Which SOMA joints were used?

SOMA77 (Root dropped from the 78-name `SOMA_neutral.npz` list):

| Role | Index | Name |
| --- | --- | --- |
| pelvis | 0 | Hips |
| thorax | 3 | Chest |
| lower cervical | 4 | Neck1 |
| upper cervical | 5 | Neck2 |
| skull joint | 6 | Head |
| vertex | 7 | HeadEnd |
| eyes | 9, 10 | LeftEye, RightEye |
| GH (not clavicle) | 12, 40 | LeftArm, RightArm |

Also the existing pose3d MediaPipe-mapped limb joints for legacy IK markers.

Local axis-angle poses (`global_orient` + 76 `body_pose`) drive FK rotations. Head yaw is reinforced with a geometric frame from Head / HeadEnd / eyes because Chest–Neck–Head are nearly collinear.

## How are C7 / head / shoulder landmarks estimated?

Not renamed SOMA joints. Each landmark is

```text
p(t) = p_body(t) + R_body(t) * delta
```

with `delta` in `calibration/anatomical_offsets.json`:

- **C7, T1, jugular**: Chest-frame offsets (posterior-superior vs anterior)
- **Acromions**: Chest-frame offsets blended with LeftArm/RightArm
- **HEAD_A/B/C**: rigid non-collinear triad in the Head frame

## How was SOMA aligned to OpenSim?

Same production maps (imported read-only):

1. Per-frame hip facing yaw normalize (`normalize_soma77_joints`)
2. `P = [[0,0,1],[0,1,0],[1,0,0]]` (Y-up right/forward → OpenSim X-forward Z-right)
3. After IK, `apply_global_yaw_to_motion` restores `globalYawRad`

Neck/head orientations are relative to that canonical torso frame. Calibration file: `calibration/soma_to_opensim_neck.json`.

## What exact new OpenSim DOFs were added?

Bodies: `neck`, `head`.

Joints (CustomJoint, 3 rotations, 0 translations):

- `cervical_lower` (`torso` → `neck`): `neck_extension` (Z), `neck_bending` (X), `neck_rotation` (Y)
- `cervical_upper` (`neck` → `head`): `head_extension` (Z), `head_bending` (X), `head_rotation` (Y)

Skull/jaw meshes moved off `torso` onto `head`. Topology follows Hu et al. (2025) two-joint cervical idea, not their numerical parameters.

## How were the new markers attached?

Official Rajagopal marker set is cleared, then:

- original 14 Kinetic Lens virtual markers (unchanged parents/weights)
- `C7` on `neck` origin; `T1`, `JUGULAR`, acromions on `torso`; `HEAD_A/B/C` on `head` with scaled local offsets

## Marker weights

Legacy unchanged. New: C7 4.0, T1 3.5, JUGULAR 3.5, acromions 2.5, HEAD_A/B/C 5.0.

## Sequence evaluated

`1872A5C0-60F0-4C3B-9A1E-000000060001` (`1872_swing.mp4`, 148 frames @ 60 Hz, large yaw change). Old outputs copied from production `session/ik/`.

## Head orientation error

Frame-0-aligned SO(3) geodesic vs SOMA head; old = torso (welded skull):

| | RMSE (deg) | mean | median | p95 | max |
| --- | --- | --- | --- | --- | --- |
| OLD | **68.60** | 50.73 | 43.30 | 113.82 | 115.50 |
| NEW | **45.29** | 30.68 | 18.54 | 97.97 | 98.44 |

**Improvement: 34.0%**

## Limitations

- Kinematic only: neck/head mass/inertia are placeholders; **do not use for cervical inverse dynamics**
- Landmark deltas are generic, not subject-scanned C7/sternum
- Head position RMSE did not improve (0.20 m old vs 0.22 m new); joint-centre vs skull-mesh origin mismatch
- Uncalibrated torso-relative geodesic still shows a large constant SOMA↔OpenSim frame offset
- T1/acromion marker residuals ~7–15 cm from uniform 185 cm scaling vs SOMA size
- Neck visualization is a cylinder, not cervical vertebrae
- HeadEnd/eye geometric frame is an assumption on SOMA vertex meaning
