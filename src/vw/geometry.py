"""Declarative canvas geometry, rasterized independently of timeline placement."""

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass

from PIL import Image, ImageDraw, __version__

from .ffmpeg import temporary_output
from .project import Project, WorkbenchError

GENERATOR_VERSION = 1
SUPERSAMPLE = 4


@dataclass(frozen=True)
class Shape:
    type: str
    x: float
    y: float
    width: float
    height: float
    fill: str = "#ffffff"
    stroke: str = "none"
    stroke_width: float = 0
    opacity: float = 1

    def identity(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [GENERATOR_VERSION, SUPERSAMPLE, __version__, asdict(self)],
                sort_keys=True,
            ).encode()
        ).hexdigest()


def load_shapes(config: dict) -> dict[str, Shape]:
    definitions = config.get("shapes", {})
    if not isinstance(definitions, dict):
        raise WorkbenchError("shapes must be a TOML table")
    shapes = {}
    for name, values in definitions.items():
        try:
            if not re.fullmatch(r"[\w-]+", name) or not isinstance(values, dict):
                raise ValueError("require a named shape table")
            unknown = values.keys() - Shape.__dataclass_fields__.keys()
            if unknown:
                raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
            shape = Shape(**values)
            if shape.type not in ("rectangle", "ellipse"):
                raise ValueError("type must be rectangle or ellipse")
            limits = {
                "x": (0, 1),
                "y": (0, 1),
                "width": (0, 2),
                "height": (0, 2),
                "stroke_width": (0, 1),
                "opacity": (0, 1),
            }
            for key, (low, high) in limits.items():
                value = getattr(shape, key)
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not low <= value <= high
                    or (key in ("width", "height") and value == 0)
                ):
                    raise ValueError(
                        f"invalid {key}; expected a finite value in {low}–{high}"
                    )
            for key in ("fill", "stroke"):
                color = getattr(shape, key)
                if not isinstance(color, str) or not re.fullmatch(
                    r"#[0-9a-fA-F]{6}|none", color
                ):
                    raise ValueError(f"{key} must be #RRGGBB or none")
            if shape.stroke_width > min(shape.width, shape.height) / 2:
                raise ValueError("stroke_width exceeds half the smaller dimension")
            if shape.stroke != "none" and shape.stroke_width <= 0:
                raise ValueError("stroke requires a positive stroke_width")
            # Canonicalize equivalent numeric and color representations for caching.
            canonical = asdict(shape)
            for key in limits:
                canonical[key] = float(canonical[key])
            for key in ("fill", "stroke"):
                canonical[key] = canonical[key].lower()
            shapes[name] = Shape(**canonical)
        except (TypeError, ValueError) as exc:
            raise WorkbenchError(f"shapes.{name}: {exc}") from exc
    return shapes


def rasterize(project: Project, shape: Shape, size: tuple[int, int]):
    """Cache a supersampled RGBA surface with an inside stroke and clipped edges."""
    key = hashlib.sha256(f"{shape.identity()}:{size}".encode()).hexdigest()
    path = project.generated(f"cache/geometry/{key}.png")
    if path.is_file():
        try:
            with Image.open(path) as cached:
                if cached.size == size and cached.mode == "RGBA":
                    cached.load()
                    return path
        except OSError:
            pass
    w, h = size
    scale = min(size) * SUPERSAMPLE
    cx, cy = shape.x * w * SUPERSAMPLE, shape.y * h * SUPERSAMPLE
    sw, sh = shape.width * scale, shape.height * scale
    image = Image.new("RGBA", (w * SUPERSAMPLE, h * SUPERSAMPLE))
    draw = ImageDraw.Draw(image)
    # Pillow includes the far endpoint; subtract one supersampled pixel so the
    # declared bounding dimensions do not grow with the rasterization resolution.
    box = (cx - sw / 2, cy - sh / 2, cx + sw / 2 - 1, cy + sh / 2 - 1)
    box = (box[0], box[1], max(box[0], box[2]), max(box[1], box[3]))
    draw_shape = draw.rectangle if shape.type == "rectangle" else draw.ellipse
    draw_shape(
        box,
        fill=None if shape.fill == "none" else shape.fill,
        outline=None if shape.stroke == "none" else shape.stroke,
        width=max(1, round(shape.stroke_width * scale)),
    )
    image = image.resize(size, Image.Resampling.LANCZOS)
    image.putalpha(image.getchannel("A").point(lambda a: round(a * shape.opacity)))
    temp = temporary_output(path)
    try:
        image.save(temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path
