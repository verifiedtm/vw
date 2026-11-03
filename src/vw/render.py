"""Compose prepared shots; editorial choices live exclusively in project files."""

import hashlib
import json

from .ffmpeg import digest, export, version
from .normalize import dimensions, prepare
from .project import Project, WorkbenchError, write_json
from .timeline import load, seconds, total_frames, validate


def render(project: Project, preview: bool = False) -> dict:
    diagnostics = validate(project)
    shots = sorted(load(project), key=lambda s: (s.layer, s.row))
    frames = total_frames(project, shots)
    w, h = dimensions(project, preview)
    settings = project.config["project"]
    background = settings.get("background", "black")
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
    for i, shot in enumerate(shots, 1):
        graph.append(f"[{i}:v]settb=expr=1/({project.fps}),setpts=N+{shot.at}[shot{i}]")
        graph.append(
            f"[base{i - 1}][shot{i}]overlay=eof_action=pass:repeatlast=0:format=rgb:enable='gte(n,{shot.at})*lt(n,{shot.end})'[base{i}]"
        )
    graph.append(
        f"[base{len(shots)}]scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setsar=1[v]"
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
