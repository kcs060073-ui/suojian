from __future__ import annotations

from pathlib import Path


def analyze_motion(video: Path, start: float, end: float) -> tuple[dict, str | None]:
    """Classify coarse camera motion using sparse optical-flow samples within a shot."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"type": "unknown", "strength": 0.0, "confidence": 0.0}, "OpenCV 未安装，运动分析不可用。"
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = max(2, min(20, int((end - start) * 2) + 1))
    sample_times = np.linspace(start, max(start + 0.05, end - 0.02), frame_count)
    prev_gray = None
    vectors: list[tuple[float, float, float]] = []
    for timestamp in sample_times:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp) * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        frame = cv2.resize(frame, (320, max(1, int(frame.shape[0] * 320 / frame.shape[1]))))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None:
            points = cv2.goodFeaturesToTrack(prev_gray, maxCorners=100, qualityLevel=0.02, minDistance=7)
            if points is not None:
                next_points, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, points, None)
                good_old, good_new = points[status.ravel() == 1], next_points[status.ravel() == 1]
                if len(good_old) >= 8:
                    delta = (good_new - good_old).reshape(-1, 2)
                    dx, dy = np.median(delta, axis=0)
                    # Radial expansion around image center: positive = zoom in.
                    center = np.array([160, gray.shape[0] / 2])
                    old_radius = np.linalg.norm(good_old.reshape(-1, 2) - center, axis=1)
                    new_radius = np.linalg.norm(good_new.reshape(-1, 2) - center, axis=1)
                    radial = float(np.median(new_radius - old_radius))
                    vectors.append((float(dx), float(dy), radial))
        prev_gray = gray
    cap.release()
    if not vectors:
        return {"type": "static", "strength": 0.0, "confidence": 0.25}, None
    median = np.median(np.asarray(vectors), axis=0)
    dx, dy, radial = map(float, median)
    translation = (dx * dx + dy * dy) ** 0.5
    if abs(radial) > max(0.7, translation * 0.8):
        return {"type": "zoom_in" if radial > 0 else "zoom_out", "strength": round(min(abs(radial) / 6, 1), 3), "confidence": 0.5}, None
    if translation > 0.7:
        direction = "right" if dx > 0 else "left"
        return {"type": f"pan_{direction}", "strength": round(min(translation / 5, 1), 3), "confidence": 0.45}, None
    return {"type": "static", "strength": round(min(translation / 2, 1), 3), "confidence": 0.55}, None
