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
│   ├── animated/         # animated RGBA shots (.mov)
│   ├── geometry/         # generated RGBA shapes (.png)
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
| project | max_upscale | omitted or `"unlimited"` preserves full-canvas fitting; positive factor such as `1.0` caps media enlargement |
| project | background | `"black"`; ffmpeg color name or `"0xRRGGBB"` |
| project | blend_color_space | `"encoded"` (default) or `"linear"`; RGB space for blending and alpha composition |
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

Use frame-addressed timing for precise edits:

```csv
asset,src_in_frame,src_out_frame,at_frame,nframes
clip-001,0,836,0,836
clip-002,0,1091,842,1091
```

At 24 fps this leaves a six-frame blackout at `[836,842)`, with the next shot
starting at output frame 842. Set `project.duration_frames = 1933` for a fixed
1,933-frame edit. Source ranges must exist in the actual footage.

Frame rows require `asset,src_in_frame,src_out_frame,at_frame`. All indices are
zero-based and ranges are half-open. Source indices count **decoded frames in
presentation order**, independently of project FPS and codec keyframes. Video
trims decode from the beginning to select the exact source range. For VFR footage,
original presentation timestamps determine cadence and duration; the exclusive
end uses the next frame's timestamp, or the final frame's declared duration at
EOF. Missing/non-increasing timestamps or an unknown final duration are rejected.
This indexing requires a full source-frame probe when loading a frame-based edit.

`at_frame` is always an **output project frame**. Optional positive `nframes` fixes
the exact output length after speed/cadence conversion: excess frames are trimmed,
and short ranges hold their last frame. Without it, the source timestamp duration
is divided by `speed`, multiplied by exact rational project FPS, and rounded once
(ties to even). For still images and generated shapes, source ranges use project frames instead.
Preview and final use the same source selection and output boundaries.

Existing millisecond rows require `asset,src_in,src_out,at` and retain their timing
behavior. Source bounds are `[src_in,src_out)`; output position and speed-adjusted
duration are rounded independently to project frames, with exact rational
arithmetic. Use rational speeds such as `3/2`. A row cannot populate both timing
schemas, even if their values appear equivalent. Different rows may use different
schemas; blank timing columns are ignored. Omitted optional columns use defaults,
and unknown annotation columns are ignored by rendering. Reading and writing a
frame CSV preserves its indices without any millisecond conversion.

| Field | Meaning |
| --- | --- |
| asset | ID in assets.csv |
| src_in, src_out | source trim bounds; for images, the interval controls duration |
| at | output start in milliseconds; nonnegative |
| src_in_frame, src_out_frame | decoded source-frame range (project frames for stills) |
| at_frame | nonnegative output start frame |
| nframes | optional positive exact output frame count for a frame row |
| layer | integer compositing order, default 0; higher layers are on top |
| fit | contain (default), cover, or stretch |
| max_upscale | optional positive factor; blank inherits project default, `unlimited` overrides it |
| speed | positive ratio, default 1; 2 doubles playback speed |
| effect | empty, grayscale, vintage, or a named TOML preset |
| crop | optional `width:height:x:y` in source pixels, after metadata autorotation |
| rotation | clockwise 0, 90, 180, or 270, after cropping |
| transition | cut (default), fade, or crossfade |
| transition_frames | positive output frame count for fade/crossfade |
| opacity | 0–1, default 1 |
| blend_mode | normal (default), screen, add, multiply, or difference |
| mask | optional shape name from project.toml; clips the fitted segment's alpha |
| animation | optional project-relative JSON keyframe file for the layer |
| mask_animation | optional JSON keyframe file for the mask; requires `mask` |
| mask_invert | `true` inverts mask coverage; `false` (default) keeps the shape interior |

Within a layer, later CSV rows composite on top of earlier rows. Gaps show the
configured background. `contain` uses transparent padding, showing the background
or lower layers. PNG/WebP alpha is preserved. Crop, rotation, pixel aspect ratio,
scale, speed, frame rate, and treatment are resolved in shot preparation; final
composition uses prepared canvas dimensions without rescaling shots.

Set `max_upscale=1` to retain native display size when fitting would enlarge a
source, or `1.5` to permit up to 150%. For example, a 320×240 clip capped at 1 in
a 1280×720 project occupies a centered 320×240 rectangle. Padding is transparent,
so lower layers or the project background remain visible. Larger footage still
downscales normally. The cap uses source dimensions after metadata autorotation,
crop, explicit rotation, and sample-aspect-ratio correction. It adds no sharpening
or color treatment.

With `contain` and `cover`, the cap limits the uniform fit scale. Capped `cover`
crops overflow and pads any dimension that cannot fill the canvas. With `stretch`,
each axis is capped independently. Raster sizes round down to stay within the cap
(with a minimum of one pixel). Previews map the final rectangle into the preview
canvas, allowing at most one pixel of edge rounding, instead of fitting the native
source again. Generated shapes remain canvas-based and ignore the project cap;
a shot-level cap on a shape is rejected.

Both `timeline validate --json` and render manifests include a `presentation`
object keyed by CSV row number. Capped shots report corrected
`source_display_size`, `max_upscale`, whether fitting was `limited`, and a `final`
layout with `scaled_size`, `visible_size`, `position`, and `crop_offset`. Render
manifests also include the actual output's `target` layout (preview or final).
Unlimited shots report `max_upscale: null` and retain the existing fitting path.

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

### Blend modes and color space

Set `blend_mode` on any segment, including generated shapes. Each active segment
blends with the accumulated composition beneath it, ordered by layer and then
CSV row. The configured background is the bottom, opaque layer.

```csv
asset,src_in,src_out,at,layer,blend_mode,opacity
clip-001,0,2000,0,0,normal,1
clip-002,0,1000,500,1,screen,0.6
```

By default, blending operates on encoded RGB channel values. To blend and apply
alpha in linear light instead, set this in the existing `[project]` table:

```toml
blend_color_space = "linear" # or "encoded" (default)
```

This setting applies to every segment, including `normal` and transitions, in
both preview and final renders. It uses the pipeline's BT.709 RGB convention,
not the sRGB transfer curve. Linear mode decodes each prepared RGB channel `E`
with `L = E / 4.5` below `E = 0.081`, otherwise
`L = ((E + 0.099) / 1.099)^(1 / 0.45)`. After composition, it encodes `L` with
`E = 4.5 * L` below `L = 0.018`, otherwise `E = 1.099 * L^0.45 - 0.099`.
This interprets prepared SDR RGB as BT.709; it does not add source color-profile
management. Output remains limited-range BT.709 yuv420p.

For each channel, let `b` be the background and `s` the source, normalized to
0–1 in the selected space. Blend functions are:

| Mode | Function `F(b, s)` | Effect |
| --- | --- | --- |
| normal | `s` | Standard source-over composition |
| screen | `1 - (1 - b) * (1 - s)` | Lightens; black leaves the background unchanged |
| add | `min(1, b + s)` | Adds light, saturating before alpha is applied |
| multiply | `b * s` | Darkens; white leaves the background unchanged |
| difference | `abs(b - s)` | Shows channel differences; identical colors give black |

The resulting channel is `(1 - a) * b + a * F(b, s)`, where `a` is the prepared
source alpha: original coverage multiplied by mask coverage, segment opacity,
and transition fade. Alpha is coverage and never undergoes a transfer function.
Fully transparent pixels leave the background unchanged even when their hidden
RGB is nonzero; transparent contain padding adds no rectangle or black halo.
RGB is straight (unassociated), and the final composition is opaque.

New blend operations use full-range 16-bit RGB intermediates with channel results
clamped to 0–1. Linear mode keeps the accumulated composition in linear light
until final encoding. Existing encoded `normal` segments retain the original
compositing path. Small rounding, scaling, chroma-subsampling, and compression
differences are expected; preview and final use identical blend math.

For example, fully opaque RGB patches `b = (64, 128, 192)` and
`s = (224, 160, 96)` give these rounded 8-bit encoded RGB results. Integration
tests verify decoded output within four channel levels at high encoding quality:

| Mode | encoded | linear |
| --- | --- | --- |
| normal | (224, 160, 96) | (224, 160, 96) |
| screen | (232, 208, 216) | (227, 190, 203) |
| add | (255, 255, 255) | (235, 207, 217) |
| multiply | (56, 80, 72) | (54, 76, 69) |
| difference | (160, 32, 96) | (212, 89, 163) |

Changing only `blend_mode` or `blend_color_space` reuses prepared shots.
Unsupported mode names and color spaces are rejected before rendering.

### Shapes and alpha masks

Define rectangles and ellipses in `project.toml`. Use `shape:<name>` as a timeline
asset to display a shape, or put its name in `mask` to clip a media segment:

```toml
[shapes.circle]
type = "ellipse"
x = 0.5
y = 0.5
width = 0.6
height = 0.6

[shapes.panel]
type = "rectangle"
x = 0.5
y = 0.85
width = 1.2
height = 0.15
fill = "#204060"
stroke = "#ffffff"
stroke_width = 0.01
opacity = 0.8
```

```csv
asset,src_in,src_out,at,layer,mask,mask_invert
clip-001,0,2000,0,0,circle,false
shape:panel,0,2000,0,1,,
```

The `shape:` prefix is reserved for generated sources. Shape-only projects do not
need ingested media. A generated source uses image timing: its source interval
sets its duration, and speed, placement, layers, opacity, and transitions work as
usual. Leave crop, rotation, fit, and effect at their defaults for shape sources.
Animate shape layers and masks with the same keyframe files used for media below.

`x` and `y` are center coordinates from 0 to 1, measured against canvas width and
height, with the origin at the top-left. `width` and `height` are positive values
up to 2, measured against the **shorter canvas dimension**. Equal ellipse
dimensions produce circles. `stroke_width` uses the same units and cannot exceed
half the smaller shape dimension. Strokes are drawn inside the bounding box.
Shapes extending beyond the canvas are clipped.

Fill defaults to white, stroke to `none`, stroke width to 0, and opacity to 1.
Colors accept `#RRGGBB` or `none`; a visible stroke requires a positive width.
Opacity must be between 0 and 1. Geometry values must be finite numbers.
Unknown properties and unsupported shape types are rejected.

Geometry is resolved at each target resolution, including preview rounding.
Normalized centers and proportions persist when resolution or aspect ratio
changes. Shapes use 4× supersampling with Lanczos downsampling, so boundaries
have fractional alpha; small rasterization differences between preview and final
are expected.

Masks use shape alpha, including stroke, opacity, and antialiasing; fill colors
do not change coverage. Masks operate in canvas space after media crop, rotation,
and fitting, multiplying the source's existing alpha. Inversion replaces mask
coverage with `1 - coverage`. Shot opacity and fades apply afterward. A mask
shares its segment's timing and creates no visible layer. One mask per segment
is supported; generated shapes can also have masks.

Prepared shots preserve transparency. MP4 exports composite them over lower
layers and the configured background, producing opaque output.

Run a standalone example with generated footage, a circular mask, and a panel:

```sh
uv run python examples/make_geometry_demo.py /tmp/vw-geometry-demo
```

The destination must be new or empty. Edit its project and timeline files to
experiment, then use the usual preview and final render commands.

### markers.csv and analysis

Columns: `time_ms,at_frame,kind,label,origin`. Each row uses either integer
`time_ms` or integer output `at_frame`; populating both is rejected. Legacy marker
files remain supported. Use `vw marker 842 "Dancer entrance" --frames` to store
a frame position directly, with no millisecond conversion. Waveform drawing and
sorting use exact rational positions at project FPS. `vw marker` adds a row with `origin=manual`
and preserves other rows and annotation columns. Kinds can describe beats, lyrics,
drops, transitions, or any editor-defined event.

Audio analysis streams mono PCM into 20 ms RMS and peak windows. It writes a
linear-amplitude waveform with manual marker lines, envelope JSON, and heuristic
suggestions for prominent onsets, quiet passages, and changes in intensity.
These are suggestions rather than a reliable musical beat grid. The window
resolution is 20 ms; reported analysis duration rounds up to a complete window.

### Keyframe animation

Reference a JSON file from the optional `animation` column in `timeline.csv`.
Paths must stay inside the project, outside generated `cache/` and `output/`.
For example, save this as `animations/move.json`:

```json
{
  "version": 1,
  "time_base": "local",
  "time_unit": "frames",
  "properties": {
    "x": [{"time": 0, "value": -0.25}, {"time": 29, "value": 0.25}],
    "y": [{"time": 0, "value": 0}, {"time": 29, "value": 0.1}],
    "scale": [{"time": 0, "value": 0.5}, {"time": 29, "value": 1}],
    "rotation": [{"time": 0, "value": 0}, {"time": 29, "value": 90}],
    "opacity": [{"time": 0, "value": 0}, {"time": 10, "value": 1}],
    "saturation": [
      {"time": 0, "value": 0, "interpolation": "hold"},
      {"time": 15, "value": 1}
    ]
  }
}
```

```csv
asset,src_in_frame,src_out_frame,at_frame,nframes,animation
clip-001,0,30,0,30,animations/move.json
```

`version`, `time_base`, and `time_unit` default to `1`, `local`, and `frames`.
Times are nonnegative integers. `local` counts **output frames since the segment
starts**, independent of source trim and speed. `timeline` counts from timeline
frame zero, then subtracts the segment's rounded start frame. With `time_unit =
"ms"`, integer milliseconds convert once using exact project FPS and nearest-even
rounding, just like timeline times. Keys must resolve within the segment's visible
frames (`0` through `nframes - 1` locally). Preview and final use the same frames.

Keys may be written in any order. Duplicate properties, unknown fields, empty
tracks, nonfinite values, and keys that collide after rounding fail validation.
A key's `interpolation` controls the interval **from that key to the next**:
`linear` (default) interpolates by output frame, and `hold` keeps the earlier
value until the exact next key frame. At a key frame its value applies. Before
the first and after the last key, the nearest endpoint value holds. Rotation
interpolates numeric degrees without shortest-path wrapping.

| Property | Default | Bounds and units |
| --- | --- | --- |
| x, y | 0 | -10 to 10; offsets as fractions of canvas width/height, positive right/down |
| scale | 1 | 0.01 to 100; uniform multiplier |
| rotation | 0 | -36000 to 36000; clockwise degrees |
| opacity | 1 | 0 to 1; multiplies existing alpha and static opacity/fades |
| brightness | 0 | -1 to 1 |
| contrast | 1 | 0 to 3 |
| saturation | 1 | 0 to 3 |

Unkeyed properties use these neutral defaults. Source normalization, crop, static
quarter-turn rotation, fit, and the static effect preset run first. Masks multiply
fitted source alpha; static opacity and transitions also multiply coverage.
Animated treatment then applies on top of the static preset in BT.709 YUV, followed
by uniform scale and clockwise rotation about the **canvas center**, translation,
and animated opacity. The affine transform samples directly onto the fixed output
canvas with transparent out-of-bounds coverage and premultiplied bilinear sampling.
It never rescales a rotated bounding box to fit or clips an intermediate rotated
surface. Content outside the final canvas is clipped. Contain padding is part of
the transformed canvas; cover cropping and geometry rasterization have already
clipped their sources before animation.

Generated `shape:` layers accept `animation` with the same semantics. To animate
only a mask, put a JSON path in `mask_animation` alongside `mask`. Mask animation
uses the same evaluator and transforms, then extracts alpha and applies optional
inversion. Treatment changes mask color but not coverage. Layer animation moves
both media and its applied mask together. Animated masks multiply existing source
alpha, static opacity, and fades; they do not replace them.

Python authors can write the same schema or evaluate it directly:

```python
from vw.animation import Animation
from vw.project import Project, write_json
from vw.timeline import load
from vw.normalize import prepare

project = Project.load("my-edit")
keyframes = {
    "properties": {
        "x": [
            {"time": 0, "value": -0.25},
            {"time": 29, "value": 0.25},
        ]
    }
}
write_json(project.path("animations/move.json"), keyframes)
# Reference animations/move.json in the timeline row's animation column.
shot = load(project)[0]
prepared_path, cache_hit = prepare(project, shot)
curve = Animation.from_dict(keyframes, fps=project.fps, at=0, frames=30)
assert curve.value("x", 0) == -0.25
```

Animated shots have a separate content-addressed cache. Editing keys reuses static
normalization; moving a locally timed shot or changing its layer reuses animation
too. Moving a timeline-timed shot resolves its keys again and may invalidate the
animation or put keys outside its range. Manifests include the complete resolved
frame/value/interpolation data for both layer and mask, so changing keys changes
the edit identity. JSON whitespace and property ordering do not affect cache keys.
Animation uses CPU per-pixel sampling; high-resolution animated layers cost more
than static layers.

## Caching and diagnostics

Prepared shots use content-addressed keys including source bytes, the ffmpeg
version, target dimensions/fps, transforms, relevant effect settings, and a
pipeline version. Generated geometry keys include shape properties, target size,
Pillow version, and the rasterization policy. Referenced masks also participate
in shot cache keys. Moving a shot or changing its layer reuses preparation.
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

The public modules are `project`, `assets`, `audio`, `timeline`, `animation`, `geometry`, `normalize`,
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
