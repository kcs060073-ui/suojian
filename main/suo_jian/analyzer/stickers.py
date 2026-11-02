from __future__ import annotations

from pathlib import Path


def analyze_stickers(video: Path, duration: float, max_candidates: int = 12) -> tuple[dict, str | None]:
    """Return conservative sticker-like graphic candidates from sampled frames."""
    caveat = "压平成片无法区分贴纸、花字与画面原生图形，也无法恢复剪映贴纸素材名称；结果仅供人工复核。"
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"available": False, "candidates": [], "method": "frame_color_contour_heuristic", "exact_asset_names": False, "caveat": caveat}, "OpenCV 未安装，贴纸分析不可用。"
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {"available": False, "candidates": [], "method": "frame_color_contour_heuristic", "exact_asset_names": False, "caveat": caveat}, "视频无法打开，贴纸分析不可用。"
    candidates: list[dict] = []
    sample_count = max(4, min(24, int(duration) + 1))
    try:
        for timestamp in np.linspace(0, max(0.0, duration - 0.05), sample_count):
            cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            height, width = frame.shape[:2]
            scale = min(1.0, 480 / max(width, 1))
            frame = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
            height, width = frame.shape[:2]
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, np.array([0, 145, 150]), np.array([179, 255, 255]))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            frame_area = height * width
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                ratio = (w * h) / max(frame_area, 1)
                if ratio < 0.002 or ratio > 0.12 or w / max(h, 1) > 5 or h / max(w, 1) > 5:
                    continue
                center_y = (y + h / 2) / height
                if 0.54 <= center_y <= 0.78:
                    continue
                normalized = {"x": round(x / width, 3), "y": round(y / height, 3), "width": round(w / width, 3), "height": round(h / height, 3)}
                if any(abs(float(timestamp) - old["start"]) < 1.1 and abs(normalized["x"] - old["bbox"]["x"]) < 0.08 and abs(normalized["y"] - old["bbox"]["y"]) < 0.08 for old in candidates):
                    continue
                likely_text = (w / max(h, 1) >= 1.15 and w / max(width, 1) < 0.36) or ((y + h / 2) / height >= 0.4 and ratio < 0.012)
                candidates.append({"start": round(float(timestamp), 3), "end": round(min(duration, float(timestamp) + 0.8), 3), "duration": round(min(0.8, max(0.05, duration - float(timestamp))), 3), "type": "text_graphic_candidate" if likely_text else "sticker_candidate", "category": "字幕/花字候选" if likely_text else "独立贴纸候选", "label": "字幕/花字候选（不是贴纸）" if likely_text else "独立贴纸候选", "bbox": normalized, "confidence": round(min(0.62, 0.34 + ratio * 3), 3), "caveat": caveat})
                if len(candidates) >= max_candidates:
                    break
            if len(candidates) >= max_candidates:
                break
    finally:
        cap.release()
    for index, item in enumerate(candidates, start=1):
        item["id"] = f"ST{index:02d}"
    return {"available": True, "candidates": candidates, "method": "frame_color_contour_heuristic_with_text_graphic_filter", "exact_asset_names": False, "caveat": caveat}, None
