"""Project text geometry profile, expressed relative to the canvas short edge.

The native-unit profile is provisional until an editor capture is available;
it must not be described as a pixel-perfect calibration.
"""
import math

TEXT_LAYOUT_VERSION = "short-edge-percent-v2"


def native_font_size(pixel_size, width, height):
    values = tuple(float(v) for v in (pixel_size, width, height))
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("字幕字号和画布尺寸必须大于零。")
    size, width, height = values
    return size * 100.0 / min(width, height)


def native_border_width(pixel_outline, pixel_size):
    # pyJianYingDraft maps its UI width to an em-relative stroke by * .002.
    return min(100.0, max(0.0, float(pixel_outline) / float(pixel_size) / .002))


def prepare_export_plan(plan):
    return {**plan, "text_layout_version": TEXT_LAYOUT_VERSION}
