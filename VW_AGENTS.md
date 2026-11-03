# vw agents — operational notes from the technomancer v2 migration

These notes supplement `verifiedtm/vw`'s repository `AGENTS.md` (which covers development) and `README.md` (which defines the current project spec). Verify all syntax against the version being used; these observations target `master` after PR #13 on 2026-11-03.

## Agent setup

- The project root contains `project.toml`, `assets.csv`, `timeline.csv`, `markers.csv`, and a `sources/` directory. All paths referenced in the project must resolve *inside* the project root. Absolute and escaping paths are rejected; a source symlink outside the root also resolves outside and is rejected. Import external files with `vw ingest`, or place/hardlink media in `sources/` before inventorying.
- From the repo, run `uv run vw --project /path/to/project timeline validate --json`, then `timeline visualize`, `render --preview`, `inspect output/preview.mp4 --json`, and finally `render --final`. Put `--json` before or after the subcommand if desired. `just test` is the quick suite, `just test-all` runs extended media tests.
- `audio_master` names an asset ID in `assets.csv` or a project-relative audio path. Video source audio is muted; the configured master is trimmed or padded to the declared edit duration.

## Frame timing — do not improvise

- `at_frame` is the **output timeline** frame, zero-based; `src_in_frame`/`src_out_frame` are **decoded presentation-order source** frame indices, NOT input timestamps or output frames. Source trims are half-open `[in,out)`. A frame row needs `asset,src_in_frame,src_out_frame,at_frame`; optional `nframes` fixes the exact output duration after speed conversion. Too-short source footage holds its final frame; too-long footage is trimmed.
- Use `project.duration_frames` to declare a fixed end, and represent blackouts as gaps. For technomancer the total is 1,933 frames at 24 fps; the blackout is output frames `[836,842)`; the following shot begins at frame 842. This is correct and must not be removed by a naive sequential concat.
- A row must not mix source-frame fields with `src_in/src_out/at` millisecond fields. Different rows may use different schemas. Integers in a frame CSV remain integers through CSV I/O.
- If converting from timestamps, consult *actual decoded presentation frames*, especially for VFR or mixed-fps material. `avg_frame_rate * seconds` is not a sufficiently reliable source index. Packet PTS are a useful provisional approximation but are not a guaranteed substitute for `vw`'s decoded frame index. Confirm suspect trims against render and `ffprobe -show_frames`.
- `markers.csv` can name manual markers with stable IDs. `at_frame` may use e.g. `marker:drop+3f`; animation JSON may use the same absolute marker references with `time_base="timeline"`. `ms` offsets and project-frame `f` offsets are resolved exactly and rounded once.

## Keyframes

- A timeline row's `animation` and `mask_animation` fields are project-relative JSON file paths. Properties include x/y, scale, rotation, opacity, brightness, contrast, saturation. `time_base` is `local` (frames since segment start) or `timeline`; `time_unit` is `frames` or `ms`. Local keys must resolve within `0..nframes-1`.
- `interpolation="hold"` at a key holds that key's value until the next; otherwise linear interpolation runs in output frame space. Animated opacity multiplies static opacity and fades; it does not replace alpha. Crop/fit/rotation are computed *before* animation; animated transforms operate on the fitted canvas.
- For a frame-accurate fade, use local frame keys. E.g. `[{"time":10,"value":1},{"time":38,"value":0}]` on a 39-frame shot reaches zero on its final frame, not after it ends.

## Scaling & composition

- Set project-level or shot-level `max_upscale` to a positive enlargement factor (`1`, `1.5`, `2`) or `unlimited`. Omitted row cap inherits project default. The factor is applied after autorotation, crop, explicit rotation and pixel-aspect correction.
- A capped `cover` shot does not magically fill the project frame: it crops any overshoot and leaves transparent padding where it cannot fill. Final render composites this padding over background/lower layers. Small clips may become deliberately inset. Preview placement is mapped from final placement, not refitted independently.
- `fit` supports `contain`, `cover`, `stretch`. `blend_mode` supports normal, screen, add, multiply, difference. `blend_color_space` defaults to encoded RGB; linear mode changes the appearance even of ordinary alpha blending. Avoid blanket grading; visual inconsistency among sources is often editorially intentional.
- The render pipeline normalizes video sources and caches prepared RGBA shots. Static source preparation precedes per-frame animated effects and composition. Generated shapes are canvas-based and ignore media upscale limits.

## Cache & verification

- Frame indexing is content-addressed by source bytes, stream/time base, index format and full ffmpeg/ffprobe version. Repeated loads reuse indexes. Long GOP sources can seek only after checksum verification against a full-decode reference; first useful long seek can be SLOW, warm seeks are the win. `VW_FRAME_DIAGNOSTICS=1` emits frame probe/decode/cache/fallback JSON on stderr. Do not misdiagnose an expensive first run as a hang.
- Run `timeline validate --json` before rendering; its per-shot presentation geometry identifies native dimensions, cap, `limited` and final position. Render manifests additionally provide actual preview/final rectangles and resolved animation keyframes.
- Prefer `just check-all` for changes to extraction/timing/scaling. Pixel inspections, exact decoded frame counts and deterministic A/B shot checks catch issues that an MP4 playback spot-check misses.

## Specific editorial reference: technomancer

- `legacy-v6.csv` is the *authority* for output `frame_start/nframes` and shot order. The v1 `vw` project is an asset map and partial composition reconstruction; never let rounded millisecond fields silently supersede the original output frames.
- Source `forest` matches SHA-256 `c84e5ffa312a87e685a00a3eda91cd23e30ddc3950f62ee0e9715db26b47bc73`, the video titled `S&P 500 Inclusion | Alex Karp's Message to Palantirians`. First scene begins at source 36s; later forest appearances are from the same media.
- The final-shot fade is editorial intent from original v6; the original v1 project omitted it. A local opacity animation has been added in v2. Legacy color grades were *not* ported; preserve varied source appearance by default.
- Per-shot `max_upscale` values in this migration are **tentative editorial choices**, not claims of exact v6 image geometry. Inspect the low-resolution inserts and revise based on the actual output.

## Questions that required repository archaeology

1. **What does a frame number address?** README `timeline.csv` section and PR #9: source-frame index is decoded presentation order, output start is project frame. Use rational frame calculations and explicit `nframes`.
2. **Why might a 240p insert display inset instead of filled?** README `max_upscale` and PR #11: capped `cover` permits transparent padding; inspect per-shot placement in validation JSON.
3. **Can markers drive animations?** README marker and keyframe sections and PR #12: manual named marker IDs, absolute marker references, project-frame offsets, exact rounding.
4. **Why is a late cut slow only the first time?** README cache diagnostics and PR #13: lazy checksum index and verification before bounded seeking.
5. **Why did an external media symlink fail?** `Project.path` in `src/vw/project.py` resolves paths and requires them to remain below root; hardlink or ingest instead.

Potential documentation improvement: put a minimal end-to-end example combining `at_frame`, `nframes`, `max_upscale`, a named marker and a local animation in a copyable project directory. A full `AGENTS.md` should also say explicitly that a preview with strategically inset low-res footage is correct, not necessarily a scaling bug.
