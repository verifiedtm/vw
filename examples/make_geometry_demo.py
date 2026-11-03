"""Render a circular video mask and a generated panel without downloaded media.

Usage: uv run python examples/make_geometry_demo.py /tmp/vw-geometry-demo
"""

import argparse
from pathlib import Path

from vw.assets import ingest
from vw.ffmpeg import ffmpeg
from vw.inspect import inspect
from vw.project import TIMELINE_FIELDS, Project, initialize, write_csv
from vw.render import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    initialize(args.directory)
    root = args.directory.resolve()
    config = root / "project.toml"
    config.write_text(
        config.read_text().replace("1920", "640").replace("1080", "360")
        + """
[shapes.circle]
type = "ellipse"
x = 0.5
y = 0.45
width = 0.7
height = 0.7

[shapes.panel]
type = "rectangle"
x = 0.5
y = 0.88
width = 1.2
height = 0.12
fill = "#204060"
stroke = "#ffffff"
stroke_width = 0.01
opacity = 0.8
"""
    )
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(root / "sources/footage.mp4"),
        ]
    )
    project = Project.load(root)
    ingest(project, "sources")
    write_csv(
        root / "timeline.csv",
        TIMELINE_FIELDS,
        [
            dict(asset="footage", src_in=0, src_out=2000, at=0, mask="circle"),
            dict(asset="shape:panel", src_in=0, src_out=2000, at=0, layer=1),
        ],
    )
    for preview in (True, False):
        result = render(project, preview=preview)
        diagnostics = inspect(project, result["output"], sheet=False)
        if not diagnostics["valid"]:
            raise RuntimeError(diagnostics["errors"])
        print(f"Rendered: {project.path(result['output'])}")


if __name__ == "__main__":
    main()
