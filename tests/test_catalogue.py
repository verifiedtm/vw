"""Catalogue behavior, exact source selection, and project reference boundaries."""

import json
import os
from pathlib import Path

import pytest
from PIL import Image

from vw import catalogue, timeline
from vw.cli import main
from vw.ffmpeg import digest, ffmpeg, probe, run
from vw.project import (
    ASSET_FIELDS,
    TIMELINE_FIELDS,
    Project,
    WorkbenchError,
    initialize,
    read_csv,
    write_csv,
    write_json,
)
from vw.render import render


def clip(id="turn", source="footage.mp4", **extra):
    return {
        "id": id,
        "source": str(source),
        "time_unit": "frames",
        "in": 0,
        "out": 25,
        "tags": ["dance", "turn"],
        "motion": "left",
        "action": "Arm sweep",
        "entry": "center",
        "exit": "left edge",
    } | extra


def manifest(tmp_path, clips):
    path = tmp_path / "library" / "clips.json"
    write_json(path, {"version": 1, "clips": clips, "custom": "preserved"})
    return path


def test_search_and_selection_without_media_or_project(tmp_path, monkeypatch, capsys):
    path = manifest(tmp_path, [clip(), clip("jump", motion="up", tags=["dance"])])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert catalogue.search(path, tags=["dance", "turn"], motion="left")["count"] == 1
    assert catalogue.search(path, query="ARM SWEEP")["count"] == 2
    assert catalogue.search(path, query="left edge")["count"] == 2
    assert catalogue.search(path, source=str(path.parent / "footage.mp4"))["count"] == 2
    assert catalogue.search(path, source="other.mp4")["count"] == 0
    assert [c["id"] for c in catalogue.select(path, ["jump", "turn"])] == [
        "jump",
        "turn",
    ]
    with pytest.raises(WorkbenchError, match="Unknown catalogue IDs"):
        catalogue.select(path, ["absent"])
    main(["catalogue", str(path), "list", "--motion", "up", "--json"])
    assert json.loads(capsys.readouterr().out)["clips"][0]["id"] == "jump"
    main(["catalogue", str(path), "show", "turn", "--json"])
    assert json.loads(capsys.readouterr().out)["clips"][0]["action"] == "Arm sweep"


@pytest.mark.parametrize(
    "change",
    [
        {"id": ""},
        {"id": "../unsafe"},
        {"source": ""},
        {"source": "https://a/b"},
        {"time_unit": "seconds"},
        {"in": True},
        {"in": -1},
        {"out": 0},
        {"out": 1.5},
        {"tags": "dance"},
        {"tags": [42]},
        {"entry": []},
        {"motion": "sideways"},
    ],
)
def test_bad_records_have_actionable_diagnostics(tmp_path, change):
    path = manifest(tmp_path, [clip(**change)])
    report = catalogue.validate(path)
    assert report["valid"] is False
    assert report["errors"][0]["row"] == 1
    with pytest.raises(WorkbenchError, match="clip 1"):
        catalogue.load(path)


@pytest.mark.parametrize(
    "document", [[], {}, {"version": True, "clips": []}, {"version": 2, "clips": []}]
)
def test_invalid_manifest(tmp_path, document):
    path = tmp_path / "bad.json"
    write_json(path, document)
    with pytest.raises(WorkbenchError, match="version"):
        catalogue.load(path)
    path.write_text("broken json")
    with pytest.raises(WorkbenchError, match="Cannot read catalogue"):
        catalogue.load(path)


def test_init_and_cli_error_preserve_existing_file(tmp_path, capsys):
    path = tmp_path / "clips.json"
    main(["catalogue", str(path), "init", "--json"])
    original = path.read_bytes()
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["catalogue", str(path), "init", "--json"])
    assert exc.value.code == 2
    assert path.read_bytes() == original
    assert "error" in json.loads(capsys.readouterr().err)
    write_json(path, {"version": 1, "clips": [clip()]})
    with pytest.raises(SystemExit) as exc:
        main(["catalogue", str(path), "validate", "--json"])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_reference_mode_is_explicit_and_scoped(tmp_path):
    root = tmp_path / "edit"
    initialize(root)
    project = Project.load(root)
    external = tmp_path / "source.mp4"
    external.write_bytes(b"untouched")
    for mode, path in [
        ("", str(external)),
        ("", "../source.mp4"),
        ("reference", "../source.mp4"),
        ("unknown", str(external)),
    ]:
        write_csv(
            project.path("assets.csv"),
            ASSET_FIELDS,
            [{"asset": "clip", "path": path, "source_mode": mode}],
        )
        with pytest.raises(WorkbenchError):
            project.assets()
    link = root / "sources" / "escape.mp4"
    link.symlink_to(external)
    write_csv(
        project.path("assets.csv"),
        ASSET_FIELDS,
        [{"asset": "clip", "path": "sources/escape.mp4"}],
    )
    with pytest.raises(WorkbenchError):
        project.assets()
    write_csv(
        project.path("assets.csv"),
        ASSET_FIELDS,
        [{"asset": "clip", "path": str(external), "source_mode": "reference"}],
    )
    assert project.media("clip") == external
    for path in (str(external), "../source.mp4"):
        with pytest.raises(WorkbenchError):
            project.path(path)
        with pytest.raises(WorkbenchError):
            project.media(path)
    (root / "cache" / "escape").symlink_to(tmp_path)
    with pytest.raises(WorkbenchError, match="relative|symlink"):
        project.generated("cache/escape/artifact.jpg")
    assert external.read_bytes() == b"untouched"


def test_add_roundtrip_validation_and_failed_add_preserve_manifest(
    tmp_path, media, monkeypatch
):
    path = manifest(tmp_path, [])
    relative = os.path.relpath(media / "red.mp4", path.parent)
    monkeypatch.chdir(tmp_path)
    first = clip(source=relative, notes="Keep this", custom={"editor": "manual"})
    catalogue.add(path, first)
    catalogue.add(
        path,
        clip(
            "second",
            source=media / "blue.mp4",
            time_unit="ms",
            **{"in": 100, "out": 1000},
        ),
    )
    data = catalogue.load(path)
    assert data["custom"] == "preserved"
    assert data["clips"][0] == first
    assert catalogue.validate(path)["valid"] is True
    original = path.read_bytes()
    for invalid in (
        first,
        clip("long", source=media / "red.mp4", out=76),
        clip("audio", source=media / "music.wav"),
        clip("still", source=media / "logo.png"),
        clip("past", source=media / "red.mp4", time_unit="ms", out=3001),
    ):
        with pytest.raises(WorkbenchError):
            catalogue.add(path, invalid)
        assert path.read_bytes() == original
    # Validation reports independent errors rather than stopping at the first clip.
    data["clips"] += [
        first,
        clip("missing"),
        clip("too-long", source=media / "red.mp4", out=76),
    ]
    write_json(path, data)
    errors = catalogue.validate(path)["errors"]
    assert len(errors) == 3
    assert "duplicate" in errors[0]["error"]
    assert "Missing source" in errors[1]["error"]
    assert "frame count" in errors[2]["error"]


def test_cli_register_and_import_without_copying(tmp_path, media, capsys):
    path = tmp_path / "clips.json"
    main(["catalogue", str(path), "init"])
    main(
        [
            "catalogue",
            str(path),
            "add",
            "turn",
            str(media / "red.mp4"),
            "0",
            "25",
            "--time-unit",
            "frames",
            "--tag",
            "dance",
            "--motion",
            "left",
            "--camera-movement",
            "pan",
            "--json",
        ]
    )
    assert catalogue.select(path, ["turn"])[0]["camera_movement"] == "pan"
    capsys.readouterr()
    root = tmp_path / "edit"
    initialize(root)
    before = digest(media / "red.mp4")
    main(
        [
            "--project",
            str(root),
            "catalogue",
            str(path),
            "import",
            "turn",
            "--at-frame",
            "0",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result["end_frame"] == 30
    project = Project.load(root)
    asset = project.assets()[result["imported"][0]["asset"]]
    assert asset["source_mode"] == "reference"
    assert project.media(asset["asset"]) == media / "red.mp4"
    assert list((root / "sources").iterdir()) == []
    path.unlink()  # Rendering does not depend on the catalogue remaining available.
    assert timeline.validate(project)["duration_frames"] == 30
    output = render(project, preview=True)
    video = next(
        s
        for s in probe(project.path(output["output"]))["streams"]
        if s["codec_type"] == "video"
    )
    assert int(video["nb_frames"]) == 30
    assert digest(media / "red.mp4") == before


def test_import_preserves_rows_annotations_reuses_assets_and_preflights(
    project, tmp_path
):
    path = manifest(
        tmp_path,
        [
            clip(source=project.media("red")),
            clip("blue", source=project.media("blue"), out=24),
        ],
    )
    fields = TIMELINE_FIELDS + ["editor_note"]
    old = {
        "asset": "red",
        "src_in_frame": "0",
        "src_out_frame": "25",
        "at_frame": "0",
        "editor_note": "original",
    }
    write_csv(project.path("timeline.csv"), fields, [old])
    assets = project.assets()
    assets["red"]["custom_annotation"] = "retain"
    write_csv(
        project.path("assets.csv"),
        ASSET_FIELDS + ["custom_annotation"],
        list(assets.values()),
    )
    count = len(project.assets())
    result = catalogue.import_clips(path, ["blue", "turn"], project, at_frame=12)
    assert [r["asset"] for r in result["imported"]] == ["blue", "red"]
    assert [r["at_frame"] for r in result["imported"]] == [12, 22]
    assert len(project.assets()) == count
    assert project.assets()["red"]["custom_annotation"] == "retain"
    rows = read_csv(project.path("timeline.csv"), ["asset"])
    assert all(rows[0][key] == value for key, value in old.items())
    assert rows[1]["catalogue_id"] == "blue"
    assert timeline.validate(project)["duration_frames"] == 32
    before = {
        name: project.path(name).read_bytes() for name in ("assets.csv", "timeline.csv")
    }
    data = catalogue.load(path)
    data["clips"][1]["out"] = 1000
    write_json(path, data)
    with pytest.raises(WorkbenchError, match="frame count"):
        catalogue.import_clips(path, ["turn", "blue"], project, at_frame=32)
    assert all(
        project.path(name).read_bytes() == value for name, value in before.items()
    )
    with pytest.raises(WorkbenchError, match="nonnegative"):
        catalogue.import_clips(path, ["turn"], project, at_frame=-1)


@pytest.fixture
def changing_media(tmp_path, media):
    """Four red frames then four blue frames; VFR plus a nonzero start timestamp."""
    path = tmp_path / "gestures.mkv"
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=red:s=64x48:r=10:d=0.4",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=64x48:r=10:d=0.4",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0,settb=1/10,setpts='if(lt(N,4),N,4+(N-4)*2)+50'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "ffv1",
            str(path),
        ]
    )
    return path


def test_vfr_preview_exclusive_end_cache_and_import(tmp_path, changing_media):
    path = manifest(tmp_path, [clip(source=changing_media, **{"in": 1, "out": 5})])
    result = catalogue.preview(path, ["turn"])
    with Image.open(result["contact_sheet"]) as sheet:
        entry, exit = sheet.getpixel((165, 100)), sheet.getpixel((495, 100))
        assert entry[0] > 220 and entry[2] < 20
        assert exit[2] > 220 and exit[0] < 20
    original_mtime = Path(result["contact_sheet"]).stat().st_mtime_ns
    assert catalogue.preview(path, ["turn"])["cache_hit"] is True
    assert Path(result["contact_sheet"]).stat().st_mtime_ns == original_mtime
    data = catalogue.load(path)
    data["clips"][0]["out"] = 4
    write_json(path, data)
    red_only = catalogue.preview(path, ["turn"])
    assert red_only["contact_sheet"] != result["contact_sheet"]
    with Image.open(red_only["contact_sheet"]) as sheet:
        assert sheet.getpixel((495, 100))[0] > 220
    # [100ms,600ms) relative to first video frame selects source frames [1,5).
    data["clips"][0].update(time_unit="ms", **{"in": 100, "out": 600})
    write_json(path, data)
    root = tmp_path / "edit"
    initialize(root)
    project = Project.load(root)
    project.config["project"].update(fps="10", width=64, height=48)
    result = catalogue.import_clips(path, ["turn"], project, at_frame=0)
    assert result["end_frame"] == 5
    shot = timeline.load(project)[0]
    assert (shot.src_in, shot.src_out, shot.frames) == (1, 5, 5)
    rendered = project.path(render(project, preview=False, seek_mode="exact")["output"])
    pixels = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(rendered),
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
    assert len(pixels) == 15
    assert pixels[0] > 220 and pixels[-1] > 220
    # Replacement source bytes invalidate both frame-index and preview identities.
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=green:s=64x48:r=10:d=1",
            "-c:v",
            "ffv1",
            str(changing_media),
        ]
    )
    replaced = catalogue.preview(path, ["turn"])
    assert replaced["cache_hit"] is False
    assert replaced["contact_sheet"] != red_only["contact_sheet"]
    with Image.open(replaced["contact_sheet"]) as sheet:
        assert sheet.getpixel((165, 100))[1] > 100


def test_subframe_interval_and_eof_preview(tmp_path, media):
    path = manifest(
        tmp_path,
        [clip(source=media / "red.mp4", time_unit="ms", **{"in": 1, "out": 2})],
    )
    assert "no source frames" in catalogue.validate(path)["errors"][0]["error"]
    write_json(
        path,
        {
            "version": 1,
            "clips": [clip(source=media / "red.mp4", **{"in": 74, "out": 75})],
        },
    )
    assert catalogue.validate(path)["valid"]
    assert Path(catalogue.preview(path, ["turn"])["contact_sheet"]).is_file()


def test_import_rolls_back_both_csvs_on_write_failure(project, tmp_path, monkeypatch):
    path = manifest(tmp_path, [clip(source=project.media("red"))])
    # Keep bytes, including the normal CSV CRLF line endings, unchanged on failure.
    before = {
        name: project.path(name).read_bytes() for name in ("assets.csv", "timeline.csv")
    }
    original_write = catalogue.write_csv

    def fail_timeline(destination, fields, rows):
        if destination.name == "timeline.csv":
            raise OSError("simulated write failure")
        return original_write(destination, fields, rows)

    monkeypatch.setattr(catalogue, "write_csv", fail_timeline)
    with pytest.raises(OSError, match="simulated"):
        catalogue.import_clips(path, ["turn"], project, at_frame=0)
    assert all(
        project.path(name).read_bytes() == value for name, value in before.items()
    )


def test_import_retains_empty_annotation_columns_and_registers_internal_media(
    tmp_path, media
):
    import shutil

    root = tmp_path / "edit"
    initialize(root)
    project = Project.load(root)
    source = root / "sources" / "red.mp4"
    shutil.copy2(media / "red.mp4", source)
    write_csv(project.path("assets.csv"), ASSET_FIELDS + ["unused_asset_note"], [])
    write_csv(project.path("timeline.csv"), TIMELINE_FIELDS + ["unused_edit_note"], [])
    path = manifest(tmp_path, [clip(source=source)])
    result = catalogue.import_clips(path, ["turn"], project, at_frame=0)
    assert "unused_asset_note" in project.assets()[result["imported"][0]["asset"]]
    assert "unused_edit_note" in read_csv(project.path("timeline.csv"), ["asset"])[0]
    asset = project.assets()[result["imported"][0]["asset"]]
    assert asset["path"] == "sources/red.mp4"
    assert asset["source_mode"] == ""
    assert timeline.load(project)[0].frames == 30


def test_import_rejects_generated_sources_and_subframe_output(project, tmp_path):
    import shutil

    source = project.generated("cache/source.mp4")
    shutil.copy2(project.media("red"), source)
    path = manifest(tmp_path, [clip(source=source)])
    before = project.path("assets.csv").read_bytes()
    with pytest.raises(WorkbenchError, match="cache/ or output/"):
        catalogue.import_clips(path, ["turn"], project, at_frame=0)
    write_json(
        path, {"version": 1, "clips": [clip(source=project.media("red"), out=1)]}
    )
    with pytest.raises(WorkbenchError, match="shorter than one output frame"):
        catalogue.import_clips(path, ["turn"], project, at_frame=0)
    assert project.path("assets.csv").read_bytes() == before
