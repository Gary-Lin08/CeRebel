#!/usr/bin/env python3
"""Extract temporally smoothed monocular 3D landmarks for Kinetic Lens.

MediaPipe's world landmarks provide a camera-relative 3D skeleton in metres.
The result is still an estimate from one camera, but unlike the previous
2D-angle heuristic it retains out-of-plane motion and global body orientation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from scipy.signal import savgol_filter


LANDMARK_NAMES = [
    "nose",
    "left_eye_inner",
    "left_eye",
    "left_eye_outer",
    "right_eye_inner",
    "right_eye",
    "right_eye_outer",
    "left_ear",
    "right_ear",
    "mouth_left",
    "mouth_right",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_pinky",
    "right_pinky",
    "left_index",
    "right_index",
    "left_thumb",
    "right_thumb",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
    "left_heel",
    "right_heel",
    "left_foot_index",
    "right_foot_index",
]

POSE_CONNECTIONS = tuple(mp.solutions.pose.POSE_CONNECTIONS)


def interpolate_missing(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    output = values.copy()
    frame_indices = np.arange(len(values))
    for landmark in range(values.shape[1]):
        observed = valid[:, landmark]
        if not observed.any():
            continue
        for axis in range(values.shape[2]):
            output[:, landmark, axis] = np.interp(
                frame_indices,
                frame_indices[observed],
                values[observed, landmark, axis],
            )
    return output


def smooth(values: np.ndarray) -> np.ndarray:
    if len(values) < 7:
        return values
    window = min(11, len(values) if len(values) % 2 else len(values) - 1)
    return savgol_filter(values, window_length=window, polyorder=2, axis=0, mode="interp")


def draw_overlay(frame: np.ndarray, image_landmarks: np.ndarray, valid: np.ndarray) -> None:
    height, width = frame.shape[:2]
    for start, end in POSE_CONNECTIONS:
        if not valid[start] or not valid[end]:
            continue
        a = tuple(np.round(image_landmarks[start, :2] * [width, height]).astype(int))
        b = tuple(np.round(image_landmarks[end, :2] * [width, height]).astype(int))
        cv2.line(frame, a, b, (95, 255, 168), 3, cv2.LINE_AA)
    for index, point in enumerate(image_landmarks):
        if not valid[index]:
            continue
        center = tuple(np.round(point[:2] * [width, height]).astype(int))
        color = (255, 190, 76) if "left" in LANDMARK_NAMES[index] else (255, 110, 125)
        cv2.circle(frame, center, 4, color, -1, cv2.LINE_AA)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    args = parser.parse_args()

    capture = cv2.VideoCapture(str(args.video))
    fps = float(capture.get(cv2.CAP_PROP_FPS)) or 30.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

    image_frames: list[np.ndarray] = []
    world_frames: list[np.ndarray] = []
    visibility_frames: list[np.ndarray] = []
    source_frames: list[np.ndarray] = []

    with mp.solutions.pose.Pose(
        static_image_mode=False,
        model_complexity=2,
        smooth_landmarks=True,
        enable_segmentation=False,
        min_detection_confidence=0.45,
        min_tracking_confidence=0.45,
    ) as pose:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            result = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            source_frames.append(frame)

            if not result.pose_landmarks or not result.pose_world_landmarks:
                image_frames.append(np.full((33, 3), np.nan, dtype=np.float64))
                world_frames.append(np.full((33, 3), np.nan, dtype=np.float64))
                visibility_frames.append(np.zeros(33, dtype=np.float64))
                continue

            image_frames.append(
                np.array(
                    [[item.x, item.y, item.z] for item in result.pose_landmarks.landmark],
                    dtype=np.float64,
                )
            )
            world_frames.append(
                np.array(
                    [[item.x, item.y, item.z] for item in result.pose_world_landmarks.landmark],
                    dtype=np.float64,
                )
            )
            visibility_frames.append(
                np.array(
                    [item.visibility for item in result.pose_world_landmarks.landmark],
                    dtype=np.float64,
                )
            )

    capture.release()
    images = np.stack(image_frames)
    worlds = np.stack(world_frames)
    visibility = np.stack(visibility_frames)
    valid = np.isfinite(worlds).all(axis=2) & (visibility >= 0.35)
    images = interpolate_missing(images, np.isfinite(images).all(axis=2))
    worlds = interpolate_missing(worlds, np.isfinite(worlds).all(axis=2))
    worlds = smooth(worlds)
    images = smooth(images)

    args.overlay.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(args.overlay),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    for index, frame in enumerate(source_frames):
        draw_overlay(frame, images[index], valid[index])
        cv2.putText(
            frame,
            f"MONOCULAR 3D LANDMARKS  /  FRAME {index + 1:03d}",
            (22, height - 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (238, 244, 246),
            1,
            cv2.LINE_AA,
        )
        writer.write(frame)
    writer.release()

    payload = {
        "metadata": {
            "source": str(args.video),
            "method": "MediaPipe Pose world landmarks, model_complexity=2",
            "coordinateSystem": "MediaPipe camera-relative world coordinates in metres",
            "fps": fps,
            "frameCount": len(worlds),
            "reportedFrameCount": frame_count,
            "warning": "Monocular 3D estimate; not calibrated optical motion capture",
        },
        "landmarkNames": LANDMARK_NAMES,
        "frames": [
            {
                "time": round(index / fps, 6),
                "image": np.round(images[index], 7).tolist(),
                "world": np.round(worlds[index], 7).tolist(),
                "visibility": np.round(visibility[index], 5).tolist(),
            }
            for index in range(len(worlds))
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, separators=(",", ":")))

    selected = [0, min(28, len(worlds) - 1), min(41, len(worlds) - 1), len(worlds) - 1]
    for index in selected:
        left_hip, right_hip = worlds[index, 23], worlds[index, 24]
        left_knee, right_knee = worlds[index, 25], worlds[index, 26]
        print(
            f"frame={index + 1:03d} "
            f"hip_width={np.linalg.norm(left_hip - right_hip):.3f}m "
            f"L_thigh={np.linalg.norm(left_hip - left_knee):.3f}m "
            f"R_thigh={np.linalg.norm(right_hip - right_knee):.3f}m"
        )


if __name__ == "__main__":
    main()
