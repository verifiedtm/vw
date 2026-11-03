"""Project files and shared, safe filesystem operations."""

import csv
import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .presentation import upscale_limit


class WorkbenchError(Exception):
    """An actionable project or media error."""


ASSET_FIELDS = [
    "asset",
    "path",
    "description",
    "provenance",
    "sha256",
    "duration_ms",
    "width",
    "height",
    "fps",
    "video_codec",
    "audio_codec",
    "pixel_format",
    "color_space",
    "color_transfer",
    "color_primaries",
    "color_range",
    "sample_rate",
    "channels",
    "streams",
]
TIMELINE_FIELDS = [
    "asset",
    "src_in",
    "src_out",
    "at",
    "layer",
    "fit",
    "max_upscale",
    "speed",
    "effect",
    "crop",
    "rotation",
    "transition",
    "transition_frames",
    "opacity",
    "src_in_frame",
    "src_out_frame",
    "at_frame",
    "nframes",
    "mask",
    "mask_invert",
    "blend_mode",
    "animation",
    "mask_animation",
]
MARKER_FIELDS = ["time_ms", "kind", "label", "origin", "at_frame"]
DEFAULT_CONFIG = """# Timeline times accept explicit frame indices or legacy integer milliseconds.
[project]
width = 1920
height = 1080
fps = "30"
# An asset ID or project-relative path. Source audio is otherwise muted.
audio_master = ""
# 0 means end at the last timeline segment.
duration_frames = 0
background = "black"
# Blend in encoded RGB (compatible default) or linear-light BT.709 RGB.
blend_color_space = "encoded"
# Optional presentation scale cap; omitted means unlimited.
# max_upscale = 1.0

[encoding]
codec = "libx264"
pixel_format = "yuv420p"
crf = 18
preset = "medium"
audio_bitrate = "192k"

[preview]
scale = 0.5
crf = 28
preset = "ultrafast"

# Optional named treatments, for example:
# [effects.mono]
# saturation = 0.0
# contrast = 1.0
# brightness = 0.0
"""


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, indent=2) + "\n")


def read_csv(path: Path, required: list[str]) -> list[dict[str, str]]:
    try:
        with path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not set(required) <= set(reader.fieldnames):
                raise WorkbenchError(f"{path}: required columns: {', '.join(required)}")
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise WorkbenchError(f"{path}: duplicate CSV columns")
            rows = list(reader)
            if any(None in row or any(v is None for v in row.values()) for row in rows):
                raise WorkbenchError(f"{path}: malformed CSV row; check column counts")
            return rows
    except (OSError, UnicodeError, csv.Error) as exc:
        raise WorkbenchError(f"Cannot read {path}: {exc}") from exc


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, output.getvalue())


@dataclass
class Project:
    root: Path
    config: dict

    @classmethod
    def load(cls, root: str | Path = ".") -> "Project":
        root = Path(root).resolve()
        try:
            with (root / "project.toml").open("rb") as stream:
                config = tomllib.load(stream)
            for name in ("project", "encoding", "preview", "effects"):
                if name in config and not isinstance(config[name], dict):
                    raise ValueError(f"{name} must be a TOML table")
            project = cls(root, config)
            settings = config["project"]
            upscale_limit(settings.get("max_upscale"))
            if settings.get("blend_color_space", "encoded") not in (
                "encoded",
                "linear",
            ):
                raise ValueError("blend_color_space must be encoded or linear")
            for key in ("width", "height"):
                value = settings[key]
                if type(value) is not int or value < 2 or value % 2:
                    raise ValueError(f"{key} must be a positive even integer")
            if project.fps <= 0 or project.fps > 240:
                raise ValueError("fps must be greater than 0 and at most 240")
            duration = settings.get("duration_frames", 0)
            if type(duration) is not int or duration < 0:
                raise ValueError("duration_frames must be a nonnegative integer")
            if config.get("encoding", {}).get("pixel_format", "yuv420p") != "yuv420p":
                raise ValueError("the initial color pipeline requires yuv420p output")
            scale = config.get("preview", {}).get("scale", 0.5)
            if not 0 < scale <= 1:
                raise ValueError("preview.scale must be greater than 0 and at most 1")
            for section in ("encoding", "preview"):
                options = config.get(section, {})
                crf = options.get("crf", 18)
                if type(crf) is not int or not 0 <= crf <= 51:
                    raise ValueError(
                        f"{section}.crf must be an integer between 0 and 51"
                    )
                if options.get("preset", "medium") not in (
                    "ultrafast",
                    "superfast",
                    "veryfast",
                    "faster",
                    "fast",
                    "medium",
                    "slow",
                    "slower",
                    "veryslow",
                ):
                    raise ValueError(
                        f"{section}.preset is not a supported encoding preset"
                    )
            if config.get("encoding", {}).get("codec", "libx264") not in (
                "libx264",
                "libx265",
            ):
                raise ValueError("encoding.codec must be libx264 or libx265")
            import re

            if not re.fullmatch(
                r"[1-9][0-9]*k", config.get("encoding", {}).get("audio_bitrate", "192k")
            ):
                raise ValueError(
                    "audio_bitrate must be a positive bitrate such as 192k"
                )
            if not isinstance(settings.get("audio_master", ""), str):
                raise ValueError("audio_master must be an asset ID or relative path")
            from .geometry import load_shapes

            load_shapes(config)
            return project
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            raise WorkbenchError(f"Invalid {root / 'project.toml'}: {exc}") from exc

    @property
    def fps(self) -> Fraction:
        return Fraction(str(self.config["project"]["fps"]))

    def path(self, relative: str | Path) -> Path:
        relative = Path(relative)
        result = (self.root / relative).resolve()
        if relative.is_absolute() or not result.is_relative_to(self.root):
            raise WorkbenchError(f"Path must stay relative to project root: {relative}")
        return result

    def generated(self, relative: str | Path) -> Path:
        relative = Path(relative)
        if not relative.parts or relative.parts[0] not in ("cache", "output"):
            raise WorkbenchError(
                f"Generated artifacts must live in cache/ or output/: {relative}"
            )
        path = self.path(relative)
        if path != self.root / relative:
            raise WorkbenchError(
                f"Generated artifact path must not follow symlinks: {relative}"
            )
        return path

    def assets(self) -> dict[str, dict[str, str]]:
        rows = read_csv(self.path("assets.csv"), ["asset", "path"])
        result = {}
        for row in rows:
            if row["asset"].startswith("shape:"):
                raise WorkbenchError(
                    "assets.csv: shape: is reserved for generated sources"
                )
            if not row["asset"] or row["asset"] in result:
                raise WorkbenchError("assets.csv: empty or duplicate asset ID")
            self.path(row["path"])
            result[row["asset"]] = row
        return result

    def media(self, value: str) -> Path:
        assets = self.assets()
        path = self.path(assets[value]["path"] if value in assets else value)
        if not path.is_file():
            raise WorkbenchError(f"Missing media: {path}")
        return path


def initialize(root: str | Path) -> dict:
    root = Path(root).resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise WorkbenchError(f"{root} is not empty; choose an empty project directory")
    root.mkdir(parents=True, exist_ok=True)
    for name in (
        "sources",
        "cache/normalized",
        "cache/thumbnails",
        "cache/waveforms",
        "output",
    ):
        (root / name).mkdir(parents=True)
    atomic_text(root / "project.toml", DEFAULT_CONFIG)
    for name, fields in [
        ("assets", ASSET_FIELDS),
        ("timeline", TIMELINE_FIELDS),
        ("markers", MARKER_FIELDS),
    ]:
        write_csv(root / f"{name}.csv", fields, [])
    return {"project": str(root)}
