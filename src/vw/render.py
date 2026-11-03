"""Compose prepared shots; editorial choices live exclusively in project files."""

import hashlib
import json

from .ffmpeg import digest, export, version
from .normalize import dimensions, prepare
from .project import Project, WorkbenchError, write_json
from .timeline import load, seconds, total_frames, validate


def transfer_filter(*, encode: bool = False) -> str:
    """BT.709 OETF and its inverse on full-range 16-bit RGB; alpha is separate."""
    value = "val/65535"
    expr = (
        f"if(lt({value},0.018),4.5*({value}),1.099*pow({value},0.45)-0.099)"
        if encode
        else f"if(lt({value},0.081),({value})/4.5,pow((({value})+0.099)/1.099,1/0.45))"
    )
    return "lutrgb=" + ":".join(f"{c}='65535*({expr})'" for c in "rgb")


def blend_filters(shot, i: int, frames: int, fps, linear: bool) -> list[str]:
    """Blend straight RGB, then apply prepared alpha as coverage exactly once.

    Padding with transparent frames gives every framesync input the full output
    time span, including gaps and frames before/after an arbitrarily placed shot.
    The composition is opaque because it starts with the configured background.
    """
    kernels = {
        "normal": "B",
        "screen": "65535-(65535-A)*(65535-B)/65535",
        "add": "min(65535,A+B)",
        "multiply": "A*B/65535",
        "difference": "abs(A-B)",
    }
    convert = "," + transfer_filter() if linear else ""
    return [
        f"[{i}:v]format=rgba,tpad=start={shot.at}:stop={max(0, frames - shot.end)}:color=black@0,"
        f"trim=end_frame={frames},settb=expr=1/({fps}),setpts=N,split[s{i}][a{i}]",
        f"[a{i}]alphaextract,format=gbrp16le[mask{i}]",
        f"[s{i}]format=gbrp16le{convert}[rgb{i}]",
        f"[base{i - 1}]format=gbrp16le,split[under{i}][b{i}]",
        f"[b{i}][rgb{i}]blend=all_expr='{kernels[shot.blend_mode]}'[mixed{i}]",
        f"[under{i}][mixed{i}][mask{i}]maskedmerge[base{i}]",
    ]


def render(project: Project, preview: bool = False) -> dict:
    diagnostics = validate(project)
    shots = sorted(load(project), key=lambda s: (s.layer, s.row))
    frames = total_frames(project, shots)
    w, h = dimensions(project, preview)
    settings = project.config["project"]
    background = settings.get("background", "black")
    linear = settings.get("blend_color_space", "encoded") == "linear"
    # A filter option, not an arbitrary filter graph.
    import re

    if not re.fullmatch(r"[A-Za-z]+|0x[0-9a-fA-F]{6}", background):
        raise WorkbenchError("background must be a color name or 0xRRGGBB")
    tool = version()
    hashes = {
        s.asset: s.shape.identity() if s.shape else digest(project.media(s.asset))
        for s in shots
    }
    prepared = [
        prepare(project, s, preview, identity=hashes[s.asset], tool_version=tool)
        for s in shots
    ]
    duration = seconds(frames, project.fps)
    inputs = [
        "-f",
        "lavfi",
        "-i",
        f"color=c={background}:s={w}x{h}:r={project.fps}:d={duration}",
    ]
    for path, _ in prepared:
        inputs += ["-i", str(path)]
    audio = settings.get("audio_master")
    if audio:
        inputs += ["-i", str(project.media(audio))]
    graph = [f"[0:v]format=rgba,settb=expr=1/({project.fps}),setpts=N[base0]"]
    if linear:
        graph[0] = (
            f"[0:v]format=rgba,format=gbrp16le,{transfer_filter()},"
            f"settb=expr=1/({project.fps}),setpts=N[base0]"
        )
    for i, shot in enumerate(shots, 1):
        if linear or shot.blend_mode != "normal":
            graph.extend(blend_filters(shot, i, frames, project.fps, linear))
            continue
        graph.append(f"[{i}:v]settb=expr=1/({project.fps}),setpts=N+{shot.at}[shot{i}]")
        graph.append(
            f"[base{i - 1}][shot{i}]overlay=eof_action=pass:repeatlast=0:format=rgb:enable='gte(n,{shot.at})*lt(n,{shot.end})'[base{i}]"
        )
    encode = transfer_filter(encode=True) + "," if linear else ""
    tags = (
        ",setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        if linear or any(s.blend_mode != "normal" for s in shots)
        else ""
    )
    graph.append(
        f"[base{len(shots)}]{encode}scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setsar=1{tags}[v]"
    )
    if audio:
        graph.append(
            f"[{len(shots) + 1}:a:0]aresample=48000,apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a]"
        )
    encoding = project.config.get("encoding", {})
    options = project.config.get("preview", {}) if preview else encoding
    codec = encoding.get("codec", "libx264")
    if codec not in ("libx264", "libx265"):
        raise WorkbenchError("encoding.codec must be libx264 or libx265")
    args = [
        *inputs,
        "-filter_complex",
        ";".join(graph),
        "-map",
        "[v]",
        "-frames:v",
        str(frames),
        "-r",
        str(project.fps),
        "-c:v",
        codec,
        "-crf",
        str(options.get("crf", 28 if preview else 18)),
        "-preset",
        options.get("preset", "ultrafast" if preview else "medium"),
        "-pix_fmt",
        "yuv420p",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-color_range",
        "tv",
        "-threads",
        "2",
        "-movflags",
        "+faststart",
    ]
    if audio:
        args += [
            "-map",
            "[a]",
            "-c:a",
            "aac",
            "-b:a",
            encoding.get("audio_bitrate", "192k"),
        ]
    else:
        args += ["-an"]
    destination = project.generated(
        "output/preview.mp4" if preview else "output/final.mp4"
    )
    export(args, destination)
    manifest = {
        "output": str(destination.relative_to(project.root)),
        "preview": preview,
        "duration_frames": frames,
        "fps": str(project.fps),
        "width": w,
        "height": h,
        "tool": tool,
        "sources": hashes,
        "geometry_masks": {str(s.row): s.mask.identity() for s in shots if s.mask},
        "animations": {
            str(s.row): {
                "layer": s.animation.manifest() if s.animation else None,
                "mask": s.mask_animation.manifest() if s.mask_animation else None,
            }
            for s in shots
            if s.animation or s.mask_animation
        },
        "blend_color_space": settings.get("blend_color_space", "encoded"),
        "audio_sha256": digest(project.media(audio)) if audio else None,
        "project_sha256": digest(project.path("project.toml")),
        "timeline_sha256": digest(project.path("timeline.csv")),
        "prepared": [str(p.relative_to(project.root)) for p, _ in prepared],
        "cache_hits": sum(hit for _, hit in prepared),
        "warnings": diagnostics["warnings"],
    }
    manifest["edit_id"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in manifest.items() if k not in ("cache_hits", "warnings")},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    write_json(destination.with_suffix(".json"), manifest)
    return manifest
