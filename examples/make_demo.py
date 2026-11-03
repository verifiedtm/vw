"""Build a reproducible edit from generated footage; requires no downloaded media.

Usage: uv run python examples/make_demo.py /tmp/vw-demo
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from vw.assets import ingest
from vw.audio import add_marker, analyze
from vw.ffmpeg import ffmpeg
from vw.inspect import inspect
from vw.project import TIMELINE_FIELDS, Project, initialize, write_csv
from vw.render import render
from vw.timeline import visualize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    initialize(args.directory)
    root = args.directory.resolve()
    config = root / "project.toml"
    config.write_text(
        config.read_text()
        .replace("1920", "640")
        .replace("1080", "360")
        .replace('audio_master = ""', 'audio_master = "music"')
    )
    for name, video in [
        ("landscape", "testsrc2=size=640x360:rate=24:duration=3"),
        ("portrait", "testsrc2=size=240x426:rate=25:duration=3"),
    ]:
        ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                video,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(root / "sources" / f"{name}.mp4"),
            ]
        )
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-af",
            "volume='if(lt(mod(t,0.5),0.08),1,0.15)':eval=frame",
            str(root / "sources/music.wav"),
        ]
    )
    logo = Image.new("RGBA", (640, 360))
    draw = ImageDraw.Draw(logo)
    draw.rounded_rectangle((20, 290, 175, 340), radius=12, fill=(15, 20, 35, 220))
    draw.text((40, 305), "vw / demo", fill="white", font_size=22)
    logo.save(root / "sources/title.png")
    project = Project.load(root)
    ingest(project, "sources")
    write_csv(
        root / "timeline.csv",
        TIMELINE_FIELDS,
        [
            {
                "asset": "landscape",
                "src_in": 0,
                "src_out": 2000,
                "at": 0,
                "fit": "cover",
            },
            {
                "asset": "portrait",
                "src_in": 250,
                "src_out": 2750,
                "at": 1500,
                "fit": "cover",
                "transition": "crossfade",
                "transition_frames": 15,
            },
            {"asset": "title", "src_in": 0, "src_out": 4000, "at": 0, "layer": 1},
        ],
    )
    add_marker(project, 1500, "transition", "Portrait enters")
    analyze(project)
    visualize(project)
    render(project, preview=True)
    render(project)
    diagnostics = inspect(project, "output/final.mp4")
    if not diagnostics["valid"]:
        raise RuntimeError(diagnostics["errors"])
    print(f"Demo complete: {root / 'output/final.mp4'}")
    print(f"Contact sheet: {diagnostics['contact_sheet']}")


if __name__ == "__main__":
    main()
