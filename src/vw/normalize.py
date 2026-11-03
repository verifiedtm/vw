"""Prepare immutable, content-addressed shots with one explicit SDR pipeline."""

import hashlib
import json
from dataclasses import asdict

from .ffmpeg import digest, export, probe, version
from .geometry import rasterize
from .project import Project, WorkbenchError
from .timeline import Shot

PIPELINE_VERSION = 3


def dimensions(project: Project, preview: bool) -> tuple[int, int]:
    settings = project.config["project"]
    scale = project.config.get("preview", {}).get("scale", 0.5) if preview else 1
    return tuple(
        max(2, round(settings[k] * scale / 2) * 2) for k in ("width", "height")
    )


def prepare(
    project: Project,
    shot: Shot,
    preview: bool = False,
    *,
    identity: str | None = None,
    tool_version: str | None = None,
):
    w, h = dimensions(project, preview)
    source = (
        rasterize(project, shot.shape, (w, h))
        if shot.shape
        else project.media(shot.asset)
    )
    key = {
        "pipeline": PIPELINE_VERSION,
        "source": identity or (shot.shape.identity() if shot.shape else digest(source)),
        "tool": tool_version or version(),
        "shot": asdict(shot),
        "size": [w, h],
        "fps": str(project.fps),
        "geometry_source": shot.shape.identity() if shot.shape else None,
        "geometry_mask": shot.mask.identity() if shot.mask else None,
        "effect_settings": project.config.get("effects", {}).get(shot.effect, {}),
    }
    key["shot"].pop("row")
    key["shot"].pop("at")
    key["shot"].pop("layer")
    key["shot"].pop("blend_mode")
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
                return destination, True
        except (WorkbenchError, StopIteration, ValueError):
            pass
    trim = (
        f"trim=start_frame={shot.src_in}:end_frame={shot.src_out}"
        if shot.frame_indexed and not shot.image
        else f"trim=duration={float((shot.src_out - shot.src_in) / (project.fps if shot.frame_indexed else 1000)):.12f}"
    )
    filters = [
        trim,
        f"setpts=(PTS-STARTPTS)/({shot.speed})",
    ]
    if shot.crop:
        filters.append("crop=" + ":".join(str(n) for n in shot.crop))
    filters += {
        0: [],
        90: ["transpose=1"],
        180: ["hflip", "vflip"],
        270: ["transpose=2"],
    }[shot.rotation]
    # Convert SDR inputs explicitly to BT.709, limited range before composition.
    filters += [
        f"fps={project.fps}",
        "scale=iw*sar:ih:out_color_matrix=bt709:out_range=tv",
        "setsar=1",
    ]
    if shot.fit == "contain":
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
    inputs += ["-i", str(source)]
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
    return destination, False
