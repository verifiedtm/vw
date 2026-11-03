"""Decoded RGB checks against independently calculated blend/coverage math."""

import pytest
from PIL import Image

from vw.assets import ingest
from vw.ffmpeg import digest, probe, run
from vw.project import TIMELINE_FIELDS, Project, WorkbenchError, write_csv
from vw.render import render

MODES = ("normal", "screen", "add", "multiply", "difference")
BACKGROUND = (64, 128, 192)
SOURCE = (224, 160, 96)


def expected(background, source, mode, alpha, space):
    def decode(v):
        return v / 4.5 if v < 0.081 else ((v + 0.099) / 1.099) ** (1 / 0.45)

    def encode(v):
        return 4.5 * v if v < 0.018 else 1.099 * v**0.45 - 0.099

    result = []
    for b, s in zip(background, source, strict=True):
        b, s = b / 255, s / 255
        if space == "linear":
            b, s = decode(b), decode(s)
        blended = {
            "normal": s,
            "screen": 1 - (1 - b) * (1 - s),
            "add": min(1, b + s),
            "multiply": b * s,
            "difference": abs(b - s),
        }[mode]
        value = (1 - alpha) * b + alpha * blended
        result.append(255 * (encode(value) if space == "linear" else value))
    return result


def frames(project, manifest):
    w, h = manifest["width"], manifest["height"]
    data = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(project.path(manifest["output"])),
            "-pix_fmt",
            "rgb24",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        binary=True,
    )
    return [
        Image.frombytes("RGB", (w, h), data[i : i + w * h * 3])
        for i in range(0, len(data), w * h * 3)
    ]


def patches(project, alpha=128):
    Image.new("RGB", (160, 90), BACKGROUND).save(project.path("sources/back.png"))
    source = Image.new("RGBA", (80, 90), (*SOURCE, alpha))
    # Hidden nonzero RGB must not affect the composition at zero alpha.
    source.paste((250, 30, 220, 0), (0, 0, 20, 90))
    source.save(project.path("sources/front.png"))
    ingest(project, project.path("sources"))
    project.config["encoding"].update(crf=0, preset="ultrafast")
    project.config["preview"].update(crf=0)


@pytest.mark.parametrize("space", ["encoded", "linear"])
@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("alpha, opacity", [(128, 0.5), (255, 1)])
def test_blend_colors_alpha_padding_and_boundaries(
    project, space, preview, alpha, opacity
):
    patches(project, alpha)
    project.config["project"]["blend_color_space"] = space
    base = dict(asset="back", src_in_frame=0, src_out_frame=13, at_frame=0)
    rows = [base] + [
        dict(
            asset="front",
            src_in_frame=0,
            src_out_frame=2,
            at_frame=1 + 2 * i,
            layer=1,
            blend_mode=mode,
            opacity=str(opacity),
        )
        for i, mode in enumerate(MODES)
    ]
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, rows)
    manifest = render(project, preview=preview)
    images = frames(project, manifest)
    assert len(images) == 13
    w, h = images[0].size
    center = (w // 2, h // 2)
    for i, mode in enumerate(MODES):
        want = expected(BACKGROUND, SOURCE, mode, round(alpha * opacity) / 255, space)
        for frame in images[1 + 2 * i : 3 + 2 * i]:
            assert frame.getpixel(center) == pytest.approx(want, abs=4)
            assert frame.getpixel((2, h // 2)) == pytest.approx(BACKGROUND, abs=4)
            assert frame.getpixel((w * 3 // 10, h // 2)) == pytest.approx(
                BACKGROUND, abs=4
            )
    for frame in [images[0], images[11], images[12]]:
        assert frame.getpixel(center) == pytest.approx(BACKGROUND, abs=4)
    assert manifest["blend_color_space"] == space
    stream = probe(project.path(manifest["output"]))["streams"][0]
    assert stream["color_space"] == stream["color_transfer"] == "bt709"


def test_normal_default_output_and_blend_cache_reuse(project):
    rows = [dict(asset="red", src_in=0, src_out=500, at=0)]
    # A legacy header without blend_mode must retain exactly the same output.
    write_csv(project.path("timeline.csv"), list(rows[0]), rows)
    original = render(project)
    original_hash = digest(project.path(original["output"]))
    rows[0]["blend_mode"] = "normal"
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, rows)
    explicit = render(project)
    assert explicit["cache_hits"] == 1
    assert digest(project.path(explicit["output"])) == original_hash
    rows[0]["blend_mode"] = "screen"
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, rows)
    assert render(project)["cache_hits"] == 1
    project.config["project"]["blend_color_space"] = "linear"
    assert render(project)["cache_hits"] == 1


@pytest.mark.parametrize("space", ["encoded", "linear"])
def test_overlapping_blends_follow_layer_then_row(project, space):
    patches(project)
    project.config["project"]["blend_color_space"] = space
    rows = [
        dict(
            asset="front", src_in=0, src_out=500, at=200, layer=4, blend_mode="screen"
        ),
        dict(asset="back", src_in=0, src_out=1000, at=0, layer=-1),
        dict(
            asset="front", src_in=0, src_out=300, at=300, layer=3, blend_mode="multiply"
        ),
        dict(
            asset="front",
            src_in=0,
            src_out=100,
            at=400,
            layer=4,
            blend_mode="difference",
        ),
    ]
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, rows)
    images = frames(project, render(project))
    for n in range(10):
        want = BACKGROUND
        for mode, start, end in [
            ("multiply", 3, 6),
            ("screen", 2, 7),
            ("difference", 4, 5),
        ]:
            if start <= n < end:
                want = expected(want, SOURCE, mode, 128 / 255, space)
        assert images[n].getpixel((80, 45)) == pytest.approx(want, abs=4)


@pytest.mark.parametrize("space", ["encoded", "linear"])
def test_blends_respect_masks_and_crossfade_alpha(project, space):
    patches(project)
    project.config["project"]["blend_color_space"] = space
    project.config["shapes"] = {
        "box": dict(type="rectangle", x=0.5, y=0.5, width=0.5, height=0.5, opacity=0.5)
    }
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [
            dict(asset="back", src_in=0, src_out=1000, at=0),
            dict(
                asset="front",
                src_in=0,
                src_out=500,
                at=200,
                layer=1,
                blend_mode="add",
                mask="box",
                opacity="0.5",
                transition="crossfade",
                transition_frames=2,
            ),
        ],
    )
    images = frames(project, render(project))
    # Source alpha 128, mask alpha 128, opacity 0.5 -> coverage 32/255.
    for n, alpha in [(2, 0), (3, 16 / 255), (4, 32 / 255), (6, 32 / 255), (7, 0)]:
        want = expected(BACKGROUND, SOURCE, "add", alpha, space)
        assert images[n].getpixel((80, 45)) == pytest.approx(want, abs=4)
        assert images[n].getpixel((80, 10)) == pytest.approx(BACKGROUND, abs=4)


def test_invalid_blend_settings_before_render(project):
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [dict(asset="red", src_in=0, src_out=500, at=0, blend_mode="Screen")],
    )
    with pytest.raises(WorkbenchError, match="row 2: blend_mode"):
        render(project)
    assert not list(project.path("cache/normalized").iterdir())
    config = project.path("project.toml")
    config.write_text(config.read_text().replace('"encoded"', '"unknown"'))
    with pytest.raises(WorkbenchError, match="blend_color_space"):
        Project.load(project.root)
