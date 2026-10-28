"""The neck IK extract writes into session/ik/ (deleted with the session), not kinetic-lens/outputs/."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "kinetic-lens"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import ik_pipeline  # noqa: E402
from retargeting_neck_v2 import extract_soma  # noqa: E402


class NeckExtractDirTests(unittest.TestCase):
    def test_extract_lands_in_session_ik_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hpe = tmp / "gemx" / "hpe_results.pt"
            hpe.parent.mkdir()
            hpe.write_bytes(b"x")
            ik_dir = tmp / "sessions" / "SID" / "ik"
            ik_dir.mkdir(parents=True)
            outputs = tmp / "neck-outputs"
            frames = 4

            def fake_worker(cmd, check):
                # Stand-in for _extract_worker.py under GEM-X python: SOMA77 joints + local poses.
                out = Path(cmd[cmd.index("--output") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                rng = np.random.default_rng(0)
                np.savez_compressed(
                    out,
                    joints_raw=rng.normal(size=(frames, 77, 3)),
                    poses_aa=rng.normal(scale=0.1, size=(frames, 77, 3)),
                    hpe=np.array([str(hpe)]),
                )

            paths = {"session": tmp / "sessions" / "SID", "pose3d": ik_dir / "pose3d.json", "hpe": hpe, "fps": 30.0}
            with mock.patch.object(extract_soma, "session_paths", return_value=paths), \
                    mock.patch.object(extract_soma, "OUTPUTS_DIR", outputs), \
                    mock.patch.object(extract_soma.subprocess, "run", side_effect=fake_worker):
                aligned = ik_pipeline._extract_soma_aligned(
                    "SID", ik_dir, device="cpu", hpe_results=hpe, fps=30.0
                )

            self.assertEqual(aligned, ik_dir / "soma_aligned.npz")
            for name in ("soma_raw.npz", "soma_aligned.npz", "extract_meta.json"):
                self.assertTrue((ik_dir / name).is_file(), name)
            self.assertFalse(outputs.exists())
            self.assertEqual(json.loads((ik_dir / "extract_meta.json").read_text())["frames"], frames)
            self.assertEqual(np.load(aligned)["joints_opensim"].shape, (frames, 77, 3))


if __name__ == "__main__":
    unittest.main()
