# Using vw

Operational guidance for agents and people assembling edits. See [README.md](README.md)
for an overview and [AGENTS.md](AGENTS.md) for development. Use the CLI's `--help`
for command arguments; module docstrings explain implementation details.

## Install and try the demo

Requires Python 3.13+, ffmpeg/ffprobe with libx264 and qtrle, and Pillow. The Nix
shell supplies the tools; uv installs Python dependencies from the lockfile.
From this repository:

```sh
nix develop
just sync
just demo /tmp/vw-demo
```

Choose a new or empty destination. The demo generates footage and a soundtrack,
then writes editable project files, preview/final videos and review images.
Open `/tmp/vw-demo/output/final.mp4`, or edit the project and render again.

## Set up a project

Use `uv run vw` from this repository, or `vw` after installing the package.
Global `--project` and `--json` options work before or after subcommands.

```sh
uv run vw init my-edit
uv run vw --project my-edit ingest /path/to/footage
```

The project contains `project.toml`, `assets.csv`, `timeline.csv`, `markers.csv`,
and `sources/`. `ingest` copies external media into `sources/`; files already
inside the project are inventoried in place. It preserves existing asset IDs,
descriptions and custom annotation columns. Re-ingest to refresh the inventory
if source files have been replaced.

Ordinary paths must resolve inside the project root. Absolute paths, escaping
paths and symlinks to outside files are rejected. The explicit exception is an
`assets.csv` row with `source_mode=reference`, whose `path` must be an absolute
local media path. Catalogue imports can create these references. Animation files
and generated outputs still require project-relative paths. Timeline sources
cannot live in the project's `cache/` or `output/`, and generated artifacts cannot
follow symlinks.

Keep source files available and immutable during indexing and rendering. For a
self-contained edit, copy or ingest footage into the project. External references
and hardlinks to managed attachments depend on the backing files remaining stable;
they are not independent copies. If source-integrity checks fail, stabilize the
files before retrying.

For ephemeral environments, use a versioned source snapshot or release artifact.
Verify any supplied checksum and record the revision and ffmpeg/ffprobe versions
with diagnostics.

## Configure the output

`vw init` writes a commented `project.toml`. Common settings:

| Section | Setting | Default / meaning |
| --- | --- | --- |
| project | width, height | 1920, 1080; positive even integers |
| project | fps | `"30"`; rational values such as `"30000/1001"` are supported |
| project | audio_master | empty for silence; otherwise an asset ID or relative path |
| project | duration_frames | 0 ends at the last segment; positive sets a fixed end |
| project | background | `"black"`; ffmpeg color name or `"0xRRGGBB"` |
| project | max_upscale | omitted or `"unlimited"`; positive factor caps enlargement |
| project | blend_color_space | `"encoded"` or `"linear"` |
| encoding | codec, pixel_format | `"libx264"`, `"yuv420p"`; codec also accepts `"libx265"` |
| encoding | crf, preset, audio_bitrate | 18, `"medium"`, `"192k"` |
| preview | scale, crf, preset | 0.5, 28, `"ultrafast"` |

Source clip audio is muted. The master soundtrack is trimmed or padded with silence
to the edit duration. Preview dimensions round to even integers. Exports are SDR,
limited-range BT.709, square pixels and constant frame rate. Convert HDR PQ/HLG
footage explicitly to SDR before use. Source audio mixing, automatic tone mapping
and styled text generation are not supported; use prepared images for titles.

## Author a timeline

For precise edits, use decoded source-frame indices and output placement frames:

```csv
asset,src_in_frame,src_out_frame,at_frame,nframes
clip-001,0,24,0,24
clip-002,0,24,30,24
```

Look up asset IDs in `assets.csv` and save the edit in `timeline.csv`. For a
millisecond edit, this places an asset's first two seconds at the start:

```csv
asset,src_in,src_out,at
clip-001,0,2000,0
```

Replace the IDs and ranges with values verified in your footage. At 24 project
FPS this example leaves a six-frame gap before the second shot.

- Source indices count decoded frames in presentation order, independently of
  project FPS or codec keyframes. Ranges are zero-based and half-open `[in,out)`.
- `at_frame` addresses the output timeline. Optional positive `nframes` fixes output
  duration after speed conversion: excess frames are trimmed and short ranges hold
  their final frame. Without it, decoded duration determines output length.
- For still images and generated shapes, source ranges count project frames.
- Legacy millisecond rows use `asset,src_in,src_out,at`. Never mix frame and
  millisecond timing fields in one row; different rows may use different schemas.
- Use `project.duration_frames` for a fixed end and leave intentional blackouts as
  gaps. Preserve output starts, lengths and gaps separately when converting trims.
- For VFR or mixed-cadence footage, use actual decoded timestamps to find frame
  indices. Neither nominal FPS multiplication nor packet PTS proves a source index.

Optional columns:

| Field | Meaning |
| --- | --- |
| layer | integer compositing order, default 0; higher layers are on top |
| fit | `contain` (default), `cover`, or `stretch` |
| max_upscale | positive factor or `unlimited`; blank inherits project setting |
| speed | positive rational, at most 100; `3/2` plays at 1.5× |
| effect | blank, `grayscale`, `vintage`, or a named TOML preset |
| crop | `width:height:x:y` in source pixels after metadata autorotation |
| rotation | clockwise 0, 90, 180, or 270, after crop |
| transition | `cut` (default), `fade`, or `crossfade` |
| transition_frames | positive output-frame count for fade/crossfade |
| opacity | 0–1, default 1 |
| blend_mode | `normal`, `screen`, `add`, `multiply`, or `difference` |
| mask | shape name from project.toml |
| mask_invert | `true` to invert mask coverage; default `false` |
| animation | project-relative layer keyframe JSON path |
| mask_animation | project-relative mask keyframe JSON path; requires `mask` |

Within one layer, later CSV rows composite on top. Gaps show the background;
transparent media and `contain` padding show lower layers. Unknown annotation
columns are preserved by ingestion/import and ignored by rendering.

A crossfade fades the incoming shot over footage below it. Arrange an overlap
covering at least `transition_frames`; validation rejects missing underlying
coverage. `fade` fades both ends, while `crossfade` fades in only.

## Review and render

```sh
uv run vw --project my-edit contact-sheet clip-001
uv run vw --project my-edit frame clip-001 1000
uv run vw --project my-edit timeline validate --json
uv run vw --project my-edit timeline visualize
uv run vw --project my-edit render --preview
uv run vw --project my-edit inspect output/preview.mp4 --preview --json
uv run vw --project my-edit render --final
uv run vw --project my-edit inspect output/final.mp4 --json
```

`frame` takes milliseconds. `timeline visualize` writes an SVG. Validation reports
trim and placement errors, gaps and soundtrack warnings. Its JSON contains named
`markers`, resolved shot `positions`, and `presentation` geometry. Inspect these
before changing an unexpectedly inset or misplaced shot.

`inspect` decodes the output and checks dimensions, cadence, frame count, color
tags and audio against the render manifest. Without a manifest, use `--preview`
when inspecting preview dimensions. Stale project files produce warnings, as do
black frames, quiet passages and high audio peaks; review their editorial intent.
Use `--no-contact-sheet` to skip review images. Failed renders preserve the previous
successful export.

`--json` writes structured results to stdout; project/media errors go to stderr.
Exit codes are 0 for success (including warnings), 1 for failed inspection or
catalogue validation, and 2 for command/project/media errors.

## Reuse clips across edits

A catalogue is a standalone JSON file with `version: 1` and a `clips` array.
Register gestures, motion, camera moves, and entry/exit composition manually:

```sh
uv run vw catalogue /media/library/clips.json init
uv run vw catalogue /media/library/clips.json add sweep take-01.mp4 25 50 \
  --time-unit frames --tag dance --motion left --action "Arm sweep" \
  --camera-movement locked --framing medium \
  --entry "Centered, arm down" --exit "Left edge, arm raised"
uv run vw catalogue /media/library/clips.json list --tag dance --motion left --json
uv run vw catalogue /media/library/clips.json show sweep --json
uv run vw catalogue /media/library/clips.json validate --json
uv run vw catalogue /media/library/clips.json preview sweep
uv run vw --project my-edit catalogue /media/library/clips.json import sweep \
  --at-frame 30 --json
```

These commands need no project except for `import`. Replace paths and bounds with
verified footage. A minimal editable manifest looks like this:

```json
{
  "version": 1,
  "clips": [
    {
      "id": "sweep",
      "source": "take-01.mp4",
      "time_unit": "frames",
      "in": 25,
      "out": 50,
      "tags": ["dance"],
      "motion": "left",
      "entry": "Centered, arm down",
      "exit": "Left edge, arm raised"
    }
  ]
}
```

Required fields are `id`, `source`, `time_unit`, `in`, and `out`; `tags`, `motion`,
`action`, `camera_movement`, `framing`, `entry`, `exit`, and `notes` are optional.
IDs start with a letter or digit and contain letters, digits, dots, underscores
and hyphens. Keep IDs stable when editing records. Edit JSON to revise or remove
entries; `init` and `add` refuse existing files and duplicate IDs respectively.

Relative source paths and `list --source` resolve against the manifest directory,
never the working directory. Absolute local paths are accepted; URIs are not.
`frames` counts decoded source frames. `ms` is relative to the first decoded video
frame and selects frames whose starting timestamps lie in `[in,out)`. Empty
selections, ranges beyond EOF, still images and audio-only files are rejected.

`motion` accepts left/right/up/down/toward/away/static/mixed. Repeated `--tag`
filters require all tags; source, motion and case-insensitive `--query` filters
combine with AND. The query searches IDs, tags and descriptive text. Listing and
showing records do not probe media; validation reports errors per record.

Inspect the entry/exit JPEG before choosing clips. The exit image is the last
included frame. Omit IDs to preview all records, up to 200 per sheet. Disposable
preview and frame-index caches live beside the manifest under `cache/`.

Import appends IDs in command order at the required output `--at-frame`, emitting
frame trims and explicit output lengths. Existing rows, gaps and annotation
columns are preserved. Import does not move existing shots or resolve overlaps;
validate the timeline afterward. Already registered sources at the same resolved
path reuse their asset IDs. New internal sources use relative paths; new external
sources get absolute `source_mode=reference` asset rows. No media is copied or
re-encoded. The edit needs those source files, but the catalogue manifest is no
longer required; timeline `catalogue_id` and `catalogue_manifest` are annotations.
For portable edits, ingest media into the project before cataloguing it.

## Mark cuts against a soundtrack

```sh
uv run vw --project my-edit analyze audio
uv run vw --project my-edit marker 1000 "Entrance" --id entrance
uv run vw --project my-edit marker 842 "Drop" --frames --id drop
```

`analyze audio [asset-or-path]` writes a waveform, an envelope and suggested events.
Suggestions are separate files, not an authoritative beat grid. Only markers
with `origin=manual` drive references. Run analysis again after adding markers to
redraw the waveform.

`markers.csv` columns are `time_ms,at_frame,kind,label,origin,id`. Each row uses
one timing field. IDs are optional, unique, case-sensitive, and match
`[A-Za-z_][A-Za-z0-9_]*`; labels need not be unique. Keep IDs stable when renaming
labels. Older files without IDs remain supported.

Timeline starts accept `marker:entrance`, `marker:entrance-100ms`, or
`marker:drop+3f`. `f` offsets always count output project frames. Use `at` with
millisecond trims or `at_frame` with source-frame trims. Marker references cannot
replace source trims, lengths or transitions. Unknown IDs and negative resolved
times fail validation.

## Fit and composite footage

Use project-level or shot-level `max_upscale=1` to keep small media at native
display size, or `1.5` to permit up to 150% enlargement. Larger footage still
scales down. The cap applies after autorotation, crop, explicit rotation and
pixel-aspect correction. A capped `cover` shot may leave transparent padding;
inspect validation's `presentation` rectangles before treating it as a defect.
Previews preserve final placement. Generated shapes ignore media upscale limits.

Set `blend_mode` per row. `project.blend_color_space="linear"` applies linear-light
BT.709 blending and alpha composition to every segment, including normal layers
and transitions; the default is encoded RGB. Rendered MP4s are opaque over the
project background. Changing blend settings reuses prepared shots.

Named effect presets go in `project.toml`:

```toml
[effects.muted]
saturation = 0.7   # 0–3
contrast = 1.05   # 0–3
brightness = 0.02 # -1–1
```

## Add shapes and masks

Define rectangles or ellipses in `project.toml`:

```toml
[shapes.circle]
type = "ellipse"
x = 0.5
y = 0.5
width = 0.6
height = 0.6
```

Use `shape:circle` as a timeline asset to display it, or `mask=circle` on a media
row to clip that layer. `mask_invert=true` keeps the outside instead. One mask per
segment is supported, including on shape layers. Shapes need no ingested media;
leave their crop, rotation, fit, effect and upscale settings at defaults.

`x`/`y` are center coordinates in 0–1 canvas units. Positive `width`/`height`, up to
2, are fractions of the shorter canvas dimension; equal ellipse dimensions make
a circle. Optional `fill` defaults to white, `stroke` to `none`, `stroke_width`
to 0, and `opacity` to 1. Colors use `#RRGGBB` or `none`. Stroke width uses the
same units as dimensions, must be positive for a visible stroke, and cannot exceed
half the smaller dimension. Strokes are inside the shape; overflow is clipped.
Masks use alpha coverage, including stroke and opacity, rather than fill color.

For a complete generated example:

```sh
uv run python examples/make_geometry_demo.py /tmp/vw-geometry-demo
```

Choose a new or empty destination, then edit its files and render as usual.

## Animate a layer or mask

Reference a project-relative JSON path in `animation`, or `mask_animation` alongside
`mask`. For a 30-output-frame segment, save `animations/move.json` as:

```json
{
  "version": 1,
  "time_base": "local",
  "time_unit": "frames",
  "properties": {
    "x": [{"time": 0, "value": -0.25}, {"time": 29, "value": 0.25}],
    "opacity": [{"time": 0, "value": 0}, {"time": 10, "value": 1}]
  }
}
```

The three header fields default to the values above. `local` counts output time
since the segment starts; `timeline` counts from frame zero. `time_unit` accepts
`frames` or `ms`. Only timeline-timed keys accept marker references, such as
`"marker:entrance+3f"`. Resolved keys must lie within output frames `0..nframes-1`.
A 39-frame fade reaches its last visible frame at key time 38, not 39.

| Property | Default | Range / units |
| --- | --- | --- |
| x, y | 0 | -10 to 10, canvas fractions; positive right/down |
| scale | 1 | 0.01 to 100, uniform multiplier |
| rotation | 0 | -36000 to 36000, clockwise degrees |
| opacity | 1 | 0–1, multiplies existing coverage |
| brightness | 0 | -1 to 1 |
| contrast, saturation | 1 | 0–3 |

Keys may be unordered but must resolve to distinct frames. `interpolation="hold"`
on a key holds its value until the next; the default is linear interpolation.
Endpoint values hold before the first key and after the last. Rotation interpolates
numeric degrees without shortest-path wrapping. Unknown properties, nonfinite
values, empty tracks and out-of-range keys fail validation.

Layer animation runs after static crop, fit and treatment. It moves media and its
applied mask together. Mask animation moves only mask coverage. Animated opacity
multiplies existing alpha, static opacity and fades. For direct Python evaluation,
`Animation.from_dict` accepts `fps`, `at`, `frames` and optional
`markers=named_markers(project)`.

## Diagnose a slow or unexpected render

Caches are disposable. Prepared RGBA intermediates can use substantial disk space;
remove old entries when needed. Changing trims or source content invalidates the
affected shots. Moving a static shot or changing its layer can reuse preparation.
Moving timeline-timed animation can change its resolved keys.

First indexing and checksum verification may decode whole files. Repeated loads
and late cuts reuse caches. `render --seek-mode auto` uses verified bounded seeks
where useful and falls back to decoding from the start if verification fails.
A `seek_fallback` event alone does not indicate a failed render. Use
`render --preview --seek-mode exact` (or `--final`) to decode from the start without
bounded-seek verification. Exact mode still hashes, indexes and validates sources.
Both policies select the same frames.

Set `VW_FRAME_DIAGNOSTICS=1` for JSON cache, timing and fallback events on stderr;
stdout stays available for `--json`. For a fresh attempt after a cached checksum
failure, remove its entry under `cache/frame-index/`. Collect the exact command,
revision, tool versions and sanitized diagnostics when reporting a defect. Prefer
a minimal generated-media example with decoded-pixel comparisons. Keep private
filenames and editorial context out of general repository documentation.

## Call from Python

```python
from vw.project import Project
from vw.timeline import validate
from vw.render import render

project = Project.load("my-edit")
diagnostics = validate(project)
manifest = render(project, preview=True, seek_mode="exact")
```

`vw.catalogue.import_clips(manifest_path, ids, project, at_frame=...)` uses the same
implementation as the CLI. Public modules raise `WorkbenchError` for actionable
project and media errors. See [the modules](src/vw/) for API and pipeline details.
