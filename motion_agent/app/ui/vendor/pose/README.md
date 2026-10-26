# Pose runtime and model (self-hosted)

The in-browser swing check and camera guide run pose detection on the user's device.
These files are served from our own origin so the browser never contacts a third party for them.

| File | Source | SHA-256 |
|---|---|---|
| `0.10.14/vision_bundle.mjs` | npm `@mediapipe/tasks-vision@0.10.14` | `e77f281f9619150d937023c355bae170e9120e3b9e43f1e23a2a7bee07197669` |
| `0.10.14/wasm/vision_wasm_internal.js` | same package | `9440cf0cc0cea21800e31581ec32aeedcc5fbf9df4509796bbc7d3f99e52ab9c` |
| `0.10.14/wasm/vision_wasm_internal.wasm` | same package | `f82a8e6c05e08a44cc9f9e7ec5f845935bcbb1b1500ebe8c2f4812fb4e2917dc` |
| `0.10.14/wasm/vision_wasm_nosimd_internal.js` | same package | `abe9b6fbeaf86fcb53a5edce3926c82ccb0619e18fed4d9d9ce561ee7f55e054` |
| `0.10.14/wasm/vision_wasm_nosimd_internal.wasm` | same package | `38b61feab2fd7934e05cbe9f68baa308978a5e3b7f85c1913bb8ae89b8ef8b97` |
| `pose_landmarker_lite_f16_v1.task` | `storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/` | `59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a` |

Both are Apache License 2.0 (`LICENSE-Apache-2.0.txt`): the package declares it in its
`package.json`, and the model card ("Model Card BlazePose GHUM 3D") states it. The model card
lists people farther than about 4 m (14 ft) from the camera and multi-person scenes as out of scope.

The server sends everything under `/static/vendor/` with a one-year immutable cache and never
rewrites it for the base path, so a new version must go in a new folder or file name.
