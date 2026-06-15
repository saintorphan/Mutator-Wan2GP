# Mutator — a Wan2GP plugin

<p align="center"><img src="assets/mutator_logo.png" alt="Mutator" width="640"></p>

A per-clip, single-track video editor living in its own tab. Load a clip, lay it
out on a draggable timeline of **segments**, give each segment its own edits,
then save it — in place or as a copy — or hand it off to another tool.

Mutator adds a **Mutator** tab to the Wan2GP UI. Load the clip you just rendered
(from the output gallery), upload one from disk, or receive one via **SendTo**.
Then trim, splice, rejoin, crop, resize, flip, change speed, reverse and
colour-correct — with full **undo / redo** — and ship the result.

## The v0.2 rebuild — three zones

The tab is one editor split into three vertical zones:

1. **Workspace** — the source `gr.Video` player, a compact tool row (no accordion
   stacks), the **draggable single-track timeline**, and a togglable **crop
   canvas**. The timeline is ONE ordered track of `Segment`s; it starts as a
   single segment spanning the whole source. **Click** a segment to select it —
   which loads *its own* edits into the tool row and the Result player. **Drag a
   segment's edges** to trim it.
2. **Result** — a `gr.Video` of the **selected** segment's render, plus an info
   readout (size / duration / which edits are active).
3. **Send** — Save in place / Save as copy, the embedded SendTo frame panel, and
   "Send edited clip".

### Per-clip edits

Every segment carries its **own** trim, crop, resize, flip H/V, speed, reverse
and colour grade — selecting it loads those into the tools, and editing mutates
only that segment. Each edit is a non-destructive description; `core/render.py`
turns a segment into pixels in one ffmpeg pass and caches the result by a content
signature (which folds in the source mtime and every edit), so an untouched
re-request is an instant cache hit and any change is a cache miss that
regenerates.

### Splice & Rejoin

- **Splice** razors the selected segment at the playhead into two halves that
  inherit every edit (the playhead, a timeline-second, is converted to a
  source-second within the clip by the clip's speed).
- **Rejoin** merges the selected segment with a contiguous **same-source**
  neighbour whose edits match (prefers the right neighbour, else the left).

### Draggable crop with aspect presets

The crop canvas is a self-contained `<iframe>` editor: an 8-handle rectangle over
the segment's **un-cropped source frame** (the bitmap is the native
`naturalWidth × naturalHeight`), with `free / 1:1 / 4:3 / 3:4 / 16:9 / 9:16`
aspect presets. It emits crop coordinates in **source pixels** (no baked image) —
the plugin rounds w/h to even (for libx264) and applies them to the selected
segment.

## Features

- **Load a clip** — from the output gallery selection, an upload, or a **SendTo**
  hand-off (drained from `state["mutator_inbox"]` on tab entry — an empty track
  loads the clip, a non-empty track appends it).
- **Draggable timeline** — click to select, drag segment edges to trim, scrub the
  playhead, with per-segment filmstrip backgrounds.
- **Per-segment edits** — trim, crop, resize (with lock + aspect presets), flip
  H/V, speed (0.1×–8×, audio kept in sync via chained atempo), reverse, and a six
  control colour grade (brightness / contrast / saturation / hue / warmth /
  gamma) with a one-click reset.
- **Splice / Rejoin** — razor at the playhead; merge contiguous same-source
  neighbours.
- **Undo / Redo** — a bounded history; a pure selection/playhead change never
  forks an undo state.
- **Save in place** — overwrites the selected segment's real source file (gallery
  clips and SendTo hand-offs that carry a real path). Uploads / temp sources have
  no canonical original, so they fall back to *Save as copy*.
- **Save as copy** — writes a new file into the Wan2GP outputs folder
  (`save_path`) with collision-safe naming (`<name>_edited.mp4`, then `(2)`, …).
- **Send out** — send the **frame at the playhead** as an image via the embedded
  **SendTo** panel, or send the whole **edited clip** to the Media Generator's
  **Continue Video** source (sets `video_source` + ensures `"V"` in
  `image_prompt_type`, then navigates to `media_gen`), or to any path-payload
  receiver such as Reel2Reel. Both routes degrade gracefully when SendTo isn't
  installed.

> **Unofficial / community plugin.** Not bundled with Wan2GP — install it
> yourself via the Plugin Manager's GitHub-URL flow below. Don't add it to
> `plugins.json` / the bundled set.

## Install

In Wan2GP, open the **Plugin Manager** tab → **Add plugin from GitHub URL**, and
paste:

```
https://github.com/saintorphan/Mutator-Wan2GP
```

The manager clones it into your `plugins/` folder, installs requirements, and
enables it. Restart (or reload the UI) and a **Mutator** tab appears in the
main tab bar.

<details>
<summary>Manual / dev install (instead of the Plugin Manager)</summary>

```bash
git clone https://github.com/saintorphan/Mutator-Wan2GP
ln -s "$(pwd)/Mutator-Wan2GP" /path/to/Wan2GP/plugins/Mutator-Wan2GP
```

Then add `"Mutator-Wan2GP"` to `enabled_plugins` in your `server_config` and
restart.
</details>

### Requirements

Mostly what Wan2GP already ships: `ffmpeg` / `ffprobe` (system binaries), plus a
couple of small libraries for thumbnail/frame work, and `imageio-ffmpeg` as a
last-resort way to locate an ffmpeg binary. No model downloads, no GPU.

## How it works

| Piece | Where |
|---|---|
| Tab + UI | full-tab plugin (`add_tab("Mutator", …)`); three-zone layout in `ui/suite.py` |
| Edit model | `core/model.py` — `Segment` + `Track` (single-track, undo/redo) |
| Rendering | `core/render.py` — one-pass `render_segment`, cache-keyed filmstrips, frame extraction |
| SendTo inbox | `core/inbox.py` drains `state["mutator_inbox"]` |
| FPS / frames | `core/ffmpeg.py: probe` (ffprobe) → host `get_video_info` fallback |
| Save naming | `core/paths.py` — render/thumb cache dirs + collision-safe copies |

### Bridge architecture

Two independent browser↔Python bridges, each a mount `gr.HTML` plus hidden pipes:

- **Timeline** — its JS ships via `add_custom_js` (the only path that runs;
  `<script>` in `gr.HTML` innerHTML never executes). It round-trips edit-JSON
  through `mut_tl_to_py` (JS→Py, debounced) and receives a `{seq, op, edit}`
  op-envelope through `mut_tl_from_py` (Py→JS, applied by a `.change(js=…)` hook,
  no server round-trip). A **monotonic `seq`** on the plugin lets the JS drop
  stale/replayed loads and freeze inbound loads during a drag.
- **Crop** — a self-contained `<iframe srcdoc>` editor. It writes
  `{seg_id, x, y, w, h}` source-pixel coords into `mut_crop_to_py` (JS→Py), and
  receives a source frame via the one-shot injector `mut_crop_from_py` (Py→JS).

### Kept features

- The logo banner and the cyan `#00d9ff` tab outline.
- The **SendTo receiver** (`mutator_inbox`) and the embedded SendTo **frame
  panel**.
- **"Send edited clip"** — Continue Video (`video_source` + `"V"` in
  `image_prompt_type` + navigate `media_gen`) and path receivers (e.g. Reel2Reel).
- **Save in place / Save as copy** and **Undo / Redo**.

## Notes

- Per-segment renders and filmstrips live under a local cache dir and are keyed on
  a content signature, so a trim/edit regenerates them and an unchanged segment is
  a cache hit. The cache is disposable and pruned on load.
- Saved copies land in the outputs **folder**; they appear in the on-screen
  gallery after the next gallery refresh / restart.
- "Save in place" is destructive — it overwrites the selected segment's source.
- `core/clipstate.py` was removed (replaced by `core/model.py`'s `Track` +
  `Track.snapshot/restore`); `ui/filmstrip.py` was removed (per-segment filmstrips
  now come from `core/render.filmstrip_for` and tile as the timeline clip
  background).

Author: saintorphan.
