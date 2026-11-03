"""Local coarse camera-motion measurement; never infers scene facts."""
from pathlib import Path
import math


def detect_camera_motion(path: Path, start=0.0, end=None) -> dict:
    import cv2
    import numpy as np
    capture = cv2.VideoCapture(str(path))
    duration = capture.get(cv2.CAP_PROP_FRAME_COUNT) / max(capture.get(cv2.CAP_PROP_FPS), 1)
    width,height = capture.get(cv2.CAP_PROP_FRAME_WIDTH),capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
    end = min(float(end or duration), duration)
    samples = np.linspace(start, max(start, end - .05), min(12, max(3, int(end-start)+1)))
    previous = None
    measurements = []
    for timestamp in samples:
        capture.set(cv2.CAP_PROP_POS_MSEC, float(timestamp)*1000)
        success, frame = capture.read()
        if not success:
            continue
        frame = cv2.resize(frame, (480, round(frame.shape[0]*480/frame.shape[1])))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Most subtitle pixels are below this boundary. RANSAC rejects other
        # independently moving features rather than treating them as camera motion.
        gray = gray[:round(gray.shape[0]*.78)]
        if previous is not None:
            points = cv2.goodFeaturesToTrack(previous, 180, .025, 8)
            if points is not None:
                moved, status, _ = cv2.calcOpticalFlowPyrLK(previous, gray, points, None)
                if moved is not None:
                    left = points[status.ravel()==1].reshape(-1,2)
                    right = moved[status.ravel()==1].reshape(-1,2)
                    if len(left) >= 15:
                        transform, inliers = cv2.estimateAffinePartial2D(left, right, method=cv2.RANSAC, ransacReprojThreshold=2.5)
                        if transform is not None:
                            center = np.array([240, gray.shape[0]/2])
                            displacement = transform[:,:2] @ center + transform[:,2] - center
                            scale = math.hypot(transform[0,0], transform[1,0])
                            measurements.append((float(displacement[0]), float(displacement[1]), float(scale-1), float(inliers.mean())))
        previous = gray
    capture.release()
    if not measurements:
        return {"type":"unknown", "confidence":0, "strength":0, "method":"optical-flow-affine", "duration":duration}
    dx,dy,zoom,inliers = np.median(measurements,axis=0)
    if abs(zoom) > .012 and abs(zoom)*240 > math.hypot(dx,dy)*.75:
        motion = "zoom_in" if zoom > 0 else "zoom_out"
    elif math.hypot(dx,dy) < 1.2:
        motion = "static"
    elif abs(dx) >= abs(dy):
        motion = "pan_left" if dx > 0 else "pan_right"
    else:
        motion = "pan_up" if dy > 0 else "pan_down"
    return {"type":motion, "confidence":round(min(.8, .4+float(inliers)*.35),3),
            "strength":round(min(1, max(math.hypot(dx,dy)/12,abs(zoom)*12)),3),
            "method":"optical-flow-affine", "duration":duration, "width":width, "height":height}
