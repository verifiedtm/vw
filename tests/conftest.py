import shutil

import pytest
from PIL import Image, ImageDraw

from vw.assets import ingest
from vw.ffmpeg import ffmpeg
from vw.project import Project, initialize


def pytest_collection_modifyitems(items):
    # Project/media fixtures invoke ffmpeg and ffprobe. Keep this boundary in one
    # place so new end-to-end cases cannot silently slow the iteration suite.
    for item in items:
        if "media" in item.fixturenames and item.get_closest_marker("smoke") is None:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required for media integration tests")
    root = tmp_path_factory.mktemp("media")
    for name, color, size, fps in [
        ("red", "red", "160x90", "25"),
        ("blue", "blue", "90x160", "24"),
    ]:
        ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                f"color={color}:s={size}:r={fps}:d=3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(root / f"{name}.mp4"),
            ]
        )
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4:sample_rate=48000",
            str(root / "music.wav"),
        ]
    )
    overlay = Image.new("RGBA", (160, 90), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle((60, 25, 100, 65), fill=(0, 255, 0, 255))
    overlay.save(root / "logo.png")
    return root


@pytest.fixture
def project(tmp_path, media):
    root = tmp_path / "edit"
    initialize(root)
    config = root / "project.toml"
    config.write_text(
        config.read_text()
        .replace("1920", "160")
        .replace("1080", "90")
        .replace('fps = "30"', 'fps = "10"')
        .replace('audio_master = ""', 'audio_master = "music"')
    )
    # Import from sources so IDs are friendly and deterministic in the test edit.
    for path in media.iterdir():
        shutil.copy2(path, root / "sources" / path.name)
    p = Project.load(root)
    ingest(p, root / "sources")
    return p
