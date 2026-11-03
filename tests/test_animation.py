"""Frame-exact animation semantics and independently measured rendered effects."""

from dataclasses import replace
from fractions import Fraction

import pytest
from PIL import Image

from vw.animation import Animation, load_animation
from vw.ffmpeg import digest, run
from vw.normalize import prepare
from vw.project import TIMELINE_FIELDS, WorkbenchError, write_csv, write_json
from vw.render import render
from vw.timeline import load


def data(**properties):
    return {"properties": properties}


def keys(*values, interpolation="linear"):
    return [dict(time=t, value=v, interpolation=interpolation) for t, v in values]


def author(project, animation, **row):
    write_json(project.path("motion.json"), animation)
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [
            dict(asset="logo", src_in=0, src_out=1000, at=0, animation="motion.json")
            | row
        ],
    )
    return load(project)[0]


def images(path, size):
    raw = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
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
    length = size[0] * size[1] * 4
    return [
        Image.frombytes("RGBA", size, raw[n : n + length])
        for n in range(0, len(raw), length)
    ]


def bounds(image):
    return image.getchannel("A").point(lambda a: 255 if a >= 64 else 0).getbbox()


def test_evaluator_rounding_endpoints_and_hold():
    animation = Animation.from_dict(
        data(
            x=keys((2, -1), (6, 1)), opacity=keys((2, 0), (6, 1), interpolation="hold")
        ),
        fps=Fraction(10),
        at=0,
        frames=10,
    )
    assert [animation.value("x", n) for n in (0, 2, 4, 6, 9)] == [-1, -1, 0, 1, 1]
    assert [animation.value("opacity", n) for n in (0, 2, 5, 6, 9)] == [0, 0, 0, 1, 1]
    assert animation.value("scale", 5) == 1
    timed = Animation.from_dict(
        data(x=keys((100, 0), (300, 1))) | dict(time_base="timeline", time_unit="ms"),
        fps=Fraction(30000, 1001),
        at=3,
        frames=10,
    )
    assert timed.value("x", 3) == 0.5
    # Half-frame ties use the same exact nearest-even rule as timeline timing.
    tied = Animation.from_dict(
        data(x=keys((250, 1))) | dict(time_unit="ms"), fps=Fraction(10), at=0, frames=10
    )
    assert tied.properties[0][1][0].frame == 2


@pytest.mark.parametrize(
    "bad",
    [
        {},
        [],
        {"unexpected": 1},
        data(missing=keys((0, 1))),
        data(x=[]),
        data(x=keys((0, 0), (0, 1))),
        data(scale=keys((0, 0))),
        data(opacity=keys((0, 1.01))),
        data(brightness=keys((0, -2))),
        data(contrast=keys((0, 4))),
        data(saturation=keys((0, -1))),
        data(rotation=keys((0, float("inf")))),
        data(x=keys((0, float("nan")))),
        data(x=keys((True, 1))),
        data(x=keys((0, True))),
        data(x=keys((1.5, 0))),
        data(x=keys((-1, 0))),
        data(x=keys((10, 0))),
        data(x=keys((0, 0), interpolation="cubic")),
        data(x=[{"time": 0}]),
        data(x=[{"time": 0, "value": 0, "typo": 1}]),
        data(x=keys((0, 0))) | dict(time_base="source"),
        data(x=keys((0, 0))) | dict(time_unit="seconds"),
        data(x=keys((0, 0))) | dict(version=True),
        data(x=keys((1, 0), (2, 1))) | dict(time_unit="ms"),
    ],
)
def test_invalid_schema(bad):
    with pytest.raises(WorkbenchError, match="Invalid animation"):
        Animation.from_dict(bad, fps=Fraction(10), at=0, frames=10)


def test_translation_scale_rotation_and_opacity(project):
    shot = author(
        project,
        data(
            x=keys((0, -0.25), (8, 0.25)),
            y=keys((0, -0.1), (8, 0.1)),
            scale=keys((0, 0.5), (8, 1.5)),
            rotation=keys((0, 0), (8, 180)),
            opacity=keys((0, 0.2), (8, 1)),
        ),
    )
    path, _ = prepare(project, shot)
    frames = images(path, (160, 90))
    assert len(frames) == 10
    for n, center, extent, alpha in [
        (0, (40, 36), 20.5, 51),
        (4, (80, 45), 41, 153),
        (8, (120, 54), 61.5, 255),
    ]:
        # At these samples the square is at 0/90/180 degrees.
        image = frames[n]
        assert image.getpixel(center)[3] == pytest.approx(alpha, abs=2)
        box = bounds(image) if n else image.getchannel("A").getbbox()
        left, top, right, bottom = box
        assert (left + right) / 2 == pytest.approx(center[0], abs=2)
        assert (top + bottom) / 2 == pytest.approx(center[1], abs=2)
        assert right - left == pytest.approx(extent, abs=3)
        assert bottom - top == pytest.approx(extent, abs=3)
        assert image.getpixel(center)[1] > 240
    assert frames[8].tobytes() == frames[9].tobytes()


def test_rotation_preserves_corners_without_rescaling(project):
    shot = author(project, data(rotation=keys((0, 45))))
    path, _ = prepare(project, shot)
    image = images(path, (160, 90))[0]
    box = bounds(image)
    assert box[2] - box[0] == pytest.approx(41 * 2**0.5, abs=3)
    assert box[3] - box[1] == pytest.approx(41 * 2**0.5, abs=3)
    assert image.getpixel((80, 19))[3] > 150
    assert image.getpixel((80, 19))[1] > 240
    assert image.getpixel((80, 10))[3] == 0


@pytest.mark.parametrize(
    "property,values",
    [
        ("brightness", (0, 0.2, 0.4)),
        ("contrast", (0.5, 1, 1.5)),
        ("saturation", (0, 0.5, 1)),
    ],
)
def test_treatments_match_static_pipeline(project, property, values):
    shot = author(
        project, data(**{property: keys((0, values[0]), (8, values[2]))}), asset="red"
    )
    path, _ = prepare(project, shot)
    animated = images(path, (160, 90))
    for n, value in zip((0, 4, 8), values, strict=True):
        project.config.setdefault("effects", {})["reference"] = {property: value}
        static = replace(shot, animation=None, effect="reference")
        reference, _ = prepare(project, static)
        expected = images(reference, (160, 90))[n]
        assert animated[n].getpixel((80, 45)) == pytest.approx(
            expected.getpixel((80, 45)), abs=2
        )


def test_hold_render_and_normalization_cache(project):
    shot = author(
        project, data(opacity=keys((2, 0.25), (6, 0.75), interpolation="hold"))
    )
    first = render(project)
    original = list(project.path("cache/normalized").glob("*.mov"))
    assert len(original) == 1
    stamp = original[0].stat().st_mtime_ns
    prepared, hit = prepare(project, shot)
    assert hit
    decoded = images(prepared, (160, 90))
    for n in range(10):
        assert decoded[n].getpixel((80, 45))[3] == pytest.approx(
            64 if n < 6 else 191, abs=1
        )
    assert render(project)["cache_hits"] == 1
    author(project, data(opacity=keys((2, 0.25), (6, 1), interpolation="hold")))
    changed = render(project)
    assert changed["cache_hits"] == 0
    assert changed["edit_id"] != first["edit_id"]
    assert changed["animations"]["2"]["layer"]["opacity"][-1]["value"] == 1
    assert list(project.path("cache/normalized").glob("*.mov")) == original
    assert original[0].stat().st_mtime_ns == stamp


def test_mask_and_shape_share_animation_and_preview_timing(project):
    project.config["shapes"] = {
        "box": dict(type="rectangle", x=0.5, y=0.5, width=0.4, height=0.4)
    }
    write_json(project.path("mask.json"), data(x=keys((0, -0.25), (8, 0.25))))
    # Move the mask across a full-canvas generated shape.
    project.config["shapes"]["full"] = dict(
        type="rectangle", x=0.5, y=0.5, width=2, height=2
    )
    shot = author(
        project,
        data(opacity=keys((0, 0.5), (8, 1))),
        asset="shape:full",
        mask="box",
        mask_animation="mask.json",
        at=300,
    )
    for preview, size in [(False, (160, 90)), (True, (80, 44))]:
        path, _ = prepare(project, shot, preview)
        decoded = images(path, size)
        for n, fraction, alpha in [(0, 0.25, 128), (4, 0.5, 191), (8, 0.75, 255)]:
            center = (round(size[0] * fraction), size[1] // 2)
            assert decoded[n].getpixel(center)[3] == pytest.approx(alpha, abs=2)
            assert decoded[n].getpixel((2, 2))[3] == 0
    # Both export paths must retain the exact segment boundaries on the timeline.
    for preview in (False, True):
        result = render(project, preview)
        decoded = images(
            project.path(result["output"]), (result["width"], result["height"])
        )
        assert len(decoded) == 13
        assert (
            max(decoded[2].getpixel((result["width"] // 2, result["height"] // 2))[:3])
            < 5
        )
        assert result["animations"]["2"]["mask"]["x"][1]["frame"] == 8
        for n, fraction, intensity in [(3, 0.25, 128), (7, 0.5, 191), (11, 0.75, 255)]:
            center = (round(result["width"] * fraction), result["height"] // 2)
            assert decoded[n].getpixel(center)[:3] == pytest.approx(
                (intensity,) * 3, abs=4
            )


def test_animation_paths_json_and_validation_before_render(project):
    with pytest.raises(WorkbenchError, match="relative"):
        load_animation(project, "../outside.json", at=0, frames=10)
    project.path("motion.json").write_text('{"properties": {}, "properties": {}}')
    with pytest.raises(WorkbenchError, match="duplicate JSON"):
        load_animation(project, "motion.json", at=0, frames=10)
    with pytest.raises(WorkbenchError, match="outside cache"):
        load_animation(project, "cache/motion.json", at=0, frames=10)
    with pytest.raises(WorkbenchError, match="row 2.*outside segment"):
        author(project, data(x=keys((10, 0))))
    assert not list(project.path("cache/normalized").iterdir())
    author(project, data(x=keys((0, 0))))
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [dict(asset="red", src_in=0, src_out=1000, at=0, mask_animation="motion.json")],
    )
    with pytest.raises(WorkbenchError, match="requires a mask"):
        render(project)


def test_identity_animation_and_legacy_output(project):
    shot = author(project, data(opacity=keys((0, 1))))
    static, _ = prepare(project, replace(shot, animation=None))
    animated, _ = prepare(project, shot)
    assert (
        images(static, (160, 90))[0].tobytes()
        == images(animated, (160, 90))[0].tobytes()
    )
    # New empty columns do not alter legacy rendering.
    rows = [dict(asset="red", src_in=0, src_out=1000, at=0)]
    write_csv(project.path("timeline.csv"), list(rows[0]), rows)
    first = render(project)
    checksum = digest(project.path(first["output"]))
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, rows)
    assert render(project)["cache_hits"] == 1
    assert digest(project.path(first["output"])) == checksum


def test_mask_inversion_source_alpha_and_fade_multiply(project):
    project.config["shapes"] = {
        "box": dict(type="rectangle", x=0.5, y=0.5, width=0.8, height=0.8)
    }
    write_json(project.path("mask.json"), data(opacity=keys((0, 0.5))))
    shot = author(
        project,
        data(opacity=keys((0, 0.5))),
        mask="box",
        mask_animation="mask.json",
        opacity=0.5,
        transition="fade",
        transition_frames=2,
    )
    for inverted in (False, True):
        path, _ = prepare(project, replace(shot, mask_invert=inverted))
        decoded = images(path, (160, 90))
        assert decoded[0].getpixel((80, 45))[3] == 0
        assert decoded[1].getpixel((80, 45))[3] == pytest.approx(16, abs=2)
        assert decoded[4].getpixel((80, 45))[3] == pytest.approx(32, abs=2)
        assert decoded[4].getpixel((10, 10))[3] == 0


def test_offscreen_transform_clips_only_at_canvas(project):
    shot = author(project, data(x=keys((0, 0.5)), scale=keys((0, 2))))
    path, _ = prepare(project, shot)
    image = images(path, (160, 90))[0]
    left, top, right, bottom = bounds(image)
    assert left == pytest.approx(120, abs=2)
    assert right == 160
    assert bottom - top == pytest.approx(82, abs=2)
    assert image.getpixel((159, 45))[3] > 250
    assert image.getpixel((159, 45))[1] > 240
