"""Frame-addressed edits are verified against decoded media, not just metadata."""

from fractions import Fraction

import pytest

from vw import audio, timeline
from vw.assets import ingest
from vw.cli import main
from vw.ffmpeg import ffmpeg, probe, run
from vw.project import TIMELINE_FIELDS, WorkbenchError, read_csv, write_csv
from vw.render import render


def frame_row(asset="red", start=0, end=25, at=0, **extra):
    return dict(
        asset=asset, src_in_frame=start, src_out_frame=end, at_frame=at, **extra
    )


def edit(project, rows, fields=TIMELINE_FIELDS):
    write_csv(project.path("timeline.csv"), fields, rows)


def configure(project, fps):
    project.config["project"]["fps"] = fps
    project.config["project"]["audio_master"] = ""
    return project


def decoded_colors(path):
    data = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            "scale=1:1,format=rgb24",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        binary=True,
    )
    return [tuple(data[i : i + 3]) for i in range(0, len(data), 3)]


def test_1933_frame_edit_blackout_and_preview_parity(project):
    configure(project, "24")
    # Still-image ranges and explicit output lengths can describe long holds.
    edit(
        project,
        [
            frame_row("red", end=25, nframes=836),
            frame_row("blue", end=24, at=842, nframes=1091, fit="stretch"),
        ],
    )
    assert timeline.validate(project)["gaps"] == [dict(start_frame=836, end_frame=842)]
    for preview in (False, True):
        result = render(project, preview=preview)
        path = project.path(result["output"])
        colors = decoded_colors(path)
        assert len(colors) == 1933
        assert all(c[0] > 220 for c in colors[:836])
        assert all(max(c) < 10 for c in colors[836:842])
        assert all(c[2] > 220 for c in colors[842:])


@pytest.mark.parametrize("fps", ["24", "30000/1001"])
def test_source_cadence_and_csv_roundtrips(project, fps):
    configure(project, fps)
    # 25 source frames at 25fps become exactly one project second.
    rows = [frame_row(at=i * round(Fraction(fps))) for i in range(3)]
    fields = ["asset", "src_in_frame", "src_out_frame", "at_frame"]
    edit(project, rows, fields)
    expected = [(s.src_in, s.src_out, s.at, s.end) for s in timeline.load(project)]
    for _ in range(5):
        rows = read_csv(project.path("timeline.csv"), fields)
        edit(project, rows, fields)
        assert [
            (s.src_in, s.src_out, s.at, s.end) for s in timeline.load(project)
        ] == expected
    result = render(project, preview=True)
    stream = next(
        s
        for s in probe(project.path(result["output"]))["streams"]
        if s["codec_type"] == "video"
    )
    assert Fraction(stream["avg_frame_rate"]) == Fraction(fps)
    assert int(stream["nb_frames"]) == 3 * round(Fraction(fps))


@pytest.mark.parametrize(
    "extra,match",
    [
        ({"at": 0}, "cannot mix"),
        ({"nframes": 0}, "shorter than one"),
        ({"src_in_frame": -1}, "src_in_frame"),
        ({"src_out_frame": 76}, "exceeds source frame count"),
        ({"at_frame": "1.5"}, "invalid literal"),
        ({"src_out_frame": ""}, "requires"),
    ],
)
def test_invalid_frame_rows(project, extra, match):
    edit(project, [frame_row() | extra])
    with pytest.raises(WorkbenchError, match=match):
        timeline.load(project)


def test_manual_frame_markers_keep_exact_positions(project, capsys):
    configure(project, "30000/1001")
    audio.add_marker(project, 500, "cut", "Legacy")
    audio.add_marker(project, None, "drop", "Exact", at_frame=37)
    rows = read_csv(project.path("markers.csv"), ["at_frame"])
    assert rows[1]["at_frame"] == "37" and rows[1]["time_ms"] == ""
    assert audio.marker_seconds(rows[1], project) == Fraction(37 * 1001, 30000)
    assert audio.analyze(project, "music")["manual_markers"] == 2
    main(
        [
            "marker",
            "42",
            "Entrance",
            "--frames",
            "--project",
            str(project.root),
            "--json",
        ]
    )
    assert '"at_frame": 42' in capsys.readouterr().out
    with pytest.raises(WorkbenchError, match="cannot mix"):
        audio.marker_seconds(dict(time_ms="1", at_frame="1"), project)


def test_vfr_source_indices_use_timestamps_and_decode_exact_frame(project):
    from PIL import Image

    source = project.path("sources/vfr.mkv")
    for i in range(12):
        Image.new("RGB", (160, 90), (20 * i, 0, 0)).save(
            project.path(f"sources/f{i:02}.png")
        )
    ffmpeg(
        [
            "-framerate",
            "24",
            "-i",
            str(project.path("sources/f%02d.png")),
            "-vf",
            "settb=1/24,setpts='if(lt(N,6),N,6+(N-6)*2)'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "ffv1",
            str(source),
        ]
    )
    ingest(project, source)
    asset = next(
        k for k, v in project.assets().items() if v["path"] == "sources/vfr.mkv"
    )
    configure(project, "24")
    edit(project, [frame_row(asset, start=7, end=10)])
    shot = timeline.load(project)[0]
    assert shot.frames == 6  # three decoded frames, each held for two output frames
    result = render(project)
    colors = decoded_colors(project.path(result["output"]))
    assert len(colors) == 6
    assert [round(c[0] / 20) for c in colors] == [7, 7, 8, 8, 9, 9]


def test_fractional_source_fps_and_non_keyframe_single_frame_trim(project):
    source = project.path("sources/fractional.mp4")
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=30000/1001:d=2",
            "-c:v",
            "libx264",
            "-g",
            "120",
            "-keyint_min",
            "120",
            "-sc_threshold",
            "0",
            str(source),
        ]
    )
    ingest(project, source)
    asset = next(
        k for k, v in project.assets().items() if v["path"] == "sources/fractional.mp4"
    )
    configure(project, "30000/1001")
    edit(project, [frame_row(asset, start=37, end=38, nframes=1)])
    result = render(project)

    # Compare to independently decoded source frame 37, inside a long GOP.
    def pixels(path, index):
        return run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                f"select=eq(n\\,{index}),format=rgb24",
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-f",
                "rawvideo",
                "-",
            ],
            binary=True,
        )

    expected = pixels(source, 37)
    actual = pixels(project.path(result["output"]), 0)
    assert len(actual) == len(expected) == 160 * 90 * 3
    assert (
        sum(abs(a - b) for a, b in zip(actual, expected, strict=True)) / len(actual) < 6
    )
    # Full-source EOF bounds and rational source-to-output duration also validate.
    edit(project, [frame_row(asset, end=60)])
    assert timeline.load(project)[0].frames == 60


def test_zero_frame_marker_and_legacy_marker_file(project):
    write_csv(project.path("markers.csv"), ["time_ms", "kind", "label", "origin"], [])
    result = audio.add_marker(project, None, "cut", "Start", at_frame=0)
    assert audio.marker_seconds(result, project) == 0
    assert audio.analyze(project)["manual_markers"] == 1
    with pytest.raises(WorkbenchError, match="cannot mix"):
        audio.marker_seconds(dict(time_ms=0, at_frame=0), project)


def test_frame_timing_with_generated_shapes_and_masks(project):
    configure(project, "24")
    project.config["shapes"] = dict(
        panel=dict(
            type="rectangle",
            x=0.5,
            y=0.5,
            width=2.0,
            height=2.0,
            fill="#00ff00",
        )
    )
    edit(project, [frame_row("shape:panel", start=10, end=16, at=2, mask="panel")])
    shot = timeline.load(project)[0]
    assert shot.frame_indexed and shot.image and shot.frames == 6
    for preview in (False, True):
        result = render(project, preview=preview)
        colors = decoded_colors(project.path(result["output"]))
        assert len(colors) == 8
        assert all(max(c) < 10 for c in colors[:2])
        assert all(c[1] > 220 for c in colors[2:]), colors
