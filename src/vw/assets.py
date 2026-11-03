"""Inventory media without changing originals or discarding editor annotations."""

import json
import shutil
from pathlib import Path

from .ffmpeg import digest, duration, probe
from .project import ASSET_FIELDS, Project, WorkbenchError, write_csv

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".m4v",
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".m4a",
    ".aac",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def ingest(project: Project, source: str | Path) -> dict:
    source = Path(source)
    source = (
        (project.root / source).resolve()
        if not source.is_absolute()
        else source.resolve()
    )
    if not source.exists():
        raise WorkbenchError(f"Import path does not exist: {source}")
    paths = (
        sorted(
            p
            for p in source.rglob("*")
            if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
        )
        if source.is_dir()
        else [source]
    )
    paths = [
        p.resolve()
        for p in paths
        if not any(
            p.resolve().is_relative_to(project.root / directory)
            for directory in ("cache", "output")
        )
    ]
    assets = project.assets()
    by_path = {row["path"]: key for key, row in assets.items()}
    imported = []
    for original in paths:
        metadata = probe(original)
        streams = metadata.get("streams", [])
        video = next(
            (
                s
                for s in streams
                if s["codec_type"] == "video"
                and not s.get("disposition", {}).get("attached_pic")
            ),
            {},
        )
        audio = next((s for s in streams if s["codec_type"] == "audio"), {})
        if not video and not audio:
            raise WorkbenchError(f"No usable media streams: {original}")
        sha = digest(original)
        target = original
        provenance = (
            original.relative_to(project.root).as_posix()
            if original.is_relative_to(project.root)
            else original.as_uri()
        )
        if not original.is_relative_to(project.root):
            target = project.path(
                f"sources/{original.stem}-{sha[:12]}{original.suffix.lower()}"
            )
            if target.exists() and digest(target) != sha:
                raise WorkbenchError(
                    f"Import destination has different content: {target}"
                )
            if not target.exists():
                shutil.copy2(original, target)
        relative = target.relative_to(project.root).as_posix()
        key = by_path.get(relative)
        if key is None:
            base = target.stem
            key = base
            number = 2
            while key in assets:
                key = f"{base}-{number}"
                number += 1
        row = dict.fromkeys(ASSET_FIELDS, "") | assets.get(key, {})
        row.update(
            {
                "asset": key,
                "path": relative,
                "sha256": sha,
                "provenance": row.get("provenance") or provenance,
                "duration_ms": round(duration(metadata) * 1000),
                "width": video.get("width", ""),
                "height": video.get("height", ""),
                "fps": video.get("avg_frame_rate", ""),
                "video_codec": video.get("codec_name", ""),
                "audio_codec": audio.get("codec_name", ""),
                "pixel_format": video.get("pix_fmt", ""),
                "color_space": video.get("color_space", ""),
                "color_transfer": video.get("color_transfer", ""),
                "color_primaries": video.get("color_primaries", ""),
                "color_range": video.get("color_range", ""),
                "sample_rate": audio.get("sample_rate", ""),
                "channels": audio.get("channels", ""),
                "streams": json.dumps(streams, separators=(",", ":")),
            }
        )
        assets[key] = row
        by_path[relative] = key
        imported.append(key)
    # Preserve custom annotation columns as well as the standard schema.
    fields = list(
        dict.fromkeys(ASSET_FIELDS + [k for row in assets.values() for k in row])
    )
    write_csv(project.path("assets.csv"), fields, list(assets.values()))
    return {"imported": imported, "asset_count": len(assets)}
