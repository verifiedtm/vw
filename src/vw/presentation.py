"""Resolve capped media placement in final-canvas coordinates."""

import math
from fractions import Fraction


def upscale_limit(value):
    if value is None or value == "unlimited":
        return None
    if isinstance(value, bool):
        raise ValueError("max_upscale must be positive or unlimited")
    try:
        limit = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("max_upscale must be positive or unlimited") from exc
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("max_upscale must be positive or unlimited")
    return limit


def display_size(video, crop, rotation):
    """Mirror ffmpeg's autorotation, crop, transpose, then square-pixel conversion."""
    width, height = video["width"], video["height"]
    sar_text = video.get("sample_aspect_ratio", "1:1")
    sar = (
        Fraction(sar_text.replace(":", "/"))
        if sar_text not in ("N/A", "0:1")
        else Fraction(1)
    )
    metadata_rotation = next(
        (d["rotation"] for d in video.get("side_data_list", []) if "rotation" in d),
        0,
    )
    # Keep the original square-pixel correction on its physical axis. Inverting
    # SAR after a transpose preserves aspect ratio but changes the native-size cap.
    pixel_width, pixel_height = sar, Fraction(1)
    if abs(metadata_rotation) % 180 == 90:
        width, height = height, width
        pixel_width, pixel_height = pixel_height, pixel_width
    if crop:
        width, height = crop[:2]
    native = (width * pixel_width, height * pixel_height)
    return native[::-1] if rotation % 180 == 90 else native


def placement(project, shot, size=None):
    """Cap fit scaling before clipping; previews map the final integer rectangle."""
    if shot.max_upscale is None or shot.shape:
        return {"max_upscale": None}
    settings = project.config["project"]
    canvas = (settings["width"], settings["height"])
    native = shot.display_size
    ratios = [Fraction(c, 1) / n for c, n in zip(canvas, native, strict=True)]
    desired = (
        ratios
        if shot.fit == "stretch"
        else [min(ratios) if shot.fit == "contain" else max(ratios)] * 2
    )
    limit = Fraction(str(shot.max_upscale))
    scales = [min(s, limit) for s in desired]
    # Floor so raster rounding never exceeds the requested cap. RGBA permits odd sizes.
    scaled = [max(1, math.floor(n * s)) for n, s in zip(native, scales, strict=True)]
    visible = [min(c, s) for c, s in zip(canvas, scaled, strict=True)]
    offset = [(c - v) // 2 for c, v in zip(canvas, visible, strict=True)]
    clipped = [(s - v) // 2 for s, v in zip(scaled, visible, strict=True)]
    final = {
        "scaled_size": scaled,
        "visible_size": visible,
        "position": offset,
        "crop_offset": clipped,
    }
    result = {
        "max_upscale": shot.max_upscale,
        "source_display_size": [float(n) for n in native],
        "limited": any(s > limit for s in desired),
        "final": final,
    }
    if size is not None:
        # Map edges rather than independently fitting to the smaller preview canvas.
        target = {key: [] for key in final}
        for axis in range(2):
            ratio = Fraction(size[axis], canvas[axis])
            start = round(offset[axis] * ratio)
            end = round((offset[axis] + visible[axis]) * ratio)
            length = max(1, min(size[axis] - start, end - start))
            crop_start = round(clipped[axis] * ratio)
            target["position"].append(start)
            target["visible_size"].append(length)
            target["crop_offset"].append(crop_start)
            target["scaled_size"].append(
                max(crop_start + length, round(scaled[axis] * ratio))
            )
        result["target"] = target
    return result
