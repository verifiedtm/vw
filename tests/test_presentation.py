"""Verify capped framing with real decoded alpha and composition pixels."""

from fractions import Fraction

import pytest
from PIL import Image
from test_workbench import edit, pixel, shot

from vw.assets import ingest
from vw.ffmpeg import ffmpeg, probe, run
from vw.normalize import prepare
from vw.presentation import display_size
from vw.project import Project, WorkbenchError
from vw.render import render
from vw.timeline import load, validate


def source(project, size, name="small", color=(0, 255, 0, 255)):
    Image.new("RGBA", size, color).save(project.root / "sources" / f"{name}.png")
    ingest(project, project.root / "sources")
    return name


def alpha_bounds(path):
    stream = next(s for s in probe(path)["streams"] if s["codec_type"] == "video")
    data = run(
        [
            "ffmpeg",
            "-v",
            "error",
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
    return (
        Image.frombytes("RGBA", (stream["width"], stream["height"]), data)
        .getchannel("A")
        .getbbox()
    )


@pytest.mark.parametrize("fit", ["contain", "cover", "stretch"])
@pytest.mark.parametrize("size", [(32, 24), (24, 32), (25, 25)])
def test_native_alpha_and_preview(project, fit, size):
    asset = source(project, size)
    edit(project, [shot(asset, max_upscale=1, fit=fit)])
    s = load(project)[0]
    final, _ = prepare(project, s)
    preview, _ = prepare(project, s, preview=True)
    layout = validate(project)["presentation"]["2"]
    assert layout["limited"]
    assert layout["final"]["scaled_size"] == list(size)
    x, y = layout["final"]["position"]
    assert alpha_bounds(final) == (x, y, x + size[0], y + size[1])
    bounds = alpha_bounds(preview)
    assert all(
        abs(a * scale - b) <= 1
        for a, b, scale in zip(
            alpha_bounds(final), bounds, [0.5, 44 / 90, 0.5, 44 / 90], strict=True
        )
    )
    assert prepare(project, s)[1]


def test_720p_native_and_layer_composition(project):
    project.config["project"].update(width=1280, height=720)
    asset = source(project, (320, 240))
    edit(project, [shot("red", fit="stretch"), shot(asset, max_upscale=1, layer=1)])
    result = render(project)
    assert result["presentation"]["3"]["final"]["visible_size"] == [320, 240]
    path = project.path(result["output"])
    assert pixel(path, 0, 640, 360)[1] > 220
    assert pixel(path, 0, 400, 360)[0] > 220
    assert pixel(path, 0, 640, 200)[0] > 220
    preview = render(project, preview=True)
    assert preview["presentation"]["3"]["target"]["visible_size"] == [160, 120]
    assert pixel(project.path(preview["output"]), 0, 320, 180)[1] > 220
    assert pixel(project.path(preview["output"]), 0, 200, 180)[0] > 220


def test_alpha_inside_native_image_and_background(project):
    asset = source(project, (32, 24), color=(0, 255, 0, 128))
    project.config["project"]["background"] = "red"
    edit(project, [shot(asset, max_upscale=1)])
    result = render(project)
    r, g, b = pixel(project.path(result["output"]), 0)
    assert 90 < r < 170 and 90 < g < 170 and b < 30
    assert pixel(project.path(result["output"]), 0, 10, 10)[0] > 220


def test_project_default_overrides_and_cache(project):
    asset = source(project, (32, 24))
    config = project.path("project.toml")
    config.write_text(
        config.read_text().replace("[project]", "[project]\nmax_upscale = 1")
    )
    project = Project.load(project.root)
    edit(
        project,
        [
            shot(asset),
            shot(asset, at=1000, max_upscale=1.5),
            shot(asset, at=2000, max_upscale="unlimited"),
        ],
    )
    shots = load(project)
    assert [s.max_upscale for s in shots] == [1, 1.5, None]
    first, _ = prepare(project, shots[0])
    second, _ = prepare(project, shots[1])
    assert first != second
    assert alpha_bounds(first) == (64, 33, 96, 57)
    assert alpha_bounds(second) == (56, 27, 104, 63)
    unlimited, _ = prepare(project, shots[2])
    assert alpha_bounds(unlimited) == (20, 0, 140, 90)


def test_downscale_and_capped_cover(project):
    asset = source(project, (1920, 1080))
    edit(project, [shot(asset, max_upscale=1)])
    layout = validate(project)["presentation"]["2"]
    assert not layout["limited"]
    path, _ = prepare(project, load(project)[0])
    assert alpha_bounds(path) == (0, 0, 160, 90)
    asset = source(project, (40, 120), "portrait")
    edit(project, [shot(asset, max_upscale=1, fit="cover")])
    path, _ = prepare(project, load(project)[0])
    assert alpha_bounds(path) == (60, 0, 100, 90)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
def test_invalid_shot_limit(project, value):
    edit(project, [shot(max_upscale=value)])
    with pytest.raises(WorkbenchError, match="row 2: max_upscale"):
        validate(project)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "true", '"bad"'])
def test_invalid_project_limit(project, value):
    config = project.path("project.toml")
    config.write_text(
        config.read_text().replace("[project]", f"[project]\nmax_upscale = {value}")
    )
    with pytest.raises(WorkbenchError, match="max_upscale"):
        Project.load(project.root)


def test_sar_crop_and_rotations(project):
    # Native display size keeps SAR correction on the original physical axis.
    video = {"width": 80, "height": 40, "sample_aspect_ratio": "2:1"}
    assert display_size(video, None, 0) == (160, 40)
    assert display_size(video, (40, 20, 0, 0), 90) == (20, 80)
    video["side_data_list"] = [{"rotation": 90}]
    assert display_size(video, None, 0) == (40, 160)
    assert display_size(video, None, 90) == (160, 40)
    assert display_size(video, (20, 60, 0, 0), 0) == (20, 120)
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=green:s=80x40:r=10:d=1",
            "-vf",
            "setsar=2",
            "-c:v",
            "libx264",
            str(project.root / "sources" / "wide.mp4"),
        ]
    )
    ffmpeg(
        [
            "-display_rotation",
            "90",
            "-i",
            str(project.root / "sources" / "wide.mp4"),
            "-c",
            "copy",
            str(project.root / "sources" / "rotated.mp4"),
        ]
    )
    ingest(project, project.root / "sources")
    for asset, crop, rotation, expected in [
        ("wide", "40:20:0:0", 90, (20, 80)),
        ("rotated", "20:30:0:0", 0, (20, 60)),
        ("rotated", "20:60:0:0", 90, (120, 20)),
    ]:
        edit(project, [shot(asset, crop=crop, rotation=rotation, max_upscale=1)])
        s = load(project)[0]
        assert s.display_size == tuple(Fraction(n) for n in expected)
        path, _ = prepare(project, s)
        x, y, right, bottom = alpha_bounds(path)
        assert (right - x, bottom - y) == expected


def test_odd_crop_and_fractional_cap(project):
    asset = source(project, (40, 40))
    edit(project, [shot(asset, crop="25:17:1:3", max_upscale=0.5)])
    s = load(project)[0]
    path, _ = prepare(project, s)
    assert alpha_bounds(path) == (74, 41, 86, 49)


def test_stretch_caps_axes_independently(project):
    asset = source(project, (40, 120))
    edit(project, [shot(asset, fit="stretch", max_upscale=1)])
    path, _ = prepare(project, load(project)[0])
    assert alpha_bounds(path) == (60, 0, 100, 90)


def test_project_cap_does_not_resize_generated_shapes(project):
    config = project.path("project.toml")
    config.write_text(
        config.read_text().replace("[project]", "[project]\nmax_upscale = 0.5")
        + '\n[shapes.panel]\ntype = "rectangle"\nx = 0.5\ny = 0.5\n'
        + 'width = 0.5\nheight = 0.5\nfill = "#00ff00"\n'
    )
    project = Project.load(project.root)
    edit(project, [shot("shape:panel")])
    s = load(project)[0]
    assert s.max_upscale is None
    path, _ = prepare(project, s)
    bounds = alpha_bounds(path)
    project.config["project"].pop("max_upscale")
    uncapped, _ = prepare(project, load(project)[0])
    assert alpha_bounds(uncapped) == bounds
    project.config["project"]["max_upscale"] = 0.5
    edit(project, [shot("shape:panel", max_upscale=1)])
    with pytest.raises(WorkbenchError, match="shapes do not support max_upscale"):
        validate(project)
