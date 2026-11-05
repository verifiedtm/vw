"""Content-addressed presentation indices and independently verified seeking.

Keyframe flags are seek hints, never proof of a decoder random-access point.
Before using a seek, compare every requested decoded frame's timestamp and SHA256
against a full decode. The normalization pass uses identical decoder/seek options.
This deliberately trades a cold full decode and two bounded warm decodes for a
small, codec-independent correctness boundary. No approximate seek is accepted.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from itertools import zip_longest
from pathlib import Path

from . import metrics
from .ffmpeg import digest, frame_boundaries, probe, run
from .project import Project, WorkbenchError, write_json

INDEX_VERSION = 1
# The benchmark shows verification/process overhead dominates short prefixes.
MIN_SEEK_PREFIX = Fraction(30)


def fingerprint(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=1)
def tools():
    return tuple(run([name, "-version"]) for name in ("ffprobe", "ffmpeg"))


@lru_cache(maxsize=32)
def _metadata(path, identity):
    return probe(path)


def metadata(path):
    return _metadata(path, fingerprint(path))


@dataclass
class FrameIndex:
    path: Path
    cache: Path
    identity: tuple
    key: dict
    stream: dict
    stamps: list[int]
    tick: Fraction
    keys: list[int]
    hashes: list | None = None
    hash_failure: str | None = None

    @property
    def boundaries(self):
        return [stamp * self.tick for stamp in self.stamps]

    def save(self):
        write_json(
            self.cache,
            dict(
                key=self.key,
                stamps=self.stamps,
                keys=self.keys,
                hashes=self.hashes,
                hash_failure=self.hash_failure,
            ),
        )

    def reference_hashes(self):
        if self.hash_failure:
            raise WorkbenchError(self.hash_failure)
        if self.hashes is None:
            try:
                rows = self.decode_hashes([])
                if [row[0] for row in rows] != self.stamps[:-1]:
                    frame, expected, actual = next(
                        (frame, expected, actual)
                        for frame, (expected, actual) in enumerate(
                            zip_longest(self.stamps[:-1], (row[0] for row in rows))
                        )
                        if expected != actual
                    )
                    raise WorkbenchError(
                        "hash decoder timestamps differ from frame index: "
                        f"stream={self.stream['index']}, time_base={self.tick}, "
                        f"indexed_frames={len(self.stamps) - 1}, "
                        f"decoded_frames={len(rows)}, first_difference_frame={frame}, "
                        f"indexed_pts={expected}, decoded_pts={actual}"
                    )
                self.hashes = rows
            except WorkbenchError as exc:
                self.hash_failure = str(exc)
                self.save()
                raise
            self.check_source()
            self.save()
        return self.hashes

    def check_source(self):
        if fingerprint(self.path) != self.identity:
            raise WorkbenchError(f"Source changed while indexing/seeking: {self.path}")

    def decode_hashes(self, inputs, trim=None, count=None):
        output = run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "debug" if metrics.enabled() else "error",
                "-nostdin",
                "-copyts",
                "-threads",
                "1",
                *inputs,
                "-i",
                str(self.path),
                "-map",
                f"0:{self.stream['index']}",
                "-an",
                "-sn",
                "-dn",
                *(["-vf", trim] if trim else []),
                *(["-frames:v", str(count)] if count else []),
                "-fps_mode",
                "passthrough",
                "-enc_time_base",
                str(self.tick),
                "-c:v",
                "rawvideo",
                "-threads",
                "1",
                "-f",
                "framehash",
                "-",
            ],
            report="seek_verify_decode" if inputs else "reference_hash_decode",
        )
        try:
            rows = []
            hash_tick = None
            for line in output.splitlines():
                if line.startswith("#tb 0:"):
                    hash_tick = line.split(":", 1)[1].strip()
                if not line or line.startswith("#"):
                    continue
                fields = [field.strip() for field in line.split(",")]
                if len(fields) != 6 or not re.fullmatch(r"[0-9a-f]{64}", fields[5]):
                    raise ValueError("invalid frame hash")
                rows.append([int(fields[2]), int(fields[4]), fields[5]])
            metrics.emit(
                "frame_hash_timing",
                source=str(self.path),
                stream=self.stream["index"],
                source_start_pts=self.stream.get("start_pts"),
                mode="seek" if inputs else "reference",
                index_time_base=str(self.tick),
                hash_time_base=hash_tick,
                hashed_frames=len(rows),
                first_pts=rows[0][0] if rows else None,
                last_pts=rows[-1][0] if rows else None,
            )
            return rows
        except (ValueError, IndexError) as exc:
            raise WorkbenchError(f"Cannot verify decoded frames: {exc}") from exc

    def seek(self, start, end):
        # Keep small/early cuts on the reference path; avoid cold hash work when
        # seeking cannot save decoding. Keyframe flags are only candidate hints;
        # the complete hash comparison below establishes whether they are safe.
        earlier = [n for n in self.keys if n <= start]
        anchor = earlier[-1] if earlier else 0
        preroll = earlier[-2] if len(earlier) > 1 else 0
        if (
            anchor == 0
            or 2 * (end - preroll) >= end
            or self.stamps[anchor] < 0
            or (self.stamps[start] - self.stamps[0]) * self.tick < MIN_SEEK_PREFIX
        ):
            metrics.emit(
                "seek_fallback",
                reason="no predicted decoding savings or source prefix shorter than 30 seconds",
                source=str(self.path),
            )
            return None
        # Round down to microseconds: never accidentally seek beyond the hint.
        micros = self.stamps[anchor] * self.tick * 1_000_000
        position = micros.numerator // micros.denominator
        inputs = [
            "-noaccurate_seek",
            "-seek_timestamp",
            "1",
            "-ss",
            f"{position // 1000000}.{position % 1000000:06d}",
        ]
        trim = f"trim=start_pts={self.stamps[start]}:end_pts={self.stamps[end]}"
        self.check_source()
        try:
            expected = self.reference_hashes()[start:end]
            actual = self.decode_hashes(inputs, trim, end - start)
            self.check_source()
            if actual != expected:
                raise WorkbenchError(
                    "bounded decode differs from full-decode frame hashes/timestamps"
                )
        except WorkbenchError as exc:
            self.check_source()
            metrics.emit("seek_fallback", reason=str(exc), source=str(self.path))
            return None
        metrics.emit(
            "seek_verified",
            source=str(self.path),
            anchor_frame=anchor,
            start_frame=start,
            end_frame=end,
        )
        return ["-copyts", "-threads", "1", *inputs], trim


def load(project: Project, path: Path, stream: dict):
    before = _load.cache_info().hits
    result = _load(
        project.root,
        path,
        fingerprint(path),
        json.dumps(stream, sort_keys=True),
        tools(),
    )
    if _load.cache_info().hits > before:
        metrics.emit("frame_index_cache", status="memory_hit", source=str(path))
    return result


@lru_cache(maxsize=32)
def _load(root, path, identity, stream_json, tool_versions):
    started = time.perf_counter()
    stream = json.loads(stream_json)
    key = dict(
        version=INDEX_VERSION,
        source=digest(path),
        tools=tool_versions,
        stream=stream["index"],
        time_base=stream["time_base"],
    )
    # JSON normalizes the version tuple so disk and memory keys compare equally.
    key = json.loads(json.dumps(key))
    hashed = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    cache = Project(root, {}).generated(f"cache/frame-index/{hashed}.json")
    tick = Fraction(stream["time_base"])
    status = "miss"
    try:
        data = json.loads(cache.read_text())
        stamps, keys, hashes = data["stamps"], data["keys"], data["hashes"]
        if (
            data["key"] != key
            or len(stamps) < 2
            or any(type(n) is not int for n in stamps)
            or any(b <= a for a, b in zip(stamps, stamps[1:], strict=False))
            or any(type(n) is not int or not 0 <= n < len(stamps) - 1 for n in keys)
            or keys != sorted(set(keys))
            or (
                hashes is not None
                and (
                    len(hashes) != len(stamps) - 1
                    or any(
                        len(row) != 3
                        or row[0] != stamp
                        or type(row[1]) is not int
                        or row[1] <= 0
                        or not re.fullmatch(r"[0-9a-f]{64}", row[2])
                        for stamp, row in zip(stamps, hashes, strict=False)
                    )
                )
            )
            or data.get("hash_failure") is not None
            and not isinstance(data["hash_failure"], str)
        ):
            raise ValueError("invalid index schema")
        result = FrameIndex(
            path,
            cache,
            identity,
            key,
            stream,
            stamps,
            tick,
            keys,
            hashes,
            data.get("hash_failure"),
        )
        status = "disk_hit"
    except FileNotFoundError:
        result = None
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        metrics.emit(
            "frame_index_invalid",
            source=str(path),
            reason="unreadable or invalid cache",
        )
        result = None
    if result is None:
        # Keep the reference's exact timestamp/end-duration validation in one place.
        frames = []
        boundaries = frame_boundaries(path, stream, frames=frames)
        stamps = [int(value / tick) for value in boundaries]
        keys = [i for i, frame in enumerate(frames) if frame.get("key_frame") == 1]
        result = FrameIndex(path, cache, identity, key, stream, stamps, tick, keys)
        result.check_source()
        result.save()
    result.check_source()
    metrics.emit(
        "frame_index_cache",
        status=status,
        source=str(path),
        seconds=time.perf_counter() - started,
        frames=len(result.stamps) - 1,
    )
    return result
