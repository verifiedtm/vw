"""Reproducible frame-index/trim benchmark; writes JSON, never clears user caches.

Run in a new directory: uv run python examples/benchmark_frames.py /tmp/vw-bench
Each measurement runs in a fresh interpreter, with OS caches left uncontrolled.
Reference mode uses the original full probe and decode-from-start paths; only
performance instrumentation is shared. See --help for the larger-source option.
"""

import argparse
import json
import logging
import platform
import statistics
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vw import metrics, normalize, timeline
from vw.assets import ingest
from vw.ffmpeg import ffmpeg, frame_boundaries, probe, run
from vw.project import TIMELINE_FIELDS, Project, initialize, write_csv
from vw.render import render


class Events(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        self.events.append(json.loads(record.getMessage()))


def reference(stack):
    """Retain pre-optimization loading/seeking semantics without a second checkout."""
    metadata = {}

    def original_metadata(path):
        metadata[path] = probe(path)
        return metadata[path]

    stack.enter_context(
        patch.object(
            timeline,
            "frame_index",
            SimpleNamespace(
                metadata=original_metadata,
                load=lambda project, path, stream: SimpleNamespace(
                    boundaries=frame_boundaries(path, stream)
                ),
            ),
        )
    )
    stack.enter_context(
        patch.object(
            normalize,
            "frame_index",
            SimpleNamespace(
                metadata=lambda path: metadata[path],
                load=lambda *_: SimpleNamespace(
                    seek=lambda *_: None, check_source=lambda: None
                ),
            ),
        )
    )


def worker(root, mode, action, preview):
    project = Project.load(root)
    recorder = Events()
    metrics.logger.addHandler(recorder)
    metrics.logger.setLevel(logging.INFO)
    with ExitStack() as stack:
        if mode == "reference":
            reference(stack)
        started = time.perf_counter()
        if action == "load":
            timeline.load(project)
        elif action == "prepare":
            shots = timeline.load(project)
            for shot in shots:
                normalize.prepare(project, shot, preview)
        else:
            render(project, preview)
        elapsed = time.perf_counter() - started
    events = recorder.events
    print(
        json.dumps(
            dict(
                seconds=elapsed,
                events=events,
                probe_seconds=sum(
                    e["seconds"] for e in events if e["event"] == "frame_probe_time"
                ),
                probe_frames=sum(
                    e["decoded_frames"] for e in events if e["event"] == "frame_probe"
                ),
                hash_frames=sum(
                    e.get("decoded_frames") or 0
                    for e in events
                    if e["event"] == "reference_hash_decode"
                ),
                verify_frames=sum(
                    e.get("decoded_frames") or 0
                    for e in events
                    if e["event"] == "seek_verify_decode"
                ),
                render_frames=sum(
                    e.get("decoded_frames") or 0
                    for e in events
                    if e["event"] == "ffmpeg_decode"
                ),
                cache_hits=sum(
                    e.get("status") in ("hit", "disk_hit", "memory_hit") for e in events
                ),
                cache_misses=sum(e.get("status") == "miss" for e in events),
            )
        )
    )


def measure(root, mode, action, preview):
    result = subprocess.run(
        [
            sys.executable,
            __file__,
            str(root),
            "--worker",
            "--mode",
            mode,
            "--action",
            action,
            *(["--preview"] if preview else []),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def cold_normalized(project):
    # Only called on artifacts created by this benchmark, in a freshly initialized
    # project. Metadata/hash caches stay warm for the warm-extraction measurement.
    for path in project.path("cache/normalized").glob("*.mov"):
        path.unlink()


def benchmark(args):
    root = args.directory.resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit("Choose a new or empty benchmark directory")
    root.mkdir(parents=True, exist_ok=True)
    sources = root / "fixtures"
    sources.mkdir()
    fps = "30000/1001"
    cases = [
        ("short-cfr", 12, False, 30),
        ("long-cfr", args.long_seconds, False, 60),
        ("vfr", 12, True, 60),
        ("long-gop", args.long_seconds, False, 300),
    ]
    results = []
    report = dict(
        complete=False,
        command=sys.argv,
        platform=platform.platform(),
        python=sys.version,
        ffmpeg=run(["ffmpeg", "-version"]),
        ffprobe=run(["ffprobe", "-version"]),
        long_seconds=args.long_seconds,
        results=results,
    )
    report_path = root / "results.json"
    for name, seconds, vfr, gop in cases:
        source = sources / f"{name}.mkv"
        ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=s=320x180:r={fps}:d={seconds}",
                *(
                    [
                        "-vf",
                        "setpts='if(lt(N,180),N,180+(N-180)*2)'",
                        "-fps_mode",
                        "vfr",
                    ]
                    if vfr
                    else []
                ),
                "-c:v",
                "libx264",
                "-g",
                str(gop),
                "-keyint_min",
                str(gop),
                "-sc_threshold",
                "0",
                "-bf",
                "3",
                "-threads",
                "1",
                str(source),
            ]
        )
        count = int(seconds * 30000 / 1001 + 0.999)
        spans = {
            "early": [(3, 13)],
            "late": [(count - 20, count - 10)],
            "many": [
                (int((count - 20) * i / 9), int((count - 20) * i / 9) + 10)
                for i in range(10)
            ],
        }
        for cut, ranges in spans.items():
            for mode in ("reference", "optimized"):
                destination = root / f"{name}-{cut}-{mode}"
                initialize(destination)
                config = destination / "project.toml"
                config.write_text(
                    config.read_text()
                    .replace("1920", "320")
                    .replace("1080", "180")
                    .replace('fps = "30"', f'fps = "{fps}"')
                )
                project = Project.load(destination)
                ingest(project, source)
                asset = next(iter(project.assets()))
                write_csv(
                    project.path("timeline.csv"),
                    TIMELINE_FIELDS,
                    [
                        dict(
                            asset=asset,
                            src_in_frame=a,
                            src_out_frame=b,
                            at_frame=i * 15,
                        )
                        for i, (a, b) in enumerate(ranges)
                    ],
                )
                records = []
                for phase, action, preview in [
                    ("cold-load", "load", False),
                    ("warm-load", "load", False),
                    ("first-prepare", "prepare", False),
                    ("warm-prepare", "prepare", False),
                    ("preview", "render", True),
                    ("preview-repeat", "render", True),
                    ("final", "render", False),
                    ("final-repeat", "render", False),
                ]:
                    if phase in ("warm-prepare", "final"):
                        cold_normalized(project)
                    samples = [
                        measure(destination, mode, action, preview)
                        for _ in range(args.repeats if phase == "warm-load" else 1)
                    ]
                    sample = min(
                        samples,
                        key=lambda s: abs(
                            s["seconds"]
                            - statistics.median(x["seconds"] for x in samples)
                        ),
                    )
                    record = dict(
                        source=name,
                        cut=cut,
                        mode=mode,
                        phase=phase,
                        **sample,
                        samples_seconds=[s["seconds"] for s in samples],
                    )
                    results.append(record)
                    report_path.write_text(json.dumps(report, indent=2) + "\n")
                    records.append(record)
                print(
                    f"{name:10} {cut:5} {mode:9} "
                    + " ".join(f"{r['phase']}={r['seconds']:.3f}s" for r in records),
                    flush=True,
                )
    report["complete"] = True
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(report_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--long-seconds",
        type=int,
        default=120,
        help="long-source duration (default: 120; use 600 for ten minutes)",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="warm-load repetitions (default: 3)"
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--mode",
        choices=["reference", "optimized"],
        default="optimized",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--action",
        choices=["load", "prepare", "render"],
        default="load",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--preview", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.long_seconds < 12 or args.repeats < 1:
        parser.error("--long-seconds must be >= 12 and --repeats >= 1")
    if args.worker:
        worker(args.directory, args.mode, args.action, args.preview)
    else:
        benchmark(args)


if __name__ == "__main__":
    main()
