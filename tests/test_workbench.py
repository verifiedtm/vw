import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from vw import audio, contact, timeline
from vw.assets import ingest
from vw.cli import main
from vw.ffmpeg import digest, probe, run
from vw.inspect import inspect
from vw.project import TIMELINE_FIELDS, Project, WorkbenchError, initialize, write_csv
from vw.render import render


def edit(project, rows):
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS, rows)


def shot(asset="red", start=0, end=1000, at=0, **kwargs):
    return {"asset": asset, "src_in": start, "src_out": end, "at": at, **kwargs}


def pixel(path, time, x=80, y=44):
    data = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(time),
            "-i",
            str(path),
            "-vf",
            "format=rgb24",
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
    info = next(s for s in probe(path)["streams"] if s["codec_type"] == "video")
    pos = (y * info["width"] + x) * 3
    return tuple(data[pos : pos + 3])


def test_real_edit_render_cache_and_inspection(project):
    edit(project, [shot(), shot("blue", at=1000, fit="cover", speed="2")])
    validation = timeline.validate(project)
    assert validation["duration_frames"] == 15
    assert not validation["gaps"]
    first = render(project)
    assert first["cache_hits"] == 0
    path = project.path(first["output"])
    assert pixel(path, 0.2)[0] > 220
    assert pixel(path, 1.2)[2] > 220
    info = probe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == 15
    assert video["pix_fmt"] == "yuv420p"
    first_hash = digest(path)
    repeated = render(project)
    assert repeated["cache_hits"] == 2
    assert repeated["edit_id"] == first["edit_id"]
    assert digest(path) == first_hash
    result = inspect(project, first["output"])
    assert result["valid"], result["errors"]
    assert not result["black_intervals"]
    assert Path(result["contact_sheet"]).exists()
    preview = render(project, preview=True)
    assert preview["width"] == 80 and preview["height"] == 44
    assert inspect(project, preview["output"], sheet=False)["valid"]
    # Moving a shot keeps shot preparation valid.
    edit(project, [shot(), shot("blue", at=1200, fit="cover", speed="2")])
    assert render(project)["cache_hits"] == 2
    assert timeline.validate(project)["gaps"] == [{"start_frame": 10, "end_frame": 12}]


def test_layered_image_alpha_and_crossfade(project):
    edit(
        project,
        [
            shot(end=2000),
            shot(
                "blue",
                at=500,
                end=1500,
                layer=1,
                fit="stretch",
                transition="crossfade",
                transition_frames=5,
            ),
            shot("logo", at=1500, end=500, layer=2),
        ],
    )
    result = render(project)
    path = project.path(result["output"])
    r, _, b = pixel(path, 0.7)
    assert r > 40 and b > 40, (r, b)
    assert pixel(path, 1.2)[2] > 220
    # Green image center; transparency preserves blue outside the logo.
    assert pixel(path, 1.7)[1] > 200
    assert pixel(path, 1.7, x=10, y=10)[2] > 200


def test_cache_invalidates_changed_source(project, media):
    edit(project, [shot()])
    original_hash = digest(project.media("red"))
    result = render(project)
    import shutil

    shutil.copy2(media / "blue.mp4", project.media("red"))
    assert digest(project.media("red")) != original_hash
    changed = render(project)
    assert changed["cache_hits"] == 0
    assert changed["prepared"] != result["prepared"]


def test_inventory_preserves_editor_annotations(project):
    path = project.path("assets.csv")
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0]["description"] = "Keep this note"
    rows[0]["editor_tag"] = "favorite"
    write_csv(path, list(rows[0]), [dict.fromkeys(rows[0], "") | row for row in rows])
    ingest(project, project.path("sources"))
    assert project.assets()[rows[0]["asset"]]["description"] == "Keep this note"
    assert project.assets()[rows[0]["asset"]]["editor_tag"] == "favorite"


@pytest.mark.parametrize(
    "row, message",
    [
        (shot("missing"), "unknown asset"),
        (shot(end=5000), "exceeds source"),
        (shot(start=-1), "src_in"),
        (shot(speed="0"), "speed"),
        (shot(fit="unknown"), "fit"),
        (shot(opacity="nan"), "opacity"),
        (shot(crop="900:900:0:0"), "crop"),
        (shot(transition="crossfade"), "transition_frames"),
        (shot(effect="made-up"), "unknown effect"),
    ],
)
def test_invalid_edits_fail_before_render(project, row, message):
    edit(project, [row])
    with pytest.raises(WorkbenchError, match=message):
        render(project)
    assert not project.path("output/final.mp4").exists()


def test_audio_markers_and_review_artifacts(project):
    audio.add_marker(project, 500, "beat", "First cut")
    before = project.path("markers.csv").read_bytes()
    result = audio.analyze(project)
    assert result["duration_ms"] == 4000
    assert result["manual_markers"] == 1
    assert project.path("markers.csv").read_bytes() == before
    assert Image.open(result["waveform"]).size == (1600, 340)
    assert Path(result["suggestions"]).exists()
    review = contact.contact_sheet(project, "red", 4)
    assert Image.open(review["contact_sheet"]).width == 1320
    assert Path(contact.extract_frame(project, "red", 500)["frame"]).exists()
    edit(project, [shot()])
    assert "<svg" in Path(timeline.visualize(project)["timeline"]).read_text()


def test_cli_json_errors_and_module_entrypoint(project, capsys):
    edit(project, [shot()])
    main(["timeline", "validate", "--project", str(project.root), "--json"])
    assert json.loads(capsys.readouterr().out)["valid"]
    with pytest.raises(SystemExit) as error:
        main(["--project", str(project.root), "contact-sheet", "nonexistent", "--json"])
    assert error.value.code == 2
    assert "error" in json.loads(capsys.readouterr().err)
    result = subprocess.run(
        [sys.executable, "-m", "vw", "--help"],
        capture_output=True,
    )
    assert result.returncode == 0 and b"render" in result.stdout


def test_project_path_safety_and_non_destructive_init(tmp_path):
    initialize(tmp_path / "new")
    with pytest.raises(WorkbenchError, match="not empty"):
        initialize(tmp_path / "new")
    project = Project.load(tmp_path / "new")
    with pytest.raises(WorkbenchError, match="relative"):
        project.path("../outside")
    (project.root / "sources" / "escape").symlink_to(tmp_path)
    with pytest.raises(WorkbenchError, match="relative"):
        project.path("sources/escape/nope")


def test_fractional_frame_rate_has_no_accumulated_drift(project):
    config = project.path("project.toml")
    config.write_text(config.read_text().replace('fps = "10"', 'fps = "30000/1001"'))
    project = Project.load(project.root)
    edit(project, [shot(at=0), shot(at=1000), shot(at=2000)])
    assert timeline.validate(project)["duration_frames"] == 90
    result = render(project, preview=True)
    assert inspect(project, result["output"], sheet=False)["valid"]


def test_gaps_silence_and_missing_audio_are_reported(project):
    config = project.path("project.toml")
    config.write_text(
        config.read_text().replace("duration_frames = 0", "duration_frames = 50")
    )
    project = Project.load(project.root)
    edit(project, [shot(at=500)])
    diagnostics = timeline.validate(project)
    assert diagnostics["gaps"][0] == {"start_frame": 0, "end_frame": 5}
    assert any("shorter" in warning for warning in diagnostics["warnings"])
    result = render(project, preview=True)
    report = inspect(project, result["output"], sheet=False)
    assert report["valid"], report["errors"]
    assert report["black_intervals"]
    assert report["silence_intervals"]
    config.write_text(
        config.read_text().replace('audio_master = "music"', 'audio_master = ""')
    )
    project = Project.load(project.root)
    result = render(project, preview=True)
    assert not any(
        s["codec_type"] == "audio"
        for s in probe(project.path(result["output"]))["streams"]
    )


def test_effects_crop_rotation_slow_motion_and_alpha(project):
    config = project.path("project.toml")
    config.write_text(config.read_text() + "\n[effects.mono]\nsaturation = 0.0\n")
    project = Project.load(project.root)
    edit(
        project,
        [
            shot(
                end=500,
                speed="1/4",
                crop="80:80:0:0",
                rotation=90,
                fit="contain",
                effect="mono",
            ),
            shot("logo", end=2000, layer=1, effect="grayscale"),
        ],
    )
    result = render(project)
    assert result["duration_frames"] == 20
    rgb = pixel(project.path(result["output"]), 0.3)
    assert max(rgb) - min(rgb) < 5, rgb
    assert inspect(project, result["output"], sheet=False)["valid"]


def test_failed_export_preserves_previous_video_and_sources(project):
    from vw.ffmpeg import export

    edit(project, [shot()])
    render(project)
    destination = project.path("output/final.mp4")
    before = digest(destination)
    sources = {p: digest(p) for p in project.path("sources").iterdir()}
    with pytest.raises(WorkbenchError):
        export(["-i", str(project.path("missing.mp4"))], destination)
    assert digest(destination) == before
    assert all(digest(p) == sha for p, sha in sources.items())
    assert not list(destination.parent.glob(".vw-*"))


def test_generated_output_symlinks_cannot_overwrite_sources(project):
    edit(project, [shot()])
    source = project.media("red")
    before = digest(source)
    project.path("output").joinpath("final.mp4").symlink_to(source)
    with pytest.raises(WorkbenchError, match="symlink"):
        render(project)
    assert digest(source) == before


def test_external_import_paths_resolve_from_project_and_preserve_originals(
    project, media
):
    original = media / "red.mp4"
    before = digest(original)
    result = ingest(project, original)
    key = result["imported"][0]
    assert project.assets()[key]["path"].startswith("sources/red-")
    assert project.media(key) != original
    assert digest(original) == digest(project.media(key)) == before
    assert ingest(project, "sources")["asset_count"] == result["asset_count"]


def test_crossfade_requires_overlap(project):
    edit(
        project,
        [shot(), shot("blue", at=1000, transition="crossfade", transition_frames=5)],
    )
    with pytest.raises(WorkbenchError, match="underlying"):
        timeline.validate(project)


def test_inspection_detects_stale_edit_and_wrong_format(project):
    edit(project, [shot()])
    render(project)
    edit(project, [shot(at=100)])
    report = inspect(project, "output/final.mp4", sheet=False)
    assert any("timeline.csv has changed" in warning for warning in report["warnings"])
    report = inspect(project, "red", sheet=False)
    assert not report["valid"]
    assert "Unexpected frame rate" in report["errors"]


def test_audio_suggestions_detect_pulses_and_preserve_manual_markers(project):
    from vw.ffmpeg import ffmpeg

    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=800:duration=3:sample_rate=16000",
            "-af",
            "volume='if(lt(mod(t,1),0.15),1,0)':eval=frame",
            str(project.path("sources/pulses.wav")),
        ]
    )
    result = audio.analyze(project, "sources/pulses.wav")
    with Path(result["suggestions"]).open() as f:
        events = list(csv.DictReader(f))
    assert any(row["kind"] == "onset" for row in events)
    assert any(row["kind"] == "quiet_start" for row in events)


@pytest.mark.parametrize(
    "replacement",
    [
        ("width = 1920", "width = 1919"),
        ('fps = "30"', 'fps = "0/1"'),
        ("scale = 0.5", "scale = 0"),
        ('codec = "libx264"', 'codec = "garbage"'),
        ("crf = 18", "crf = 99"),
    ],
)
def test_invalid_config_is_actionable(tmp_path, replacement):
    initialize(tmp_path / "edit")
    path = tmp_path / "edit/project.toml"
    path.write_text(path.read_text().replace(*replacement))
    with pytest.raises(WorkbenchError, match="Invalid"):
        Project.load(path.parent)


def test_trim_bounds_use_video_duration_not_longer_audio(project):
    from vw.ffmpeg import ffmpeg

    ffmpeg(
        [
            "-i",
            str(project.media("red")),
            "-i",
            str(project.media("music")),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(project.path("sources/mixed.mkv")),
        ]
    )
    ingest(project, "sources/mixed.mkv")
    edit(project, [shot("mixed", end=3500)])
    with pytest.raises(WorkbenchError, match="exceeds source"):
        timeline.validate(project)


def test_inspection_bad_manifest_is_actionable(project):
    edit(project, [shot()])
    render(project)
    project.path("output/final.json").write_text("[]")
    with pytest.raises(WorkbenchError, match="manifest"):
        inspect(project, "output/final.mp4", sheet=False)
