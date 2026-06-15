# Mutator — a Wan2GP plugin

A single-clip video editor, living in its own tab. Think of it as a *lite*
reel editor: load one clip, work it over with a frame-accurate mini-timeline
and a stack of quick edits, then save it — in place or as a copy — or hand it
off to another tool.

Mutator adds a **Mutator** tab to the Wan2GP UI. Load the clip you just
rendered (from the output gallery), upload one from disk, or receive one via
**SendTo**. Then trim, split, crop, resize, flip, change speed, and
colour-correct — with full **undo / redo** — and ship the result.

## Features

- **Load a clip** — three ways in:
  - **Load from preview selection** pulls the clip currently selected in the
    Wan2GP output gallery (reads the host's `state["gen"]` bookkeeping; audio
    picks are rejected).
  - **Upload** any video from disk.
  - **SendTo hand-off** — another plugin can send a clip path straight into
    Mutator's inbox.
- **Frame-accurate mini-timeline** — a horizontal filmstrip of evenly-spaced
  thumbnails sits under the preview. A dual-handle range slider (or two plain
  sliders when `gradio_rangeslider` isn't present) sets the in/out **frames**,
  with live in/out time readout and start/end frame thumbnails.
- **Trim & Split** — cut to the selected in/out range, or split at the
  playhead frame and keep the head or the tail. FPS comes from `ffprobe`'s
  exact rational frame rate (e.g. 23.976, not 24), with a host fallback; cuts
  land on real frames and the end frame is inclusive.
- **Transform** — flip horizontal / vertical.
- **Resize** — set width and/or height (leave one blank for an
  aspect-preserving auto dimension), with an optional lock and size presets.
- **Crop** — numeric x / y / width / height.
- **Speed** — re-time from 0.1× to 8×, with audio kept in sync (atempo is
  chained for factors outside ffmpeg's 0.5×–2× window).
- **Colour** — brightness, contrast, saturation, hue, gamma, and warmth, with
  a one-click reset to neutral.
- **Undo / Redo / Reset** — every edit writes a new working file and pushes
  onto an undo stack, so you can step back and forth freely or reset to the
  clip you started from.
- **Save in place** — overwrites the original file (for gallery clips and
  SendTo hand-offs that carry a real source path). Uploads have no canonical
  original, so they fall back to *Save as copy*.
- **Save as copy** — writes a new file into the Wan2GP outputs folder
  (`server_config["save_path"]`) with collision-safe naming
  (`<name>_edited.mp4`, then `(2)`, `(3)`, …).
- **Send out** — send the **current frame** (at the playhead) as an image via
  the embedded **SendTo** panel (Image Suite, the img2vid init/end slots, disk…),
  or send the whole **edited clip** to the Media Generator's **Continue Video**
  (`video_source`) continuation source, or to any path-payload receiver such as
  Reel2Reel. The clip and frame routes degrade gracefully when SendTo isn't
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

Mostly what Wan2GP already ships: `ffmpeg` / `ffprobe` (system binaries), plus
a couple of small libraries (`Pillow`, `numpy`) for filmstrip and thumbnail
compositing, and `imageio-ffmpeg` as a last-resort way to locate an ffmpeg
binary. The dual-handle trim slider (`gradio_rangeslider`) is host-provided and
optional — Mutator falls back to two plain sliders without it. No model
downloads, no GPU.

## How it works

| Piece | Where |
|---|---|
| Tab + UI | full-tab plugin (`add_tab("Mutator", …)`); layout in `ui/suite.py` |
| Selected clip | `state["gen"]["file_list"][state["gen"]["selected"]]` |
| SendTo inbox | `core/inbox.py` drains `state["mutator_inbox"]` |
| FPS / frames | `core/ffmpeg.py: probe` (ffprobe) → host `get_video_info` fallback |
| Trim / split | `core/trim.py` (`ffmpeg … libx264`, frame-accurate) |
| Edits | `core/ops.py` (flip / resize / crop / speed / colour, one-shot ffmpeg) |
| Undo stack | `core/clipstate.py: ClipSession` (held on the plugin instance) |
| Save naming | `core/paths.py` — collision-safe copies into the outputs folder |

## Notes

- Each edit writes a fresh working file under a local cache dir, which keeps the
  preview in sync and powers undo/redo. The cache is disposable.
- Saved copies land in the outputs **folder**; they appear in the on-screen
  gallery after the next gallery refresh / restart.
- "Save in place" is destructive — it overwrites the original file.
- Video is re-encoded with `libx264` (`-crf 18 -pix_fmt yuv420p`) on edit, so
  cuts and effects are frame-accurate at the cost of a re-encode.

Author: saintorphan.
