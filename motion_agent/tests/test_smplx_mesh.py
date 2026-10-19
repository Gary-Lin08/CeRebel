"""Tests for average SMPL-X shape + mesh clip helpers."""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from motion_agent.mesh.average_shape import (
    average_smplx_shape_path,
    load_average_smplx_betas,
    load_average_smplx_shape,
)
from motion_agent.mesh.soma_to_smplx import resolve_hpe_results, smplx_mesh_status


DATA_DIR = Path("/root/CerebelServer/data")
SESSION = DATA_DIR / "sessions" / "2151D83E-CF3E-4680-8047-B5F8C209D14B"


class TestAverageSmplxShape(unittest.TestCase):
    def test_load_average_betas(self) -> None:
        path = average_smplx_shape_path()
        self.assertTrue(path.is_file(), path)
        data = load_average_smplx_shape()
        self.assertEqual(data["model_type"], "smplx")
        betas = load_average_smplx_betas()
        self.assertEqual(betas.shape[0], 10)
        self.assertTrue(np.allclose(betas, 0.0))
        padded = load_average_smplx_betas(num_betas=16)
        self.assertEqual(padded.shape[0], 16)
        self.assertTrue(np.allclose(padded[:10], 0.0))


class TestSmplxMeshStatus(unittest.TestCase):
    def test_status_for_known_session(self) -> None:
        if not SESSION.is_dir():
            self.skipTest("sample session missing")
        hpe = resolve_hpe_results(SESSION)
        self.assertIsNotNone(hpe)
        self.assertTrue(Path(hpe).is_file())
        status = smplx_mesh_status(SESSION)
        self.assertFalse(status.get("error"))
        self.assertTrue(status.get("hpe_available"))


if __name__ == "__main__":
    unittest.main()
