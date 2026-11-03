from dataclasses import replace

import pytest
from PIL import Image

from vw.assets import ingest
from vw.ffmpeg import run
from vw.geometry import load_shapes, rasterize
from vw.inspect import inspect
from vw.normalize import prepare
from vw.project import TIMELINE_FIELDS, Project, WorkbenchError, initialize, write_csv
from vw.render import render
from vw.timeline import load, visualize

CIRCLE = dict(type="ellipse", x=0.5, y=0.5, width=0.6, height=0.6)


def configure(project):
    config = project.path("project.toml")
    config.write_text(
        config.read_text() + '\n[shapes.circle]\ntype = "ellipse"\nx = 0.5\ny = 0.5\n'
        "width = 0.6\nheight = 0.6\n"
        '\n[shapes.panel]\ntype = "rectangle"\nx = 0.2\ny = 0.5\n'
        'width = 0.2\nheight = 0.2\nfill = "#00ff00"\n'
    )
    return Project.load(project.root)


def segment(asset="red", **kwargs):
    return dict(asset=asset, src_in=0, src_out=1000, at=0) | kwargs


def frame(path, size, at=0):
    data = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(at),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgba",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        binary=True,
    )
    return Image.frombytes("RGBA", size, data)


@pytest.mark.parametrize("inverted", [False, True])
def test_circular_mask_alpha_and_composition(project, inverted):
    project = configure(project)
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [
            segment("blue", fit="cover"),
            segment(layer=1, mask="circle", mask_invert=str(inverted).lower()),
        ],
    )
    shot = load(project)[1]
    prepared, _ = prepare(project, shot)
    image = frame(prepared, (160, 90))
    assert image.getpixel((80, 45))[3] == (0 if inverted else 255)
    assert image.getpixel((10, 10))[3] == (255 if inverted else 0)
    assert any(0 < a < 255 for a in image.getchannel("A").tobytes())
    result = render(project)
    output = frame(project.path(result["output"]), (160, 90), at=0.5)
    assert output.getpixel((80, 45))[2 if inverted else 0] > 220
    assert output.getpixel((10, 10))[0 if inverted else 2] > 220
    assert result["geometry_masks"][str(shot.row)] == shot.mask.identity()
    assert inspect(project, result["output"], sheet=False)["valid"]


def test_mask_preserves_existing_alpha_and_applies_opacity_afterward(project):
    project = configure(project)
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [
            segment("logo", mask="circle", opacity="0.5", effect="grayscale"),
        ],
    )
    shot = load(project)[0]
    path, _ = prepare(project, shot)
    image = frame(path, (160, 90))
    assert 126 <= image.getpixel((80, 45))[3] <= 128
    assert image.getpixel((55, 45))[3] == 0  # inside circle, outside original logo
    assert image.getpixel((10, 10))[3] == 0
    faded, _ = prepare(project, replace(shot, transition="fade", transition_frames=3))
    assert frame(faded, (160, 90)).getpixel((80, 45))[3] == 0


def test_shape_sources_layers_timing_preview_and_cache(project):
    project = configure(project)
    rows = [
        segment("blue", fit="cover"),
        segment("shape:circle", layer=1, src_in=250, src_out=1250),
        segment("shape:panel", layer=2, at=200, src_out=500),
    ]
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, rows)
    first = render(project)
    image = frame(project.path(first["output"]), (160, 90), at=0.4)
    assert min(image.getpixel((80, 45))[:3]) > 220
    assert image.getpixel((32, 45))[1] > 220
    assert image.getpixel((10, 10))[2] > 220
    early = frame(project.path(first["output"]), (160, 90), at=0)
    assert early.getpixel((32, 45))[2] > 220
    assert render(project)["cache_hits"] == 3
    rows[2]["at"] = 300
    rows[2]["layer"] = 3
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, rows)
    assert render(project)["cache_hits"] == 3
    project.config["shapes"]["panel"]["fill"] = "#ff0000"
    changed = render(project)
    assert changed["cache_hits"] == 2
    assert changed["prepared"][1] == first["prepared"][1]
    preview = render(project, preview=True)
    small = frame(project.path(preview["output"]), (80, 44), at=0.4)
    assert min(small.getpixel((40, 22))[:3]) > 220
    assert small.getpixel((16, 22))[0] > 220
    assert small.getpixel((5, 5))[2] > 220
    assert inspect(project, preview["output"], sheet=False)["valid"]
    assert project.path("output/timeline.svg").name in visualize(project)["timeline"]


def test_mask_cache_and_generated_source_mask(project):
    project = configure(project)
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [
            segment(mask="circle"),
            segment("shape:circle", layer=1, mask="panel"),
        ],
    )
    first = render(project)
    assert render(project)["cache_hits"] == 2
    project.config["shapes"]["panel"]["x"] = 0.5
    changed = render(project)
    assert changed["cache_hits"] == 1
    assert changed["prepared"][0] == first["prepared"][0]
    project.config["shapes"]["circle"]["width"] = 0.4
    assert render(project)["cache_hits"] == 0


def test_fractional_source_and_mask_alpha_multiply(project):
    project = configure(project)
    project.config["shapes"]["circle"]["opacity"] = 0.5
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [segment("shape:circle", mask="circle", opacity="0.5")],
    )
    path, _ = prepare(project, load(project)[0])
    assert 30 <= frame(path, (160, 90)).getpixel((80, 45))[3] <= 33
    # Direct preparation and rendering must use the same generated-source key.
    assert render(project)["cache_hits"] == 1


@pytest.mark.parametrize("size", [(160, 90), (80, 44), (90, 160)])
def test_geometry_scaling_stroke_clipping_and_cache(project, size):
    circle = load_shapes({"shapes": {"circle": CIRCLE}})["circle"]
    path = rasterize(project, circle, size)
    stamp = path.stat().st_mtime_ns
    assert rasterize(project, circle, size).stat().st_mtime_ns == stamp
    with Image.open(path) as image:
        binary = image.getchannel("A").point(lambda a: 255 if a >= 128 else 0)
        left, top, right, bottom = binary.getbbox()
        assert abs((left + right) / 2 - size[0] / 2) <= 1
        assert abs((top + bottom) / 2 - size[1] / 2) <= 1
        assert abs((right - left) - (bottom - top)) <= 1
        assert abs(right - left - min(size) * 0.6) <= 1
    stroked = replace(
        circle, fill="none", stroke="#ff0000", stroke_width=0.1, opacity=0.5
    )
    with Image.open(rasterize(project, stroked, size)) as image:
        assert image.getpixel((size[0] // 2, size[1] // 2))[3] == 0
        assert (
            126
            <= image.getpixel((size[0] // 2, round(size[1] / 2 - min(size) * 0.25)))[3]
            <= 128
        )
    with Image.open(rasterize(project, replace(circle, x=0, y=0), size)) as image:
        assert image.getpixel((0, 0))[3] == 255
        assert image.getpixel((size[0] - 1, size[1] - 1))[3] == 0


@pytest.mark.parametrize(
    "values",
    [
        {"type": "polygon"},
        {"x": -1},
        {"y": 2},
        {"width": 0},
        {"height": -1},
        {"width": 3},
        {"x": float("nan")},
        {"width": float("inf")},
        {"x": True},
        {"opacity": 2},
        {"fill": "red"},
        {"stroke": "#fff"},
        {"stroke": "#ffffff"},
        {"stroke_width": 0.4},
        {"opacity": False},
        {"radius": 2},
        {"x": "0.5"},
    ],
)
def test_invalid_geometry(values):
    with pytest.raises(WorkbenchError, match="shapes.bad"):
        load_shapes({"shapes": {"bad": CIRCLE | values}})


@pytest.mark.parametrize(
    "row, message",
    [
        (segment("shape:missing"), "unknown shape"),
        (segment(mask="missing"), "unknown mask"),
        (segment(mask_invert="true"), "requires a mask"),
        (segment(mask="circle", mask_invert="yes"), "true or false"),
        (segment("shape:circle", crop="10:10:0:0"), "crop or effect"),
        (segment("shape:circle", rotation="90"), "rotation"),
        (segment("shape:circle", fit="cover"), "fit"),
    ],
)
def test_invalid_geometry_timeline(project, row, message):
    project = configure(project)
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, [row])
    with pytest.raises(WorkbenchError, match=f"row 2: .*{message}"):
        render(project)
    assert not project.path("output/final.mp4").exists()


def test_shape_only_project_and_legacy_csv(tmp_path):
    root = tmp_path / "edit"
    initialize(root)
    project = configure(Project.load(root))
    project.config["project"].update(width=160, height=90)
    write_csv(
        project.path("timeline.csv"),
        ["asset", "src_in", "src_out", "at"],
        [
            segment("shape:circle"),
        ],
    )
    assert render(project)["duration_frames"] == 30


def test_shape_validation_at_project_load(project):
    path = project.path("project.toml")
    path.write_text(path.read_text() + '\n[shapes.invalid]\ntype = "ellipse"\n')
    with pytest.raises(WorkbenchError, match="shapes.invalid"):
        Project.load(project.root)


def test_legacy_media_csv_without_geometry_columns(project):
    write_csv(
        project.path("timeline.csv"),
        ["asset", "src_in", "src_out", "at"],
        [segment()],
    )
    result = render(project)
    image = frame(project.path(result["output"]), (160, 90))
    assert image.getpixel((80, 45))[0] > 220
    assert result["duration_frames"] == 10


def test_reserved_asset_prefix_and_geometry_path_safety(project):
    # Import filenames that happen to contain the reserved prefix under a safe ID.
    source = project.path("sources/shape:circle.png")
    Image.new("RGBA", (10, 10), "white").save(source)
    assert ingest(project, source)["imported"] == ["media-shape:circle"]
    project = configure(project)
    shape = load_shapes(project.config)["circle"]
    outside = project.root.parent / "outside"
    outside.mkdir()
    project.path("cache/geometry").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkbenchError, match="symlinks|relative"):
        rasterize(project, shape, (160, 90))
    assert not list(outside.iterdir())
