"""Mesh helpers for motion_agent golf phase preview.

Display path follows Kinetic Lens: GEM-X SOMA → OpenSim IK → rajagopal-bones.glb.
Average SMPL-X shape helpers remain available for other uses.
"""

from motion_agent.mesh.average_shape import (
    average_smplx_shape_path,
    load_average_smplx_betas,
    load_average_smplx_shape,
)
from motion_agent.mesh.opensim_clip import (
    get_opensim_skeleton_clip,
    get_smplx_mesh_clip,
    opensim_skeleton_status,
    smplx_mesh_status,
)

__all__ = [
    "average_smplx_shape_path",
    "load_average_smplx_betas",
    "load_average_smplx_shape",
    "get_opensim_skeleton_clip",
    "get_smplx_mesh_clip",
    "opensim_skeleton_status",
    "smplx_mesh_status",
]
