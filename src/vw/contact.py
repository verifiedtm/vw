"""Review thumbnails sampled from source footage or actual rendered output."""

from fractions import Fraction
from io import BytesIO

from PIL import Image, ImageDraw, ImageOps

from .ffmpeg import export, probe, run, stream_duration
from .project import Project, WorkbenchError


def contact_sheet(project: Project, asset: str, count: int = 12) -> dict:
    if not 1 <= count <= 200:
        raise WorkbenchError("Contact sheet count must be between 1 and 200")
    path = project.media(asset)
    info = probe(path)
    if not any(s["codec_type"] == "video" for s in info["streams"]):
        raise WorkbenchError(f"{asset} has no video stream")
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    length = stream_duration(video, info)
    rate = Fraction(video.get("avg_frame_rate", "0/1"))
    frame_count = int(video.get("nb_frames") or max(1, int(length * rate)))
    columns = min(count, 4)
    rows = (count + columns - 1) // columns
    sheet = Image.new("RGB", (columns * 330, rows * 212 + 35), "#171c26")
    draw = ImageDraw.Draw(sheet)
    draw.text((10, 10), path.name, fill="white")
    timestamps = []
    for i in range(count):
        # Seek to an existing frame rather than into the tail after its PTS.
        index = min(frame_count - 1, (2 * i + 1) * frame_count // (2 * count))
        timestamp = float(index / rate) if rate else 0.0
        timestamps.append(round(timestamp * 1000))
        data = run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-ss",
                f"{timestamp:.9f}",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=320:180:force_original_aspect_ratio=decrease",
                "-threads",
                "1",
                "-f",
                "image2pipe",
                "-c:v",
                "png",
                "-",
            ],
            binary=True,
        )
        if not data:
            raise WorkbenchError(f"No frame at {timestamp:.3f}s in {path}")
        image = Image.open(BytesIO(data)).convert("RGB")
        image = ImageOps.pad(image, (320, 180), color="black")
        x, y = i % columns * 330 + 5, i // columns * 212 + 35
        sheet.paste(image, (x, y))
        draw.text((x + 4, y + 185), f"{timestamp:.3f}s", fill="white")
    from .ffmpeg import digest

    destination = project.generated(f"cache/thumbnails/{digest(path)[:16]}-{count}.jpg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=90)
    return {"contact_sheet": str(destination), "timestamps_ms": timestamps}


def extract_frame(project: Project, asset: str, time_ms: int) -> dict:
    if time_ms < 0:
        raise WorkbenchError("time_ms must be nonnegative")
    path = project.media(asset)
    from .ffmpeg import digest

    destination = project.generated(
        f"cache/thumbnails/{digest(path)[:16]}-{time_ms}.png"
    )
    export(
        [
            "-ss",
            f"{time_ms / 1000:.9f}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-threads",
            "1",
        ],
        destination,
    )
    if not destination.exists() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise WorkbenchError("No frame at requested time; check source duration")
    return {"frame": str(destination)}
