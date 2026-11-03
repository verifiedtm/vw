"""Small, shell-free ffmpeg boundary shared by the public modules."""

import hashlib
import json
import os
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from .project import WorkbenchError


def run(args: list[str], *, binary: bool = False):
    try:
        result = subprocess.run(
            [str(arg) for arg in args],
            capture_output=True,
            check=True,
        )
        return result.stdout if binary else result.stdout.decode()
    except FileNotFoundError as exc:
        raise WorkbenchError(
            f"{args[0]} is missing; enter `nix develop` or install ffmpeg"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace")[-6000:]
        raise WorkbenchError(f"{args[0]} failed:\n{detail}") from exc


def ffmpeg(args: list[str]) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            *args,
        ]
    )


def probe(path: Path) -> dict:
    return json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def version() -> str:
    return run(["ffmpeg", "-version"]).splitlines()[0]


def temporary_output(destination: Path):
    """Return a sibling temporary filename, preserving the container suffix."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".vw-", suffix=destination.suffix, dir=destination.parent
    )
    os.close(fd)
    return Path(name)


def export(args: list[str], destination: Path) -> None:
    temp = temporary_output(destination)
    try:
        ffmpeg([*args, str(temp)])
        if not temp.exists() or temp.stat().st_size == 0:
            raise WorkbenchError(
                "ffmpeg produced no output; check requested time and source duration"
            )
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def duration(metadata: dict) -> float:
    values = [metadata.get("format", {}).get("duration")]
    values += [s.get("duration") for s in metadata.get("streams", [])]
    return max(
        (float(value) for value in values if value not in (None, "N/A")), default=0.0
    )


def stream_duration(stream: dict, metadata: dict) -> float:
    """Prefer the selected stream's end over a longer companion audio stream."""
    if stream.get("duration") not in (None, "N/A"):
        return float(stream["duration"])
    if stream.get("duration_ts") is not None and stream.get("time_base"):
        return float(int(stream["duration_ts"]) * Fraction(stream["time_base"]))
    tag = stream.get("tags", {}).get("DURATION")
    if tag:
        hours, minutes, seconds = tag.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if stream.get("nb_frames") and stream.get("avg_frame_rate") not in (
        None,
        "0/0",
        "0/1",
    ):
        return float(int(stream["nb_frames"]) / Fraction(stream["avg_frame_rate"]))
    return duration(metadata)
