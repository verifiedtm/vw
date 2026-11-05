"""Parse declarative edit decisions using exact rational time arithmetic.

A Shot uses source-frame indices when frame_indexed, otherwise milliseconds.
Its at, frames, end and transition_frames always use output project frames.
Source-frame ranges are half-open decoded presentation-order indices, unrelated
to project FPS or codec keyframes. VFR durations come from frame-index boundaries,
including the last frame's declared duration at EOF. Stills and generated shapes
instead interpret source-frame ranges at project FPS.

For frame rows, absent nframes is round(source_duration / speed * project.fps).
Explicit nframes controls trimming or last-frame holding during preparation.
Millisecond rows round their output start and speed-adjusted duration separately.
Use Fraction until these boundaries; Python round supplies nearest-even rounding.
Marker resolution uses the same rule before shot-local animation keys are loaded.

load validates source bounds, treatments, masks and underlying crossfade coverage.
validate adds gaps, soundtrack warnings, named markers, presentation geometry and
resolved positions. Position records expose exclusive end_frame and both local
and absolute timeline frames for animation keys. Authoring examples live in
USAGE.md; optional input columns are enumerated in project.TIMELINE_FIELDS.
"""

import math
import re
from dataclasses import dataclass
from fractions import Fraction

from . import frame_index
from .animation import Animation, load_animation
from .assets import IMAGE_EXTENSIONS
from .ffmpeg import probe, stream_duration
from .geometry import Shape, load_shapes
from .markers import named_markers, resolve_reference
from .presentation import display_size, placement, upscale_limit
from .project import Project, WorkbenchError, read_csv


@dataclass(frozen=True)
class Shot:
    row: int
    asset: str
    src_in: int
    src_out: int
    at: int
    frames: int
    layer: int
    fit: str
    speed: Fraction
    effect: str
    crop: tuple[int, ...] | None
    rotation: int
    transition: str
    transition_frames: int
    opacity: float
    image: bool
    shape: Shape | None = None
    mask: Shape | None = None
    mask_invert: bool = False
    frame_indexed: bool = False
    blend_mode: str = "normal"
    animation: Animation | None = None
    mask_animation: Animation | None = None
    max_upscale: float | None = None
    display_size: tuple[Fraction, Fraction] | None = None

    @property
    def end(self):
        return self.at + self.frames


def frame_at(milliseconds: int, fps: Fraction) -> int:
    # Round only at the boundary; never accumulate floating point durations.
    return round(Fraction(milliseconds, 1000) * fps)


def seconds(frames: int, fps: Fraction) -> str:
    return f"{float(frames / fps):.12f}"


def load(project: Project) -> list[Shot]:
    markers = named_markers(project)
    assets = project.assets()
    shapes = load_shapes(project.config)
    rows = read_csv(project.path("timeline.csv"), ["asset"])
    shots = []
    metadata = {}
    boundaries = {}
    for number, row in enumerate(rows, 2):
        try:
            asset = row["asset"]
            shape = None
            if asset.startswith("shape:"):
                name = asset.removeprefix("shape:")
                if name not in shapes:
                    raise ValueError(f"unknown shape {name!r}")
                shape = shapes[name]
                image = True
                for field, default in (("fit", "contain"), ("rotation", "0")):
                    if row.get(field) and row[field] != default:
                        raise ValueError(f"generated shapes do not support {field}")
                if row.get("crop") or row.get("effect"):
                    raise ValueError("generated shapes do not support crop or effect")
            else:
                if asset not in assets:
                    raise ValueError(f"unknown asset {asset!r}; run vw ingest first")
                path = project.media(asset)
                if path.is_relative_to(project.root / "cache") or path.is_relative_to(
                    project.root / "output"
                ):
                    raise ValueError(
                        "timeline sources must live outside generated cache/ and output/ directories"
                    )
                if asset not in metadata:
                    metadata[asset] = frame_index.metadata(path)
                info = metadata[asset]
                video = next(
                    (
                        s
                        for s in info["streams"]
                        if s["codec_type"] == "video"
                        and not s.get("disposition", {}).get("attached_pic")
                    ),
                    None,
                )
                if video is None:
                    raise ValueError(f"{asset} has no video stream")
                image = path.suffix.lower() in IMAGE_EXTENSIONS
                if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
                    raise ValueError(
                        "HDR inputs require explicit SDR conversion before ingest"
                    )
            speed = Fraction(row.get("speed") or "1")
            if speed <= 0 or speed > 100:
                raise ValueError("speed must be greater than 0 and at most 100")
            frame_indexed = any(
                row.get(k)
                for k in ("src_in_frame", "src_out_frame", "at_frame", "nframes")
            )
            if frame_indexed:
                if any(row.get(k) for k in ("src_in", "src_out", "at")):
                    raise ValueError(
                        "cannot mix millisecond and frame timing in one row"
                    )
                if (
                    not row.get("at_frame")
                    or not row.get("src_in_frame")
                    or not row.get("src_out_frame")
                ):
                    raise ValueError(
                        "frame timing requires src_in_frame, src_out_frame, at_frame"
                    )
                start, end = (int(row[k]) for k in ("src_in_frame", "src_out_frame"))
                value = row["at_frame"]
                at = (
                    resolve_reference(value, fps=project.fps, markers=markers)
                    if value.startswith("marker:")
                    else int(value)
                )
                if start < 0 or end <= start or at < 0:
                    raise ValueError(
                        "require 0 <= src_in_frame < src_out_frame and at_frame >= 0"
                    )
                if image:
                    # Stills have no source cadence; their range uses project frames.
                    length = Fraction(end - start, 1) / project.fps
                else:
                    if asset not in boundaries:
                        boundaries[asset] = frame_index.load(
                            project, path, video
                        ).boundaries
                    stamps = boundaries[asset]
                    if end >= len(stamps):
                        raise ValueError(
                            f"src_out_frame {end} exceeds source frame count {len(stamps) - 1}"
                        )
                    length = stamps[end] - stamps[start]
                frames = (
                    int(row["nframes"])
                    if row.get("nframes")
                    else round(length / speed * project.fps)
                )
            else:
                if not all(row.get(k) for k in ("src_in", "src_out", "at")):
                    raise ValueError("millisecond timing requires src_in, src_out, at")
                start, end = (int(row[key]) for key in ("src_in", "src_out"))
                value = row["at"]
                at_ms = 0 if value.startswith("marker:") else int(value)
                if start < 0 or end <= start or at_ms < 0:
                    raise ValueError("require 0 <= src_in < src_out and at >= 0")
                if not image and end > round(stream_duration(video, info) * 1000) + 1:
                    raise ValueError(f"src_out {end} exceeds source duration")
                at = (
                    resolve_reference(value, fps=project.fps, markers=markers)
                    if value.startswith("marker:")
                    else frame_at(at_ms, project.fps)
                )
                frames = round(Fraction(end - start, 1000) / speed * project.fps)
            if frames < 1:
                raise ValueError("segment is shorter than one output frame")
            fit = row.get("fit") or "contain"
            if fit not in ("contain", "cover", "stretch"):
                raise ValueError("fit must be contain, cover, or stretch")
            rotation = int(row.get("rotation") or 0)
            if rotation not in (0, 90, 180, 270):
                raise ValueError("rotation must be 0, 90, 180, or 270")
            crop = None
            if row.get("crop"):
                crop = tuple(int(n) for n in row["crop"].split(":"))
                if len(crop) != 4:
                    raise ValueError("crop must be width:height:x:y")
                w, h, x, y = crop
                source_w, source_h = video["width"], video["height"]
                rotation_metadata = next(
                    (
                        d.get("rotation", 0)
                        for d in video.get("side_data_list", [])
                        if "rotation" in d
                    ),
                    0,
                )
                if abs(rotation_metadata) % 180 == 90:
                    source_w, source_h = source_h, source_w
                if (
                    min(w, h) <= 0
                    or min(x, y) < 0
                    or x + w > source_w
                    or y + h > source_h
                ):
                    raise ValueError("crop lies outside source dimensions")
            max_upscale = upscale_limit(
                row.get("max_upscale") or project.config["project"].get("max_upscale")
            )
            if shape and row.get("max_upscale"):
                raise ValueError("generated shapes do not support max_upscale")
            native_size = None if shape else display_size(video, crop, rotation)
            effect = row.get("effect") or ""
            if (
                effect
                and effect not in ("grayscale", "vintage")
                and effect not in project.config.get("effects", {})
            ):
                raise ValueError(f"unknown effect {effect!r}")
            transition = row.get("transition") or "cut"
            if transition not in ("cut", "fade", "crossfade"):
                raise ValueError("transition must be cut, fade, or crossfade")
            transition_frames = int(row.get("transition_frames") or 0)
            if transition != "cut" and not 0 < transition_frames <= frames:
                raise ValueError(
                    "fade/crossfade needs transition_frames between 1 and segment length"
                )
            opacity = float(row.get("opacity") or 1)
            if not math.isfinite(opacity) or not 0 <= opacity <= 1:
                raise ValueError("opacity must be between 0 and 1")
            blend_mode = row.get("blend_mode") or "normal"
            if blend_mode not in ("normal", "screen", "add", "multiply", "difference"):
                raise ValueError(
                    "blend_mode must be normal, screen, add, multiply, or difference"
                )
            mask_name = row.get("mask") or ""
            if mask_name and mask_name not in shapes:
                raise ValueError(f"unknown mask shape {mask_name!r}")
            inversion = row.get("mask_invert") or "false"
            if inversion not in ("true", "false"):
                raise ValueError("mask_invert must be true or false")
            if inversion == "true" and not mask_name:
                raise ValueError("mask_invert requires a mask")
            animation = load_animation(
                project,
                row.get("animation") or "",
                at=at,
                frames=frames,
                markers=markers,
            )
            mask_animation = load_animation(
                project,
                row.get("mask_animation") or "",
                at=at,
                frames=frames,
                markers=markers,
            )
            if mask_animation and not mask_name:
                raise ValueError("mask_animation requires a mask")
            shots.append(
                Shot(
                    number,
                    asset,
                    start,
                    end,
                    at,
                    frames,
                    int(row.get("layer") or 0),
                    fit,
                    speed,
                    effect,
                    crop,
                    rotation,
                    transition,
                    transition_frames,
                    opacity,
                    image,
                    shape,
                    shapes.get(mask_name),
                    inversion == "true",
                    frame_indexed,
                    blend_mode,
                    animation,
                    mask_animation,
                    None if shape else max_upscale,
                    native_size,
                )
            )
        except (ValueError, ZeroDivisionError, KeyError, WorkbenchError) as exc:
            raise WorkbenchError(f"timeline.csv row {number}: {exc}") from exc
    if not shots:
        raise WorkbenchError("timeline.csv is empty; add at least one segment")
    limit = project.config["project"].get("duration_frames", 0)
    if limit and any(shot.end > limit for shot in shots):
        raise WorkbenchError("Timeline extends beyond configured duration_frames")
    # Validate named effect settings before any expensive rendering.
    for name, values in project.config.get("effects", {}).items():
        if not re.fullmatch(r"[\w-]+", name) or not isinstance(values, dict):
            raise WorkbenchError(f"Invalid effect preset {name!r}")
        limits = {"saturation": (0, 3), "contrast": (0, 3), "brightness": (-1, 1)}
        for key, value in values.items():
            if (
                key not in limits
                or not isinstance(value, (float, int))
                or not limits[key][0] <= value <= limits[key][1]
            ):
                raise WorkbenchError(f"Invalid effect setting {name}.{key}")
    ordered = sorted(shots, key=lambda s: (s.layer, s.row))
    for i, shot in enumerate(ordered):
        if shot.transition == "crossfade":
            cursor = shot.at
            for start, end in sorted(
                (s.at, s.end) for s in ordered[:i] if s.opacity > 0
            ):
                if start <= cursor:
                    cursor = max(cursor, end)
            if cursor < shot.at + shot.transition_frames:
                raise WorkbenchError(
                    f"timeline.csv row {shot.row}: crossfade needs an underlying segment throughout its transition; overlap clips or use fade"
                )
    return shots


def total_frames(project: Project, shots: list[Shot]) -> int:
    return project.config["project"].get("duration_frames", 0) or max(
        s.end for s in shots
    )


def resolved_positions(shots: list[Shot]) -> list[dict]:
    result = []
    for shot in shots:
        animations = {}
        for name, animation in (
            ("layer", shot.animation),
            ("mask", shot.mask_animation),
        ):
            if animation:
                animations[name] = {
                    prop: [
                        dict(
                            frame=k.frame,
                            timeline_frame=shot.at + k.frame,
                            value=k.value,
                            interpolation=k.interpolation,
                        )
                        for k in keys
                    ]
                    for prop, keys in animation.properties
                }
        result.append(
            dict(
                row=shot.row,
                start_frame=shot.at,
                end_frame=shot.end,
                animations=animations,
            )
        )
    return result


def validate(project: Project) -> dict:
    shots = load(project)
    total = total_frames(project, shots)
    intervals = sorted((s.at, s.end) for s in shots if s.opacity > 0)
    gaps = []
    cursor = 0
    for start, end in intervals:
        if start > cursor:
            gaps.append({"start_frame": cursor, "end_frame": start})
        cursor = max(cursor, end)
    if cursor < total:
        gaps.append({"start_frame": cursor, "end_frame": total})
    audio = project.config["project"].get("audio_master")
    warnings = []
    if audio:
        audio_path = project.media(audio)
        if any(
            audio_path.is_relative_to(project.root / folder)
            for folder in ("cache", "output")
        ):
            raise WorkbenchError(
                "audio_master must live outside generated cache/ and output/ directories"
            )
        info = probe(audio_path)
        if not any(s["codec_type"] == "audio" for s in info["streams"]):
            raise WorkbenchError("audio_master has no audio stream")
        soundtrack = next(s for s in info["streams"] if s["codec_type"] == "audio")
        if stream_duration(soundtrack, info) < float(total / project.fps):
            warnings.append(
                "Soundtrack is shorter than timeline; the remainder will be silent"
            )
    else:
        warnings.append("No audio_master configured; export will be silent")
    if gaps:
        warnings.append("Timeline gaps will show the configured background")
    return {
        "valid": True,
        "positions": resolved_positions(shots),
        "markers": {
            name: round(time * project.fps)
            for name, time in named_markers(project).items()
        },
        "segments": len(shots),
        "duration_frames": total,
        "fps": str(project.fps),
        "gaps": gaps,
        "warnings": warnings,
        "presentation": {str(s.row): placement(project, s) for s in shots},
    }


def visualize(project: Project) -> dict:
    """Write a self-contained, escaped SVG showing the actual timeline positions."""
    from html import escape

    from .project import atomic_text

    shots = load(project)
    total = total_frames(project, shots)
    layers = sorted({s.layer for s in shots})
    width = 1100
    height = 90 + 65 * len(layers)
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#171c26"/>',
        '<g font-family="sans-serif" font-size="13" fill="white">',
        f'<text x="20" y="25">Timeline · {total} frames · {escape(str(project.fps))} fps</text>',
    ]
    for i, layer in enumerate(layers):
        y = 55 + i * 65
        svg.append(f'<text x="10" y="{y + 25}">Layer {layer}</text>')
        for shot in (s for s in shots if s.layer == layer):
            x = 100 + 980 * shot.at / total
            w = 980 * shot.frames / total
            svg.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="40" rx="4" fill="hsl({shot.row * 57 % 360},50%,40%)"><title>{escape(shot.asset)}: {shot.at}–{shot.end}</title></rect>'
            )
            svg.append(f'<text x="{x + 4}" y="{y + 24}">{escape(shot.asset)}</text>')
    for name, position in named_markers(project).items():
        frame = round(position * project.fps)
        if frame > total:
            continue
        x = 100 + 980 * frame / total
        svg.append(
            f'<path d="M{x},42 V{height - 15}" stroke="#ffd166" stroke-dasharray="4 3"><title>marker:{escape(name)}: frame {frame}</title></path>'
        )
        svg.append(f'<text x="{x}" y="42">{escape(name)}</text>')
    for shot, position in zip(shots, resolved_positions(shots), strict=True):
        layer_y = 55 + layers.index(shot.layer) * 65
        for target, properties in position["animations"].items():
            for prop, keys in properties.items():
                for key in keys:
                    frame = key["timeline_frame"]
                    x = 100 + 980 * frame / total
                    svg.append(
                        f'<circle cx="{x}" cy="{layer_y + 35}" r="3" fill="white"><title>row {shot.row} {target}.{prop}: timeline frame {frame}, local frame {key["frame"]}</title></circle>'
                    )
    svg.append("</g></svg>")
    path = project.generated("output/timeline.svg")
    atomic_text(path, "\n".join(svg))
    return {"timeline": str(path)}
