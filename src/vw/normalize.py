"""Prepare immutable, content-addressed shots with one explicit SDR pipeline.

Static preparation resolves source selection, speed/cadence, crop, rotation,
pixel aspect, canvas fit and treatment into lossless RGBA intermediates. Masks
multiply fitted alpha; inversion uses 1-coverage. Static opacity and fades also
multiply coverage. Animation runs afterward on the prepared canvas; render.py
composites it without fitting shots again. PNG/WebP source alpha is preserved.

Static cache keys include source bytes, ffmpeg version, pipeline version, target
size/FPS, relevant treatment/geometry/mask settings and resolved shot properties,
but omit row number, output position, layer and blend mode. Animated preparation
has a separate cache keyed by static identity and resolved layer/mask curves.
Editing keys can reuse static normalization; moving a timeline-timed animation
can change its curves. Animated affine sampling is CPU work per output pixel.

Auto and exact seeking share cache keys because both must select identical frames.
Exact skips seek verification but still hashes and indexes sources. Preparation
holds the final frame or trims to the requested output count, and checks source
integrity before publishing. Authoring syntax lives in USAGE.md.
"""

import hashlib
import json
from dataclasses import asdict, replace

from . import frame_index, metrics
from .animation import filters as animation_filters
from .ffmpeg import digest, export, probe, version
from .geometry import rasterize
from .presentation import placement
from .project import Project, WorkbenchError
from .timeline import Shot

PIPELINE_VERSION = 4


def dimensions(project: Project, preview: bool) -> tuple[int, int]:
    settings = project.config["project"]
    scale = project.config.get("preview", {}).get("scale", 0.5) if preview else 1
    return tuple(
        max(2, round(settings[k] * scale / 2) * 2) for k in ("width", "height")
    )


def validate_seek_mode(seek_mode: str) -> None:
    if seek_mode not in ("auto", "exact"):
        raise WorkbenchError("seek_mode must be auto or exact")


def prepare(
    project: Project,
    shot: Shot,
    preview: bool = False,
    *,
    seek_mode: str = "auto",
    identity: str | None = None,
    tool_version: str | None = None,
):
    """Prepare exact frames; seek_mode only controls verified-seek optimization."""
    validate_seek_mode(seek_mode)
    if shot.animation or shot.mask_animation:
        return prepare_animated(
            project,
            shot,
            preview,
            seek_mode=seek_mode,
            identity=identity,
            tool_version=tool_version,
        )
    w, h = dimensions(project, preview)
    source = (
        rasterize(project, shot.shape, (w, h))
        if shot.shape
        else project.media(shot.asset)
    )
    # Both seek policies produce identical frames and share normalized artifacts.
    key = {
        "pipeline": PIPELINE_VERSION,
        "source": identity or (shot.shape.identity() if shot.shape else digest(source)),
        "tool": tool_version or version(),
        "shot": asdict(shot),
        "size": [w, h],
        "presentation": placement(project, shot, (w, h)),
        "fps": str(project.fps),
        "geometry_source": shot.shape.identity() if shot.shape else None,
        "geometry_mask": shot.mask.identity() if shot.mask else None,
        "effect_settings": project.config.get("effects", {}).get(shot.effect, {}),
    }
    key["shot"].pop("row")
    key["shot"].pop("at")
    key["shot"].pop("layer")
    key["shot"].pop("blend_mode")
    key["shot"].pop("animation")
    key["shot"].pop("mask_animation")
    hashed = hashlib.sha256(
        json.dumps(key, sort_keys=True, default=str).encode()
    ).hexdigest()
    destination = project.generated(f"cache/normalized/{hashed}.mov")
    if destination.is_file():
        try:
            stream = next(
                s for s in probe(destination)["streams"] if s["codec_type"] == "video"
            )
            if (
                stream["width"] == w
                and stream["height"] == h
                and int(stream.get("nb_frames", 0)) == shot.frames
            ):
                metrics.emit(
                    "normalized_cache",
                    status="hit",
                    source=str(source),
                    seek_mode=seek_mode,
                )
                return destination, True
        except (WorkbenchError, StopIteration, ValueError):
            pass
    trim = (
        f"trim=start_frame={shot.src_in}:end_frame={shot.src_out}"
        if shot.frame_indexed and not shot.image
        else f"trim=duration={float((shot.src_out - shot.src_in) / (project.fps if shot.frame_indexed else 1000)):.12f}"
    )
    metrics.emit(
        "normalized_cache", status="miss", source=str(source), seek_mode=seek_mode
    )
    seek_inputs = []
    source_index = None
    if shot.frame_indexed and not shot.image:
        video = next(
            s
            for s in frame_index.metadata(source)["streams"]
            if s["codec_type"] == "video"
            and not s.get("disposition", {}).get("attached_pic")
        )
        source_index = frame_index.load(project, source, video)
        if seek_mode == "auto":
            plan = source_index.seek(shot.src_in, shot.src_out)
            if plan:
                seek_inputs, trim = plan
        else:
            metrics.emit("seek_disabled", seek_mode=seek_mode, source=str(source))
    filters = [
        trim,
        f"setpts=(PTS-STARTPTS)/({shot.speed})",
    ]
    if shot.crop:
        crop_filter = "crop=" + ":".join(str(n) for n in shot.crop)
        if shot.max_upscale is not None:
            crop_filter += ":exact=1"
        filters.append(crop_filter)
    filters += {
        0: [],
        90: ["transpose=1"],
        180: ["hflip", "vflip"],
        270: ["transpose=2"],
    }[shot.rotation]
    # Convert SDR inputs explicitly to BT.709, limited range before composition.
    filters.append(f"fps={project.fps}")
    if shot.max_upscale is None:
        filters += [
            "scale=iw*sar:ih:out_color_matrix=bt709:out_range=tv",
            "setsar=1",
        ]
    if shot.max_upscale is not None and not shot.shape:
        layout = placement(project, shot, (w, h))["target"]
        sw, sh = layout["scaled_size"]
        vw, vh = layout["visible_size"]
        cx, cy = layout["crop_offset"]
        x, y = layout["position"]
        filters += [
            f"scale={sw}:{sh}:out_color_matrix=bt709:out_range=tv",
            "format=rgba",
            "setsar=1",
            f"crop={vw}:{vh}:{cx}:{cy}:exact=1",
            f"pad={w}:{h}:{x}:{y}:color=black@0",
        ]
    elif shot.fit == "contain":
        filters += [
            f"scale={w}:{h}:force_original_aspect_ratio=decrease",
            "format=rgba",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black@0",
        ]
    elif shot.fit == "cover":
        filters += [
            f"scale={w}:{h}:force_original_aspect_ratio=increase",
            f"crop={w}:{h}",
            "format=rgba",
        ]
    else:
        filters += [f"scale={w}:{h}", "format=rgba"]
    treatments = {
        "grayscale": {"saturation": 0},
        "vintage": {"saturation": 0.65, "contrast": 1.08, "brightness": 0.025},
    }
    treatment = project.config.get("effects", {}).get(
        shot.effect, treatments.get(shot.effect, {})
    )
    if treatment:
        # eq operates in YUV; preserve the alpha plane separately via format negotiation.
        filters += [
            "format=yuva444p",
            "eq=" + ":".join(f"{k}={v}" for k, v in treatment.items()),
        ]
    # Masks operate on fitted canvas alpha; opacity and fades follow masking.
    canvas_filters = filters
    filters = ["format=argb", f"colorchannelmixer=aa={shot.opacity}"]
    if shot.transition in ("fade", "crossfade"):
        filters += [f"fade=t=in:s=0:n={shot.transition_frames}:alpha=1"]
        if shot.transition == "fade":
            filters += [
                f"fade=t=out:s={shot.frames - shot.transition_frames}:n={shot.transition_frames}:alpha=1"
            ]
    # Cover fractional source endpoints without changing the declared shot length.
    filters += [
        f"tpad=stop_mode=clone:stop_duration={float(shot.frames / project.fps):.12f}",
        f"trim=end_frame={shot.frames}",
        f"setpts=N/({project.fps}*TB)",
        "setsar=1",
    ]
    inputs = ["-loop", "1"] if shot.image else []
    if not shot.frame_indexed:
        source_start = 0 if shot.shape else shot.src_in
        inputs += ["-ss", f"{source_start / 1000:.9f}"]
    inputs += [*seek_inputs, "-i", str(source)]
    if shot.mask:
        mask_path = rasterize(project, shot.mask, (w, h))
        inputs += ["-loop", "1", "-i", str(mask_path)]
        mask_filters = f"fps={project.fps},format=rgba,alphaextract"
        if shot.mask_invert:
            mask_filters += ",negate"
        graph = (
            f"[0:v]{','.join(canvas_filters)},split[color][alpha];"
            "[alpha]alphaextract[a];"
            f"[1:v]{mask_filters}[mask];"
            "[a][mask]blend=all_expr='A*B/255'[coverage];"
            f"[color][coverage]alphamerge,{','.join(filters)}[out]"
        )
        filter_args = ["-filter_complex", graph, "-map", "[out]"]
    else:
        filter_args = ["-vf", ",".join(canvas_filters + filters), "-map", "0:v:0"]
    export(
        [
            *inputs,
            *filter_args,
            "-an",
            "-frames:v",
            str(shot.frames),
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
            "-threads",
            "1",
        ],
        destination,
    )
    if source_index is not None:
        try:
            source_index.check_source()
        except WorkbenchError:
            destination.unlink(missing_ok=True)
            raise
    return destination, False


def prepare_animated(project, shot, preview, *, seek_mode, identity, tool_version):
    # Animated masks need the unmasked source. All animation changes reuse this
    # static normalization artifact, including source decoding, crop, and fit.
    base = replace(
        shot,
        animation=None,
        mask_animation=None,
        mask=None if shot.mask_animation else shot.mask,
        mask_invert=False if shot.mask_animation else shot.mask_invert,
    )
    source, _ = prepare(
        project,
        base,
        preview,
        seek_mode=seek_mode,
        identity=identity,
        tool_version=tool_version,
    )
    key = {
        "animation_pipeline": 1,
        "source": source.name,
        "animation": shot.animation.manifest() if shot.animation else None,
        "mask_animation": shot.mask_animation.manifest()
        if shot.mask_animation
        else None,
        "mask": shot.mask.identity() if shot.mask_animation else None,
        "mask_invert": shot.mask_invert,
    }
    hashed = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    destination = project.generated(f"cache/animated/{hashed}.mov")
    w, h = dimensions(project, preview)
    if destination.is_file():
        try:
            stream = next(
                s for s in probe(destination)["streams"] if s["codec_type"] == "video"
            )
            if (stream["width"], stream["height"], int(stream.get("nb_frames", 0))) == (
                w,
                h,
                shot.frames,
            ):
                return destination, True
        except (WorkbenchError, StopIteration, ValueError):
            pass
    inputs = ["-i", str(source)]
    graph = []
    label = "0:v"
    if shot.mask_animation:
        mask = rasterize(project, shot.mask, (w, h))
        inputs += ["-loop", "1", "-framerate", str(project.fps), "-i", str(mask)]
        mask_filters = animation_filters(shot.mask_animation) + ["alphaextract"]
        if shot.mask_invert:
            mask_filters += ["negate"]
        graph += [
            "[0:v]format=rgba,split[color][alpha]",
            "[alpha]alphaextract[a]",
            f"[1:v]{','.join(mask_filters)}[mask]",
            "[a][mask]blend=all_expr='A*B/255'[coverage]",
            "[color][coverage]alphamerge[masked]",
        ]
        label = "masked"
    layer_filters = (
        animation_filters(shot.animation) if shot.animation else ["format=argb"]
    )
    graph += [f"[{label}]{','.join(layer_filters)}[out]"]
    export(
        [
            *inputs,
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[out]",
            "-an",
            "-frames:v",
            str(shot.frames),
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
            "-threads",
            "1",
        ],
        destination,
    )
    return destination, False
