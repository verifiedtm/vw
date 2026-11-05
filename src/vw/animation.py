"""Validated, output-frame keyframes shared by media, geometry, and masks.

Animation.from_dict canonicalizes property tracks into sorted immutable keys.
LIMITS defines bounds and neutral defaults. Millisecond times round once at exact
project FPS; timeline times then subtract the resolved segment start. Marker
references are allowed only in timeline time_base, independent of time_unit.
Keys must resolve to distinct visible output frames, including after rounding.

Interpolation belongs to the outgoing interval: hold retains a key's value until
the next key; linear interpolates numeric values. Endpoints hold outside the key
range, and rotation does not wrap by shortest path. value and expression implement
the same evaluator for Python and ffmpeg callers.

Layer/mask animation uses the fitted canvas after static source preparation.
Treatment applies in BT.709 YUV before the affine transform. The transform samples
onto the fixed output canvas with premultiplied bilinear sampling, rotating about
the canvas center without refitting a rotated bounding box. Applied masks move
with layer animation; mask_animation transforms only mask alpha. Resolved keys,
not JSON whitespace or ordering, determine animation identity. See normalize.py
for static/animated cache boundaries and USAGE.md for authoring examples.
"""

import json
import math
from dataclasses import dataclass
from fractions import Fraction

from .markers import named_markers, resolve_reference
from .project import Project, WorkbenchError

# Defaults are neutral additions to the existing static preparation pipeline.
LIMITS = {
    "opacity": (0, 1, 1),
    "x": (-10, 10, 0),
    "y": (-10, 10, 0),
    "scale": (0.01, 100, 1),
    "rotation": (-36000, 36000, 0),
    "brightness": (-1, 1, 0),
    "contrast": (0, 3, 1),
    "saturation": (0, 3, 1),
}


@dataclass(frozen=True)
class Keyframe:
    frame: int
    value: float
    interpolation: str = "linear"


@dataclass(frozen=True)
class Animation:
    properties: tuple[tuple[str, tuple[Keyframe, ...]], ...]

    @classmethod
    def from_dict(cls, data, *, fps: Fraction, at: int, frames: int, markers=None):
        """Resolve authoring times to segment-local output frames once."""
        try:
            if not isinstance(data, dict) or set(data) - {
                "version",
                "time_base",
                "time_unit",
                "properties",
            }:
                raise ValueError("unknown animation keys or expected an object")
            if type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
                raise ValueError("version must be 1")
            base = data.get("time_base", "local")
            unit = data.get("time_unit", "frames")
            if base not in ("local", "timeline") or unit not in ("frames", "ms"):
                raise ValueError(
                    "time_base must be local/timeline; time_unit frames/ms"
                )
            properties = data.get("properties")
            if not isinstance(properties, dict) or not properties:
                raise ValueError("properties must be a nonempty object")
            result = []
            for name, keys in sorted(properties.items()):
                if name not in LIMITS:
                    raise ValueError(f"unknown property {name!r}")
                if not isinstance(keys, list) or not keys:
                    raise ValueError(f"{name}: expected a nonempty keyframe list")
                parsed = []
                for key in keys:
                    if not isinstance(key, dict) or set(key) - {
                        "time",
                        "value",
                        "interpolation",
                    }:
                        raise ValueError(f"{name}: unknown keyframe keys")
                    time = key["time"]
                    if isinstance(time, str):
                        if base != "timeline":
                            raise ValueError(
                                f"{name}: marker references require timeline time_base"
                            )
                        frame = resolve_reference(time, fps=fps, markers=markers or {})
                    else:
                        if type(time) is not int or time < 0:
                            raise ValueError(
                                f"{name}: time must be a nonnegative integer or marker reference"
                            )
                        frame = (
                            round(Fraction(time, 1000) * fps) if unit == "ms" else time
                        )
                    if base == "timeline":
                        frame -= at
                    if not 0 <= frame < frames:
                        raise ValueError(
                            f"{name}: keyframe outside segment output frames"
                        )
                    value = key["value"]
                    low, high, _ = LIMITS[name]
                    if (
                        type(value) not in (int, float)
                        or not math.isfinite(value)
                        or not low <= value <= high
                    ):
                        raise ValueError(
                            f"{name}: value must be finite in [{low}, {high}]"
                        )
                    interpolation = key.get("interpolation", "linear")
                    if interpolation not in ("hold", "linear"):
                        raise ValueError(
                            f"{name}: interpolation must be hold or linear"
                        )
                    parsed.append(Keyframe(frame, float(value), interpolation))
                parsed.sort(key=lambda key: key.frame)
                if len({key.frame for key in parsed}) != len(parsed):
                    raise ValueError(
                        f"{name}: conflicting keyframes on the same output frame"
                    )
                result.append((name, tuple(parsed)))
            return cls(tuple(result))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise WorkbenchError(f"Invalid animation: {exc}") from exc

    def value(self, name: str, frame: int) -> float:
        keys = dict(self.properties).get(name)
        if not keys:
            return LIMITS[name][2]
        if frame <= keys[0].frame:
            return keys[0].value
        for a, b in zip(keys, keys[1:], strict=False):
            if frame < b.frame:
                if a.interpolation == "hold":
                    return a.value
                return a.value + (b.value - a.value) * (frame - a.frame) / (
                    b.frame - a.frame
                )
        return keys[-1].value

    def expression(self, name: str, variable: str = "N") -> str:
        """Generate numeric-only ffmpeg expressions from validated keyframes."""
        keys = dict(self.properties).get(name)
        if not keys:
            return str(LIMITS[name][2])
        expr = str(keys[-1].value)
        for a, b in reversed(list(zip(keys, keys[1:], strict=False))):
            interval = str(a.value)
            if a.interpolation == "linear":
                interval = f"({a.value}+({b.value - a.value})*({variable}-{a.frame})/{b.frame - a.frame})"
            expr = f"if(lt({variable},{b.frame}),{interval},{expr})"
        return f"if(lt({variable},{keys[0].frame}),{keys[0].value},{expr})"

    def manifest(self):
        return {
            name: [
                dict(frame=k.frame, value=k.value, interpolation=k.interpolation)
                for k in keys
            ]
            for name, keys in self.properties
        }


def load_animation(project: Project, path: str, *, at: int, frames: int, markers=None):
    if not path:
        return None
    source = project.path(path)
    if any(
        source.is_relative_to(project.root / folder) for folder in ("cache", "output")
    ):
        raise WorkbenchError("animation files must live outside cache/ and output/")
    try:

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key {key!r}")
                result[key] = value
            return result

        data = json.loads(source.read_text(), object_pairs_hook=unique)
        return Animation.from_dict(
            data,
            fps=project.fps,
            at=at,
            frames=frames,
            markers=named_markers(project) if markers is None else markers,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkbenchError(f"Cannot read animation {path}: {exc}") from exc


def filters(animation: Animation) -> list[str]:
    """Inverse-map one affine transform, clipping only at the output canvas.

    A transparent border makes bilinear sampling at source edges well-defined.
    Premultiplied interpolation avoids dark/colored fringes around alpha edges.
    """
    names = dict(animation.properties)
    result = []
    treatment = [
        f"{name}='{animation.expression(name, 'n')}'"
        for name in ("brightness", "contrast", "saturation")
        if name in names
    ]
    if treatment:
        result += [
            "scale=in_range=pc:out_range=tv:out_color_matrix=bt709",
            "format=yuva444p",
            "eq=eval=frame:" + ":".join(treatment),
            "scale=in_color_matrix=bt709:in_range=tv:out_range=pc",
            "format=argb",
        ]
    transform = bool(names.keys() & {"x", "y", "scale", "rotation"})
    if transform:
        x, y, scale, angle = (
            animation.expression(k) for k in ("x", "y", "scale", "rotation")
        )
        angle = f"(({angle})*PI/180)"
        dx, dy = f"(X-(W-1)/2-({x})*(W-2))", f"(Y-(H-1)/2-({y})*(H-2))"
        sx = f"((cos({angle})*{dx}+sin({angle})*{dy})/({scale})+(W-1)/2)"
        sy = f"((-sin({angle})*{dx}+cos({angle})*{dy})/({scale})+(H-1)/2)"
        coords = f"st(0,{sx});st(1,{sy});"
        inside = "between(ld(0),0,W-1)*between(ld(1),0,H-1)"
        channels = [
            f"{option}='{coords}if({inside},{sample}(ld(0),ld(1)),0)'"
            for option, sample in (("r", "r"), ("g", "g"), ("b", "b"), ("a", "alpha"))
        ]
        result += [
            "format=rgba",
            "pad=iw+2:ih+2:1:1:color=black@0",
            "format=gbrap16le",
            "premultiply=inplace=1",
            "geq=" + ":".join(channels),
            "unpremultiply=inplace=1",
            "crop=iw-2:ih-2:1:1",
        ]
    if "opacity" in names:
        opacity = animation.expression("opacity")
        result += [
            "format=gbrap16le",
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({opacity})'",
        ]
    return result + ["format=argb"]
