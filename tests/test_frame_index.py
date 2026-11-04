"""Timestamp failures must explain why exact seek verification is unavailable."""

import json
import logging
from fractions import Fraction

import pytest

from vw import frame_index
from vw.project import WorkbenchError


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mkv"
    path.write_bytes(b"source identity")
    return frame_index.FrameIndex(
        path,
        tmp_path / "index.json",
        frame_index.fingerprint(path),
        {},
        {"index": 2},
        [100, 110, 120, 130],
        Fraction(1, 1000),
        [0],
    )


@pytest.mark.parametrize(
    "stamps, frame, expected, actual",
    [
        ([0, 10, 20], 0, 100, 0),  # constant offset
        ([100, 111, 120], 1, 110, 111),  # later timestamp difference
        ([100, 110], 2, 120, None),  # fewer decoded frames
        ([100, 110, 120, 130], 3, None, 130),  # extra decoded frame
    ],
)
def test_reference_timestamp_failure_is_actionable_and_cached(
    source, monkeypatch, stamps, frame, expected, actual
):
    calls = []

    def decode(*args):
        calls.append(args)
        return [[pts, 16, "a" * 64] for pts in stamps]

    monkeypatch.setattr(source, "decode_hashes", decode)
    for _ in range(2):
        with pytest.raises(WorkbenchError, match="hash decoder timestamps differ"):
            source.reference_hashes()
    assert len(calls) == 1
    assert source.hashes is None
    failure = json.loads(source.cache.read_text())["hash_failure"]
    assert failure == source.hash_failure
    for detail in (
        "stream=2",
        "time_base=1/1000",
        "indexed_frames=3",
        f"decoded_frames={len(stamps)}",
        f"first_difference_frame={frame}",
        f"indexed_pts={expected}",
        f"decoded_pts={actual}",
    ):
        assert detail in failure


@pytest.mark.parametrize("time_base", ["1/90000", None, "0/0"])
def test_hash_time_base_is_diagnostic_only(source, monkeypatch, caplog, time_base):
    header = f"#tb 0: {time_base}\n" if time_base is not None else ""
    monkeypatch.setattr(
        frame_index,
        "run",
        lambda *args, **kwargs: header + f"0, 100, 100, 10, 16, {'a' * 64}\n",
    )
    caplog.set_level(logging.INFO, logger="vw.performance")
    assert source.decode_hashes([]) == [[100, 16, "a" * 64]]
    assert source.hashes is None
    assert source.hash_failure is None
    event = json.loads(caplog.records[-1].message)
    assert event["hash_time_base"] == time_base


def test_hash_timing_diagnostics_preserve_exact_timestamps(source, monkeypatch, caplog):
    # Preserve the declared time base; source stream 2 maps to output 0.
    monkeypatch.setattr(
        frame_index,
        "run",
        lambda *args, **kwargs: (
            "#tb 0: 2/2000\n"
            f"0, 100, 100, 10, 16, {'a' * 64}\n"
            f"0, 110, 110, 10, 16, {'b' * 64}\n"
            f"0, 120, 120, 10, 16, {'c' * 64}\n"
        ),
    )
    caplog.set_level(logging.INFO, logger="vw.performance")
    hashes = source.reference_hashes()
    assert [row[0] for row in hashes] == [100, 110, 120]
    assert [row[2] for row in hashes] == ["a" * 64, "b" * 64, "c" * 64]
    event = json.loads(caplog.records[-1].message)
    assert event["event"] == "frame_hash_timing"
    assert event["stream"] == 2
    assert event["index_time_base"] == "1/1000"
    assert event["hash_time_base"] == "2/2000"
    assert event["hashed_frames"] == 3
    assert event["first_pts"] == 100
    assert event["last_pts"] == 120
