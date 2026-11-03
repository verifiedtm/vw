"""Prepare immutable, content-addressed shots with one explicit SDR pipeline."""

import hashlib
import json
from dataclasses import asdict

from .ffmpeg import digest, export, probe, version
from .project import Project, WorkbenchError
from .timeline import Shot

PIPELINE_VERSION = 1


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
    source = project.media(shot.asset)
    w, h = dimensions(project, preview)
    key = {
        "pipeline": PIPELINE_VERSION,
        "source": identity or digest(source),
        "tool": tool_version or version(),
        "shot": asdict(shot),
        "size": [w, h],
        "fps": str(project.fps),
        "effect_settings": project.config.get("effects", {}).get(shot.effect, {}),
    }
    key["shot"].pop("row")
    key["shot"].pop("at")
    key["shot"].pop("layer")
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
    filters = [
        f"trim=duration={float((shot.src_out - shot.src_in) / 1000):.9f}",
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
    filters += ["format=argb", f"colorchannelmixer=aa={shot.opacity}"]
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
    export(
        [
            *inputs,
            "-ss",
            f"{shot.src_in / 1000:.9f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            ",".join(filters),
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
