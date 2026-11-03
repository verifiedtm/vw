# vw

A scriptable video workbench. Describe an edit in TOML and CSV; Python prepares
and caches shots, and ffmpeg composites and encodes the result. Original media
is never edited. The CLI and Python modules use the same implementation.

Requires Python 3.13+, ffmpeg/ffprobe with libx264 and qtrle, and Pillow. The Nix
shell supplies Python, ffmpeg, uv, and development tools; uv installs Python
dependencies from `uv.lock`.

```sh
nix develop
just sync
just run --help
just check
```

Create and render a complete example from generated footage and a pulsed tone:

```sh
just demo /tmp/vw-demo
```

The destination must be new or empty. This produces editable project files,
source clips with different dimensions and frame rates, a transparent title
image, soundtrack analysis, a timeline SVG, preview/final videos, render
manifests, an inspection report, and a contact sheet of the final render.

## Make an edit

Use `uv run vw` inside this repository, or install the package and use `vw`.
Global `--project` and `--json` options work before or after subcommands.

```sh
uv run vw init my-edit
# Copy your media to my-edit/sources/, then inventory it:
uv run vw --project my-edit ingest sources/
# Alternatively import outside files; vw copies them into sources/:
uv run vw --project my-edit ingest /absolute/path/to/footage/
```

Set `audio_master` in `my-edit/project.toml` to an asset ID from `assets.csv`, or
a project-relative soundtrack path. Edit `timeline.csv` with a text editor,
spreadsheet, or Python's CSV module. For assets named `clip-001` and `clip-002`:

```csv
asset,src_in,src_out,at,layer,fit,speed,effect
clip-001,0,2000,0,0,cover,1,
clip-002,1000,4000,2000,0,contain,1,grayscale
```

```sh
uv run vw --project my-edit analyze audio
uv run vw --project my-edit marker 2000 "Second shot starts" --kind cut
uv run vw --project my-edit contact-sheet clip-001
uv run vw --project my-edit frame clip-001 1000
uv run vw --project my-edit timeline validate --json
uv run vw --project my-edit timeline visualize
uv run vw --project my-edit render --preview
uv run vw --project my-edit inspect output/preview.mp4
uv run vw --project my-edit render --final
uv run vw --project my-edit inspect output/final.mp4 --json
```

`analyze audio [asset-or-path]` can analyze an explicit soundtrack without changing
configuration. Run it again after adding markers to include them on the waveform.
Analysis suggestions are separate CSV files; nothing automatically changes an
edit or promotes a suggestion to a manual marker.

## Project files

All media references resolve relative to the video project root. Absolute paths
and paths escaping the root are rejected, except import inputs, which can refer
to outside media. Imported external files receive a content suffix to avoid name
collisions. Ingestion preserves existing IDs, descriptions, provenance, and custom
CSV annotation columns. It stores complete ffprobe stream metadata and SHA-256
hashes alongside dimensions, duration, codecs, color metadata, and audio details.
Re-ingest to refresh the inventory after replacing sources. Rendering independently
hashes and probes referenced sources so it cannot reuse a shot for changed media.

```text
my-edit/
├── project.toml
├── assets.csv
├── timeline.csv
├── markers.csv
├── sources/
├── cache/
│   ├── normalized/       # lossless RGBA shot intermediates (.mov)
│   ├── thumbnails/       # labeled contact sheets and extracted frames
│   └── waveforms/        # waveform PNG, envelope JSON, suggestion CSV
└── output/               # preview.mp4, final.mp4, manifests, reports, timeline.svg
```

Generated artifacts cannot follow symlinks, and timeline sources cannot live in
`cache/` or `output/`. Failed renders preserve the previous successful export.
Caches are disposable: deleting them causes preparation to run again.

### project.toml

`vw init` writes a commented configuration. Core settings:

| Section | Setting | Default / behavior |
| --- | --- | --- |
| project | width, height | 1920, 1080; positive even integers |
| project | fps | `"30"`; also accepts `"30000/1001"` |
| project | audio_master | empty means silent export; source clip audio is muted |
| project | duration_frames | 0 means last segment end; positive value adds a fixed end |
| project | background | `"black"`; ffmpeg color name or `"0xRRGGBB"` |
| encoding | codec | `"libx264"`; also `"libx265"` if your ffmpeg supports it |
| encoding | pixel_format | `"yuv420p"`, required for this pipeline |
| encoding | crf, preset | 18, `"medium"` |
| encoding | audio_bitrate | `"192k"`, AAC at 48 kHz |
| preview | scale, crf, preset | 0.5, 28, `"ultrafast"` |

The edit ends at its declared frame count. A long soundtrack is trimmed; a short
soundtrack is padded with silence and produces a validation warning. Preview
scaling rounds each dimension to the nearest even integer. Shot transforms use
the same edit decisions at both resolutions.

### timeline.csv

Only `asset,src_in,src_out,at` are required. Omitted optional columns or empty
values use defaults. Unknown annotation columns are ignored by rendering.

**All four time fields are integer milliseconds.** Source bounds are a half-open
interval `[src_in, src_out)`. Output position and speed-adjusted duration are each
converted once to the nearest target frame using exact rational arithmetic
(ties round to even). Segment ends are calculated from those integer frames;
rounding never accumulates across the timeline. At 30 fps, 1000 ms is frame 30.
Use rational speeds such as `3/2` when exact ratios matter.

| Field | Meaning |
| --- | --- |
| asset | ID in assets.csv |
| src_in, src_out | source trim bounds; for images, the interval controls duration |
| at | output start; nonnegative |
| layer | integer compositing order, default 0; higher layers are on top |
| fit | contain (default), cover, or stretch |
| speed | positive ratio, default 1; 2 doubles playback speed |
| effect | empty, grayscale, vintage, or a named TOML preset |
| crop | optional `width:height:x:y` in source pixels, after metadata autorotation |
| rotation | clockwise 0, 90, 180, or 270, after cropping |
| transition | cut (default), fade, or crossfade |
| transition_frames | positive output frame count for fade/crossfade |
| opacity | 0–1, default 1 |

Within a layer, later CSV rows composite on top of earlier rows. Gaps show the
configured background. `contain` uses transparent padding, showing the background
or lower layers. PNG/WebP alpha is preserved. Crop, rotation, pixel aspect ratio,
scale, speed, frame rate, and treatment are resolved in shot preparation; final
composition uses prepared canvas dimensions without rescaling shots.

A crossfade fades the incoming shot's alpha over an underlying shot. Arrange an
overlap at least as long as `transition_frames`; validation rejects missing
underlying coverage. Example at 30 fps:

```csv
asset,src_in,src_out,at,transition,transition_frames
clip-001,0,2000,0,cut,
clip-002,0,2000,1500,crossfade,15
```

`fade` fades in and out over the given frame count; `crossfade` fades in only.
Effects express technical treatments, never automatic editorial choices. Add
reusable presets to `project.toml`:

```toml
[effects.muted]
saturation = 0.7   # 0–3
contrast = 1.05   # 0–3
brightness = 0.02 # -1–1
```

### markers.csv and analysis

Columns: `time_ms,kind,label,origin`. `vw marker` adds a row with `origin=manual`
and preserves other rows and annotation columns. Kinds can describe beats, lyrics,
drops, transitions, or any editor-defined event.

Audio analysis streams mono PCM into 20 ms RMS and peak windows. It writes a
linear-amplitude waveform with manual marker lines, envelope JSON, and heuristic
suggestions for prominent onsets, quiet passages, and changes in intensity.
These are suggestions rather than a reliable musical beat grid. The window
resolution is 20 ms; reported analysis duration rounds up to a complete window.

## Caching and diagnostics

Prepared shots use content-addressed keys including source bytes, the ffmpeg
version, target dimensions/fps, transforms, relevant effect settings, and a
pipeline version. Moving a shot or changing its layer reuses preparation.
Changing a shot's trim or replacing its source invalidates that shot. Changing
project geometry invalidates affected preparation. Lossless alpha intermediates
favor inspectability and correctness over disk space; keep projects short and
remove old cache entries manually when needed.

Each export has a JSON manifest with source/config/timeline hashes, settings,
frame count, prepared artifact paths, and cache hit count. No timestamp or random
seed is involved in editorial decisions. Byte-identical encoding is only expected
with the same tools and platform; the lockfiles pin development dependencies.

`timeline validate` checks source presence, trim bounds, numeric values, presets,
transitions, duration, and soundtrack availability. It reports uncovered frame
ranges and warnings about silence. `timeline visualize` writes a standalone SVG.

`inspect` decodes the actual output, checks dimensions, fps, frame count, duration,
pixel format, SDR color tags, and soundtrack presence. Render manifests supply
expected settings for previews and finals, and changed project/timeline files
produce stale-render warnings. Without a manifest, it checks against current
project dimensions (or preview dimensions with `--preview`). It also reports near
black intervals, silence below -50 dBFS lasting at least 250 ms, and audio peaks
approaching full scale. Those findings are warnings: an intentional black frame,
quiet passage, or high peak requires editorial judgment. The contact sheet samples
actual rendered frames. Use `--no-contact-sheet` to skip it.

Commands return exit code 0 for success (including editorial warnings), 1 for
failed output inspection, and 2 for project/media errors. `--json` returns JSON
on stdout on success and an `error` object on stderr on failure. Argparse usage
errors use its normal stderr output and exit code 2.

## Python use and development

```python
from vw.project import Project
from vw.timeline import validate
from vw.render import render

project = Project.load("my-edit")
diagnostics = validate(project)
manifest = render(project, preview=True)
```

The public modules are `project`, `assets`, `audio`, `timeline`, `normalize`,
`render`, `contact`, `inspect`, and `ffmpeg`. Errors use `WorkbenchError`.
There is no database, daemon, web service, MoviePy dependency, or agent adapter.

The output pipeline is **SDR, limited-range BT.709, square pixels, constant frame
rate, yuv420p**. HDR PQ/HLG timeline sources are rejected with an actionable error;
convert them explicitly to SDR before use. Untagged SDR relies on ffmpeg's source
interpretation. Automatic HDR tone mapping, advanced color grading, source audio
mixing, animated/styled text, arbitrary transition plugins, and a graphical editor
are outside the current implementation.

`just check` runs Python/Nix/justfile lint and integration tests using generated
media. Tests check decoded pixels, layering/alpha, transitions, speed, exact frame
counts, fractional frame rates, cache behavior, file preservation, audio/review
artifacts, and CLI diagnostics. Media tests require ffmpeg/ffprobe and skip when
those tools are missing. `just build` produces a wheel and source distribution.
