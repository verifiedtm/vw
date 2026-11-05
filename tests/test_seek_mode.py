"""CLI and Python callers share an explicit, validated seek policy."""

import pytest

from vw import cli, normalize
from vw.project import WorkbenchError
from vw.render import render


@pytest.mark.parametrize("mode", ["auto", "exact"])
@pytest.mark.parametrize("target", ["--preview", "--final"])
def test_cli_forwards_seek_mode(mode, target, monkeypatch, capsys):
    project = object()
    monkeypatch.setattr(cli.Project, "load", lambda _: project)

    def capture(actual, preview, *, seek_mode):
        assert actual is project
        assert preview == (target == "--preview")
        assert seek_mode == mode
        return {"seek_mode": seek_mode}

    monkeypatch.setattr(cli.render, "render", capture)
    cli.main(["render", target, "--seek-mode", mode, "--json"])
    assert mode in capsys.readouterr().out


def test_cli_seek_mode_default_and_rejection():
    assert cli.parser().parse_args(["render", "--final"]).seek_mode == "auto"
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(["render", "--final", "--seek-mode", "approximate"])
    assert error.value.code == 2


@pytest.mark.parametrize("mode", ["approximate", "", None])
def test_python_rejects_invalid_mode_before_work(mode):
    with pytest.raises(WorkbenchError, match="seek_mode must be auto or exact"):
        render(None, seek_mode=mode)
    with pytest.raises(WorkbenchError, match="seek_mode must be auto or exact"):
        normalize.prepare(None, None, seek_mode=mode)
