# Neck-aware retargeting v2 (isolated)

This directory is a **standalone experiment**. It does not modify the production Kinetic Lens pipeline:

```text
GEM-X SOMA77 → pose3d → virtual markers → OpenSim IK → RajagopalLaiUhlrich2023 → GLB
```

## 1. Dependencies

- GEM-X Python (`CEREBEL_GEMX_PYTHON`, default `/root/GEM-X/.venv/bin/python`) for SomaLayer decode
- OpenSim Python (`CEREBEL_IK_PYTHON`, default `/root/CerebelServer/.venv-ik/bin/python`) for IK, GLB export, evaluation
- Official Rajagopal model at `kinetic-lens/analysis/RajagopalLaiUhlrich2023.osim`
- Geometry at `kinetic-lens/analysis/Geometry/` (referenced, not copied)
- `numpy`, `matplotlib` (plots), `ffmpeg` optional (MP4; GIF fallback)

## 2. Build the augmented model

```bash
cd /root/CerebelServer
PYTHONPATH=kinetic-lens python -m retargeting_neck_v2.build_augmented_model
```

Writes `models/RajagopalLaiUhlrich2023_neck.osim` plus local head/neck VTPs under `models/geometry/`.

## 3. Calibration

Offsets live in `calibration/anatomical_offsets.json`.

Per-sequence SOMA→OpenSim body-frame calibration is written by the demo to `calibration/soma_to_opensim_neck.json` (and copied conceptually into `outputs/<id>/`).

```bash
PYTHONPATH=kinetic-lens python -c "from retargeting_neck_v2.anatomical_landmarks import load_or_init_offsets; load_or_init_offsets()"
```

## 4. Run one sequence

```bash
cd /root/CerebelServer
PYTHONPATH=kinetic-lens /root/CerebelServer/.venv-ik/bin/python -m retargeting_neck_v2.demo \
    --sequence 1872A5C0-60F0-4C3B-9A1E-000000060001
```

Equivalent:

```bash
PYTHONPATH=kinetic-lens python -m retargeting_neck_v2.demo --sequence <session_id>
```

No Python files need editing. `--device cuda:0` (falls back to CPU in the extract worker).

## 5. Outputs

```text
outputs/<sequence>/
    soma_raw.npz
    soma_aligned.npz
    old/opensim-motion.json          # production IK copy
    old/rajagopal-bones.glb
    new/opensim-motion.json          # neck-aware IK
    new/rajagopal-bones-neck.glb
    ik-work/                         # scaled osim, trc, mot, diagnostics
    plots/neck_coordinates.png
    plots/head_orientation_error.png
    plots/head_position_error.png
    plots/old_vs_new.mp4 (or .gif)
    metrics.json
    REPORT.json
    sanity_checks.json
```

## 6. Compare old vs new

`metrics.json` → `head_orientation_relative_to_torso`:

- `old_RMSE_deg` — production Rajagopal (skull welded to torso)
- `new_RMSE_deg` — articulated neck/head after IK
- `improvement_percent`

Side-by-side animation: `plots/old_vs_new.mp4`.

Load `new/rajagopal-bones-neck.glb` with `new/opensim-motion.json` in the existing Kinetic Lens OpenSim viewport pattern (body names now include `neck` and `head`).

## Tests

```bash
cd /root/CerebelServer
PYTHONPATH=kinetic-lens python -m unittest retargeting_neck_v2.tests.test_neck_v2
```

---

## Anatomical assumptions

### Directly observed from SOMA

- SOMA77 **joint positions** from SomaLayer (`joints`, metres)
- SOMA77 **local axis-angle poses** (`global_orient` + `body_pose`, 77 joints)
- Hierarchy / names from `SOMA_neutral.npz` after dropping unused `Root`:
  `Hips, Spine1, Spine2, Chest, Neck1, Neck2, Head, HeadEnd, Jaw, LeftEye, RightEye, …`
- Production **per-frame yaw normalize** and **SOMA Y-up → OpenSim** map (`cerebel_ik_retarget`, imported read-only)

`Neck1` is **not** C7. `Chest` is **not** the jugular notch. `Head` is a kinematic joint, not a skull anatomical landmark.

### Derived geometrically

- Global SOMA rotations via parent FK, then the same world linear maps as the joints
- Head orientation blended with a geometric frame from `Head`, `HeadEnd`, `LeftEye`, `RightEye` so axial rotation is observable
- Landmarks:

```text
p(t) = p_body(t) + R_body(t) * delta
```

  - **C7 / T1**: offsets in the Chest frame along the Chest→Neck1 direction (posterior/superior estimates)
  - **Jugular notch**: anterior Chest-frame offset
  - **Acromions**: Chest-frame offsets blended with SOMA `LeftArm` / `RightArm` (glenohumeral, not clavicle)
  - **HEAD_A/B/C**: rigid triad in the Head frame (forehead / left / right)

### Assumed / calibrated

- Two-joint cervical topology after Hu et al. (2025), adapted onto Rajagopal: `torso → neck` (C7/T1-region) and `neck → head` (C1/skull-region), **3 rotational DOFs each, 0 translational**
- Unscaled joint centres `NECK_IN_TORSO = (-0.025, 0.418, 0)`, `HEAD_IN_NECK = (0.010, 0.090, 0)` in Rajagopal torso/neck frames
- Uniform Kinetic Lens scale **185 cm / 93 kg** (`VISUAL_SCALE = 1.85/1.75`); neck offsets scale with the rest of the model
- Neck/head **mass and inertia are kinematic placeholders** (torso 21.23 kg, neck 1.10 kg, head 4.50 kg). **Not valid for physiological inverse dynamics.**
- Skull/jaw VTPs translated from torso origin into the head body frame; neck visualization is a cylinder, not a cervical vertebra mesh
- Selected relative mapping after comparing step-to-step geodesic stability: **Chest→Neck1** (lower) and **Neck2→Head** (upper)
- `R_calib = R_soma_neutralᵀ` so OpenSim default (identity) matches the upright SOMA frame; no second world yaw

### Explicitly not claimed

- Markerless C7 or sternal notch measurement
- Validated cervical inverse dynamics
- Sequence-specific angle hacks

## New OpenSim DOFs

| Coordinate | Joint | Axis (OpenSim) | Role |
| --- | --- | --- | --- |
| `neck_extension` | `cervical_lower` | Z | C7/T1 flexion/extension |
| `neck_bending` | `cervical_lower` | X | lateral bending |
| `neck_rotation` | `cervical_lower` | Y | axial rotation |
| `head_extension` | `cervical_upper` | Z | C1/skull flexion/extension |
| `head_bending` | `cervical_upper` | X | lateral bending |
| `head_rotation` | `cervical_upper` | Y | axial rotation |

Shoulder joints remain children of `torso`. Head geometry is removed from `torso`.

## Marker weights

Legacy weights unchanged. New: C7 4.0, T1 3.5, JUGULAR 3.5, acromions 2.5, HEAD_A/B/C 5.0 (see `config.py`).
