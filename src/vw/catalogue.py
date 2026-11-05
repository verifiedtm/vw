"""Manual, project-independent clip libraries and exact timeline references.

Version 1 manifests contain a clips array. load checks schema and unique stable
IDs without opening media; validate aggregates per-record schema and media errors.
add preserves unknown manifest/record fields. source_path anchors relative paths
to the resolved manifest directory, independent of working directory or project.

_range converts half-open integer ms or frame intervals to decoded frame indices.
Milliseconds start at the first decoded video PTS; bisect_left selects frame
starts in [in,out). Duration and EOF checks use exact presentation boundaries,
never avg_frame_rate. Preview and import share this selection.

Previews use the existing frame-index cache and exact frame-number extraction.
Their atomic JPEG cache key includes source identity, selected records, range,
size, tool versions and preview format version. Both caches live beside the
manifest under cache/ and may be discarded.

import_clips validates selections before rewriting project CSVs, appends in caller
order, and rounds each decoded duration to project frames once. Same-path assets
are reused; new external media opts into Project.asset_path reference resolution.
The two CSV writes roll back on ordinary write failures; they are not a multi-file
crash transaction. Catalogue provenance columns are annotations, so rendering
requires only the source media, not the original manifest. See USAGE.md for
manifest examples and the CLI workflow.
"""

import csv
import hashlib
import json
import os
import re
import tempfile
from bisect import bisect_left
from fractions import Fraction
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from . import frame_index
from .assets import IMAGE_EXTENSIONS
from .ffmpeg import run
from .project import (
    ASSET_FIELDS,
    TIMELINE_FIELDS,
    Project,
    WorkbenchError,
    atomic_text,
    read_csv,
    write_csv,
    write_json,
)

TEXT_FIELDS = ("notes", "action", "camera_movement", "framing", "entry", "exit")
MOTIONS = ("left", "right", "up", "down", "toward", "away", "static", "mixed")


def _document(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError) as exc:
        raise WorkbenchError(f"Cannot read catalogue {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or not isinstance(data.get("clips"), list)
    ):
        raise WorkbenchError(f"{path}: require version = 1 and a clips array")
    return data


def _record(clip: dict) -> None:
    if not isinstance(clip, dict):
        raise WorkbenchError("clip must be an object")
    if not isinstance(clip.get("id"), str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", clip["id"]
    ):
        raise WorkbenchError(
            "id must use letters, digits, underscores, dots or hyphens"
        )
    if not isinstance(clip.get("source"), str) or not clip["source"].strip():
        raise WorkbenchError("source must be a nonempty local filesystem path")
    if "\x00" in clip["source"] or "://" in clip["source"]:
        raise WorkbenchError("source must be a local filesystem path, not a URI")
    if clip.get("time_unit") not in ("ms", "frames"):
        raise WorkbenchError("time_unit must be ms or frames")
    if (
        type(clip.get("in")) is not int
        or type(clip.get("out")) is not int
        or not 0 <= clip["in"] < clip["out"]
    ):
        raise WorkbenchError("require integer 0 <= in < out (exclusive)")
    for field in TEXT_FIELDS:
        if field in clip and not isinstance(clip[field], str):
            raise WorkbenchError(f"{field} must be text")
    if not isinstance(clip.get("tags", []), list) or any(
        not isinstance(tag, str) or not tag.strip() for tag in clip.get("tags", [])
    ):
        raise WorkbenchError("tags must be an array of nonempty strings")
    if clip.get("motion", "") not in ("", *MOTIONS):
        raise WorkbenchError(f"motion must be one of {', '.join(MOTIONS)}")


def load(path: str | Path) -> dict:
    path = Path(path).resolve()
    data = _document(path)
    seen = set()
    for number, clip in enumerate(data["clips"], 1):
        try:
            _record(clip)
            if clip["id"] in seen:
                raise WorkbenchError(f"duplicate id {clip['id']!r}")
            seen.add(clip["id"])
        except WorkbenchError as exc:
            raise WorkbenchError(f"{path}: clip {number}: {exc}") from exc
    return data


def initialize(path: str | Path) -> dict:
    path = Path(path).absolute()
    # Exclusive creation protects an existing manifest, including symlinks.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(json.dumps({"version": 1, "clips": []}, indent=2) + "\n")
    return {"catalogue": str(path)}


def source_path(path: str | Path, clip: dict) -> Path:
    return (Path(path).resolve().parent / clip["source"]).resolve()


def _range(path: Path, clip: dict):
    source = source_path(path, clip)
    if not source.is_file():
        raise WorkbenchError(f"Missing source: {source}")
    info = frame_index.metadata(source)
    video = next(
        (
            s
            for s in info.get("streams", [])
            if s["codec_type"] == "video"
            and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if video is None or source.suffix.lower() in IMAGE_EXTENSIONS:
        raise WorkbenchError(f"Source must be moving video: {source}")
    index = frame_index.load(Project(path.parent, {}), source, video)
    stamps = index.boundaries
    start, end = clip["in"], clip["out"]
    if clip["time_unit"] == "ms":
        # Source-relative milliseconds start at the first decoded video frame.
        if Fraction(end, 1000) > stamps[-1] - stamps[0]:
            raise WorkbenchError("out exceeds source duration")
        start = bisect_left(stamps, stamps[0] + Fraction(start, 1000))
        end = bisect_left(stamps, stamps[0] + Fraction(end, 1000))
    if end >= len(stamps):
        raise WorkbenchError(f"out exceeds source frame count {len(stamps) - 1}")
    if start >= end:
        raise WorkbenchError("interval contains no source frames")
    return source, video, index, start, end


def add(path: str | Path, clip: dict) -> dict:
    path = Path(path).resolve()
    data = load(path)
    _record(clip)
    if any(row["id"] == clip["id"] for row in data["clips"]):
        raise WorkbenchError(f"Duplicate clip id: {clip['id']}")
    _range(path, clip)
    data["clips"].append(clip)
    write_json(path, data)
    return {"added": clip["id"]}


def search(path: str | Path, *, source=None, tags=(), motion=None, query=None) -> dict:
    clips = load(path)["clips"]
    if source is not None:
        target = (Path(path).resolve().parent / source).resolve()
        clips = [c for c in clips if source_path(path, c) == target]
    if motion is not None:
        clips = [c for c in clips if c.get("motion") == motion]
    clips = [c for c in clips if set(tags) <= set(c.get("tags", []))]
    if query:
        clips = [
            c
            for c in clips
            if query.casefold()
            in " ".join(
                [c["id"], *c.get("tags", []), *(c.get(k, "") for k in TEXT_FIELDS)]
            ).casefold()
        ]
    return {"clips": clips, "count": len(clips)}


def select(path: str | Path, ids: list[str]) -> list[dict]:
    clips = load(path)["clips"]
    if not ids:
        return clips
    by_id = {c["id"]: c for c in clips}
    missing = [key for key in ids if key not in by_id]
    if missing:
        raise WorkbenchError(f"Unknown catalogue IDs: {', '.join(missing)}")
    return [by_id[key] for key in ids]


def validate(path: str | Path) -> dict:
    path = Path(path).resolve()
    data = _document(path)
    errors = []
    seen = set()
    for number, clip in enumerate(data["clips"], 1):
        label = clip.get("id", number) if isinstance(clip, dict) else number
        try:
            _record(clip)
            if clip["id"] in seen:
                raise WorkbenchError("duplicate id")
            seen.add(clip["id"])
            _range(path, clip)
        except (WorkbenchError, OSError) as exc:
            errors.append({"clip": label, "row": number, "error": str(exc)})
    return {"valid": not errors, "clip_count": len(data["clips"]), "errors": errors}


def preview(path: str | Path, ids: list[str]) -> dict:
    path = Path(path).resolve()
    clips = select(path, ids)
    if not clips or len(clips) > 200:
        raise WorkbenchError("Select between 1 and 200 clips per preview")
    resolved = [(c, _range(path, c)) for c in clips]
    key = {
        "version": 1,
        "size": [320, 180],
        "tools": frame_index.tools(),
        "clips": [
            (c, index.key, start, end) for c, (_, _, index, start, end) in resolved
        ],
    }
    hashed = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    destination = Project(path.parent, {}).generated(f"cache/catalogue/{hashed}.jpg")
    if destination.is_file():
        return {"contact_sheet": str(destination), "cache_hit": True}
    sheet = Image.new("RGB", (660, len(clips) * 220), "#171c26")
    draw = ImageDraw.Draw(sheet)
    for row, (clip, (source, video, index, start, end)) in enumerate(resolved):
        for column, number in enumerate((start, end - 1)):
            data = run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(source),
                    "-map",
                    f"0:{video['index']}",
                    "-vf",
                    f"select=eq(n\\,{number}),scale=320:180:force_original_aspect_ratio=decrease",
                    "-frames:v",
                    "1",
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
                raise WorkbenchError(f"No frame {number} in {source}")
            image = ImageOps.pad(Image.open(BytesIO(data)).convert("RGB"), (320, 180))
            x, y = column * 330 + 5, row * 220 + 20
            sheet.paste(image, (x, y))
            seconds = index.boundaries[number] - index.boundaries[0]
            draw.text(
                (x, y + 182),
                f"{'entry' if column == 0 else 'exit'}: frame {number}, {float(seconds):.6f}s",
                fill="white",
            )
        draw.text((5, row * 220 + 3), clip["id"], fill="white")
        index.check_source()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Publish atomically so readers never see partial JPEGs.
    output = BytesIO()
    sheet.save(output, format="JPEG", quality=90)
    fd, temporary = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(output.getvalue())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"contact_sheet": str(destination), "cache_hit": False}


def import_clips(
    path: str | Path, ids: list[str], project: Project, *, at_frame: int
) -> dict:
    """Append frame-addressed rows; register read-only external source references."""
    if type(at_frame) is not int or at_frame < 0:
        raise WorkbenchError("at_frame must be a nonnegative integer")
    if not ids:
        raise WorkbenchError("Select at least one clip ID to import")
    path = Path(path).resolve()
    clips = select(path, ids)
    assets = project.assets()
    rows = read_csv(project.path("timeline.csv"), ["asset"])
    imported = []
    for clip in clips:
        source, video, index, start, end = _range(path, clip)
        if source.is_relative_to(project.root / "cache") or source.is_relative_to(
            project.root / "output"
        ):
            raise WorkbenchError(
                "Catalogue sources cannot live in project cache/ or output/"
            )
        if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
            raise WorkbenchError(
                "HDR inputs require explicit SDR conversion before import"
            )
        frames = round((index.boundaries[end] - index.boundaries[start]) * project.fps)
        if frames < 1:
            raise WorkbenchError(f"Clip {clip['id']} is shorter than one output frame")
        asset = next(
            (key for key in assets if project.asset_path(assets[key]) == source), None
        )
        if asset is None:
            sha = index.key["source"]
            base = f"catalogue-{sha[:12]}"
            asset = base
            suffix = 2
            while asset in assets:
                asset = f"{base}-{suffix}"
                suffix += 1
            internal = source.is_relative_to(project.root)
            assets[asset] = dict.fromkeys(ASSET_FIELDS, "") | {
                "asset": asset,
                "path": source.relative_to(project.root).as_posix()
                if internal
                else str(source),
                "source_mode": "" if internal else "reference",
                "sha256": sha,
                "provenance": source.as_uri(),
            }
        rows.append(
            {
                "asset": asset,
                "src_in_frame": start,
                "src_out_frame": end,
                "at_frame": at_frame,
                "nframes": frames,
                "catalogue_id": clip["id"],
                "catalogue_manifest": str(path),
            }
        )
        imported.append(
            {"id": clip["id"], "asset": asset, "at_frame": at_frame, "nframes": frames}
        )
        at_frame += frames
        index.check_source()
    asset_path, timeline_path = project.path("assets.csv"), project.path("timeline.csv")
    with asset_path.open(newline="") as stream:
        original_assets = stream.read()
    with timeline_path.open(newline="") as stream:
        original_timeline = stream.read()
    # Preserve even unused annotation columns in empty CSVs.
    old_asset_fields = next(csv.reader(original_assets.splitlines()))
    old_timeline_fields = next(csv.reader(original_timeline.splitlines()))
    asset_fields = list(
        dict.fromkeys(
            ASSET_FIELDS + old_asset_fields + [k for a in assets.values() for k in a]
        )
    )
    timeline_fields = list(
        dict.fromkeys(
            TIMELINE_FIELDS + old_timeline_fields + [k for row in rows for k in row]
        )
    )
    try:
        write_csv(asset_path, asset_fields, list(assets.values()))
        write_csv(timeline_path, timeline_fields, rows)
    except OSError:
        atomic_text(asset_path, original_assets)
        atomic_text(timeline_path, original_timeline)
        raise
    return {"imported": imported, "end_frame": at_frame}
