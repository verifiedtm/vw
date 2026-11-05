"""The public shell interface. Functions in sibling modules are also importable."""

import argparse
import json
import sys

from . import assets, audio, catalogue, contact, inspect, render, timeline
from .project import Project, WorkbenchError, initialize


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="vw", description="A declarative Python video workbench"
    )
    root.add_argument(
        "--project", default=".", help="video project root (default: current directory)"
    )
    root.add_argument("--json", action="store_true", help="return structured JSON")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="initialize an empty video project directory"
    )
    init.add_argument("directory")
    ingest = commands.add_parser(
        "ingest", help="inventory media; external sources are copied into sources/"
    )
    ingest.add_argument("source")
    analyze = commands.add_parser(
        "analyze", help="waveform and suggested soundtrack events"
    )
    analysis = analyze.add_subparsers(dest="analysis", required=True)
    soundtrack = analysis.add_parser("audio")
    soundtrack.add_argument(
        "asset", nargs="?", help="asset ID or relative path; defaults to audio_master"
    )
    marker = commands.add_parser("marker", help="add an authoritative manual marker")
    marker.add_argument(
        "time_ms", type=int, help="milliseconds, or output frames with --frames"
    )
    marker.add_argument(
        "--frames",
        action="store_true",
        help="interpret position as an output frame index",
    )
    marker.add_argument("label")
    marker.add_argument("--kind", default="cut")
    marker.add_argument("--id", default="", help="unique marker reference identifier")
    sheet = commands.add_parser(
        "contact-sheet", help="sample source footage or rendered output"
    )
    sheet.add_argument("asset")
    sheet.add_argument("--count", type=int, default=12)
    frame = commands.add_parser("frame", help="extract one source or output frame")
    frame.add_argument("asset")
    frame.add_argument("time_ms", type=int)
    edit = commands.add_parser("timeline", help="validate or visualize the timeline")
    edit.add_argument("action", choices=["validate", "visualize"])
    output = commands.add_parser(
        "render", help="prepare cached shots and compose an export"
    )
    mode = output.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--final", action="store_true")
    output.add_argument(
        "--seek-mode",
        choices=("auto", "exact"),
        default="auto",
        help="frame-addressed cuts: verified seeking (auto) or decode from start (exact)",
    )
    inspection = commands.add_parser("inspect", help="decode and diagnose an output")
    inspection.add_argument("media")
    inspection.add_argument(
        "--preview",
        action="store_true",
        help="expect preview dimensions for files without manifests",
    )
    inspection.add_argument("--no-contact-sheet", action="store_true")
    library = commands.add_parser("catalogue", help="manage a reusable clip library")
    library.add_argument("manifest", help="standalone JSON catalogue path")
    actions = library.add_subparsers(dest="catalogue_action", required=True)
    actions.add_parser("init", help="create an empty catalogue")
    register = actions.add_parser("add", help="register a source excerpt")
    register.add_argument("id")
    register.add_argument("source", help="absolute or manifest-relative media path")
    register.add_argument("start", type=int)
    register.add_argument("end", type=int, help="exclusive out point")
    register.add_argument("--time-unit", choices=("ms", "frames"), required=True)
    register.add_argument("--tag", action="append", default=[])
    register.add_argument("--motion", choices=catalogue.MOTIONS, default="")
    for field in catalogue.TEXT_FIELDS:
        register.add_argument("--" + field.replace("_", "-"), default="")
    listing = actions.add_parser("list", help="filter catalogue records")
    listing.add_argument("--source")
    listing.add_argument("--tag", action="append", default=[])
    listing.add_argument("--motion", choices=catalogue.MOTIONS)
    listing.add_argument("--query")
    show = actions.add_parser("show", help="show records in selection order")
    show.add_argument("ids", nargs="+")
    actions.add_parser("validate", help="check records, source files and trim bounds")
    preview = actions.add_parser("preview", help="entry/exit contact sheet")
    preview.add_argument(
        "ids", nargs="*", help="IDs; omitted means all clips (max 200)"
    )
    importing = actions.add_parser("import", help="append selected clips to a project")
    importing.add_argument("ids", nargs="+")
    importing.add_argument("--at-frame", type=int, required=True)
    return root


def catalogue_command(args) -> dict:
    match args.catalogue_action:
        case "init":
            return catalogue.initialize(args.manifest)
        case "add":
            clip = {
                "id": args.id,
                "source": args.source,
                "in": args.start,
                "out": args.end,
                "time_unit": args.time_unit,
                "tags": args.tag,
                "motion": args.motion,
            } | {field: getattr(args, field) for field in catalogue.TEXT_FIELDS}
            return catalogue.add(args.manifest, clip)
        case "list":
            return catalogue.search(
                args.manifest,
                source=args.source,
                tags=args.tag,
                motion=args.motion,
                query=args.query,
            )
        case "show":
            return {"clips": catalogue.select(args.manifest, args.ids)}
        case "validate":
            return catalogue.validate(args.manifest)
        case "preview":
            return catalogue.preview(args.manifest, args.ids)
        case "import":
            return catalogue.import_clips(
                args.manifest,
                args.ids,
                Project.load(args.project),
                at_frame=args.at_frame,
            )
    raise WorkbenchError("Unknown catalogue command")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    # Permit global options at either end of the command, including nested commands.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=".")
    common.add_argument("--json", action="store_true")
    global_args, remaining = common.parse_known_args(argv)
    args = parser().parse_args(remaining)
    args.project, args.json = global_args.project, global_args.json
    try:
        if args.command == "init":
            result = initialize(args.directory)
        elif args.command == "catalogue":
            result = catalogue_command(args)
        else:
            project = Project.load(args.project)
            match args.command:
                case "ingest":
                    result = assets.ingest(project, args.source)
                case "analyze":
                    result = audio.analyze(project, args.asset)
                case "marker":
                    result = audio.add_marker(
                        project,
                        None if args.frames else args.time_ms,
                        args.kind,
                        args.label,
                        at_frame=args.time_ms if args.frames else None,
                        id=args.id,
                    )
                case "contact-sheet":
                    result = contact.contact_sheet(project, args.asset, args.count)
                case "frame":
                    result = contact.extract_frame(project, args.asset, args.time_ms)
                case "timeline":
                    result = (
                        timeline.validate(project)
                        if args.action == "validate"
                        else timeline.visualize(project)
                    )
                case "render":
                    result = render.render(
                        project, args.preview, seek_mode=args.seek_mode
                    )
                case "inspect":
                    result = inspect.inspect(
                        project,
                        args.media,
                        preview=args.preview,
                        sheet=not args.no_contact_sheet,
                    )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for key, value in result.items():
                if key not in ("video", "audio"):
                    print(
                        f"{key}: {json.dumps(value) if isinstance(value, (dict, list)) else value}"
                    )
        if result.get("valid") is False:
            raise SystemExit(1)
    except (WorkbenchError, OSError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"vw: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
