"""Cache invalidation and seeking must preserve the full-decode reference pixels."""

import json
import logging
import os

import pytest

from vw import ffmpeg as media_tools
from vw import frame_index, normalize, timeline
from vw.assets import ingest
from vw.ffmpeg import ffmpeg, run
from vw.project import TIMELINE_FIELDS, WorkbenchError, write_csv


@pytest.fixture(autouse=True)
def clear_indices():
    frame_index._load.cache_clear()
    frame_index._metadata.cache_clear()
    yield
    frame_index._load.cache_clear()
    frame_index._metadata.cache_clear()


def rows(project, asset="red", spans=((0, 10),)):
    write_csv(
        project.path("timeline.csv"),
        TIMELINE_FIELDS,
        [
            dict(asset=asset, src_in_frame=a, src_out_frame=b, at_frame=i * 30)
            for i, (a, b) in enumerate(spans)
        ],
    )


def index(project, asset="red"):
    path = project.media(asset)
    stream = next(
        s for s in frame_index.metadata(path)["streams"] if s["codec_type"] == "video"
    )
    return frame_index.load(project, path, stream)


def test_persistent_index_and_process_reuse(project, monkeypatch):
    rows(project, spans=((0, 10), (30, 40), (60, 75)))
    original = media_tools.run
    probes = []

    def counted(args, **kwargs):
        if "-show_frames" in args:
            probes.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(media_tools, "run", counted)
    expected = timeline.load(project)
    assert len(probes) == 1
    assert timeline.load(project) == expected
    assert len(probes) == 1
    frame_index._load.cache_clear()  # simulate a new invocation
    assert timeline.load(project) == expected
    assert len(probes) == 1
    # A genuinely new interpreter must also reuse the persisted index.
    env = {**os.environ, "VW_FRAME_DIAGNOSTICS": "1"}
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vw",
            "--project",
            str(project.root),
            "timeline",
            "validate",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    events = [json.loads(line) for line in result.stderr.splitlines()]
    assert any(e.get("status") == "disk_hit" for e in events)
    assert not any(e["event"] == "frame_probe" for e in events)
    assert json.loads(result.stdout)["valid"]


def test_content_tool_and_corrupt_cache_invalidation(project, monkeypatch):
    first = index(project)
    original_stamps = first.stamps
    data = json.loads(first.cache.read_text())
    data["stamps"] = [1, 1]
    first.cache.write_text(json.dumps(data))
    frame_index._load.cache_clear()
    assert index(project).stamps == original_stamps
    assert not list(first.cache.parent.glob(".*"))  # atomic siblings cleaned up
    real_tools = frame_index.tools()
    monkeypatch.setattr(frame_index, "tools", lambda: (*real_tools, "changed-prober"))
    second = index(project)
    assert second.cache != first.cache and second.stamps == first.stamps
    # Replacement with the same timestamps must invalidate the process memo too.
    source = project.media("red")
    old_stat = source.stat()
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=green:s=160x90:r=25:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    third = index(project)
    assert third.cache != second.cache and len(third.stamps) == 51


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"stamps": [0]},
        {"key": {}, "stamps": "bad", "keys": [], "hashes": None},
    ],
)
def test_malformed_cache_is_rebuilt(project, bad):
    first = index(project)
    first.cache.write_text(json.dumps(bad))
    frame_index._load.cache_clear()
    assert index(project).stamps == first.stamps


@pytest.fixture
def seek_source(project, request):
    kind = request.param
    path = project.path(f"sources/seek-{kind}.mkv")
    filters = []
    if kind == "vfr":
        filters = ["-vf", "setpts='if(lt(N,180),N,180+(N-180)*2)'", "-fps_mode", "vfr"]
    if kind == "offset":
        filters = ["-vf", "setpts=PTS+5/TB", "-fps_mode", "passthrough"]
    options = [
        "-c:v",
        "libx264",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-bf",
        "3",
    ]
    if kind == "open-gop":
        options += ["-x264-params", "open-gop=1"]
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=30000/1001:d=36.036",
            *filters,
            *options,
            "-threads",
            "1",
            str(path),
        ]
    )
    ingest(project, path)
    asset = next(
        k
        for k, v in project.assets().items()
        if v["path"] == f"sources/seek-{kind}.mkv"
    )
    return asset


def pixels(path):
    return run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "argb",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        binary=True,
    )


@pytest.mark.parametrize(
    "seek_source", ["cfr", "vfr", "offset", "open-gop"], indirect=True
)
@pytest.mark.parametrize("fps", ["24", "30000/1001"])
def test_bounded_seek_matches_reference_pixels(
    project, seek_source, fps, caplog, monkeypatch
):
    project.config["project"]["fps"] = fps
    rows(project, seek_source, ((1, 4), (903, 911), (1007, 1018), (1074, 1080)))
    shots = timeline.load(project)
    caplog.set_level(logging.INFO, logger="vw.performance")
    for shot in shots:
        path, hit = normalize.prepare(project, shot)
        assert not hit
        actual = pixels(path)
        assert len(actual) == shot.frames * 160 * 90 * 4
        path.unlink()
        with monkeypatch.context() as patch:
            patch.setattr(frame_index.FrameIndex, "seek", lambda *_: None)
            reference, hit = normalize.prepare(project, shot)
        assert not hit and pixels(reference) == actual
    events = [json.loads(record.message) for record in caplog.records]
    assert sum(e["event"] == "seek_verified" for e in events) == 3
    verifies = [e for e in events if e["event"] == "seek_verify_decode"]
    assert len(verifies) == 3
    assert all(0 < e["decoded_frames"] < 200 for e in verifies)
    assert sum(e["event"] == "reference_hash_decode" for e in events) == 1


def test_mismatched_seek_falls_back_with_diagnostics(project, monkeypatch, caplog):
    source = index(project)
    source.keys = [0, 10, 20, 30]
    monkeypatch.setattr(frame_index, "MIN_SEEK_PREFIX", 0)
    monkeypatch.setattr(
        source,
        "reference_hashes",
        lambda: [[n, 1, "a" * 64] for n in source.stamps[:-1]],
    )
    monkeypatch.setattr(source, "decode_hashes", lambda *_: [])
    caplog.set_level(logging.INFO, logger="vw.performance")
    assert source.seek(30, 35) is None
    assert any("bounded decode differs" in record.message for record in caplog.records)
    rows(project, spans=((30, 35),))
    shot = timeline.load(project)[0]
    actual, _ = normalize.prepare(project, shot)
    result = pixels(actual)
    actual.unlink()
    monkeypatch.setattr(frame_index.FrameIndex, "seek", lambda *_: None)
    expected, _ = normalize.prepare(project, shot)
    assert result == pixels(expected)


def test_source_changed_during_index_is_rejected(project, monkeypatch):
    original = frame_index.frame_boundaries

    def changed(path, stream, **kwargs):
        result = original(path, stream, **kwargs)
        os.utime(path, ns=(0, 0))
        return result

    monkeypatch.setattr(frame_index, "frame_boundaries", changed)
    with pytest.raises(WorkbenchError, match="Source changed"):
        index(project)
    assert not list(project.path("cache/frame-index").glob("*.json"))


def test_unverifiable_decoder_uses_reference_path(project, monkeypatch, caplog):
    source = index(project)
    source.keys = [0, 10, 20, 30]
    monkeypatch.setattr(frame_index, "MIN_SEEK_PREFIX", 0)

    def unsupported(*args):
        raise WorkbenchError("decoder cannot provide exact hashes")

    monkeypatch.setattr(source, "decode_hashes", unsupported)
    caplog.set_level(logging.INFO, logger="vw.performance")
    assert source.seek(30, 35) is None
    assert source.seek(30, 35) is None
    assert source.hash_failure == "decoder cannot provide exact hashes"
    assert json.loads(source.cache.read_text())["hash_failure"] == source.hash_failure
    assert sum("seek_fallback" in record.message for record in caplog.records) == 2


def test_index_cache_cannot_follow_symlinks(project, tmp_path):
    source = index(project)
    outside = tmp_path / "outside.json"
    original = source.cache.read_bytes()
    outside.write_bytes(original)
    source.cache.unlink()
    source.cache.symlink_to(outside)
    frame_index._load.cache_clear()
    with pytest.raises(WorkbenchError, match="(symlink|relative)"):
        index(project)
    assert outside.read_bytes() == original


def test_source_change_during_seek_does_not_fall_back(project, monkeypatch):
    source = index(project)
    source.keys = [0, 10, 20, 30]
    monkeypatch.setattr(frame_index, "MIN_SEEK_PREFIX", 0)
    monkeypatch.setattr(source, "reference_hashes", lambda: [])

    def change(*args):
        os.utime(source.path, ns=(0, 0))
        return []

    monkeypatch.setattr(source, "decode_hashes", change)
    with pytest.raises(WorkbenchError, match="Source changed"):
        source.seek(30, 35)
