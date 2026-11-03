"""Declarative video editing with plain project files and ffmpeg."""


def main() -> None:
    from .cli import main as cli_main

    cli_main()
