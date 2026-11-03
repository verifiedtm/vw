"""Named positions share exact output timing across edits, animation and CLI."""

import json
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree

import pytest

from vw import audio, timeline
from vw.animation import Animation, load_animation
from vw.cli import main
from vw.ffmpeg import run
from vw.inspect import inspect
from vw.markers import named_markers, resolve_reference
from vw.project import (
    MARKER_FIELDS,
    TIMELINE_FIELDS,
    WorkbenchError,
    read_csv,
    write_csv,
    write_json,
)
from vw.render import render


@pytest.mark.parametrize(
    "fps,position,reference,expected",
    [
        ("10", Fraction(1, 4), "marker:cue", 2),
        ("10", Fraction(1, 4), "marker:cue+1f", 4),
        ("10", Fraction(1, 4), "marker:cue-1f", 2),
        ("10", Fraction(1, 4), "marker:cue+100ms", 4),
        ("10", Fraction(1, 4), "marker:cue-100ms", 2),
        ("30000/1001", Fraction(1001, 1000), "marker:cue", 30),
        ("30000/1001", Fraction(1001, 1000), "marker:cue+3f", 33),
        ("30000/1001", Fraction(1001, 1000), "marker:cue-3f", 27),
        ("30000/1001", Fraction(1001, 1000), "marker:cue+100ms", 33),
        ("30000/1001", Fraction(1001, 1000), "marker:cue-100ms", 27),
        # Frame-authored positions retain their exact sub-millisecond timestamp.
        ("30000/1001", Fraction(37 * 1001, 30000), "marker:cue", 37),
        ("30000/1001", Fraction(37 * 1001, 30000), "marker:cue+2f", 39),
        ("30000/1001", Fraction(37 * 1001, 30000), "marker:cue-100ms", 34),
    ],
)
def test_exact_resolution(fps, position, reference, expected):
    assert (
        resolve_reference(reference, fps=Fraction(fps), markers={"cue": position})
        == expected
    )


@pytest.mark.parametrize(
    "reference,match",
    [
        ("marker:unknown", "Unknown manual marker"),
        ("marker:", "Malformed"),
        ("marker:cue+1", "Malformed"),
        ("marker:cue+1.5f", "Malformed"),
        ("marker:cue--1ms", "Malformed"),
        ("marker:cue+1s", "Malformed"),
        ("marker:cue+1f+1ms", "Malformed"),
        ("marker:cue-1001ms", "negative time"),
        ("marker:cue-11f", "negative time"),
    ],
)
def test_invalid_references(reference, match):
    with pytest.raises(WorkbenchError, match=match):
        resolve_reference(reference, fps=Fraction(10), markers={"cue": Fraction(1)})


def author(project, time=500):
    write_csv(
        project.path("markers.csv"),
        MARKER_FIELDS,
        [dict(id="cue", time_ms=time, kind="cut", label="<Entrance>", origin="manual")],
    )
    write_json(
        project.path("motion.json"),
        dict(
            time_base="timeline",
            properties={
                "opacity": [
                    dict(time="marker:cue+100ms", value=0, interpolation="hold"),
                    dict(time="marker:cue+3f", value=1),
                ]
            },
        ),
    )
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [
            dict(asset="red", src_in=0, src_out=1500, at="marker:cue-2f"),
            dict(
                asset="logo",
                src_in_frame=0,
                src_out_frame=10,
                at_frame="marker:cue+100ms",
                layer=1,
                animation="motion.json",
            ),
        ],
    )


def test_marker_edit_updates_dependents_cli_and_visualization(project, capsys):
    author(project)
    timeline_bytes = project.path("timeline.csv").read_bytes()
    animation_bytes = project.path("motion.json").read_bytes()
    for marker_time, starts, key_frame in [(500, [3, 6], 8), (700, [5, 8], 10)]:
        rows = read_csv(project.path("markers.csv"), ["id"])
        rows[0]["time_ms"] = marker_time
        write_csv(project.path("markers.csv"), MARKER_FIELDS, rows)
        shots = timeline.load(project)
        assert [s.at for s in shots] == starts
        keys = dict(shots[1].animation.properties)["opacity"]
        assert [k.frame for k in keys] == [0, 2]
        # Standalone animation loading resolves the same named markers.
        assert (
            load_animation(project, "motion.json", at=starts[1], frames=10)
            == shots[1].animation
        )
        expected = timeline.validate(project)
        main(["timeline", "validate", "--project", str(project.root), "--json"])
        assert json.loads(capsys.readouterr().out) == expected
        position = expected["positions"][1]
        assert position["start_frame"] == starts[1]
        assert (
            position["animations"]["layer"]["opacity"][1]["timeline_frame"] == key_frame
        )
        svg_path = Path(timeline.visualize(project)["timeline"])
        svg = svg_path.read_text()
        ElementTree.fromstring(svg)
        assert f"timeline frame {key_frame}, local frame 2" in svg
        assert f"marker:cue: frame {marker_time // 100}" in svg
    assert project.path("timeline.csv").read_bytes() == timeline_bytes
    assert project.path("motion.json").read_bytes() == animation_bytes


def test_render_moves_segments_and_property_change(project):
    author(project)
    previous = None
    for marker_time, red_start, green_start in [(500, 3, 8), (700, 5, 10)]:
        rows = read_csv(project.path("markers.csv"), ["id"])
        rows[0]["time_ms"] = marker_time
        write_csv(project.path("markers.csv"), MARKER_FIELDS, rows)
        if previous:
            assert (
                "markers.csv has changed since this render"
                in inspect(project, previous["output"], sheet=False)["warnings"]
            )
        result = render(project)
        if previous:
            assert result["cache_hits"] == 2
            assert result["edit_id"] != previous["edit_id"]
        assert [p["start_frame"] for p in result["positions"]] == [
            red_start,
            red_start + 3,
        ]
        previous = result
        raw = run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(project.path(result["output"])),
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
        stride = 160 * 90 * 3
        pixels = [
            tuple(raw[n + (45 * 160 + 80) * 3 : n + (45 * 160 + 80) * 3 + 3])
            for n in range(0, len(raw), stride)
        ]
        assert all(max(pixel) < 10 for pixel in pixels[:red_start])
        assert all(r > 200 and g < 20 for r, g, _ in pixels[red_start:green_start])
        r, g, _ = pixels[green_start]
        assert g > 100 and r < 30


def test_marker_creation_ids_and_legacy_files(project, capsys):
    write_csv(
        project.path("markers.csv"),
        ["time_ms", "kind", "label", "origin"],
        [dict(time_ms=0, kind="cut", label="Legacy", origin="manual")],
    )
    audio.add_marker(project, 100, "cut", "Same label", id="first")
    main(
        [
            "marker",
            "37",
            "Same label",
            "--frames",
            "--id",
            "second",
            "--project",
            str(project.root),
            "--json",
        ]
    )
    assert json.loads(capsys.readouterr().out)["id"] == "second"
    assert named_markers(project) == {
        "first": Fraction(1, 10),
        "second": Fraction(37, 10),
    }
    before = project.path("markers.csv").read_bytes()
    for identifier, match in [("first", "duplicate"), ("bad-id", "invalid marker id")]:
        with pytest.raises(WorkbenchError, match=match):
            audio.add_marker(project, 200, "cut", "New", id=identifier)
        assert project.path("markers.csv").read_bytes() == before


def test_suggestions_never_resolve_and_analysis_preserves_ids(project):
    author(project)
    before = project.path("markers.csv").read_bytes()
    suggestions = audio.analyze(project)["suggestions"]
    assert project.path("markers.csv").read_bytes() == before
    # A named suggestion, even copied into markers.csv, remains advisory.
    rows = read_csv(project.path("markers.csv"), ["id"])
    rows.append(
        dict(id="advice", time_ms=200, kind="onset", label="Guess", origin="analysis")
    )
    write_csv(project.path("markers.csv"), MARKER_FIELDS, rows)
    assert set(named_markers(project)) == {"cue"}
    with pytest.raises(WorkbenchError, match="Unknown manual marker"):
        resolve_reference(
            "marker:advice", fps=project.fps, markers=named_markers(project)
        )
    assert Path(suggestions).exists()
    assert timeline.load(project)[0].at == 3
    rows.append(rows[0])
    write_csv(project.path("markers.csv"), MARKER_FIELDS, rows)
    with pytest.raises(WorkbenchError, match="markers.csv row 4: duplicate"):
        timeline.load(project)


@pytest.mark.parametrize(
    "data,at,frames,match",
    [
        (
            {"properties": {"x": [{"time": "marker:cue", "value": 0}]}},
            0,
            20,
            "require timeline",
        ),
        (
            {
                "time_base": "timeline",
                "properties": {"x": [{"time": "marker:cue", "value": 0}]},
            },
            11,
            20,
            "outside segment",
        ),
        (
            {
                "time_base": "timeline",
                "properties": {"x": [{"time": "marker:cue+1f", "value": 0}]},
            },
            0,
            11,
            "outside segment",
        ),
        (
            {
                "time_base": "timeline",
                "properties": {
                    "x": [
                        {"time": "marker:cue", "value": 0},
                        {"time": "marker:cue+1ms", "value": 1},
                    ]
                },
            },
            0,
            20,
            "same output frame",
        ),
    ],
)
def test_reference_animation_errors(data, at, frames, match):
    with pytest.raises(WorkbenchError, match=match):
        Animation.from_dict(
            data, fps=Fraction(10), at=at, frames=frames, markers={"cue": Fraction(1)}
        )


def test_fractional_project_marker_and_mask_animation(project):
    project.config["project"]["fps"] = "30000/1001"
    project.config["shapes"] = {
        "box": {"type": "rectangle", "x": 0.5, "y": 0.5, "width": 1, "height": 1}
    }
    audio.add_marker(project, None, "cut", "Exact", at_frame=37, id="cue")
    write_json(
        project.path("mask.json"),
        dict(
            time_base="timeline",
            time_unit="ms",
            properties={"x": [dict(time="marker:cue+2f", value=0.5)]},
        ),
    )
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [
            dict(
                asset="logo",
                src_in=0,
                src_out=1000,
                at="marker:cue-100ms",
                mask="box",
                mask_animation="mask.json",
            )
        ],
    )
    shot = timeline.load(project)[0]
    assert shot.at == 34
    assert shot.mask_animation.manifest()["x"][0]["frame"] == 5
