"""Decode outputs and report measurable problems without judging editorial intent.

Render manifests provide expected preview/final geometry, FPS, frame count, color
metadata and audio presence. Changed project/timeline/marker hashes mark renders
stale. Without a manifest, compare current project settings and the caller's
preview flag. Detection also reports near-black frames, silence below -50 dBFS
for at least 250 ms, and high audio peaks as warnings, not editorial failures.
Review contact sheets sample the actual encoded output, not source thumbnails.
"""

import json
import re
import subprocess
from fractions import Fraction

from .contact import contact_sheet
from .ffmpeg import duration, probe
from .project import Project, WorkbenchError, write_json


def inspect(
    project: Project, media: str, *, preview: bool = False, sheet: bool = True
) -> dict:
    path = project.media(media)
    metadata = probe(path)
    video = next((s for s in metadata["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in metadata["streams"] if s["codec_type"] == "audio"), None)
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(path)]
    if video:
        args += [
            "-map",
            "0:v:0",
            "-vf",
            "blackdetect=d=0.04:pix_th=0.02",
            "-fps_mode",
            "passthrough",
        ]
    if audio:
        args += [
            "-map",
            "0:a:0",
            "-af",
            "silencedetect=noise=-50dB:d=0.25,astats=metadata=0:reset=0",
        ]
    args += ["-f", "null", "-"]
    try:
        process = subprocess.run(args, capture_output=True)
    except FileNotFoundError as exc:
        raise WorkbenchError("ffmpeg is missing; enter `nix develop`") from exc
    log = process.stderr.decode(errors="replace")
    if process.returncode:
        raise WorkbenchError("Output decoding failed:\n" + log[-4000:])
    black = [
        {"start": float(a), "end": float(b), "duration": float(c)}
        for a, b, c in re.findall(
            r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)", log
        )
    ]
    silences = []
    starts = re.findall(r"silence_start: ([\d.]+)", log)
    ends = re.findall(r"silence_end: ([\d.]+)", log)
    for i, start in enumerate(starts):
        silences.append(
            {
                "start": float(start),
                "end": float(ends[i]) if i < len(ends) else duration(metadata),
            }
        )
    peaks = [float(x) for x in re.findall(r"Peak level dB: (-?[\d.]+)", log)]
    warnings = []
    errors = []
    if black:
        warnings.append("Black frames detected; review whether these are intentional")
    if silences:
        warnings.append("Silence detected; review soundtrack and intended gaps")
    if peaks and max(peaks) >= -0.1:
        warnings.append("Audio approaches full scale; possible clipping")
    manifest_path = path.with_suffix(".json")
    manifest = None
    if (
        path
        in (
            project.generated("output/final.mp4"),
            project.generated("output/preview.mp4"),
        )
        and manifest_path.exists()
    ):
        try:
            manifest = json.loads(manifest_path.read_text())
            for key in ("width", "height", "duration_frames"):
                if type(manifest[key]) is not int or manifest[key] <= 0:
                    raise ValueError(f"{key} must be a positive integer")
            if Fraction(manifest["fps"]) <= 0:
                raise ValueError("fps must be positive")
        except (ValueError, OSError, KeyError, TypeError, ZeroDivisionError) as exc:
            raise WorkbenchError(f"Cannot read render manifest: {exc}") from exc
    if manifest:
        from .ffmpeg import digest

        files = [("project", "toml"), ("timeline", "csv")]
        if "markers_sha256" in manifest:
            files.append(("markers", "csv"))
        for name, suffix in files:
            if digest(project.path(f"{name}.{suffix}")) != manifest.get(
                f"{name}_sha256"
            ):
                warnings.append(f"{name}.{suffix} has changed since this render")
    if video:
        from .normalize import dimensions

        expected_size = (
            (manifest["width"], manifest["height"])
            if manifest
            else dimensions(project, preview)
        )
        if (video["width"], video["height"]) != expected_size:
            errors.append(
                f"Unexpected dimensions: {video['width']}x{video['height']}; expected {expected_size}"
            )
        if video.get("pix_fmt") != "yuv420p":
            errors.append(f"Unexpected pixel format: {video.get('pix_fmt')}")
        expected_fps = Fraction(manifest["fps"]) if manifest else project.fps
        if Fraction(video.get("avg_frame_rate", "0")) != expected_fps:
            errors.append("Unexpected frame rate")
        if manifest:
            expected_duration = manifest["duration_frames"] / expected_fps
            if abs(duration(metadata) - float(expected_duration)) > max(
                0.06, float(1 / expected_fps)
            ):
                errors.append("Output duration differs from declared timeline duration")
            if (
                video.get("nb_frames")
                and int(video["nb_frames"]) != manifest["duration_frames"]
            ):
                errors.append(
                    "Output frame count differs from declared timeline frame count"
                )
        if video.get("color_space") != "bt709" or video.get("color_range") != "tv":
            errors.append("Unexpected color metadata; expected limited-range BT.709")
    else:
        errors.append("Output contains no video")
    if project.config["project"].get("audio_master") and not audio:
        errors.append("Configured soundtrack is missing from output")
    result = {
        "media": str(path),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "duration_seconds": duration(metadata),
        "black_intervals": black,
        "silence_intervals": silences,
        "peak_dbfs": max(peaks) if peaks else None,
        "video": video,
        "audio": audio,
    }
    if sheet and video:
        result.update(contact_sheet(project, media))
    # Diagnostics are disposable artifacts; inspecting an input never edits it.
    from .ffmpeg import digest

    report = project.generated(f"output/inspection-{digest(path)[:16]}.json")
    write_json(report, result)
    result["report"] = str(report)
    return result
