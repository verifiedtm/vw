"""Waveform and conservative onset suggestions; manual markers remain authoritative."""

import math
import subprocess
import sys
from array import array
from fractions import Fraction
from statistics import median, pstdev

from PIL import Image, ImageDraw

from .ffmpeg import digest
from .markers import marker_seconds, named_markers
from .project import (
    MARKER_FIELDS,
    Project,
    WorkbenchError,
    read_csv,
    write_csv,
    write_json,
)

RATE = 16000
HOP = 320


def envelope(path):
    """Stream PCM in bounded chunks, retaining only 50 analysis windows/second."""
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(RATE),
        "-f",
        "f32le",
        "-",
    ]
    import tempfile

    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        except FileNotFoundError as exc:
            raise WorkbenchError("ffmpeg is missing; enter `nix develop`") from exc
        rms, peaks = [], []
        try:
            while data := process.stdout.read(HOP * 4):
                samples = array("f")
                samples.frombytes(data)
                if sys.byteorder != "little":
                    samples.byteswap()
                rms.append(math.sqrt(math.fsum(x * x for x in samples) / len(samples)))
                peaks.append(max(abs(x) for x in samples))
            process.wait()
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        if process.returncode:
            errors.seek(0)
            raise WorkbenchError(
                "Audio decoding failed: "
                + errors.read().decode(errors="replace")[-2000:]
            )
    if not rms:
        raise WorkbenchError(f"No audio samples in {path}")
    return rms, peaks


def analyze(project: Project, audio: str | None = None) -> dict:
    audio = audio or project.config["project"].get("audio_master")
    if not audio:
        raise WorkbenchError("Set project.audio_master or pass an audio asset/path")
    path = project.media(audio)
    rms, peaks = envelope(path)
    length_ms = len(rms) * HOP * 1000 // RATE
    markers = read_csv(project.path("markers.csv"), ["kind", "label", "origin"])
    for row in markers:
        if marker_seconds(row, project) > Fraction(length_ms, 1000):
            raise WorkbenchError(f"Invalid marker {row}: timestamp outside soundtrack")
    key = digest(path)[:16]
    folder = project.generated("cache/waveforms")
    folder.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1600, 340), "#151b25")
    draw = ImageDraw.Draw(image)
    for x in range(1560):
        chunk = peaks[
            x * len(peaks) // 1560 : max(
                x * len(peaks) // 1560 + 1, (x + 1) * len(peaks) // 1560
            )
        ]
        if chunk:
            amplitude = min(1, max(chunk))
            draw.line(
                (20 + x, 170 - amplitude * 120, 20 + x, 170 + amplitude * 120),
                fill="#64b5e8",
            )
    draw.text(
        (20, 12),
        f"{path.name} | {length_ms / 1000:.2f} seconds | linear amplitude",
        fill="white",
    )
    for i in range(11):
        x = 20 + i * 156
        draw.text((min(x, 1510), 310), f"{length_ms * i / 10000:.2f}s", fill="white")
    waveform = folder / f"{key}.png"
    for row in markers:
        x = 20 + marker_seconds(row, project) * 1000 * 1560 / max(length_ms, 1)
        draw.line((x, 40, x, 295), fill="#ffd166", width=2)
        draw.text((min(x + 3, 1400), 35), row["label"][:24], fill="#ffd166")
    image.save(waveform)
    # Positive RMS jumps above a local baseline are onset suggestions, not a tempo grid.
    flux = [0.0] + [max(0, b - a) for a, b in zip(rms[:-1], rms[1:], strict=True)]
    events = []
    last = -1000
    for i in range(1, len(rms) - 1):
        baseline = flux[max(0, i - 25) : i + 26]
        threshold = max(0.015, median(baseline) + 2 * pstdev(baseline))
        time_ms = i * HOP * 1000 // RATE
        if (
            flux[i] > threshold
            and flux[i] >= flux[i - 1]
            and flux[i] >= flux[i + 1]
            and time_ms - last >= 180
        ):
            events.append(
                {
                    "time_ms": time_ms,
                    "kind": "onset",
                    "label": "Suggested cut",
                    "origin": "automatic",
                }
            )
            last = time_ms
    quiet_start = None
    for i, value in enumerate([*rms, 1.0]):
        if value < 0.01 and quiet_start is None:
            quiet_start = i
        elif value >= 0.01 and quiet_start is not None:
            if (i - quiet_start) * 20 >= 500:
                events.extend(
                    [
                        {
                            "time_ms": quiet_start * 20,
                            "kind": "quiet_start",
                            "label": "Quiet passage",
                            "origin": "automatic",
                        },
                        {
                            "time_ms": i * 20,
                            "kind": "quiet_end",
                            "label": "End quiet passage",
                            "origin": "automatic",
                        },
                    ]
                )
            quiet_start = None
    # Compare adjacent half-second windows for broad intensity changes.
    for i in range(25, len(rms) - 25, 25):
        before = math.fsum(rms[i - 25 : i]) / 25
        after = math.fsum(rms[i : i + 25]) / 25
        change_db = 20 * math.log10(max(after, 1e-6) / max(before, 1e-6))
        if abs(change_db) >= 6 and max(before, after) >= 0.02:
            events.append(
                {
                    "time_ms": i * 20,
                    "kind": "intensity",
                    "label": f"Energy change {change_db:+.1f} dB",
                    "origin": "automatic",
                }
            )
    events.sort(key=lambda row: row["time_ms"])
    # Keep suggestions separate. Never overwrite an editor's marker file.
    suggestions = folder / f"{key}-suggestions.csv"
    write_csv(suggestions, MARKER_FIELDS, events)
    windows = [
        {"time_ms": i * 20, "rms": round(float(r), 6), "peak": round(float(p), 6)}
        for i, (r, p) in enumerate(zip(rms, peaks, strict=True))
    ]
    data = folder / f"{key}.json"
    write_json(
        data, {"source_sha256": digest(path), "window_ms": 20, "windows": windows}
    )
    return {
        "waveform": str(waveform),
        "analysis": str(data),
        "suggestions": str(suggestions),
        "suggestion_count": len(events),
        "manual_markers": len(markers),
        "duration_ms": length_ms,
        "peak_dbfs": round(20 * math.log10(max(max(peaks), 1e-12)), 2),
    }


def add_marker(
    project: Project,
    time_ms: int | None,
    kind: str,
    label: str,
    *,
    at_frame: int | None = None,
    id: str = "",
) -> dict:
    if (time_ms is None) == (at_frame is None):
        raise WorkbenchError("Specify exactly one of time_ms or at_frame")
    if (time_ms if time_ms is not None else at_frame) < 0:
        raise WorkbenchError("Marker time must be nonnegative")
    path = project.path("markers.csv")
    rows = read_csv(path, ["kind", "label", "origin"])
    fields = list(dict.fromkeys(MARKER_FIELDS + [k for row in rows for k in row]))
    row = dict.fromkeys(fields, "") | {
        "time_ms": time_ms if time_ms is not None else "",
        "at_frame": at_frame if at_frame is not None else "",
        "kind": kind,
        "label": label,
        "origin": "manual",
        "id": id,
    }
    rows.append(row)
    named_markers(project, rows)
    rows.sort(key=lambda r: marker_seconds(r, project))
    write_csv(path, fields, rows)
    return row
